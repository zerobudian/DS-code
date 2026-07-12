# -*- coding: utf-8 -*-
"""代码分析层 — 提供 AST 级理解、依赖图、符号索引、自动验证

子模块:
  - code_parser.py         : Python AST 解析 / 代码复杂度 / 符号提取
  - dependency_analyzer.py : 导入依赖图 / 循环检测 / 影响分析
  - symbol_index.py        : 项目级符号索引 / 搜索 / 引用定位
  - code_intel.py          : 统一代码智能层 — 给 AI 模型用的洞察器
  - auto_validation.py     : 自动验证循环 — 语法 / pyflakes / mypy / pytest
"""
from src.analysis.code_parser import CodeParser, FileInfo
from src.analysis.dependency_analyzer import DependencyAnalyzer
from src.analysis.symbol_index import SymbolIndex, SymbolEntry
from src.analysis.code_intel import CodeIntel
from src.analysis.auto_validation import AutoValidator, ValidationResult

__all__ = [
    "CodeParser", "FileInfo", "DependencyAnalyzer", "SymbolIndex",
    "SymbolEntry", "CodeIntel", "AutoValidator", "ValidationResult",
]
