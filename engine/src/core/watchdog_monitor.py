# -*- coding: utf-8 -*-
"""文件系统监听 (Watchdog)。

右侧"项目文件树"在外部写入或工具调用后自动刷新。

安全重点:
1. 在 watchdog.EventHandler 的回调中把事件聚合到后台线程 — 访问本地 listdir/stat 全部包装在 try/except 中，
   任一异常不影响调用方；
2. 在 fallback 轮询时，先获取 root 目录以 realpath 规范化，避免跟随符号链接到外部；
3. 文件树刷新回调不写任何文件到磁盘，只计算文件系统状态；
4. debounce 时间窗口内多次事件合并为一次回调；
"""

import logging

logger = logging.getLogger(__name__)

import fnmatch
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, TYPE_CHECKING, Type

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False


_MIN_POLL_INTERVAL = 2.0  # fallback 轮询最小间隔（秒）
_MAX_SCAN_ENTRIES = 5000  # 单轮扫描最多条目数上限
_MAX_SCAN_DEPTH = 6       # 轮询扫描最大深度
_DEBOUNCE_WINDOW = 0.3    # debounce 窗口（秒）
# 监听事件忽略模式：降低大目录/临时文件导致的噪音与开销
_IGNORE_PATTERNS = {
    ".dscode", ".git", ".hg", ".svn", ".idea", ".vscode",
    "node_modules", "__pycache__", ".venv", "venv",
    "backup_*", "*.tmp", "*.log", "*.pyc",
}


def _is_ignored_path(path: str) -> bool:
    """检查路径是否命中忽略模式（按路径组件匹配）。"""
    try:
        parts = Path(path).parts
        for part in parts:
            for pat in _IGNORE_PATTERNS:
                if fnmatch.fnmatch(part, pat):
                    return True
    except Exception as _exc:
        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
    return False


class _Debouncer:
    """debounce 合并连续事件：窗口内多次触发只回调一次。

    使用 threading.Timer 实现，支持在线程中回调；回调异常被吞掉，不影响后续事件。
    """

    def __init__(self, callback: Callable[[], None], window: float):
        self._callback = callback
        self._window = float(window)
        self._lock = threading.Lock()
        self._pending = False
        self._timer: Optional[threading.Timer] = None
        self._last_call = 0.0

    def trigger(self) -> None:
        """收到事件时调用：重置计时器，等待窗口结束后统一回调。"""
        with self._lock:
            self._pending = True
            if self._timer is not None:
                try:
                    self._timer.cancel()
                except Exception:
                    pass
                self._timer = None
            self._timer = threading.Timer(self._window, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            if not self._pending:
                return
            self._pending = False
            self._timer = None
        now = time.monotonic()
        if now - self._last_call < self._window:
            return
        self._last_call = now
        try:
            self._callback()
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def stop(self) -> None:
        """取消待触发的计时器，阻止后续回调。调用方应在外部停止监听时调用。"""
        with self._lock:
            self._pending = False
            if self._timer is not None:
                try:
                    self._timer.cancel()
                except Exception:
                    pass
                self._timer = None


class _DummyEventHandler:
    """watchdog 不可用时 _Handler 的占位基类。"""
    pass


# 静态分析时始终使用 FileSystemEventHandler 作为基类；运行时根据可用性选择。
_HandlerBase: Type[Any]
if TYPE_CHECKING:
    _HandlerBase = FileSystemEventHandler
else:
    _HandlerBase = FileSystemEventHandler if WATCHDOG_AVAILABLE else _DummyEventHandler


class _Handler(_HandlerBase):
    """文件变化事件处理。

    - 仅作 debounce 后回调触发，不直接修改文件。
    """

    def __init__(self, debouncer: _Debouncer):
        self._debouncer = debouncer

    if WATCHDOG_AVAILABLE:
        def _should_handle(self, event) -> bool:
            try:
                if event.src_path and _is_ignored_path(event.src_path):
                    return False
                if getattr(event, "dest_path", None) and _is_ignored_path(event.dest_path):
                    return False
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            return True

        def on_created(self, event):
            if not self._should_handle(event):
                return
            try:
                self._debouncer.trigger()
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        def on_deleted(self, event):
            if not self._should_handle(event):
                return
            try:
                self._debouncer.trigger()
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        def on_modified(self, event):
            if not self._should_handle(event):
                return
            try:
                self._debouncer.trigger()
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        def on_moved(self, event):
            if not self._should_handle(event):
                return
            try:
                self._debouncer.trigger()
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)


