# -*- coding: utf-8 -*-
"""DNS 解析超时辅助模块（C-006）。

提供带超时的 socket.getaddrinfo 包装，避免 DNS 查询在弱网/恶意 DNS 下永久阻塞。
使用 daemon 线程执行解析，防止超时后滞留线程阻止进程退出。
"""
import socket
import threading
from typing import Any, Callable, List, Tuple

DEFAULT_DNS_TIMEOUT = 3.0

# 线程创建计数，用于命名和调试
_thread_counter = 0
_thread_counter_lock = threading.Lock()


def getaddrinfo_with_timeout(
    host: Any,
    port: Any = None,
    timeout: float = DEFAULT_DNS_TIMEOUT,
    getaddrinfo_fn: Callable[..., List[Tuple]] = None,
    *args: Any,
    **kwargs: Any,
) -> List[Tuple]:
    """带超时的 getaddrinfo 包装。

    Args:
        host: 待解析主机名或 IP。
        port: 端口号（可选）。
        timeout: 解析超时秒数，默认 3.0。
        getaddrinfo_fn: 实际使用的 getaddrinfo 函数；默认 socket.getaddrinfo。
            传入模块级保存的原始 getaddrinfo 可避免与 SSRF DNS pinning 补丁递归。
        *args, **kwargs: 透传给 getaddrinfo_fn。

    Returns:
        getaddrinfo 的结果列表。

    Raises:
        socket.timeout: 解析超时。
        原函数抛出的其它异常会原样抛出。
    """
    if getaddrinfo_fn is None:
        getaddrinfo_fn = socket.getaddrinfo

    result_holder = {"result": None, "exception": None, "done": False}
    done_event = threading.Event()

    def _resolve():
        try:
            result_holder["result"] = getaddrinfo_fn(host, port, *args, **kwargs)
        except Exception as e:
            result_holder["exception"] = e
        finally:
            result_holder["done"] = True
            done_event.set()

    global _thread_counter
    with _thread_counter_lock:
        _thread_counter += 1
        tid = _thread_counter

    t = threading.Thread(target=_resolve, name=f"dns_resolve_{tid}", daemon=True)
    t.start()

    if not done_event.wait(timeout=timeout):
        raise socket.timeout(f"DNS resolution timeout after {timeout}s for {host}")

    if result_holder["exception"] is not None:
        raise result_holder["exception"]
    return result_holder["result"]
