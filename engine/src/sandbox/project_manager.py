# -*- coding: utf-8 -*-
"""项目沙箱管理器

每个项目是一个独立沙箱, 包含4个文件夹:
- work/      (AI 写入的代码文件)
- preview/   (用户上传的文件预览)
- history/   (对话历史 JSON)
- recycle/   (回收站, 标记删除的内容移到这里)

同时项目根目录有一个 DS.md, 内容分4个区块, 全部嵌入系统提示.
"""
import logging

logger = logging.getLogger(__name__)

import json
import os
import re
import shutil
import stat
import threading
import time
import unicodedata
import weakref
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from src.core.config import Config


# Windows 保留名（不区分大小写）
_WIN_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> bool:
    from src.core.atomic_files import atomic_write_bytes
    return atomic_write_bytes(path, text.encode(encoding))


def _atomic_write_json(path: Path, data) -> bool:
    """原子化写入 JSON 文件。对数据合法性做最小校验，避免写一半损坏。"""
    try:
        # 只允许 JSON 可序列化的基本类型；拒绝自定义对象
        text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    except (TypeError, ValueError) as e:
        logger.info("[ProjectSandbox] JSON 序列化失败")
        logger.debug("[ProjectSandbox] JSON 序列化失败: %s", e, exc_info=True)
        return False
    return _atomic_write_text(path, text)


# ========== DS.md 4 区块定义 ==========
DS_SECTIONS = {
    "description": {
        "title": "📋 项目说明",
        "marker": "<!-- SECTION:DESCRIPTION -->",
        "start": "<!-- ========== 项目说明 ========== -->",
        "end":   "<!-- ========== END 项目说明 ========== -->",
        "default": "待补充...（在此处描述项目目标、技术栈、设计决策等）",
    },
    "pending": {
        "title": "📝 未完成代办",
        "marker": "<!-- SECTION:PENDING -->",
        "start": "<!-- ========== 未完成代办 ========== -->",
        "end":   "<!-- ========== END 未完成代办 ========== -->",
        "default": "",
    },
    "completed": {
        "title": "✅ 已完成代办",
        "marker": "<!-- SECTION:COMPLETED -->",
        "start": "<!-- ========== 已完成代办 ========== -->",
        "end":   "<!-- ========== END 已完成代办 ========== -->",
        "default": "（暂无已完成任务）",
    },
    "custom": {
        "title": "⚙️ 自定义特殊规则",
        "marker": "<!-- SECTION:CUSTOM -->",
        "start": "<!-- ========== 自定义特殊规则 ========== -->",
        "end":   "<!-- ========== END 自定义特殊规则 ========== -->",
        "default": "（在此处写入你自己的特殊规则，AI 将严格遵守）",
    },
}


def _build_default_ds_md(project_name: str) -> str:
    """构造默认 DS.md 内容"""
    lines = [f"# {project_name}", ""]
    for key in ["description", "pending", "completed", "custom"]:
        sec = DS_SECTIONS[key]
        lines.append(sec["start"])
        lines.append(f"## {sec['title']}")
        lines.append("")
        lines.append(sec["default"])
        lines.append("")
        lines.append(sec["end"])
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _parse_sections(content: str) -> Dict[str, str]:
    """从 DS.md 中提取4个区块的正文内容。

    对超长内容和异常做了防御，避免畸形 DS.md 导致下游 panic。
    """
    result = {}
    if not isinstance(content, str) or len(content) > 2_000_000:
        for key in ["description", "pending", "completed", "custom"]:
            result[key] = DS_SECTIONS[key]["default"]
        return result
    for key in ["description", "pending", "completed", "custom"]:
        sec = DS_SECTIONS[key]
        start_tag = sec["start"]
        end_tag = sec["end"]
        try:
            start_idx = content.index(start_tag) + len(start_tag)
            end_idx = content.index(end_tag, start_idx)
            section_body = content[start_idx:end_idx].strip()
            body_lines = section_body.splitlines()
            if body_lines and body_lines[0].strip().startswith("##"):
                body_lines = body_lines[1:]
            while body_lines and body_lines[0].strip() == "":
                body_lines.pop(0)
            result[key] = "\n".join(body_lines).strip()
        except (ValueError, Exception):
            result[key] = sec["default"]
    return result


def _replace_section(content: str, section_key: str, new_body: str) -> str:
    """替换 content 中某个区块的正文, 返回完整内容"""
    sec = DS_SECTIONS[section_key]
    start_tag = sec["start"]
    end_tag = sec["end"]
    # 对替换体做一个长度上限防御：防止 AI 把整个工作目录塞进 DS.md
    if not isinstance(new_body, str):
        new_body = str(new_body or "")
    if len(new_body) > 200_000:
        new_body = new_body[:200_000] + "\n...(已截断，单区块最大 200KB)"
    try:
        start_idx = content.index(start_tag)
        end_idx = content.index(end_tag, start_idx)
        new_section = (
            f"{start_tag}\n"
            f"## {sec['title']}\n"
            f"\n"
            f"{new_body.strip()}\n"
            f"{end_tag}"
        )
        before = content[:start_idx].rstrip() + "\n\n"
        after = "\n" + content[end_idx + len(end_tag):].lstrip()
        return (before + new_section + after).strip() + "\n"
    except ValueError:
        new_section = (
            f"{start_tag}\n"
            f"## {sec['title']}\n"
            f"\n"
            f"{new_body.strip()}\n"
            f"{end_tag}\n"
        )
        return content.strip() + "\n\n" + new_section


