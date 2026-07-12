# -*- coding: utf-8 -*-
"""代码解析层 — AST 级理解

提供 AST-level code analysis — 基于 Python 的 ast 模块：
  - 类 / 函数 / 变量 / 导入  提取
  - 函数调用关系
  - 代码复杂度
  - 代码变更风险评估

用法:
    from src.analysis.code_parser import CodeParser
    parser = CodeParser("/path/to/work/dir")
    symbols = parser.parse_project()
    info = parser.parse_file("src/auth.py")
"""
import ast
import logging
import os
import json
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)
from collections import defaultdict

try:
    import jedi

    HAS_JEDI = True
except Exception:  # pragma: no cover - 降级环境
    jedi = None  # type: ignore
    HAS_JEDI = False


class _ASTVisitor(ast.NodeVisitor):
    """遍历文件 AST 的访问器"""

    COMPLEXITY_WEIGHTS = {
        "If": 1, "For": 1, "While": 1, "Try": 1,
        "With": 1, "ExceptHandler": 1,
        "FunctionDef": 1, "AsyncFunctionDef": 1, "ClassDef": 1,
        "Return": 1, "BoolOp": 1, "Compare": 1, "Lambda": 1,
    }

    def __init__(self):
        self.classes = []
        self.functions = []
        self.imports = []
        self.class_stack = []
        self.function_calls = defaultdict(list)
        self.cur_func = None
        self.complexity_score = 0

    def visit_ClassDef(self, node):
        methods = []
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append(child.name)
        doc = ast.get_docstring(node) or ""
        self.classes.append({
            "name": node.name,
            "line": node.lineno,
            "end_line": getattr(node, "end_lineno", node.lineno),
            "docstring": doc[:200],
            "methods": methods,
            "bases": [self._attr_to_str(b) for b in node.bases],
            "decorators": [self._attr_to_str(d) for d in node.decorator_list],
        })
        self.class_stack.append(node.name)
        # 保存并重置 cur_func，避免将类体中的调用错误归因到上层函数
        saved_cur_func = self.cur_func
        self.cur_func = None
        self.generic_visit(node)
        self.cur_func = saved_cur_func
        self.class_stack.pop()

    def visit_FunctionDef(self, node):
        return self._handle_function(node, async_fn=False)

    def visit_AsyncFunctionDef(self, node):
        return self._handle_function(node, async_fn=True)

    def _handle_function(self, node, async_fn=False):
        args = []
        for a in node.args.posonlyargs:
            args.append(a.arg)
        for a in node.args.args:
            args.append(a.arg)
        if node.args.vararg:
            args.append("*" + node.args.vararg.arg)
        for a in node.args.kwonlyargs:
            args.append(a.arg)
        if node.args.kwarg:
            args.append("**" + node.args.kwarg.arg)
        returns = self._attr_to_str(node.returns) if node.returns else ""
        doc = ast.get_docstring(node) or ""
        qual = ".".join(self.class_stack + [node.name])
        entry = {
            "name": node.name,
            "qualified_name": qual,
            "line": node.lineno,
            "end_line": getattr(node, "end_lineno", node.lineno),
            "args": args,
            "returns": returns,
            "docstring": doc[:200],
            "async": async_fn,
            "in_class": self.class_stack[-1] if self.class_stack else None,
            "decorators": [self._attr_to_str(d) for d in node.decorator_list],
        }
        self.functions.append(entry)
        # 保存并恢复 cur_func，防止嵌套函数导致外层函数上下文丢失
        saved_cur_func = self.cur_func
        self.cur_func = qual
        # 收集函数内部的调用 — 使用 generic_visit 递归而非 ast.walk，
        # 避免对每个函数都做一次全树遍历（O(n^2) 性能问题）。
        self.generic_visit(node)
        self.cur_func = saved_cur_func

    def visit_Import(self, node):
        for alias in node.names:
            self.imports.append({
                "module": alias.name, "alias": alias.asname, "line": node.lineno,
            })
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            self.imports.append({
                "module": node.module or "",
                "name": alias.name,
                "alias": alias.asname,
                "level": getattr(node, "level", 0),
                "line": node.lineno,
            })
        self.generic_visit(node)

    def visit_Call(self, node):
        """收集函数调用关系。复杂度由 generic_visit 统一计入，避免重复计数。"""
        if self.cur_func:
            call_name = self._attr_to_str(node.func)
            if call_name:
                self.function_calls[self.cur_func].append(call_name)
        self.generic_visit(node)

    def generic_visit(self, node):
        nt = type(node).__name__
        if nt in self.COMPLEXITY_WEIGHTS:
            self.complexity_score += self.COMPLEXITY_WEIGHTS[nt]
        super().generic_visit(node)

    @staticmethod
    def _attr_to_str(node) -> str:
        if node is None:
            return ""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            # 递归处理嵌套属性链，如 a.b.c
            base = _ASTVisitor._attr_to_str(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        if isinstance(node, ast.Call):
            return _ASTVisitor._attr_to_str(node.func)
        if isinstance(node, ast.Constant):
            return str(node.value)
        return ""


class _CompletionExtractor(ast.NodeVisitor):
    """AST 补全候选提取器：收集当前文件的类/函数/变量/导入名。"""

    def __init__(self):
        self.names: set = set()

    def visit_ClassDef(self, node):
        self.names.add(node.name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self.names.add(node.name)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self.names.add(node.name)
        self.generic_visit(node)

    def visit_Assign(self, node):
        for target in node.targets:
            for n in ast.walk(target):
                if isinstance(n, ast.Name):
                    self.names.add(n.id)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if isinstance(node.target, ast.Name):
            self.names.add(node.target.id)
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            self.names.add(alias.asname or alias.name)
        self.generic_visit(node)


class FileInfo:
    """单个文件的解析结果"""

    def __init__(self, path: str):
        self.path = path
        self.classes = []
        self.functions = []
        self.imports = []
        self.function_calls = {}
        self.complexity = 0
        self.line_count = 0
        self.code_lines = 0
        self.empty_lines = 0
        self.comment_lines = 0
        self.raw_content = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "classes": self.classes,
            "functions": self.functions,
            "imports": self.imports,
            "function_calls": dict(self.function_calls),
            "complexity": self.complexity,
            "line_count": self.line_count,
            "code_lines": self.code_lines,
        }


class CodeParser:
    """项目级代码解析器 — 提供 AST 级理解"""

    def __init__(self, work_dir):
        self.work_dir = Path(work_dir)
        self._cache: Dict[str, FileInfo] = {}
        self._project_cache: Optional[Dict[str, FileInfo]] = None
        self._project_cache_dirty = True

    def _invalidate_project_cache(self):
        """标记项目级缓存为脏，下次 parse_project() 将重新解析。"""
        self._project_cache_dirty = True

    # ---------- 公开 API ----------

    def parse_file(self, rel_path: str) -> Optional[FileInfo]:
        """解析单个文件，返回 FileInfo；非 Python 文件只统计行数。"""
        abs_path = (self.work_dir / rel_path).resolve()
        # 路径安全校验：确保在 work_dir 内
        try:
            abs_path.relative_to(self.work_dir.resolve())
        except ValueError:
            return None
        if not abs_path.exists():
            return None
        # 文件大小保护（10MB）：超大文件跳过 AST 解析，只统计行数
        try:
            if abs_path.stat().st_size > 10 * 1024 * 1024:
                info = FileInfo(rel_path)
                info.line_count = -1  # 标记为超大文件，未解析
                return info
        except Exception:
            return None
        info = FileInfo(rel_path)
        try:
            source = abs_path.read_text(encoding="utf-8")
        except Exception:
            return info
        info.raw_content = source
        lines = source.splitlines()
        info.line_count = len(lines)
        for line in lines:
            stripped = line.strip()
            if not stripped:
                info.empty_lines += 1
            elif stripped.startswith("#"):
                info.comment_lines += 1
            else:
                info.code_lines += 1
        if not rel_path.endswith(".py"):
            return info
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return info
        except (RecursionError, MemoryError, ValueError):
            # 文件过大或嵌套过深时 AST 解析可能失败，返回基本信息
            return info
        visitor = _ASTVisitor()
        visitor.visit(tree)
        info.classes = visitor.classes
        info.functions = visitor.functions
        info.imports = visitor.imports
        info.function_calls = dict(visitor.function_calls)
        info.complexity = visitor.complexity_score
        self._cache[rel_path] = info
        self._invalidate_project_cache()
        return info

    def parse_project(self) -> Dict[str, FileInfo]:
        """解析整个工作目录。结果会被缓存，直到某个文件被单独 parse_file() 修改。"""
        if not self._project_cache_dirty and self._project_cache is not None:
            return self._project_cache
        results = {}
        for root, dirs, files in os.walk(self.work_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for fn in files:
                if fn.endswith(".py"):
                    abs_path = Path(root) / fn
                    rel = abs_path.relative_to(self.work_dir)
                    rel_str = str(rel)
                    info = self.parse_file(rel_str)
                    if info is not None:
                        results[rel_str] = info
        self._project_cache = results
        self._project_cache_dirty = False
        return dict(results)

    def get_symbol(self, symbol_name: str) -> List[Dict]:
        """搜索整个项目中名为 symbol_name 的符号。"""
        results = []
        for path, info in self.parse_project().items():
            for c in info.classes:
                if c["name"] == symbol_name:
                    results.append({"path": path, "kind": "class",
                                    "line": c["line"], "info": c})
            for f in info.functions:
                if f["name"] == symbol_name:
                    results.append({"path": path, "kind": "function",
                                    "line": f["line"], "info": f})
        return results

    def get_call_chain(self, func_name: str) -> List[str]:
        """获取 func_name 内部调用了哪些函数/方法名。"""
        results = []
        for _, info in self.parse_project().items():
            for f in info.functions:
                qn = f["qualified_name"]
                if f["name"] == func_name or qn.endswith("." + func_name):
                    calls = info.function_calls.get(qn, [])
                    results.extend([c for c in calls if c])
        return results

    def get_complexity_report(self) -> Dict[str, Dict]:
        """复杂度报告：每个文件的行数 / 复杂度 / 类数 / 函数数。"""
        report = {}
        for path, info in self.parse_project().items():
            report[path] = {
                "complexity": info.complexity,
                "lines": info.line_count,
                "classes": len(info.classes),
                "functions": len(info.functions),
                "code_lines": info.code_lines,
            }
        return report

    def get_risk_files(self, top_n: int = 10) -> List[Dict]:
        """风险文件：复杂度高、行数多、缺少 docstring 的函数。"""
        risks = []
        for path, info in self.parse_project().items():
            if info.line_count == 0:
                continue
            undoc = sum(1 for f in info.functions if not f["docstring"])
            score = info.complexity + info.line_count / 10 + undoc * 5
            risks.append({
                "path": path, "score": round(score, 1),
                "complexity": info.complexity, "lines": info.line_count,
                "undoc": undoc,
            })
        risks.sort(key=lambda x: -x["score"])
        return risks[:top_n]

    def summary(self) -> str:
        """人类可读的项目摘要。"""
        parsed = self.parse_project()
        total_lines = 0
        total_classes = 0
        total_funcs = 0
        total_imports = 0
        for _, info in parsed.items():
            total_lines += info.line_count
            total_classes += len(info.classes)
            total_funcs += len(info.functions)
            total_imports += len(info.imports)
        lines = [
            "=== 项目代码摘要 ===",
            f"文件数: {len(parsed)}",
            f"总代码行: {total_lines}",
            f"类: {total_classes}",
            f"函数/方法: {total_funcs}",
            f"import 语句: {total_imports}",
        ]
        risks = self.get_risk_files(5)
        if risks:
            lines.append("\nTop 5 风险文件:")
            for r in risks:
                lines.append(
                    f"  - {r['path']}  score={r['score']}  "
                    f"lines={r['lines']}  complexity={r['complexity']}  "
                    f"undoc_funcs={r['undoc']}"
                )
        return "\n".join(lines)

    def to_json(self) -> str:
        parsed = self.parse_project()
        out = {
            "project_root": str(self.work_dir),
            "files": {p: info.to_dict() for p, info in parsed.items()},
        }
        return json.dumps(out, ensure_ascii=False, indent=2)

    def get_completions(self, rel_path: str, line: int = 1, column: int = 0,
                        prefix: str = "") -> List[Dict[str, str]]:
        """返回当前文件的补全候选。优先使用 jedi，不可用时回退到 AST 提取。"""
        abs_path = (self.work_dir / rel_path).resolve()
        try:
            abs_path.relative_to(self.work_dir.resolve())
        except ValueError:
            return []
        if not abs_path.exists():
            return []
        if not str(rel_path).endswith(".py"):
            return []

        prefix = str(prefix or "").lower()
        candidates: List[Dict[str, str]] = []

        if HAS_JEDI and jedi is not None:
            try:
                script = jedi.Script(path=str(abs_path))
                for c in script.complete(line, column):
                    if prefix and not c.name.lower().startswith(prefix):
                        continue
                    candidates.append({
                        "name": c.name,
                        "type": c.type,
                        "description": c.description or "",
                        "source": "jedi",
                    })
                return candidates[:50]
            except Exception as _exc:
                logger.debug("jedi 补全失败，回退到 AST: %s", _exc, exc_info=True)

        # AST fallback：提取当前文件的类/函数/变量/导入名
        try:
            source = abs_path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source)
            extractor = _CompletionExtractor()
            extractor.visit(tree)
            for name in sorted(extractor.names):
                if prefix and not name.lower().startswith(prefix):
                    continue
                candidates.append({
                    "name": name,
                    "type": "name",
                    "description": "",
                    "source": "ast",
                })
        except Exception as _exc:
            logger.debug("AST 补全失败: %s", _exc, exc_info=True)
            return []
        return candidates[:50]
