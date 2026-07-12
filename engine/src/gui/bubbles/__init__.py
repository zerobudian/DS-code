# -*- coding: utf-8 -*-
"""消息气泡 / 欢迎页 / 文件标签 / 工具调用卡片 / 打字指示器（拆分为子模块的包）。

严格按设计规格:
  - 卡片圆角: 28px 大卡片(hero) / 24px 面板(lg) / 20px 气泡(bubble) / 12px 工具卡片(tool_card)
  - 主色: PRIM(#CBE3FF), PRIM_DARK(#000000)
  - 背景: BGD(#FFFFFF)
  - 正文: 15px, TEXT(#000000)

子模块:
  - _helpers:         工具函数与常量（_img / _darken / _pixel_pm / _try_pm / _time_slot / pick_greeting）
  - animation_icons:  AnimatedWhaleIcon / ToolAnimationIcon
  - reasoning:        ReasoningBubble / _BreathingDot / TypingIndicator
  - welcome:          WelcomeCard / StatsCard
  - chat_bubbles:     UserBubble / AIBubble
  - tool_bubbles:     ToolCallBubble / PlanBubble
  - file_bubbles:     FilePill / FilePreviewBubble / DiffPreviewBubble
  - image_bubbles:    ClickableImageLabel / ImagePreviewDialog / ImageBubble / UserAttachmentBubble / _show_image_preview
  - streaming_bubbles: StreamingCommandBubble / StreamingStatusBubble / _STATUS_PREFIX_MAP

本 __init__ 重新导出原 bubbles.py 的全部公开名称，以及测试/内部使用的私有名称，
保证 `from src.gui.bubbles import X` 与原单体模块完全向后兼容。
"""
from __future__ import annotations

from src.gui.bubbles._helpers import (
    ASSETS_DIR, GREETINGS, GREETING_HINTS,
    _img, _darken, _pixel_pm, _try_pm, _time_slot, pick_greeting,
)
from src.gui.bubbles.animation_icons import AnimatedWhaleIcon, ToolAnimationIcon
from src.gui.bubbles.reasoning import ReasoningBubble, _BreathingDot, TypingIndicator
from src.gui.bubbles.welcome import WelcomeCard, StatsCard
from src.gui.bubbles.chat_bubbles import UserBubble, AIBubble
from src.gui.bubbles.tool_bubbles import ToolCallBubble, PlanBubble
from src.gui.bubbles.file_bubbles import FilePill, FilePreviewBubble, DiffPreviewBubble
from src.gui.bubbles.image_bubbles import (
    ClickableImageLabel, ImagePreviewDialog, ImageBubble, UserAttachmentBubble,
    _show_image_preview,
)
from src.gui.bubbles.streaming_bubbles import (
    StreamingCommandBubble, StreamingStatusBubble, _STATUS_PREFIX_MAP,
)

__all__ = [
    # constants
    "ASSETS_DIR", "GREETINGS", "GREETING_HINTS",
    # helpers
    "pick_greeting",
    # animation icons
    "AnimatedWhaleIcon", "ToolAnimationIcon",
    # reasoning
    "ReasoningBubble", "TypingIndicator",
    # welcome
    "WelcomeCard", "StatsCard",
    # chat
    "UserBubble", "AIBubble",
    # tool
    "ToolCallBubble", "PlanBubble",
    # file
    "FilePill", "FilePreviewBubble", "DiffPreviewBubble",
    # image
    "ClickableImageLabel", "ImagePreviewDialog", "ImageBubble", "UserAttachmentBubble",
    # streaming
    "StreamingCommandBubble", "StreamingStatusBubble",
]