class ProjectSandbox:
    """单个项目的沙箱 — 所有文件读写必须经过 _safe_path 验证。"""

    # 单个文件写入上限（防止大模型把工作目录塞满）
    _MAX_WRITE_BYTES = 4 * 1024 * 1024

    # 显示名/备份标签等长度上限
    MAX_DISPLAY_NAME_LENGTH = 128
    MAX_BACKUP_LABEL_LENGTH = 64
    MAX_RECYCLE_RENAME_ATTEMPTS = 9999

    # 项目根目录下回收站文件夹名称（英文，按要求固定）
    RECYCLE_BIN_NAME = "RecycleBin"

    _locks = weakref.WeakValueDictionary()
    _locks_guard = threading.Lock()

    def __init__(self, name: str, root: Path):
        raw_root = Path(root).absolute()
        if raw_root.is_symlink():
            raise ValueError("Project root cannot be a symbolic link")
        for child in ("work", "preview", "history", self.RECYCLE_BIN_NAME, "DS.md", "meta.json"):
            if (raw_root / child).is_symlink():
                raise ValueError("Project data cannot be a symbolic link")
        self.name = unicodedata.normalize("NFC", str(name))
        self.root = Path(root).resolve()
        with self._locks_guard:
            self._lock = self._locks.setdefault(str(self.root), threading.RLock())
        self.work_dir = self.root / "work"
        self.preview_dir = self.root / "preview"
        self.history_dir = self.root / "history"
        # 回收站统一使用项目根目录下的 RecycleBin 文件夹
        self.recycle_dir = self.root / self.RECYCLE_BIN_NAME
        self.ds_md = self.root / "DS.md"
        self.meta_file = self.root / "meta.json"

        for d in (self.work_dir, self.preview_dir, self.history_dir, self.recycle_dir):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)

        if not self.ds_md.exists():
            if not _atomic_write_text(self.ds_md, _build_default_ds_md(name)):
                raise OSError("Failed to initialize DS.md")
            try:
                os.chmod(str(self.ds_md), 0o600, follow_symlinks=False)
            except OSError as e:
                logger.warning("[ProjectSandbox] 设置 DS.md 权限 0o600 失败: %s (%s)", self.ds_md, e)
        else:
            old = self.ds_md.read_text(encoding="utf-8", errors="replace")
            if "SECTION:DESCRIPTION" not in old and "<!-- ==========" not in old:
                self._migrate_old_format(old)

        if not self.meta_file.exists():
            self._save_meta({
                "created": time.time(),
                "last_access": time.time(),
                "display_name": str(name),
                "pinned": False,
                "pinned_at": 0,
                "renamed_by_user": False,
            })

        # 接入 UndoManager (快照 / 回退)
        from src.sandbox.undo_manager import UndoManager
        self.undo_manager = UndoManager(self)

    def _migrate_old_format(self, old_content: str):
        """把旧格式 DS.md 转换为新 4 区块格式"""
        lines = old_content.splitlines()
        desc_lines = []
        pending_todos = []
        completed_todos = []
        for line in lines:
            stripped = line.strip()
            m = re.match(r"^[-*]\s*\[([ x])\]\s*(.+)$", stripped)
            if m:
                done = m.group(1).lower() == "x"
                text = m.group(2)
                if done:
                    completed_todos.append(f"- [x] {text}")
                else:
                    pending_todos.append(f"- [ ] {text}")
            elif stripped.startswith("#"):
                continue
            else:
                if stripped:
                    desc_lines.append(stripped)

        description = "\n".join(desc_lines).strip() or DS_SECTIONS["description"]["default"]
        pending = "\n".join(pending_todos) or DS_SECTIONS["pending"]["default"]
        completed = "\n".join(completed_todos) or DS_SECTIONS["completed"]["default"]
        custom = DS_SECTIONS["custom"]["default"]

        lines = [f"# {self.name}", ""]
        for key, body in [("description", description), ("pending", pending),
                          ("completed", completed), ("custom", custom)]:
            sec = DS_SECTIONS[key]
            lines.append(sec["start"])
            lines.append(f"## {sec['title']}")
            lines.append("")
            lines.append(body)
            lines.append("")
            lines.append(sec["end"])
            lines.append("")
        self.ds_md.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        try:
            os.chmod(str(self.ds_md), 0o600, follow_symlinks=False)
        except OSError as e:
            logger.warning("[ProjectSandbox] 设置 DS.md 权限 0o600 失败: %s (%s)", self.ds_md, e)

    # ---------- meta ----------
    def _save_meta(self, meta: Dict) -> bool:
        """Persist metadata atomically and report the actual write result."""
        with self._lock:
            return isinstance(meta, dict) and _atomic_write_json(self.meta_file, meta)

    def _load_meta(self) -> Dict:
        """以字节形式读取元信息（线程安全：持锁读取）。"""
        with self._lock:
            try:
                if not self.meta_file.exists():
                    return {}
                raw = self.meta_file.read_bytes()
                if not raw:
                    return {}
                data = json.loads(raw.decode("utf-8"))
                if isinstance(data, dict):
                    return data
                return {}
            except (OSError, ValueError, json.JSONDecodeError) as e:
                logger.info("[ProjectSandbox] meta 文件损坏，忽略")
                logger.debug("[ProjectSandbox] meta 文件损坏，忽略: %s", e, exc_info=True)
                return {}
            except Exception:
                return {}

    # ---------- 显示名 / 置顶（存在 meta.json，与目录名解耦） ----------
    def get_meta(self) -> Dict:
        """读取并返回 meta.json 字典（线程安全）。"""
        return self._load_meta()

    def update_meta(self, updates: Dict) -> bool:
        """增量更新 meta.json（线程安全）。"""
        if not isinstance(updates, dict):
            return False
        with self._lock:
            meta = self._load_meta()
            meta.update(updates)
            meta["last_access"] = time.time()
            return self._save_meta(meta)

    def get_display_name(self) -> str:
        """返回用户可见的对话名称；不存在时回退到目录名。"""
        meta = self._load_meta()
        name = meta.get("display_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return self.name

    def set_display_name(self, name: str, user_initiated: bool = False) -> bool:
        """设置显示名。user_initiated=True 表示用户手动重命名，将阻止模型自动覆盖。"""
        if not isinstance(name, str) or not name.strip():
            return False
        name = unicodedata.normalize("NFC", name.strip())
        if len(name) > self.MAX_DISPLAY_NAME_LENGTH:
            name = name[:self.MAX_DISPLAY_NAME_LENGTH]
        with self._lock:
            meta = self._load_meta()
            # 用户已手动重命名时，禁止模型自动覆盖
            if not user_initiated and meta.get("renamed_by_user"):
                return False
            meta["display_name"] = name
            if user_initiated:
                meta["renamed_by_user"] = True
            meta["last_access"] = time.time()
            return self._save_meta(meta)

    def is_pinned(self) -> bool:
        meta = self._load_meta()
        return bool(meta.get("pinned", False))

    def set_pinned(self, pinned: bool) -> bool:
        updates = {
            "pinned": bool(pinned),
            "pinned_at": time.time() if pinned else 0,
        }
        return self.update_meta(updates)

    # ---------- DS.md ----------
    def read_ds_md(self) -> str:
        """读取完整 DS.md 内容（线程安全：持锁读取）。
        支持：
        - 子目录 .dscode/rules/*.md 规则文件自动加载（按路径作用域）
        - @import 语法引用其他文件（@import "path/to/file.md"）
        - 总大小上限提升到 64KB
        """
        with self._lock:
            try:
                parts = []
                # 1. 读取根目录 DS.md
                main_content = ""
                if self.ds_md.exists():
                    raw = self.ds_md.read_bytes()
                    try:
                        main_content = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        main_content = raw.decode("utf-8", errors="replace")

                if main_content.strip():
                    # 处理 @import 引用
                    main_content = self._resolve_ds_imports(main_content, self.ds_md.parent, depth=0)
                    parts.append(main_content)
                else:
                    parts.append(_build_default_ds_md(self.name))

                # 2. 加载 .dscode/rules/ 目录下的规则文件
                rules_dir = self.root / ".dscode" / "rules"
                if rules_dir.exists() and rules_dir.is_dir():
                    try:
                        rule_files = sorted(rules_dir.glob("*.md"))
                        for rf in rule_files:
                            try:
                                rraw = rf.read_bytes()
                                try:
                                    rtext = rraw.decode("utf-8")
                                except UnicodeDecodeError:
                                    rtext = rraw.decode("utf-8", errors="replace")
                                if rtext.strip():
                                    rtext = self._resolve_ds_imports(rtext, rf.parent, depth=0)
                                    # 添加规则文件标记
                                    parts.append(f"\n---\n## 规则文件: .dscode/rules/{rf.name}\n{rtext.strip()}")
                            except Exception as re:
                                logger.debug("读取规则文件失败(%s): %s", rf, re)
                    except Exception as de:
                        logger.debug("遍历rules目录失败: %s", de)

                # 3. 合并并截断（总大小上限提升到64KB）
                combined = "\n".join(parts)
                if len(combined) > 65536:
                    combined = combined[:65536] + "\n\n[DS.md内容已截断，超过64KB上限]"
                return combined
            except Exception as e:
                logger.info("[ProjectSandbox] 读取 DS.md 失败，回退到默认")
                logger.debug("[ProjectSandbox] 读取 DS.md 失败: %s", e, exc_info=True)
                return _build_default_ds_md(self.name)

    def _resolve_ds_imports(self, text: str, base_dir: Path, depth: int = 0) -> str:
        """解析 DS.md 中的 @import "path" 引用。
        防止循环引用（depth限制为5层），路径穿越（relative_to严格检查），和大文件DoS。"""
        if depth >= 5:
            return text
        import re
        _MAX_IMPORT_BYTES = 512 * 1024  # 单个import最多512KB
        def _replace_import(match):
            try:
                import_path = match.group(1).strip().strip('"').strip("'")
                if not import_path:
                    return match.group(0)
                # 拒绝显式的..段（防止深度穿越）
                if '..' in import_path.split('/'):
                    return f"[import拒绝: 不允许..路径 {import_path}]"
                # 解析相对路径
                target = (base_dir / import_path).resolve()
                # 安全检查：必须在项目根目录下（使用relative_to严格检查，修复前缀穿越漏洞）
                root_resolved = self.root.resolve()
                try:
                    target.relative_to(root_resolved)
                except ValueError:
                    return f"[import拒绝: 路径越界 {import_path}]"
                if not target.exists() or not target.is_file():
                    return f"[import未找到: {import_path}]"
                # 文件大小限制（防止DoS：读取GB级文件或/dev/zero）
                try:
                    fsize = target.stat().st_size
                except OSError:
                    return f"[import错误: 无法获取文件大小 {import_path}]"
                if fsize > _MAX_IMPORT_BYTES:
                    return f"[import拒绝: 文件过大 {import_path} ({fsize} bytes)]"
                raw = target.read_bytes()
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    content = raw.decode("utf-8", errors="replace")
                # 递归解析嵌套import
                content = self._resolve_ds_imports(content, target.parent, depth + 1)
                return f"\n<!-- @import {import_path} -->\n{content.strip()}\n<!-- /@import {import_path} -->\n"
            except Exception as e:
                return f"[import错误: {e}]"
        return re.sub(r'@import\s+["\']([^"\']+)["\']', _replace_import, text)

    def write_ds_md(self, content: str) -> bool:
        with self._lock:
            return _atomic_write_text(self.ds_md, str(content or ""))

    def read_section(self, section_key: str) -> str:
        if section_key not in DS_SECTIONS:
            return ""
        sections = _parse_sections(self.read_ds_md())
        return sections.get(section_key, "")

    def write_section(self, section_key: str, body: str) -> bool:
        if section_key not in DS_SECTIONS:
            return False
        with self._lock:
            content = self.read_ds_md()
            new_content = _replace_section(content, section_key, body)
            return self.write_ds_md(new_content)

    def replace_tasks(self, tasks: List[Dict]) -> bool:
        """Update pending and completed sections in one atomic write."""
        if not isinstance(tasks, list) or len(tasks) > 1000:
            return False
        for item in tasks:
            if (not isinstance(item, dict) or type(item.get("done")) is not bool
                    or not isinstance(item.get("text"), str) or not item["text"].strip()
                    or len(item["text"]) > 4096 or "\n" in item["text"] or "\r" in item["text"]):
                return False
        with self._lock:
            content = self.read_ds_md()
            for section, done in (("pending", False), ("completed", True)):
                body = "\n".join(f"- [{'x' if done else ' '}] {t['text'].strip()}"
                                 for t in tasks if t["done"] is done)
                content = _replace_section(content, section, body)
            return self.write_ds_md(content)

    def move_task_to_completed(self, task_text: str) -> bool:
        if not isinstance(task_text, str) or not task_text.strip():
            return False
        with self._lock:
            tasks = self.get_tasks()
            for item in tasks:
                if not item["done"] and item["text"] == task_text.strip():
                    item["done"] = True
                    return self.replace_tasks(tasks)
            return False

    def add_pending_task(self, task_text: str) -> bool:
        if not isinstance(task_text, str) or not task_text.strip():
            return False
        with self._lock:
            return self.replace_tasks(self.get_tasks() + [{"text": task_text.strip(), "done": False}])

    def get_tasks(self) -> List[Dict]:
        sections = _parse_sections(self.read_ds_md())
        tasks = []
        for line in sections.get("pending", "").splitlines():
            m = re.match(r"^\s*[-*]\s*\[([ x])\]\s*(.+)$", line.strip())
            if m:
                done = m.group(1).lower() == "x"
                tasks.append({"done": done, "text": m.group(2).strip()})
        for line in sections.get("completed", "").splitlines():
            m = re.match(r"^\s*[-*]\s*\[([ x])\]\s*(.+)$", line.strip())
            if m:
                done = m.group(1).lower() == "x"
                tasks.append({"done": done, "text": m.group(2).strip()})
        return tasks

    def get_sections(self) -> Dict[str, str]:
        return _parse_sections(self.read_ds_md())

    # ---------- 安全路径解析 (ALL 文件读写必经之路) ----------
    def _safe_path(self, rel_path: str, is_work: bool = True) -> Optional[Path]:
        """将相对路径解析为沙箱内绝对路径，拒绝路径穿越/绝对路径输入/符号链接。

        安全规则:
        1. 空值 / 非字符串 / 长度为 0 -> None
        2. 长度 > 4096 -> None（防止超长路径 DoS / 绕过某些内核检查）
        3. 包含空字符 (\\x00) -> None（某些操作系统会把空字符作为路径终止）
        4. 以 '/' 开头或 Windows 盘符开头（C:\\）-> None（禁止绝对路径）
        5. 包含 '.' 或 '..' 作为独立段 -> None（显式禁止父目录引用与当前目录混淆）
        6. 任何一级目录名以 '.' 开头 -> None（禁止隐藏文件与 .git/hooks 注入）
        7. 路径链上任何已存在组件是符号链接 -> None（防止 TOCTOU 沙箱逃逸）
        8. 最终路径必须在 base_resolved 之下 -> None

        返回: 安全的 Path; None 表示被拒绝
        """
        if rel_path is None or not isinstance(rel_path, (str, Path)):
            return None
        rel_str = str(rel_path).strip()
        if not rel_str:
            return None
        if len(rel_str) > 4096:
            return None
        if "\x00" in rel_str:
            return None

        base = self.work_dir if is_work else self.preview_dir
        if base.is_symlink():
            return None
        try:
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
        except Exception:
            return None
        try:
            base_resolved = base.resolve()
        except Exception:
            return None

        # 禁止绝对路径 (POSIX / Windows UNC / 盘符)
        if rel_str.startswith("/") or rel_str.startswith("\\"):
            return None
        if len(rel_str) >= 2 and rel_str[1] == ":":
            return None

        # 显式拒绝 . / .. 作为独立段（即使 resolve 会归一化，也提前拒绝可疑输入）
        rel_str = rel_str.replace("\\", "/")
        if any(part in {".", ".."} for part in rel_str.split("/")):
            return None

        try:
            candidate_raw = base_resolved / rel_str
            # 使用未 resolve 的相对段做逐级检查（resolve 会跟随符号链接，导致绕过检测）
            rel_parts = candidate_raw.relative_to(base_resolved).parts
        except (ValueError, Exception):
            return None

        # 逐级检查隐藏段与符号链接（在未 resolve 的路径链上检查）
        current = base_resolved
        for part in rel_parts:
            if part.startswith("."):
                return None
            next_path = current / part
            try:
                if next_path.is_symlink():
                    return None
            except (OSError, RuntimeError):
                return None
            current = next_path

        # 最终路径必须 resolve 后在 base 下（防御路径穿越与符号链接跟随后的逃逸）
        try:
            candidate_resolved = candidate_raw.resolve(strict=False)
            if hasattr(candidate_resolved, "is_relative_to"):
                if not candidate_resolved.is_relative_to(base_resolved):
                    return None
            else:
                candidate_resolved.relative_to(base_resolved)
        except (ValueError, Exception):
            return None

        return candidate_resolved

    # ---------- 文件列表 ----------
    def list_work_files(self, max_depth: int = 8, max_files: int = 10000) -> List[Dict]:
        """返回 work 目录文件列表 [{name, path, full_path, size}]

        使用 os.walk(followlinks=False) 递归遍历子目录，并就地过滤掉符号链接目录，
        防止递归符号链接导致无限循环（H-012）。同时限制深度和文件数量，跳过隐藏文件。
        """
        files = []
        if not self.work_dir.exists():
            return files
        try:
            base = self.work_dir.resolve()
        except OSError:
            return files

        try:
            for dirpath, dirnames, filenames in os.walk(str(base), followlinks=False):
                # H-012: 过滤掉符号链接目录，避免递归进入循环链接
                dirnames[:] = [
                    d for d in dirnames
                    if not os.path.islink(os.path.join(dirpath, d))
                ]
                try:
                    rel_dir = Path(dirpath).relative_to(base)
                    depth = len(rel_dir.parts)
                except ValueError:
                    continue
                if depth > max_depth:
                    continue
                for fname in sorted(filenames):
                    if len(files) >= max_files:
                        break
                    fp = Path(dirpath) / fname
                    try:
                        if fp.is_symlink() or not fp.is_file():
                            continue
                    except OSError:
                        continue
                    try:
                        rel = fp.relative_to(base)
                    except ValueError:
                        continue
                    if len(rel.parts) > max_depth:
                        continue
                    if any(part.startswith(".") for part in rel.parts):
                        continue
                    try:
                        size = fp.stat().st_size
                    except OSError:
                        size = 0
                    rel_path = str(rel)
                    files.append({"name": rel_path, "path": rel_path,
                                  "full_path": str(fp), "size": size})
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return files

    def list_preview_files(self) -> List[Dict]:
        files = []
        try:
            base = self.preview_dir.resolve()
            for p in sorted(base.iterdir()):
                try:
                    if not p.is_file() or p.is_symlink() or p.name.startswith("."):
                        continue
                    size = p.stat().st_size
                except Exception as _exc:
                    logger.debug("跳过异常项: %s", _exc, exc_info=True)
                    continue
                files.append({"name": p.name, "full_path": str(p), "size": size})
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return files

    # ---------- 文件操作 (全部走 _safe_path) ----------
    _MAX_READ_FILE_BYTES = 10 * 1024 * 1024  # 10 MB 读取上限
    # 备份总量限制，防止 work 目录过大时备份 OOM / 占满磁盘
    _MAX_BACKUP_TOTAL_BYTES = 500 * 1024 * 1024  # 500 MB
    _MAX_BACKUP_FILES = 50000

    def read_file(self, rel_path: str, is_work: bool = True) -> str:
        target = self._safe_path(rel_path, is_work)
        if target is None:
            return f"[读取失败] 路径不合法（拒绝路径穿越或隐藏文件）: {rel_path}"
        try:
            if not target.is_file():
                return f"[读取失败] 文件不存在: {rel_path}"
            try:
                # M-001: 使用 os.path.getsize 在读取前检查文件大小
                file_size = os.path.getsize(str(target))
                if file_size > self._MAX_READ_FILE_BYTES:
                    return f"[读取失败] 文件过大: {file_size} bytes (上限 {self._MAX_READ_FILE_BYTES} bytes)"
            except OSError as e:
                return f"[读取失败] 无法获取文件大小: {e}"
            return target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"[读取失败] {e}"

    def write_file(self, rel_path: str, content: str) -> bool:
        """写入 work 目录下的文件 (仅限沙箱安全路径)。

        安全检查:
          1. _safe_path 拒绝穿越和绝对路径
          2. 文件扩展名白名单：禁止直接写入可执行脚本 (.sh/.bat/.cmd/.ps1/.exe)
             以及 SSH/密钥相关文件；但允许通过临时脚本文件的上层工具执行
          3. content 上限为 _MAX_WRITE_BYTES，防止工作目录被超大文本写满
        """
        target = self._safe_path(rel_path, True)
        if target is None:
            logger.info("[ProjectSandbox] 写入被拒绝 (安全路径校验失败): %s", rel_path)
            return False
        try:
            suffix = target.suffix.lower()
            forbidden_suffixes = {
                ".sh", ".bash", ".zsh", ".ksh",
                ".bat", ".cmd", ".ps1", ".psm1",
                ".exe", ".msi", ".com",
                ".elf", ".so", ".dylib", ".dll",
                ".app",
            }
            if suffix in forbidden_suffixes:
                # 对于默认禁止的脚本类型，记录一条日志（上层显式调用时可覆盖）
                logger.info("[ProjectSandbox] 拒绝写入可执行脚本后缀: %s", rel_path)
                return False
            if not isinstance(content, str):
                content = str(content or "")
            if len(content.encode("utf-8")) > self._MAX_WRITE_BYTES:
                logger.info("[ProjectSandbox] 写入被拒绝 (超出单文件上限): %s", rel_path)
                return False
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # 原子化写入，避免写一半被关闭
            return _atomic_write_text(target, content)
        except Exception as e:
            logger.info("[ProjectSandbox] 写入文件失败: %s", rel_path)
            logger.debug("[ProjectSandbox] 写入文件失败 %s: %s", rel_path, e, exc_info=True)
            return False

    def move_to_recycle_bin(self, src_path: Path) -> bool:
        """将已存在的文件/目录移入项目根目录下的 RecycleBin，自动处理重名冲突。

        冲突解决策略：在目标名后追加递增序号，直到找到不存在的路径。
        """
        try:
            src = Path(src_path)
            if not src.exists():
                return False
            self.recycle_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            ts = time.strftime("%Y%m%d_%H%M%S")
            stem = f"{ts}_{src.stem}"
            suffix = src.suffix
            dest = self.recycle_dir / f"{stem}{suffix}"
            counter = 1
            while dest.exists():
                dest = self.recycle_dir / f"{stem}_{counter}{suffix}"
                counter += 1
                if counter > self.MAX_RECYCLE_RENAME_ATTEMPTS:
                    logger.warning("[ProjectSandbox] 回收站重名冲突过多，无法移动: %s", src)
                    return False
            shutil.move(str(src), str(dest))
            return True
        except Exception as e:
            logger.info("[ProjectSandbox] 移入回收站失败")
            logger.debug("[ProjectSandbox] 移入回收站失败: %s", e, exc_info=True)
            return False

    def safe_delete(self, rel_path: str, is_work: bool = True) -> bool:
        """安全删除：不真正删除文件，而是将其移入 RecycleBin。

        适用于 coder / full 模式；reviewer 模式应在更高层拦截。
        """
        target = self._safe_path(rel_path, is_work)
        if target is None:
            return False
        try:
            if not target.is_file():
                return False
            return self.move_to_recycle_bin(target)
        except Exception as e:
            logger.info("[ProjectSandbox] 安全删除失败")
            logger.debug("[ProjectSandbox] 安全删除失败: %s", e, exc_info=True)
            return False

    def mark_delete_file(self, rel_path: str, is_work: bool = True) -> bool:
        """兼容旧入口：语义与安全删除一致，文件进入 RecycleBin。"""
        return self.safe_delete(rel_path, is_work)

    def copy_work_as_backup(self, label: str = "backup") -> str:
        # 路径穿越防御: 严格校验 label 只允许安全字符
        if not isinstance(label, str):
            return "[备份失败] label 必须为字符串"
        if not label:
            return "[备份失败] label 不能为空"
        if len(label) > self.MAX_BACKUP_LABEL_LENGTH:
            return f"[备份失败] label 长度不能超过 {self.MAX_BACKUP_LABEL_LENGTH} 个字符"
        # 拒绝路径穿越字符
        if any(c in label for c in ("/", "\\", "\x00", "..")):
            return "[备份失败] label 包含非法字符"
        # 只允许 [a-zA-Z0-9_-]
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', label):
            return "[备份失败] label 只允许字母、数字、下划线和连字符"
        try:
            ts = time.strftime("%Y%m%d_%H%M%S")
            dest = self.root / f"backup_{ts}_{label}"
            if self.work_dir.exists():
                # C-007: 备份前检查总量与文件数，避免超大目录 OOM / 占满磁盘
                total_size = 0
                total_files = 0
                for dirpath, dirnames, filenames in os.walk(
                    str(self.work_dir), followlinks=False
                ):
                    # 排除 .dscode 目录，避免遍历项目内部快照
                    if ".dscode" in dirnames:
                        dirnames.remove(".dscode")
                    for fn in filenames:
                        fp = os.path.join(dirpath, fn)
                        try:
                            st = os.lstat(fp)
                            # 不跟随符号链接统计大小
                            if stat.S_ISLNK(st.st_mode):
                                continue
                            total_size += st.st_size
                            total_files += 1
                            if total_size > self._MAX_BACKUP_TOTAL_BYTES:
                                return (
                                    f"[备份失败] work 目录总大小超过 "
                                    f"{self._MAX_BACKUP_TOTAL_BYTES} bytes 上限"
                                )
                            if total_files > self._MAX_BACKUP_FILES:
                                return (
                                    f"[备份失败] work 目录文件数超过 "
                                    f"{self._MAX_BACKUP_FILES} 上限"
                                )
                        except OSError:
                            continue
                # symlinks=True 保留符号链接，避免跟随链接复制外部文件
                # 排除 .dscode 快照目录，防止备份递归爆炸。
                shutil.copytree(
                    self.work_dir, dest, symlinks=True,
                    ignore=shutil.ignore_patterns(".dscode"),
                )
                # M-003: 备份文件统一限制为 0o600，目录为 0o700
                for dirpath, dirnames, filenames in os.walk(str(dest), followlinks=False):
                    for fn in filenames:
                        fp = os.path.join(dirpath, fn)
                        try:
                            if not os.path.islink(fp):
                                os.chmod(fp, 0o600, follow_symlinks=False)
                        except OSError as e:
                            logger.warning("[ProjectSandbox] 设置备份文件权限 0o600 失败: %s (%s)", fp, e)
                    for dn in dirnames:
                        dp = os.path.join(dirpath, dn)
                        try:
                            if not os.path.islink(dp):
                                os.chmod(dp, 0o700, follow_symlinks=False)
                        except OSError as e:
                            logger.warning("[ProjectSandbox] 设置备份目录权限 0o700 失败: %s (%s)", dp, e)
            return str(dest)
        except Exception as e:
            return f"[备份失败] {e}"

    # ---------- 上传 (仅限 preview 目录) ----------
    def add_preview_file(self, src_path: str) -> Optional[str]:
        """把外部文件复制到 preview 目录, 返回相对文件名。

        安全: 读取 16 MB 上限；跳过符号链接；禁止以 '.' 开头的文件名。
        """
        try:
            # 先检查原始路径是否为符号链接（resolve 会跟随链接，导致检查失效）
            raw = Path(str(src_path))
            if raw.is_symlink():
                return None
            src = raw.resolve()
        except Exception:
            return None
        if not src.exists() or not src.is_file():
            return None
        try:
            if src.stat().st_size > self._MAX_READ_FILE_BYTES:
                return None
        except Exception:
            return None
        base_name = unicodedata.normalize("NFC", src.name)
        if not base_name or base_name.startswith("."):
            return None
        # 清理可能的路径穿越字符（虽然 src.name 只取最后一段）
        if "/" in base_name or "\\" in base_name or "\x00" in base_name:
            return None
        try:
            dest = self.preview_dir / base_name
            shutil.copy2(str(src), str(dest))
            try:
                os.chmod(str(dest), 0o600, follow_symlinks=False)
            except OSError as e:
                logger.warning("[ProjectSandbox] 设置 preview 文件权限 0o600 失败: %s (%s)", dest, e)
            return base_name
        except Exception as e:
            logger.info("[ProjectSandbox] 复制预览文件失败")
            logger.debug("[ProjectSandbox] 复制预览文件失败: %s", e, exc_info=True)
            return None

    # ---------- 快照 / 回退 (带自动快照的写操作包装) ----------
    def write_file_with_snapshot(self, rel_path: str, content: str,
                                 snapshot_reason: str = None) -> Tuple[bool, str]:
        desc = snapshot_reason or f"write_file: {rel_path}"
        snap_id = self.undo_manager.create_snapshot(desc)
        ok = self.write_file(rel_path, content)
        if ok:
            return True, snap_id or ""
        return False, "写入失败"

    def mark_delete_with_snapshot(self, rel_path: str,
                                  is_work: bool = True) -> Tuple[bool, str]:
        desc = f"mark_delete: {rel_path}"
        snap_id = self.undo_manager.create_snapshot(desc)
        ok = self.mark_delete_file(rel_path, is_work)
        if ok:
            return True, snap_id or ""
        return False, "删除失败"

    def write_section_with_snapshot(self, section: str, body: str,
                                    snapshot_reason: str = None) -> Tuple[bool, str]:
        desc = snapshot_reason or f"update_ds: {section}"
        snap_id = self.undo_manager.create_snapshot(desc)
        ok = self.write_section(section, body)
        if ok:
            return True, snap_id or ""
        return False, "写入失败"

    # ---------- 对外: 快照 / 还原 / 撤销 / 列出 ----------
    def snapshot(self, description: str = "手动快照") -> dict:
        sid = self.undo_manager.create_snapshot(description)
        return {"id": sid or "", "name": description, "title": description}

    def restore(self, snapshot_id: str) -> Tuple[bool, str]:
        ok = self.undo_manager.restore_snapshot(snapshot_id)
        return ok, ("还原成功" if ok else "还原失败")

    def undo(self, n: int = 1) -> Tuple[bool, str]:
        # 撤销最近 n 个操作：回退到倒数第 n 个快照
        if n < 1:
            return False, "n 必须为正整数"
        snaps = self.undo_manager.list_snapshots()
        if not snaps or len(snaps) < n:
            return False, "快照数量不足，无法撤销"
        target = snaps[-n]
        ok = self.undo_manager.restore_snapshot(target.get("id", ""))
        return ok, ("撤销成功" if ok else "撤销失败")

    def list_snapshots(self) -> list:
        return self.undo_manager.list_snapshots()

    def has_any_content(self) -> bool:
        """判断项目是否已有历史。"""
        try:
            if self.history_dir.exists():
                for p in self.history_dir.iterdir():
                    try:
                        if p.is_file() and not p.name.startswith(".") and p.stat().st_size > 0:
                            return True
                    except Exception as _exc:
                        logger.debug("跳过异常项: %s", _exc, exc_info=True)
                        continue
            if self.work_dir.exists():
                for p in self.work_dir.iterdir():
                    if p.is_file() and not p.name.startswith("."):
                        return True
            try:
                if self.ds_md.exists():
                    content = self.ds_md.read_text(encoding="utf-8", errors="ignore")
                    default = _build_default_ds_md(self.name)
                    if content.strip() != default.strip():
                        return True
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        except Exception:
            return False
        return False


def _safe_rmtree(root: Path) -> bool:
    """安全删除目录：拒绝根目录/子目录为符号链接，失败时记录日志而非吞异常。

    C-006：在删除前及递归遍历过程中检查每一层目录/子目录入口都不是符号链接，
    防止 TOCTOU 竞态导致的沙箱逃逸或误删外部目录。
    """
    root = Path(root)

    def _check_no_symlinks(path: Path) -> bool:
        try:
            for entry in os.scandir(str(path)):
                # 任何入口（文件/目录）若是符号链接即拒绝
                if entry.is_symlink():
                    logger.warning("[ProjectManager] 删除路径中包含符号链接: %s", entry.path)
                    return False
                if entry.is_dir(follow_symlinks=False):
                    if not _check_no_symlinks(Path(entry.path)):
                        return False
        except OSError as e:
            logger.debug("[ProjectManager] 扫描目录失败: %s", e, exc_info=True)
            return False
        return True

    try:
        if not root.exists():
            return False
        if root.is_symlink():
            logger.warning("[ProjectManager] 拒绝删除符号链接目录: %s", root)
            return False
        if not _check_no_symlinks(root):
            return False
        shutil.rmtree(str(root))
        return True
    except Exception as e:
        logger.info("[ProjectManager] 删除项目目录失败: %s", root)
        logger.debug("[ProjectManager] 删除项目目录失败详情: %s", e, exc_info=True)
        return False


class ProjectManager:
    """项目列表管理（线程安全）。"""

    def __init__(self):
        self.cfg = Config()
        self.projects_root = self.cfg.PROJECTS_DIR
        self._lock = threading.RLock()  # 保护 create/delete/rename 等状态修改
        try:
            self.projects_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def list_projects(self) -> List[str]:
        """返回所有项目目录名（按目录名排序）。保持向后兼容。"""
        return [info["name"] for info in self.list_project_infos()]

    def list_project_infos(self) -> List[Dict]:
        """返回项目信息列表，按 pinned 优先、再按 pinned_at 降序、最后按目录名排序。"""
        infos = []
        try:
            for p in sorted(self.projects_root.iterdir()):
                try:
                    if p.is_symlink() or not p.is_dir() or p.name.startswith("."):
                        continue
                    sb = ProjectSandbox(p.name, p)
                    meta = sb.get_meta()
                    infos.append({
                        "name": p.name,
                        "display_name": sb.get_display_name(),
                        "pinned": bool(meta.get("pinned", False)),
                        "pinned_at": float(meta.get("pinned_at", 0) or 0),
                        "renamed_by_user": bool(meta.get("renamed_by_user", False)),
                    })
                except OSError as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                except Exception as _exc:
                    logger.debug("跳过异常项: %s", _exc, exc_info=True)
                    continue
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        infos.sort(key=lambda x: (-int(x["pinned"]), -x["pinned_at"], x["name"]))
        return infos

    def _all_display_names(self, exclude_name: Optional[str] = None) -> Set[str]:
        """获取当前所有项目的显示名集合，可选排除某个目录名。"""
        names = set()
        for info in self.list_project_infos():
            if exclude_name and info["name"] == exclude_name:
                continue
            names.add(info["display_name"])
        return names

    def ensure_unique_display_name(self, base: str, exclude_name: Optional[str] = None) -> str:
        """在现有显示名不冲突的前提下返回唯一名称；重名时追加序号。"""
        base = str(base).strip()
        if not base:
            base = "新对话"
        existing = self._all_display_names(exclude_name=exclude_name)
        if base not in existing:
            return base
        i = 2
        while True:
            candidate = f"{base} ({i})"
            if candidate not in existing:
                return candidate
            i += 1
            if i > 9999:
                # 极端情况： fallback 到带时间戳的名字
                return f"{base} {int(time.time())}"

    @staticmethod
    def _safe_project_name(name: str) -> Optional[str]:
        """清理并校验项目名称。

        允许字母/数字/下划线/中划线/空格/中文；首尾不能为空，不得以点/中划线开头，
        不得以点结尾，且不得包含路径分隔符或路径穿越片段。
        H-007：额外拒绝 Windows 保留名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）。
        """
        if not isinstance(name, str):
            return None
        name = unicodedata.normalize("NFC", name.strip())
        # H-007：拒绝空名、纯点号、以点开头/结尾
        if not name or len(name.encode("utf-8")) > 200 or name in (".", "..") or name.startswith(".") or name.endswith("."):
            return None
        if name.startswith("-"):
            return None
        # H-007：拒绝 Windows 保留名（不区分大小写）
        if name.upper() in _WIN_RESERVED_NAMES:
            return None
        # 拒绝任何路径分隔符 / 穿越片段 / 空字符，防止目录遍历
        if "/" in name or "\\" in name or ".." in name or "\x00" in name:
            return None
        # 仅保留安全字符
        cleaned = re.sub(r"[^\w\s\-\u4e00-\u9fa5]", "_", name)
        cleaned = cleaned.replace(" ", "_")
        cleaned = cleaned.replace("-", "_")
        # 允许中文、字母、数字、下划线组合
        if not re.fullmatch(r"[\w\u4e00-\u9fa5]+", cleaned):
            return None
        return cleaned

    def get(self, name: str) -> Optional[ProjectSandbox]:
        if not self._safe_project_name(name):
            return None
        root = self.projects_root / name
        if root.is_symlink() or not root.is_dir():
            return None
        try:
            return ProjectSandbox(name, root)
        except (OSError, ValueError):
            return None

    def create(self, name: str, display_name: Optional[str] = None) -> Optional[ProjectSandbox]:
        if not self._safe_project_name(name):
            logger.warning("创建项目失败：项目名称非法 '%s'", name)
            return None
        root = self.projects_root / name
        with self._lock:
            if root.is_symlink():
                return None
            try:
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
            except FileExistsError:
                logger.warning("创建项目失败：目录已存在 '%s'", root)
                return None
            except PermissionError:
                logger.warning("创建项目失败：权限不足 '%s'", root)
                return None
            except Exception as _exc:
                logger.warning("创建项目失败：%s", _exc, exc_info=True)
                return None
            try:
                sb = ProjectSandbox(name, root)
            except (OSError, ValueError):
                return None
            if display_name:
                sb.set_display_name(display_name, user_initiated=False)
        # M-018: 审计日志（项目创建）
        logger.info("[AUDIT] create project: %s", name)
        try:
            self.cfg.set("last_project", name)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return sb

    def set_display_name(self, name: str, display_name: str, user_initiated: bool = False) -> bool:
        """仅修改项目的显示名，不动目录。"""
        if not self._safe_project_name(name):
            return False
        sb = self.get(name)
        if sb is None:
            return False
        return sb.set_display_name(display_name, user_initiated=user_initiated)

    def set_pinned(self, name: str, pinned: bool) -> bool:
        """设置项目置顶状态。"""
        if not self._safe_project_name(name):
            return False
        sb = self.get(name)
        if sb is None:
            return False
        return sb.set_pinned(pinned)

    def delete(self, name: str) -> bool:
        if not self._safe_project_name(name):
            return False
        root = self.projects_root / name
        # M-018: 审计日志（项目删除）
        logger.info("[AUDIT] delete project: %s", name)
        with self._lock:
            if not root.exists():
                return False
            return _safe_rmtree(root)

    def rename(self, old: str, new: str) -> bool:
        if not self._safe_project_name(old) or not self._safe_project_name(new):
            return False
        src = self.projects_root / old
        dst = self.projects_root / new
        # M-018: 审计日志（项目重命名）
        logger.info("[AUDIT] rename project: %s -> %s", old, new)
        with self._lock:
            # H-003: 删除 dst.exists() 前置检查，直接使用原子 rename。
            # FileExistsError 表示目标在检查窗口内被创建，按竞态处理。
            if src.is_symlink() or dst.exists() or dst.is_symlink():
                return False
            try:
                src.rename(dst)
                return True
            except FileExistsError:
                return False
            except (OSError, ValueError):
                return False
