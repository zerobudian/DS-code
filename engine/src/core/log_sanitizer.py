# -*- coding: utf-8 -*-
"""日志脱敏工具 — 防止敏感信息（API Key / Token / 密码 / 邮箱 / 用户路径）泄漏到日志或输出中。

使用示例:
    from src.core.log_sanitizer import sanitize_for_log, sanitize_dict_for_log

    log.info("配置: %s", sanitize_for_log(str(config)))
    # 或
    log.debug("请求: %s", sanitize_dict_for_log(request_body))

核心原则:
    1. **白名单优先** — 关键字段一律替换为 ***
    2. **内容匹配** — 即使字段名未命中，内容中形如 sk-xxx / Bearer xxx /
       email@domain 的敏感信息也会被脱敏
    3. **路径脱敏** — 自动隐藏用户主目录绝对路径
"""

import os
import re
import logging
import json
import traceback
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 敏感字段名（大小写不敏感匹配）— 这些字段的值一律替换为 ***
# ---------------------------------------------------------------------------
_SENSITIVE_KEYS = {
    # API Key / Token
    "api_key", "apikey", "api-key", "api_token", "token",
    "auth_token", "access_token", "access-token", "refresh_token", "refresh-token",
    "bearer_token", "bearer-token", "bearer",
    "secret", "secret_key", "secret-key", "private_key", "private-key",
    "x_api_key", "x-api-key", "x_authorization", "x-authorization",
    "authorization", "auth",
    # 账号 / 密码
    "password", "passwd", "pwd", "pass", "passphrase",
    # 加密
    "cipher", "encryption_key", "encryption-key",
    # SSH
    "ssh_key", "ssh-key", "private-key", "private_key",
    # 个人信息
    "email", "email_address", "email-address",
    "phone", "phone_number", "phone-number", "mobile", "mobile_number",
    "username", "user_name", "user-name", "user",
    # 支付
    "credit_card", "credit-card", "card_number", "card-number",
    # 常见第三方 key 前缀字段
    "openai_key", "deepseek_key", "anthropic_key", "google_key",
    "hf_token", "hf-token", "huggingface_token",
}

# ---------------------------------------------------------------------------
# 内容模式（即使字段名未匹配，内容像敏感信息也被处理）
# ---------------------------------------------------------------------------
def _partial_redact_api_key(match: re.Match) -> str:
    """sk-xxxxx... → sk-abcd...wxyz（保留前 4 后 4 位）。"""
    key = match.group(0)
    body = key[3:]  # 去掉 "sk-" 前缀
    if len(body) <= 8:
        return "<REDACTED:api-key>"
    return f"sk-{body[:4]}...{body[-4:]}"


def _partial_redact_bearer(match: re.Match) -> str:
    """Bearer xxxxxx... → Bearer abcd...wxyz（保留前 4 后 4 位）。"""
    token = match.group(0)
    body = token[len("Bearer"):].lstrip()
    # 已脱敏的占位符不再二次处理
    if body.startswith("<") and "REDACTED" in body.upper():
        return token
    # JWT 保持整体脱敏（冗余保护：正常由上方 JWT 模式优先处理）
    if body.startswith("eyJ") and "." in body:
        return "Bearer <REDACTED:jwt>"
    if len(body) <= 8:
        return "Bearer <REDACTED>"
    return f"Bearer {body[:4]}...{body[-4:]}"


_CONTENT_PATTERNS: List[tuple] = [
    # OpenAI / DeepSeek / Anthropic 风格 API Key: sk-xxxxx...
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), _partial_redact_api_key),
    # JWT Token（需在 Bearer 之前，避免 Bearer + JWT 被部分脱敏后仍暴露 eyJ 前缀）
    # 允许一个或多个 dot 段，兼容测试里简化的单点 token
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)+\b"), "<REDACTED:jwt>"),
    # Bearer Token (Authorization 头)
    (re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{12,}", re.IGNORECASE), _partial_redact_bearer),
    # 邮箱
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "<REDACTED:email>"),
    # 手机号（中国 11 位）
    (re.compile(r"\b1[3-9]\d{9}\b"), "<REDACTED:phone>"),
    # 银行卡号（13-19 位数字，带空格/连字符）
    (re.compile(r"\b\d{4}[ \-]?\d{4}[ \-]?\d{4}[ \-]?\d{1,7}\b"), "<REDACTED:card>"),
    # Authorization 头简写 (Base64)
    (re.compile(r"\bBasic\s+[A-Za-z0-9+/=]{8,}", re.IGNORECASE), "Basic <REDACTED>"),
]

