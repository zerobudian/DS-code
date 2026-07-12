"""MCP stdio 客户端。

通过子进程启动 MCP 服务器，以 JSON-RPC 2.0 经由 stdin/stdout 通信。
本实现基于官方 MCP Python SDK:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client, StdioServerParameters

核心要点:
  1. 长生命周期连接: session 与子进程在 start/stop 间持续存活，避免每次
     call_tool 都重新 fork。实现方式是通过持久化 event loop（后台线程
     run_forever）来承载所有异步操作，所有同步 API 通过
     asyncio.run_coroutine_threadsafe 提交到该 loop。
  2. 线程安全: 内部用 asyncio.Lock 保护对 session 的并发调用，保证同个
     ClientSession 同一时间只有一个请求。由于所有异步操作共用同一个持久化
     event loop，asyncio.Lock 可以正常工作。
  3. 同步 API + 异步实现: 对外暴露 start/stop/call_tool 等阻塞方法，内部
     通过持久化 event loop 跑异步代码，便于 Qt 线程调用。
  4. 错误降级: MCP SDK 缺失、子进程崩溃、超时、协议错误都返回结构化的
     MCPToolResult，上层 UI 可以直接显示给用户。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from datetime import timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP SDK 可用性检测 & 延迟导入
# ---------------------------------------------------------------------------
_MCP_AVAILABLE = False
try:
    from mcp import ClientSession  # noqa: F401
    from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: F401

    _MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - SDK 缺失时走 fallback 路径
    logger.warning("MCP Python SDK 未安装 (pip install mcp)，MCP 客户端将以 fallback 模式运行")


# ---------------------------------------------------------------------------
# 工具: 运行 async 函数到一个新的 event loop 中 (同步化)
# ---------------------------------------------------------------------------
def _run_async(coro, timeout: Optional[float] = None) -> Any:
    """在当前线程新建一个 event loop 并执行 coro，拿到同步返回值。

    注意: 不要在已存在 running loop 的线程里调用。GUI (Qt) 线程没有
    asyncio event loop，可以直接调用。

    此函数已保留用于向后兼容；MCPClient 内部已改用持久化 event loop 模式。
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        if timeout is not None:
            coro = asyncio.wait_for(coro, timeout=timeout)
        return loop.run_until_complete(coro)
    finally:
        try:
            # 清理待执行的 task
            pending = asyncio.all_tasks(loop)
            if pending:
                for t in pending:
                    t.cancel()
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            loop.close()
            asyncio.set_event_loop(None)


