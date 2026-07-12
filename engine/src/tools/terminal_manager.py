# -*- coding: utf-8 -*-
"""内置终端管理器：在项目目录下启动一个长期运行的 shell 子进程。"""

import codecs
import logging
import os
import shlex
import shutil
import signal
import subprocess
import threading
from typing import List, Optional

logger = logging.getLogger(__name__)

from src.tools.subprocess_limits import acquire_subprocess_slot, release_subprocess_slot


def _preexec_terminal_safety() -> None:
    """终端专用preexec：setsid创建独立进程组，重置信号处理器，并设置宽松的rlimit。
    注意：持久终端长期运行，CPU/FILE限制比一次性命令更宽松，但仍设置NPROC/AS/FSIZE防止资源耗尽。
    """
    try:
        os.setsid()
    except Exception:
        pass
    # 重置关键信号为默认行为，避免继承父进程处理器
    for sig in (signal.SIGPIPE, signal.SIGINT, signal.SIGTERM,
                signal.SIGTSTP, signal.SIGQUIT, signal.SIGHUP):
        try:
            signal.signal(sig, signal.SIG_DFL)
        except Exception:
            pass
    # 设置基本资源限制（比一次性命令更宽松）
    try:
        import resource as _resource
        # 最多256个进程（防fork bomb）
        _resource.setrlimit(_resource.RLIMIT_NPROC, (256, 256))
    except Exception:
        pass
    try:
        import resource as _resource
        # 虚拟内存上限2GB（防止内存耗尽）
        _mem = 2 * 1024 * 1024 * 1024
        _resource.setrlimit(_resource.RLIMIT_AS, (_mem, _mem))
    except Exception:
        pass
    try:
        import resource as _resource
        # 文件写入上限100MB
        _fsize = 100 * 1024 * 1024
        _resource.setrlimit(_resource.RLIMIT_FSIZE, (_fsize, _fsize))
    except Exception:
        pass
    # RLIMIT_CPU不设：长期运行的shell可能需要执行长时间编译


