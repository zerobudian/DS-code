# -*- coding: utf-8 -*-
"""实时预览服务器 — 轻量 HTTP + SSE 热重载。

架构：
1. 在后台线程启动 http.server，绑定到 127.0.0.1:port（自动选空闲端口）
2. `/__reload__` 端点发送 SSE 事件，客户端订阅后收到文件变更即刷新
3. HTML 响应自动注入 reload-client 脚本（`<script>...</script>`）
4. Python `webbrowser` 模块或外部浏览器都可以打开 `http://127.0.0.1:port/`

线程安全：所有外部调用（start/stop/trigger_reload）都通过 `_lock` 保护。
"""

import html
import http.server
import json
import logging
import mimetypes
import secrets
import select
import socket
import socketserver
import threading
import traceback
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)


# ============================================================================
# SSE 客户端集
# ============================================================================

class _SSEHandler(http.server.BaseHTTPRequestHandler):
    """处理 HTTP 请求 + SSE 事件。

    注意：此类由 ThreadingHTTPServer 在各自线程中实例化，
    因此它通过模块级属性访问共享状态 (server_state)。
    """

    # H-011: SSE/写操作 CSRF Token 参数与头
    _CSRF_TOKEN_PARAM = "csrf_token"
    _CSRF_TOKEN_HEADER = "X-CSRF-Token"

    # log_message 保持安静 — 默认会打印到 stderr，在 IDE 中会刷屏
    def log_message(self, format: str, *args) -> None:
        return

    def log_request(self, code: str = "-", size: str = "-") -> None:
        return

    def _send_cors_headers(self) -> None:
        """为本地预览添加 CORS 头，仅允许 localhost 来源。"""
        self.send_header("Access-Control-Allow-Origin", "http://localhost:*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Cache-Control, X-CSRF-Token")

    def _get_origin(self) -> Optional[str]:
        """读取 Origin 或 Referer 头，用于 CSRF 判断。"""
        origin = self.headers.get("Origin")
        if origin:
            return origin
        referer = self.headers.get("Referer")
        if referer:
            # 只取 scheme://host 部分
            try:
                parsed = urllib.parse.urlparse(referer)
                return f"{parsed.scheme}://{parsed.netloc}"
            except Exception:
                pass
        return None

    @staticmethod
    def _is_local_origin(origin: Optional[str]) -> bool:
        """H-011: 仅允许本地来源（localhost / 127.0.0.1）。"""
        if not origin:
            return True  # 无 Origin/Referer 时放行（curl/直接访问）
        origin_lower = origin.lower()
        for allowed in (
            "http://localhost",
            "https://localhost",
            "http://127.0.0.1",
            "https://127.0.0.1",
        ):
            if origin_lower.startswith(allowed):
                return True
        return False

    def _check_origin_or_forbid(self) -> bool:
        """H-011: 检查 Origin/Referer 是否来自本地；非本地来源返回 403。"""
        if not self._is_local_origin(self._get_origin()):
            self.send_error(403, "Forbidden: invalid origin")
            return False
        return True

    def _validate_csrf_token(self, state) -> bool:
        """H-011: SSE 端点校验 CSRF Token（支持 query / header）。"""
        parsed = urllib.parse.urlparse(self.path)
        token = urllib.parse.parse_qs(parsed.query).get(self._CSRF_TOKEN_PARAM, [None])[0]
        if not token:
            token = self.headers.get(self._CSRF_TOKEN_HEADER)
        if token and token == state.csrf_token:
            return True
        self.send_error(403, "Forbidden: invalid csrf token")
        return False

    # ---- 路由 ----
    def do_GET(self) -> None:
        state = getattr(self.server, "_preview_state", None)
        if state is None:
            self.send_error(503, "Server not ready")
            return

        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        # SSE 订阅端点
        if path in ("/__reload__", "/__sse__"):
            self._handle_sse(state)
            return

        # 状态端点
        if path == "/__status__":
            self._handle_status(state)
            return

        # 静态文件服务
        self._handle_file(state, path)

    def do_POST(self) -> None:
        state = getattr(self.server, "_preview_state", None)
        if state is None:
            self.send_error(503, "Server not ready")
            return

        # H-011: 写操作必须来自本地 origin
        if not self._check_origin_or_forbid():
            return

        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/__reload__":
            # H-011: POST 刷新也需要 CSRF Token
            if not self._validate_csrf_token(state):
                return
            state.trigger_reload("manual")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"ok": True, "reload": True}).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_OPTIONS(self) -> None:
        state = getattr(self.server, "_preview_state", None)
        if state is None:
            self.send_error(503, "Server not ready")
            return
        # H-011: 预检请求也校验本地 origin
        if not self._check_origin_or_forbid():
            return
        self.send_response(204, "No Content")
        self._send_cors_headers()
        self.end_headers()

    # ---- 处理函数 ----
    def _is_sensitive_path(self, target: Path, root: Path) -> bool:
        """禁止访问敏感文件/目录（API key、配置、密钥、版本控制、隐私目录）。"""
        try:
            rel_parts = target.relative_to(root).parts
        except ValueError:
            return True
        sensitive_names = {
            ".dscode", ".git", ".hg", ".svn", ".ssh", ".gnupg", ".aws", ".docker",
            "node_modules", "venv", ".venv", "__pycache__",
        }
        sensitive_files = {
            ".env", ".env.local", ".env.production", ".env.development",
            "secret.key", "secrets.json", "id_rsa", "id_rsa.pub", "id_dsa",
            "id_ecdsa", "id_ed25519", ".netrc", ".pypirc", "token.json",
        }
        for part in rel_parts:
            if part in sensitive_names:
                return True
            if part.startswith(".") and part not in {".", ".."}:
                # 默认隐藏目录/文件均不对外暴露
                return True
        if rel_parts and rel_parts[-1] in sensitive_files:
            return True
        return False

    def _handle_sse(self, state) -> None:
        """处理 /__reload__ SSE 端点，用于触发浏览器刷新。

        注册前初始化与事件循环分离：注册前失败不会泄漏客户端；注册成功后
        通过独立 try/finally 保证 unregister_client 被执行。
        """
        # H-011: SSE 端点必须校验 CSRF Token
        if not self._validate_csrf_token(state):
            return

        # 阶段 1：发送响应头与 welcome 事件（注册前初始化）
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self._send_cors_headers()
            self.end_headers()

            # 先发送一个 welcome 事件，建立连接
            self.wfile.write(b"event: ready\ndata: ok\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

        # 阶段 2：注册客户端
        client_id: Optional[int] = None
        try:
            client_id = state.register_client(self.wfile)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

        # 阶段 3：事件循环 — 注册成功后必须注销
        try:
            # 保持连接 — 定期发送心跳，同时通过 select 检测客户端是否已断开
            while True:
                # 15s 心跳间隔，期间若连接可读（对端关闭或发送数据）则提前醒来
                readable, _, _ = select.select([self.connection], [], [], 15)
                if readable:
                    try:
                        # 对 SSE 来说客户端不应发送数据；读到空字节表示断开
                        data = self.connection.recv(1)
                        if not data:
                            break
                        # 若意外收到数据，直接忽略并继续心跳
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                if not state.is_running():
                    break
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        finally:
            if client_id is not None:
                state.unregister_client(client_id)

    def _handle_status(self, state) -> None:
        info = {
            "running": state.is_running(),
            "port": state.port,
            "root": str(state.root),
            "clients": state.client_count(),
            "reloads": state.reload_count(),
        }
        body = json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_file(self, state, path: str) -> None:
        root = state.root

        # 将 URL 路径规范化到文件系统路径
        if path in ("", "/"):
            # 根路径：先找 index.html，再回退到目录列表
            index_file = root / "index.html"
            if index_file.is_file():
                self._serve_file(index_file, state)
                return
            self._serve_directory_listing(root, state)
            return

        # 防止目录穿越（path traversal）
        try:
            relative = path.lstrip("/")
            target = (root / relative).resolve()
            # 确保 target 在 root 目录内
            root_resolved = root.resolve()
            try:
                target.relative_to(root_resolved)
            except ValueError:
                self.send_error(403, "Forbidden")
                return
        except (OSError, RuntimeError):
            self.send_error(400, "Bad Path")
            return

        # 敏感路径访问控制
        if self._is_sensitive_path(target, root_resolved):
            self.send_error(403, "Forbidden")
            return

        if target.is_dir():
            # 目录：先试 index.html，否则目录列表
            index_file = target / "index.html"
            if index_file.is_file():
                self._serve_file(index_file, state)
            else:
                self._serve_directory_listing(target, state)
            return

        if target.is_file():
            self._serve_file(target, state)
            return

        self.send_error(404, "Not Found")

    # ---- 文件服务 ----
    def _serve_file(self, file_path: Path, state) -> None:
        # 拒绝符号链接，防止通过链接读取项目外部文件
        try:
            if file_path.is_symlink():
                self.send_error(403, "Forbidden")
                return
        except OSError:
            self.send_error(400, "Bad Path")
            return

        ext = file_path.suffix.lower()

        try:
            size = file_path.stat().st_size
        except OSError:
            self.send_error(404)
            return

        mime, _ = mimetypes.guess_type(str(file_path))
        if mime is None:
            mime = "application/octet-stream"
            if ext in (".ts", ".tsx"):
                mime = "text/typescript"
        if mime.startswith("text/") or mime in ("application/json", "application/javascript"):
            mime = f"{mime}; charset=utf-8"

        _MAX_HTML_SIZE = 10 * 1024 * 1024  # 10 MB
        _MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB
        try:
            if size > _MAX_FILE_SIZE:
                self.send_error(413, "Payload Too Large")
                return
            if ext in (".html", ".htm") and size <= _MAX_HTML_SIZE:
                # HTML — 先读取 + 注入 CSRF Token meta 与 reload 脚本，再发送 headers
                try:
                    with open(file_path, "rb") as f:
                        data = f.read()
                except OSError:
                    return

                # H-011: 生成含 CSRF Token 的 HTML 注入片段
                csrf_meta = (
                    f'<meta name="csrf-token" content="{html.escape(state.csrf_token)}">'
                ).encode("utf-8")
                reload_script = (
                    b"\n<script>"
                    b"(function(){"
                    b"if(typeof EventSource==='undefined'){return;}"
                    b"var meta=document.querySelector('meta[name=\"csrf-token\"]');"
                    b"var token=meta?meta.content:'';"
                    b"var es=new EventSource('/__reload__'+(token?'?csrf_token='+encodeURIComponent(token):''));"
                    b"es.addEventListener('reload',function(){window.location.reload();});"
                    b"es.addEventListener('message',function(e){if(e.data==='reload'){window.location.reload();}});"
                    b"es.onerror=function(){es.close();setTimeout(function(){window.location.reload();},1500);};"
                    b"})();"
                    b"</script>"
                )

                # 优先在 <head> 中插入 CSRF Token；无 <head> 时放在文档开头
                head_open = data.lower().find(b"<head>")
                if head_open >= 0:
                    insert_pos = head_open + len(b"<head>")
                    data = data[:insert_pos] + b"\n" + csrf_meta + data[insert_pos:]
                else:
                    data = csrf_meta + b"\n" + data

                # 在 </body> 或 </html> 前插入 reload 脚本
                close_body = data.rfind(b"</body>")
                close_html = data.rfind(b"</html>")
                if close_body >= 0:
                    data = data[:close_body] + reload_script + data[close_body:]
                elif close_html >= 0:
                    data = data[:close_html] + reload_script + data[close_html:]
                else:
                    data = data + reload_script

                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self._send_cors_headers()
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, OSError) as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            else:
                # 其他文件 — 先发送 headers，再流式分块读取
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-cache")
                self._send_cors_headers()
                self.end_headers()
                try:
                    with open(file_path, "rb") as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError) as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        except Exception as e:
            logger.warning("[preview_server] _serve_file %s failed: %s\n%s",
                           file_path, e, traceback.format_exc())
            self.send_error(500, "Internal Server Error")

    def _serve_directory_listing(self, dir_path: Path, state) -> None:
        """简单的目录列表 — 显示当前目录的文件树。"""
        try:
            entries = sorted(
                [p for p in dir_path.iterdir() if not p.is_symlink() and not p.name.startswith(".")],
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except OSError:
            self.send_error(403, "Cannot read directory")
            return

        try:
            rel = str(dir_path.relative_to(state.root)) if dir_path != state.root else ""
        except ValueError:
            rel = ""

        rows: List[str] = []
        for e in entries:
            try:
                e_name = html.escape(e.name)
                e_rel = e_name if not rel else f"{html.escape(rel)}/{e_name}"
                is_dir = e.is_dir()
                icon = "📁" if is_dir else "📄"
                # 文件可能在 iterdir 后被删除，stat() 需要异常保护
                try:
                    size = f"{e.stat().st_size:,} B" if not is_dir else "—"
                except OSError:
                    size = "—"
                link = html.escape(f"/{e_rel}/" if is_dir else f"/{e_rel}")
                rows.append(
                    f'  <tr><td style="padding:8px 12px">{icon}</td>'
                    f'<td style="padding:8px 12px"><a href="{link}" '
                    f'style="color:#2563eb;text-decoration:none;font-family:system-ui,sans-serif;font-size:14px">'
                    f'{e_name}{"/" if is_dir else ""}</a></td>'
                    f'<td style="padding:8px 12px;color:#64748b;font-size:13px;text-align:right">{size}</td></tr>'
                )
            except OSError as _exc:
                logger.debug("跳过异常项: %s", _exc, exc_info=True)
                continue

        rel_escaped = html.escape(rel or "项目根目录")
        csrf_meta = f'<meta name="csrf-token" content="{html.escape(state.csrf_token)}">'
        reload_script = (
            "<script>"
            "(function(){"
            "if(typeof EventSource==='undefined'){return;}"
            "var meta=document.querySelector('meta[name=\"csrf-token\"]');"
            "var token=meta?meta.content:'';"
            "var es=new EventSource('/__reload__'+(token?'?csrf_token='+encodeURIComponent(token):''));"
            "es.addEventListener('reload',function(){window.location.reload();});"
            "es.addEventListener('message',function(e){if(e.data==='reload'){window.location.reload();}});"
            "es.onerror=function(){es.close();setTimeout(function(){window.location.reload();},1500);};"
            "})();"
            "</script>"
        )
        html_doc = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"{csrf_meta}"
            "<title>DS Code — Preview</title>"
            "<style>"
            "body{font-family:system-ui,sans-serif;background:#f8fafc;margin:0;padding:40px;}"
            "h1{color:#0f172a;font-size:22px;font-weight:700;margin:0 0 20px 0;}"
            "table{background:white;border-radius:12px;border-collapse:collapse;"
            "box-shadow:0 1px 3px rgba(0,0,0,0.08);width:100%;max-width:720px;}"
            "th{background:#f1f5f9;color:#334155;font-weight:600;padding:12px;text-align:left;"
            "font-size:13px;border-bottom:2px solid #e2e8f0;}"
            "td{border-bottom:1px solid #f1f5f9;}"
            "tr:hover{background:#f8fafc;}tr:last-child td{border-bottom:none;}"
            "</style></head><body>"
            f"<h1>📂 {rel_escaped}</h1>"
            f"<p style='color:#64748b;font-size:14px;margin-bottom:20px'>"
            f"自动热重载已启用 · 文件变更后页面将自动刷新</p>"
            "<table><thead><tr><th></th><th style='padding:12px'>名称</th>"
            f"<th style='padding:12px;text-align:right'>大小</th></tr></thead><tbody>"
        )
        # 返回上级链接
        if rel:
            parent = "/".join(rel.split("/")[:-1])
            parent_link = html.escape("/" + parent if parent else "/")
            html_doc += (
                f'  <tr><td style="padding:8px 12px">⬆️</td>'
                f'<td style="padding:8px 12px"><a href="{parent_link}" '
                f'style="color:#2563eb;text-decoration:none;font-family:system-ui,sans-serif;font-size:14px">'
                f'.. (返回上级)</a></td><td></td></tr>'
            )
        html_doc += "".join(rows)
        html_doc += f"</tbody></table>{reload_script}</body></html>"

        body = html_doc.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._send_cors_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError) as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)


