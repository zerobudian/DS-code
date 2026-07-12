# -*- coding: utf-8 -*-
"""``chat_engine`` 包 — 将原 ``chat_engine.py`` 拆分为多个 mixin 组合而成。

组合顺序（MRO）：``ChatEngine`` 先继承各功能 mixin，最后继承
``ChatEngineBase``，确保 ``__init__`` 与核心状态来自基类。
"""
import time  # noqa: F401  保留以兼容 ``patch("src.core.chat_engine.time.sleep")``

from src.core.chat_engine._base import ChatEngineBase
from src.core.chat_engine._prompt_mixin import SystemPromptMixin
from src.core.chat_engine._api_mixin import APIStreamingMixin
from src.core.chat_engine._deep_loop_mixin import DeepLoopMixin
from src.core.chat_engine._command_mixin import SlashCommandMixin
from src.core.chat_engine._hook_mixin import HookCommandMixin
from src.core.chat_engine._lsp_mixin import LSPCommandMixin
from src.core.chat_engine._subagent_mixin import SubAgentMixin
from src.core.chat_engine._callback_mixin import CallbackMixin

# 重新导出模块级常量/协议，保持与原 chat_engine.py 模块命名空间兼容
from src.core.chat_engine._protocols import (  # noqa: F401
    TOOL_CALL_THRESHOLD,
    DEFAULT_MAX_TURN_TIME,
    _MCPManagerLike,
    _SklIIIManagerLike,
    _ImageDocumentManagerLike,
    _SubAgentEngineLike,
)


class ChatEngine(
    DeepLoopMixin,
    SlashCommandMixin,
    SystemPromptMixin,
    APIStreamingMixin,
    HookCommandMixin,
    LSPCommandMixin,
    SubAgentMixin,
    CallbackMixin,
    ChatEngineBase,
):
    """对话引擎 - 整合 DeepSeek API + 工具调用 + 历史上下文。"""

    pass


__all__ = ["ChatEngine"]
