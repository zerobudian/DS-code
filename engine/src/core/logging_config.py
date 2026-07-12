# -*- coding: utf-8 -*-
"""日志配置：敏感信息过滤器与启动注册入口。

安全审计要求 M-004 / C-005：
- 实现 SensitiveDataFilter，按指定正则过滤 API Key / Bearer Token / Authorization 头。
- 敏感值仅保留前 4 后 4 位（如 sk-abcd...wxyz）。
- 在应用启动时（main.py / app.py）注册到 root logger 及所有 handler。
- 覆盖 record.exc_info / record.exc_text，避免 traceback 泄露敏感信息。
- 与 src.core.log_sanitizer 中的 _sanitize_error_message 兼容，互不干扰。
"""

import logging
import os
import re
import traceback
from typing import Optional

__all__ = ["SensitiveDataFilter", "setup_logging"]


# -----------------------------------------------------------------------------
# 部分脱敏辅助：保留前 4 后 4 位
# -----------------------------------------------------------------------------
def _partial_redact_api_key(match: re.Match) -> str:
    """将 sk-xxx API Key 脱敏为 sk-abcd...wxyz（保留前 4 后 4 位）。"""
    key = match.group(0)
    body = key[3:]  # 去掉 "sk-" 前缀
    if len(body) <= 8:
        return "<REDACTED:api-key>"
    return f"sk-{body[:4]}...{body[-4:]}"


def _partial_redact_bearer(match: re.Match) -> str:
    """将 Bearer Token 脱敏；JWT 整体脱敏，其它保留前 4 后 4 位。"""
    token = match.group(1)  # 捕获组：Bearer 之后的 token
    # 已脱敏的占位符不再二次处理
    if token.startswith("<") and "REDACTED" in token.upper():
        return match.group(0)
    # JWT 保持整体脱敏
    if token.startswith("eyJ") and "." in token:
        return "Bearer <REDACTED:jwt>"
    if len(token) <= 8:
        return "Bearer <REDACTED>"
    return f"Bearer {token[:4]}...{token[-4:]}"


def _partial_redact_authorization(match: re.Match) -> str:
    """将 Authorization: Bearer <token> 脱敏；JWT 整体脱敏，其它保留前 4 后 4 位。"""
    token = match.group(1)  # 捕获组：Bearer 之后的 token
    # JWT 保持整体脱敏，避免 Authorization 模式二次部分脱敏暴露 eyJ 前缀
    if token.startswith("eyJ") and "." in token:
        return "Authorization: Bearer <REDACTED:jwt>"
    if len(token) <= 8:
        return "Authorization: Bearer <REDACTED>"
    return f"Authorization: Bearer {token[:4]}...{token[-4:]}"


def _partial_redact_json_api_key(match: re.Match) -> str:
    """将 JSON 中的 "api_key": "sk-..." 值部分脱敏。"""
    body = match.group(1)  # 捕获组：引号内的值
    if len(body) <= 8:
        return '"api_key": "***"'
    return f'"api_key": "{body[:4]}...{body[-4:]}"'


