# -*- coding: utf-8 -*-
"""测试和质量工具 —— 所有命令均走 cwd=work_dir

安全策略：
1. ALLOWED_COMMANDS 是 frozenset 白名单 —— 只有显式列出的命令才能执行
2. _check_argv 做参数长度 / 空字符 / 危险操作符（; | && 等）二次校验
3. cwd 永远指向项目 work_dir（由外部 ProjectSandbox.work_dir 提供）
4. 单个命令输出截断（6000/5000 字符），避免子进程输出 DOS
5. lint_code / check_types / format_code 也走白名单：只允许 python3 -m 子命令
6. 所有子进程走 subprocess_slot 全局并发/速率限制
7. 使用 start_new_session=True 创建独立进程组，超时可 kill 整个进程组
8. 环境变量清洗，仅保留最小安全集合
"""

import logging

logger = logging.getLogger(__name__)

import os
import signal
import subprocess
import shlex

from src.tools.subprocess_limits import subprocess_slot, apply_child_limits


# 只允许 Python 质量检查工具（通过 python3 -m 方式执行，避免直接调用 npm/pip/npx 等高危命令）
ALLOWED_COMMANDS = frozenset({
    "python3", "python", "pytest", "pyflakes", "mypy", "ruff", "black",
    "flake8",
})

_MAX_ARGV = 64
_MAX_TOKEN_LENGTH = 512

# 安全环境变量白名单（与 command_tools 保持一致）
_SAFE_ENV_KEYS = frozenset({"LANG", "LC_ALL", "TERM", "TZ"})


def _default_work_dir() -> str:
    """获取当前工作目录（兜底）。"""
    try:
        return os.path.realpath(os.getcwd())
    except Exception:
        return os.getcwd()


def _sanitize_env() -> dict:
    """构造安全的子进程环境变量，剥离敏感信息。"""
    env: dict = {}
    for k, v in os.environ.items():
        if k not in _SAFE_ENV_KEYS:
            continue
        if any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")):
            continue
        env[k] = v
    # 固定 PATH 为可信系统目录
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    # 强制 Python 不生成 .pyc 文件、不缓冲输出
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _safe_popen_kwargs() -> dict:
    """返回安全的 Popen 关键字参数：新进程组 + 子进程资源限制。"""
    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
        kwargs["preexec_fn"] = apply_child_limits
    return kwargs


def _kill_process_group(proc: subprocess.Popen) -> None:
    """安全杀死整个进程组（防止孙子进程成为孤儿）。"""
    if proc is None:
        return
    try:
        if os.name == "posix":
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(pgid, signal.SIGKILL)
                    proc.wait(timeout=2)
            except (ProcessLookupError, OSError):
                pass
        else:
            proc.kill()
            proc.wait(timeout=2)
    except Exception:
        pass


def _run_safely(cmd_tokens, cwd, timeout, max_output):
    """安全执行子进程：带槽位限制、进程组管理、超时清理、输出截断。

    Returns:
        (returncode: int, output: str)  returncode 为进程退出码，-1 表示启动失败/超时。
    """
    env = _sanitize_env()
    popen_kwargs = _safe_popen_kwargs()
    proc = None
    try:
        with subprocess_slot():
            proc = subprocess.Popen(
                cmd_tokens, shell=False, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, **popen_kwargs,
            )
            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
                out = (stdout_bytes.decode("utf-8", errors="replace") or "") + "\n" + \
                      (stderr_bytes.decode("utf-8", errors="replace") or "")
                return proc.returncode, "$ " + " ".join(cmd_tokens) + "\nexit=" + \
                    str(proc.returncode) + "\n" + out[:max_output]
            except subprocess.TimeoutExpired:
                _kill_process_group(proc)
                # 排空管道并关闭 fd，防止文件描述符泄漏
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                return -1, "超时 (" + str(timeout) + "s)"
    except RuntimeError as e:
        return -1, "子进程限制触发: " + str(e)
    except Exception as e:
        if proc is not None:
            _kill_process_group(proc)
        return -1, str(e)
    finally:
        if proc is not None and proc.poll() is None:
            _kill_process_group(proc)