class TerminalManager:
    """管理一个交互式 shell 子进程。

    - Linux/macOS 优先使用 bash，否则 sh
    - Windows 优先使用 cmd.exe
    - 环境变量清理：移除 LD_PRELOAD/LD_LIBRARY_PATH/PYTHONPATH
    - 输出通过后台线程异步读取
    - C-008: 受全局子进程并发/总数/速率限制
    """

    def __init__(self, cwd: Optional[str] = None, rows: int = 24, cols: int = 80, work_dir: Optional[str] = None):
        self.cwd = cwd or os.getcwd()
        self.work_dir = os.path.abspath(work_dir) if work_dir else None
        self.rows = rows
        self.cols = cols
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()  # 串行化 stdin 写入，防止并发写入交错
        self._buffer: List[str] = []
        self._closed = False
        self._reader_thread: Optional[threading.Thread] = None
        self._slot_acquired = False

    def start(self) -> bool:
        """启动 shell 子进程。closed后不可重启。"""
        with self._lock:
            if self._closed:
                return False
            if self._proc is not None:
                return True
            shell = self._detect_shell()
            if not shell:
                return False
            env = self._build_env()
            try:
                # C-008: 占用全局子进程槽位，防止并发/总数/速率超限和 fork bomb
                acquire_subprocess_slot()
                self._slot_acquired = True
                popen_kwargs = {"preexec_fn": _preexec_terminal_safety,
                                "start_new_session": True} if os.name == "posix" else {}
                self._proc = subprocess.Popen(
                    [shell],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=self.cwd,
                    env=env,
                    bufsize=0,
                    universal_newlines=False,
                    **popen_kwargs,
                )
            except RuntimeError:
                if self._slot_acquired:
                    release_subprocess_slot()
                    self._slot_acquired = False
                return False
            except Exception:
                if self._slot_acquired:
                    release_subprocess_slot()
                    self._slot_acquired = False
                return False
            try:
                self._reader_thread = threading.Thread(target=self._read_loop, daemon=True, name="TerminalReader")
                self._reader_thread.start()
            except Exception:
                # reader线程创建失败，清理进程
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=2)
                except Exception:
                    pass
                self._proc = None
                if self._slot_acquired:
                    release_subprocess_slot()
                    self._slot_acquired = False
                return False
            return True

    def _detect_shell(self) -> Optional[str]:
        if os.name == "nt":
            for candidate in ["cmd.exe", "powershell.exe"]:
                path = shutil.which(candidate)
                if path:
                    return path
            return None
        for candidate in ["bash", "sh", "zsh"]:
            path = shutil.which(candidate)
            if path:
                return path
        return None

    def _build_env(self) -> dict:
        """构造适合终端使用的环境变量（清理敏感变量，防止SSH agent/编辑器劫持）。"""
        env: dict = {}
        # 保留关键基础变量（注意：SSH_AUTH_SOCK/GIT_EDITOR/EDITOR/VISUAL不保留，防止横向移动和编辑器注入）
        _KEEP_KEYS = frozenset({
            "LANG", "LC_ALL", "TERM", "TZ", "HOME", "USER", "LOGNAME",
            "SHELL", "PATH", "COLORTERM", "TERM_PROGRAM", "PAGER",
            "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "no_proxy",
        })
        # 显式移除的高风险变量
        _REMOVE_KEYS = frozenset({
            "SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_EDITOR", "EDITOR", "VISUAL",
            "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",  # 防止X11/Wayland逃逸
            "SUDO_COMMAND", "SUDO_USER", "SUDO_UID", "SUDO_GID",
        })
        _SENSITIVE_SUBSTR = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
                             "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH")
        for k, v in os.environ.items():
            if k in _REMOVE_KEYS:
                continue
            if k not in _KEEP_KEYS:
                continue
            if any(s in k.upper() for s in _SENSITIVE_SUBSTR):
                continue
            env[k] = v
        # PATH：使用系统PATH但确保包含常用路径，且不在项目目录前
        if "PATH" not in env:
            env["PATH"] = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        # 添加Apple Silicon Homebrew路径
        if os.path.exists("/opt/homebrew/bin") and "/opt/homebrew/bin" not in env["PATH"]:
            env["PATH"] = "/opt/homebrew/bin:" + env["PATH"]
        # 设置PS1为简单标记，避免复杂的prompt污染输出
        env["PS1"] = "$ "
        return env

    def _read_loop(self):
        if self._proc is None or self._proc.stdout is None:
            return
        # 捕获stdout为局部变量，防止terminate()将_proc置None导致AttributeError
        stdout = self._proc.stdout
        _MAX_BUFFER_ITEMS = 5000  # 最多缓冲 5000 条，防止 OOM
        # 使用增量解码器，避免多字节 UTF-8 字符被按 chunk 截断
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while not self._closed:
                try:
                    chunk = stdout.read(1024)
                except Exception:
                    break
                if not chunk:
                    break
                try:
                    text = decoder.decode(chunk)
                except Exception:
                    text = chunk.decode("utf-8", errors="replace")
                with self._lock:
                    self._buffer.append(text)
                    # 超限时丢弃最旧的数据
                    if len(self._buffer) > _MAX_BUFFER_ITEMS:
                        del self._buffer[:len(self._buffer) - _MAX_BUFFER_ITEMS]
        except Exception as _exc:
            logger.debug("终端读取循环异常: %s", _exc, exc_info=True)

    def write(self, command: str) -> bool:
        """向 shell 写入一条命令。线程安全：写锁串行化，防止并发写入交错。"""
        with self._write_lock:
            with self._lock:
                if self._proc is None or self._proc.stdin is None:
                    return False
                stdin = self._proc.stdin
            try:
                data = (command.rstrip() + "\n").encode("utf-8", errors="replace")
                stdin.write(data)
                stdin.flush()
                return True
            except Exception:
                return False

    def read_output(self, clear: bool = True) -> str:
        """读取累计输出。"""
        with self._lock:
            out = "".join(self._buffer)
            if clear:
                self._buffer.clear()
            return out

    def set_cwd(self, cwd: str) -> None:
        """通过发送 cd 命令切换工作目录。路径经过 shell 元字符校验防止注入。"""
        if not cwd or not isinstance(cwd, str):
            return
        # 拒绝 shell 元字符防止命令注入
        for ch in ('"', "'", ';', '|', '&', '`', '$', '\n', '\r', '\t', '\0',
                   '>', '<', '*', '?', '[', ']', '(', ')', '{', '}', '!', '#', '~', '\\'):
            if ch in cwd:
                return
        if not os.path.isdir(cwd):
            return
        # 使用 shlex.quote 安全引用路径
        abs_cwd = os.path.abspath(cwd)
        if self.write("cd " + shlex.quote(abs_cwd)):
            with self._lock:
                self.cwd = abs_cwd

    def resize(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols

    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def terminate(self) -> None:
        with self._lock:
            self._closed = True
            proc = self._proc
            self._proc = None
            slot_acquired = self._slot_acquired
            self._slot_acquired = False
            reader = self._reader_thread
            self._reader_thread = None

        # 先关闭stdin让shell收到EOF，促使其退出
        try:
            if proc is not None and proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass

        try:
            if proc is not None and proc.poll() is None:
                # 尝试终止整个进程组（setsid已创建独立进程组）
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    # 强制杀死
                    try:
                        pgid = os.getpgid(proc.pid)
                        os.killpg(pgid, signal.SIGKILL)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        pass
        except Exception:
            pass

        # 关闭管道fd
        if proc is not None:
            for fd_name in ("stdin", "stdout", "stderr"):
                try:
                    fd = getattr(proc, fd_name, None)
                    if fd is not None:
                        fd.close()
                except Exception:
                    pass

        # 等待reader线程退出
        if reader is not None:
            try:
                reader.join(timeout=2)
            except Exception:
                pass

        # C-008: 释放全局子进程槽位
        if slot_acquired:
            try:
                release_subprocess_slot()
            except Exception:
                pass

    def __del__(self):
        self.terminate()
