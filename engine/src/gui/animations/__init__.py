# -*- coding: utf-8 -*-
"""全局动画工具 — 为整个 GUI 提供统一的丝滑动效（拆分为子模块的包）。

核心:
  1. 默认非线性缓动曲线 (OutCubic / OutBack / InOutQuart)
  2. 组合动画 (同时运行 pos + opacity + scale + blur)
  3. 动画完成后自动清理 QGraphicsEffect (避免内存泄漏)
  4. 所有函数线程安全 (仅调用 Qt 的属性)

用法:
  from src.gui.animations import (fade_in, fade_out, slide_in, slide_out,
                                    smooth_scroll, pop_in, breathe,
                                    enter_anim, exit_anim)

  # 高级入场（淡入 + 从下向上滑 + 模糊）
  enter_anim(widget, direction="up", duration_ms=280, blur=True)

  # 平滑滚动
  smooth_scroll(chat_view.verticalScrollBar(), 2000, duration_ms=300)

  # 缩放弹入 (spring-like)
  pop_in(widget, duration_ms=350, start_scale=0.85)

  # 按钮颜色过渡
  animate_background(btn, COLORS['PRIM_LIGHT'], COLORS['CARD'], 150)

  # 呼吸光效 (用于鲸鱼图标)
  breathe(whale_widget, duration_ms=3000)

全部基于 QPropertyAnimation，无第三方依赖。

子模块:
  - _constants: 缓动曲线与统一时长常量
  - _helpers:   _stop_existing_anim / _ensure_opacity_effect / _FadeEffectWrapper
  - _effects:   淡入淡出 / 模糊 / 滑动 / 滚动 / 缩放 / 呼吸 / 脉冲 等基础动画
  - _widgets:   AnimatedWidget / AnimatedButton / SmoothColorButton /
                HoverColorButton / HoverCard / _ButtonPixmapCache
  - _shadow:    DropShadowEffect / drop_shadow
  - _ticker:    _NumberTickerBridge / number_ticker / token_ticker

本 __init__ 重新导出原 animations.py 的全部公开名称，以及测试/内部使用的私有名称，
保证 `from src.gui.animations import X` 与原单体模块完全向后兼容。
"""
from __future__ import annotations

from src.gui.animations._constants import (
    EASE_SOFT, EASE_SPRING, EASE_EXPO, EASE_ACCENT, EASE_ELASTIC,
    EASE_SINE, EASE_INOUT, EASE_INOUT_Q, EASE_INOUTSINE,
    DURATION_BUBBLE, DURATION_PANEL, DURATION_BUTTON, DURATION_FADE, DURATION_SCROLL,
)
from src.gui.animations._helpers import (
    _stop_existing_anim, _ensure_opacity_effect, _FadeEffectWrapper,
)
from src.gui.animations._effects import (
    fade_in, fade_out, blur_fade_in, blur_fade_out,
    slide_in, slide_out, enter_anim, exit_anim,
    smooth_scroll, pop_in, pop_out, animate_background,
    breathe, pulse, _BreathingCycle,
)
from src.gui.animations._widgets import (
    AnimatedWidget, AnimatedButton, _ButtonPixmapCache,
    SmoothColorButton, HoverColorButton, HoverCard,
)
from src.gui.animations._shadow import (
    DropShadowEffect, drop_shadow,
)
from src.gui.animations._ticker import (
    _NumberTickerBridge, number_ticker, token_ticker,
)

__all__ = [
    # 缓动曲线
    "EASE_SOFT", "EASE_SPRING", "EASE_EXPO", "EASE_ACCENT", "EASE_ELASTIC",
    "EASE_SINE", "EASE_INOUT", "EASE_INOUT_Q", "EASE_INOUTSINE",
    # 时长常量
    "DURATION_BUBBLE", "DURATION_PANEL", "DURATION_BUTTON",
    "DURATION_FADE", "DURATION_SCROLL",
    # 基础动画
    "fade_in", "fade_out", "blur_fade_in", "blur_fade_out",
    "slide_in", "slide_out", "enter_anim", "exit_anim",
    "smooth_scroll", "pop_in", "pop_out", "animate_background",
    "breathe", "pulse",
    # 动画 widget 组件
    "AnimatedWidget", "AnimatedButton", "SmoothColorButton",
    "HoverColorButton", "HoverCard",
    # 阴影
    "DropShadowEffect", "drop_shadow",
    # 数字滚动
    "number_ticker", "token_ticker",
    # 内部/测试使用的私有名称
    "_stop_existing_anim", "_ensure_opacity_effect", "_FadeEffectWrapper",
    "_BreathingCycle", "_ButtonPixmapCache", "_NumberTickerBridge",
]
