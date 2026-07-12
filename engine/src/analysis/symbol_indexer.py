# -*- coding: utf-8 -*-
"""符号索引器 (Symbol Indexer)

对项目目录下的源代码文件做 AST 级别的语义解析，构建符号索引：
  - 函数 / 方法 (def)
  - 类 (class)
  - 导入 (import / from ... import)
  - 顶层常量 / 变量赋值

支持的语言:
  - Python (标准库 ast，零依赖)
  - JavaScript/TypeScript (轻量级正则解析，无外部依赖)
  - JSON (顶层 keys)

设计目标:
  1. 增量更新 — 单文件变化只重新解析该文件
  2. 快速查找 — O(1) symbol -> (file, line, col, kind)
  3. 模糊搜索 — 支持 "fn_hello" / "hello" / "hlo" 的前缀匹配与模糊匹配
  4. 上下文注入 — 当对话消息包含 "@符号名" 时，自动把符号源码注入消息前缀

典型用法:
    idx = SymbolIndex("/path/to/project")
    idx.scan_all()                               # 全量扫描
    idx.invalidate_file("/path/to/file.py")      # 文件变化时重扫

    results = idx.search("hello")                # 模糊搜索
    sym = idx.find_symbol("hello_function")      # 精确搜索
    source = idx.get_symbol_source(sym)          # 获取符号的源代码片段

    injected = idx.inject_mentions(user_text)    # 把 @函数名 替换为源碼上下文
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import os
import re
import json
import ast
import time
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Symbol:
    """单个符号条目。"""

    name: str                   # 符号名 (函数名 / 类名 / 顶层变量名)
    kind: str                   # "function" | "class" | "method" | "import" | "variable" | "constant" | "key"
    file_path: str              # 绝对路径
    line: int                   # 1-based 行号
    col: int = 0                # 0-based 列号
    end_line: int = 0           # 结束行号（函数/类）
    docstring: str = ""         # docstring（若有）
    signature: str = ""         # 函数签名（如 "def foo(a, b) -> int"）
    parent: str = ""            # 类名（对于方法）

    def as_dict(self) -> Dict:
        return asdict(self)

    @property
    def short_path(self) -> str:
        return os.path.basename(self.file_path)

    def describe(self) -> str:
        prefix_map = {
            "function": "ƒ",
            "method": "m",
            "class": "◉",
            "import": "↩",
            "variable": "v",
            "constant": "C",
            "key": "k",
        }
        return f"{prefix_map.get(self.kind, '?')} {self.name} — {self.short_path}:{self.line}"


# ---------------------------------------------------------------------------
# 辅助：文件类型检测
# ---------------------------------------------------------------------------

_PY_EXT = {".py", ".pyw"}
_JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_JSON_EXT = {".json"}


def _detect_language(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _PY_EXT:
        return "python"
    if ext in _JS_EXT:
        return "javascript"
    if ext in _JSON_EXT:
        return "json"
    return "unknown"


# ---------------------------------------------------------------------------
# 各语言解析器
# ---------------------------------------------------------------------------


class _PythonParser:
    """使用标准库 ast 解析 Python。"""

    @staticmethod
    def parse(file_path: str, source: str) -> List[Symbol]:
        symbols: List[Symbol] = []
        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError:
            # 语法错误时尝试回退到逐行扫描（至少抓到一些符号）
            return _PythonParser._fallback_regex(file_path, source)
        except Exception:
            return []

        _PythonParser._walk(tree, file_path, symbols, parent="")
        return symbols

    @staticmethod
    def _walk(node: ast.AST, file_path: str, out: List[Symbol], parent: str) -> None:
        # 函数
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sig = _PythonParser._make_signature(node)
            doc = _PythonParser._get_docstring(node)
            end_line = getattr(node, "end_lineno", node.lineno)
            out.append(Symbol(
                name=node.name,
                kind="method" if parent else "function",
                file_path=file_path,
                line=node.lineno,
                col=node.col_offset,
                end_line=end_line,
                docstring=doc,
                signature=sig,
                parent=parent,
            ))
            # 递归方法
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    child_parent = f"{parent}.{node.name}" if parent else node.name
                    _PythonParser._walk(child, file_path, out, parent=child_parent)
            return

        if isinstance(node, ast.ClassDef):
            doc = _PythonParser._get_docstring(node)
            sig = f"class {node.name}"
            bases = [ast.unparse(b) for b in node.bases] if hasattr(ast, "unparse") else []
            if bases:
                sig += "(" + ", ".join(bases) + ")"
            end_line = getattr(node, "end_lineno", node.lineno)
            out.append(Symbol(
                name=node.name,
                kind="class",
                file_path=file_path,
                line=node.lineno,
                col=node.col_offset,
                end_line=end_line,
                docstring=doc,
                signature=sig,
                parent=parent,
            ))
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    child_parent = f"{parent}.{node.name}" if parent else node.name
                    _PythonParser._walk(child, file_path, out, parent=child_parent)
            return

        # 顶层导入 (仅非嵌套模块级收集)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = _PythonParser._import_names(node)
            for n in names:
                out.append(Symbol(
                    name=n,
                    kind="import",
                    file_path=file_path,
                    line=node.lineno,
                    col=getattr(node, "col_offset", 0),
                    signature="",
                ))
            return

        # 顶层变量 / 常量（普通赋值）
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id[0:1].isalpha():
                    name = target.id
                    is_const = name.isupper() and (len(name) > 1 or "_" in name)
                    kind = "constant" if is_const else "variable"
                    out.append(Symbol(
                        name=name,
                        kind=kind,
                        file_path=file_path,
                        line=node.lineno,
                        col=0,
                        signature="",
                    ))

        # 类型注解赋值 (AnnAssign): x: int = 1 / CONST: str = "hello"
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id[0:1].isalpha():
                name = target.id
                is_const = name.isupper() and (len(name) > 1 or "_" in name)
                kind = "constant" if is_const else "variable"
                out.append(Symbol(
                    name=name,
                    kind=kind,
                    file_path=file_path,
                    line=node.lineno,
                    col=getattr(node, "col_offset", 0),
                    signature="",
                ))

        # 继续递归
        for child in ast.iter_child_nodes(node):
            if hasattr(child, "lineno"):
                _PythonParser._walk(child, file_path, out, parent=parent)

    @staticmethod
    def _make_signature(node) -> str:
        try:
            parts = []
            if isinstance(node, ast.AsyncFunctionDef):
                parts.append("async ")
            parts.append(f"def {node.name}(")
            args = node.args
            arg_strs = []

            def _arg_name(a):
                name = a.arg
                if a.annotation is not None and hasattr(ast, "unparse"):
                    try:
                        name += ": " + ast.unparse(a.annotation)
                    except Exception as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                return name

            # 位置参数 (不含默认值)
            pos_args = args.posonlyargs if hasattr(args, "posonlyargs") else []
            for a in pos_args:
                arg_strs.append(_arg_name(a))
            # posonlyargs 后追加 / 分隔符
            if pos_args:
                arg_strs.append("/")
            # 普通位置/关键字参数，带默认值
            defaults = list(args.defaults)
            n_args = len(args.args)
            n_defaults = len(defaults)
            for i, a in enumerate(args.args):
                s = _arg_name(a)
                default_idx = i - (n_args - n_defaults)
                if default_idx >= 0 and hasattr(ast, "unparse"):
                    try:
                        s += "=" + ast.unparse(defaults[default_idx])
                    except Exception as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                arg_strs.append(s)
            # *args
            if args.vararg is not None:
                arg_strs.append("*" + _arg_name(args.vararg).lstrip("*"))
            else:
                # 无*args但有关键字-only参数时，需要裸*分隔符
                kwonly = list(args.kwonlyargs)
                if kwonly:
                    arg_strs.append("*")
            # 关键字-only 参数
            kwonly = list(args.kwonlyargs)
            kw_defaults = list(args.kw_defaults)
            for i, a in enumerate(kwonly):
                s = _arg_name(a)
                d = kw_defaults[i] if i < len(kw_defaults) and kw_defaults[i] is not None else None
                if d is not None and hasattr(ast, "unparse"):
                    try:
                        s += "=" + ast.unparse(d)
                    except Exception as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                arg_strs.append(s)
            # **kwargs
            if args.kwarg is not None:
                arg_strs.append("**" + _arg_name(args.kwarg).lstrip("*"))

            parts.append(", ".join(arg_strs))
            parts.append(")")
            if node.returns is not None and hasattr(ast, "unparse"):
                try:
                    parts.append(" -> " + ast.unparse(node.returns))
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            return "".join(parts)
        except Exception:
            return f"def {node.name}(...)"

    @staticmethod
    def _get_docstring(node) -> str:
        try:
            doc = ast.get_docstring(node) or ""
            if len(doc) > 300:
                doc = doc[:300] + "…"
            return doc.strip()
        except Exception:
            return ""

    @staticmethod
    def _import_names(node) -> List[str]:
        names: List[str] = []
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name + (" as " + alias.asname if alias.asname else ""))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                local = alias.asname or alias.name
                names.append(f"{module}.{local}" if module else local)
        return names[:20]

    @staticmethod
    def _fallback_regex(file_path: str, source: str) -> List[Symbol]:
        symbols: List[Symbol] = []
        for i, line in enumerate(source.splitlines(), 1):
            m = re.match(r"\s*(?:async\s+)?def\s+(\w+)\s*\(", line)
            if m:
                symbols.append(Symbol(name=m.group(1), kind="function",
                                      file_path=file_path, line=i))
                continue
            m = re.match(r"\s*class\s+(\w+)", line)
            if m:
                symbols.append(Symbol(name=m.group(1), kind="class",
                                      file_path=file_path, line=i))
        return symbols


class _JsParser:
    """轻量级 JS/TS 解析（零依赖）。"""

    _CLASS_RE = re.compile(r"\bclass\s+(\w+)", re.MULTILINE)
    _FUNC_RE = re.compile(r"(?:(?:async\s+)?function\s+|const\s+|let\s+)?(\w+)\s*(?:=\s*(?:async\s*)?)?\(", re.MULTILINE)
    _EXPORT_FN_RE = re.compile(r"export\s+(?:async\s+)?(?:function|const)\s+(\w+)", re.MULTILINE)
    _METHOD_RE = re.compile(r"(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{", re.MULTILINE)

    @staticmethod
    def parse(file_path: str, source: str) -> List[Symbol]:
        symbols: List[Symbol] = []
        seen = set()

        for m in _JsParser._CLASS_RE.finditer(source):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            line = source[:m.start()].count("\n") + 1
            symbols.append(Symbol(name=name, kind="class", file_path=file_path, line=line))

        for m in _JsParser._EXPORT_FN_RE.finditer(source):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            line = source[:m.start()].count("\n") + 1
            symbols.append(Symbol(name=name, kind="function", file_path=file_path, line=line))

        for m in _JsParser._FUNC_RE.finditer(source):
            name = m.group(1)
            if name in seen or not name[0:1].isalpha():
                continue
            if name in {"if", "for", "while", "switch", "return", "catch", "throw", "typeof", "new"}:
                continue
            seen.add(name)
            line = source[:m.start()].count("\n") + 1
            symbols.append(Symbol(name=name, kind="function", file_path=file_path, line=line))

        return symbols


class _JsonParser:
    """JSON 顶层 key 提取。"""

    @staticmethod
    def parse(file_path: str, source: str) -> List[Symbol]:
        try:
            data = json.loads(source)
        except Exception:
            return []

        symbols: List[Symbol] = []
        if isinstance(data, dict):
            for i, key in enumerate(data.keys()):
                symbols.append(Symbol(
                    name=str(key),
                    kind="key",
                    file_path=file_path,
                    line=i + 1,
                ))
        return symbols


# ---------------------------------------------------------------------------
# 主索引器
# ---------------------------------------------------------------------------

class SymbolIndex:
    """项目级符号索引。线程安全（内部锁）。"""

    def __init__(self, root_dir: Optional[str] = None):
        self._root = os.path.abspath(root_dir) if root_dir else None
        self._symbols_by_file: Dict[str, List[Symbol]] = {}   # file_path -> [Symbol]
        self._name_index: Dict[str, List[Symbol]] = {}        # lowercase_name -> [Symbol]
        self._file_mtime: Dict[str, float] = {}               # file_path -> last mtime
        self._lock = threading.RLock()
        self._last_scan_time: float = 0.0

    @property
    def root(self) -> Optional[str]:
        with self._lock:
            return self._root

    # ------------------------------------------------------------------
    # 扫描 / 增量更新
    # ------------------------------------------------------------------

    def set_root(self, root_dir: str) -> None:
        with self._lock:
            self._root = os.path.abspath(root_dir)

    def scan_all(self, root_dir: Optional[str] = None, max_files: int = 500) -> int:
        """扫描整个项目目录。返回扫描的文件数。线程安全：文件I/O在锁外，结果提交在锁内。"""
        if root_dir:
            with self._lock:
                self._root = os.path.abspath(root_dir)
        with self._lock:
            root = self._root
        if not root:
            return 0

        root_resolved = Path(root).resolve()
        # 阶段1：收集要扫描的文件（锁外做I/O）
        files_to_scan: List[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in ("node_modules", "__pycache__", ".git", ".svn", ".hg",
                             "dist", "build", ".venv", "venv", "env", ".DS_Store")
                and not (Path(dirpath) / d).is_symlink()
            ]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                path = Path(dirpath) / fn
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    path.resolve().relative_to(root_resolved)
                except (ValueError, OSError):
                    continue
                if _detect_language(str(path)) == "unknown":
                    continue
                files_to_scan.append(str(path))
                if len(files_to_scan) >= max_files:
                    break
            if len(files_to_scan) >= max_files:
                break

        # 阶段2：锁内提交结果
        scanned = 0
        with self._lock:
            for fp in files_to_scan:
                if self._update_file(fp):
                    scanned += 1
            self._last_scan_time = time.time()
        return scanned

    def remove_file(self, file_path: str) -> None:
        """文件被删除时从索引中移除。线程安全。"""
        abs_path = os.path.abspath(file_path)
        with self._lock:
            old_symbols = self._symbols_by_file.pop(abs_path, None)
            self._file_mtime.pop(abs_path, None)
            if old_symbols:
                # 从_name_index中清除
                for sym in old_symbols:
                    key = sym.name.lower()
                    if key in self._name_index:
                        self._name_index[key] = [
                            s for s in self._name_index[key]
                            if s.file_path != abs_path
                        ]
                        if not self._name_index[key]:
                            del self._name_index[key]

    def invalidate_file(self, file_path: str) -> None:
        """某个文件变化 — 重新解析它（如果文件已删除则移除）。"""
        abs_path = os.path.abspath(file_path)
        with self._lock:
            # 检查文件是否还存在
            if not os.path.exists(abs_path) or not os.path.isfile(abs_path):
                # 文件已删除，清理索引
                self._update_file_locked_remove(abs_path)
                return
            self._update_file(abs_path)

    def _update_file_locked_remove(self, file_path: str) -> None:
        """锁内：从索引中移除文件条目（调用方必须持有_lock）"""
        old_symbols = self._symbols_by_file.pop(file_path, None)
        self._file_mtime.pop(file_path, None)
        if old_symbols:
            for sym in old_symbols:
                key = sym.name.lower()
                if key in self._name_index:
                    self._name_index[key] = [
                        s for s in self._name_index[key]
                        if s.file_path != file_path
                    ]
                    if not self._name_index[key]:
                        del self._name_index[key]

    _MAX_FILE_BYTES = 1 * 1024 * 1024  # 1 MB，超大源文件跳过 AST 解析避免 OOM

    def _update_file(self, file_path: str) -> bool:
        """解析单个文件并更新索引。返回 True 表示有更新。"""
        try:
            mtime = os.path.getmtime(file_path)
            # 检查缓存是否有效
            if file_path in self._file_mtime and self._file_mtime[file_path] == mtime:
                return False

            # C-007: 跳过超大源文件，避免 AST / 正则解析消耗大量内存
            try:
                file_size = os.path.getsize(file_path)
            except OSError:
                return False
            if file_size > self._MAX_FILE_BYTES:
                logger.debug("[SymbolIndex] 跳过超大文件: %s (%s bytes)", file_path, file_size)
                return False

            # 读取并解析 (先解析，成功后再清除旧索引，避免解析失败导致数据丢失)
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()

            lang = _detect_language(file_path)
            symbols: List[Symbol] = []
            if lang == "python":
                symbols = _PythonParser.parse(file_path, source)
            elif lang == "javascript":
                symbols = _JsParser.parse(file_path, source)
            elif lang == "json":
                symbols = _JsonParser.parse(file_path, source)

            # 清除旧索引 (解析成功后安全清除)
            if file_path in self._symbols_by_file:
                old_syms = self._symbols_by_file[file_path]
                for s in old_syms:
                    key = s.name.lower()
                    if key in self._name_index:
                        self._name_index[key] = [
                            x for x in self._name_index[key] if x.file_path != file_path
                        ]
                        if not self._name_index[key]:
                            del self._name_index[key]

            # 缓存并建立反向索引
            self._symbols_by_file[file_path] = symbols
            self._file_mtime[file_path] = mtime
            for s in symbols:
                key = s.name.lower()
                self._name_index.setdefault(key, []).append(s)

            return True
        except (OSError, UnicodeDecodeError) as e:
            import sys
            print(f"[SymbolIndexer] WARNING: I/O error indexing {file_path}: {e}", file=sys.stderr)
            return False
        except SyntaxError as e:
            import sys
            print(f"[SymbolIndexer] WARNING: Syntax error in {file_path}: {e}", file=sys.stderr)
            return False
        except Exception as e:
            import sys
            import traceback
            print(f"[SymbolIndexer] ERROR: Unexpected error indexing {file_path}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return False

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def total_symbols(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._symbols_by_file.values())

    def total_files(self) -> int:
        with self._lock:
            return len(self._symbols_by_file)

    def find_symbol(self, name: str) -> Optional[Symbol]:
        """精确匹配（大小写不敏感），返回最佳匹配。"""
        with self._lock:
            key = name.lower()
            if key in self._name_index:
                syms = self._name_index[key]
                # 优先选择函数/类
                for kind_priority in ("function", "class", "method", "constant", "variable", "import", "key"):
                    for s in syms:
                        if s.kind == kind_priority:
                            return s
                return syms[0]
            return None

    def search(self, query: str, limit: int = 30) -> List[Symbol]:
        """模糊搜索。支持:
            - 精确子串: "hello" 匹配所有包含 hello 的符号
            - 模糊匹配: "hlo" 按字符序列匹配
            - 前缀匹配: "hello_" 按前缀
        """
        if not query:
            return []
        with self._lock:
            q = query.lower().strip()
            results: List[Tuple[Symbol, int]] = []  # (symbol, score)

            for name_lower, syms in self._name_index.items():
                score = -1
                # 精确匹配 (最高)
                if name_lower == q:
                    score = 1000
                # 前缀匹配
                elif name_lower.startswith(q):
                    score = 500
                # 子串包含
                elif q in name_lower:
                    score = 200
                # 模糊匹配 (每个字符按顺序出现)
                elif self._fuzzy_match(q, name_lower):
                    score = 50 + max(0, 30 - abs(len(name_lower) - len(q)))
                # CamelCase 段匹配: "CC" 匹配 "ChatController"
                elif self._camel_match(q, name_lower):
                    score = 80

                if score > 0:
                    # 同类每个符号取一个
                    for s in syms:
                        results.append((s, score))
                    # 注意: 不 break，继续检查其他 name 组

            # 排序 (分数降序 + 函数/类优先 + 短名优先)
            kind_order = {"function": 0, "class": 1, "method": 2, "constant": 3,
                          "variable": 4, "key": 5, "import": 6}
            results.sort(key=lambda x: (
                -x[1],
                kind_order.get(x[0].kind, 10),
                len(x[0].name),
            ))
            return [s for s, _ in results[:limit]]

    def list_file_symbols(self, file_path: str) -> List[Symbol]:
        """返回单个文件的所有符号。"""
        with self._lock:
            return list(self._symbols_by_file.get(os.path.abspath(file_path), []))

    def list_all_files(self) -> List[str]:
        with self._lock:
            return list(self._symbols_by_file.keys())

    # ------------------------------------------------------------------
    # 源代码提取 & 上下文注入
    # ------------------------------------------------------------------

    def get_symbol_source(self, symbol: Symbol, context_lines: int = 2) -> str:
        """提取符号对应的源代码片段（含上下文）。"""
        try:
            with open(symbol.file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception:
            return ""

        start = max(0, symbol.line - 1 - context_lines)
        if symbol.end_line and symbol.end_line > symbol.line:
            end = min(len(lines), symbol.end_line + context_lines)
        else:
            # 默认取到函数结束 (简化: 到下一个同缩进 def/class 或文件末尾)
            end = min(len(lines), symbol.line + 40)
            # 尝试找到下一个 def/class
            for j in range(symbol.line, min(len(lines), symbol.line + 80)):
                line = lines[j]
                stripped = line.lstrip()
                if not stripped:
                    continue
                indent = len(line) - len(stripped)
                if indent == 0 and (stripped.startswith("def ") or stripped.startswith("class ") or stripped.startswith("async def")):
                    end = j
                    break

        snippet = "".join(lines[start:end])
        return snippet.rstrip()

    _MENTION_RE = re.compile(r"@([A-Za-z_][A-Za-z0-9_\.]*)(\b|$)")

    def inject_mentions(self, text: str, max_injects: int = 5) -> str:
        """扫描用户输入中的 @符号名，把对应源码作为上下文注入。

        返回: (原文本, 注入的上下文字符串) 的组合。
        """
        if not text:
            return text

        mentions = set()
        for m in self._MENTION_RE.finditer(text):
            mentions.add(m.group(1))
            if len(mentions) >= max_injects:
                break

        if not mentions:
            return text

        injected_parts: List[str] = []
        for name in mentions:
            sym = self.find_symbol(name)
            if sym is None:
                continue
            src = self.get_symbol_source(sym, context_lines=1)
            if src:
                injected_parts.append(
                    f"<!-- [@{sym.short_path}:{sym.line}] {sym.kind} `{sym.name}`"
                    + ("\n" + sym.signature if sym.signature else "")
                    + f" -->\n```{_detect_language(sym.file_path)}\n{src}\n```"
                )

        if not injected_parts:
            return text

        header = (
            "— 以下上下文来自项目代码（已通过 @符号名 自动注入）—\n"
            + "\n\n".join(injected_parts)
            + "\n\n— 上下文结束 —\n\n"
        )
        return header + text

    # ------------------------------------------------------------------
    # 内部: 模糊匹配辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _fuzzy_match(q: str, name: str) -> bool:
        """q 的每个字符按顺序出现在 name 中。"""
        if len(q) > len(name):
            return False
        it = iter(name)
        return all(ch in it for ch in q)

    @staticmethod
    def _camel_match(q: str, name: str) -> bool:
        """大写字母序列匹配 — "cc" 匹配 "ChatController"。"""
        capitals = "".join(ch for ch in name if ch.isupper())
        return q in capitals.lower()

    # ------------------------------------------------------------------
    # 调试: 打印统计
    # ------------------------------------------------------------------

    def stats(self) -> str:
        with self._lock:
            files = len(self._symbols_by_file)
            total = sum(len(v) for v in self._symbols_by_file.values())
            kinds: Dict[str, int] = {}
            for syms in self._symbols_by_file.values():
                for s in syms:
                    kinds[s.kind] = kinds.get(s.kind, 0) + 1
            return (f"SymbolIndex: {files} 个文件, {total} 个符号. "
                    + " ".join(f"{k}={v}" for k, v in sorted(kinds.items())))


# ---------------------------------------------------------------------------
# 单例快捷入口（供全局一处使用）
# ---------------------------------------------------------------------------

_global_index: Optional[SymbolIndex] = None
_global_lock = threading.Lock()


def get_global_index(root_dir: Optional[str] = None) -> SymbolIndex:
    """获取全局共享的索引器。线程安全：整个检查-创建过程在锁内原子完成。"""
    global _global_index
    with _global_lock:
        if _global_index is None:
            _global_index = SymbolIndex(root_dir)
        elif root_dir is not None and _global_index.root != os.path.abspath(root_dir):
            _global_index = SymbolIndex(root_dir)
    return _global_index


def reset_global_index() -> None:
    global _global_index
    with _global_lock:
        _global_index = None
