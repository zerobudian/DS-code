# -*- coding: utf-8 -*-
"""符号索引 & 语义搜索

构建项目级的符号索引（类/函数/变量/import），提供语义级别的代码搜索。

用法:
    from src.analysis.symbol_index import SymbolIndex
    idx = SymbolIndex("/path/to/work")
    idx.build()
    # 按名称搜索
    results = idx.search("get_user")
    # 按类型搜索
    classes = idx.by_kind("class")
    # 找一个符号的定义位置
    loc = idx.find_definition("UserService")
"""
import os
import re
import json
from pathlib import Path
from typing import Dict, List, Optional, Any
from collections import defaultdict


class SymbolEntry:
    """一个符号条目（类/函数/变量/导入）"""

    def __init__(self, name: str, kind: str, path: str, line: int,
                 end_line: int = 0, meta: Optional[Dict] = None):
        self.name = name
        self.kind = kind  # "class", "function", "method", "import", "var"
        self.path = path
        self.line = line
        self.end_line = end_line
        self.meta = meta or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind,
            "path": self.path, "line": self.line,
            "end_line": self.end_line,
            "meta": self.meta,
        }

    def short_repr(self) -> str:
        if self.kind == "class":
            return f"class {self.name} ({self.path}:{self.line})"
        if self.kind in ("function", "method"):
            args = self.meta.get("args", "")
            return f"def {self.name}({args}) ({self.path}:{self.line})"
        if self.kind == "import":
            target = self.meta.get("target", "")
            alias = self.meta.get("alias", "")
            return f"import {target}" + (f" as {alias}" if alias else "") + f" ({self.path}:{self.line})"
        return f"{self.kind} {self.name} ({self.path}:{self.line})"


