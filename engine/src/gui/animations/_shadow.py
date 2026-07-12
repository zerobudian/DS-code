# -*- coding: utf-8 -*-
"""阴影动画 — widget 周围动态阴影（用于选中/提示效果）。

包含:
  - DropShadowEffect: 阴影管理类 — 设置 widget 周围的阴影
  - drop_shadow:      为 widget 添加阴影动画（短暂增强后恢复）
"""
from __future__ import annotations

from PySide6.QtCore import QPropertyAnimation
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget, QGraphicsDropShadowEffect

from src.gui.style import COLORS
from src.gui.animations._constants import EASE_SPRING, DURATION_PANEL
from src.gui.animations._helpers import _stop_existing_anim


class DropShadowEffect(QGraphicsDropShadowEffect):
    """阴影管理类 — 设置 widget 周围的阴影。"""

    def __init__(
        self,
        widget: QWidget,
        blur_radius: float = 20.0,
        color: str = COLORS['TEXT_4'],
    ) -> None:
        super().__init__(widget)
        self.widget: QWidget = widget
        self.setBlurRadius(blur_radius)
        self.setColor(QColor(color))
        widget.setGraphicsEffect(self)

    def set_blur(self, radius: float) -> None:
        self.setBlurRadius(radius)


def drop_shadow(
    widget: QWidget,
    duration_ms: int = DURATION_PANEL,
    start_radius: float = 8.0,
    end_radius: float = 24.0,
) -> None:
    """为 widget 添加阴影动画（短暂增强后恢复）。默认 220ms。"""
    _stop_existing_anim(widget, "_ds_shadow_anim")
    try:
        widget.setGraphicsEffect(None)
    except Exception:
        pass

    effect = QGraphicsBlurEffect(widget)
    effect.setBlurRadius(start_radius)
    widget.setGraphicsEffect(effect)

    anim = QPropertyAnimation(effect, b"blurRadius", parent=widget)
    anim.setDuration(duration_ms)
    anim.setStartValue(start_radius)
    anim.setEndValue(end_radius)
    anim.setEasingCurve(EASE_SPRING)
    widget._ds_shadow_anim = anim

    # 恢复
    def _cleanup() -> None:
        try:
            widget.setGraphicsEffect(None)
            widget._ds_shadow_anim = None
        except Exception:
            pass
    anim.finished.connect(_cleanup)
    anim.start(QPropertyAnimation.DeleteWhenStopped)


__all__ = [
    "DropShadowEffect",
    "drop_shadow",
]
