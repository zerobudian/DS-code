# -*- coding: utf-8 -*-
"""DScode V2 — Python Sidecar (纯 stdlib，零依赖)

用法:
    python3 sidecar/sidecar.py

本地启动后，Electron 渲染进程通过 HTTP/SSE 与 sidecar 通信。
所有 Python 依赖仅在现有环境中使用（无额外安装）。
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs

SIDECAR_DIR = Path(__file__).resolve().parent
DSCODE_V2_ROOT = SIDECAR_DIR.parent
_OLD_DSC_ROOT = DSCODE_V2_ROOT / "engine"
os.chdir(str(DSCODE_V2_ROOT))
for _p in (DSCODE_V2_ROOT, _OLD_DSC_ROOT):
    _ps = str(_p)
    if _ps not in sys.path and Path(_ps).exists():
        sys.path.insert(0, _ps)

from src.core.logging_config import setup_logging
setup_logging()

logger = logging.getLogger("dscode-sidecar")


# ── SSE 事件流（从现有 ChatEngine 驱动）─────────────────
_HAS_EVENTS = threading.Event()
_EVENT_LOCK = threading.Lock()
_EVENT_BUF = []


def _push_event(etype: str, data):
    with _EVENT_LOCK:
        _EVENT_BUF.append(json.dumps({"type": etype, "data": data}, ensure_ascii=False, default=str))
    _HAS_EVENTS.set()


def _pop_events():
    with _EVENT_LOCK:
        if _EVENT_BUF:
            events = _EVENT_BUF[:]
            _EVENT_BUF.clear()
            _HAS_EVENTS.clear()
            return events
    return []


def init_engine():
    """初始化全局引擎单例并注入回调。"""
    from src.core.config import Config
    from src.core.chat_engine import ChatEngine
    from src.sandbox.project_manager import ProjectManager

    cfg = Config()
    print(f"[Engine] Config 加载: model={cfg.model}, api_key存在={bool(cfg.api_key)}")
    projects = ProjectManager()
    print(f"[Engine] ProjectManager 就绪, 项目列表={projects.list_projects()}")
    engine = ChatEngine()
    engine.set_mode("normal")

    # 注入回调 → _push_event
    engine.on_content_delta = lambda d: _push_event("content", str(d))
    engine.on_reasoning_delta = lambda d: _push_event("reasoning", str(d))
    engine.on_new_turn = lambda t: _push_event("new_turn", {"turn": t})
    engine.on_finished = lambda c, h, cached, total: _push_event("done", {
        "cost": round(c, 6) if c else 0,
        "cache_hit_rate": round(h, 2) if h else 0,
        "cached_tokens": cached,
        "total_tokens": total,
    })
    engine.on_error = lambda code, msg: _push_event("srv_error", {"code": code, "message": msg})
    engine.on_status_update = lambda m: _push_event("status", str(m))
    engine.on_tool_call_detected = lambda c: _push_event("tool_call", _safe_dict(c))
    engine.on_tool_result = lambda n, r: _push_event("tool_result", {"name": n, "result": str(r)[:500]})
    engine.on_plan_ready = lambda text: _push_event("plan_ready", str(text))
    engine.on_confirm_command = lambda c: _push_event("confirm_command", _safe_dict(c))
    engine.on_confirm_edit = lambda fp, diff: _push_event("confirm_edit", {"filepath": fp, "diff": str(diff)[:2000]})
    engine.on_assistant_message = lambda c, r, tc: _push_event("assistant_message", {
        "content": str(c)[:500], "reasoning": str(r)[:500], "tool_count": len(tc) if tc else 0
    })

    print(f"[Engine] 回调已注入, engine.history={engine.history}")
    return cfg, projects, engine


def _safe_dict(obj):
    """安全序列化任意对象为 dict/str。"""
    if isinstance(obj, dict):
        return {k: _safe_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_dict(i) for i in obj]
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    return str(obj)[:1000]


# ── HTTP 路由处理 ─────────────────────────────────────
def _json_response(data, status=200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return (
        f"HTTP/1.1 {status} OK\r\n"
        f"Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Access-Control-Allow-Origin: *\r\n"
        f"\r\n"
    ).encode("utf-8") + body


def _sse_response():
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream\r\n"
        b"Cache-Control: no-cache\r\n"
        b"Connection: keep-alive\r\n"
        b"X-Accel-Buffering: no\r\n"
        b"Access-Control-Allow-Origin: *\r\n"
        b"\r\n"
    )


async def handle_request(reader, writer):
    """单连接处理器。"""
    try:
        data = await asyncio.wait_for(reader.read(65536), timeout=5)
    except (asyncio.TimeoutError, ConnectionError):
        writer.close()
        return

    request = data.decode("utf-8", errors="replace")
    lines = request.split("\r\n")
    if not lines:
        writer.close()
        return

    # 空行/非HTTP行直接忽略
    first = lines[0].strip()
    if not first or first.startswith("{"):
        writer.close()
        return

    try:
        method, raw_path, _ = first.split(" ", 2)
    except ValueError:
        writer.close()
        return

    parsed = urlparse(raw_path)
    path = parsed.path          # 纯路径，不含 query string
    qs = parse_qs(parsed.query)

    # 读取 body
    body_str = ""
    if method in ("POST", "PUT", "DELETE"):
        try:
            body_idx = request.index("\r\n\r\n") + 4
            body_str = request[body_idx:]
        except ValueError:
            pass

    # ── CORS preflight ──
    if method == "OPTIONS":
        writer.write(
            b"HTTP/1.1 204 No Content\r\n"
            b"Access-Control-Allow-Origin: *\r\n"
            b"Access-Control-Allow-Methods: GET,POST,PUT,DELETE,OPTIONS\r\n"
            b"Access-Control-Allow-Headers: *\r\n"
            b"\r\n"
        )
        await writer.drain()
        writer.close()
        return

    # ── 路由 ──
    try:
        print(f"[HTTP] {method} {path}", flush=True)  # 请求日志
        if path == "/api/health":
            cfg = _cfg()
            resp = _json_response({
                "status": "ok", "model": cfg.model,
                "onboarding_completed": cfg.data.get("onboarding_completed", False),
                "version": "2.0.1-0712-NEXT",
            })
            writer.write(resp)

        elif path == "/api/config":
            if method == "PUT":
                try:
                    body = json.loads(body_str)
                    cfg = _cfg()
                    for key in ("model", "reasoning_effort", "permission_mode", "chat_mode",
                                "language", "theme", "nickname", "base_url", "api_key",
                                "onboarding_completed", "custom_models"):
                        if key in body and body[key] is not None:
                            if key == "model":
                                cfg.model = str(body[key])
                            elif key == "api_key":
                                cfg.api_key = str(body[key])
                            elif key in ("reasoning_effort", "chat_mode", "language", "theme"):
                                cfg.data[key] = str(body[key])
                            elif key == "permission_mode":
                                cfg.permission_mode = str(body[key])
                            elif key == "nickname":
                                cfg.nickname = str(body[key])
                            elif key == "base_url":
                                cfg.base_url = str(body[key])
                            elif key == "onboarding_completed":
                                cfg.data["onboarding_completed"] = bool(body[key])
                            elif key == "custom_models":
                                if isinstance(body[key], list):
                                    valid = []
                                    for item in body[key]:
                                        if isinstance(item, dict) and item.get("name"):
                                            valid.append({
                                                "name": str(item["name"]).strip(),
                                                "context_1m": bool(item.get("context_1m", False)),
                                            })
                                    cfg.data["custom_models"] = valid
                    cfg.save()
                except (json.JSONDecodeError, Exception) as e:
                    writer.write(_json_response({"error": str(e)}, 400))
                    await writer.drain()
                    writer.close()
                    return
            cfg = _cfg()
            writer.write(_json_response({
                "model": cfg.model,
                "base_url": cfg.base_url,
                "reasoning_effort": cfg.reasoning_effort,
                "permission_mode": cfg.permission_mode,
                "chat_mode": cfg.data.get("chat_mode", "normal"),
                "language": cfg.language,
                "theme": cfg.data.get("theme", "light"),
                "nickname": cfg.nickname,
                "balance_cny": cfg.data.get("balance_cny", 0.0),
                "cache_hit_rate": cfg.data.get("cache_hit_rate", 0.0),
                "available_models": cfg.get_available_models() or [
                    {"name": "deepseek-v4-pro", "label": "V4 Pro", "primary": True},
                    {"name": "deepseek-v4-flash", "label": "V4 Flash", "primary": False},
                ],
                "api_key_exists": bool(cfg.api_key),
                "onboarding_completed": cfg.data.get("onboarding_completed", False),
                "custom_models": cfg.data.get("custom_models", []),
            }))

        elif path == "/api/projects":
            if method == "GET":
                _p = _projects()
                infos = _p.list_project_infos()
                result = [{"name": i["name"], "display_name": i["display_name"]} for i in infos]
                writer.write(_json_response({"projects": result}))
            elif method == "POST":
                try:
                    body = json.loads(body_str)
                    name = (body.get("name") or "").strip()
                    if not name:
                        writer.write(_json_response({"error": "项目名不能为空"}, 400))
                    else:
                        display_name = (body.get("display_name") or name).strip()
                        sb = _projects().create(name, display_name=display_name)
                        writer.write(_json_response(
                            {"name": name, "display_name": display_name,
                             "ok": sb is not None}))
                except json.JSONDecodeError:
                    writer.write(_json_response({"error": "无效 JSON"}, 400))
        elif path.startswith("/api/projects/") and method == "DELETE":
            name = path.split("/api/projects/")[-1]
            if name and name.strip():
                try:
                    _projects().delete(name.strip())
                except Exception:
                    pass
                writer.write(_json_response({"deleted": name.strip()}))

        elif path == "/api/balance":
            info = {"available_balance": 0, "total_balance": 0, "ok": False}
            cfg = _cfg()
            api_key = cfg.api_key
            if not api_key or api_key == "your_api_key_here":
                info["error"] = "未配置 API Key"
                writer.write(_json_response(info))
                await writer.drain()
                writer.close()
                return
            base = cfg.base_url or "https://api.deepseek.com"
            try:
                import http.client
                pu = urlparse(base)
                host = pu.hostname or "api.deepseek.com"
                conn = http.client.HTTPSConnection(host, timeout=10)
                try:
                    conn.request("GET", "/user/balance", "",
                                 {"Accept": "application/json",
                                  "Authorization": f"Bearer {api_key}"})
                    resp = conn.getresponse()
                    body = resp.read().decode("utf-8")
                    if resp.status == 200:
                        data = json.loads(body)
                        infos = data.get("balance_infos", [])
                        total = 0.0
                        for bi in infos:
                            try:
                                total += float(bi.get("total_balance", "0") or "0")
                            except (ValueError, TypeError):
                                pass
                        info["available_balance"] = total
                        info["total_balance"] = total
                        info["ok"] = True
                        info["is_available"] = data.get("is_available", False)
                    else:
                        info["error"] = f"status {resp.status}"
                finally:
                    conn.close()
            except Exception as e:
                info["error"] = str(e)
            writer.write(_json_response(info))

        elif path == "/api/models":
            # 从 DeepSeek API 动态获取模型列表（仅官方 API 生效）
            cfg = _cfg()
            api_key = cfg.api_key
            if not api_key or api_key == "your_api_key_here":
                writer.write(_json_response({"models": [], "error": "未配置 API Key"}))
                await writer.drain()
                writer.close()
                return
            base = cfg.base_url or "https://api.deepseek.com"
            try:
                import http.client
                pu = urlparse(base)
                host = pu.hostname or "api.deepseek.com"
                conn = http.client.HTTPSConnection(host, timeout=10)
                try:
                    conn.request("GET", "/models", "",
                                 {"Accept": "application/json",
                                  "Authorization": f"Bearer {api_key}"})
                    resp = conn.getresponse()
                    body = resp.read().decode("utf-8")
                    if resp.status == 200:
                        data = json.loads(body)
                        models = []
                        for m in data.get("data", []):
                            mid = m.get("id", "")
                            if not mid:
                                continue
                            # 从 model id 推断 1M context 支持
                            context_1m = "chat" in mid.lower() or "v4" in mid.lower() or "reasoner" in mid.lower()
                            models.append({
                                "id": mid,
                                "name": mid,
                                "label": mid.replace("deepseek-", "").replace("v4-", "V4 "),
                                "owned_by": m.get("owned_by", ""),
                                "context_1m": context_1m,
                            })
                        writer.write(_json_response({"models": models, "ok": True}))
                    else:
                        writer.write(_json_response({"models": [], "ok": False, "error": f"status {resp.status}"}))
                finally:
                    conn.close()
            except Exception as e:
                writer.write(_json_response({"models": [], "ok": False, "error": str(e)}))

        elif path == "/api/chat/send":
            # POST → 后台线程调用 engine.send_message，客户端连 /api/chat/stream 收 SSE
            try:
                body = json.loads(body_str)
                text = body.get("text", "")
                project = body.get("project", "demo")
                if not text:
                    writer.write(_json_response({"error": "text 不能为空"}, 400))
                else:
                    _start_chat(project, text)
                    writer.write(_json_response({"ok": True, "project": project}))
            except json.JSONDecodeError:
                writer.write(_json_response({"error": "无效 JSON"}, 400))

        elif path == "/api/chat/stream":
            # SSE — 阻塞式持续写事件直到 done/error
            text = qs.get("text", [""])[0]
            project = qs.get("project", ["demo"])[0]
            print(f"[SSE] 收到流请求: project={project}, text={text[:40]}...", flush=True)
            if not text:
                writer.write(_json_response({"error": "text 不能为空"}, 400))
                await writer.drain()
                writer.close()
                return

            # 启动引擎（后台线程）
            _start_chat(project, text)

            # SSE header
            writer.write(_sse_response())
            await writer.drain()
            print(f"[SSE] header 已发送", flush=True)

            deadline = time.monotonic() + 300
            poll_count = 0
            try:
                while time.monotonic() < deadline:
                    events = _pop_events()
                    if events:
                        poll_count += 1
                        if poll_count <= 3:
                            for evt_json in events:
                                evt = json.loads(evt_json)
                                print(f"[SSE] 事件: type={evt['type']}, data={str(evt['data'])[:60]}", flush=True)
                        should_stop = False
                        for evt_json in events:
                            evt = json.loads(evt_json)
                            etype = evt["type"]
                            edata = evt["data"]
                            payload = json.dumps(edata, ensure_ascii=False, default=str)
                            line = f"event: {etype}\ndata: {payload}\n\n"
                            try:
                                writer.write(line.encode("utf-8"))
                            except ConnectionError:
                                return
                            if etype in ("done", "error", "srv_error"):
                                should_stop = True
                        await writer.drain()
                        if should_stop:
                            writer.close()
                            print(f"[SSE] 流结束 (done/error)", flush=True)
                            return
                    else:
                        if poll_count == 0:
                            pass  # 第一次循环，worker 可能还没启动
                        elif poll_count > 0 and poll_count % 40 == 0:
                            print(f"[SSE] 等待中... (循环{poll_count})", flush=True)
                        await asyncio.sleep(0.05)
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"[SSE] 循环异常: {e}", flush=True)

            # 超时
            timeout_msg = json.dumps({"code": 504, "message": "请求超时"})
            writer.write(f"event: srv_error\ndata: {timeout_msg}\n\n".encode("utf-8"))
            await writer.drain()
            writer.close()
            return

        elif path == "/api/chat/stop":
            try:
                _engine().stop()
            except Exception:
                pass
            writer.write(_json_response({"stopped": True}))

        else:
            writer.write(_json_response({"error": f"未知路径: {path}"}, 404))

    except Exception as e:
        import traceback
        traceback.print_exc()
        writer.write(_json_response({"error": str(e)}, 500))

    try:
        await writer.drain()
    except Exception:
        pass
    writer.close()


# ── 单例 getter ───────────────────────────────────────
_ENGINE = None
_CFG = None
_PROJECTS = None


def _init():
    global _ENGINE, _CFG, _PROJECTS
    if _ENGINE is None:
        _CFG, _PROJECTS, _ENGINE = init_engine()
    return _CFG, _PROJECTS, _ENGINE


def _cfg():
    _, __, ___ = _init()
    return _CFG


def _projects():
    _, p, __ = _init()
    return p


def _engine():
    _, __, e = _init()
    return e


def _start_chat(project: str, text: str):
    """在后台线程中调 engine.send_message。"""
    print(f"[_start_chat] 开始: project={project}, text={text[:40]}...", flush=True)
    _init()
    print(f"[_start_chat] _init 完成", flush=True)

    # 清空事件缓冲区
    _pop_events()
    print(f"[_start_chat] 事件缓冲已清空", flush=True)

    def _worker():
        try:
            # 规范化项目名：统一使用 canonical 名称，避免与目录名/磁盘名比较不一致
            canonical = _PROJECTS._safe_project_name(project) or project
            print(f"[ChatWorker] 线程启动: project={project}, canonical={canonical}, text={text[:40]}...", flush=True)
            existing = _PROJECTS.list_projects()
            print(f"[ChatWorker] 现有项目: {existing}", flush=True)
            if canonical not in existing:
                print(f"[ChatWorker] 创建新项目: {canonical}", flush=True)
                sb = _PROJECTS.create(canonical, display_name=project.capitalize())
                if sb is None:
                    print(f"[ChatWorker] 创建项目失败!", flush=True)
                    _push_event("srv_error", {"code": 500, "message": "创建项目失败"})
                    _push_event("done", {"cost": 0, "cache_hit_rate": 0, "cached_tokens": 0, "total_tokens": 0})
                    return
                print(f"[ChatWorker] 项目创建成功: {sb}", flush=True)
            else:
                sb = _PROJECTS.get(canonical)
                print(f"[ChatWorker] 已存在项目: {sb}", flush=True)

            if sb is not None and (_ENGINE.sandbox is None or _ENGINE.sandbox.name != canonical):
                print(f"[ChatWorker] 设置 sandbox: {project}", flush=True)
                _ENGINE.set_project(sb)
                print(f"[ChatWorker] sandbox 设置完成", flush=True)
            elif sb is None:
                print(f"[ChatWorker] 项目不存在!", flush=True)
                _push_event("srv_error", {"code": 500, "message": f"项目 {project} 不存在"})
                _push_event("done", {"cost": 0, "cache_hit_rate": 0, "cached_tokens": 0, "total_tokens": 0})
                return
            else:
                print(f"[ChatWorker] sandbox 已存在且项目匹配, 跳过 set_project", flush=True)

            print(f"[ChatWorker] 调用 engine.send_message...", flush=True)
            _ENGINE.send_message(text)
            print(f"[ChatWorker] send_message 完成", flush=True)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[ChatWorker] 异常: {e}", flush=True)
            _push_event("srv_error", {"code": 500, "message": str(e)})
            _push_event("done", {"cost": 0, "cache_hit_rate": 0, "cached_tokens": 0, "total_tokens": 0})

    print(f"[_start_chat] 启动后台线程...", flush=True)
    threading.Thread(target=_worker, daemon=True, name="sidecar-chat").start()
    print(f"[_start_chat] 后台线程已启动", flush=True)


# ── 主入口 ────────────────────────────────────────────
def main(host="127.0.0.1", port=8765):
    # 命令行 --port 覆盖
    if "--port" in sys.argv:
        try:
            idx = sys.argv.index("--port")
            port = int(sys.argv[idx + 1])
        except Exception:
            pass

    print(f"[DScode Sidecar] 启动 → http://{host}:{port}")
    print(f"[DScode Sidecar] SSE 端点: http://{host}:{port}/api/chat/stream?project=demo&text=hello")

    _init()
    print(f"[DScode Sidecar] 模型: {_CFG.model}, 项目: {_PROJECTS.list_projects()}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    server = loop.run_until_complete(
        asyncio.start_server(handle_request, host, port, reuse_address=True)
    )

    print(f"[DScode Sidecar] 已就绪，等待连接...")

    def _shutdown():
        print("\n[Sidecar] 正在关闭...")
        server.close()
        loop.stop()

    try:
        loop.add_signal_handler(signal.SIGINT, _shutdown)
        loop.add_signal_handler(signal.SIGTERM, _shutdown)
    except NotImplementedError:
        # Windows 不支持 add_signal_handler
        pass

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        _shutdown()
    finally:
        loop.close()


if __name__ == "__main__":
    main()