class SensitiveDataFilter(logging.Filter):
    r"""按审计报告要求过滤日志中的敏感信息。

    覆盖模式：
      - sk-[A-Za-z0-9_\-]{8,}
      - Bearer\s+[^\s]+
      - "api_key"\s*:\s*"[^"]*"
      - Authorization:\s*Bearer\s+[^\s]+

    脱敏策略：
      - API Key / Bearer Token 保留前 4 后 4 位，中间用 ... 代替。
      - 脱敏同时作用于 record.msg、record.args、record.exc_info / exc_text。
    """

    _PATTERNS = [
        # 先处理完整 Authorization 头，避免后续 JWT/Bearer 模式将 token 部分替换后
        # 又被 Authorization 模式二次部分脱敏
        # 使用捕获组提取 token，避免固定长度切片与变长空白不匹配
        (re.compile(r"Authorization:\s*Bearer\s+([^\s]+)", re.IGNORECASE), _partial_redact_authorization),
        (re.compile(r"Bearer\s+([^\s]+)", re.IGNORECASE), _partial_redact_bearer),
        # JWT 保持整体脱敏，避免 Bearer + JWT 场景暴露 eyJ 前缀
        (re.compile(r"\beyJ[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)+\b"), "<REDACTED:jwt>"),
        (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), _partial_redact_api_key),
        (re.compile(r'"api_key"\s*:\s*"([^"]*)"', re.IGNORECASE), _partial_redact_json_api_key),
    ]

    def _redact(self, text: str) -> str:
        if not isinstance(text, str):
            text = str(text)
        for pattern, replacement in self._PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.args:
                # 当存在 args 时，record.msg 是 printf 风格的格式字符串（含 %s 等占位符），
                # 必须原样保留。若对 msg 脱敏，正则可能匹配并替换掉 "%s" 等占位符，
                # 导致后续 record.getMessage() 执行 msg % args 时抛 TypeError，
                # 日志记录被静默丢弃。此时仅对 args 中的字符串值脱敏。
                if isinstance(record.args, dict):
                    record.args = {
                        k: self._redact(v) if isinstance(v, str) else v
                        for k, v in record.args.items()
                    }
                elif isinstance(record.args, (list, tuple)):
                    record.args = tuple(
                        self._redact(a) if isinstance(a, str) else a
                        for a in record.args
                    )
            else:
                # 无 args：record.msg 即最终文本（或其 __str__），可直接脱敏
                record.msg = self._redact(str(record.msg))
            # 处理 traceback / exc_info 中可能包含的敏感信息
            if record.exc_info:
                tb_text = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text = self._redact(tb_text)
                record.exc_info = None
        except Exception:
            # 脱敏失败不应阻塞日志记录
            pass
        return True


_GLOBAL_FILTER: Optional[SensitiveDataFilter] = None


def get_log_level() -> int:
    """根据环境变量决定默认日志级别（M-014）。

    优先级：DSCODE_LOG_LEVEL > ENV。
    生产环境（ENV=production/prod）默认 WARNING；
    开发/测试/其它环境默认 INFO。
    """
    env_level = os.environ.get("DSCODE_LOG_LEVEL", "").strip().upper()
    if env_level:
        try:
            return int(logging.getLevelName(env_level))
        except (ValueError, TypeError):
            pass
    env = os.environ.get("ENV", "").strip().lower()
    if env in ("production", "prod"):
        return logging.WARNING
    return logging.INFO


def setup_logging() -> None:
    """将 SensitiveDataFilter 安装到根 logger、所有已创建的 logger 及其 handler。

    同时根据环境变量设置根 logger 级别（M-014）。
    """
    global _GLOBAL_FILTER
    if _GLOBAL_FILTER is None:
        _GLOBAL_FILTER = SensitiveDataFilter()

    def _attach(logger_obj: logging.Logger) -> None:
        # C-005: 避免重复添加过滤器
        try:
            if _GLOBAL_FILTER not in logger_obj.filters:
                logger_obj.addFilter(_GLOBAL_FILTER)
        except Exception:
            pass
        # C-005: 过滤器必须覆盖到所有 handler，确保 logging.info/error/debug 输出也脱敏
        for handler in getattr(logger_obj, "handlers", []):
            try:
                if _GLOBAL_FILTER not in handler.filters:
                    handler.addFilter(_GLOBAL_FILTER)
            except Exception:
                pass

    # 根 logger：设置日志级别（M-014）
    root = logging.getLogger()
    try:
        root.setLevel(get_log_level())
    except Exception:
        pass
    try:
        _attach(root)
    except Exception:
        pass

    # 已创建的 logger
    try:
        for existing in logging.Logger.manager.loggerDict.values():
            if isinstance(existing, logging.Logger):
                _attach(existing)
    except Exception:
        pass
