"""ChangeSet / ChangeFile / Hunk — 变更集模型。

负责:
- 记录本次会话中 AI 对每个文件的新旧内容
- 生成 unified diff 文本（仅依赖标准库 difflib）
- 解析为 Hunk 块（每块对应一个 ``@@ -a,b +c,d @@`` 段）
- 维护每文件 pending / accepted / rejected 状态
- 提供应用（写回文件）、回滚、分块应用以及统计信息
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import difflib
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Hunk
# ---------------------------------------------------------------------------
class Hunk:
    """一段 ``@@ ... @@`` 代码块。

    Attributes:
        old_start: 原文件起始行号（1-based）。
        old_lines: 原文件中该段占的行数。
        new_start: 新文件起始行号（1-based）。
        new_lines: 新文件中该段占的行数。
        header: 完整的 ``@@ -a,b +c,d @@ func_name`` 头部文本。
        lines: 解析后的行列表，每项为 ``(tag, line_text)``。
            tag 取值: ``' '`` 上下文, ``'-'`` 删除, ``'+'`` 新增。
    """

    def __init__(self, old_start: int, old_lines: int,
                 new_start: int, new_lines: int, header: str = "") -> None:
        self.old_start: int = old_start
        self.old_lines: int = old_lines
        self.new_start: int = new_start
        self.new_lines: int = new_lines
        self.header: str = header
        self.lines: List[Tuple[str, str]] = []

    def changed_line_count(self) -> int:
        """返回本段中 '-' 与 '+' 行的数量（不含上下文行）。"""
        return sum(1 for tag, _ in self.lines if tag in ("+", "-"))

    def added_lines(self) -> int:
        """返回本段新增行数。"""
        return sum(1 for tag, _ in self.lines if tag == "+")

    def removed_lines(self) -> int:
        """返回本段删除行数。"""
        return sum(1 for tag, _ in self.lines if tag == "-")

    def render(self) -> str:
        """将本段还原为 unified diff 文本片段（仅本段）。"""
        body = "\n".join(f"{tag}{line}" for tag, line in self.lines)
        return f"{self.header}\n{body}" if body else self.header

    def __repr__(self) -> str:
        return (f"Hunk(old={self.old_start},{self.old_lines} "
                f"new={self.new_start},{self.new_lines} "
                f"changes={self.changed_line_count()})")


# ---------------------------------------------------------------------------
# 辅助: 解析 unified diff -> Hunk 列表
# ---------------------------------------------------------------------------
_HUNK_HEADER_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(.*)$")


def _parse_hunks(diff_text: str) -> List[Hunk]:
    """将 ``difflib.unified_diff`` 输出的文本解析为 Hunk 列表。

    Args:
        diff_text: 完整的 unified diff 字符串。
            支持包含/不包含 ``--- / +++`` 头两行。

    Returns:
        解析出的 Hunk 列表；若没有变更则返回 ``[]``。
    """
    hunks: List[Hunk] = []
    current: Optional[Hunk] = None

    for raw in diff_text.splitlines():
        # 跳过文件头
        if raw.startswith(("--- ", "+++ ")):
            continue
        # 跳过 \\ No newline at end of file 之类的元信息
        if raw.startswith("\\"):
            continue

        header_match = _HUNK_HEADER_RE.match(raw)
        if header_match:
            old_start = int(header_match.group(1))
            old_lines = int(header_match.group(2)) if header_match.group(2) is not None else 1
            new_start = int(header_match.group(3))
            new_lines = int(header_match.group(4)) if header_match.group(4) is not None else 1
            tail = header_match.group(5).rstrip()
            current = Hunk(old_start, old_lines, new_start, new_lines, raw)
            # 保留 tail 以便展示 func name（完整 header 已保留在 raw 中）
            _ = tail
            hunks.append(current)
            continue

        if current is None:
            continue

        # 统一 diff 行: ' ', '-', '+'
        if not raw:
            # 空行视为上下文行
            current.lines.append((" ", ""))
            continue

        tag = raw[0]
        text = raw[1:] if len(raw) > 1 else ""
        if tag in (" ", "-", "+"):
            current.lines.append((tag, text))

    return hunks


# ---------------------------------------------------------------------------
# ChangeFile
# ---------------------------------------------------------------------------
class ChangeFile:
    """单个被修改文件的变更记录。

    Attributes:
        file_path: 目标文件的绝对或相对路径。
        old_content: 修改前的文件内容。
        new_content: 修改后的文件内容。
        reason: 此次修改的原因/描述（AI 给出）。
        status: ``pending`` / ``accepted`` / ``rejected``。
        hunks: 解析后的 ``Hunk`` 列表。
        timestamp: 创建时间戳（秒）。
    """

    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_REJECTED = "rejected"

    def __init__(self, file_path: str, old_content: str, new_content: str,
                 reason: str = "", timestamp: Optional[float] = None,
                 project_root: Optional[Path] = None) -> None:
        self.file_path: str = file_path
        self.old_content: str = old_content or ""
        self.new_content: str = new_content or ""
        self.reason: str = reason
        self.status: str = ChangeFile.STATUS_PENDING
        self.timestamp: float = float(timestamp) if timestamp is not None else time.time()
        self._project_root: Path = Path(project_root) if project_root else Path.cwd()
        self.hunks: List[Hunk] = self._compute_hunks()

    # ---- diff 计算 --------------------------------------------------------
    def _compute_hunks(self) -> List[Hunk]:
        """通过 ``difflib.unified_diff`` 生成文本并解析为 Hunk 列表。"""
        if self.old_content == self.new_content:
            return []

        old_lines = self.old_content.splitlines(keepends=False)
        new_lines = self.new_content.splitlines(keepends=False)

        # 统一 diff: 3 行上下文
        diff_gen = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=os.path.basename(self.file_path) or self.file_path,
            tofile=os.path.basename(self.file_path) or self.file_path,
            n=3,
        )
        diff_text = "\n".join(diff_gen)
        if not diff_text.strip():
            return []

        return _parse_hunks(diff_text)

    def unified_diff_text(self) -> str:
        """返回完整的 unified diff 文本（供 Diff 视图渲染）。"""
        if self.old_content == self.new_content:
            return ""

        old_lines = self.old_content.splitlines(keepends=False)
        new_lines = self.new_content.splitlines(keepends=False)
        diff_gen = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{self.file_path}",
            tofile=f"b/{self.file_path}",
            n=3,
        )
        return "\n".join(diff_gen)

    def _is_safe_path(self, path: str) -> Optional[Path]:
        """防止路径穿越与符号链接沙箱逃逸。

        返回解析后的安全绝对路径；若路径非法或不在 project_root 内则返回 None。
        """
        if not path:
            return None
        if os.path.isabs(path) or (len(path) >= 2 and path[1] == ":"):
            return None
        parts = path.replace("\\", "/").split("/")
        if not parts or ".." in parts:
            return None
        # 拒绝隐藏目录组件（如 .git / .hidden / .DS_Store 目录），防止向隐藏目录
        # 写入文件；但保留末尾 dotfile（如 .gitignore / .env / .dockerignore）。
        for part in parts[:-1]:
            if part.startswith("."):
                return None
        # 允许 dotfile（如 .gitignore, .env, .dockerignore），
        # 仅拒绝 ".." 目录穿越（已在上方检查）和空路径组件
        try:
            root = self._project_root.resolve()
            target = (root / path).resolve()
            target.relative_to(root)
        except (ValueError, OSError, RuntimeError):
            return None
        # 路径链上任何组件不得是符号链接
        current = root
        try:
            rel_parts = target.relative_to(root).parts
        except ValueError:
            return None
        for part in rel_parts:
            current = current / part
            try:
                if current.is_symlink():
                    return None
            except (OSError, RuntimeError):
                return None
        return target

    # ---- 应用 / 回滚 ------------------------------------------------------
    @staticmethod
    def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> bool:
        """原子写入：先写临时文件再 os.replace，避免半写。"""
        try:
            parent = path.parent
            parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=str(parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding=encoding, newline="", closefd=True) as f:
                    f.write(text)
                os.replace(tmp_name, str(path))
                return True
            except Exception:
                try:
                    os.unlink(tmp_name)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                return False
        except Exception:
            return False

    def apply(self) -> bool:
        """将 ``new_content`` 原子写回 ``file_path``，并将状态置为 accepted。

        Returns:
            写入成功返回 ``True``，任何错误返回 ``False``。
        """
        target = self._is_safe_path(self.file_path)
        if target is None:
            return False
        if self._atomic_write_text(target, self.new_content):
            self.status = ChangeFile.STATUS_ACCEPTED
            return True
        return False

    def revert(self) -> bool:
        """将 ``old_content`` 原子写回 ``file_path``，并将状态置为 rejected。

        Returns:
            写入成功返回 ``True``，任何错误返回 ``False``。
        """
        target = self._is_safe_path(self.file_path)
        if target is None:
            return False
        if self._atomic_write_text(target, self.old_content):
            self.status = ChangeFile.STATUS_REJECTED
            return True
        return False

    def apply_hunk(self, hunk_index: int) -> bool:
        """仅应用某个 hunk（基于当前 ``new_content`` 动态重写）。

        简化策略:
            - 若该文件已全部接受或拒绝，直接返回 False。
            - 否则将 hunk_index 标记为已应用（在内部保留已应用的 hunk 索引），
              当全部 hunk 被应用时直接调用 ``apply()``。
            - 对于"部分应用"的场景，基于 ``old_content`` 按行合并出"已应用 hunk
              的新内容"再写回文件。

        Args:
            hunk_index: 要应用的 hunk 在 ``self.hunks`` 中的下标。

        Returns:
            成功写入或状态变更返回 True。
        """
        if not (0 <= hunk_index < len(self.hunks)):
            return False
        if self.status != ChangeFile.STATUS_PENDING:
            return False

        applied = getattr(self, "_applied_hunks", None)
        if applied is None:
            applied = set()
            setattr(self, "_applied_hunks", applied)
        applied.add(hunk_index)

        # 若全部 hunk 已被应用，则直接 apply 整个文件
        if len(applied) >= len(self.hunks):
            return self.apply()

        # 部分应用: 基于 old_content，按 hunk 在 new_content 中的范围替换
        try:
            merged = _merge_hunks(self.old_content, self.hunks, applied)
        except (IndexError, ValueError, TypeError):
            return False
        target = self._is_safe_path(self.file_path)
        if target is None:
            return False
        # 部分应用不改变 status（仍视为 pending）
        return self._atomic_write_text(target, merged)

    # ---- 状态 / 统计 ------------------------------------------------------
    def is_pending(self) -> bool:
        return self.status == ChangeFile.STATUS_PENDING

    def is_accepted(self) -> bool:
        return self.status == ChangeFile.STATUS_ACCEPTED

    def is_rejected(self) -> bool:
        return self.status == ChangeFile.STATUS_REJECTED

    def summary(self) -> dict:
        """返回 ``{lines_added, lines_removed, hunks}`` 统计。"""
        added = sum(h.added_lines() for h in self.hunks)
        removed = sum(h.removed_lines() for h in self.hunks)
        return {
            "lines_added": added,
            "lines_removed": removed,
            "hunks": len(self.hunks),
        }

    def status_label(self) -> str:
        """返回面向 UI 的中文状态标签。"""
        return {
            ChangeFile.STATUS_PENDING: "待审",
            ChangeFile.STATUS_ACCEPTED: "已接受",
            ChangeFile.STATUS_REJECTED: "已拒绝",
        }.get(self.status, self.status)

    def __repr__(self) -> str:
        s = self.summary()
        return (f"ChangeFile({self.file_path!r} status={self.status} "
                f"+{s['lines_added']} -{s['lines_removed']} "
                f"hunks={s['hunks']})")


# ---------------------------------------------------------------------------
# 部分应用辅助：基于 old_content 按 hunk 合并出"已应用的新内容"
# ---------------------------------------------------------------------------
def _merge_hunks(old_content: str, hunks: List[Hunk],
                 applied_indices: set) -> str:
    """根据已应用的 hunk 索引，从 old_content 拼出最终内容。

    策略: 按 old_start 升序处理，输出:
        - 上一段结束到当前 hunk.old_start 之间的原内容
        - 若该 hunk 被应用: 输出该 hunk 中的 ' ' / '+' 行（去掉 '-'）
        - 若未应用: 输出该 hunk 中的 ' ' / '-' 行（保留原值，忽略 '+'）
        - 最后输出剩余 old_content
    """
    old_lines = old_content.splitlines(keepends=False)
    keep_trailing_newline = old_content.endswith("\n")

    # 按 old_start 排序（多数情况下已排序）
    ordered = sorted(enumerate(hunks), key=lambda pair: pair[1].old_start)

    out: List[str] = []
    cursor = 0  # 0-based 行索引
    total_old = len(old_lines)

    for idx, hunk in ordered:
        # hunk.old_start 是 1-based, 闭区间 [old_start, old_start+old_lines)
        # 边界校验：防止非法坐标导致负索引或越界
        # old_start=0, old_lines=0 表示新增文件（@@ -0,0 +1,N @@），需保留其新增行
        if hunk.old_lines < 0:
            continue
        if hunk.old_start <= 0:
            # 新增文件场景：从 hunk.lines 中提取 '+' 标记的行
            for tag, line in hunk.lines:
                if tag == "+":
                    out.append(line)
            continue
        start_0 = hunk.old_start - 1
        end_0 = start_0 + hunk.old_lines
        # 夹紧到有效范围，防止 cursor 越界
        if start_0 < 0:
            start_0 = 0
        if end_0 > total_old:
            end_0 = total_old

        # 输出 hunk 之前的原内容
        while cursor < start_0 and cursor < total_old:
            out.append(old_lines[cursor])
            cursor += 1

        if idx in applied_indices:
            # 应用: 输出 ' ' 与 '+' 行，跳过 '-'
            for tag, line in hunk.lines:
                if tag in (" ", "+"):
                    out.append(line)
        else:
            # 未应用: 输出 ' ' 与 '-' 行（即原内容），跳过 '+'
            for tag, line in hunk.lines:
                if tag in (" ", "-"):
                    out.append(line)

        cursor = end_0

    # 剩余原内容
    while cursor < total_old:
        out.append(old_lines[cursor])
        cursor += 1

    result = "\n".join(out)
    if keep_trailing_newline:
        result += "\n"
    return result


# ---------------------------------------------------------------------------
# ChangeSet
# ---------------------------------------------------------------------------
class ChangeSet:
    """全局变更管理器。

    以 ``file_path`` 为唯一键；相同路径重复调用 ``add_change`` 会覆盖。
    """

    def __init__(self, project_root: Optional[Path] = None) -> None:
        self._files: dict = {}
        self._project_root: Path = Path(project_root) if project_root else Path.cwd()

    # ---- 增删查 -----------------------------------------------------------
    def add_change(self, file_path: str, old_content: str, new_content: str,
                   reason: str = "") -> ChangeFile:
        """记录一次文件变更。

        若 ``file_path`` 已存在，将被覆盖（重置为 pending）。
        """
        cf = ChangeFile(file_path, old_content, new_content, reason=reason,
                        project_root=self._project_root)
        self._files[file_path] = cf
        return cf

    def remove(self, file_path: str) -> None:
        """从变更集中移除某文件（不写磁盘）。"""
        self._files.pop(file_path, None)

    def get_file(self, file_path: str) -> Optional[ChangeFile]:
        """按路径返回 ChangeFile；不存在则返回 None。"""
        return self._files.get(file_path)

    def has_file(self, file_path: str) -> bool:
        return file_path in self._files

    def all_files(self) -> List[ChangeFile]:
        """按 timestamp 升序返回所有 ChangeFile。"""
        return sorted(self._files.values(), key=lambda cf: cf.timestamp)

    def pending_files(self) -> List[ChangeFile]:
        """返回仍处于 pending 状态的 ChangeFile 列表。"""
        return [cf for cf in self.all_files() if cf.is_pending()]

    # ---- 批量操作 ---------------------------------------------------------
    def accept_all(self) -> int:
        """接受所有 pending 的变更，返回成功写入的文件数。"""
        count = 0
        for cf in self.all_files():
            if cf.is_pending() and cf.apply():
                count += 1
        return count

    def reject_all(self) -> int:
        """拒绝所有 pending 的变更，返回成功回滚的文件数。"""
        count = 0
        for cf in self.all_files():
            if cf.is_pending() and cf.revert():
                count += 1
        return count

    def clear(self) -> None:
        """清空变更集。"""
        self._files.clear()

    # ---- 统计 -------------------------------------------------------------
    def total_files(self) -> int:
        return len(self._files)

    def changed_files(self) -> int:
        """返回实际存在差异（至少有 1 个 hunk 或 new != old）的文件数。"""
        return sum(1 for cf in self._files.values()
                   if cf.old_content != cf.new_content)

    def total_hunks(self) -> int:
        return sum(len(cf.hunks) for cf in self._files.values())

    def stats(self) -> dict:
        """返回 ``{total, pending, accepted, rejected, hunks_total}`` 统计。"""
        total = len(self._files)
        pending = accepted = rejected = 0
        hunks_total = 0
        for cf in self._files.values():
            if cf.is_pending():
                pending += 1
            elif cf.is_accepted():
                accepted += 1
            elif cf.is_rejected():
                rejected += 1
            hunks_total += len(cf.hunks)
        return {
            "total": total,
            "pending": pending,
            "accepted": accepted,
            "rejected": rejected,
            "hunks_total": hunks_total,
        }

    def __len__(self) -> int:
        return len(self._files)

    def __iter__(self):
        return iter(self.all_files())

    def __contains__(self, file_path: str) -> bool:
        return file_path in self._files


# ---------------------------------------------------------------------------
# 便捷: 全局单例（可选使用）
# ---------------------------------------------------------------------------
_global_change_set: Optional[ChangeSet] = None
_global_change_set_lock = threading.Lock()


def get_global_change_set() -> ChangeSet:
    """返回进程级别的全局 ChangeSet 单例（线程安全）。"""
    global _global_change_set
    if _global_change_set is None:
        with _global_change_set_lock:
            if _global_change_set is None:
                _global_change_set = ChangeSet()
    return _global_change_set


__all__ = [
    "Hunk",
    "ChangeFile",
    "ChangeSet",
    "get_global_change_set",
]
