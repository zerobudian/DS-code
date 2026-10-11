"""DS Code local HTTP/SSE entrypoint. Run: python3 -m sidecar.sidecar."""

import argparse
import asyncio
import hmac
import logging
import os
import queue
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

from sidecar.http_protocol import HTTPError, read_request, response, sse_event, sse_headers
from sidecar.runtime import ChatRuntime
from sidecar.service import SidecarService

logger = logging.getLogger(__name__)


class SidecarServer:
    def __init__(self, service, token="", stream_timeout=300):
        self.service, self.token = service, token
        self.stream_timeout = stream_timeout

    def authorize(self, request):
        origin = request.headers.get("origin")
        if origin not in (None, "null"):
            raise HTTPError(403, "不允许的请求来源")
        if self.token:
            supplied = request.headers.get("authorization", "")
            supplied = supplied[7:] if supplied.startswith("Bearer ") else ""
            if request.path == "/api/chat/stream":
                supplied = request.query.get("token", [supplied])[0]
            if not hmac.compare_digest(supplied.encode(), self.token.encode()):
                raise HTTPError(401, "未授权请求")
        return origin

    async def handle(self, reader, writer):
        origin = None
        streaming = False
        try:
            request = await read_request(reader)
            if request.method == "OPTIONS":
                if request.headers.get("origin") != "null":
                    raise HTTPError(403, "不允许的请求来源")
                writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n"
                             b"Access-Control-Allow-Origin: null\r\nVary: Origin\r\n"
                             b"Access-Control-Allow-Methods: GET,POST,PUT,DELETE,OPTIONS\r\n"
                             b"Access-Control-Allow-Headers: Content-Type,Authorization\r\n"
                             b"Connection: close\r\n\r\n")
            else:
                origin = self.authorize(request)
                if request.path == "/api/chat/stream":
                    self.service.require_method(request, "GET")
                    run_id = request.query.get("run_id", [""])[0]
                    if not run_id:
                        text = request.query.get("text", [""])[0]
                        if not text.strip():
                            raise HTTPError(400, "缺少 run_id 或 text")
                        run = await asyncio.to_thread(self.service.runtime.start,
                            request.query.get("project", ["demo"])[0], text)
                        run_id = run.id
                    run = self.service.runtime.attach(run_id)
                    streaming = True
                    writer.write(sse_headers(origin))
                    await writer.drain()
                    await self.stream(run, reader, writer)
                else:
                    data = await asyncio.to_thread(self.service.route, request)
                    writer.write(response(data, origin=origin))
            await writer.drain()
        except HTTPError as exc:
            if not streaming:
                writer.write(response({"error": str(exc)}, exc.status, origin))
                await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception:
            logger.exception("Local API request failed")
            if not streaming:
                writer.write(response({"error": "内部服务错误"}, 500, origin))
                await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def stream(self, run, reader, writer):
        deadline = time.monotonic() + self.stream_timeout
        heartbeat = time.monotonic()
        try:
            while time.monotonic() < deadline:
                if reader.at_eof():
                    return
                try:
                    kind, data = run.events.get_nowait()
                except queue.Empty:
                    if time.monotonic() - heartbeat > 10:
                        writer.write(b": heartbeat\n\n")
                        await writer.drain()
                        heartbeat = time.monotonic()
                    await asyncio.sleep(.02)
                    continue
                writer.write(sse_event(kind, data))
                await asyncio.wait_for(writer.drain(), 10)
                if kind in {"done", "srv_error"}:
                    return
            writer.write(sse_event("srv_error", {"code": 504, "message": "请求超时"}))
            await writer.drain()
        finally:
            await asyncio.to_thread(self.service.runtime.stop, run.id)
            with self.service.runtime.lock:
                self.service.runtime.runs.pop(run.id, None)


async def serve(host, port):
    from src.core.config import Config
    from src.core.chat_engine import ChatEngine
    from src.core.logging_config import setup_logging
    from src.sandbox.project_manager import ProjectManager
    setup_logging()
    runtime = ChatRuntime(Config(), ProjectManager(), ChatEngine())
    api = SidecarServer(SidecarService(runtime), os.environ.get("DSCODE_SIDECAR_TOKEN", ""))
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    async def watch_parent():
        parent = os.environ.get("DSCODE_PARENT_PID", "")
        if os.name != "posix" or not parent.isdecimal():
            return
        while not stopped.is_set():
            if os.getppid() != int(parent):
                stopped.set()
                return
            await asyncio.sleep(.5)
    parent_watch = asyncio.create_task(watch_parent())
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopped.set)
        except NotImplementedError:
            pass
    try:
        server = await asyncio.start_server(api.handle, host, port, limit=16 * 1024)
        async with server:
            print(f"[Sidecar] ready http://{host}:{port}", flush=True)
            await stopped.wait()
    finally:
        parent_watch.cancel()
        await asyncio.to_thread(runtime.close)


def main(host="127.0.0.1", port=8765):
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=port)
    args = parser.parse_args()
    try:
        asyncio.run(serve(host, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
