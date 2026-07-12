# -*- coding: utf-8 -*-
"""向后兼容垫片（shim）。

真实实现已拆分到 ``src/core/chat_engine/`` 包（见 ``chat_engine/__init__.py``）。
Python 导入系统在同目录下同时存在同名 *包*（``chat_engine/``）与 *模块*
（本文件）时会优先选择包，因此 ``from src.core.chat_engine import ChatEngine``
实际由包的 ``__init__.py`` 提供实现。本文件仅作兼容旧路径与文档说明保留，
并重新导出常用模块级名称，便于任何按文件路径引用的旧代码继续可用。
"""
from src.core.chat_engine import ChatEngine  # noqa: F401
from src.core.chat_engine import (  # noqa: F401
    DEFAULT_MAX_TURN_TIME,
    TOOL_CALL_THRESHOLD,
    _ImageDocumentManagerLike,
    _MCPManagerLike,
    _SklIIIManagerLike,
    _SubAgentEngineLike,
)

__all__ = [
    "ChatEngine",
    "TOOL_CALL_THRESHOLD",
    "DEFAULT_MAX_TURN_TIME",
    "_MCPManagerLike",
    "_SklIIIManagerLike",
    "_ImageDocumentManagerLike",
    "_SubAgentEngineLike",
]
