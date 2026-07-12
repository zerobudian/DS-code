# -*- coding: utf-8 -*-
"""命令执行工具 — 从 tool_executor.py 拆分

安全加固要点:
1. 命令文本长度限制 + DANGEROUS_PATTERNS 覆盖管道/引号嵌套/sudo/fork 炸弹
2. shell=False + shlex.split() 解析，解析失败直接拒绝
3. argv[0] 必须落在 ALLOWED_COMMANDS（frozenset，不可变白名单）
4. ConfirmationGate 对 on_confirm_command 返回值做 bool 校验；异常时拒绝
5. execute_custom 的 python 分支限制 code 字节数 + 子代理禁用
6. calculator：表达式长度 + AST 节点深度 / 节点类型白名单双重校验
"""

import logging

logger = logging.getLogger(__name__)

import os
import re
import shlex
import signal
import subprocess
import json
import tempfile
import queue
import threading
from typing import Dict, List, Optional, Tuple

from src.core.config import Config
from src.core.confirmation import ConfirmationError
from src.tools.subprocess_limits import (
    subprocess_slot,
    acquire_subprocess_slot,
    release_subprocess_slot,
    _preexec_child_safety,
)


def _sanitize_log_text(text: object) -> str:
    """清理日志中的控制字符，防止用户输入被当作日志格式化字符串或转义序列（H-006）。"""
    if text is None:
        return ""
    s = str(text)
    # 替换 C0 控制字符 (\x00-\x1f) 为占位符，保留常规可打印字符与 Unicode
    return "".join(ch if not (0 <= ord(ch) <= 31) else "?" for ch in s)


def _safe_terminate(proc: Optional[subprocess.Popen], force_kill: bool = False) -> None:
    """安全清理 Popen 句柄，优先以进程组为单位终止，防止子进程继续运行。

    策略：
    - 若子进程使用 start_new_session=True / preexec_fn=os.setsid，则通过 killpg
      向整个进程组发送 SIGTERM/SIGKILL；
    - 兜底：对单个进程调用 terminate()/kill()；
    - 任何异常都被吞。
    """
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return

    pgid: Optional[int] = None
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pass

    def _send(sig: int) -> None:
        try:
            if pgid is not None and pgid > 0:
                os.killpg(pgid, sig)
            else:
                os.kill(proc.pid, sig)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    _send(signal.SIGTERM)
    try:
        # 给进程最多 1s 响应 SIGTERM
        proc.wait(timeout=1)
        return
    except Exception as _exc:
        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    if force_kill:
        _send(signal.SIGKILL)
        try:
            proc.wait(timeout=2)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)


# 基础命令白名单：仅保留只读/构建/版本控制/常见代码工具
# 包管理器（pip/npm/npx）和编译器存在下载/执行任意代码风险，后续版本应引入
# 二级确认；当前先收紧入口。
ALLOWED_COMMANDS = frozenset({
    # 文件查看
    "ls", "cat", "head", "tail", "grep", "find", "wc", "sort", "uniq", "echo",
    "pwd", "which", "diff", "mkdir", "cp", "mv", "touch", "chmod", "env",
    "printenv", "id", "df", "du", "ps", "top", "tar", "unzip",
    # Python / Node 运行时与常用代码检查
    "python3", "python", "node",
    "pytest", "pyflakes", "mypy", "black", "ruff",
    "eslint", "prettier", "tsc",
    # 版本控制
    "git",
    # 构建工具（存在风险，需限制仅用于当前项目）
    "make", "cargo", "go", "rustc", "javac", "java", "gcc", "g++", "clang",
    "cmake", "meson", "ninja",
        # 包管理器（风险最高）。npm/pip 已被移除白名单（H-005），
    # 仅保留 npx/yarn/pnpm，且仍需经过命令确认门。
    "npx", "yarn", "pnpm",
})

# 高危命令：已从 ALLOWED_COMMANDS 移除，需要额外二级确认或完全禁止
HIGH_RISK_COMMANDS = frozenset({"npm", "pip", "pip3"})

# 明确禁止的高危命令/工具（即使出现在 PATH 中也不允许直接执行）
FORBIDDEN_COMMANDS = frozenset({
    "curl", "wget", "docker", "vim", "nano", "vi", "emacs", "htop", "top",
    "ssh", "scp", "sftp", "ftp", "telnet", "nc", "netcat", "bash", "sh", "zsh",
})

# reviewer 模式下 run_command 允许的只读查询命令（仅用于本地权限检查）
_READONLY_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "grep", "find", "wc", "sort", "uniq",
    "pwd", "which", "diff", "printenv", "id", "df", "du", "ps", "top",
    "echo", "git",
})


_SAFE_ENV_KEYS = frozenset({"LANG", "LC_ALL", "TERM", "TZ"})

# 必须显式清除的高风险环境变量（即使父进程未设置也防止通过 extra 传入）
_EXPLICITLY_REMOVED_ENV_KEYS = frozenset({
    "HOME", "SHELL", "USER",
    "GIT_EDITOR", "EDITOR", "VISUAL",
    "SSH_AUTH_SOCK",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH",
    "PERL5LIB", "RUBYLIB", "CLASSPATH",
    "DYLD_INSERT_LIBRARIES",
})


def _is_safe_env_value(value: str) -> bool:
    """校验环境变量值是否安全：禁止路径回退、shell 元字符、变量扩展。"""
    if not isinstance(value, str):
        return False
    if ".." in value:
        return False
    if ";" in value or "|" in value:
        return False
    if "${" in value:
        return False
    if value.startswith("$"):
        return False
    return True


def _sanitize_command_env(extra: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
    """构造子进程环境变量白名单，剥离敏感 token/密钥，并固定 PATH。

    修复 CWE-95：仅保留最小必要环境变量，固定可信 PATH，并显式移除/覆盖
    LD_PRELOAD、LD_LIBRARY_PATH、PYTHONPATH 等常被用于注入的动态库/模块路径。

    C-003：若 extra 中显式包含 _EXPLICITLY_REMOVED_ENV_KEYS 中的任意键，
    整体返回 None（调用方应视为失败），而不是简单 pop 后继续执行。
    """
    env: Dict[str, str] = {}
    # 仅保留真正需要的最小环境变量，且过滤掉包含敏感字段名的键
    for k, v in os.environ.items():
        if k not in _SAFE_ENV_KEYS:
            continue
        if any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")):
            continue
        if _is_safe_env_value(v):
            env[k] = v

    # 固定为可信系统目录，顺序与常见 Linux 发行版一致
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

    # 显式覆盖/清除高风险注入变量
    for dangerous in _EXPLICITLY_REMOVED_ENV_KEYS:
        env.pop(dangerous, None)

    # C-003：extra 中不得包含显式移除的键，否则整体失败
    if extra:
        forbidden_in_extra = {k for k in extra if k in _EXPLICITLY_REMOVED_ENV_KEYS}
        if forbidden_in_extra:
            logger.info("[_sanitize_command_env] extra 包含高风险环境变量: %s", forbidden_in_extra)
            return None

    if extra:
        for k, v in extra.items():
            # M-017: 拒绝空字符串值的环境变量
            if v == "":
                logger.info("[_sanitize_command_env] extra 环境变量 %s 值为空字符串，拒绝", k)
                return None
            if k in _SAFE_ENV_KEYS and _is_safe_env_value(v):
                env[k] = v
    return env


# 安全获取 ast.Num / ast.Str（Python 3.8+ 中已废弃为 ast.Constant 的别名，
# Python 3.14 将彻底移除，因此在模块加载时做一次兼容处理）。
import ast as _ast_mod
_AST_NUM = getattr(_ast_mod, "Num", _ast_mod.Constant)
_AST_STR = getattr(_ast_mod, "Str", _ast_mod.Constant)


# 危险模式：覆盖管道 shell、重定向到敏感路径、sudo、setuid、危险磁盘命令、fork 炸弹、
# 命令替换（反引号 / $()）、命令拼接（; && ||）等常见注入形式。
# 所有正则统一使用 re.IGNORECASE + 额外 \s+ 放宽，防止大小写/空白绕过。
# 注意：不再使用简单的 \b 全词匹配去误杀普通参数里的关键词（如 "echo rm"），
# 改为只在命令起始位置或分隔符之后检测，避免安全修复引入功能回归。
_MAX_DANGEROUS_PATTERN_SUBMATCH_LEN = 200  # 限制子表达式匹配长度，降低 ReDoS 风险
DANGEROUS_PATTERNS = [
    # rm / 危险删除 —— 只在命令位置或分隔符后出现（避免误伤普通参数中的 "rm"）
    r"(?:^|[;|&])\s*rm\b",
    # rmdir 作为独立命令
    r"(?:^|[;|&])\s*rmdir\b",
    # 格式化 / 磁盘写入 / dd 等块设备 —— 只在命令位置或分隔符后
    r"(?:^|[;|&])\s*(mkfs|format|fdisk|dd|dd_rescue|dcfldd)\b",
    # fork 炸弹 / 递归函数调用
    r":\s*\)\s*\{",
    # 通过管道将网络工具输出交给 shell（直接执行恶意脚本）
    r"(wget|curl)\b[^\n]*\|\s*(bash|sh|zsh|ksh|fish|dash|sudo|python|python3|node)\b",
    # 重定向到 / 系统路径写入（/dev/null 除外，它是合法输出抑制）
    r">>?\s*/(etc|usr|var|boot|proc|sys|root|bin|sbin|tmp)/",
    r">>?\s*/dev/(?!null\b)[^/\s]+",
    # sudo / su 提权 —— 只在命令位置或分隔符后
    r"(?:^|[;|&])\s*sudo\b",
    r"(?:^|[;|&])\s*su\s+-",
    # setuid / setgid / sticky bit（例如 chmod 4755、chmod 2755、chmod 1755、chmod u+s）
    # P1-5 修复: 正则 [4-7] 改为 [1-7]，覆盖 setgid(2xxx) 和 sticky bit(1xxx)
    r"chmod\s+.*\+s", r"chmod\s+\d*[1-7]\d{3}\b",
    # 递归 chown / chmod
    r"\b(chown|chmod)\s+(-R|--recursive)\b",
    # 关机 / 重启 —— 只在命令位置或分隔符后
    r"(?:^|[;|&])\s*(shutdown|reboot|halt|poweroff)\b",
    # Windows 风格强制删除
    r"\bdel\s+/s\s+/q\b", r"\brmdir\s+/s\b", r"\bformat\s+[A-Za-z]:",
    # 反向 shell 关键词（即使在参数里）
    r"/dev/(tcp|udp)/",
    # 反引号 / $( ) 形式的命令替换
    rf"`[^`\n]{{0,{_MAX_DANGEROUS_PATTERN_SUBMATCH_LEN}}}`",
    rf"\$\([^)]{{0,{_MAX_DANGEROUS_PATTERN_SUBMATCH_LEN}}}\)",
    # $ENV / ${...} 形式的环境变量注入（即使 shell=False，也禁止传入潜在 payload）
    r"\$[A-Za-z_]\w*", r"\$\{[^}]*\}",
    # 分号后的二次命令（命令拼接绕过）
    r";\s*(rm|sh|bash|zsh|sudo|reboot|shutdown|mkfs|dd|nc|ncat|curl|wget)\b",
    # && / || 拼接危险命令
    r"(&&|\|\|)\s*(rm|sh|bash|zsh|sudo|reboot|shutdown|mkfs|dd|nc|ncat|curl|wget|chmod|chown)\b",
    # 管道后调用 shell 解释器
    r"\|\s*(sh|bash|zsh|ksh|fish|dash)\b",
    # 直接调用危险解释器（不在白名单内会被 argv[0] 挡住，但这里再次防御）
    r"(?:^|[;|&])\s*(eval|exec|nohup|setsid|daemonize)\b",
    # nc / ncat 等网络后门 —— 只在命令位置或分隔符后
    r"(?:^|[;|&])\s*(nc|ncat|netcat|socat)\b",
]


# ======= 全局硬限制（防御 DoS / 注入） =======
_MAX_COMMAND_LENGTH = 4096           # 命令字符串最大长度
_MAX_CALC_EXPR_LENGTH = 400          # 计算器表达式最大字符
_MAX_CALC_AST_DEPTH = 24             # 计算器 AST 最大深度（防御深度递归）
_MAX_PYTHON_CODE_BYTES = 32 * 1024   # execute_custom Python 代码最大字节
_MAX_PYTHON_CPU_SEC = 10             # Python 沙箱子进程 CPU 时间上限
_MAX_PYTHON_MEM_BYTES = 256 * 1024 * 1024  # Python 沙箱子进程地址空间上限（256MB）
_MAX_PYTHON_PROC_TIMEOUT = 60        # Python 沙箱子进程 wall-clock 超时

# M-013: 危险模式正则匹配超时（秒）与输入长度上限
_REGEX_MATCH_TIMEOUT_SECONDS = 1.0
_MAX_DANGEROUS_PATTERN_INPUT_LENGTH = 8192
_regex_signal_lock = threading.Lock()  # 保护 SIGALRM 信号设置，防止多线程互相干扰
DEFAULT_COMMAND_TIMEOUT = 300        # M-011: run_command / run_command_stream 默认超时（秒）
_MAX_COMMAND_OUTPUT_DISPLAY = 4000   # run_command 返回输出最大显示字符数


class _RegexTimeoutError(Exception):
    """正则匹配超时异常。"""


def _raise_regex_timeout(_signum, _frame) -> None:
    """SIGALRM 处理函数：中断可能陷入灾难性回溯的正则匹配。"""
    raise _RegexTimeoutError("危险模式正则匹配超时")


def _regex_search_with_timeout(pattern: str, text: str, flags: int = 0):
    """带超时保护的正则匹配。

    POSIX 环境下优先使用 setitimer(ITIMER_REAL) 提供亚秒级超时；
    不可用时回退到 signal.alarm。非主线程或无法设置信号时，回退到
    先截断输入再执行普通 re.search（输入上限 8192 字符）。
    """
    if len(text) > _MAX_DANGEROUS_PATTERN_INPUT_LENGTH:
        text = text[:_MAX_DANGEROUS_PATTERN_INPUT_LENGTH]

    # 仅在主线程中使用信号超时（SIGALRM 是进程级的，非主线程设置不安全）
    is_main_thread = threading.current_thread() is threading.main_thread()

    if not is_main_thread:
        # Bug fix #11: 非主线程无法使用 signal 超时，但输入已截断到 8192 字符
        # 且 DANGEROUS_PATTERNS 都是简单交替模式，灾难性回溯风险极低。
        # 为了纵深防御，使用 ThreadPoolExecutor 提供线程级超时保护。
        try:
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
            _executor = ThreadPoolExecutor(max_workers=1)
            try:
                _future = _executor.submit(re.search, pattern, text, flags)
                try:
                    return _future.result(timeout=_REGEX_MATCH_TIMEOUT_SECONDS)
                except FuturesTimeoutError:
                    logger.warning(
                        "[_check_dangerous_patterns] 非主线程正则匹配超时，按命中处理: pattern=%s",
                        pattern,
                    )
                    return True
            finally:
                # shutdown(wait=False) 避免卡住线程等待已超时的正则完成
                _executor.shutdown(wait=False)
        except Exception:
            # 线程池方案不可用时，回退到截断后的普通匹配（输入已限长，ReDoS 风险可接受）
            try:
                return re.search(pattern, text, flags)
            except Exception:
                return True  # 解析失败按命中处理，安全保守

    with _regex_signal_lock:
        try:
            if hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer"):
                old_handler = signal.signal(signal.SIGALRM, _raise_regex_timeout)
                # setitimer 返回 (old_delay, old_interval)，必须保存并在 finally 中恢复
                old_delay, old_interval = signal.setitimer(signal.ITIMER_REAL, _REGEX_MATCH_TIMEOUT_SECONDS)
                try:
                    return re.search(pattern, text, flags)
                finally:
                    signal.setitimer(signal.ITIMER_REAL, old_delay, old_interval)
                    signal.signal(signal.SIGALRM, old_handler)
            elif hasattr(signal, "SIGALRM"):
                old_handler = signal.signal(signal.SIGALRM, _raise_regex_timeout)
                old_alarm = signal.alarm(max(1, int(_REGEX_MATCH_TIMEOUT_SECONDS)))
                try:
                    return re.search(pattern, text, flags)
                finally:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old_handler)
                    if old_alarm:
                        signal.alarm(old_alarm)
            else:
                return re.search(pattern, text, flags)
        except _RegexTimeoutError:
            logger.warning(
                "[_check_dangerous_patterns] 正则匹配超时，按命中处理: pattern=%s",
                pattern,
            )
            # 返回真值，让调用方按命中危险模式处理
            return True
        except ValueError:
            # 非主线程无法设置信号：回退到截断后的普通匹配
            try:
                return re.search(pattern, text, flags)
            except Exception:
                return True


