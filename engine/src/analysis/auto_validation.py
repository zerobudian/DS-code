# -*- coding: utf-8 -*-
"""自动验证循环

每次代码修改后触发：
  1. 语法检查 (py_compile)
  2. 代码静态检查 (pyflakes 可选)
  3. 运行 pytest (若项目有测试)
  4. 收集结果 → 结构化报告

这让 AI 模型每次写代码之后都能看到"验证结果"，而不是盲目地继续写下去。
"""
import os
import re
import subprocess
import py_compile
from pathlib import Path
from typing import List, Dict, Optional, Any


class ValidationResult:
    """一次验证的结果。"""

    def __init__(self, kind: str, passed: bool, message: str = ""):
        self.kind = kind
        self.passed = passed
        self.message = message

    def short_line(self) -> str:
        mark = "✓" if self.passed else "✗"
        return f"{mark} [{self.kind}] {self.message[:200]}"

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "passed": self.passed, "message": self.message}


class AutoValidator:
    """代码修改后的自动验证器。"""

    def __init__(self, work_dir):
        self.work_dir = Path(work_dir)

    # ---------- 安全工具 ----------

    def _sanitize_file_path(self, file_path: str) -> Optional[str]:
        """校验文件路径在工作目录内，返回相对路径或 None（路径遍历防护）。"""
        try:
            full = (self.work_dir / file_path).resolve()
            full.relative_to(self.work_dir.resolve())
        except (ValueError, OSError):
            return None
        if not full.exists():
            return None
        return str(full)

    def _sanitize_file_list(self, files: Optional[List[str]]) -> List[str]:
        """过滤文件列表，只保留工作目录内的合法文件。"""
        if files is None:
            return self._find_py_files()
        safe = []
        for f in files:
            clean = self._sanitize_file_path(f)
            if clean is not None:
                safe.append(clean)
        return safe

    # ---------- 单项检查 ----------

    def check_syntax(self, files: Optional[List[str]] = None) -> ValidationResult:
        """语法检查 (py_compile)。"""
        target = self._sanitize_file_list(files)
        errors: List[str] = []
        for f in target:
            try:
                py_compile.compile(f, doraise=True)
            except py_compile.PyCompileError as e:
                # PyCompileError.msg 通常已是格式化后的字符串
                msg = getattr(e, "msg", None) or str(e)
                if not isinstance(msg, str):
                    msg = str(msg) or str(e)
                errors.append(f"{f}: {msg}")
            except (SyntaxError, ValueError) as e:
                errors.append(f"{f}: {type(e).__name__}: {e}")
            except Exception as e:  # noqa: BLE001 — 校验阶段捕获所有异常，防止单个坏文件中止整体
                errors.append(f"{f}: {type(e).__name__}: {e}")
        if errors:
            msg = "\n".join(errors[:10])
            return ValidationResult("syntax", False, msg)
        return ValidationResult("syntax", True, f"{len(target)} files OK")

    def check_import(self) -> ValidationResult:
        """导入检查：仅做编译阶段检查 (py_compile)，不实际执行模块代码。

        注意: 不使用 exec_module 以避免执行项目中的任意 Python 代码。
        性能优化：限制扫描深度和文件数，避免在超大项目中遍历全部文件。
        """
        errors = []
        checked = 0
        max_files = 500  # 单次检查最多扫描 500 个文件
        for root, dirs, files in os.walk(self.work_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                fp = os.path.join(root, fn)
                rel = os.path.relpath(fp, self.work_dir)
                checked += 1
                if checked > max_files:
                    break
                try:
                    py_compile.compile(fp, doraise=True)
                except py_compile.PyCompileError as e:
                    errors.append(f"{rel}: {getattr(e, 'msg', str(e))}")
                except Exception as e:
                    err_type = type(e).__name__
                    errors.append(f"{rel}: {err_type}: {str(e)[:100]}")
            if checked > max_files:
                break
        if errors:
            msg = "\n".join(errors[:5])
            return ValidationResult("import", False, f"{len(errors)} / {checked} compile errors:\n{msg}")
        if checked >= max_files:
            return ValidationResult("import", True, f"{checked} modules compiled OK (scan limit reached)")
        return ValidationResult("import", True, f"{checked} modules compiled OK")

    def run_pyflakes(self, files: Optional[List[str]] = None) -> ValidationResult:
        """运行 pyflakes 检查未使用的变量 / 未定义的名字。"""
        target = self._sanitize_file_list(files)
        if not target:
            return ValidationResult("pyflakes", True, "(no files)")
        try:
            cmd = ["python3", "-m", "pyflakes"] + target
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
                cwd=str(self.work_dir))
            output = (result.stdout or "") + (result.stderr or "")
            return ValidationResult("pyflakes", result.returncode == 0,
                                    output[:1000] if output.strip() else "no issues")
        except Exception as e:
            return ValidationResult("pyflakes", False, f"exec error: {e}")

    def run_mypy(self, files: Optional[List[str]] = None) -> ValidationResult:
        """运行 mypy 类型检查。"""
        target = self._sanitize_file_list(files) if files else ["."]
        try:
            cmd = ["python3", "-m", "mypy", "--no-color-output",
                   "--no-error-summary", "--ignore-missing-imports"] + target
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
                cwd=str(self.work_dir))
            output = (result.stdout or "") + (result.stderr or "")
            if "error:" in output.lower():
                return ValidationResult("mypy", False, output[:800])
            return ValidationResult("mypy", True, output[:300] or "clean")
        except Exception as e:
            return ValidationResult("mypy", False, f"exec error: {e}")

    def run_pytest(self, scope: str = ".", pattern: str = "") -> ValidationResult:
        """运行 pytest 测试。scope 和 pattern 参数仅允许安全字符。"""
        import os as _os
        try:
            # 安全校验 scope: 仅允许安全的路径字符，且拒绝路径穿越 (..) / 绝对路径 / 选项注入
            if not re.match(r'^[\w\-_. /]+$', scope):
                return ValidationResult("pytest", False,
                    f"Invalid scope: {scope[:100]}")
            if ".." in scope or scope.startswith("-") or _os.path.isabs(scope):
                return ValidationResult("pytest", False,
                    f"Path traversal / option injection rejected: {scope[:100]}")
            cmd = ["python3", "-m", "pytest", "-v", "--tb=short", "-q"]
            if pattern:
                # 安全校验: pattern 仅允许字母、数字、下划线、连字符、空格和逻辑运算符
                if not re.match(r'^[\w\s\-_()\[\]&|!=<>*?:.,]+$', pattern):
                    return ValidationResult("pytest", False,
                        f"Invalid pattern: {pattern[:100]}")
                cmd += ["-k", pattern]
            cmd += ["--", scope]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=180,
                cwd=str(self.work_dir))
            output = (result.stdout or "") + (result.stderr or "")
            passed = (result.returncode == 0)
            # 取最后 20 行作为摘要
            tail_lines = [ln for ln in output.splitlines()[-20:] if ln.strip()]
            summary = "\n".join(tail_lines) or "(no output)"
            return ValidationResult("pytest", passed, summary[:1500])
        except Exception as e:
            return ValidationResult("pytest", False, f"exec error: {e}")

    def has_tests(self) -> bool:
        """项目中是否存在 tests / test_*.py 文件。"""
        for root, dirs, files in os.walk(self.work_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            # 检查是否有 tests 目录
            if "tests" in dirs:
                return True
            for f in files:
                if f.startswith("test_") and f.endswith(".py"):
                    return True
        return False

    # ---------- 聚合检查 ----------

    def validate_all(self, files: Optional[List[str]] = None) -> List[ValidationResult]:
        """运行全套检查，返回结果列表。"""
        results: List[ValidationResult] = []
        results.append(self.check_syntax(files))
        results.append(self.run_pyflakes(files))
        if self.has_tests():
            results.append(self.run_pytest())
        return results

    def report(self, results: List[ValidationResult]) -> str:
        """生成人类可读报告。"""
        lines = ["=== VALIDATION REPORT ==="]
        passed = sum(1 for r in results if r.passed)
        failed = len(results) - passed
        lines.append(f"Result: {passed} passed, {failed} failed")
        lines.append("")
        for r in results:
            lines.append(r.short_line())
            if r.message and not r.passed:
                for ln in str(r.message).splitlines()[:10]:
                    lines.append(f"    {ln}")
        if failed == 0:
            lines.append("\nAll checks passed ✓")
        else:
            lines.append(f"\n{failed} check(s) need attention")
        return "\n".join(lines)

    def quick_check_file(self, file_path: str) -> str:
        """只检查单个文件，快速返回结果。"""
        results = []
        results.append(self.check_syntax([file_path]))
        results.append(self.run_pyflakes([file_path]))
        return self.report(results)

    # ---------- 工具 ----------

    def _find_py_files(self) -> List[str]:
        out = []
        for root, dirs, files in os.walk(self.work_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for f in files:
                if f.endswith(".py"):
                    fp = os.path.join(root, f)
                    out.append(os.path.relpath(fp, self.work_dir))
        return out