# ============================================================================
# 预览服务器状态
# ============================================================================

class _PreviewState:
    def __init__(self, root: Path, port: int):
        self.root = root
        self.port = port
        self._lock = threading.Lock()
        self._clients: Dict[int, Any] = {}
        self._next_client_id = 0
        self._running = True
        self._reloads = 0
        # H-011: 每次启动服务器生成随机 CSRF Token，HTML 与 SSE 共享
        self.csrf_token = secrets.token_urlsafe(16)

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def stop(self) -> None:
        with self._lock:
            self._running = False
            for w in list(self._clients.values()):
                try:
                    w.close()
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            self._clients.clear()

    def register_client(self, wfile) -> int:
        with self._lock:
            cid = self._next_client_id
            self._next_client_id += 1
            self._clients[cid] = wfile
            return cid

    def unregister_client(self, cid: int) -> None:
        with self._lock:
            self._clients.pop(cid, None)

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def reload_count(self) -> int:
        with self._lock:
            return self._reloads

    def trigger_reload(self, reason: str = "file_change") -> None:
        """向所有已连接的客户端发送 reload SSE 事件，并清理失效连接。"""
        with self._lock:
            self._reloads += 1
            clients = list(self._clients.items())

        msg = f"event: reload\ndata: {reason}\n\n".encode("utf-8")
        dead: List[int] = []
        for cid, wfile in clients:
            try:
                wfile.write(msg)
                wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError, AttributeError):
                dead.append(cid)
        if dead:
            with self._lock:
                for cid in dead:
                    self._clients.pop(cid, None)