# ---------------------------------------------------------------------------
# MCPClient
# ---------------------------------------------------------------------------
class MCPClient:
    """单个 MCP 服务器的客户端连接。

    生命周期: new() -> start() -> [list_tools() -> call_tool()]* -> stop()

    异步实现 (asyncio)；对外 API 都提供同步版本，内部通过持久化 event loop
    运行在后台线程中，方便在 Qt 线程或普通同步代码里调用。
    """

    _LOOP_STOP_TIMEOUT = 5.0  # 停止 event loop 时的等待超时（秒）

    def __init__(
        self,
        server_name: str,
        command: str,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        working_dir: Optional[str] = None,
    ) -> None:
        """
        Args:
            server_name: 客户端内部标识（用于日志/聚合工具名前缀）
            command: 可执行文件路径 (例如 "python3" / "npx" / "uvx")
            args: 命令行参数
            env: 额外环境变量，会叠加在默认继承环境之上
            working_dir: 子进程的工作目录；None 则继承当前进程
        """
        self.server_name = server_name
        self.command = command
        self.args: List[str] = list(args or [])
        self.env: Dict[str, str] = dict(env or {})
        self.working_dir: Optional[str] = working_dir

        # 运行时状态
        self._session: Any = None                # ClientSession (MCP SDK)
        self._cm_exit_stack: Any = None           # stdio_client.__aexit__ 包装
        self._server_info: Dict[str, Any] = {}    # initialize 返回的元数据
        self._available_tools: List[Dict[str, Any]] = []
        self._lock: Any = None                    # asyncio.Lock，在持久化 loop 中创建
        self._running = False

        # 持久化 event loop（后台线程）
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_ready: threading.Event = threading.Event()
        self._loop_lock = threading.Lock()  # 保护 _ensure_loop / _stop_loop 的并发调用

        # 线程安全锁：保护 start/stop 等状态变更操作
        self._thread_lock = threading.Lock()
        # 保护 asyncio.Lock 的懒初始化，防止多线程创建多个锁
        self._lock_lock = threading.Lock()

    # --------------------- 属性 ---------------------
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def name(self) -> str:
        return self.server_name

    @property
    def available_tools(self) -> List[Dict[str, Any]]:
        return list(self._available_tools)

    # --------------------- 持久化 event loop 管理 ---------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """确保持久化 event loop 在后台线程中运行，返回该 loop。

        线程安全：内部使用 _loop_lock 保护。
        """
        with self._loop_lock:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
                self._loop_ready.clear()
                self._loop_thread = threading.Thread(
                    target=self._run_loop_forever,
                    name=f"mcp-{self.server_name}",
                    daemon=True,
                )
                self._loop_thread.start()
                # 等待 loop 真正开始运行
                if not self._loop_ready.wait(timeout=10.0):
                    raise RuntimeError("MCP event loop 启动超时")
            return self._loop

    def _run_loop_forever(self) -> None:
        """后台线程入口：运行 event loop 直到被 stop。

        修复：使用 loop.call_soon 将 _loop_ready.set() 推迟到 loop 实际开始
        运行后再触发，避免 race condition 导致 _ensure_loop 在 loop 尚未真正
        运行时就已经返回。
        P2-005 修复：loop 异常退出后重置状态标志，防止后续调用向已停止的 loop 提交任务。
        """
        if self._loop is None:
            raise RuntimeError("MCP event loop 未初始化")
        asyncio.set_event_loop(self._loop)
        # 将 ready 信号推迟到 loop 实际开始运行后，避免竞态条件
        self._loop.call_soon(self._loop_ready.set)
        try:
            self._loop.run_forever()
        except Exception:
            logger.exception("MCP event loop 异常退出")
        finally:
            # 清理 loop 中剩余的待处理任务
            try:
                pending = asyncio.all_tasks(self._loop)
                if pending:
                    for t in pending:
                        t.cancel()
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            # P2-005: 重置运行状态，让后续 _ensure_loop 能重建 loop
            with self._loop_lock:
                if self._loop is not None and not self._loop.is_closed():
                    try:
                        self._loop.close()
                    except Exception as _exc:
                        logger.debug("关闭异常 loop 失败: %s", _exc, exc_info=True)
                self._loop = None
                self._loop_thread = None
                self._running = False
                self._loop_ready.clear()

    def _stop_loop(self) -> None:
        """停止持久化 event loop 并等待后台线程退出。

        线程安全：内部使用 _loop_lock 保护。
        注意：
          - 通过 loop.call_soon_threadsafe(loop.stop) 向 loop 线程发送停止信号；
          - 待处理任务的取消、gather 和 loop.close() 全部在 loop 线程的
            _run_loop_forever finally 块中完成，避免从非 loop 线程调用
            run_until_complete 导致 RuntimeError；
          - 若后台线程未能在超时时间内退出，则作为最后手段直接关闭 loop
            并置空引用，由守护线程机制保证进程退出。
        """
        loop: "asyncio.AbstractEventLoop | None" = None
        thread: "threading.Thread | None" = None
        with self._loop_lock:
            loop = self._loop
            thread = self._loop_thread
            # 注意：不在此处置空 self._loop / self._loop_thread；
            # 让 _run_loop_forever 的 finally 块在 loop 线程内完成清理后
            # 再置空，避免从非 loop 线程操作 loop。

        scheduled_stop = False
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
                scheduled_stop = True
            except (RuntimeError, OSError):
                # loop 可能已被其它线程关闭或处于不可调度状态
                scheduled_stop = False

        if thread is not None and thread.is_alive():
            thread.join(timeout=self._LOOP_STOP_TIMEOUT)
            if thread.is_alive():
                logger.warning(
                    "MCP event loop 线程未能在 %.1fs 内退出，"
                    "由 daemon 机制在进程退出时回收",
                    self._LOOP_STOP_TIMEOUT,
                )
                # 最后手段：在非 loop 线程直接关闭 loop（不再调用 run_until_complete）
                if loop is not None and not loop.is_closed():
                    try:
                        loop.call_soon_threadsafe(loop.stop)
                    except Exception:
                        pass
                    # 给 loop 一个极短的宽限期再尝试 close
                    thread.join(timeout=1.0)
                    if not loop.is_closed():
                        try:
                            loop.close()
                        except Exception as _exc:
                            logger.debug("强制关闭 loop 失败: %s", _exc, exc_info=True)
                with self._loop_lock:
                    self._loop = None
                    self._loop_thread = None
                    self._running = False
                    self._loop_ready.clear()
                return

        # 正常路径：_run_loop_forever 的 finally 块已在 loop 线程完成
        # 取消待处理任务、gather、loop.close() 并将 self._loop 等置空。
        # 此处仅做兜底：若因异常导致 loop 仍未关闭/引用未清理，则补清理。
        if scheduled_stop and loop is not None and not loop.is_closed():
            try:
                loop.close()
            except Exception as _exc:
                logger.debug("兜底关闭 loop 失败: %s", _exc, exc_info=True)
        with self._loop_lock:
            if self._loop is loop:
                self._loop = None
            if self._loop_thread is thread:
                self._loop_thread = None
            self._running = False
            self._loop_ready.clear()

    def _run_on_loop(self, coro, timeout: Optional[float] = None) -> Any:
        """将 coro 提交到持久化 event loop 并等待结果。

        线程安全：通过 asyncio.run_coroutine_threadsafe 跨线程提交。
        """
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise asyncio.TimeoutError("操作超时")

    # --------------------- 异步实现 ---------------------
    async def start_async(self) -> Dict[str, Any]:
        """异步启动 MCP 服务器，完成 MCP handshake (initialize)，返回服务器信息。

        返回:
            {"server_name": str, "version": str, "capabilities": {...}, "ok": True}
            失败时返回 {"ok": False, "error": str}
        """
        if self._running:
            return {"ok": True, **self._server_info}

        if not _MCP_AVAILABLE:
            return {
                "ok": False,
                "error": "MCP Python SDK 未安装 (pip install mcp>=1.27)，无法启动 MCP 服务器",
                "server_name": self.server_name,
            }

        # 在持久化 loop 中创建锁（确保绑定到当前运行中的 loop）
        with self._lock_lock:
            if self._lock is None:
                self._lock = asyncio.Lock()

        try:
            server_params = StdioServerParameters(
                command=self.command,
                args=list(self.args),
                env=dict(self.env) if self.env else None,
                cwd=self.working_dir,
            )
            # 直接手动 enter，以获取可跨多次调用存活的 read/write stream
            cm = stdio_client(server_params)
            read_stream, write_stream = await cm.__aenter__()
            self._cm_exit_stack = cm

            session = ClientSession(read_stream, write_stream)
            init_result = await session.initialize()

            self._session = session
            self._running = True

            # 解析 server_info
            try:
                info_dict = (
                    init_result.serverInfo.model_dump()
                    if hasattr(init_result, "serverInfo") and init_result.serverInfo
                    else {}
                )
            except Exception:
                info_dict = {}

            try:
                caps = (
                    init_result.capabilities.model_dump()
                    if hasattr(init_result, "capabilities") and init_result.capabilities
                    else {}
                )
            except Exception:
                caps = {}

            self._server_info = {
                "ok": True,
                "server_name": info_dict.get("name", self.server_name),
                "version": info_dict.get("version", ""),
                "capabilities": caps,
            }

            # 初始化后立即拿一次工具列表
            try:
                self._available_tools = await self._list_tools_async_locked()
            except Exception as exc:  # pragma: no cover - 仅记录
                logger.warning("list_tools 初始化失败: %s", exc)

            return dict(self._server_info)

        except Exception as exc:
            logger.exception("MCP 服务器 %s 启动失败", self.server_name)
            # 尝试清理
            try:
                if self._cm_exit_stack is not None:
                    await self._cm_exit_stack.__aexit__(type(exc), exc, None)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            self._cm_exit_stack = None
            self._session = None
            self._running = False
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def stop_async(self) -> None:
        """优雅关闭会话与子进程。"""
        if not self._running:
            return
        self._running = False
        try:
            # 通知服务端结束会话 (可选)
            if self._session is not None and hasattr(self._session, "close"):
                try:
                    await asyncio.wait_for(self._session.close(), timeout=1.0)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        finally:
            if self._cm_exit_stack is not None:
                try:
                    await self._cm_exit_stack.__aexit__(None, None, None)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                self._cm_exit_stack = None
            self._session = None
            self._available_tools = []

    async def _list_tools_async_locked(self) -> List[Dict[str, Any]]:
        """已持有锁的内部 list_tools 实现。"""
        if self._session is None:
            raise RuntimeError("session 未初始化 (尚未 start)")

        tools_result = await self._session.list_tools()
        tools: List[Dict[str, Any]] = []
        raw_list = getattr(tools_result, "tools", []) or []
        for tool in raw_list:
            try:
                if hasattr(tool, "model_dump"):
                    data = tool.model_dump()
                elif isinstance(tool, dict):
                    data = dict(tool)
                else:
                    data = {
                        "name": getattr(tool, "name", ""),
                        "description": getattr(tool, "description", ""),
                        "inputSchema": getattr(tool, "inputSchema", {}),
                    }
                # 统一字段名
                name = data.get("name") or ""
                description = data.get("description") or ""
                input_schema = data.get("inputSchema") or data.get("input_schema") or {}
                tools.append(
                    {
                        "name": name,
                        "description": description,
                        "inputSchema": input_schema,
                        "server": self.server_name,
                    }
                )
            except Exception as _exc:  # pragma: no cover
                logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
        return tools

    async def list_tools_async(self) -> List[Dict[str, Any]]:
        """列出该服务器所有可用工具。"""
        if not self._running or self._session is None:
            raise RuntimeError("MCP 客户端尚未 start()")
        if self._lock is None:
            raise RuntimeError("asyncio.Lock 未初始化，请先调用 start()")
        async with self._lock:
            tools = await self._list_tools_async_locked()
            self._available_tools = tools
            return tools

    async def call_tool_async(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        read_timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """异步调用工具。返回 MCPToolResult 字典:
        {"ok": bool, "content": str, "error": str | None, "isError": bool, "raw": Any}
        """
        if not _MCP_AVAILABLE:
            return {
                "ok": False,
                "content": "",
                "error": "MCP Python SDK 未安装",
                "isError": True,
                "raw": None,
            }
        if not self._running or self._session is None:
            return {
                "ok": False,
                "content": "",
                "error": "MCP 客户端尚未 start()",
                "isError": True,
                "raw": None,
            }
        if self._lock is None:
            return {
                "ok": False,
                "content": "",
                "error": "asyncio.Lock 未初始化，请先调用 start()",
                "isError": True,
                "raw": None,
            }

        async with self._lock:
            try:
                kwargs: Dict[str, Any] = {"name": tool_name, "arguments": arguments or {}}
                if read_timeout is not None:
                    kwargs["read_timeout_seconds"] = timedelta(seconds=read_timeout)

                result = await self._session.call_tool(**kwargs)
                is_error = bool(getattr(result, "isError", False))
                content_parts: List[str] = []
                for item in getattr(result, "content", []) or []:
                    # 处理 TextContent / ImageContent 等
                    if hasattr(item, "text") and item.text is not None:
                        content_parts.append(str(item.text))
                    elif hasattr(item, "model_dump"):
                        try:
                            data = item.model_dump()
                            if "text" in data:
                                content_parts.append(str(data["text"]))
                            else:
                                content_parts.append(str(data))
                        except Exception:
                            content_parts.append(str(item))
                    else:
                        content_parts.append(str(item))

                content_text = "\n".join(content_parts)
                return {
                    "ok": not is_error,
                    "content": content_text,
                    "error": None if not is_error else "工具返回错误",
                    "isError": is_error,
                    "raw": result,
                }
            except asyncio.TimeoutError as exc:
                return {
                    "ok": False,
                    "content": "",
                    "error": f"调用 MCP 工具超时: {exc}",
                    "isError": True,
                    "raw": None,
                }
            except Exception as exc:
                logger.exception("MCP 工具调用异常 [%s]", tool_name)
                return {
                    "ok": False,
                    "content": "",
                    "error": f"{type(exc).__name__}: {exc}",
                    "isError": True,
                    "raw": None,
                }

    # --------------------- 同步包装 ---------------------
    def start(self, timeout: float = 30.0) -> Dict[str, Any]:
        """同步启动。"""
        with self._thread_lock:
            return self._run_on_loop(self.start_async(), timeout=timeout)

    def stop(self) -> None:
        """同步停止。"""
        with self._thread_lock:
            if not self._running:
                return
            try:
                self._run_on_loop(self.stop_async(), timeout=5.0)
            except Exception as exc:  # pragma: no cover
                logger.warning("MCP 客户端关闭时出错: %s", exc)
                self._running = False
                self._session = None
                self._cm_exit_stack = None
            finally:
                # 确保持久化 event loop 被正确关闭
                self._stop_loop()

    def list_tools(self) -> List[Dict[str, Any]]:
        """同步列出工具。"""
        result = self._run_on_loop(self.list_tools_async(), timeout=30.0)
        return result or []

    def call_tool(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """同步调用工具。"""
        return self._run_on_loop(
            self.call_tool_async(tool_name, arguments, timeout),
            timeout=timeout + 5.0,
        )

    # --------------------- 上下文管理器 (可选) ---------------------
    def __enter__(self) -> "MCPClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    async def __aenter__(self) -> "MCPClient":
        # 异步上下文管理器：使用异步 start 避免阻塞事件循环
        await self.start_async()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # 异步上下文管理器：使用异步 stop 优雅关闭
        await self.stop_async()