# -*- coding: utf-8 -*-
"""子进程创建限制器（C-008）。

提供全局的并发/总数/速率限制，并在子进程中应用 RLIMIT_NPROC，
防止 run_command / terminal / 自定义命令等路径被子进程 fork bomb 拖垮。
"""
import logging
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# 并发、速率上限
MAX_CONCURRENT_SUBPROCESSES = 5
MAX_SUBPROCESS_CREATION_RATE = 10  # 每秒最多创建多少个
_SUBPROCESS_SLOT_TIMEOUT = 5.0

_concurrent_sem = threading.Semaphore(MAX_CONCURRENT_SUBPROCESSES)
_active_count = 0
_rate_count = 0
_rate_last_reset = time.monotonic()
_lock = threading.Lock()


def acquire_subprocess_slot(timeout: float = _SUBPROCESS_SLOT_TIMEOUT) -> None:
    """获取一个子进程槽位；若并发/速率超限则抛出 RuntimeError。

    注意：_total_count 的设计初衷是防止进程总数爆炸，但它不递减会导致
    长时间运行后所有子进程创建被永久拒绝。改为只限制**并发数**和**创建速率**，
    这两者已经足够防止 fork bomb 和资源耗尽。
    """
    global _active_count, _rate_count, _rate_last_reset
    if not _concurrent_sem.acquire(timeout=timeout):
        raise RuntimeError(
            f"并发外部子进程数超过上限 {MAX_CONCURRENT_SUBPROCESSES}"
        )
    try:
        with _lock:
            now = time.monotonic()
            if now - _rate_last_reset >= 1.0:
                _rate_last_reset = now
                _rate_count = 0
            _rate_count += 1
            if _rate_count > MAX_SUBPROCESS_CREATION_RATE:
                raise RuntimeError(
                    f"子进程创建速率超过上限 {MAX_SUBPROCESS_CREATION_RATE}/s"
                )
            _active_count += 1
    except Exception:
        _concurrent_sem.release()
        raise


def release_subprocess_slot() -> None:
    """释放一个子进程槽位。"""
    global _active_count
    with _lock:
        if _active_count > 0:
            _active_count -= 1
            _concurrent_sem.release()


@contextmanager
def subprocess_slot(timeout: float = _SUBPROCESS_SLOT_TIMEOUT):
    """子进程槽位上下文管理器，在 with 块内持有并发槽位。"""
    acquire_subprocess_slot(timeout)
    try:
        yield
    finally:
        release_subprocess_slot()


def apply_child_limits() -> None:
    """在子进程内设置资源限制（POSIX 平台）。

    设置 RLIMIT_NPROC / RLIMIT_CPU / RLIMIT_AS / RLIMIT_FSIZE 形成纵深防御，
    防止 fork bomb、CPU 耗尽、内存 OOM、写满磁盘。
    """
    try:
        import resource as _resource
    except ImportError:
        return
    try:
        # 对单个外部命令再收紧可创建进程数，防止 fork bomb
        _resource.setrlimit(_resource.RLIMIT_NPROC, (64, 64))
    except Exception as e:
        logger.debug("RLIMIT_NPROC 设置失败: %s", e, exc_info=True)
    try:
        # CPU 时间限制 30s（wall-clock timeout 通常为 300s，CPU time 不等同）
        _resource.setrlimit(_resource.RLIMIT_CPU, (30, 30))
    except Exception as e:
        logger.debug("RLIMIT_CPU 设置失败: %s", e, exc_info=True)
    try:
        # 内存限制 512MB
        _mem = 512 * 1024 * 1024
        _resource.setrlimit(_resource.RLIMIT_AS, (_mem, _mem))
    except Exception as e:
        logger.debug("RLIMIT_AS 设置失败: %s", e, exc_info=True)
    try:
        # 写入文件大小限制 10MB
        _fsize = 10 * 1024 * 1024
        _resource.setrlimit(_resource.RLIMIT_FSIZE, (_fsize, _fsize))
    except Exception as e:
        logger.debug("RLIMIT_FSIZE 设置失败: %s", e, exc_info=True)


def get_subprocess_stats() -> dict:
    """返回当前子进程限制器状态（调试用）。"""
    with _lock:
        return {
            "max_concurrent": MAX_CONCURRENT_SUBPROCESSES,
            "available_slots": MAX_CONCURRENT_SUBPROCESSES - _active_count,
            "active_count": _active_count,
            "rate_count": _rate_count,
            "max_rate": MAX_SUBPROCESS_CREATION_RATE,
        }


def _preexec_child_safety() -> None:
    """供 Popen(preexec_fn=...) 使用的入口函数（必须是顶层可序列化函数）。"""
    apply_child_limits()