# ============================================================================
# HTTP 服务器包装
# ============================================================================

class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """支持多线程的 HTTP 服务器。daemon_threads = True 确保关闭时不会卡住。"""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, state: _PreviewState):
        super().__init__(addr, handler)
        self._preview_state = state


class PreviewServer:
    """轻量级实时预览服务器 — 用户调用 start() 启动，stop() 停止。

    典型用法::

        server = PreviewServer("/path/to/project")
        url = server.start()
        print(f"预览已启动: {url}")
        ...
        server.trigger_reload()  # 文件变更时调用
        ...
        server.stop()
    """

    def __init__(self, root_dir: str):
        self._root = Path(root_dir).resolve()
        self._port = 0
        self._server: Optional[_ThreadingServer] = None
        self._thread: Optional[threading.Thread] = None
        self._state: Optional[_PreviewState] = None
        self._lock = threading.Lock()
        # 注册 MIME 类型 — 某些系统缺省不识别 .ts/.mjs
        mimetypes.add_type("application/javascript", ".mjs", True)
        mimetypes.add_type("text/typescript", ".ts", True)
        mimetypes.add_type("text/typescript", ".tsx", True)

    # ---- 生命周期 ----
    def start(self, preferred_port: int = 0) -> str:
        """启动服务器，返回 URL。preferred_port 为 0 时自动选空闲端口。

        绑定失败时自动尝试 8000-9000 或让系统分配端口；
        直接创建服务器对象，避免先探测端口再释放导致的端口竞态。
        """
        with self._lock:
            if self._server is not None:
                return self.url()

            candidates = []
            if preferred_port:
                candidates.append(preferred_port)
            candidates.extend(range(8000, 9001))
            candidates.append(0)  # 最后让系统分配

            server: Optional[_ThreadingServer] = None
            state: Optional[_PreviewState] = None
            port = 0
            for candidate in candidates:
                try:
                    state = _PreviewState(self._root, 0)
                    server = _ThreadingServer(("127.0.0.1", candidate), _SSEHandler, state)
                    port = server.server_address[1]
                    state.port = port
                    break
                except OSError as _exc:
                    logger.debug("跳过异常项: %s", _exc, exc_info=True)
                    continue

            if server is None or state is None:
                self._port = 0
                return ""

            self._port = port
            self._state = state
            self._server = server
            self._server.daemon_threads = True

            self._thread = threading.Thread(target=self._serve_forever, name="preview-server", daemon=True)
            self._thread.start()

        return self.url()

    def _serve_forever(self) -> None:
        if self._server is None:
            return
        try:
            self._server.serve_forever(poll_interval=0.5)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def stop(self) -> None:
        """停止服务器。线程安全、可重复调用。"""
        with self._lock:
            if self._state is not None:
                self._state.stop()
            if self._server is not None:
                try:
                    self._server.shutdown()
                    self._server.server_close()
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            self._server = None
            self._state = None
            self._thread = None

    def is_running(self) -> bool:
        with self._lock:
            return self._state is not None and self._state.is_running()

    # ---- 触发刷新 ----
    def trigger_reload(self, reason: str = "file_change") -> int:
        """触发一次浏览器刷新。返回当前连接的客户端数量。"""
        state = self._state
        if state is None:
            return 0
        state.trigger_reload(reason)
        return state.client_count()

    def client_count(self) -> int:
        state = self._state
        return state.client_count() if state is not None else 0

    # ---- 访问器 ----
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}/"

    @property
    def port(self) -> int:
        return self._port

    @property
    def root(self) -> Path:
        return self._root

    @property
    def csrf_token(self) -> Optional[str]:
        """H-011: 返回当前服务器的 CSRF Token（SSE/POST 校验用）。"""
        return self._state.csrf_token if self._state is not None else None

    # ---- 工具 ----
    @staticmethod
    def _try_port(port: int) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def _try_find_port(self, preferred: int) -> int:
        if preferred and self._try_port(preferred):
            return preferred
        # 从 8000 起尝试
        for p in range(8000, 9001):
            if self._try_port(p):
                return p
        # 最后让系统分配
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
        finally:
            sock.close()