class QualityTools:
    """测试和质量工具集：run_tests, lint_code, check_types, format_code, validate"""

    def __init__(self, executor):
        self._exec = executor
        self._work_dir = None

    def _safe_work_root(self) -> str:
        """拿到项目 work_dir 的绝对路径；拿不到就回退到 os.getcwd()。"""
        sandbox = getattr(self._exec, "sandbox", None)
        candidate = None
        if sandbox is not None:
            candidate = getattr(sandbox, "work_dir", None)
        if not candidate:
            for attr in ("root", "base_dir", "project_dir", "cwd"):
                candidate = getattr(sandbox, attr, None)
                if candidate:
                    break
        if not candidate:
            return _default_work_dir()
        try:
            return str(os.path.realpath(str(candidate)))
        except Exception:
            return _default_work_dir()

    def _check_argv(self, argv) -> bool:
        """验证命令白名单（fail-closed 设计）。"""
        if not argv:
            return False
        if len(argv) > _MAX_ARGV:
            return False
        if argv[0] not in ALLOWED_COMMANDS:
            return False
        for token in argv:
            if not token or not isinstance(token, str):
                return False
            if len(token) > _MAX_TOKEN_LENGTH:
                return False
            if "\x00" in token or "\n" in token or "\r" in token:
                return False
            for danger in (";", "|", "&", "`", "$(", "${", ">>", ">", "<"):
                if danger in token:
                    return False
        return True

    def run_tests(self, call):
        scope = str(call.get("scope") or ".").strip() or "."
        pattern = str(call.get("pattern") or "").strip()
        if len(scope) > 512 or len(pattern) > 512:
            return False, "[run_tests] 参数过长"
        if "\x00" in scope or "\x00" in pattern or "\n" in scope or "\n" in pattern:
            return False, "[run_tests] 参数含非法控制字符"
        if scope.startswith("-") or (pattern and pattern.startswith("-")):
            return False, "[run_tests] scope/pattern 不允许以 '-' 开头"
        if pattern:
            for ch in (";", "|", "&", "`", "$", "\n", "\r", "\t"):
                if ch in pattern:
                    return False, f"[run_tests] pattern 含非法字符: {ch}"
        if os.path.isabs(scope) or ".." in scope.replace("\\", "/").split("/"):
            return False, "[run_tests] scope 必须是工作目录下的相对路径"

        cmd_tokens = ["python3", "-m", "pytest"]
        if pattern:
            cmd_tokens.extend(["-k", pattern])
        cmd_tokens.extend(["--", scope])

        retcode, msg = _run_safely(cmd_tokens, self._safe_work_root(), timeout=300, max_output=6000)
        if retcode == -1:
            return False, "[run_tests] " + msg
        return True, "[run_tests] " + msg

    @staticmethod
    def _split_file_args(files: str):
        """将 filenames 字符串安全拆分为参数列表，拒绝选项注入与路径穿越。

        采用 fail-closed 策略：如果原始输入包含 shell 元字符（; | & ` $ 等），
        直接返回空列表，防止攻击者通过构造特殊文件名绕过过滤。
        """
        out = []
        if not isinstance(files, str):
            return out
        # 先对原始输入做 shell 元字符检查（fail-closed）
        for ch in (";", "|", "&", "`", "$", "\n", "\r", "\t", ">", "<", "(", ")", "{", "}", "[", "]", "*", "?"):
            if ch in files:
                return []
        try:
            tokens = shlex.split(files, posix=True)
        except ValueError:
            return out
        for fn in tokens:
            if not fn or len(fn) > 256 or "\x00" in fn or "\n" in fn:
                continue
            if fn.startswith("-"):
                continue
            if os.path.isabs(fn) or ".." in fn.replace("\\", "/").split("/"):
                continue
            out.append(fn)
        return out

    def lint_code(self, call):
        files = str(call.get("filenames", "") or "").strip()
        engine = str(call.get("engine", "") or "pyflakes").strip().lower()
        if engine not in ("pyflakes", "flake8", "ruff", "pylint"):
            engine = "pyflakes"
        if not files:
            return False, "[lint_code] 缺少 filenames"
        cmd_tokens = ["python3", "-m", engine, "--"]
        for fn in self._split_file_args(files):
            cmd_tokens.append(fn)
        if len(cmd_tokens) <= 4:
            return False, "[lint_code] 无可操作文件"
        if not self._check_argv(cmd_tokens):
            return False, "[lint_code] 命令不在白名单中或含非法字符"
        retcode, msg = _run_safely(cmd_tokens, self._safe_work_root(), timeout=120, max_output=5000)
        if retcode == -1:
            return False, "[lint_code] " + msg
        return True, "[lint_code] " + msg

    def check_types(self, call):
        files = str(call.get("filenames", "") or "").strip()
        if not files:
            return False, "[check_types] 缺少 filenames"
        cmd_tokens = ["python3", "-m", "mypy", "--"]
        for fn in self._split_file_args(files):
            cmd_tokens.append(fn)
        if len(cmd_tokens) <= 4:
            return False, "[check_types] 无可操作文件"
        if not self._check_argv(cmd_tokens):
            return False, "[check_types] 命令不在白名单中或含非法字符"
        retcode, msg = _run_safely(cmd_tokens, self._safe_work_root(), timeout=180, max_output=5000)
        if retcode == -1:
            return False, "[check_types] " + msg
        return True, "[check_types] " + msg

    def format_code(self, call):
        files = str(call.get("filenames", "") or "").strip()
        if not files:
            return False, "[format_code] 缺少 filenames"
        safe_files = self._split_file_args(files)
        if not safe_files:
            return False, "[format_code] 无可操作文件"
        for engine in ("black", "autopep8", "ruff"):
            cmd_tokens = ["python3", "-m", engine, "--"] + list(safe_files)
            if not self._check_argv(cmd_tokens):
                continue
            retcode, msg = _run_safely(cmd_tokens, self._safe_work_root(), timeout=120, max_output=2000)
            if retcode == 0:
                return True, "[format_code] (" + engine + ") " + msg
        return False, "[format_code] black/autopep8/ruff 均未成功安装或文件格式化失败"

    # ========== 自动验证工具（对传入路径做强校验）==========
    def _get_validator(self):
        """懒加载验证器：若外部有 `src.analysis.auto_validation` 模块则使用它。"""
        if not hasattr(self._exec, "_validator"):
            try:
                from src.analysis.auto_validation import AutoValidator
                self._exec._validator = AutoValidator(self._safe_work_root())
            except Exception:
                self._exec._validator = False
        val = self._exec._validator
        if val is False:
            return None
        return val

    def validate_all(self, call):
        try:
            files_raw = call.get("files", "") or ""
            targets = None
            if files_raw:
                tokens = []
                for f in str(files_raw).split(","):
                    f = f.strip()
                    if not f or len(f) > 256:
                        continue
                    if f.startswith("/") or f.startswith("\\") or ".." in f:
                        continue
                    if "\x00" in f:
                        continue
                    tokens.append(f)
                targets = tokens or None
            v = self._get_validator()
            if v is None:
                return False, "[validate_all] 验证器未安装（缺少 src.analysis.auto_validation）"
            results = v.validate_all(targets)
            return True, str(results)
        except Exception as e:
            return False, "[validate_all] 执行失败: " + str(e)

    def validate_file(self, call):
        fp = str(call.get("filename", "") or call.get("path", "") or "").strip()
        if not fp:
            return False, "[validate_file] 缺少 filename"
        if len(fp) > 512 or "\x00" in fp:
            return False, "[validate_file] filename 非法"
        if fp.startswith("/") or fp.startswith("\\") or ".." in fp.replace("\\", "/"):
            return False, "[validate_file] 拒绝绝对路径或穿越路径"
        try:
            v = self._get_validator()
            if v is None:
                return False, "[validate_file] 验证器未安装"
            return True, str(v.quick_check_file(fp))
        except Exception as e:
            return False, "[validate_file] 失败: " + str(e)

    def quick_syntax_check(self, call):
        try:
            files_raw = call.get("files", "") or ""
            targets = None
            if files_raw:
                tokens = []
                for f in str(files_raw).split(","):
                    f = f.strip()
                    if not f or len(f) > 256:
                        continue
                    if f.startswith("/") or f.startswith("\\") or ".." in f:
                        continue
                    if "\x00" in f:
                        continue
                    tokens.append(f)
                targets = tokens or None
            v = self._get_validator()
            if v is None:
                return False, "[quick_syntax_check] 验证器未安装"
            r = v.check_syntax(targets)
            return True, str(r)
        except Exception as e:
            return False, "[quick_syntax_check] 失败: " + str(e)