# Python 沙箱：允许导入的模块白名单
# 仅保留安全的基础/数学/数据处理模块；移除 subprocess、socket、os、sys 等危险模块。
_PYTHON_ALLOWED_IMPORTS = frozenset({
    "math", "random", "json", "re", "datetime", "collections", "statistics",
})

# Python 沙箱：禁止出现的名称（危险函数与敏感模块）
_PYTHON_FORBIDDEN_NAMES = frozenset({
    "eval", "exec", "compile", "open", "input",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "__import__", "breakpoint", "quit", "exit",
    "super", "type", "object", "class", "__class__", "__bases__", "__subclasses__",
    "__loader__", "__spec__", "__builtins__",
    "os", "sys", "subprocess", "socket", "requests", "urllib", "ftplib", "telnetlib",
    "shutil", "pathlib", "importlib", "platform", "pwd", "grp", "ctypes",
    "builtins", "types", "_imp",
})

# Python 沙箱：允许在子进程中使用的内置函数白名单
_PYTHON_SAFE_BUILTINS = frozenset({
    "print", "range", "len", "abs", "sum", "min", "max", "round",
    "zip", "enumerate", "sorted", "reversed",
    "isinstance", "issubclass", "callable",
    "chr", "ord", "hex", "oct", "bin",
    "repr", "str", "int", "float", "bool",
    "list", "tuple", "dict", "set", "frozenset",
    "complex", "bytes", "bytearray",
    "all", "any", "filter", "map", "iter", "next", "slice",
    "staticmethod", "classmethod", "property",
    # 注意：不暴露 object/type/open/exec/eval/getattr/__import__/help 等危险入口
})

# Python 沙箱：允许暴露的模块属性白名单。
# 不直接暴露完整模块对象，防止通过 module.sys / module.os 等残留属性逃逸。
_SAFE_MODULE_ATTRS = {
    "math": frozenset({
        "pi", "e", "tau", "inf", "nan",
        "sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
        "sinh", "cosh", "tanh", "asinh", "acosh", "atanh",
        "exp", "log", "log10", "log2", "pow", "ceil", "floor",
        "fabs", "factorial", "gcd", "degrees", "radians",
        "isfinite", "isinf", "isnan", "trunc", "modf", "frexp", "ldexp",
        "comb", "perm", "prod", "sumprod", "dist", "hypot",
    }),
    "random": frozenset({
        "random", "randint", "randrange", "choice", "choices", "sample", "shuffle",
        "seed", "uniform", "gauss", "triangular", "betavariate", "expovariate",
        "gammavariate", "lognormvariate", "normalvariate", "vonmisesvariate",
        "paretovariate", "weibullvariate",
    }),
    "json": frozenset({"loads", "dumps"}),
    "re": frozenset({"match", "search", "findall", "finditer", "split", "sub", "subn", "compile", "escape", "fullmatch"}),
    "datetime": frozenset({"datetime", "date", "time", "timedelta", "MAXYEAR", "MINYEAR", "UTC"}),
    "collections": frozenset({"Counter", "deque", "OrderedDict", "defaultdict", "ChainMap", "UserDict", "UserList", "UserString"}),
    "statistics": frozenset({
        "mean", "median", "median_low", "median_high", "median_grouped",
        "mode", "multimode", "stdev", "pstdev", "variance", "pvariance",
        "quantiles", "covariance", "correlation", "linear_regression",
    }),
}


def _check_command_permission(parts: List[str], tool_name: str = "run_command") -> Optional[Tuple[bool, str]]:
    """reviewer 模式下只允许白名单内的只读查询命令。

    返回值: None 表示允许；否则为 (False, error_msg)。
    """
    try:
        if Config().permission_mode != "reviewer":
            return None
    except Exception:
        return False, f"[{tool_name}] 无法验证权限模式，拒绝执行"
    if not parts:
        return False, f"[{tool_name}] reviewer 模式禁止执行空命令"
    if parts[0] not in _READONLY_COMMANDS:
        return False, f"[{tool_name}] reviewer 模式仅允许只读查询命令: {parts[0]}"
    return None


def _check_dangerous_patterns(cmd_text: str) -> str:
    """扫描命令文本是否命中危险模式。返回第一个命中的模式描述；无命中返回空串。

    增强防护（CWE-78）：
    1. 拒绝非 ASCII 字符与控制字符，防止同形异义字符绕过解析；
    2. 同时检查原始和 Unicode 转义后的版本（防止 \\uXXXX 绕过）；
    3. 对第一个单词做额外检查：shlex.split 后检查第一个命令是否在危险命令集合中。
    """
    if not cmd_text:
        return ""

    # 拒绝控制字符（含换行、TAB、响铃等）与 DEL，但允许正常的 Unicode 可打印字符，
    # 避免误杀包含中文文件名/参数的合法命令。
    for ch in cmd_text:
        o = ord(ch)
        if o < 32 or o == 127:
            return "[control-character]"

    # 第一轮：检查原始版本（带超时保护，防止 ReDoS）
    for pat in DANGEROUS_PATTERNS:
        try:
            if _regex_search_with_timeout(pat, cmd_text, re.IGNORECASE | re.DOTALL):
                return pat
        except re.error:
            # 单个正则异常不影响整条命令
            continue

    # 第二轮：检查 Unicode 转义后的版本（防止 \\uXXXX 等 Unicode 转义绕过）
    try:
        unicode_unescaped = cmd_text.encode("raw_unicode_escape", errors="ignore").decode("unicode_escape", errors="ignore")
        if unicode_unescaped and unicode_unescaped != cmd_text:
            for pat in DANGEROUS_PATTERNS:
                try:
                    if _regex_search_with_timeout(pat, unicode_unescaped, re.IGNORECASE | re.DOTALL):
                        return pat
                except re.error as _exc:
                    logger.debug("跳过异常项: %s", _exc, exc_info=True)
                    continue
    except (UnicodeError, LookupError) as _exc:
        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    # 第三轮：对第一个单词做额外检查
    # 用 shlex.split 解析后检查第一个命令是否在危险命令集合中
    _DANGEROUS_FIRST_WORDS = {"rm", "mkfs", "dd", "sudo", "reboot", "shutdown"}
    try:
        parts = shlex.split(cmd_text)
        if parts and parts[0] in _DANGEROUS_FIRST_WORDS:
            return parts[0]
    except ValueError as _exc:
        # C-001：shlex.split 解析失败直接按危险处理，避免后续继续执行
        logger.info("[_check_dangerous_patterns] shlex.split 解析失败，按危险模式处理")
        logger.debug("[_check_dangerous_patterns] shlex.split 解析失败详情: %s", _exc, exc_info=True)
        return "[shlex-error]"

    return ""