# ============================================================================
# 便捷工厂：一个进程中单例共享
# ============================================================================

_preview_instance: Optional[PreviewServer] = None
_preview_instance_lock = threading.Lock()


def get_preview_server(root_dir: str) -> PreviewServer:
    """获取（或创建）单例预览服务器。"""
    global _preview_instance
    with _preview_instance_lock:
        if _preview_instance is None:
            _preview_instance = PreviewServer(root_dir)
        else:
            # 如果 root_dir 改变了 → 重启
            if str(_preview_instance.root) != str(Path(root_dir).resolve()):
                try:
                    _preview_instance.stop()
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                _preview_instance = PreviewServer(root_dir)
        return _preview_instance


def start_preview(root_dir: str, preferred_port: int = 0) -> str:
    """便捷函数：启动并返回 URL。"""
    return get_preview_server(root_dir).start(preferred_port)


def stop_preview() -> None:
    """便捷函数：停止全局预览服务器（如果有）。"""
    global _preview_instance
    with _preview_instance_lock:
        srv = _preview_instance
        _preview_instance = None
    if srv is not None:
        try:
            srv.stop()
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)


def trigger_reload(reason: str = "file_change") -> int:
    """便捷函数：触发刷新，返回当前客户端数。"""
    srv = _preview_instance
    if srv is None or not srv.is_running():
        return 0
    return srv.trigger_reload(reason)


def preview_url() -> Optional[str]:
    """便捷函数：获取当前 URL（未启动返回 None）。"""
    srv = _preview_instance
    if srv is None or not srv.is_running():
        return None
    return srv.url()
