"""Local API routes; transport and engine lifecycle live in separate modules."""

from urllib.parse import unquote, urlsplit

from .http_protocol import HTTPError


class SidecarService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.cfg = runtime.cfg

    def config(self):
        cfg = self.cfg
        custom = cfg.get("custom_models", [])
        models = ([{"name": m["name"], "label": m["name"], "context_1m": m["context_1m"]}
                   for m in custom] if custom else cfg.get_available_models())
        return {
            "model": cfg.model, "base_url": cfg.base_url, "reasoning_effort": cfg.reasoning_effort,
            "permission_mode": cfg.permission_mode, "chat_mode": cfg.get("chat_mode", "normal"),
            "language": cfg.language, "theme": cfg.theme, "nickname": cfg.nickname,
            "balance_cny": cfg.get("balance_cny", 0), "cache_hit_rate": cfg.get("cache_hit_rate", 0),
            "api_key_exists": bool(cfg.api_key), "onboarding_completed": cfg.get("onboarding_completed", False),
            "available_models": models, "custom_models": custom,
            "last_project": cfg.get("last_project", "demo"),
        }

    def update_config(self, body):
        enums = {"reasoning_effort": {"high", "max"}, "permission_mode": {"reviewer", "coder", "full"},
                 "chat_mode": {"normal", "plan", "ultra"}, "language": {"zh", "en"},
                 "theme": {"light", "dark", "system"}}
        allowed = set(enums) | {"model", "nickname", "base_url", "api_key", "onboarding_completed", "custom_models"}
        if set(body) - allowed:
            raise HTTPError(400, "存在不支持的配置字段")
        values = {}
        for key, value in body.items():
            if key in enums:
                if not isinstance(value, str) or value not in enums[key]:
                    raise HTTPError(400, f"无效配置: {key}")
            elif key == "onboarding_completed":
                if type(value) is not bool:
                    raise HTTPError(400, "onboarding_completed 必须是布尔值")
            elif key == "custom_models":
                if not isinstance(value, list) or len(value) > 2:
                    raise HTTPError(400, "custom_models 最多包含两个模型")
                models = []
                for item in value:
                    if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
                        raise HTTPError(400, "无效自定义模型")
                    context = item.get("context_1m", item.get("context1m", False))
                    if type(context) is not bool:
                        raise HTTPError(400, "context_1m 必须是布尔值")
                    models.append({"name": item["name"].strip(), "context_1m": context})
                if len({m["name"] for m in models}) != len(models):
                    raise HTTPError(400, "自定义模型名重复")
                value = models
            else:
                if not isinstance(value, str) or len(value) > 4096:
                    raise HTTPError(400, f"无效配置: {key}")
                value = value.strip()
                if key in {"model", "base_url"} and not value:
                    raise HTTPError(400, f"{key} 不能为空")
                if key == "api_key" and value == "********":
                    raise HTTPError(400, "不能保存脱敏占位符")
                if key == "base_url":
                    parsed = urlsplit(value)
                    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                            or parsed.username or parsed.password or parsed.query or parsed.fragment):
                        raise HTTPError(400, "base_url 必须是有效的 HTTP(S) 地址")
                    try:
                        parsed.port
                    except ValueError as exc:
                        raise HTTPError(400, "无效 base_url 端口") from exc
                    value = value.rstrip("/")
            values[key] = value
        with self.runtime.lock:
            if set(values) - {"language", "theme", "nickname"}:
                self.runtime.require_idle()
            self.cfg.update_settings(values)
        return self.config()

    @staticmethod
    def require_method(request, *methods):
        if request.method not in methods:
            raise HTTPError(405, "此端点不支持该方法")

    def route(self, request):
        rt, path = self.runtime, request.path
        if path == "/api/health":
            self.require_method(request, "GET")
            return {"status": "ok", "service": "dscode-sidecar", "version": "2.0.1-0712-NEXT",
                    "model": self.cfg.model, "onboarding_completed": self.cfg.get("onboarding_completed", False)}
        if path == "/api/config":
            self.require_method(request, "GET", "PUT")
            return self.update_config(request.json()) if request.method == "PUT" else self.config()
        if path == "/api/projects":
            self.require_method(request, "GET", "POST")
            if request.method == "GET":
                with rt.lock:
                    return {"projects": rt.projects.list_project_infos()}
            body = request.json()
            with rt.lock:
                rt.require_idle()
                name = rt.canonical(body.get("name", ""))
                if name in rt.projects.list_projects():
                    raise HTTPError(409, "项目已存在")
                display = body.get("display_name", body.get("name", name))
                if not isinstance(display, str):
                    raise HTTPError(400, "无效显示名")
                sb = rt.projects.create(name, display_name=display.strip())
                if sb is None:
                    raise HTTPError(500, "创建项目失败")
                return {"name": sb.name, "display_name": sb.get_display_name(), "ok": True}
        if path.startswith("/api/projects/"):
            rest = path[len("/api/projects/"):]
            history = rest.endswith("/history")
            name = unquote(rest[:-len("/history")] if history else rest)
            self.require_method(request, "GET" if history else "DELETE")
            with rt.lock:
                name = rt.canonical(name)
                sb = rt.projects.get(name)
                if sb is None:
                    raise HTTPError(404, "项目不存在")
                if history:
                    from src.sandbox.history import ConversationHistory
                    if rt.engine.sandbox is not None and rt.engine.sandbox.name == name:
                        h = rt.engine.history
                    else:
                        h = ConversationHistory(sb.history_dir)
                    return {"name": name, "messages": h.snapshot()}
                rt.require_idle()
                if rt.engine.sandbox is not None and rt.engine.sandbox.name == name:
                    rt.engine.set_project(None)
                if not rt.projects.delete(name):
                    raise HTTPError(500, "删除项目失败")
                return {"deleted": name}
        if path == "/api/balance":
            self.require_method(request, "GET")
            return rt.engine.api.check_balance()
        if path == "/api/models":
            self.require_method(request, "GET")
            custom = self.cfg.get("custom_models", [])
            if custom:
                return {"ok": True, "models": [{**m, "id": m["name"], "label": m["name"]} for m in custom]}
            return rt.engine.api.list_models()
        if path == "/api/chat/send":
            self.require_method(request, "POST")
            body = request.json()
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                raise HTTPError(400, "text 不能为空")
            run = rt.start(body.get("project", "demo"), text)
            return {"ok": True, "run_id": run.id, "project": run.project}
        if path == "/api/chat/stop":
            self.require_method(request, "POST")
            body = request.json() if request.body else {}
            return {"stopped": rt.stop(body.get("run_id"))}
        if path == "/api/chat/confirm":
            self.require_method(request, "POST")
            body = request.json()
            kind, value = body.get("kind"), body.get("value")
            if not isinstance(kind, str) or kind not in {"command", "edit", "answer"}:
                raise HTTPError(400, "无效确认类型")
            if (kind == "answer" and not isinstance(value, str)) or (kind != "answer" and type(value) is not bool):
                raise HTTPError(400, "无效确认值")
            rt.submit(body.get("run_id"), kind, body.get("request_id"), value)
            return {"ok": True}
        if path == "/api/chat/plan":
            self.require_method(request, "POST")
            body = request.json()
            if type(body.get("confirmed")) is not bool:
                raise HTTPError(400, "confirmed 必须是布尔值")
            with rt.lock:
                rt.require_idle()
                project = rt.canonical(body.get("project", "demo"))
                if (rt.engine.sandbox is None or rt.engine.sandbox.name != project
                        or not rt.engine.get_pending_plan()):
                    raise HTTPError(409, "当前项目没有待确认计划")
                if not body["confirmed"]:
                    rt.engine.reject_plan()
                    return {"ok": True}
                run = rt.start(project, "", continue_plan=True)
                return {"ok": True, "run_id": run.id, "project": run.project}
        raise HTTPError(404, "未知路径")
