# -*- coding: utf-8 -*-
"""代码分析工具。

实现：
- summary_project / analyse / find_function 等基础扫描；
- 被 tool_executor 引用的高级分析方法（analyze_file / find_imports /
  show_call_chain / code_metrics / show_dependencies / project_brief /
  file_intel / find_related / analyze_symbol / impact_analysis /
  all_symbols / complexity_report / risk_files）。

安全策略：
1. 所有入口 path 先 realpath 归一化；拒绝离开 sandbox.work_dir 或不在其下的路径；
2. 扫描目录设深度上限；扫描文件设总数上限；
3. 文件扩展名白名单限制；
4. 读取时设字节上限，防止对大文件读取耗尽内存；
5. 不使用 eval/exec，对 call.get("keyword") 做字符串清洗与截断。
"""

import logging

logger = logging.getLogger(__name__)

import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_MAX_FILE_BYTES = 2 * 1024 * 1024     # 单文件读取上限 2MB
_MAX_TOTAL_FILES = 2000            # 扫描总文件数上限
_MAX_SCAN_DEPTH = 8             # 递归最大深度
_MAX_KEYWORD_LEN = 256         # keyword 最大长度
_MAX_SYMBOL_LEN = 256          # symbol 最大长度
_MAX_LINE_LEN = 300            # 单行展示最大字符数
_MAX_OUT_LINES = 500           # 单次分析返回行数上限

# 可分析扩展名白名单
ALLOWED_SUFFIXES = {
    ".py", ".pyw",
    ".js", ".jsx", ".ts", ".tsx",
    ".java", ".kt", ".kts",
    ".cs", ".cpp", ".c", ".cc", ".cxx", ".h", ".hpp", ".hh",
    ".go", ".rs", ".rb", ".php", ".swift",
    ".html", ".htm", ".css", ".scss", ".sass",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".md", ".txt", ".rst",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".sql",
}

# 需跳过（拒绝分析）的目录名
_SKIP_DIRNAMES = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", "venv",
    ".venv", "dist", "build", "target", "vendor",
    ".DS_Store",
}

# 用于粗略 import 扫描的关键字
_IMPORT_PREFIXES = (
    "import ",
    "from ",
    "require(",
    "const ",
    "let ",
    "var ",
    "#include ",
)


# 用于粗略标识符提取的预编译正则（避免每次循环内重新编译）
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]{1,80}")