# wget/curl 输出标志黑名单（防止 -O/-o 写入任意路径）
_WGET_OUTPUT_FLAGS = frozenset({"-O", "--output-document"})
_CURL_OUTPUT_FLAGS = frozenset({"-o", "--output"})
_WGET_SHORT_OUTPUT_RE = re.compile(r"^-[a-zA-Z]*[Oo]$")
_CURL_SHORT_OUTPUT_RE = re.compile(r"^-[a-zA-Z]*[oO]$")


def _check_wget_curl_output_flags(parts):
    """检查 wget/curl 是否使用文件输出标志（防止 -O/-o 写任意路径）。"""
    if not parts:
        return ""
    cmd = parts[0]
    if cmd == "wget":
        banned = _WGET_OUTPUT_FLAGS
        short_re = _WGET_SHORT_OUTPUT_RE
    elif cmd == "curl":
        banned = _CURL_OUTPUT_FLAGS
        short_re = _CURL_SHORT_OUTPUT_RE
    else:
        return ""
    for flag in parts[1:]:
        if flag in banned or short_re.match(flag):
            return flag
    return ""


# 解释器的内联代码参数（P0 修复: 禁止 python3 -c / node -e 绕过沙箱）
_INTERPRETER_INLINE_FLAGS = {
    "python": frozenset({"-c", "--command"}),
    "python3": frozenset({"-c", "--command"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "nodejs": frozenset({"-e", "--eval", "-p", "--print"}),
    "bash": frozenset({"-c"}),
    "sh": frozenset({"-c"}),
    "zsh": frozenset({"-c"}),
    "ruby": frozenset({"-e"}),
    "perl": frozenset({"-e"}),
    "php": frozenset({"-r", "-B", "-R", "-F", "-E"}),
}

# 命令的危险选项（可改变目标路径/执行任意命令）
_DANGEROUS_CMD_FLAGS = {
    "tar": frozenset({"-C", "--directory", "--to-command", "--checkpoint-action", "-I", "--use-compress-program"}),
    "find": frozenset({"-exec", "-execdir", "-ok", "-okdir"}),
    "cp": frozenset({"-t", "--target-directory"}),  # cp -t /tmp 可写入沙箱外
    "mv": frozenset({"-t", "--target-directory"}),  # mv -t /tmp 可移动到沙箱外
    "mkdir": frozenset(),  # mkdir 路径参数由 _check_absolute_path_args 校验
    "chmod": frozenset(),
    "ln": frozenset({"-t", "--target-directory"}),  # ln -t /tmp 可创建沙箱外链
}

# 遵循 POSIX -- 语义的命令：-- 后的参数均为文件名，应跳过危险选项检测。
# find 不在此列：GNU find 中 -- 仅终止起始路径列表，不终止表达式，
# -- 之后的 -exec 等选项仍然有效。
_DASH_DASH_SAFE_CMDS = frozenset({"tar", "cp", "mv", "ln"})


def _check_interpreter_inline_code(parts):
    """检查解释器是否使用 -c/-e 等内联代码参数（防止绕过 execute_custom 沙箱）。

    返回空字符串表示安全，否则返回拒绝原因。
    P0-3 修复: 检测 --eval=code / --print=code 等长选项带等号的形式，防止 node 等解释器绕过检测。
    """
    if not parts:
        return ""
    cmd = os.path.basename(parts[0])
    # 去掉版本号后缀：python3.11 -> python3, python2 -> python
    base_cmd = cmd.rstrip("0123456789.") if cmd[0].isalpha() else cmd
    inline_flags = _INTERPRETER_INLINE_FLAGS.get(base_cmd) or _INTERPRETER_INLINE_FLAGS.get(cmd)
    if not inline_flags:
        return ""
    for arg in parts[1:]:
        if not isinstance(arg, str):
            continue
        # 精确匹配：-c / -e / --eval / --command 等
        if arg in inline_flags:
            return f"{cmd} 不允许使用 {arg} 参数（内联代码会绕过沙箱），请使用 execute_custom 工具"
        for fl in inline_flags:
            # 短选项连写形式：-ecode / -c'code'（短选项以单 - 开头，不是 --）
            if fl.startswith("-") and not fl.startswith("--"):
                if arg.startswith(fl) and len(arg) > len(fl):
                    return f"{cmd} 不允许使用 {fl} 参数（内联代码会绕过沙箱），请使用 execute_custom 工具"
            # 长选项带等号形式：--eval=code / --command="code"
            if fl.startswith("--"):
                if arg == fl or arg.startswith(fl + "="):
                    return f"{cmd} 不允许使用 {fl} 参数（内联代码会绕过沙箱），请使用 execute_custom 工具"
    return ""


def _check_absolute_path_args(parts, work_dir):
    """检查命令参数中是否包含逃逸出 work_dir 的路径（绝对路径或含 .. 的相对路径）。

    返回空字符串表示安全，否则返回拒绝原因。
    P0 修复: 不仅检查绝对路径，还检查相对路径中的 .. 段（防止 cat ../../etc/passwd 沙箱逃逸）。
    """
    if not parts or work_dir in (None, "", "."):
        return ""
    work_dir_real = os.path.realpath(work_dir)
    for i, arg in enumerate(parts[1:], 1):
        if not isinstance(arg, str):
            continue
        # 跳过选项标志：-C, -rf, --verbose 等（短选项以-开头后跟字母，长选项以--开头）
        # 但 -1、-2.5 等负数参数不当作选项
        if arg.startswith("-") and len(arg) > 1:
            if arg.startswith("--"):
                # 长选项：检查 --flag=/path 形式中的路径部分
                if "=" in arg:
                    val = arg.split("=", 1)[1]
                    if val and (val.startswith("/") or ".." in val):
                        arg = val  # fall through to path check
                    else:
                        continue
                else:
                    continue
            elif not arg[1].isdigit() and arg[1] != ".":
                # 短选项（-C, -rf, -vC.., -I/bin/sh 等）：危险选项由 _check_dangerous_cmd_flags 检测
                # 这里检查短选项后直接拼接的路径值（如 -C/etc、-vC..）
                rest = arg[1:]
                # 先尝试 / 分隔的绝对路径
                if "/" in rest:
                    idx = rest.find("/")
                    path_candidate = rest[idx:]
                    try:
                        real = os.path.realpath(path_candidate)
                        if not (real == work_dir_real or real.startswith(work_dir_real + os.sep)):
                            return f"参数路径 {path_candidate[:200]} 不在项目目录内，已拒绝"
                    except OSError:
                        pass
                # 检查 .. 形式的相对路径（如 -C..、-vC..）
                # 找到选项字母后的第一个路径起点（非字母数字+非选项字符的位置）
                for split_idx in range(len(rest)):
                    ch = rest[split_idx]
                    if ch == "." and split_idx + 1 < len(rest) and rest[split_idx + 1] == ".":
                        path_candidate = rest[split_idx:]
                        try:
                            real = os.path.realpath(os.path.join(work_dir, path_candidate))
                            if not (real == work_dir_real or real.startswith(work_dir_real + os.sep)):
                                return f"参数路径 {path_candidate[:200]} 不在项目目录内，已拒绝"
                        except OSError:
                            pass
                        break
                continue
        # 空参数跳过
        if not arg:
            continue
        try:
            # 对绝对路径和含 .. 的相对路径都做 realpath 校验
            if arg.startswith("/"):
                real = os.path.realpath(arg)
            elif ".." in arg.split("/") or ".." in arg.split(os.sep):
                real = os.path.realpath(os.path.join(work_dir, arg))
            else:
                # 普通相对路径（无 ..）：在 cwd 内执行是安全的，但仍需验证拼接后不逃逸
                real = os.path.realpath(os.path.join(work_dir, arg))
        except OSError:
            continue
        # 路径必须位于 work_dir 内
        if not (real == work_dir_real or real.startswith(work_dir_real + os.sep)):
            return f"参数路径 {arg[:200]} 不在项目目录内，已拒绝"
    return ""


def _check_dangerous_cmd_flags(parts):
    """检查 tar/find 等命令的危险选项（如 -C / --to-command / -exec）。
    P0 修复: 检测短选项连写（-C/etc）和长选项等号形式（--directory=/etc），防止绕过。
    """
    if not parts:
        return ""
    cmd = os.path.basename(parts[0])
    dangerous = _DANGEROUS_CMD_FLAGS.get(cmd)
    if dangerous is None:
        return ""
    # "--" 终止符：仅对遵循 POSIX 语义的命令（tar/cp/mv/ln）跳过后续参数检测。
    # find 不识别 -- 终止符（GNU find 中 -- 仅终止起始路径列表，不终止表达式），
    # 因此 find -- -exec 中的 -exec 仍需被检测。
    seen_double_dash = False
    for arg in parts[1:]:
        if not isinstance(arg, str):
            continue
        # 一旦遇到过 "--"（仅对 POSIX 兼容命令），后续参数均为文件名，直接跳过
        if seen_double_dash:
            continue
        if arg == "--":
            if cmd in _DASH_DASH_SAFE_CMDS:
                seen_double_dash = True
                continue
            # find 等命令不识别 --，继续作为普通参数检测
        # 精确匹配
        if arg in dangerous:
            return f"{cmd} 不允许使用 {arg} 选项（可导致路径逃逸或任意命令执行）"
        # 检查连写/等号形式
        for fl in dangerous:
            if fl.startswith("--"):
                # 长选项：--directory=/etc 形式
                if arg == fl or arg.startswith(fl + "="):
                    return f"{cmd} 不允许使用 {fl} 选项（可导致路径逃逸或任意命令执行）"
                # GNU getopt_long 接受长选项的无歧义缩写（如 --to-com = --to-command）
                # 检测缩写：arg 去掉 = 后的值部分，如果是某个危险长选项的前缀则拒绝
                if arg.startswith("--"):
                    arg_name = arg.split("=", 1)[0]
                    if fl.startswith(arg_name) and len(arg_name) >= 4:
                        return f"{cmd} 不允许使用 {fl} 选项（缩写 {arg_name}，可导致路径逃逸或任意命令执行）"
            elif fl.startswith("-") and not fl.startswith("--"):
                # 短选项：-C/etc、-vC..（组合短选项中包含危险标志）、-I/bin/sh 形式
                fl_char = fl[1] if len(fl) == 2 else None
                if arg.startswith(fl) and len(arg) > len(fl):
                    return f"{cmd} 不允许使用 {fl} 选项（可导致路径逃逸或任意命令执行）"
                # 组合短选项：检查危险选项字母是否出现在 -xxx 中（如 -vC 包含 -C）
                if fl_char and arg.startswith("-") and not arg.startswith("--") and fl_char in arg[1:]:
                    return f"{cmd} 不允许使用 {fl} 选项（可导致路径逃逸或任意命令执行）"
    return ""


def _safe_split_command(cmd_text: str):
    """安全拆分命令：非 str / 超长 / 包含空字符 / shlex 异常 / 非 ASCII 都直接拒绝。

    增强（CWE-78）：在 shlex.split 之前先做字符白名单校验，确保解析器不会
    遇到同形异义字符或控制字符；拆分后再做 token 级长度与结构检查。
    """
    if not isinstance(cmd_text, str):
        return None
    if len(cmd_text) > _MAX_COMMAND_LENGTH:
        return None
    # 禁止嵌入空字符（可以绕过某些解析器）
    if "\x00" in cmd_text:
        return None
    # 拒绝控制字符与 DEL，允许 Unicode 可打印字符与空格，避免中文参数被误杀
    for ch in cmd_text:
        o = ord(ch)
        if o < 32 or o == 127:
            return None
    # 拒绝未转义的 shell 分隔符（;、|、&），防止 shlex 把它们合并到参数里
    in_quote = None
    escaped = False
    for ch in cmd_text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch in ('"', "'"):
            if in_quote == ch:
                in_quote = None
            elif in_quote is None:
                in_quote = ch
            continue
        if in_quote is None and ch in ";|&":
            return None
    try:
        parts = shlex.split(cmd_text, posix=True)
    except ValueError:
        return None
    # 去掉空 token
    parts = [p for p in parts if isinstance(p, str) and p != ""]
    if not parts:
        return None
    # 限制最大 token 数量，防止超长参数攻击
    if len(parts) > 64:
        return None
    # token 级长度限制，防止单个参数膨胀
    if any(len(p) > 1024 for p in parts):
        return None
    return parts


# ======= 辅助：安全解析命令二进制路径，避免 PATH 劫持 =======
# 可信系统目录（与 _sanitize_command_env 固定的 PATH 保持一致）。
# 同时从启动时的 PATH 中补充非世界可写、不在当前工作目录下的绝对路径目录，
# 避免把合法但安装到非标准路径的工具（如 nvm 中的 node/npm）误杀。
def _build_trusted_bin_dirs() -> Tuple[str, ...]:
    fixed = ["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    extras: List[str] = []
    try:
        cwd = os.path.realpath(os.getcwd())
    except Exception:
        cwd = ""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isabs(d):
            continue
        try:
            real = os.path.realpath(d)
            st = os.stat(real)
        except OSError:
            continue
        if not os.path.isdir(real):
            continue
        # 排除世界可写目录（如 /tmp），防止被恶意替换
        if st.st_mode & 0o002:
            continue
        # 排除当前工作目录下的子目录，防止项目内伪造二进制被当成可信命令
        if cwd and (real == cwd or real.startswith(cwd + os.sep)):
            continue
        if real not in fixed and real not in extras:
            extras.append(real)
    return tuple(fixed + extras)


_TRUSTED_BIN_DIRS = _build_trusted_bin_dirs()


def _resolve_safe_binary(name: str) -> Optional[str]:
    """解析命令二进制文件绝对路径，并进行安全检查（CWE-59 / CWE-78）。

    安全策略（纵深防御）：
    1. name 必须是非空 ASCII 字符串，拒绝以 ``-`` 开头（防止被解析为选项）；
    2. name 不得包含任何路径分隔符或空字符（仅允许裸命令名，如 ``python3``）；
    3. 仅在固定可信目录中搜索，不依赖外部 PATH；
    4. 命中文件后必须不是符号链接，并用 realpath 归一，防止目录/文件被替换；
    5. 解析后的路径必须落在可信系统目录下。
    """
    # —— 1. 基本类型 / 空值 / 长度 / ASCII 防御 ——
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name or len(name) > 64:
        return None
    try:
        name.encode("ascii")
    except UnicodeEncodeError:
        return None
    # —— 2. 拒绝空字符、控制字符（shlex 已在上层解析校验，这里再做一次）——
    if "\x00" in name or "\n" in name or "\r" in name:
        return None
    # —— 3. 拒绝选项式开头 / 任何路径分隔符（只允许裸命令名）——
    if name.startswith("-"):
        return None
    if os.sep in name or "/" in name or "\\" in name:
        return None
    # —— 4. 仅在固定可信目录中搜索，并跟随/拒绝符号链接 ——
    # 策略：允许系统目录内的符号链接（如 python3 -> python3.10），但 realpath
    # 必须仍落在可信目录内；禁止链接指向可信目录外的文件（CWE-59）。
    for d in _TRUSTED_BIN_DIRS:
        candidate = os.path.join(d, name)
        try:
            real = os.path.realpath(candidate)
            # 真实目标必须在可信目录内
            if not any(real.startswith(td + os.sep) or real == td for td in _TRUSTED_BIN_DIRS):
                continue
            if os.path.isfile(real) and os.access(real, os.X_OK):
                return real
        except OSError:
            continue
    return None


# ---------- Python 代码沙箱辅助函数 ----------

def _apply_child_limits() -> None:
    """在子进程中设置资源限制（CPU / 内存 / 进程数 / 写文件大小）。"""
    try:
        import resource as _resource
    except Exception:
        return
    try:
        _resource.setrlimit(
            _resource.RLIMIT_CPU,
            (_MAX_PYTHON_CPU_SEC, _MAX_PYTHON_CPU_SEC),
        )
        _resource.setrlimit(
            _resource.RLIMIT_AS,
            (_MAX_PYTHON_MEM_BYTES, _MAX_PYTHON_MEM_BYTES),
        )
        try:
            _resource.setrlimit(_resource.RLIMIT_NPROC, (16, 16))
        except Exception:
            pass
        try:
            _resource.setrlimit(
                _resource.RLIMIT_FSIZE,
                (10 * 1024 * 1024, 10 * 1024 * 1024),
            )
        except Exception:
            pass
    except Exception as e:
        logger.debug("子进程资源限制失败: %s", e, exc_info=True)


class _PythonSandboxVisitor(_ast_mod.NodeVisitor):
    """Python 代码 AST 白名单校验器（CWE-94 兜底）。"""

    def visit_Import(self, node):
        # C-002：彻底禁止 import 语句，防止通过 importlib / types / _imp 等逃逸。
        raise ValueError("import statements not allowed")

    def visit_ImportFrom(self, node):
        # C-002：彻底禁止 from ... import ... 形式。
        raise ValueError("from imports not allowed")

    def visit_Attribute(self, node):
        if not isinstance(node.attr, str):
            self.generic_visit(node)
            return
        # 严格禁止访问任何双下划线属性（包括 __class__/__bases__/__globals__/__subclasses__ 等逃逸入口）
        if node.attr.startswith("__") and node.attr.endswith("__"):
            raise ValueError(f"attribute '{node.attr}' not allowed")
        if node.attr.startswith("_"):
            raise ValueError(f"attribute '{node.attr}' not allowed")
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id in _PYTHON_FORBIDDEN_NAMES:
            raise ValueError(f"name '{node.id}' not allowed")
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, _ast_mod.Name) and node.func.id in _PYTHON_FORBIDDEN_NAMES:
            raise ValueError(f"call to '{node.func.id}' not allowed")
        if isinstance(node.func, _ast_mod.Attribute) and node.func.attr.startswith("_"):
            raise ValueError(f"call to private attribute '{node.func.attr}' not allowed")
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        raise ValueError("class definitions not allowed")

    def visit_FunctionDef(self, node):
        # C-002：禁止函数定义，防止通过默认参数、闭包、__globals__ 等绕过。
        raise ValueError("function definitions not allowed")

    def visit_AsyncFunctionDef(self, node):
        raise ValueError("async function definitions not allowed")

    def visit_Lambda(self, node):
        # C-002：禁止 lambda，防止构造逃逸闭包。
        raise ValueError("lambda expressions not allowed")

    def visit_With(self, node):
        raise ValueError("with statements not allowed")

    def visit_Try(self, node):
        raise ValueError("try statements not allowed")

    def visit_Raise(self, node):
        raise ValueError("raise statements not allowed")

    def visit_Global(self, node):
        raise ValueError("global statements not allowed")

    def visit_Nonlocal(self, node):
        raise ValueError("nonlocal statements not allowed")


def _validate_python_code(source: str) -> Tuple[bool, str]:
    """Python 代码静态检查：优先使用 RestrictedPython，否则用 AST 白名单兜底。

    返回 (ok, error_message)。失败时 ok=False，错误信息 human-readable。
    """
    # 1) RestrictedPython 策略检查（如果已安装）
    try:
        from RestrictedPython import compile_restricted
        compile_restricted(source, "<sandbox>", "exec")
    except ImportError:
        pass
    except Exception as e:
        return False, f"RestrictedPython policy check failed: {e}"

    # 2) AST 白名单兜底 / 强化
    try:
        tree = _ast_mod.parse(source)
    except SyntaxError as e:
        return False, f"Syntax error: {e}"
    visitor = _PythonSandboxVisitor()
    try:
        visitor.visit(tree)
    except ValueError as e:
        return False, str(e)
    return True, ""


def _generate_runner_script() -> str:
    """生成在独立子进程中执行的 Python 沙箱 runner 脚本（自包含，不依赖外部 PYTHONPATH）。

    加固（C-002）：
    - 尝试加载 seccomp 过滤器（Linux 下限制高危 syscall，失败则忽略）；
    - 再次使用 AST 白名单检查用户代码，并阻断 __loader__ / __spec__ 等敏感入口；
    - 彻底禁止 Import / ImportFrom / ClassDef / FunctionDef / Lambda；
    - __builtins__ 仅保留明确白名单，不暴露 __import__；
    - 白名单模块通过 _SafeModuleProxy 代理暴露，防止 module.sys / module.os 等残留属性逃逸。
    """
    return f"""# -*- coding: utf-8 -*-
import ast
import os
import re
import sys

# 尝试启用 seccomp 黑名单（仅当系统已安装 libseccomp 时生效）
try:
    import seccomp
except Exception:
    seccomp = None

if seccomp is not None:
    try:
        _filter = seccomp.SyscallFilter(seccomp.ALLOW)
        for _syscall in (
            "execve", "execveat", "fork", "vfork", "clone", "unshare",
            "pivot_root", "mount", "umount", "umount2", "chroot", "ptrace",
            "socket", "socketpair", "connect", "bind", "listen", "accept",
            "accept4", "sendto", "recvfrom", "sendmsg", "recvmsg",
            "open_by_handle_at", "perf_event_open", "bpf", "kexec_load",
            "create_module", "init_module", "delete_module", "iopl", "ioperm",
            "swapon", "swapoff", "reboot", "settimeofday", "adjtimex",
            "clock_settime", "sethostname", "setdomainname", "syslog",
            "process_vm_writev", "process_vm_readv", "keyctl", "add_key",
            "request_key", "fanotify_init", "fanotify_mark",
        ):
            try:
                _filter.add_rule(seccomp.KILL, _syscall)
            except Exception:
                pass
        _filter.load()
    except Exception:
        pass

_FORBIDDEN_NAMES = {repr(sorted(_PYTHON_FORBIDDEN_NAMES))}
_SAFE_BUILTINS_NAMES = {repr(sorted(_PYTHON_SAFE_BUILTINS))}
_SAFE_MODULE_ATTRS = {repr(_SAFE_MODULE_ATTRS)}
_FORBIDDEN_DUNDERS = frozenset({{"__loader__", "__spec__", "__builtins__", "__import__",
                                "__class__", "__base__", "__bases__", "__subclasses__",
                                "__globals__", "__code__", "__closure__"}})

# 严格禁止所有双下划线属性访问，彻底封死对象图逃逸通道
_DUNDER_RE = re.compile(r"^__[A-Za-z0-9_]+__$")

class _SafeModuleProxy:
    __slots__ = ("_module", "_attrs")

    def __init__(self, module, attrs):
        object.__setattr__(self, "_module", module)
        object.__setattr__(self, "_attrs", frozenset(attrs))

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError("attribute '{{}}' not allowed".format(name))
        allowed = object.__getattribute__(self, "_attrs")
        if name not in allowed:
            raise AttributeError("attribute '{{}}' not allowed".format(name))
        return getattr(object.__getattribute__(self, "_module"), name)

    def __dir__(self):
        return sorted(object.__getattribute__(self, "_attrs"))

    def __repr__(self):
        return "<safe module proxy>"

class _Visitor(ast.NodeVisitor):
    def visit_Import(self, node):
        # C-002：彻底禁止 import 语句。
        raise ValueError("import statements not allowed")

    def visit_ImportFrom(self, node):
        # C-002：彻底禁止 from ... import ... 形式。
        raise ValueError("from imports not allowed")

    def visit_Attribute(self, node):
        if not isinstance(node.attr, str):
            self.generic_visit(node)
            return
        if _DUNDER_RE.match(node.attr) or node.attr in _FORBIDDEN_DUNDERS:
            raise ValueError("attribute '{{}}' not allowed".format(node.attr))
        if node.attr.startswith("_"):
            raise ValueError("attribute '{{}}' not allowed".format(node.attr))
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id in _FORBIDDEN_NAMES or node.id in _FORBIDDEN_DUNDERS:
            raise ValueError("name '{{}}' not allowed".format(node.id))
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_NAMES:
            raise ValueError("call to '{{}}' not allowed".format(node.func.id))
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.attr, str) and (node.func.attr.startswith("_") or node.func.attr in _FORBIDDEN_DUNDERS):
            raise ValueError("call to private attribute '{{}}' not allowed".format(node.func.attr))
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        raise ValueError("class definitions not allowed")

    def visit_FunctionDef(self, node):
        raise ValueError("function definitions not allowed")

    def visit_AsyncFunctionDef(self, node):
        raise ValueError("async function definitions not allowed")

    def visit_Lambda(self, node):
        raise ValueError("lambda expressions not allowed")

    def visit_With(self, node):
        raise ValueError("with statements not allowed")

    def visit_Try(self, node):
        raise ValueError("try statements not allowed")

    def visit_Raise(self, node):
        raise ValueError("raise statements not allowed")

    def visit_Global(self, node):
        raise ValueError("global statements not allowed")

    def visit_Nonlocal(self, node):
        raise ValueError("nonlocal statements not allowed")

source_path = sys.argv[1]
with open(source_path, "r", encoding="utf-8") as _f:
    _source = _f.read()
_visitor = _Visitor()
_visitor.visit(ast.parse(_source))

_real_builtins = __builtins__ if isinstance(__builtins__, dict) else __builtins__.__dict__
# C-002：__builtins__ 仅保留明确白名单，不暴露 __import__。
_safe_builtins = {{name: _real_builtins[name] for name in _SAFE_BUILTINS_NAMES if name in _real_builtins}}

# C-002：_safe_globals 中不暴露 __loader__ / __spec__；模块通过代理暴露。
_safe_globals = {{"__builtins__": _safe_builtins}}
for _mod, _attrs in _SAFE_MODULE_ATTRS.items():
    try:
        _safe_globals[_mod] = _SafeModuleProxy(__import__(_mod), _attrs)
    except Exception:
        pass

# C-004：进一步清理 sys.modules 中残留的危险模块引用，降低通过对象图间接访问的风险
for _badmod in ("os", "sys", "subprocess", "socket", "importlib", "types", "_imp", "builtins"):
    sys.modules.pop(_badmod, None)

exec(compile(_source, "<sandbox>", "exec"), _safe_globals)
"""


class CommandTools:
    """命令执行工具集：run_command, run_command_stream, execute_custom, ask_user_questions"""

    def __init__(self, executor):
        self._exec = executor
        self._terminal_lock = threading.Lock()  # 保护 _persistent_terminal 懒加载

    def ask_user_questions(self, call):
        qs = call.get("questions") or []
        if not isinstance(qs, list) or len(qs) == 0:
            return False, "[ask_user_questions] 缺少 questions"
        # 限制单轮问题数量，防止 GUI 渲染负担
        if len(qs) > 16:
            return False, "[ask_user_questions] 一次最多 16 个问题"
        cleaned = []
        for q in qs:
            if not isinstance(q, dict):
                return False, "[ask_user_questions] 每个问题必须是对象"
            qid = str(q.get("id", "") or ("q" + str(len(cleaned) + 1)))
            question = str(q.get("question", "")).strip()
            options = q.get("options") or []
            qtype = str(q.get("type", "single")).strip().lower()
            if qtype not in ("single", "multi"):
                qtype = "single"
            if not question or not isinstance(options, list) or len(options) == 0:
                return False, "[ask_user_questions] 缺少 question/options"
            if len(options) > 32:
                return False, "[ask_user_questions] 每个问题最多 32 个选项"
            cleaned.append({
                "id": qid[:64],
                "question": question[:1024],
                "options": [str(o)[:256] for o in options],
                "type": qtype,
            })
        payload = "__ASK_USER_QUESTIONS__" + json.dumps({"questions": cleaned}, ensure_ascii=False)
        return True, payload

    # -------- 核心运行函数 --------
    def run_command(self, call):
        """简单版：同步执行命令并返回前 4000 字（保持向后兼容）。"""
        if not isinstance(call, dict):
            return False, "[run_command] 参数格式错误，需要 JSON 对象"
        cmd = call.get("command", "")
        if not isinstance(cmd, str) or not cmd.strip():
            return False, "[run_command] 缺少 command"
        cmd = cmd.strip()
        # 子代理禁用
        if self._exec._subagent_restrictions.get("forbid_commands", False):
            return False, "[run_command] 子代理不允许执行 shell 命令"
        # 危险模式预扫描
        hit = _check_dangerous_patterns(cmd)
        if hit:
            return False, f"[run_command] 命中危险模式 ({hit}): " + cmd[:200]
        # 拆分 + 白名单
        parts = _safe_split_command(cmd)
        if not parts:
            return False, "[run_command] 命令解析失败（可能含非法字符、空字符或超长）"
        # 权限模式检查（reviewer 仅允许只读查询）
        perm_err = _check_command_permission(parts, "run_command")
        if perm_err:
            return perm_err
        if parts[0] in FORBIDDEN_COMMANDS:
            return False, "[run_command] 命令被禁止执行: " + parts[0]
        if parts[0] in HIGH_RISK_COMMANDS:
            return False, "[run_command] 高危包管理器命令，已被移出白名单: " + parts[0]
        if parts[0] not in ALLOWED_COMMANDS:
            return False, "[run_command] 命令不在白名单中: " + parts[0]
        # 解析二进制路径（防止 PATH 劫持：攻击者不能在工作目录写入同名二进制）
        safe_bin = _resolve_safe_binary(parts[0])
        if not safe_bin:
            return False, "[run_command] 无法安全解析命令路径 (疑似 PATH 劫持): " + parts[0]
        # 禁止 wget/curl 使用 -O/-o 等文件输出标志，防止沙箱逃逸
        bad_flag = _check_wget_curl_output_flags(parts)
        if bad_flag:
            return False, "[run_command] wget/curl 不允许使用文件输出标志: " + bad_flag
        # P0-2 修复: 禁止解释器内联代码参数（-c/-e），防止绕过 execute_custom 沙箱
        bad_inline = _check_interpreter_inline_code(parts)
        if bad_inline:
            return False, "[run_command] " + bad_inline
        # P0-1 修复: 禁止 tar -C / find -exec 等危险选项
        bad_flag = _check_dangerous_cmd_flags(parts)
        if bad_flag:
            return False, "[run_command] " + bad_flag
        parts = [safe_bin] + list(parts[1:])

        # 用户确认门（Level-2 操作，必须由外部回调显式批准；缺失/异常默认拒绝）
        confirm_fn = getattr(self._exec, "on_confirm_command", None)
        if not callable(confirm_fn):
            return False, "[run_command] 缺少命令审批回调，已拒绝执行: " + cmd[:200]
        try:
            confirm_result = confirm_fn(call)
            if not bool(confirm_result):
                return False, "[run_command] 用户拒绝执行: " + cmd[:200]
        except ConfirmationError:
            logger.info("[run_command] 命令确认回调异常，已拒绝执行")
            logger.debug("[run_command] 命令确认回调异常详情", exc_info=True)
            return False, "[run_command] 确认系统异常，已拒绝执行"
        except Exception as e:
            logger.info("[run_command] 命令确认回调出现未知异常，已拒绝执行")
            logger.debug("[run_command] 命令确认回调异常详情: %s", e, exc_info=True)
            return False, "[run_command] 确认系统异常，已拒绝执行"

        try:
            timeout = int(call.get("timeout") or 0)
            if timeout <= 0 or timeout > 1800:
                timeout = DEFAULT_COMMAND_TIMEOUT
        except (TypeError, ValueError):
            timeout = DEFAULT_COMMAND_TIMEOUT

        # M-018: 审计日志（危险命令执行）
        logger.info("[AUDIT] run_command: %s", _sanitize_log_text(cmd[:200]))

        try:
            cwd = str(getattr(getattr(self._exec, "sandbox", None), "work_dir", "."))
            # P0-1 修复: sandbox 不可用时拒绝执行，避免在非预期目录运行命令
            if cwd in (None, "", "."):
                return False, "[run_command] 无法确定项目工作目录，已拒绝执行"
            # P0-1 修复: 校验所有绝对路径参数必须在 work_dir 内
            bad_path = _check_absolute_path_args(parts, cwd)
            if bad_path:
                return False, "[run_command] " + bad_path
            # 限制输出缓冲区：读满后截断，防止子进程输出 OOM；使用 Popen + start_new_session
            # 确保超时时可以 kill 整个进程组（CWE-697）
            env = _sanitize_command_env()
            if env is None:
                return False, "[run_command] 环境变量校验失败（存在非法注入变量）"
            # 把解析到的二进制所在目录加入 PATH，便于 npx 等工具找到同目录的 node
            bin_dir = os.path.dirname(safe_bin)
            if bin_dir and bin_dir not in env["PATH"].split(os.pathsep):
                env["PATH"] = bin_dir + os.pathsep + env["PATH"]
            # C-008: 占用全局子进程槽位，防止并发/总数/速率超限和 fork bomb
            popen_kwargs = {"preexec_fn": _preexec_child_safety, "start_new_session": True} if os.name == "posix" else {}
            with subprocess_slot():
                proc = subprocess.Popen(
                    parts, shell=False, cwd=cwd,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=env,
                    **popen_kwargs,
                )
                try:
                    stdout_b, stderr_b = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    _safe_terminate(proc, force_kill=True)
                    return False, "[run_command] 超时 (" + str(timeout) + "s)"
                out = (stdout_b or b"").decode("utf-8", errors="replace") + (stderr_b or b"").decode("utf-8", errors="replace")
                return True, "[run_command] $ " + cmd + "\n" + out[:_MAX_COMMAND_OUTPUT_DISPLAY] + "\n[exit: " + str(proc.returncode) + "]"
        except FileNotFoundError:
            return False, "[run_command] 未找到命令: " + parts[0]
        except PermissionError:
            return False, "[run_command] 权限不足: " + parts[0]
        except RuntimeError as e:
            return False, "[run_command] 子进程限制触发: " + str(e)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            return False, "[run_command] " + str(e)

    def run_command_stream(self, call):
        cmd = call.get("command", "").strip() if isinstance(call, dict) else ""
        if not cmd:
            return False, "[run_command_stream] 缺少 command"
        if self._exec._subagent_restrictions.get("forbid_commands", False):
            return False, "[run_command_stream] 已禁用 (子代理不可执行 shell 命令)"
        hit = _check_dangerous_patterns(cmd)
        if hit:
            return False, f"[run_command_stream] 命中危险模式 ({hit}): " + cmd[:200]

        parts = _safe_split_command(cmd)
        if not parts:
            return False, "[run_command_stream] 命令解析失败（可能含非法字符、空字符或超长）"
        # 权限模式检查（reviewer 仅允许只读查询）
        perm_err = _check_command_permission(parts, "run_command_stream")
        if perm_err:
            return perm_err
        if parts[0] in FORBIDDEN_COMMANDS:
            return False, "[run_command_stream] 命令被禁止执行: " + parts[0]
        if parts[0] in HIGH_RISK_COMMANDS:
            return False, "[run_command_stream] 高危包管理器命令，已被移出白名单: " + parts[0]
        if parts[0] not in ALLOWED_COMMANDS:
            return False, "[run_command_stream] 命令不在白名单中: " + parts[0]
        # 解析二进制路径（防止 PATH 劫持）
        safe_bin = _resolve_safe_binary(parts[0])
        if not safe_bin:
            return False, "[run_command_stream] 无法安全解析命令路径 (疑似 PATH 劫持): " + parts[0]
        # 禁止 wget/curl 使用 -O/-o 等文件输出标志，防止沙箱逃逸
        bad_flag = _check_wget_curl_output_flags(parts)
        if bad_flag:
            return False, "[run_command_stream] wget/curl 不允许使用文件输出标志: " + bad_flag
        # P0-2 修复: 禁止解释器内联代码参数
        bad_inline = _check_interpreter_inline_code(parts)
        if bad_inline:
            return False, "[run_command_stream] " + bad_inline
        # P0-1 修复: 禁止 tar -C / find -exec 等危险选项
        bad_flag = _check_dangerous_cmd_flags(parts)
        if bad_flag:
            return False, "[run_command_stream] " + bad_flag
        parts = [safe_bin] + list(parts[1:])

        confirm_fn = getattr(self._exec, "on_confirm_command", None)
        if not callable(confirm_fn):
            return False, "[run_command_stream] 缺少命令审批回调，已拒绝执行: " + cmd[:200]
        try:
            confirm_result = confirm_fn(call)
            if not bool(confirm_result):
                return False, "[run_command_stream] 用户拒绝执行: " + cmd[:200]
        except ConfirmationError:
            logger.info("[run_command_stream] 命令确认回调异常，已拒绝执行")
            logger.debug("[run_command_stream] 命令确认回调异常详情", exc_info=True)
            return False, "[run_command_stream] 确认系统异常，已拒绝执行"
        except Exception as e:
            logger.info("[run_command_stream] 命令确认回调出现未知异常，已拒绝执行")
            logger.debug("[run_command_stream] 命令确认回调异常详情: %s", e, exc_info=True)
            return False, "[run_command_stream] 确认系统异常，已拒绝执行"

        try:
            timeout = int(call.get("timeout") or 0)
            if timeout <= 0 or timeout > 1800:
                timeout = DEFAULT_COMMAND_TIMEOUT
        except (TypeError, ValueError):
            timeout = DEFAULT_COMMAND_TIMEOUT

        # M-018: 审计日志（危险命令执行）
        logger.info("[AUDIT] run_command_stream: %s", _sanitize_log_text(cmd[:200]))

        cwd = str(getattr(getattr(self._exec, "sandbox", None), "work_dir", "."))
        # P0-1 修复: sandbox 不可用时拒绝执行
        if cwd in (None, "", "."):
            return False, "[run_command_stream] 无法确定项目工作目录，已拒绝执行"
        bad_path = _check_absolute_path_args(parts, cwd)
        if bad_path:
            return False, "[run_command_stream] " + bad_path
        env = _sanitize_command_env()
        if env is None:
            return False, "[run_command_stream] 环境变量校验失败（存在非法注入变量）"
        # 把解析到的二进制所在目录加入 PATH，便于 npx 等工具找到同目录的 node
        bin_dir = os.path.dirname(safe_bin)
        if bin_dir and bin_dir not in env["PATH"].split(os.pathsep):
            env["PATH"] = bin_dir + os.pathsep + env["PATH"]

        collected_lines = []
        cancelled = [False]

        proc: Optional[subprocess.Popen] = None
        _slot_acquired = False
        # C-008: 占用全局子进程槽位，防止并发/总数/速率超限和 fork bomb
        popen_kwargs = {"preexec_fn": _preexec_child_safety, "start_new_session": True} if os.name == "posix" else {}
        try:
            acquire_subprocess_slot()
            _slot_acquired = True
            proc = subprocess.Popen(
                parts, shell=False, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", env=env, bufsize=1,
                **popen_kwargs,
            )
        except FileNotFoundError:
            if _slot_acquired:
                release_subprocess_slot()
                _slot_acquired = False
            return False, "[run_command_stream] 未找到命令: " + parts[0]
        except PermissionError:
            if _slot_acquired:
                release_subprocess_slot()
                _slot_acquired = False
            return False, "[run_command_stream] 权限不足: " + parts[0]
        except RuntimeError as e:
            if _slot_acquired:
                release_subprocess_slot()
                _slot_acquired = False
            return False, "[run_command_stream] 子进程限制触发: " + str(e)
        except Exception as e:
            if _slot_acquired:
                release_subprocess_slot()
                _slot_acquired = False
            return False, "[run_command_stream] 启动失败: " + str(e)

        import time as _time
        start_time = _time.time()
        # 收集到的总行数上限（防止无限输出 OOM）
        _MAX_LINES = 4096
        _CHARS_PER_LINE = 512
        returncode_val: Optional[int] = None

        def _check_cancel_or_timeout() -> Optional[str]:
            """检查取消标志或超时；返回错误消息字符串或 None（正常继续）。"""
            on_cancel = getattr(self._exec, "on_cancel_check", None)
            if on_cancel:
                try:
                    if bool(on_cancel()):
                        cancelled[0] = True
                        _safe_terminate(proc)
                        return "__cancelled__"
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            if _time.time() - start_time > timeout:
                _safe_terminate(proc)
                return "[run_command_stream] 超时 (" + str(timeout) + "s)"
            return None

        # 使用独立读线程 + Queue，避免 readline() 在 Windows/无换行输出时阻塞
        out_queue: "queue.Queue[str]" = queue.Queue()
        reader_done = threading.Event()

        def _reader():
            try:
                if proc is None or proc.stdout is None:
                    return
                for line in proc.stdout:
                    if line is None:
                        break
                    out_queue.put(line)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            finally:
                reader_done.set()

        reader_thread = threading.Thread(target=_reader, daemon=True, name=f"CmdReader-{id(proc)}")
        reader_thread.start()

        try:
            while True:
                # 优先从队列取输出，超时后再检查取消/超时与进程状态
                try:
                    line = out_queue.get(timeout=0.25)
                except queue.Empty:
                    line = None

                if line is not None:
                    stripped = line.rstrip("\n")[:_CHARS_PER_LINE]
                    collected_lines.append(stripped)
                    if len(collected_lines) > _MAX_LINES:
                        # 滚动截断：保留最后 N 行
                        collected_lines = collected_lines[-_MAX_LINES:]
                    on_output = getattr(self._exec, "on_output_line", None)
                    if on_output:
                        try:
                            on_output(stripped)
                        except Exception as _exc:
                            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                else:
                    # 无输出时检查进程是否结束
                    if proc is not None and proc.poll() is not None and reader_done.is_set():
                        break
                    err = _check_cancel_or_timeout()
                    if err:
                        if err == "__cancelled__":
                            return False, "[run_command_stream] 已取消"
                        return False, err
                    continue

                err = _check_cancel_or_timeout()
                if err:
                    if err == "__cancelled__":
                        return False, "[run_command_stream] 已取消"
                    return False, err

            # 等待读线程收尾，最多 2 秒
            reader_done.wait(timeout=2.0)

            # 收集剩余输出
            while not out_queue.empty():
                try:
                    line = out_queue.get_nowait()
                    stripped = line.rstrip("\n")[:_CHARS_PER_LINE]
                    collected_lines.append(stripped)
                    if len(collected_lines) > _MAX_LINES:
                        collected_lines = collected_lines[-_MAX_LINES:]
                    on_output2 = getattr(self._exec, "on_output_line", None)
                    if on_output2:
                        try:
                            on_output2(stripped)
                        except Exception as e:
                            logger.info("[run_command_stream] 命令输出回调异常")
                            logger.debug("[run_command_stream] 命令输出回调异常: %s", e, exc_info=True)
                except queue.Empty:
                    break

            returncode_val = proc.returncode if proc is not None else 0
        except Exception as e:
            _safe_terminate(proc)
            return False, "[run_command_stream] 异常: " + str(e)
        finally:
            # 最后兜底：无论哪个路径退出，都确保进程被 kill
            _safe_terminate(proc, force_kill=True)
            reader_done.set()
            if reader_thread.is_alive():
                try:
                    reader_thread.join(timeout=1.0)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            # C-008: 释放全局子进程槽位
            if _slot_acquired:
                release_subprocess_slot()
                _slot_acquired = False

        if cancelled[0]:
            summary_tail = "\n[已被用户取消]"
        else:
            summary_tail = "\n[exit: " + str(returncode_val) + "]"
        out_text = "\n".join(collected_lines[-800:]) if len(collected_lines) > 800 else "\n".join(collected_lines)
        # H-3 修复: 截断时添加提示
        trunc_note = ""
        if len(out_text) > 8000:
            out_text = out_text[:8000]
            trunc_note = "\n[输出已截断，仅显示前 8000 字符]"
        return True, "[run_command_stream] $ " + cmd + "\n" + out_text + trunc_note + summary_tail

    def execute_custom(self, call):
        cmd_type = (call.get("type") or "shell").lower().strip()
        # reviewer 模式下非 shell 的 execute_custom（python/mcp）不是只读查询，直接拒绝
        try:
            if Config().permission_mode == "reviewer" and cmd_type != "shell":
                return False, "[execute_custom] reviewer 模式仅允许 shell 只读查询"
        except Exception:
            return False, "[execute_custom] 无法验证权限模式，拒绝执行"
        if cmd_type == "shell":
            command = (call.get("command", "") or "").strip()
            if not command:
                return False, "[execute_custom] shell 缺少 command"
            # 复用 run_command 的完整安全链（白名单 + 危险模式 + 确认门 + 权限检查）
            return self.run_command({"tool": "run_command", "command": command})
        elif cmd_type == "python":
            # 子代理已禁用命令执行 — 直接拒绝
            if self._exec._subagent_restrictions.get("forbid_commands", False):
                return False, "[execute_custom] 已禁用 (子代理不可执行 python 代码)"
            code = call.get("code", "")
            if not isinstance(code, str) or not code:
                return False, "[execute_custom] python 缺少 code"
            # 代码长度上限：避免超长脚本注入（CWE-94）
            if len(code) > _MAX_PYTHON_CODE_BYTES:
                return False, f"[execute_custom] python 代码超过 {_MAX_PYTHON_CODE_BYTES} 字节上限"
            # 禁止空字符嵌入
            if "\x00" in code:
                return False, "[execute_custom] python 代码包含非法字符"

            # AST 静态检查 + RestrictedPython compile_restricted（如果已安装），否则 AST 白名单兜底
            ok, err = _validate_python_code(code)
            if not ok:
                return False, f"[execute_custom] python 代码安全检查失败: {err}"

            # 用户确认门（Level-2 操作，缺失/异常默认拒绝）
            confirm_fn = getattr(self._exec, "on_confirm_command", None)
            if not callable(confirm_fn):
                return False, "[execute_custom] 缺少 python 审批回调，已拒绝执行"
            try:
                confirm_call = dict(call)
                confirm_call["command"] = "python: " + code[:200]
                confirmed = bool(confirm_fn(confirm_call))
                if not confirmed:
                    return False, "[execute_custom] 用户拒绝执行 python 代码"
            except Exception:
                logger.info("[execute_custom] python 确认回调异常，已拒绝执行")
                logger.debug("[execute_custom] python 确认回调异常详情", exc_info=True)
                return False, "[execute_custom] 确认失败，操作已取消"

            try:
                timeout = int(call.get("timeout") or _MAX_PYTHON_PROC_TIMEOUT)
                if timeout <= 0 or timeout > 300:
                    timeout = _MAX_PYTHON_PROC_TIMEOUT
            except (TypeError, ValueError):
                timeout = _MAX_PYTHON_PROC_TIMEOUT

            try:
                work_dir = str(getattr(getattr(self._exec, "sandbox", None), "work_dir", "."))
                # 审计日志：记录即将执行的 Python 代码前 200 字符
                # H-006: 使用 %s 占位符并对用户可控数据做控制字符清理
                logger.info(
                    "[AUDIT] execute_custom python code (len=%d): %s",
                    len(code), _sanitize_log_text(code[:200])
                )
                # 临时文件创建在系统临时目录，避免出现在项目文件列表中（B-12）
                script_fd, script_path = tempfile.mkstemp(suffix=".py")
                runner_fd, runner_path = tempfile.mkstemp(suffix=".py")
                try:
                    with os.fdopen(script_fd, "w", encoding="utf-8") as f:
                        f.write(code)
                    with os.fdopen(runner_fd, "w", encoding="utf-8") as f:
                        f.write(_generate_runner_script())
                    # 使用 _resolve_safe_binary 防止 PATH 劫持
                    python_bin = _resolve_safe_binary("python3")
                    if python_bin is None:
                        return False, "[execute_custom] 无法安全解析 python3 路径"
                    # 子进程环境变量白名单：剥离敏感 token/密钥，防止用户脚本泄露（CWE-95）
                    sanitized_env = _sanitize_command_env()
                    if sanitized_env is None:
                        return False, "[execute_custom] 环境变量校验失败（存在非法注入变量）"
                    sanitized_env.update({
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONNOUSERSITE": "1",
                    })
                    # 在独立子进程中执行，并通过 preexec_fn 设置 CPU/内存/进程数限制（CWE-94 / CWE-697）
                    # C-008: 同时占用全局子进程槽位，防止 fork bomb
                    popen_kwargs = {"preexec_fn": _apply_child_limits, "start_new_session": True} if os.name == "posix" else {}
                    with subprocess_slot():
                        proc = subprocess.Popen(
                            # -S 跳过 site 自定义，-I 开启隔离模式，防止 PYTHONPATH/env 注入
                            [python_bin, "-S", "-I", runner_path, script_path],
                            shell=False, cwd=work_dir,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=sanitized_env,
                            **popen_kwargs,
                        )
                        try:
                            stdout_b, stderr_b = proc.communicate(timeout=timeout)
                        except subprocess.TimeoutExpired:
                            _safe_terminate(proc, force_kill=True)
                            return False, f"[execute_custom] python 执行超时 ({timeout}s)"

                        out = (stdout_b or b"").decode("utf-8", errors="replace") + "\n" + (stderr_b or b"").decode("utf-8", errors="replace")
                        ok = proc.returncode == 0
                        # H-3 修复: 截断时添加提示
                        trunc_note = ""
                        if len(out) > 6000:
                            out = out[:6000]
                            trunc_note = "\n[输出已截断，仅显示前 6000 字符]"
                        msg = "[execute_custom] python exit=" + str(proc.returncode) + "\n" + out + trunc_note
                        return ok, msg
                finally:
                    for _tmp in (script_path, runner_path):
                        try:
                            os.remove(_tmp)
                        except Exception as e:
                            logger.info("[execute_custom] 临时文件删除异常")
                            logger.debug("[execute_custom] 临时文件删除异常: %s", e, exc_info=True)
            except Exception as e:
                return False, "[execute_custom] python 失败: " + str(e)
        elif cmd_type == "mcp":
            # 子代理禁用命令类执行，防止通过 MCP 服务器间接执行 shell/文件操作
            if self._exec._subagent_restrictions.get("forbid_commands", False):
                return False, "[execute_custom] 已禁用 (子代理不可通过 MCP 执行命令)"
            # 委托到 ToolExecutor.execute 以走完整路径（参数名：execute_custom 里写的是
            # "tool" 字段，但 mcp_call 期望 "tool_name"——这里显式转换）。
            mcp_call_payload = {
                "tool": "mcp_call",
                "server_name": str(call.get("server_name", "") or ""),
                "tool_name": str(call.get("tool", "") or ""),
                "args": call.get("args", {}) if isinstance(call.get("args"), dict) else {},
            }
            return self._exec.execute(mcp_call_payload)
        else:
            return False, "[execute_custom] 未知 type: " + cmd_type

    def calculator(self, call):
        """安全计算器：支持基本算术、科学函数、括号运算。

        安全策略（纵深层防御, P1-001 已审查）：
        1) 表达式长度上限 (400 字符)
        2) 字符合法性白名单（仅数字、运算符、逗号、点、字母、空格）
        3) AST 解析；节点深度上限 (24层)；节点类型白名单 (7种)；Name 白名单；Call 名白名单
        4) eval 使用受限 globals — __builtins__=空, 不暴露外部模块
        5) compile / eval 均被 AST 过滤再执行
        6) AST 属性链攻击已阻断: Attribute 节点不在 ALLOWED_NODE_TYPES 中,
           __class__/__bases__/__subclasses__ 等逃逸路径均不可达
        """
        import math, ast, re
        if not isinstance(call, dict):
            return False, "[calculator] 参数格式错误，需要 JSON 对象"

        expr = str(call.get("expression", "")).strip()
        if not expr:
            return False, "[calculator] 缺少 expression"
        if len(expr) > _MAX_CALC_EXPR_LENGTH:
            return False, f"[calculator] 表达式过长 (>{_MAX_CALC_EXPR_LENGTH})"
        if "\x00" in expr:
            return False, "[calculator] 表达式含非法字符"

        # 白名单字符检查（只允许数字、运算符、括号、逗号、点、字母函数名、空格）
        if not re.match(r"^[0-9+\-*/()., \s\w]+$", expr, re.UNICODE):
            return False, f"[calculator] 表达式含非法字符: {expr[:80]}"

        # 白名单常量 / 函数 / 名称
        SAFE_NAMES = {
            'pi': math.pi, 'e': math.e, 'tau': math.tau,
            'inf': float('inf'), 'nan': float('nan'),
            'sqrt': math.sqrt, 'sin': math.sin, 'cos': math.cos, 'tan': math.tan,
            'asin': math.asin, 'acos': math.acos, 'atan': math.atan,
            'atan2': math.atan2, 'sinh': math.sinh, 'cosh': math.cosh, 'tanh': math.tanh,
            'asinh': math.asinh, 'acosh': math.acosh, 'atanh': math.atanh,
            'exp': math.exp, 'log': math.log, 'log10': math.log10, 'log2': math.log2,
            'pow': math.pow, 'ceil': math.ceil, 'floor': math.floor,
            'fabs': math.fabs, 'factorial': math.factorial, 'gcd': math.gcd,
            'degrees': math.degrees, 'radians': math.radians,
            'abs': abs, 'min': min, 'max': max, 'round': round,
            'True': True, 'False': False, 'None': None,
        }
        SAFE_FUNC_NAMES = {
            'abs', 'min', 'max', 'round',
            'sin', 'cos', 'tan', 'asin', 'acos', 'atan', 'atan2',
            'sinh', 'cosh', 'tanh', 'asinh', 'acosh', 'atanh',
            'exp', 'log', 'log10', 'log2', 'sqrt', 'pow',
            'ceil', 'floor', 'fabs', 'factorial', 'gcd', 'degrees', 'radians',
        }

        try:
            tree = ast.parse(expr, mode='eval')
        except SyntaxError as e:
            return False, f"[calculator] 语法错误: {e}"

        # ---------- 节点深度检查（避免深度递归栈溢出 / 性能耗竭） ----------
        def _node_depth(node):
            if not hasattr(node, 'children') and not hasattr(node, '_fields'):
                return 1
            child_depths = []
            for field_name in node._fields:
                value = getattr(node, field_name, None)
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, ast.AST):
                            child_depths.append(_node_depth(item))
                elif isinstance(value, ast.AST):
                    child_depths.append(_node_depth(value))
            return 1 + (max(child_depths) if child_depths else 0)

        if _node_depth(tree) > _MAX_CALC_AST_DEPTH:
            return False, "[calculator] 表达式嵌套过深，已拒绝执行"

        # ---------- 节点类型白名单验证 ----------
        ALLOWED_NODE_TYPES = (
            ast.Expression, ast.Constant, _AST_NUM, _AST_STR,
            ast.BinOp, ast.UnaryOp, ast.Call, ast.Name, ast.Load,
        )
        ALLOWED_OPS = (
            ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
            ast.USub, ast.UAdd, ast.FloorDiv,
        )

        def _check_node(node, depth=0):
            if depth > _MAX_CALC_AST_DEPTH:
                raise ValueError("表达式嵌套过深")
            if not isinstance(node, ALLOWED_NODE_TYPES):
                raise ValueError(f"非法节点类型: {type(node).__name__}")
            if isinstance(node, ast.BinOp):
                if not isinstance(node.op, ALLOWED_OPS):
                    raise ValueError(f"非法运算符: {type(node.op).__name__}")
                _check_node(node.left, depth + 1)
                _check_node(node.right, depth + 1)
                return
            if isinstance(node, ast.UnaryOp):
                if not isinstance(node.op, ALLOWED_OPS):
                    raise ValueError(f"非法一元运算符: {type(node.op).__name__}")
                _check_node(node.operand, depth + 1)
                return
            if isinstance(node, ast.Call):
                # 函数调用必须是简单 Name 且在 SAFE_FUNC_NAMES 中（不允许链式调用）
                if not isinstance(node.func, ast.Name) or node.func.id not in SAFE_FUNC_NAMES:
                    raise ValueError(f"非法函数调用: {ast.dump(node.func)}")
                # 禁止 **kwargs（潜在危险调用）
                if node.keywords and any(kw.arg is None for kw in node.keywords):
                    raise ValueError("不允许 **kwargs 形式")
                for arg in node.args:
                    _check_node(arg, depth + 1)
                for kw in node.keywords:
                    _check_node(kw.value, depth + 1)
                return
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id not in SAFE_NAMES:
                    raise ValueError(f"未定义变量: {node.id}")
                return
            if isinstance(node, ast.Expression):
                _check_node(node.body, depth + 1)
                return
            # 常量节点：直接通过
            if isinstance(node, (ast.Constant, _AST_NUM, _AST_STR)):
                return
            # 其他子节点递归
            for field_name in node._fields:
                value = getattr(node, field_name, None)
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, ast.AST):
                            _check_node(item, depth + 1)
                elif isinstance(value, ast.AST):
                    _check_node(value, depth + 1)

        try:
            _check_node(tree)
        except ValueError as e:
            return False, f"[calculator] 安全限制: {e}"

        # 执行计算
        try:
            compiled = compile(tree, '<calculator>', 'eval')
            # 双锁：__builtins__ 置空，只提供 SAFE_NAMES 作为局部
            result = eval(compiled, {"__builtins__": {}}, SAFE_NAMES)  # nosec B307: AST 白名单沙箱，仅允许数学运算
            # 格式化输出：整数直接显示，浮点数最多 6 位有效数字
            if isinstance(result, float):
                if result.is_integer():
                    result_str = str(int(result))
                else:
                    result_str = f"{result:.6g}"
            else:
                result_str = str(result)
            return True, f"{expr} = {result_str}"
        except ZeroDivisionError:
            return False, "[calculator] 错误: 除零"
        except OverflowError:
            return False, "[calculator] 错误: 数值溢出"
        except Exception as e:
            return False, f"[calculator] 计算失败: {e}"

    # -------- 持久终端会话 --------
    def _get_terminal(self):
        """获取或创建持久终端（懒加载，带锁防止并发创建多个进程）"""
        with self._terminal_lock:
            term = getattr(self._exec, "_persistent_terminal", None)
            if term is not None and term.is_running():
                return term
            # 清理已退出的旧终端
            if term is not None:
                try:
                    term.terminate()
                except Exception:
                    pass
                self._exec._persistent_terminal = None
            if self._exec.sandbox is None:
                return None
            from src.tools.terminal_manager import TerminalManager
            cwd = str(self._exec.sandbox.work_dir)
            term = TerminalManager(cwd=cwd)
            if term.start():
                self._exec._persistent_terminal = term
                return term
            # 启动失败也要清理
            try:
                term.terminate()
            except Exception:
                pass
            return None

    def shell_start(self, call) -> Tuple[bool, str]:
        """启动或重启持久shell会话"""
        term = self._get_terminal()
        if term is None:
            return False, "[shell_start] 无法启动shell（请检查bash/sh是否可用）"
        return True, f"[shell_start] 持久shell已启动 (cwd={term.cwd})"

    def shell_exec(self, call) -> Tuple[bool, str]:
        """在持久shell中执行命令并等待结果
        参数:
            command: 要执行的命令
            timeout: 等待超时秒数（默认10）
        """
        cmd = call.get("command", "")
        raw_timeout = call.get("timeout", 10)
        if not isinstance(cmd, str) or not cmd.strip():
            return False, "[shell_exec] 缺少 command"
        if self._exec._subagent_restrictions.get("forbid_commands", False):
            return False, "[shell_exec] 子代理不允许执行 shell 命令"

        # 危险模式预扫描（防止shell元字符绕过）
        hit = _check_dangerous_patterns(cmd)
        if hit:
            return False, f"[shell_exec] 命中危险模式 ({hit}): " + cmd[:200]

        # 解析命令做基本白名单校验（取第一个;或&&或||之前的命令）
        import re as _re
        import os as _os
        first_cmd = _re.split(r'[;&|]', cmd, maxsplit=1)[0].strip()
        if first_cmd:
            parts = _safe_split_command(first_cmd)
            if parts:
                # 规范化路径：提取基名防止 /bin/bash 绕过
                cmd_base = _os.path.basename(parts[0])
                if cmd_base in FORBIDDEN_COMMANDS or parts[0] in FORBIDDEN_COMMANDS:
                    return False, "[shell_exec] 命令被禁止执行: " + parts[0]

                # 解释器内联代码检查（需要传入 parts 列表而非原始字符串）
                interp_hit = _check_interpreter_inline_code(parts)
                if interp_hit:
                    return False, f"[shell_exec] 命中解释器内联代码模式 ({interp_hit}): " + cmd[:200]

                # 危险命令标志检查（需要传入 parts 列表）
                flags_hit = _check_dangerous_cmd_flags(parts)
                if flags_hit:
                    return False, f"[shell_exec] 命中危险命令标志 ({flags_hit}): " + cmd[:200]

        # P0-1 修复: 校验所有绝对路径参数必须在沙箱 work_dir 内（覆盖整条命令，包括 ;/&&/|| 链式部分）
        if self._exec.sandbox is None:
            return False, "[shell_exec] 未加载项目"
        cwd = str(self._exec.sandbox.work_dir)
        if cwd in (None, "", "."):
            return False, "[shell_exec] 无法确定项目工作目录，已拒绝执行"
        try:
            full_parts = shlex.split(cmd)
        except ValueError:
            full_parts = None
        if full_parts:
            bad_path = _check_absolute_path_args(full_parts, cwd)
            if bad_path:
                return False, "[shell_exec] " + bad_path

        term = self._get_terminal()
        if term is None:
            return False, "[shell_exec] 持久shell未启动（先调用shell_start）"

        # timeout类型安全
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            timeout = 10.0
        timeout = min(max(timeout, 1), 60)

        # 清空缓冲区
        term.read_output(clear=True)

        # 使用独立行+heredoc-like marker避免cmd中引号导致marker被吞
        import time, random
        marker = f"__DONE_{int(time.time()*1000)}_{random.randint(1000,9999)}__"
        # 用换行而非;分隔，并用printf输出marker（echo在某些shell有转义问题）
        safe_cmd = cmd.rstrip("\n")
        term.write(safe_cmd + "\n")
        term.write(f'printf "====={marker}===== %s\\n" $?\n')

        # 读取输出直到看到标记或超时
        deadline = time.time() + timeout
        output_lines: List[str] = []
        exit_code = -1
        while time.time() < deadline:
            time.sleep(0.1)
            out = term.read_output(clear=True)
            if out:
                output_lines.append(out)
            combined = "".join(output_lines)
            if marker in combined:
                m = _re.search(re.escape(f"====={marker}=====") + r"[ \t]+(\d+)", combined)
                if m:
                    exit_code = int(m.group(1))
                idx = combined.find(f"====={marker}=====")
                if idx >= 0:
                    combined = combined[:idx].rstrip() + "\n"
                output_lines = [combined]
                break

        result = "".join(output_lines)
        if len(result) > 8000:
            result = result[:8000] + "\n... (输出已截断)"

        status = "✓" if exit_code == 0 else f"✗ (exit={exit_code})"
        return True, f"[shell_exec] {status}\n{result}"

    def shell_send(self, call) -> Tuple[bool, str]:
        """向持久shell发送命令（不等待完成）"""
        cmd = call.get("command", "")
        if not isinstance(cmd, str) or not cmd.strip():
            return False, "[shell_send] 缺少 command"
        # 子代理禁止（Critical: shell_send之前缺失此检查）
        if self._exec._subagent_restrictions.get("forbid_commands", False):
            return False, "[shell_send] 子代理不允许执行 shell 命令"
        # reviewer模式禁止
        from ..config import Config
        if Config().permission_mode == "reviewer":
            return False, "[shell_send] reviewer模式下禁止执行shell命令"
        # 危险模式预扫描
        hit = _check_dangerous_patterns(cmd)
        if hit:
            return False, f"[shell_send] 命中危险模式 ({hit}): " + cmd[:200]
        # 禁止命令检查（取第一个token，规范化路径）
        import os as _os2
        import shlex
        tokens = None
        try:
            tokens = shlex.split(cmd)
            if tokens:
                first_tok = tokens[0]
                cmd_base = _os2.basename(first_tok)
                if first_tok in FORBIDDEN_COMMANDS or cmd_base in FORBIDDEN_COMMANDS:
                    return False, "[shell_send] 命令被禁止执行: " + first_tok
                # 解释器内联代码检查（传入 tokens 列表）
                interp_hit = _check_interpreter_inline_code(tokens)
                if interp_hit:
                    return False, f"[shell_send] 命中解释器内联代码模式 ({interp_hit}): " + cmd[:200]
                # 危险命令标志检查（传入 tokens 列表）
                flags_hit = _check_dangerous_cmd_flags(tokens)
                if flags_hit:
                    return False, f"[shell_send] 命中危险命令标志 ({flags_hit}): " + cmd[:200]
        except ValueError:
            # shlex 解析失败（引号未闭合等），拒绝执行
            return False, "[shell_send] 命令解析失败（引号不匹配）: " + cmd[:200]
        except Exception:
            pass
        # P0-1 修复: 校验所有绝对路径参数必须在沙箱 work_dir 内
        if self._exec.sandbox is None:
            return False, "[shell_send] 未加载项目"
        cwd = str(self._exec.sandbox.work_dir)
        if cwd in (None, "", "."):
            return False, "[shell_send] 无法确定项目工作目录，已拒绝执行"
        if tokens:
            bad_path = _check_absolute_path_args(tokens, cwd)
            if bad_path:
                return False, "[shell_send] " + bad_path
        term = self._get_terminal()
        if term is None:
            return False, "[shell_send] 持久shell未启动"
        # 追加换行确保执行
        send_cmd = cmd.rstrip("\n") + "\n"
        if term.write(send_cmd):
            return True, f"[shell_send] 已发送: {cmd[:100]}"
        return False, "[shell_send] 写入失败"

    def shell_read(self, call) -> Tuple[bool, str]:
        """读取持久shell的输出（不阻塞）
        参数:
            clear: 是否清空缓冲区（默认true）
        """
        clear = call.get("clear", True)
        term = getattr(self._exec, "_persistent_terminal", None)
        if term is None or not term.is_running():
            return True, "[shell_read] 没有运行中的shell"
        out = term.read_output(clear=clear)
        if not out:
            return True, "[shell_read] (无新输出)"
        if len(out) > 8000:
            out = out[:8000] + "\n... (输出已截断)"
        return True, out

    def shell_cd(self, call) -> Tuple[bool, str]:
        """切换持久shell的工作目录
        参数:
            path: 目标目录（相对或绝对）
        """
        path = call.get("path", "")
        if not path:
            term = getattr(self._exec, "_persistent_terminal", None)
            if term:
                return True, f"[shell_cd] 当前目录: {term.cwd}"
            return True, "[shell_cd] 无shell"
        if self._exec.sandbox is None:
            return False, "[shell_cd] 未加载项目"
        # 安全路径解析
        from pathlib import Path
        p = Path(path)
        if not p.is_absolute():
            p = self._exec.sandbox.work_dir / p
        try:
            p = p.resolve()
            work_dir = self._exec.sandbox.work_dir.resolve()
            # 严格路径边界检查（修复startswith无分隔符的穿越漏洞）
            try:
                p.relative_to(work_dir)
            except ValueError:
                return False, f"[shell_cd] 不能切换到项目外: {p}"
        except Exception:
            return False, f"[shell_cd] 路径无效: {path}"
        if not p.is_dir():
            return False, f"[shell_cd] 目录不存在: {path}"

        term = self._get_terminal()
        if term is None:
            return False, "[shell_cd] shell未启动"
        term.set_cwd(str(p))
        # 读取一下cd后的输出（忽略错误）
        import time
        time.sleep(0.2)
        term.read_output(clear=True)
        return True, f"[shell_cd] 已切换到 {p}"

    def shell_stop(self, call) -> Tuple[bool, str]:
        """停止持久shell"""
        term = getattr(self._exec, "_persistent_terminal", None)
        if term is not None:
            term.terminate()
            self._exec._persistent_terminal = None
        return True, "[shell_stop] 持久shell已停止"
