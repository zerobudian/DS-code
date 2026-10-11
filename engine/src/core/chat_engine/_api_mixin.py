# -*- coding: utf-8 -*-
"""APIStreamingMixin — 模型调用与流式响应处理（拆分自 chat_engine.py）。"""
from __future__ import annotations

from src.core.chat_engine._imports import (
    APIError,
    Dict,
    List,
    Optional,
    get_global_rate_limiter,
    time,
)

import logging

logger = logging.getLogger(__name__)




class APIStreamingMixin(object):
    """模型 API 调用与流式输出相关方法。"""

    def _should_retry_step(self, error) -> bool:
        """判断 step 级错误是否可重试（网络/5xx/429）。4xx / 认证类错误不可重试。"""
        if isinstance(error, APIError):
            code = error.code
            if code == 429:
                return True
            if 500 <= code < 600:
                return True
            if 400 <= code < 500:
                return False
            # 其它未知状态码按错误自身标志决定
            return getattr(error, "need_retry", True)
        msg = str(error).lower()
        retryable_keywords = (
            # 英文瞬时错误标志
            "connection", "timeout", "network", "dns", "temporarily unavailable",
            "timed out", "refused", "reset", "temporarily", "unreachable",
            "fail", "failed", "failure",
            # 中文瞬时错误标志
            "超时", "连接", "网络", "临时失败", "失败", "重试", "不可达", "中断",
        )
        return any(k in msg for k in retryable_keywords)

    def _retry_delay(self, attempt: int) -> float:
        """step 级重试的指数退避：第 1 次 1s，第 2 次 2s，第 3 次 4s，最高 16s。"""
        return min(2.0 ** (attempt - 1), self._MAX_RETRY_DELAY)

    def _call_model(self, api_messages: List[Dict]) -> Dict:
        """调用 LLM，失败时按策略重试。4xx / 认证错误直接抛出，不重试。

        除 APIError 外，也会捕获连接/超时/网络等可重试异常，避免底层网络抖
        动直接中断深度模式循环。重试次数与退避上限由类常量控制（C-001）。
        """
        if self._is_stopped():
            raise APIError(499, "请求已被用户取消", need_retry=False)
        # DeepSeekAPI owns transport retries. Never replay a partially delivered stream here.
        return self._stream_chat(api_messages)

    def _stream_chat(self, api_messages: List[Dict]) -> Dict:
        """调用 DeepSeek API，流式输出 content / reasoning，返回 usage。"""
        limiter = get_global_rate_limiter()
        session_key = getattr(self, "_session_id", None) or "default"
        if not limiter.is_allowed(session_key):
            wait = limiter.wait_time(session_key)
            raise APIError(429, f"客户端速率限制已满，请等待 {wait:.1f}s 后重试")

        with self._state_lock:
            raw_images = self._state.get("images_for_message") or []
            images = list(raw_images) if isinstance(raw_images, list) else []
        on_cancel = lambda: self._is_stopped()

        model = self._get_model()
        _reasoning_effort = self.cfg.get("reasoning_effort", "high") or "high"

        # 回调: on_text / on_reasoning
        def on_text(delta):
            # 同步喂给 ArtifactManager，自动识别 HTML/CSS/JS
            if self.artifact_manager is not None:
                try:
                    self.artifact_manager.feed(delta)
                except Exception as _exc:
                    logger.debug("ArtifactManager feed 异常: %s", _exc, exc_info=True)
            if self.on_content_delta:
                try:
                    self.on_content_delta(delta)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        def on_reasoning(delta):
            if self.on_reasoning_delta:
                try:
                    self.on_reasoning_delta(delta)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            # on_reasoning 与 on_reasoning_delta 在 app.py 中指向同一个 bridge，
            # 只发射一次避免 double-emit
            if self.on_reasoning and self.on_reasoning is not self.on_reasoning_delta:
                try:
                    self.on_reasoning(delta)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        try:
            if images:
                usage = self.api.chat_stream_with_images(
                    api_messages,
                    model=model,
                    on_text=on_text,
                    on_reasoning=on_reasoning,
                    image_data_urls=list(images),
                    on_cancel_check=on_cancel,
                    reasoning_effort=_reasoning_effort,
                    enable_thinking=True,
                )
            else:
                usage = self.api.chat_stream(
                    api_messages,
                    model=model,
                    on_text=on_text,
                    on_reasoning=on_reasoning,
                    on_cancel_check=on_cancel,
                    reasoning_effort=_reasoning_effort,
                    enable_thinking=True,
                )
            # 流式结束：强制刷新 ArtifactManager，确保最终 artifact 通知到位
            if self.artifact_manager is not None:
                try:
                    self.artifact_manager.flush()
                except Exception as _exc:
                    logger.debug("ArtifactManager flush 异常: %s", _exc, exc_info=True)
            if not isinstance(usage, dict):
                raise APIError(500, "API 返回数据格式异常")
            return usage
        except APIError:
            raise
        except (ConnectionError, TimeoutError) as e:
            raise APIError(503, "网络连接失败: " + str(e))
        except ValueError as e:
            raise APIError(500, "流式响应解析失败: " + str(e))
        except Exception as e:
            # 将未知异常包一层 APIError
            raise APIError(500, "流式调用异常: " + str(e))