class CodeAnalysisTools:
    """代码分析：summary / find_function 等。"""

    def __init__(self, executor):
        self._exec = executor

    # ---------- 辅助 ----------
    def _sandbox_root(self) -> Optional[Path]:
        try:
            sandbox = self._exec.sandbox
        except Exception:
            sandbox = None
        try:
            work = getattr(sandbox, "work_dir", None) or "."
            return Path(work).resolve()
        except Exception:
            return None

    @staticmethod
    def _clean_text(raw: Any, max_len: int) -> str:
        """清洗 keyword/desc 等用户可控字符串。"""
        if raw is None:
            return ""
        if not isinstance(raw, str):
            try:
                raw = str(raw)
            except Exception:
                return ""
        out: List[str] = []
        for ch in raw:
            o = ord(ch)
            if o == 0:
                continue
            if o < 32 and ch not in ("\n", "\r", "\t"):
                continue
            if o == 0x7f:
                continue
            out.append(ch)
        s = "".join(out).strip()
        data = s.encode("utf-8", errors="replace")
        if len(data) > max_len:
            data = data[:max_len]
            s = data.decode("utf-8", errors="ignore")
        return s

    def _sanitize_target(self, raw_path: Optional[str]) -> Optional[Path]:
        """规范化并校验目标路径（相对 work_dir 或绝对均需落在 work 下）。"""
        root = self._sandbox_root()
        if root is None:
            return None
        # 未指定默认扫描整个 sandbox
        if raw_path is None or raw_path == "":
            return root
        cleaned = self._clean_text(str(raw_path), 512)
        if not cleaned:
            return root
        try:
            if os.path.isabs(cleaned):
                candidate = Path(cleaned).resolve()
            else:
                candidate = (root / cleaned).resolve()
        except Exception:
            return None
        # 必须落在 root 之内
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    def _iter_safe_files(self, base: Path):
        """安全地枚举 base 下的所有可分析文件。"""
        root = self._sandbox_root()
        if root is None:
            return
        try:
            root_real = root.resolve()
        except Exception:
            return
        try:
            base_real = base.resolve()
            if not (base_real == root_real or
                    str(base_real).startswith(str(root_real) + os.sep)):
                return
        except Exception:
            return

        total_files = 0
        stack = [(base_real, 0)]
        while stack and total_files < _MAX_TOTAL_FILES:
            current, depth = stack.pop()
            if depth > _MAX_SCAN_DEPTH:
                continue
            try:
                with os.scandir(str(current)) as scandir_it:
                    entries = list(scandir_it)
            except (PermissionError, OSError) as _exc:
                logger.debug("跳过异常项: %s", _exc, exc_info=True)
                continue
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in _SKIP_DIRNAMES:
                            continue
                        if entry.name.startswith(".") and entry.name not in (".", ".."):
                            continue
                        stack.append((Path(entry.path), depth + 1))
                        continue
                    if entry.is_file(follow_symlinks=False):
                        p = Path(entry.path)
                        suffix = p.suffix.lower()
                        if suffix not in ALLOWED_SUFFIXES:
                            continue
                        try:
                            st = entry.stat(follow_symlinks=False)
                            if st.st_size > _MAX_FILE_BYTES:
                                continue
                        except OSError as _exc:
                            logger.debug("跳过异常项: %s", _exc, exc_info=True)
                            continue
                        yield p
                        total_files += 1
                        if total_files >= _MAX_TOTAL_FILES:
                            return
                except (OSError, PermissionError) as _exc:
                    logger.debug("跳过异常项: %s", _exc, exc_info=True)
                    continue

    @staticmethod
    def _read_safe(path: Path) -> str:
        """最多读取 _MAX_FILE_BYTES 字节，按 utf-8 解码，失败退回 cp1252。"""
        try:
            with open(path, "rb") as f:
                data = f.read(_MAX_FILE_BYTES)
        except (OSError, PermissionError):
            return ""
        try:
            return data.decode("utf-8", errors="replace")
        except Exception:
            try:
                return data.decode("latin-1", errors="replace")
            except Exception:
                return ""

    def _rel(self, path: Path) -> str:
        root = self._sandbox_root()
        try:
            if root is not None:
                return str(path.relative_to(root.resolve()))
        except ValueError as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return str(path)

    # ---------- 基础工具方法 ----------
    def analyse(self, call) -> Tuple[bool, str]:
        """分析一个文件 / 整个项目：返回简要统计。"""
        target_raw = (call.get("file") or call.get("path") or call.get("dir") or ""
                      if isinstance(call, dict) else "")
        target = self._sanitize_target(target_raw)
        if target is None:
            return False, "[analyse] 目标路径非法或不在沙箱内"

        if target.is_file():
            text = self._read_safe(target)
            lines = text.count("\n") + (0 if text.endswith("\n") or text == "" else 1)
            non_empty = sum(1 for l in text.splitlines() if l.strip())
            return True, (
                f"[analyse] file={target.name}\n"
                f"bytes={min(len(text.encode('utf-8')), _MAX_FILE_BYTES)}, lines={lines}, "
                f"non_empty={non_empty}\n---\n"
                f"{text[:4000]}"
            )

        # 目录：收集扩展名统计
        stats: Dict[str, Dict[str, int]] = {}
        total = 0
        for p in self._iter_safe_files(target):
            total += 1
            ext = p.suffix.lower()
            s = stats.setdefault(ext, {"files": 0, "lines": 0})
            text = self._read_safe(p)
            s["files"] += 1
            s["lines"] += text.count("\n") + (0 if text.endswith("\n") or text == "" else 1)
        report_lines = []
        for ext, s in sorted(stats.items(), key=lambda kv: -kv[1]["files"]):
            report_lines.append(f"{ext}: files={s['files']}, lines={s['lines']}")
        return True, (
            f"[analyse] root={target}\n"
            f"files_scanned={total}\n" + "\n".join(report_lines[:200])
        )

    def find_function(self, call) -> Tuple[bool, str]:
        """按关键字 / 函数名搜索文件。"""
        if not isinstance(call, dict):
            return False, "[find_function] 参数无效"
        keyword = self._clean_text(
            call.get("keyword") or call.get("name") or "", _MAX_KEYWORD_LEN)
        target_raw = call.get("path") or call.get("dir") or call.get("root") or ""
        target = self._sanitize_target(target_raw)
        if target is None:
            return False, "[find_function] 目标路径非法"
        if not keyword:
            return False, "[find_function] 缺少 keyword"

        # 修复：当 target 是单个文件时，_iter_safe_files 内部 os.scandir
        # 会失败并静默返回 0 个文件。需与 find_imports 一致，先判断 is_file()。
        if target.is_file():
            files_to_scan = [target]
        else:
            files_to_scan = list(self._iter_safe_files(target))

        matches: List[str] = []
        hit_limit = 100
        for p in files_to_scan:
            text = self._read_safe(p)
            if not text:
                continue
            lower = text.lower()
            needle = keyword.lower()
            if needle in lower:
                lines = text.splitlines()
                for i, line in enumerate(lines):
                    if needle in line.lower():
                        matches.append(f"{self._rel(p)}:{i+1}: {line.strip()[:200]}")
                        break
                if len(matches) >= hit_limit:
                    break
        body = "\n".join(matches[:hit_limit])
        return True, (
            f"[find_function] keyword={keyword}\n"
            f"hits={len(matches)}\n"
            f"{body[:6000]}"
        )

    def summary_project(self, call=None) -> Tuple[bool, str]:
        """项目摘要：返回 analyse(call={}) 的精简结果。"""
        return self.analyse(call or {})

    # ---------- 被 tool_executor 引用的分析入口 ----------
    def analyze_file(self, call) -> Tuple[bool, str]:
        """等价于 analyse(call)，用于 tool_executor 里 tool == 'analyze_file'。"""
        return self.analyse(call or {})

    def find_imports(self, call) -> Tuple[bool, str]:
        """提取单个文件 / 整个项目的 import 语句。"""
        if not isinstance(call, dict):
            return False, "[find_imports] 参数无效"
        target_raw = (call.get("file") or call.get("path") or
                      call.get("dir") or "" if isinstance(call, dict) else "")
        target = self._sanitize_target(target_raw)
        if target is None:
            return False, "[find_imports] 目标路径非法"

        if target.is_file():
            files_to_scan = [target]
        else:
            files_to_scan = list(self._iter_safe_files(target))
            files_to_scan = files_to_scan[:200]

        lines_out: List[str] = []
        for p in files_to_scan:
            text = self._read_safe(p)
            if not text:
                continue
            found = []
            for raw_line in text.splitlines():
                s = raw_line.strip()
                if not s:
                    continue
                if any(s.startswith(prefix) for prefix in _IMPORT_PREFIXES):
                    found.append(s[:_MAX_LINE_LEN])
            if found:
                lines_out.append(f"# {self._rel(p)}")
                lines_out.extend(found[:100])
        return True, (
            f"[find_imports] scanned={len(files_to_scan)}\n"
            f"{chr(10).join(lines_out[:_MAX_OUT_LINES])}"
        )

    def show_call_chain(self, call) -> Tuple[bool, str]:
        """围绕一个符号粗略展示调用链（字面匹配近似）。"""
        if not isinstance(call, dict):
            return False, "[show_call_chain] 参数无效"
        symbol = self._clean_text(call.get("symbol") or "", _MAX_SYMBOL_LEN)
        if not symbol:
            return False, "[show_call_chain] 缺少 symbol"
        try:
            depth = int(call.get("depth") or 3)
        except (TypeError, ValueError):
            depth = 3
        depth = max(1, min(depth, 5))

        # 第 0 层：找到 symbol 的定义位置
        defs = []
        callers_by_level: List[List[str]] = [[] for _ in range(depth)]
        pattern_symbol = symbol  # 不使用 re.compile，避免 DoS / ReDoS
        # 但我们用 str.find 精确匹配
        current_tokens: List[str] = [pattern_symbol]
        seen_tokens = {pattern_symbol}

        file_symbols_cache: Dict[str, Dict[str, List[str]]] = {}

        root = self._sanitize_target("")
        if root is None:
            return False, "[show_call_chain] sandbox root 未就绪"

        all_files = list(self._iter_safe_files(root))

        # 先预缓存每文件的"疑似定义 / 疑似调用"
        for p in all_files:
            text = self._read_safe(p)
            if not text:
                continue
            defined: List[str] = []
            used: List[str] = []
            for raw_line in text.splitlines():
                stripped = raw_line.strip()
                if not stripped:
                    continue
                # 粗略提取标识符（大小写敏感）
                for tok in _IDENTIFIER_RE.findall(stripped):
                    used.append(tok)
                    # 定义位置
                    if (stripped.startswith("def ") or
                            stripped.startswith("class ") or
                            stripped.startswith("function ") or
                            stripped.startswith("const ") or
                            stripped.startswith("let ")):
                        if tok in stripped.split()[1:3]:
                            defined.append(tok)
            file_symbols_cache[self._rel(p)] = {"defined": defined, "used": used}

        # 找定义
        for rel, sym in file_symbols_cache.items():
            if pattern_symbol in sym["defined"]:
                defs.append(rel)

        # BFS 扩散调用者
        for level in range(depth):
            new_tokens: List[str] = []
            for rel, sym in file_symbols_cache.items():
                for token in current_tokens:
                    if token in sym["used"]:
                        if level == 0 and token != pattern_symbol:
                            # 为避免直接匹配关键字，不在这里过滤
                            pass
                        callers_by_level[level].append(f"{rel} 调用 {token}")
                        # 把该文件中定义的符号作为下一层候选
                        for defn in sym["defined"]:
                            if defn not in seen_tokens:
                                seen_tokens.add(defn)
                                new_tokens.append(defn)
            # 去重
            callers_by_level[level] = list(dict.fromkeys(callers_by_level[level]))
            current_tokens = new_tokens[:50]

        lines_out = [f"[show_call_chain] symbol={pattern_symbol}, depth={depth}"]
        if defs:
            lines_out.append("定义位置（粗略）:")
            for d in defs[:20]:
                lines_out.append(f"  - {d}")
        for i, lvl in enumerate(callers_by_level):
            lines_out.append(f"第 {i+1} 层调用者（前 50 条，共 {len(lvl)} 条）:")
            for entry in lvl[:50]:
                lines_out.append(f"  - {entry}")

        return True, "\n".join(lines_out[:_MAX_OUT_LINES])

    def code_metrics(self) -> Tuple[bool, str]:
        """返回项目的粗略代码度量。"""
        root = self._sanitize_target("")
        if root is None:
            return False, "[code_metrics] sandbox root 未就绪"
        by_ext: Dict[str, Dict[str, int]] = defaultdict(lambda: {"files": 0, "lines": 0})
        total_files = 0
        total_lines = 0
        for p in self._iter_safe_files(root):
            text = self._read_safe(p)
            lines = text.count("\n") + (0 if text.endswith("\n") or text == "" else 1)
            by_ext[p.suffix.lower()]["files"] += 1
            by_ext[p.suffix.lower()]["lines"] += lines
            total_files += 1
            total_lines += lines
        lines_out = [
            f"[code_metrics] total_files={total_files}, total_lines={total_lines}"
        ]
        for ext, s in sorted(by_ext.items(), key=lambda kv: -kv[1]["files"])[:20]:
            lines_out.append(
                f"  {ext or '<no-ext>'}: files={s['files']}, lines={s['lines']}"
            )
        return True, "\n".join(lines_out)

    def show_dependencies(self) -> Tuple[bool, str]:
        """扫描项目中引用的第三方模块（粗略估算）。"""
        root = self._sanitize_target("")
        if root is None:
            return False, "[show_dependencies] sandbox root 未就绪"
        deps: Dict[str, int] = defaultdict(int)
        for p in self._iter_safe_files(root):
            text = self._read_safe(p)
            if not text:
                continue
            for raw_line in text.splitlines():
                s = raw_line.strip()
                # Python: import X / from X import
                if s.startswith("import ") or s.startswith("from "):
                    m = re.match(r"^(?:from|import)\s+([A-Za-z_][A-Za-z_0-9.]*).*", s)
                    if m:
                        module = m.group(1).split(".")[0]
                        if module and not module.startswith("_"):
                            deps[module] += 1
                # JS: require('x') / import x from 'x'
                m = re.search(r"require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", s)
                if m:
                    module = m.group(1)
                    if "/" in module and not module.startswith("."):
                        module = module.split("/")[0]
                    if module and not module.startswith("."):
                        deps[module] += 1
                m = re.search(r"^import\s+.*\s+from\s+['\"]([^'\"]+)['\"]", s)
                if m:
                    module = m.group(1)
                    if module and not module.startswith("."):
                        if "/" in module:
                            module = module.split("/")[0]
                        deps[module] += 1

        lines_out = [f"[show_dependencies] distinct_modules={len(deps)}"]
        for name, cnt in sorted(deps.items(), key=lambda kv: -kv[1])[:50]:
            lines_out.append(f"  - {name}  # 出现在 {cnt} 个语句")
        return True, "\n".join(lines_out)

    # ---------- CodeIntel 层工具 ----------
    def project_brief(self) -> Tuple[bool, str]:
        """项目简短摘要（文件数、行数、目录结构、主要语言）。"""
        root = self._sanitize_target("")
        if root is None:
            return False, "[project_brief] sandbox root 未就绪"
        by_ext: Dict[str, int] = defaultdict(int)
        dirs = set()
        total_files = 0
        total_lines = 0
        for p in self._iter_safe_files(root):
            total_files += 1
            by_ext[p.suffix.lower()] += 1
            try:
                rel = p.relative_to(root.resolve())
                parts = rel.parts
                if len(parts) > 1:
                    dirs.add(parts[0])
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            text = self._read_safe(p)
            total_lines += text.count("\n") + (
                0 if text.endswith("\n") or text == "" else 1)
        main_langs = sorted(by_ext.items(), key=lambda kv: -kv[1])[:5]
        lines_out = [
            "[project_brief]",
            f"  files={total_files}, lines_approx={total_lines}",
            f"  top_dirs={sorted(dirs)[:20]}",
            "  top_languages (by file count):",
        ]
        for ext, cnt in main_langs:
            lines_out.append(f"    - {ext or '<no-ext>'}: {cnt} files")
        return True, "\n".join(lines_out)

    def file_intel(self, call) -> Tuple[bool, str]:
        """对单个文件做情报摘要（import、函数/类列表、粗略 TODO）。"""
        if not isinstance(call, dict):
            return False, "[file_intel] 参数无效"
        target_raw = (call.get("file") or call.get("path") or
                      call.get("filename") or "")
        target = self._sanitize_target(target_raw)
        if target is None or not target.is_file():
            return False, "[file_intel] 未找到文件"
        text = self._read_safe(target)
        if not text:
            return False, "[file_intel] 文件为空或读取失败"

        imports: List[str] = []
        symbols: List[str] = []
        todos: List[str] = []
        for i, raw_line in enumerate(text.splitlines(), 1):
            s = raw_line.strip()
            if not s:
                continue
            if s.startswith("import ") or s.startswith("from "):
                imports.append(f"L{i}: {s[:_MAX_LINE_LEN]}")
            m_def = re.match(r"(?:def|class|function)\s+([A-Za-z_][A-Za-z_0-9]*)", s)
            if m_def:
                symbols.append(f"L{i}: {m_def.group(1)}")
            # 粗略 TODO/FIXME
            if "TODO" in s or "FIXME" in s or "XXX" in s:
                todos.append(f"L{i}: {s[:_MAX_LINE_LEN]}")

        lines_out = [
            f"[file_intel] file={target.name}",
            f"bytes={min(len(text.encode('utf-8')), _MAX_FILE_BYTES)}",
            f"imports ({len(imports)}):",
        ]
        lines_out.extend(f"  - {x}" for x in imports[:100])
        lines_out.append(f"symbols/definitions ({len(symbols)}):")
        lines_out.extend(f"  - {x}" for x in symbols[:200])
        lines_out.append(f"TODO/FIXME ({len(todos)}):")
        lines_out.extend(f"  - {x}" for x in todos[:200])
        return True, "\n".join(lines_out[:_MAX_OUT_LINES])

    def find_related(self, call) -> Tuple[bool, str]:
        """查找与给定文件 / 符号相关的其它文件（基于 import 和命名相似度）。"""
        if not isinstance(call, dict):
            return False, "[find_related] 参数无效"
        path_key = self._clean_text(call.get("path") or call.get("file") or "", 512)
        symbol = self._clean_text(call.get("symbol") or "", _MAX_SYMBOL_LEN)

        root = self._sanitize_target("")
        if root is None:
            return False, "[find_related] sandbox root 未就绪"
        all_files = list(self._iter_safe_files(root))

        # 从目标文件里收集"种子符号"（import 名称 + 定义名称）
        seeds: set = set()
        if path_key:
            for p in all_files:
                if self._rel(p) == path_key or str(p.name) == path_key:
                    text = self._read_safe(p)
                    if text:
                        for raw_line in text.splitlines():
                            s = raw_line.strip()
                            if s.startswith("import ") or s.startswith("from "):
                                m = re.match(
                                    r"^(?:from|import)\s+([A-Za-z_][A-Za-z_0-9.]*)",
                                    s)
                                if m:
                                    seeds.add(m.group(1).split(".")[0])
                            m_def = re.match(
                                r"(?:def|class|function)\s+([A-Za-z_][A-Za-z_0-9]*)", s)
                            if m_def:
                                seeds.add(m_def.group(1))
                    break
        if symbol:
            seeds.add(symbol)
        if not seeds:
            return False, "[find_related] 没有可以追踪的种子（缺少 path 或 symbol）"

        scored: Dict[str, int] = defaultdict(int)
        for p in all_files:
            text = self._read_safe(p)
            if not text:
                continue
            rel = self._rel(p)
            if path_key and (rel == path_key or str(p.name) == path_key):
                continue
            for seed in seeds:
                if seed in text:
                    scored[rel] += 1

        ranked = sorted(scored.items(), key=lambda kv: -kv[1])[:40]
        lines_out = [
            f"[find_related] seeds={list(seeds)[:20]}",
            f"candidates={len(ranked)}"
        ]
        for rel, score in ranked:
            lines_out.append(f"  - {rel}  # score={score}")
        return True, "\n".join(lines_out)

    def analyze_symbol(self, call) -> Tuple[bool, str]:
        """围绕一个符号做深度分析：定义位置、调用位置、典型上下文。"""
        if not isinstance(call, dict):
            return False, "[analyze_symbol] 参数无效"
        symbol = self._clean_text(call.get("symbol") or "", _MAX_SYMBOL_LEN)
        if not symbol:
            return False, "[analyze_symbol] 缺少 symbol"
        root = self._sanitize_target("")
        if root is None:
            return False, "[analyze_symbol] sandbox root 未就绪"
        defs: List[str] = []
        uses: List[str] = []
        for p in self._iter_safe_files(root):
            text = self._read_safe(p)
            if not text:
                continue
            rel = self._rel(p)
            for i, raw_line in enumerate(text.splitlines(), 1):
                stripped = raw_line.strip()
                if symbol not in stripped:
                    continue
                is_def = bool(re.match(
                    r"(?:def|class|function|const|let|var)\s+" + re.escape(symbol),
                    stripped))
                if is_def:
                    defs.append(f"{rel}:{i}: {stripped[:_MAX_LINE_LEN]}")
                else:
                    uses.append(f"{rel}:{i}: {stripped[:_MAX_LINE_LEN]}")
                if len(uses) >= 200:
                    break
            if len(uses) >= 200:
                break
        lines_out = [
            f"[analyze_symbol] symbol={symbol}",
            f"definitions ({len(defs)}):",
        ]
        lines_out.extend(f"  - {d}" for d in defs[:60])
        lines_out.append(f"usages ({len(uses)}):")
        lines_out.extend(f"  - {u}" for u in uses[:200])
        return True, "\n".join(lines_out[:_MAX_OUT_LINES])

    def impact_analysis(self, call) -> Tuple[bool, str]:
        """对目标文件做变更影响分析：找到它引用 / 被引用的其它文件。"""
        if not isinstance(call, dict):
            return False, "[impact_analysis] 参数无效"
        target_raw = (call.get("file") or call.get("path") or
                      call.get("filename") or "")
        target = self._sanitize_target(target_raw)
        if target is None:
            return False, "[impact_analysis] 未找到文件"
        if not target.is_file():
            return False, "[impact_analysis] 目标不是单文件"

        target_text = self._read_safe(target)
        target_rel = self._rel(target)
        root = self._sanitize_target("") or target.parent

        # 从目标文件提取被 import 的本地文件（粗略）
        local_names = set()
        for raw_line in target_text.splitlines():
            s = raw_line.strip()
            if s.startswith("from ") or s.startswith("import "):
                m = re.match(r"^(?:from|import)\s+([A-Za-z_0-9./]+)", s)
                if m:
                    local_names.add(m.group(1))
        # 本地相对路径（from .xxx ...）暂不展开
        # 再用种子符号反向查找引用本文件的其它文件
        seeds = set(local_names)
        # 把目标文件名也作为种子
        seeds.add(target.stem)
        # 去除常见模块名干扰
        seeds.discard("")

        referenced_by: List[str] = []
        for p in self._iter_safe_files(root):
            if self._rel(p) == target_rel:
                continue
            text = self._read_safe(p)
            if not text:
                continue
            hit = False
            for seed in seeds:
                if seed and len(seed) >= 3 and seed in text:
                    hit = True
                    break
            if hit:
                referenced_by.append(self._rel(p))
            if len(referenced_by) >= 100:
                break

        lines_out = [
            f"[impact_analysis] file={target_rel}",
            f"imported_tokens (approx)={sorted(local_names)[:30]}",
            f"likely_referenced_by ({len(referenced_by)}):",
        ]
        lines_out.extend(f"  - {r}" for r in referenced_by[:100])
        return True, "\n".join(lines_out[:_MAX_OUT_LINES])

    def all_symbols(self) -> Tuple[bool, str]:
        """返回项目中所有"定义型"符号（粗略，按文件分组）。"""
        root = self._sanitize_target("")
        if root is None:
            return False, "[all_symbols] sandbox root 未就绪"
        lines_out: List[str] = []
        total_symbols = 0
        for p in self._iter_safe_files(root):
            text = self._read_safe(p)
            if not text:
                continue
            syms: List[str] = []
            for raw_line in text.splitlines():
                s = raw_line.strip()
                m = re.match(r"(?:def|class|function|const|let|var)\s+([A-Za-z_][A-Za-z_0-9]*)", s)
                if m:
                    syms.append(m.group(1))
            if syms:
                lines_out.append(f"- {self._rel(p)}: {', '.join(syms[:20])}"
                                 + (" ..." if len(syms) > 20 else ""))
                total_symbols += len(syms)
        header = [f"[all_symbols] total={total_symbols}, files_with_defs={len(lines_out)}"]
        return True, "\n".join(header + lines_out[:400])

    def complexity_report(self) -> Tuple[bool, str]:
        """复杂度粗略估算：按文件行数排序，展示 Top N。"""
        root = self._sanitize_target("")
        if root is None:
            return False, "[complexity_report] sandbox root 未就绪"
        data: List[Tuple[str, int]] = []
        for p in self._iter_safe_files(root):
            text = self._read_safe(p)
            if not text:
                continue
            lines = text.count("\n") + (0 if text.endswith("\n") or text == "" else 1)
            data.append((self._rel(p), lines))
        data.sort(key=lambda kv: -kv[1])
        lines_out = [f"[complexity_report] top={len(data[:50])}"]
        for rel, lines in data[:50]:
            lines_out.append(f"  - {rel}: {lines} lines")
        return True, "\n".join(lines_out)

    def risk_files(self) -> Tuple[bool, str]:
        """返回风险文件列表：行数高 / TODO 密度高 / 包含 FIXME/XXX。"""
        root = self._sanitize_target("")
        if root is None:
            return False, "[risk_files] sandbox root 未就绪"
        entries: List[Tuple[str, int, int]] = []
        for p in self._iter_safe_files(root):
            text = self._read_safe(p)
            if not text:
                continue
            lines = text.count("\n") + (0 if text.endswith("\n") or text == "" else 1)
            todo_count = sum(1 for line in text.splitlines()
                             if "TODO" in line or "FIXME" in line or "XXX" in line)
            if lines >= 300 or todo_count > 0:
                entries.append((self._rel(p), lines, todo_count))
        # 综合排序：行数 + TODO 数*50
        entries.sort(key=lambda kv: -(kv[1] + kv[2] * 50))
        lines_out = [f"[risk_files] candidates={len(entries)}"]
        for rel, lines, todo_cnt in entries[:60]:
            lines_out.append(f"  - {rel}  # lines={lines}, TODO_count={todo_cnt}")
        return True, "\n".join(lines_out)

    def codebase_search(self, call) -> Tuple[bool, str]:
        """基于 TF-IDF 向量检索代码库，返回最相关的代码片段。

        参数：
          - query: 自然语言查询或关键词
          - top_k: 返回结果数量（默认 8，最大 20）
          - force_reindex: 是否强制重建索引（默认 False）
        """
        query = str(call.get("query", "")).strip()
        if not query:
            return False, "[codebase_search] 缺少 query"
        if len(query) > _MAX_KEYWORD_LEN:
            query = query[:_MAX_KEYWORD_LEN]

        try:
            top_k = int(call.get("top_k", 8) or 8)
        except (TypeError, ValueError):
            top_k = 8
        top_k = max(1, min(20, top_k))
        _fr = call.get("force_reindex", False)
        force_reindex = _fr is True or (isinstance(_fr, str) and _fr.strip().lower() in ("1", "true", "yes"))

        root = self._sanitize_target("")
        if root is None:
            return False, "[codebase_search] sandbox root 未就绪"

        try:
            from src.analysis.code_vector_store import CodeVectorStore
        except Exception as exc:
            return False, f"[codebase_search] 加载向量索引失败: {exc}"

        try:
            store = CodeVectorStore(root)
            stats = store.index(force=force_reindex)
            results = store.search(query, top_k=top_k)
        except Exception as exc:
            return False, f"[codebase_search] 检索失败: {exc}"

        if not results:
            status = stats.get("status", "unknown")
            return True, f"[codebase_search] 无匹配结果（索引状态: {status}）"

        lines_out = [
            f"[codebase_search] query='{query}', status={stats.get('status', 'unknown')}, "
            f"files={stats.get('files', 0)}, chunks={stats.get('chunks', 0)}",
            "相关片段：",
        ]
        for r in results:
            path = r.get("path", "")
            start = r.get("start_line", 1)
            end = r.get("end_line", 1)
            score = r.get("score", 0.0)
            lines_out.append(f"\n--- {path}:{start}-{end} (score={score}) ---")
            snippet = r.get("snippet", "")
            if snippet:
                lines_out.append(snippet)
        return True, "\n".join(lines_out)


__all__ = ["CodeAnalysisTools", "ALLOWED_SUFFIXES"]
