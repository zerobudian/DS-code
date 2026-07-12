# -*- coding: utf-8 -*-
"""代码智能层 — 把项目结构洞察给 AI 模型

整合 CodeParser / DependencyAnalyzer / SymbolIndex，
给 AI 模型一个紧凑的"项目情报摘要"。

这一层是让 AI 从"瞎写"到"懂项目"的关键。

用法:
    from src.analysis.code_intel import CodeIntel
    intel = CodeIntel("/work/root")
    # 获取项目结构摘要
    summary = intel.project_brief()
    # 获取单个文件的结构
    intel.file_intel("src/auth.py")
    # 找相关文件（受影响文件）
    related = intel.find_related("src/foo.py")
"""
import json
from pathlib import Path
from typing import Dict, List

from src.analysis.code_parser import CodeParser
from src.analysis.dependency_analyzer import DependencyAnalyzer
from src.analysis.symbol_index import SymbolIndex


class CodeIntel:
    """项目代码洞察 — 整合解析 / 依赖 / 索引

    主要用途：给 AI 模型生成项目级洞察力。
    """

    def __init__(self, work_dir):
        self.work_dir = Path(work_dir)
        self.parser = CodeParser(work_dir)
        self.deps = DependencyAnalyzer(work_dir)
        self.index = SymbolIndex(work_dir)
        self._dirty = True

    def refresh(self):
        """重建所有索引（文件变化后调用）。"""
        self.parser = CodeParser(self.work_dir)
        self.deps = DependencyAnalyzer(self.work_dir)
        self.index = SymbolIndex(self.work_dir)
        self.deps.build()
        self.index.build()
        self._dirty = False

    # ---------- 项目级摘要 ----------

    def project_brief(self) -> str:
        """生成给 AI 模型的项目结构摘要（紧凑，信息密度高）。"""
        if self._dirty:
            self.refresh()
        parsed = self.parser.parse_project()
        files = list(parsed.keys())
        lines = []

        lines.append("=== PROJECT STRUCTURE ===")
        lines.append("Python files: " + str(len(files)))

        # 按目录分组
        dir_map: Dict[str, List[str]] = {}
        for f in files:
            parts = Path(f).parts
            if len(parts) <= 1:
                key = "(root)"
            else:
                key = parts[0]
            dir_map.setdefault(key, []).append(f)

        for d, fs in sorted(dir_map.items()):
            lines.append("  " + d + "/: " + str(len(fs)) + " files")
            for fp in sorted(fs)[:8]:
                lines.append("    - " + fp)
            if len(fs) > 8:
                lines.append("    ... (+" + str(len(fs) - 8) + " more)")

        total_classes = sum(len(v.classes) for v in parsed.values())
        total_functions = sum(len(v.functions) for v in parsed.values())
        lines.append("")
        lines.append("Classes: " + str(total_classes) +
                     ", Functions: " + str(total_functions))

        # 主要类
        lines.append("")
        lines.append("--- TOP CLASSES:")
        all_classes = []
        for path, info in parsed.items():
            for c in info.classes:
                all_classes.append((path, c["name"], len(c.get("methods", []))))
        all_classes.sort(key=lambda x: -x[2])
        for path, name, n_methods in all_classes[:20]:
            lines.append("  class " + name + " (" + path +
                         ", methods=" + str(n_methods) + ")")
        if len(all_classes) > 20:
            lines.append("  ... (+" + str(len(all_classes) - 20) + " more)")

        # 第三方包
        pkgs = self.deps.get_external_packages()
        if pkgs:
            lines.append("")
            lines.append("--- EXTERNAL PACKAGES:")
            show = pkgs[:30]
            lines.append("  [" + ", ".join(show) +
                         (", ...]" if len(pkgs) > 30 else "]"))

        # 高风险文件
        risks = self.parser.get_risk_files(5)
        if risks:
            lines.append("")
            lines.append("--- HIGH-RISK FILES:")
            for r in risks:
                lines.append("  " + r["path"] + "  score=" + str(r["score"]))
        return "\n".join(lines)

    def file_intel(self, file_path: str) -> str:
        """单个文件的洞察 — 生成给 AI 看的结构摘要。"""
        if self._dirty:
            self.refresh()
        info = self.parser.parse_file(file_path)
        if info is None:
            return "(file not found: " + file_path + ")"
        lines = ["=== FILE: " + file_path + " ==="]
        lines.append("Lines: " + str(info.line_count) +
                     " (code: " + str(info.code_lines) + ")")
        lines.append("Complexity: " + str(info.complexity))

        if info.classes:
            lines.append("")
            lines.append("Classes:")
            for c in info.classes:
                methods = ", ".join(c["methods"])
                lines.append("  class " + c["name"] + " (line " +
                             str(c["line"]) + ")")
                if methods:
                    lines.append("    methods: " + methods)

        if info.functions:
            lines.append("")
            lines.append("Functions:")
            for f in info.functions:
                args = ", ".join(f["args"])
                ret = f["returns"] or ""
                sig = "  def " + f["name"] + "(" + args + ")"
                if ret:
                    sig += " -> " + ret
                lines.append(sig)
                if f["docstring"]:
                    lines.append('    """' + f["docstring"][:80])

        if info.imports:
            lines.append("")
            lines.append("Imports:")
            for imp in info.imports[:20]:
                # from X import Y: 有 module 和 name 两个字段
                # import X: 只有 module 字段，没有 name
                if imp.get("name"):
                    # from X import Y
                    lines.append("  from " + str(imp.get("module", "")) +
                                 " import " + str(imp["name"]))
                elif imp.get("module"):
                    # import X
                    lines.append("  import " + str(imp["module"]))
                else:
                    lines.append("  import " + str(imp))
        return "\n".join(lines)

    def find_related(self, file_path: str) -> str:
        """给定文件，找它依赖的和依赖它的文件。"""
        if self._dirty:
            self.refresh()
        deps = self.deps.get_dependencies(file_path)
        dependents = self.deps.get_reverse_dependencies(file_path)
        lines = ["=== RELATED TO " + file_path + " ==="]
        lines.append("")
        lines.append("DEPENDENCIES (it imports these):")
        for d in sorted(deps)[:20]:
            lines.append("  <- " + d)
        if len(deps) > 20:
            lines.append("  ... (+" + str(len(deps) - 20) + " more)")
        lines.append("")
        lines.append("DEPENDENTS (these import it):")
        for d in sorted(dependents)[:20]:
            lines.append("  -> " + d)
        if len(dependents) > 20:
            lines.append("  ... (+" + str(len(dependents) - 20) + " more)")
        return "\n".join(lines)

    def analyze_symbol(self, symbol_name: str) -> str:
        """AI 需要了解某个符号时调用：定义位置 + 引用位置。"""
        if self._dirty:
            self.refresh()
        loc = self.index.find_definition(symbol_name)
        if not loc:
            return "(symbol not found: " + symbol_name + ")"
        lines = ["=== SYMBOL: " + symbol_name + " ==="]
        lines.append("Defined at: " + loc.path + ":" + str(loc.line) +
                     " (" + loc.kind + ")")
        refs = self.index.find_references(symbol_name, limit=20)
        if refs:
            lines.append("")
            lines.append("References (" + str(len(refs)) + " places):")
            for r in refs:
                lines.append("  " + r["path"] + ":" + str(r["line"]) +
                             " — " + str(r["snippet"])[:80])
        return "\n".join(lines)

    def impact(self, changed_files: List[str], max_depth: int = 2) -> str:
        """影响分析：修改 changed_files 后会影响哪些文件。"""
        if self._dirty:
            self.refresh()
        impact_map = self.deps.get_impact(changed_files, depth=max_depth)
        lines = ["=== IMPACT ANALYSIS ==="]
        for f, affected in impact_map.items():
            if affected:
                lines.append(f + "  affects " + str(len(affected)) + " files:")
                for a in affected:
                    lines.append("  * " + a)
            else:
                lines.append(f + "  (no internal dependents)")
        return "\n".join(lines)

    # ---------- JSON 导出 ----------

    def get_context_for_file(self, file_path: str) -> str:
        """整合 file_intel + related — 给 AI 写代码前看一眼。"""
        if self._dirty:
            self.refresh()
        return self.file_intel(file_path) + "\n\n" + self.find_related(file_path)

    def all_symbols_by_file(self) -> str:
        """返回所有符号，按文件组织。"""
        if self._dirty:
            self.refresh()
        entries = self.index.by_kind("class") + self.index.by_kind("function")
        out = []
        for e in entries[:200]:
            out.append("  " + e.kind + "  " + e.path + ":" + str(e.line) + "  " + e.name)
        suffix = ("\n... (truncated, total: " + str(len(entries)) + " symbols)") \
            if len(entries) > 200 else ""
        return "\n".join(out) + suffix

    def to_json(self) -> str:
        if self._dirty:
            self.refresh()
        data = {
            "parser": json.loads(self.parser.to_json()),
            "dependencies": self.deps.get_summary(),
        }
        return json.dumps(data, ensure_ascii=False, indent=2)
