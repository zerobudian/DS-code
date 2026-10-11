# -*- coding: utf-8 -*-
"""DeepSeek API 封装层 (改进版)

- 流式对话 (stream=True)
- 思考模式 (reasoning)
- 可取消 (on_cancel_check)
- 图片理解 (vision / 多模态)
- 余额 / 服务器状态检查
- 错误处理 (401/402/500/网络)
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import json
import os
import re
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from typing import Any, List, Dict, Optional, Callable, TYPE_CHECKING

try:
    import httpx
except ImportError:
    httpx = None
try:
    import certifi
except ImportError:
    certifi = None

from src.core.config import Config
from src.core.log_sanitizer import sanitize_for_log


_dns_timeout_lock = threading.Lock()


@contextmanager
def _socket_dns_timeout(seconds: float):
    """C-006: 在 with 块内为 socket 操作（含 DNS 解析）设置全局默认超时。

    注意：socket.setdefaulttimeout 是进程级设置，多线程并发调用会互相覆盖。
    使用锁串行化 save/restore，避免竞态导致全局超时停留在错误值。
    """
    with _dns_timeout_lock:
        old = socket.getdefaulttimeout()
        socket.setdefaulttimeout(seconds)
        try:
            yield
        finally:
            socket.setdefaulttimeout(old)

if TYPE_CHECKING:
    from openai import OpenAI


# 延迟加载 openai 重型依赖：仅在真正需要调用 API 时 import，降低启动耗时。
_OpenAIAPIError: Any = None
_OpenAIConnError: Any = None
_OPENAI_ERRORS: tuple = ()


def _ensure_openai() -> None:
    """确保 openai 模块已加载，并缓存异常类型元组。"""
    global _OpenAIAPIError, _OpenAIConnError, _OPENAI_ERRORS
    if _OPENAI_ERRORS:
        return
    from openai import OpenAI as _OpenAI
    from openai import APIError as _Err
    from openai import APIConnectionError as _ConnErr
    # 为了让类型标注和构造 client 都能工作，把 OpenAI 类挂到模块全局
    globals()["OpenAI"] = _OpenAI
    _OpenAIAPIError = _Err
    _OpenAIConnError = _ConnErr
    _OPENAI_ERRORS = (_Err, _ConnErr)


class APIError(Exception):
    def __init__(self, code: int, message: str, need_retry: bool = True):
        self.code = code
        self.message = message
        self.need_retry = need_retry
        super().__init__(f"[{code}] {message}")


class DeepSeekAPI:
    MODELS = {
        "deepseek-v4-flash": "DeepSeek V4 Flash (快速)",
        "deepseek-v4-pro":   "DeepSeek V4 Pro (深度推理)",
    }

    def __init__(self):
        self.cfg = Config()
        self._client: Optional[OpenAI] = None
        self._client_key: str = ""
        self._client_base_url: str = ""
        self._http_session: Optional[Any] = None
        self._http_session_key: str = ""  # 用于感知 api_key 变化后重建 session
        self._session_lock = threading.RLock()  # 保护 _client / _http_session 重建（RLock 允许 _get_client 内部调用 _get_http_session）

    def _get_ssl_verify(self) -> Any:
        """返回 SSL 验证参数：优先使用 certifi 提供的 CA 证书。

        当 certifi 不可用时显式记录 warning，而不是静默 fallback 到系统默认 CA（M-004）。
        """
        if os.environ.get("SSL_CERT_FILE"):
            return os.environ["SSL_CERT_FILE"]
        if certifi is not None:
            return certifi.where()
        logger.warning(
            "certifi 不可用，SSL/TLS 验证将回退到系统默认 CA 证书。"
            "在代理或受限网络环境中，这可能降低对中间人攻击的防护能力。"
        )
        return True

    def _get_http_session(self) -> Optional[Any]:
        """获取复用的 HTTP 会话（httpx.Client），统一 SSL/TLS 配置。

        复用 session 可避免重复创建无 verify 的短连接，并保证余额查询与
        流式对话使用一致的 SSL 策略（CWE-295 / MEDIUM-001）。
        线程安全：使用 _session_lock 防止多线程并发重建导致 client 被提前关闭。
        """
        with self._session_lock:
            key = self.cfg.api_key
            if (self._http_session is not None
                    and self._http_session_key == key):
                return self._http_session
            # api_key 变化或首次创建：重建 session
            if self._http_session is not None:
                try:
                    self._http_session.close()
                except Exception as _exc:
                    logger.debug("关闭旧 http session 异常: %s", _exc, exc_info=True)
                self._http_session = None
            if httpx is None:
                return None
            self._http_session = self._make_http_client()
            self._http_session_key = key
            return self._http_session

    def _make_http_client(self):
        if httpx is None:
            return None
        verify = self._get_ssl_verify()
        if isinstance(verify, str):
            verify = ssl.create_default_context(cafile=verify)
        return httpx.Client(verify=verify, timeout=self._make_httpx_timeout())

    def _get_client(self) -> Optional["OpenAI"]:
        _ensure_openai()
        with self._session_lock:
            key = self.cfg.api_key
            if not key or key == "your_api_key_here":
                return None
            base_url = self._base_url()
            if (self._client is not None
                    and self._client_key == key
                    and self._client_base_url == base_url):
                return self._client
            # 使用 httpx.Timeout 设置连接/读取超时，避免弱网/服务端无响应时永久阻塞
            timeout = self._make_httpx_timeout()
            # 显式传入 http_client 并强制开启 SSL 证书验证（CWE-295 / MEDIUM-001）
            # Each OpenAI client owns its transport. Closing an old client must
            # never close the diagnostics session or its replacement client.
            http_client = self._make_http_client()
            old_client = self._client
            self._client = OpenAI(
                api_key=key, base_url=base_url,
                timeout=timeout, max_retries=0,
                http_client=http_client,
            )
            self._client_key = key
            self._client_base_url = base_url
            # 关闭旧 client 避免资源泄漏（在锁内安全完成替换）
            if old_client is not None and old_client is not self._client:
                try:
                    old_client.close()
                except Exception as _exc:
                    logger.debug("关闭旧 OpenAI client 异常: %s", _exc, exc_info=True)
            return self._client

    def close(self):
        """显式关闭复用的 HTTP session 与 OpenAI client。"""
        if self._client is not None:
            try:
                self._client.close()
            except Exception as _exc:
                logger.debug("关闭 OpenAI client 异常: %s", _exc, exc_info=True)
            self._client = None
            self._client_key = ""
            self._client_base_url = ""
        if self._http_session is not None:
            try:
                self._http_session.close()
            except Exception as _exc:
                logger.debug("关闭 http session 异常: %s", _exc, exc_info=True)
            self._http_session = None
            self._http_session_key = ""

    # ----- 状态 -----
    # 统一超时时间（读/写分离）：防止弱网环境下长期阻塞 UI
    _HTTP_TIMEOUT_CONNECT = 10
    _HTTP_TIMEOUT_READ = 30

    @staticmethod
    def _to_openai_tools(tool_descriptions):
        """将 V1 tool_descriptions 转换为 OpenAI function-calling 格式。"""
        tools = []
        for t in tool_descriptions:
            if not isinstance(t, dict):
                continue
            name = t.get("name", "")
            desc = t.get("description", "")
            fields = t.get("fields") or []
            properties = {}
            required = []
            if isinstance(fields, list):
                for f in fields:
                    if not isinstance(f, dict):
                        continue
                    key = f.get("key", "")
                    if not key:
                        continue
                    # 确保 description 永远是 string——某些字段的 example 是 JSON object
                    ex = f.get("example", "")
                    if not isinstance(ex, str):
                        ex = json.dumps(ex, ensure_ascii=False)
                    note = f.get("note", "")
                    if not isinstance(note, str):
                        note = str(note)
                    # 确保 type 是有效的 JSON Schema type
                    raw_type = f.get("type", "string")
                    valid_types = {"string", "number", "integer", "boolean", "object", "array"}
                    if not isinstance(raw_type, str) or raw_type not in valid_types:
                        raw_type = "string"
                    properties[key] = {
                        "type": raw_type,
                        "description": str(ex or note or key),
                    }
                    if f.get("required"):
                        required.append(key)
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc[:1024] if desc else name,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": [r for r in required if r in properties],
                    },
                },
            })
        return tools

    @classmethod
    def _make_httpx_timeout(cls) -> Any:
        """H-010: 生成统一的 httpx Timeout（兼容要求显式设置四个参数的版本）。"""
        if httpx is None:
            return None
        return httpx.Timeout(
            connect=cls._HTTP_TIMEOUT_CONNECT,
            read=cls._HTTP_TIMEOUT_READ,
            write=cls._HTTP_TIMEOUT_READ,
            pool=cls._HTTP_TIMEOUT_READ,
        )

    # 流式对话重试策略：指数退避，最大重试 3 次
    _STREAM_MAX_RETRIES = 3
    _STREAM_RETRY_BACKOFF_BASE = 1.0
    _STREAM_RETRY_BACKOFF_MAX = 16.0

    def _base_url(self) -> str:
        try:
            active = self.cfg.active_provider
            url = active.get("base_url") if isinstance(active, dict) else None
            if not url:
                url = self.cfg.get("base_url", "https://api.deepseek.com")
            url = str(url or "").strip().rstrip("/")
            return url or "https://api.deepseek.com"
        except Exception:
            return "https://api.deepseek.com"

    def check_balance(self) -> Dict:
        key = self.cfg.api_key
        if not key or key == "your_api_key_here":
            return {"available_balance": 0, "ok": False}
        url = f"{self._base_url()}/user/balance"
        headers = {"Authorization": f"Bearer {key}"}
        timeout = (self._HTTP_TIMEOUT_CONNECT, self._HTTP_TIMEOUT_READ)

        # MEDIUM-001: 复用已配置 SSL 的 http session，避免单独创建无 verify 的 requests。
        session = self._get_http_session()
        if session is not None:
            try:
                # C-006: 用 socket 默认超时把 DNS 解析包含在连接超时内
                with _socket_dns_timeout(self._HTTP_TIMEOUT_CONNECT):
                    # H-010: 统一 httpx 与 openai 客户端超时语义
                    resp = session.get(
                        url, headers=headers,
                        timeout=self._make_httpx_timeout(),
                    )
                try:
                    if resp.status_code == 200:
                        try:
                            data = resp.json()
                        except (ValueError, json.JSONDecodeError):
                            return {"available_balance": 0, "ok": False, "error": "bad_json"}
                        return self._normalize_balance(data)
                    return {
                        "available_balance": 0, "ok": False,
                        "code": resp.status_code,
                        "status_text": self._sanitize_error_message(
                            getattr(resp, "reason_phrase", None)
                            or getattr(resp, "reason", "") or "", max_length=200),
                    }
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass
            except Exception as e:
                return self._balance_error_response(e)

        # httpx 不可用时回退到 requests，但显式开启 SSL 验证
        import requests
        resp = None
        try:
            # C-006: 用 socket 默认超时把 DNS 解析包含在连接超时内
            with _socket_dns_timeout(self._HTTP_TIMEOUT_CONNECT):
                resp = requests.get(
                    url, headers=headers, timeout=timeout,
                    verify=self._get_ssl_verify(),
                )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except (ValueError, json.JSONDecodeError):
                    return {"available_balance": 0, "ok": False, "error": "bad_json"}
                return self._normalize_balance(data)
            return {
                "available_balance": 0, "ok": False,
                "code": resp.status_code,
                "status_text": self._sanitize_error_message(resp.reason or "", max_length=200),
            }
        except Exception as e:
            return self._balance_error_response(e)
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    @staticmethod
    def _normalize_balance(data: Dict) -> Dict:
        infos = data.get("balance_infos", [])
        if infos:
            selected = next((i for i in infos if i.get("currency") == "CNY"), infos[0])
            total = float(selected.get("total_balance", 0) or 0)
            return {"available_balance": total, "total_balance": total, "ok": True,
                    "currency": selected.get("currency", ""), "balance_infos": infos,
                    "is_available": bool(data.get("is_available", False))}
        return {"available_balance": float(data.get("available_balance", 0) or 0),
                "total_balance": float(data.get("total_balance", 0) or 0), "ok": True,
                "currency": data.get("currency", "CNY")}

    def list_models(self) -> Dict:
        """Fetch models using the configured URL, port, path and TLS policy."""
        if not self.cfg.api_key:
            return {"ok": False, "models": [], "error": "未配置 API Key"}
        try:
            session = self._get_http_session()
            if session is None:
                return {"ok": False, "models": [], "error": "请安装 requirements.txt 中的依赖"}
            resp = session.get(f"{self._base_url()}/models",
                               headers={"Authorization": f"Bearer {self.cfg.api_key}"})
            try:
                resp.raise_for_status()
                return {"ok": True, "models": [
                    {"id": m["id"], "name": m["id"], "label": m["id"]}
                    for m in resp.json().get("data", []) if m.get("id")]}
            finally:
                resp.close()
        except Exception as exc:
            return {"ok": False, "models": [], "error": self._sanitize_error_message(str(exc))}

    def _balance_error_response(self, e) -> Dict:
        """统一余额查询异常返回格式（detail 经过脱敏，避免泄漏 API Key）。"""
        detail = self._sanitize_error_message(str(e), max_length=200)
        msg = detail.lower()
        if "timeout" in msg:
            return {"available_balance": 0, "ok": False, "error": "timeout", "detail": detail}
        if "connection" in msg or "network" in msg or "dns" in msg:
            return {"available_balance": 0, "ok": False, "error": "network", "detail": detail}
        return {"available_balance": 0, "ok": False, "error": detail}

    def check_server_status(self) -> Dict:
        url = f"{self._base_url()}/models"
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        timeout = (self._HTTP_TIMEOUT_CONNECT, self._HTTP_TIMEOUT_READ)

        # MEDIUM-001: 复用已配置 SSL 的 http session
        session = self._get_http_session()
        if session is not None:
            try:
                # C-006: 用 socket 默认超时把 DNS 解析包含在连接超时内
                with _socket_dns_timeout(self._HTTP_TIMEOUT_CONNECT):
                    # H-010: 统一 httpx 与 openai 客户端超时语义
                    resp = session.get(
                        url, headers=headers,
                        timeout=self._make_httpx_timeout(),
                    )
                try:
                    return {"ok": resp.status_code == 200, "status": resp.status_code}
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass
            except Exception:
                return {"ok": False, "status": "error"}

        # httpx 不可用时回退到 requests，显式开启 SSL 验证
        import requests
        resp = None
        try:
            # C-006: 用 socket 默认超时把 DNS 解析包含在连接超时内
            with _socket_dns_timeout(self._HTTP_TIMEOUT_CONNECT):
                resp = requests.get(
                    url, headers=headers, timeout=timeout,
                    verify=self._get_ssl_verify(),
                )
            return {"ok": resp.status_code == 200, "status": resp.status_code}
        except requests.exceptions.ConnectionError:
            return {"ok": False, "status": "network"}
        except requests.exceptions.Timeout:
            return {"ok": False, "status": "timeout"}
        except requests.exceptions.RequestException:
            return {"ok": False, "status": "error"}
        except Exception:
            return {"ok": False, "status": "unknown"}
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    # ----- 流式对话 -----
    def chat_stream(self, messages: List[Dict], model: Optional[str] = None,
                    on_text: Optional[Callable[[str], None]] = None,
                    on_reasoning: Optional[Callable[[str], None]] = None,
                    on_cancel_check: Optional[Callable[[], bool]] = None,
                    reasoning_effort: Optional[str] = None,
                    enable_thinking: bool = True,
                    _vision_retry_count: int = 0,
                    _is_degraded_call: bool = False) -> Dict:
        """
        流式对话 (阻塞式调用):
          on_text(delta_str) -> 收到普通文本增量
          on_reasoning(delta_str) -> 收到思考文本增量
          on_cancel_check() -> 返回 True 时立即停止

        返回: {content, reasoning_content, prompt_tokens, completion_tokens,
               prompt_cache_hit_tokens, prompt_cache_miss_tokens, cost_cny, cache_hit_rate}
        """
        # P0-001: 防止图片降级无限递归，超过 2 次重试直接抛错
        if _vision_retry_count >= 2:
            raise APIError(500, "图片理解降级失败，无法继续")

        client = self._get_client()
        if client is None:
            raise APIError(401, "请先在设置中配置 API Key")

        use_model = model or self.cfg.model
        _reasoning_effort = reasoning_effort or self.cfg.get("reasoning_effort", "high")

        last_exception: Optional[APIError] = None
        for attempt in range(1, self._STREAM_MAX_RETRIES + 1):
            # 重试前检查是否已被取消
            if on_cancel_check:
                cancelled = False
                try:
                    cancelled = bool(on_cancel_check())
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                if cancelled:
                    raise APIError(499, "请求已被用户取消", need_retry=False)
            try:
                return self._chat_stream_once(
                    client, use_model, messages, _reasoning_effort,
                    on_text=on_text, on_reasoning=on_reasoning,
                    on_cancel_check=on_cancel_check,
                    enable_thinking=enable_thinking,
                )
            except APIError as e:
                last_exception = e
                # 客户端错误与取消不 retry；其余（网络/超时/5xx/429）允许重试
                no_retry_codes = {400, 401, 403, 404, 405, 422, 499}
                if e.code in no_retry_codes or not e.need_retry or attempt >= self._STREAM_MAX_RETRIES:
                    raise
                # 指数退避：1, 2, 4, ... 最高 16 秒
                backoff = min(
                    self._STREAM_RETRY_BACKOFF_BASE * (2 ** (attempt - 1)),
                    self._STREAM_RETRY_BACKOFF_MAX,
                )
                # 可中断等待：分段 sleep，每段检查取消
                _cancel_event = threading.Event()
                _sleep_step = 0.2
                _slept = 0.0
                while _slept < backoff:
                    if on_cancel_check and on_cancel_check():
                        raise APIError(499, "用户取消请求（退避等待中）")
                    _cancel_event.wait(min(_sleep_step, backoff - _slept))
                    _slept += _sleep_step
            except Exception as e:
                code, msg = self._parse_error(e)
                raise APIError(code, msg)

        if last_exception is not None:
            raise last_exception

    def _request_kwargs(self, model, messages, reasoning_effort, enable_thinking):
        from urllib.parse import urlsplit
        capabilities = self.cfg.get("provider_capabilities", {}) or {}
        deepseek = urlsplit(self._base_url()).hostname == "api.deepseek.com" or model.startswith("deepseek-")
        kwargs = dict(model=model, messages=messages, stream=True)
        if capabilities.get("stream_usage", True):
            kwargs["stream_options"] = {"include_usage": True}
        if enable_thinking and capabilities.get("reasoning_effort", deepseek):
            kwargs["reasoning_effort"] = reasoning_effort
        if enable_thinking and capabilities.get("thinking", deepseek):
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        if capabilities.get("tools", True):
            from src.core.prompts.tool_descriptions import TOOL_DESCRIPTIONS
            kwargs["tools"] = self._to_openai_tools(TOOL_DESCRIPTIONS)
        from src.core.context_budget import fit_context
        try:
            limit = int(self.cfg.get("context_tokens", 128000))
            kwargs["messages"], removed = fit_context(messages, limit, kwargs.get("tools"))
        except (ValueError, TypeError) as exc:
            raise APIError(400, str(exc), need_retry=False) from exc
        if removed:
            logger.info("上下文预算省略 %s 条旧消息；磁盘历史保持完整", removed)
        return kwargs

    def _chat_stream_once(self, client: "OpenAI", use_model: str, messages: List[Dict],
                          reasoning_effort: str,
                          on_text: Optional[Callable[[str], None]] = None,
                          on_reasoning: Optional[Callable[[str], None]] = None,
                          on_cancel_check: Optional[Callable[[], bool]] = None,
                          enable_thinking: bool = True) -> Dict:
        """单次流式对话尝试（内部方法）。"""
        _ensure_openai()
        stream = None

        try:
            # C-006: 用 socket 默认超时把 DNS 解析包含在连接超时内；
            # stream 对象返回后已建立连接，随即恢复旧超时。
            with _socket_dns_timeout(self._HTTP_TIMEOUT_CONNECT):
                # 构建请求参数 — 思考模式 vs 非思考模式
                create_kwargs = self._request_kwargs(use_model, messages, reasoning_effort, enable_thinking)
                stream = client.chat.completions.create(**create_kwargs)
        except APIError:
            raise
        except Exception as e:
            code, msg = self._parse_error(e)
            raise APIError(code, msg) from e

        return self._consume_stream(stream, use_model, on_text, on_reasoning, on_cancel_check)

    def _consume_stream(self, stream, use_model, on_text=None, on_reasoning=None, on_cancel_check=None):
        full_content = ""
        full_reasoning = ""
        usage_summary = {}
        _request_cancelled = [False]
        try:
            for chunk in stream:
                # 每次迭代前检查取消
                if on_cancel_check:
                    try:
                        if bool(on_cancel_check()):
                            _request_cancelled[0] = True
                            break
                    except Exception as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                if _request_cancelled[0]:
                    break

                # usage chunk: choices 为空
                if hasattr(chunk, "usage") and chunk.usage:
                    u = chunk.usage
                    usage_summary.update({
                        "prompt_tokens": getattr(u, "prompt_tokens", 0),
                        "completion_tokens": getattr(u, "completion_tokens", 0),
                        "total_tokens": getattr(u, "total_tokens", 0),
                        "prompt_cache_hit_tokens": getattr(u, "prompt_cache_hit_tokens", 0) or 0,
                        "prompt_cache_miss_tokens": getattr(u, "prompt_cache_miss_tokens", 0) or 0,
                    })
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None) \
                         or getattr(delta, "reasoning", None) or ""
                text = getattr(delta, "content", None) or ""
                # 捕获原生 OpenAI-style tool_calls（DeepSeek V4 支持）
                tc_list = getattr(delta, "tool_calls", None)
                if tc_list:
                    usage_summary.setdefault("tool_calls", [])
                    for tc in tc_list:
                        idx = getattr(tc, "index", 0)
                        fn = getattr(tc, "function", None)
                        fn_name = getattr(fn, "name", "") if fn else ""
                        fn_args = getattr(fn, "arguments", "") if fn else ""
                        # 合并同 index 的增量（streaming 可能分片）
                        existing = [t for t in usage_summary["tool_calls"] if t.get("index") == idx]
                        if existing:
                            existing[0]["function"]["arguments"] += fn_args
                            if fn_name:
                                existing[0]["function"]["name"] += fn_name
                            if getattr(tc, "id", None):
                                existing[0]["id"] = tc.id
                        else:
                            usage_summary["tool_calls"].append({
                                "index": idx,
                                "id": getattr(tc, "id", None) or f"call_{idx}",
                                "type": "function",
                                "function": {"name": fn_name, "arguments": fn_args},
                            })
                if reasoning:
                    full_reasoning += reasoning
                    if on_reasoning:
                        try:
                            on_reasoning(reasoning)
                        except Exception as _exc:
                            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                if text:
                    full_content += text
                    if on_text:
                        try:
                            on_text(text)
                        except Exception as _exc:
                            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        except _OPENAI_ERRORS as e:
            code, msg = self._parse_error(e)
            raise APIError(code, msg, need_retry=not (full_content or full_reasoning or usage_summary.get("tool_calls"))) from e
        except Exception as e:
            code, msg = self._parse_error(e)
            raise APIError(code, msg, need_retry=not (full_content or full_reasoning or usage_summary.get("tool_calls"))) from e
        finally:
            try:
                if stream is not None:
                    stream.close()
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        usage_summary["content"] = full_content
        usage_summary["reasoning_content"] = full_reasoning
        usage_summary["cancelled"] = _request_cancelled[0]
        usage_summary.update(self._calc_cost(usage_summary, use_model))
        return usage_summary

    # ----- 多模态 (图片理解) -----
    def chat_stream_with_images(self, messages: List[Dict], model: Optional[str] = None,
                                 on_text: Optional[Callable[[str], None]] = None,
                                 on_reasoning: Optional[Callable[[str], None]] = None,
                                 image_data_urls: Optional[List[str]] = None,
                                 on_cancel_check: Optional[Callable[[], bool]] = None,
                                 _vision_retry_count: int = 0,
                                 _is_degraded_call: bool = False,
                                 reasoning_effort: Optional[str] = None,
                                 enable_thinking: bool = True) -> Dict:
        """
        支持图片的流式对话。

        image_data_urls: list of str, 形如 "data:image/png;base64,xxxx"
        如果传入了图片，会把最后一条用户消息改写为 multi-part (image_url + text)。
        如果模型/服务端不支持 vision，则自动降级为纯文本 chat_stream。

        thinking + reasoning_effort 同样会传递。
        """
        imgs = list(image_data_urls or [])
        capabilities = self.cfg.get("provider_capabilities", {}) or {}
        if imgs and capabilities.get("vision") is False:
            raise APIError(400, "当前供应商已禁用图片输入，请选择支持图片的模型", need_retry=False)
        prepared = self._build_vision_messages(messages, imgs) if imgs else messages
        result = self.chat_stream(prepared, model=model, on_text=on_text,
                                  on_reasoning=on_reasoning, on_cancel_check=on_cancel_check,
                                  reasoning_effort=reasoning_effort, enable_thinking=enable_thinking)
        result["used_vision"] = bool(imgs)
        return result

    # ----- 工具: 把最后一条用户消息转为多模态格式 -----
    def _build_vision_messages(self, messages: List[Dict], image_data_urls: List[str]) -> List[Dict]:
        if not messages:
            return list(messages)

        result = []
        last_user_idx = -1
        for i, m in enumerate(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                last_user_idx = i

        for i, m in enumerate(messages):
            if i != last_user_idx:
                result.append(dict(m) if isinstance(m, dict) else m)
                continue

            # 处理最后一条 user 消息：提取文本内容
            content = m.get("content") if isinstance(m, dict) else m
            text_parts = []
            preserved_images: List[Dict] = []
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict):
                        if p.get("type") == "text":
                            text_parts.append(p.get("text", ""))
                        elif p.get("type") == "image_url":
                            preserved_images.append(dict(p))
                        else:
                            text_parts.append(str(p))
                    else:
                        text_parts.append(str(p))

            new_content: List[Dict] = []
            # 先追加历史图片，再追加本次新图片，保留视觉上下文
            new_content.extend(preserved_images)
            for url in image_data_urls:
                new_content.append({"type": "image_url", "image_url": {"url": url}})
            combined_text = "\n".join([t for t in text_parts if t])
            if combined_text:
                new_content.append({"type": "text", "text": combined_text})
            else:
                new_content.append({"type": "text", "text": "请描述这些图片的内容。"})

            new_msg = dict(m) if isinstance(m, dict) else {"role": "user"}
            new_msg["content"] = new_content
            result.append(new_msg)

        return result

    @staticmethod
    def _sanitize_error_message(msg: str, max_length: int = 500) -> str:
        """清理错误消息中的敏感信息，防止 API Key / Bearer Token / JWT 等泄露。

        复用全局 log_sanitizer，并额外兜底：
          - 正则替换 sk-xxx API Key
          - 替换 JSON 中的 "api_key": "..."
          - 先按 max_length*2 截断再脱敏，再限制到 max_length (H-003)
        返回结果可用于日志、UI 提示等。
        """
        if not msg:
            return msg
        # H-003: 先粗截断，避免超长消息拖慢正则/脱敏
        if len(msg) > max_length * 2:
            msg = msg[:max_length * 2]
        cleaned = sanitize_for_log(msg)
        # C-002: 显式兜底，替换 DeepSeek/OpenAI 风格 API key
        cleaned = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "[REDACTED:api-key]", cleaned)
        # C-002: 替换 JSON 形式 api_key 字段
        cleaned = re.sub(r'"api_key"\s*:\s*"[^"]*"', '"api_key": "***"', cleaned, flags=re.IGNORECASE)
        # 最终长度限制
        if len(cleaned) > max_length:
            cleaned = cleaned[:max_length] + "...<truncated>"
        return cleaned

    def _parse_error(self, e) -> tuple:
        """将异常标准化为 (code, msg) 元组。

        优先从 APIError/status_code/异常类型推断，最后回退到通用错误。
        """
        # requests 是可选依赖，导入失败时跳过 requests 专属的异常类型判断
        try:
            import requests
            _has_requests = True
        except ImportError:
            _has_requests = False
        # Already-APIError 短路
        if isinstance(e, APIError):
            return e.code, e.message
        status_code = getattr(e, "status_code", None)
        # C-002 / H-003: 脱敏并把最终消息限制在 200 字符内
        msg = self._sanitize_error_message(str(e) if e is not None else "unknown", max_length=200)
        low = msg.lower()
        # OpenAI SDK 兼容属性
        response = getattr(e, "response", None)
        if response is not None and status_code is None:
            status_code = getattr(response, "status_code", None)

        # C-003: 网络错误优先判断——若消息中含 connection/network/timeout/dns，
        # 或为 requests 的 ConnectionError/Timeout，优先归类为可重试的网络错误，
        # 避免 500 + "timeout" 被误判为服务器内部错误
        if _has_requests:
            if isinstance(e, (requests.exceptions.ConnectionError,)):
                return 0, "网络连接失败, 请检查网络后重试"
            if isinstance(e, (requests.exceptions.Timeout,)):
                return 0, "请求超时, 请检查网络后重试"
        if "connection" in low or "network" in low or "timeout" in low or "dns" in low:
            return 0, "网络连接失败, 请检查网络后重试"

        # 优先依赖 HTTP status_code，仅在无 status 时对消息做严格模式匹配
        if status_code == 401 or (status_code is None and re.search(r"\b401\b", msg)) or "unauthorized" in low or "authentication" in low:
            return 401, "API Key 错误或认证失败"
        if status_code == 402 or (status_code is None and re.search(r"\b402\b", msg)) or "balance" in low or "insufficient" in low or "quota" in low:
            return 402, "账号余额不足, 请充值 (按回车重试)"
        if status_code == 429 or (status_code is None and re.search(r"\b429\b", msg)) or "rate limit" in low or "too many requests" in low:
            return 429, "请求速率超限, 请稍后再试 (按回车重试)"
        if status_code == 503 or (status_code is None and re.search(r"\b503\b", msg)) or "unavailable" in low:
            return 503, "服务器负载过高, 请稍后重试 (按回车重试)"
        if status_code == 500 or (status_code is None and re.search(r"\b500\b", msg)) or "internal" in low:
            return 500, "服务器内部错误, 请稍后重试 (按回车重试)"
        if status_code and 400 <= status_code < 500:
            return status_code, f"请求错误 ({status_code}): {msg}"
        if status_code and 500 <= status_code < 600:
            return status_code, f"上游错误 ({status_code}): {msg}"
        return 500, f"API 调用失败: {msg[:200]}"

    def _calc_cost(self, usage: Dict, model: str) -> Dict:
        costs_pro = {"input": 2.0 / 1_000_000, "cached": 0.25 / 1_000_000, "output": 6.0 / 1_000_000}
        costs_flash = {"input": 1.0 / 1_000_000, "cached": 0.1 / 1_000_000, "output": 2.0 / 1_000_000}
        # 使用更精确的匹配：仅匹配已知的 pro 模型名称，避免误匹配含 "pro" 子串的模型
        _model_lower = (model or "").lower()
        # 精确匹配已知模型类别：检查完整模型名称前缀
        _known_pro_models = (
            "deepseek-chat", "deepseek-reasoner", "deepseek-v3",
            "deepseek-v4-pro",
        )
        _known_flash_models = (
            "deepseek-flash",
            "deepseek-v4-flash",
        )
        is_pro = any(_model_lower.startswith(m) for m in _known_pro_models) and not any(_model_lower.startswith(m) for m in _known_flash_models)
        if not is_pro and not any(_model_lower.startswith(m) for m in _known_flash_models):
            # 未识别的模型名称，回退到子串检测
            is_pro = "pro" in _model_lower and "flash" not in _model_lower
        costs = costs_pro if is_pro else costs_flash
        cached = usage.get("prompt_cache_hit_tokens", 0) or 0
        missed = usage.get("prompt_cache_miss_tokens", 0) or 0
        out = usage.get("completion_tokens", 0) or 0
        cost = cached * costs["cached"] + missed * costs["input"] + out * costs["output"]
        total = cached + missed
        hit_rate = (cached / total * 100) if total > 0 else 0.0
        return {"cost_cny": round(cost, 6), "cache_hit_rate": round(hit_rate, 2)}
