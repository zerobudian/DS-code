# -*- coding: utf-8 -*-
"""全局动画常量 — 缓动曲线与统一时长。

所有动画模块共用同一套缓动曲线和时长，保证视觉风格统一。

子模块:
  - EASE_*:    非线性缓动曲线 (OutCubic / OutBack / InOutQuart 等)
  - DURATION_*: 各场景统一时长 (毫秒)
"""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve

# ============================================================================
# 1. 缓动曲线统一管理 — 所有动画共用同一套曲线 — — — — — — — — — — — — — —
# ============================================================================

EASE_SOFT: QEasingCurve = QEasingCurve.OutCubic        # 默认: 柔软、自然 (最常用)
EASE_SPRING: QEasingCurve = QEasingCurve.OutBack       # spring-like 回弹 (按钮/气泡弹入)
EASE_EXPO: QEasingCurve = QEasingCurve.OutExpo         # 快速启动
EASE_ACCENT: QEasingCurve = QEasingCurve.InBack        # 快速收尾 (反向)
EASE_ELASTIC: QEasingCurve = QEasingCurve.OutElastic   # 弹性效果 (很少用)
EASE_SINE: QEasingCurve = QEasingCurve.OutSine         # gentle slow start
EASE_INOUT: QEasingCurve = QEasingCurve.InOutCubic     # InOutCubic — 进出都平滑
EASE_INOUT_Q: QEasingCurve = QEasingCurve.InOutQuart   # InOutQuart (smooth scroll 用)
EASE_INOUTSINE: QEasingCurve = QEasingCurve.InOutSine  # 呼吸光效 (柔和正弦波)

# ============================================================================
# 2. 性能优化后的统一时长（毫秒）
# ============================================================================

DURATION_BUBBLE: int = 180   # 气泡出现/消失
DURATION_PANEL: int = 220    # 面板展开/折叠
DURATION_BUTTON: int = 80    # 按钮 hover/press 微动画
DURATION_FADE: int = 180     # 通用淡入淡出
DURATION_SCROLL: int = 250   # 平滑滚动


__all__ = [
    "EASE_SOFT",
    "EASE_SPRING",
    "EASE_EXPO",
    "EASE_ACCENT",
    "EASE_ELASTIC",
    "EASE_SINE",
    "EASE_INOUT",
    "EASE_INOUT_Q",
    "EASE_INOUTSINE",
    "DURATION_BUBBLE",
    "DURATION_PANEL",
    "DURATION_BUTTON",
    "DURATION_FADE",
    "DURATION_SCROLL",
]