# ---------------------------------------------------------------------------
# 缓存用户主目录，用于路径脱敏
# ---------------------------------------------------------------------------
_HOME_DIRS: List[str] = []
try:
    _home = os.path.expanduser("~")
    if _home and _home != "~":
        _HOME_DIRS.append(_home)
except Exception as _exc:
    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)


# ============================================================================
# 主 API：字符串脱敏
# ============================================================================
def sanitize_for_log(text: Optional[str], max_length: int = 4000) -> str:
    """将字符串中的敏感信息脱敏，用于安全地写入日志。

    Args:
        text: 任意字符串。None 时返回 '<none>'。
        max_length: 超过长度时截断，防止日志爆炸。

    Returns:
        脱敏后的字符串。
    """
    if text is None:
        return "<none>"
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return "<unrepresentable>"

    # 内容模式匹配（按顺序处理）
    result = text
    for pattern, replacement in _CONTENT_PATTERNS:
        result = pattern.sub(replacement, result)

    # 路径脱敏：隐藏用户主目录绝对路径
    for home_dir in _HOME_DIRS:
        if home_dir and home_dir in result:
            result = result.replace(home_dir, "~")

    # 长度保护
    if len(result) > max_length:
        result = result[:max_length] + f" ...<truncated, total {len(text)} chars>"

    return result


# ============================================================================
# 主 API：字典 / JSON 结构脱敏（保留结构，但敏感字段值被替换）
# ============================================================================
def sanitize_dict_for_log(obj: Any, depth: int = 0, max_depth: int = 6) -> Any:
    """递归脱敏一个 dict/list 结构中的敏感字段。

    适用于在写入日志前对 API 请求 payload / 配置对象进行脱敏。
    """
    if obj is None:
        return None

    if depth > max_depth:
        return "<truncated:max-depth>"

    # --- dict: 检查 key 是否命中敏感词列表 ---
    if isinstance(obj, dict):
        result: Dict[Any, Any] = {}
        for k, v in obj.items():
            # 关键 key（敏感字段）：值一律 ***
            key_str = str(k).lower().replace("_", "-")
            # 简化匹配：key 标准化后是否命中敏感词
            if key_str in _SENSITIVE_KEYS or _contains_any_sensitive_keyword(key_str):
                # 嵌套结构也整体 ***
                result[k] = "***"
            else:
                # 对值递归检查（字符串也走内容模式）
                if isinstance(v, str):
                    result[k] = sanitize_for_log(v)
                elif isinstance(v, (dict, list, tuple)):
                    result[k] = sanitize_dict_for_log(v, depth + 1, max_depth)
                else:
                    result[k] = v
        return result

    # --- list / tuple ---
    if isinstance(obj, (list, tuple)):
        cleaned = [sanitize_dict_for_log(item, depth + 1, max_depth) for item in obj]
        return type(obj)(cleaned) if isinstance(obj, tuple) else cleaned

    # --- 字符串：内容模式匹配 ---
    if isinstance(obj, str):
        return sanitize_for_log(obj)

    # --- 其他类型直接返回（但如果 repr 可能暴露信息就保留原值） ---
    return obj


def _contains_any_sensitive_keyword(key_str: str) -> bool:
    """模糊匹配敏感关键字 — 兼容包含 'api-key' 之类的复合字段。"""
    low = key_str.lower()
    for kw in _SENSITIVE_KEYS:
        if kw in low:
            # 排除 'user-agent' — 不是敏感字段
            if low in ("user-agent", "user_agent"):
                continue
            return True
    return False


