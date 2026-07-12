# -*- coding: utf-8 -*-
"""依赖分析器

构建项目的导入依赖图，用于:
  - 判断哪些文件受某个修改影响 (impact analysis)
  - 找循环依赖
  - 识别模块间的耦合关系
"""
import logging

logger = logging.getLogger(__name__)

import os
from pathlib import Path
from typing import Dict, List, Set
from collections import defaultdict


class DependencyAnalyzer:
    """导入依赖分析器"""

    def __init__(self, work_dir):
        self.work_dir = Path(work_dir)
        self._project_imports: Dict[str, Set[str]] = {}
        self._external_packages: Set[str] = set()
        self._internal_deps: Dict[str, Set[str]] = defaultdict(set)
        self._reverse_deps: Dict[str, Set[str]] = defaultdict(set)
        self._built = False
        # 加速查找: top_level_name -> set of file paths
        self._top_module_index: Dict[str, Set[str]] = defaultdict(set)

    # ---------- 构建 ----------

    def build(self) -> "DependencyAnalyzer":
        """扫描整个项目，构建依赖图。"""
        self._project_imports.clear()
        self._external_packages.clear()
        self._internal_deps.clear()
        self._reverse_deps.clear()
        self._top_module_index.clear()

        # 第一步: 收集所有内部模块名（不含 .py 扩展名）
        all_modules = set()
        for root, dirs, files in os.walk(self.work_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                abs_path = Path(root) / fn
                rel = abs_path.relative_to(self.work_dir)
                rel_str = str(rel)
                module_name = rel_str.replace(os.sep, ".")
                if module_name.endswith(".py"):
                    module_name = module_name[:-3]
                all_modules.add(module_name)
                self._project_imports[rel_str] = set()
                # 构建顶级模块名索引以加速 _add_import
                top = module_name.split(".")[0]
                self._top_module_index[top].add(rel_str)

        # 第二步: 解析每个文件的 import 语句
        for rel_str, imports_set in self._project_imports.items():
            abs_path = self.work_dir / rel_str
            try:
                source = abs_path.read_text(encoding="utf-8")
            except Exception as _exc:
                logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
            for line in source.splitlines():
                stripped = line.strip()
                # import xxx[, yyy]
                if stripped.startswith("import "):
                    rest = stripped[7:].strip()
                    for part in rest.split(","):
                        pkg = part.strip().split(" as ")[0].strip()
                        top = pkg.split(".")[0]
                        self._add_import(rel_str, pkg, top, all_modules)
                # from xxx import yyy
                elif stripped.startswith("from ") and " import " in stripped:
                    rest = stripped[5:]
                    module_part = rest.split(" import ")[0].strip()
                    top = module_part.lstrip(".").split(".")[0]
                    self._add_import(rel_str, module_part, top, all_modules)

        # 第三步: 构建反向依赖（谁依赖我）
        for src, targets in self._internal_deps.items():
            for t in targets:
                self._reverse_deps[t].add(src)

        self._built = True
        return self

    def _add_import(self, src_file: str, full_pkg: str, top_pkg: str, all_modules: Set[str]):
        """把一条 import 分类为内部依赖或外部包。"""
        # 如果顶级模块在项目内，认为是内部依赖
        found_internal = False
        if top_pkg in self._top_module_index:
            # 使用索引加速查找
            candidate = full_pkg.replace(".", os.sep)
            # 安全检查：拒绝含 ".." 段的路径（防止路径穿越）
            if ".." in candidate.split(os.sep):
                self._external_packages.add(top_pkg)
                return
            for ext in (".py", os.sep + "__init__.py"):
                cand = candidate + ext
                if (self.work_dir / cand).exists():
                    self._internal_deps[src_file].add(cand)
                    found_internal = True
                    break
            if not found_internal:
                # 没找到精确文件，使用顶级模块索引匹配
                for m_path in self._top_module_index[top_pkg]:
                    if m_path.startswith(candidate.split(os.sep)[0]):
                        self._internal_deps[src_file].add(m_path)
                        found_internal = True
                        break
        if not found_internal and top_pkg and not top_pkg.startswith("_"):
            self._external_packages.add(top_pkg)

    # ---------- 查询 API ----------

    def get_dependencies(self, file_path: str) -> Set[str]:
        """file_path 内部依赖了哪些文件。"""
        if not self._built:
            self.build()
        return set(self._internal_deps.get(file_path, []))

    def get_reverse_dependencies(self, file_path: str) -> Set[str]:
        """哪些文件依赖了 file_path (修改它会影响谁)。"""
        if not self._built:
            self.build()
        return set(self._reverse_deps.get(file_path, []))

    def get_impact(self, changed_files: List[str], depth: int = 2) -> Dict[str, List[str]]:
        """影响分析：修改 changed_files 会影响到哪些其他文件。

        返回: {changed_file: [受影响的文件列表]}
        """
        if not self._built:
            self.build()
        result = {}
        for f in changed_files:
            affected = set()
            frontier = [f]
            for _ in range(depth):
                next_frontier = []
                for cur in frontier:
                    for dep in self._reverse_deps.get(cur, []):
                        if dep not in affected and dep != f:
                            affected.add(dep)
                            next_frontier.append(dep)
                frontier = next_frontier
                if not frontier:
                    break
            result[f] = sorted(affected)
        return result

    def find_cycles(self) -> List[List[str]]:
        """找循环依赖（迭代 DFS 检测，避免递归栈溢出）。"""
        if not self._built:
            self.build()
        cycles = []
        WHITE, GRAY, BLACK = 0, 1, 2
        color = defaultdict(int)
        parent = {}

        def find_cycle(start: str, end: str) -> List[str]:
            """回溯 parent 链构建循环路径。"""
            cycle = [end]
            cur = start
            while cur != end:
                cycle.append(cur)
                cur = parent.get(cur, "")
                if not cur or len(cycle) > 1000:
                    break
            cycle.append(end)
            cycle.reverse()
            return cycle

        # 使用显式栈进行迭代 DFS，避免递归深度过大导致栈溢出
        for start_node in list(self._internal_deps.keys()):
            if color[start_node] != WHITE:
                continue
            stack = [(start_node, iter(self._internal_deps.get(start_node, [])))]
            color[start_node] = GRAY
            while stack:
                node, children = stack[-1]
                try:
                    nxt = next(children)
                except StopIteration:
                    color[node] = BLACK
                    stack.pop()
                    continue
                if color[nxt] == GRAY:
                    cycles.append(find_cycle(node, nxt))
                elif color[nxt] == WHITE:
                    color[nxt] = GRAY
                    parent[nxt] = node
                    stack.append((nxt, iter(self._internal_deps.get(nxt, []))))
        return cycles

    def get_external_packages(self) -> List[str]:
        """返回项目使用的所有第三方包名（基于 import 语句推断）。"""
        if not self._built:
            self.build()
        return sorted(self._external_packages)

    def get_summary(self) -> str:
        """人类可读的依赖摘要。"""
        if not self._built:
            self.build()
        total_nodes = len(self._internal_deps)
        total_edges = sum(len(v) for v in self._internal_deps.values())
        cycles = self.find_cycles()
        ext = self.get_external_packages()
        lines = [
            "=== 依赖图摘要 ===",
            f"模块数: {total_nodes}",
            f"内部依赖边: {total_edges}",
            f"循环依赖: {len(cycles)} 个",
            f"第三方包: {len(ext)} 个",
        ]
        if ext:
            lines.append("  [" + ", ".join(ext[:20]) + (", ..." if len(ext) > 20 else "") + "]")
        if cycles:
            lines.append("\n循环依赖示例:")
            for c in cycles[:5]:
                lines.append("  -> " + " -> ".join(c))
        # 高耦合文件
        most_depended = sorted(
            self._reverse_deps.items(), key=lambda x: -len(x[1]))[:5]
        if most_depended:
            lines.append("\n被依赖最多的文件 (hub):")
            for f, deps in most_depended:
                lines.append(f"  - {f}  (被 {len(deps)} 个文件导入)")
        return "\n".join(lines)

    def build_adjacency_matrix(self) -> Dict[str, Dict[str, bool]]:
        """返回邻接矩阵形式: {file_A: {file_B: True 表示 A -> B}}"""
        if not self._built:
            self.build()
        matrix = {}
        for src, targets in self._internal_deps.items():
            matrix[src] = {t: True for t in targets}
        return matrix