class FileSystemMonitor:
    """文件系统监控（线程安全：start/stop 受锁保护）。

    - start(root, callback) 开始监听；
    - stop() 停止并等待线程退出；
    - 支持在内部线程中回调；
    - 并发 start/stop 调用受 self._lock 保证互斥执行。
    """

    def __init__(self):
        self._observer: Optional[object] = None
        self._watching_root: Optional[Path] = None
        self._callback: Optional[Callable[[], None]] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()
        self._lock = threading.RLock()  # 保护 start/stop 与内部字段更新
        self._debouncer: Optional[_Debouncer] = None

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------
    def start(self, root, callback: Callable[[], None]):
        # 先停止旧监听（_stop_internal 会持锁）
        self._stop_internal()
        try:
            root_path = Path(root).expanduser()
            root_real = root_path.resolve()
            if not root_real.is_dir():
                # 目录不存在则仅记录，不抛异常
                return
        except Exception:
            return

        with self._lock:
            self._watching_root = root_real
            self._callback = callback
            self._debouncer = _Debouncer(callback, _DEBOUNCE_WINDOW)
            # 重置 stop_event（clear 必须在锁内）
            self._stop_event.clear()

        if WATCHDOG_AVAILABLE:
            try:
                handler = _Handler(self._debouncer)
                # recursive=True 会递归监听；事件只调用者传入路径
                observer = Observer()
                observer.schedule(handler, str(self._watching_root), recursive=True)
                observer.daemon = True
                observer.start()
                with self._lock:
                    self._observer = observer
                return
            except Exception as e:
                print(f"[Watchdog] observer 启动失败, 回退到轮询: {e}")

        # fallback: polling
        self._start_poll()

    def stop(self):
        """停止当前监听（阻塞等待 observer / poll 线程退出）。"""
        self._stop_internal()

    # ------------------------------------------------------------------
    # 内部：统一停止逻辑
    # ------------------------------------------------------------------
    def _stop_internal(self):
        """在锁保护下停止 observer 和 poll 线程；对外停止等待线程退出。"""
        observer_to_stop = None
        poll_thread_to_join: Optional[threading.Thread] = None
        debouncer_to_stop: Optional[_Debouncer] = None
        with self._lock:
            if self._observer is not None:
                observer_to_stop = self._observer
                self._observer = None
            # 设置 stop_event，唤醒 wait() 中的轮询线程
            try:
                self._stop_event.set()
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            if self._poll_thread is not None:
                poll_thread_to_join = self._poll_thread
                self._poll_thread = None
            if self._debouncer is not None:
                debouncer_to_stop = self._debouncer
                self._debouncer = None
        # 在锁外停止 debouncer，避免 cancel 时持锁阻塞
        if debouncer_to_stop is not None:
            try:
                debouncer_to_stop.stop()
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        # stop observer 锁外执行（observer.stop 可能耗时）
        if observer_to_stop is not None:
            try:
                observer_to_stop.stop()
                observer_to_stop.join(timeout=2.0)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        # join polling 线程
        if poll_thread_to_join is not None and poll_thread_to_join.is_alive():
            try:
                poll_thread_to_join.join(timeout=3.0)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    # ------------------------------------------------------------------
    # 内部：轮询
    # ------------------------------------------------------------------
    def _start_poll(self):
        """fallback 轮询线程入口：
        - 每次用 stop_event.wait(interval)，stop_event 使线程立即退出；
        - 扫描目录是只读：用 _scan_state 严格控制深度与条目数；
        - 比较上一次状态与本次不同即通过 debouncer 合并后触发 callback。

        注意：last_state 在 start() 调用线程中同步捕获，避免 poll 线程被延迟调度时
        错过 start() 返回后立刻发生的文件变更。
        """
        def _run(initial_state: str):
            last_state = initial_state
            while not self._stop_event.is_set():
                # 使用 Event.wait 代替 time.sleep，使得 stop() 能立即唤醒退出
                self._stop_event.wait(timeout=_MIN_POLL_INTERVAL)
                if self._stop_event.is_set():
                    break
                try:
                    state = self._scan_state(self._watching_root)
                except Exception as _exc:
                    logger.debug("跳过异常项: %s", _exc, exc_info=True)
                    continue
                if state and state != last_state:
                    last_state = state
                    debouncer = self._debouncer
                    if debouncer is not None:
                        debouncer.trigger()

        # 同步捕获初始状态，确保与后续变更比较有确定性的起点
        try:
            initial_state = self._scan_state(self._watching_root)
        except Exception:
            initial_state = ""

        t = threading.Thread(
            target=_run, args=(initial_state,), name="WatchdogFallbackPoll", daemon=True
        )
        with self._lock:
            # double-check: 如果在创建线程期间已经调用了 stop，避免启动
            if self._stop_event.is_set():
                return
            self._poll_thread = t
            t.start()

    @staticmethod
    def _scan_state(root: Path, max_depth: int = _MAX_SCAN_DEPTH) -> str:
        """返回一个简短的目录状态摘要。只读，不写磁盘。

        策略：递归扫描，受 _MAX_SCAN_ENTRIES 条目数与 max_depth 深度限制，
        每个文件取 path|size|mtime 字符串拼接。
        """
        try:
            if root is None:
                return ""
            root_real = Path(root).resolve()
            if not root_real.is_dir():
                return ""
        except Exception:
            return ""

        parts: List[str] = []
        count = 0
        # BFS 栈：(dir_path, depth)
        stack = [(root_real, 0)]
        try:
            while stack and count < _MAX_SCAN_ENTRIES:
                cur_dir, depth = stack.pop(0)
                try:
                    with os.scandir(str(cur_dir)) as dir_iter:
                        for entry in dir_iter:
                            if count >= _MAX_SCAN_ENTRIES:
                                break
                            try:
                                if entry.is_symlink():
                                    # 不跟随符号链接，避免遍历到项目外
                                    continue
                                if _is_ignored_path(entry.path):
                                    continue
                                if entry.is_dir(follow_symlinks=False):
                                    parts.append(f"{entry.path}|d")
                                    if depth < max_depth:
                                        stack.append((Path(entry.path), depth + 1))
                                elif entry.is_file(follow_symlinks=False):
                                    try:
                                        st = entry.stat(follow_symlinks=False)
                                        # 使用浮点 mtime，支持同秒内多次写入也能被检测
                                        parts.append(
                                            f"{entry.path}|f|{st.st_size}|{st.st_mtime:.6f}"
                                        )
                                    except OSError as _exc:
                                        logger.debug("跳过异常项: %s", _exc, exc_info=True)
                                        continue
                            except OSError as _exc:
                                logger.debug("跳过异常项: %s", _exc, exc_info=True)
                                continue
                            count += 1
                except OSError as _exc:
                    logger.debug("跳过异常项: %s", _exc, exc_info=True)
                    continue
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        # 排序保证状态摘要稳定
        parts.sort()
        return "\n".join(parts)