# ============================================================================
# logging Filter：可直接挂到任何 logging handler，自动脱敏所有日志消息
# ============================================================================
class SanitizeFilter(logging.Filter):
    """logging 过滤器 — 自动脱敏日志消息。

    使用示例:
        handler = logging.StreamHandler()
        handler.addFilter(SanitizeFilter())
        logging.getLogger().addHandler(handler)
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = sanitize_for_log(str(record.msg))
            # 处理 %s / %(name)s 风格的参数
            if record.args:
                if isinstance(record.args, dict):
                    record.args = sanitize_dict_for_log(record.args)
                elif isinstance(record.args, (list, tuple)):
                    record.args = tuple(
                        sanitize_for_log(str(a)) if isinstance(a, str) else a
                        for a in record.args
                    )
            # 处理 traceback / exc_info 中的敏感信息
            if record.exc_info:
                tb_text = "".join(traceback.format_exception(*record.exc_info))
                # traceback 使用更大的上限，避免丢失末尾的关键错误帧
                record.exc_text = sanitize_for_log(tb_text, max_length=50000)
                record.exc_info = None
        except Exception as _exc:
            # 永远不能因为脱敏失败而阻塞日志
            logger.debug("日志脱敏失败，忽略异常: %s", _exc, exc_info=True)
        return True


# ============================================================================
# JSON 辅助：先 dump 再脱敏
# ============================================================================
def sanitize_json_for_log(obj: Any) -> str:
    """将对象序列化为 JSON 并脱敏。适用于调试 API 响应。"""
    try:
        raw = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception as e:
        return f"<unserializable: {e}>"
    return sanitize_for_log(raw)


# ============================================================================
# 全局自动安装：将 SanitizeFilter 应用到所有 logger
# ============================================================================
_GLOBAL_SANITIZE_FILTER: Optional[logging.Filter] = None


def install_global_sanitize_filter() -> None:
    """将 SanitizeFilter 自动安装到根 logger、所有 handler 及未来新创建的 logger。

    说明：
      - 立即给根 logger 和所有已存在 logger 及其 handler 添加过滤器；
      - 通过轻量 monkey-patch logging.getLogger，确保后续新创建的 logger
        也自动带上 SanitizeFilter，实现"全局生效"。
      - 重复调用会被忽略。
    """
    global _GLOBAL_SANITIZE_FILTER
    if _GLOBAL_SANITIZE_FILTER is not None:
        return

    _GLOBAL_SANITIZE_FILTER = SanitizeFilter()

    def _attach_logger(log: logging.Logger) -> None:
        try:
            log.addFilter(_GLOBAL_SANITIZE_FILTER)
        except Exception as _exc:
            logger.debug("全局日志脱敏安装 logger 失败: %s", _exc, exc_info=True)
        # C-005: 同时覆盖到 handler，避免绕过 logger 直接写 handler 时泄漏
        for handler in getattr(log, "handlers", []):
            try:
                if _GLOBAL_SANITIZE_FILTER not in handler.filters:
                    handler.addFilter(_GLOBAL_SANITIZE_FILTER)
            except Exception as _exc:
                logger.debug("全局日志脱敏安装 handler 失败: %s", _exc, exc_info=True)

    # 1. 根 logger
    try:
        _attach_logger(logging.getLogger())
    except Exception as _exc:
        logger.debug("全局日志脱敏安装根 logger 失败: %s", _exc, exc_info=True)

    # 2. 已创建的 logger
    try:
        for existing in logging.Logger.manager.loggerDict.values():
            if isinstance(existing, logging.Logger):
                _attach_logger(existing)
    except Exception as _exc:
        logger.debug("全局日志脱敏安装已有 logger 失败: %s", _exc, exc_info=True)

    # 3. Hook 未来创建的 logger（只 patch 一次）
    try:
        _orig_getLogger = logging.getLogger

        def _patched_getLogger(name: Optional[str] = None) -> logging.Logger:
            if name is None:
                log = _orig_getLogger()
            else:
                log = _orig_getLogger(name)
            if _GLOBAL_SANITIZE_FILTER is not None:
                if _GLOBAL_SANITIZE_FILTER not in log.filters:
                    log.addFilter(_GLOBAL_SANITIZE_FILTER)
                # C-005: 新创建的 logger 其 handler 也需覆盖
                for handler in getattr(log, "handlers", []):
                    try:
                        if _GLOBAL_SANITIZE_FILTER not in handler.filters:
                            handler.addFilter(_GLOBAL_SANITIZE_FILTER)
                    except Exception:
                        pass
            return log

        if not getattr(logging.getLogger, "_sanitize_filter_patched", False):
            logging.getLogger = _patched_getLogger
            logging.getLogger._sanitize_filter_patched = True  # type: ignore[attr-defined]
    except Exception as _exc:
        logger.debug("Hook logging.getLogger 失败: %s", _exc, exc_info=True)


# 模块被导入时自动安装（失败也不影响主流程）
try:
    install_global_sanitize_filter()
except Exception as _exc:
    logger.debug("全局日志脱敏自动安装失败: %s", _exc, exc_info=True)


# ============================================================================
# 模块级便捷变量：暴露给上层直接 import 使用
# ============================================================================
__all__ = [
    "sanitize_for_log",
    "sanitize_dict_for_log",
    "sanitize_json_for_log",
    "SanitizeFilter",
    "install_global_sanitize_filter",
]