class SymbolIndex:
    """项目级符号索引

    支持:
      - 按名称精确 / 模糊搜索
      - 按类型筛选
      - 按文件 / 目录分组
      - 查找定义位置 / 引用位置
      - 增量更新（修改单个文件后更新索引）
    """

    def __init__(self, work_dir):
        self.work_dir = Path(work_dir)
        self._entries: List[SymbolEntry] = []
        self._name_index: Dict[str, List[int]] = defaultdict(list)
        self._kind_index: Dict[str, List[int]] = defaultdict(list)
        self._file_index: Dict[str, List[int]] = defaultdict(list)
        self._ref_index: Dict[str, List[Dict]] = defaultdict(list)
        self._built = False

    # ---------- 构建 ----------

    def build(self) -> "SymbolIndex":
        """扫描整个项目并构建索引。"""
        self._entries.clear()
        self._name_index.clear()
        self._kind_index.clear()
        self._file_index.clear()
        self._ref_index.clear()

        for root, dirs, files in os.walk(self.work_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                abs_path = Path(root) / fn
                rel = str(abs_path.relative_to(self.work_dir))
                self._index_file(rel)

        self._built = True
        return self

    def _compact(self):
        """压实索引：移除所有 None 条目并重建所有辅助索引。"""
        # 过滤掉 None 条目
        new_entries = [e for e in self._entries if e is not None]
        self._entries = new_entries
        # 重建所有辅助索引
        self._name_index.clear()
        self._kind_index.clear()
        self._file_index.clear()
        for eid, entry in enumerate(self._entries):
            self._name_index.setdefault(entry.name, []).append(eid)
            self._kind_index.setdefault(entry.kind, []).append(eid)
            self._file_index.setdefault(entry.path, []).append(eid)
        # 引用索引: 压实后无法简单重建（需要重新读取源文件），
        # 保留现有引用索引供跨文件引用查询使用

    def update_file(self, file_path: str) -> "SymbolIndex":
        """单个文件修改后增量更新 — 先清除旧条目再重新索引。"""
        # 清除旧条目
        old_ids = self._file_index.get(file_path, [])
        if old_ids:
            old_ids = list(old_ids)  # 副本，避免迭代过程中修改
            for eid in old_ids:
                if eid < len(self._entries):
                    entry = self._entries[eid]
                    name_list = self._name_index.get(entry.name, [])
                    if eid in name_list:
                        name_list.remove(eid)
                    if not name_list:
                        self._name_index.pop(entry.name, None)
                    kind_list = self._kind_index.get(entry.kind, [])
                    if eid in kind_list:
                        kind_list.remove(eid)
            self._file_index.pop(file_path, None)
            # 清理该文件已有的引用记录，避免 update_file 后产生重复引用
            old_names = {
                self._entries[eid].name
                for eid in old_ids
                if eid < len(self._entries) and self._entries[eid] is not None
            }
            for name in old_names:
                if name in self._ref_index:
                    self._ref_index[name] = [
                        ref for ref in self._ref_index[name]
                        if ref.get("path") != file_path
                    ]
                    if not self._ref_index[name]:
                        self._ref_index.pop(name, None)
            # 清理无效条目（标记为 None 以保持索引稳定）
            for eid in old_ids:
                if eid < len(self._entries):
                    self._entries[eid] = None
        self._index_file(file_path)
        # 压实：移除所有 None 条目并重建索引
        if old_ids:
            self._compact()
        return self

    def _index_file(self, rel_path: str):
        abs_path = self.work_dir / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            import sys
            print(f"[SymbolIndex] WARNING: Cannot read {rel_path}: {e}", file=sys.stderr)
            return
        except Exception as e:
            import sys
            print(f"[SymbolIndex] WARNING: Unexpected error reading {rel_path}: {e}", file=sys.stderr)
            return

        entry_ids_for_this_file = []

        # Python 文件用 AST 级解析
        if rel_path.endswith(".py"):
            try:
                import ast as _ast
                tree = _ast.parse(source)
                self._walk_ast(tree, rel_path, entry_ids_for_this_file)
            except SyntaxError:
                # 解析失败，退化到正则提取
                self._extract_by_regex(source, rel_path, entry_ids_for_this_file)
        else:
            self._extract_by_regex(source, rel_path, entry_ids_for_this_file)

        for eid in entry_ids_for_this_file:
            self._file_index[rel_path].append(eid)

        # 引用搜索
        self._index_references_for_file(rel_path, entry_ids_for_this_file, source)

    def _index_references_for_file(self, rel_path: str, entry_ids: List[int], source: str):
        """为单个文件中的符号建立引用索引。"""
        all_names = [self._entries[i].name for i in entry_ids
                     if i < len(self._entries) and self._entries[i] is not None]
        for line_no, line in enumerate(source.splitlines(), 1):
            for name in all_names:
                if len(name) < 3:
                    continue
                pattern = r"\b" + re.escape(name) + r"\b"
                if re.search(pattern, line):
                    self._ref_index[name].append({
                        "path": rel_path, "line": line_no, "snippet": line.strip()[:100]})

    def _walk_ast(self, tree, rel_path: str, ids_out: List[int]):
        import ast as _ast

        def add_entry(name, kind, line, end_line=0, meta=None):
            eid = len(self._entries)
            entry = SymbolEntry(name, kind, rel_path, line, end_line, meta or {})
            self._entries.append(entry)
            ids_out.append(eid)
            self._name_index[name].append(eid)
            self._kind_index[kind].append(eid)

        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef):
                methods = [c.name for c in node.body
                           if isinstance(c, (_ast.FunctionDef, _ast.AsyncFunctionDef))]
                add_entry(node.name, "class", node.lineno,
                          getattr(node, "end_lineno", node.lineno),
                          {"methods": methods})

        # 第二次遍历：使用 NodeVisitor 追踪类作用域，区分类方法和模块级函数
        class _FuncKindVisitor(_ast.NodeVisitor):
            def __init__(self):
                self._class_depth = 0

            def visit_ClassDef(self, node):
                self._class_depth += 1
                self.generic_visit(node)
                self._class_depth -= 1

            def visit_FunctionDef(self, node):
                self._collect(node, async_fn=False)
                self.generic_visit(node)

            def visit_AsyncFunctionDef(self, node):
                self._collect(node, async_fn=True)
                self.generic_visit(node)

            def _collect(self, node, async_fn):
                args = ", ".join(a.arg for a in node.args.args)
                kind = "method" if self._class_depth > 0 else "function"
                add_entry(node.name, kind, node.lineno,
                          getattr(node, "end_lineno", node.lineno),
                          {"args": args, "async": async_fn})

        _FuncKindVisitor().visit(tree)

        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                for alias in node.names:
                    # Bug fix #10: 绑定名应为 alias.asname（若存在）否则 alias.name
                    # 例如 `from os import path as p` 绑定 "p"，`from os import path` 绑定 "path"
                    bound_name = alias.asname or alias.name
                    add_entry(bound_name, "import", node.lineno, node.lineno,
                              {"target": f"{node.module or ''}.{alias.name}",
                               "alias": alias.asname or ""})
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    # 绑定到命名空间的名称: `import os.path` 绑定 "os", `import x as y` 绑定 "y"
                    bound_name = alias.asname or alias.name.split(".")[0]
                    add_entry(bound_name, "import", node.lineno, node.lineno,
                              {"target": alias.name, "alias": alias.asname or ""})

    def _extract_by_regex(self, source: str, rel_path: str, ids_out: List[int]):
        """非 Python 文件时的退化方案。"""
        import re
        for line_no, line in enumerate(source.splitlines(), 1):
            # class Xxx
            m = re.match(r"\s*class\s+(\w+)", line)
            if m:
                self._add_regex_entry(m.group(1), "class", rel_path, line_no, ids_out)
                continue
            # def xxx(
            m = re.match(r"\s*def\s+(\w+)", line)
            if m:
                self._add_regex_entry(m.group(1), "function", rel_path, line_no, ids_out)
                continue
            # function xxx(
            m = re.match(r"\s*function\s+(\w+)", line)
            if m:
                self._add_regex_entry(m.group(1), "function", rel_path, line_no, ids_out)
                continue
            # const xxx =
            m = re.match(r"\s*(?:const|let|var)\s+(\w+)\s*=", line)
            if m:
                self._add_regex_entry(m.group(1), "var", rel_path, line_no, ids_out)

    def _add_regex_entry(self, name, kind, path, line, ids_out):
        eid = len(self._entries)
        entry = SymbolEntry(name, kind, path, line, line, {})
        self._entries.append(entry)
        ids_out.append(eid)
        self._name_index[name].append(eid)
        self._kind_index[kind].append(eid)

    # ---------- 查询 API ----------

    def search(self, query: str, kind: str = "", fuzzy: bool = True,
               limit: int = 50) -> List[SymbolEntry]:
        """搜索符号。

        kind: "", "class", "function", "method", "import", "var"
        fuzzy: True 时做子串/模糊匹配；False 时精确匹配。
        """
        if not self._built:
            self.build()
        results = []
        query_l = (query or "").lower()
        if kind and kind in self._kind_index:
            pool_ids = self._kind_index[kind]
        elif not query:
            # 无查询条件时返回所有符号，取前 limit 个
            pool_ids = range(len(self._entries))
        else:
            # 有查询无 kind 过滤时，使用生成器避免创建大列表
            pool_ids = range(len(self._entries))

        for eid in pool_ids:
            if eid >= len(self._entries):
                continue
            entry = self._entries[eid]
            if entry is None:
                continue
            if not query:
                results.append(entry)
            elif fuzzy:
                if query_l in entry.name.lower():
                    results.append(entry)
            else:
                if entry.name == query:
                    results.append(entry)
            if len(results) >= limit:
                break
        return results

    def by_kind(self, kind: str) -> List[SymbolEntry]:
        if not self._built:
            self.build()
        return [self._entries[i] for i in self._kind_index.get(kind, [])
                if i < len(self._entries) and self._entries[i] is not None]

    def by_file(self, file_path: str) -> List[SymbolEntry]:
        if not self._built:
            self.build()
        return [self._entries[i] for i in self._file_index.get(file_path, [])
                if i < len(self._entries) and self._entries[i] is not None]

    def find_definition(self, symbol: str) -> Optional[SymbolEntry]:
        """找 symbol 的首个定义位置。"""
        if not self._built:
            self.build()
        ids = self._name_index.get(symbol, [])
        if not ids:
            return None
        # 优先返回类 / 函数定义
        priority = {"class": 0, "function": 1, "method": 2, "import": 3, "var": 4}
        # 过滤掉越界和 None 的条目
        valid_ids = [eid for eid in ids
                     if eid < len(self._entries) and self._entries[eid] is not None]
        if not valid_ids:
            return None
        ids_sorted = sorted(valid_ids, key=lambda eid: priority.get(
            self._entries[eid].kind, 99))
        return self._entries[ids_sorted[0]]

    def find_references(self, symbol: str, limit: int = 50) -> List[Dict]:
        """找 symbol 在项目中的引用位置。
        
        注意: 引用索引基于逐文件的自引用扫描，同一个文件内对自身定义符号的引用
        会被完整收录；跨文件引用则依赖于各文件分别索引时对同名符号的匹配。
        """
        if not self._built:
            self.build()
        refs = self._ref_index.get(symbol, [])
        return refs[:limit]

    def get_summary(self) -> str:
        """人类可读的索引摘要。"""
        if not self._built:
            self.build()
        lines = ["=== 符号索引摘要 ==="]
        lines.append(f"符号总数: {len(self._entries)}")
        for kind in ("class", "function", "import", "method", "var"):
            n = len(self._kind_index.get(kind, []))
            if n > 0:
                lines.append(f"  {kind}: {n}")
        lines.append(f"文件数: {len(self._file_index)}")
        return "\n".join(lines)

    def to_json(self) -> str:
        if not self._built:
            self.build()
        return json.dumps(
            [e.to_dict() for e in self._entries],
            ensure_ascii=False, indent=2)
