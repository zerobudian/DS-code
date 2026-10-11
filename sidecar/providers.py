"""Provider management without exposing or persisting masked credentials."""

from .http_protocol import HTTPError


class ProviderService:
    def __init__(self, runtime, validate):
        self.runtime, self.cfg, self.validate = runtime, runtime.cfg, validate

    def public(self):
        providers = self.cfg.get("providers", [])
        return {"active": self.cfg.active_provider_index, "providers": [
            {"index": i, "name": item.get("name", f"Provider {i + 1}"),
             "base_url": item.get("base_url", ""), "model": item.get("model", ""),
             "api_key_exists": bool(item.get("api_key"))}
            for i, item in enumerate(providers) if isinstance(item, dict)]}

    def route(self, request):
        with self.runtime.lock:
            if request.path == "/api/providers" and request.method == "GET":
                return self.public()
            self.runtime.require_idle()
            providers = self.cfg.get("providers", [])
            if request.path == "/api/providers":
                if request.method != "POST": raise HTTPError(405, "不支持该方法")
                body = request.json()
                name = body.pop("name", "")
                if not isinstance(name, str) or not name.strip() or len(name) > 128:
                    raise HTTPError(400, "无效供应商名称")
                allowed = {"api_key", "base_url", "model", "custom_models", "provider_capabilities", "context_tokens"}
                if set(body) - allowed: raise HTTPError(400, "无效供应商字段")
                values = self.validate(body)
                if not values.get("api_key") or not values.get("base_url") or not values.get("model"):
                    raise HTTPError(400, "供应商需要 API Key、Base URL 和模型名")
                if not providers and self.cfg.api_key:
                    providers.append({"name": "Default", "api_key": self.cfg.api_key,
                        "base_url": self.cfg.base_url, "model": self.cfg.model,
                        "custom_models": self.cfg.get("custom_models", []),
                        "provider_capabilities": self.cfg.get("provider_capabilities", {}),
                        "context_tokens": self.cfg.get("context_tokens", 128000)})
                if len(providers) >= 20: raise HTTPError(400, "最多 20 个供应商")
                if any(item.get("name") == name.strip() for item in providers):
                    raise HTTPError(409, "供应商名称重复")
                providers.append({"name": name.strip(), **values})
                self.cfg.update_settings({"providers": providers})
                return self.public()
            try:
                index = int(request.path[len("/api/providers/"):])
            except ValueError as exc:
                raise HTTPError(404, "供应商不存在") from exc
            if not 0 <= index < len(providers): raise HTTPError(404, "供应商不存在")
            active = self.cfg.active_provider_index
            if request.method == "PUT":
                if request.json() != {"active": True}: raise HTTPError(400, "切换需要 active=true")
                active = index
            elif request.method == "DELETE":
                if len(providers) == 1: raise HTTPError(409, "至少保留一个供应商")
                providers.pop(index)
                active = max(0, active - int(index <= active))
            else:
                raise HTTPError(405, "不支持该方法")
            selected = providers[active]
            self.cfg.update_settings({"providers": providers, "active_provider_index": active,
                "api_key": selected.get("api_key", ""), "base_url": selected.get("base_url", ""),
                "model": selected.get("model", ""), "custom_models": selected.get("custom_models", []),
                "provider_capabilities": selected.get("provider_capabilities", {}),
                "context_tokens": selected.get("context_tokens", 128000)})
            return self.public()
