# -*- coding: utf-8 -*-
"""动画共用辅助函数与包装器。

提供:
  - _stop_existing_anim:     停止并清理 widget 上已保存的动画引用
  - _ensure_opacity_effect:  复用 widget 已有的 QGraphicsOpacityEffect
  - _FadeEffectWrapper:      兼容旧调用的 opacity effect 包装器
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QWidget, QGraphicsOpacityEffect


def _stop_existing_anim(widget: QWidget, attr: str = "_ds_anim") -> None:
    """停止并清理 widget 上已保存的动画引用，避免重复触发/内存泄漏。"""
    anim = getattr(widget, attr, None)
    if anim is not None:
        try:
            anim.stop()
        except Exception:
            pass
        setattr(widget, attr, None)


def _ensure_opacity_effect(
    widget: QWidget, initial_opacity: float = 0.0
) -> Optional[QGraphicsOpacityEffect]:
    """复用 widget 已有的 QGraphicsOpacityEffect，避免重复创建/销毁。

    如果当前 effect 不是 QGraphicsOpacityEffect，则先移除旧 effect 再创建新的。
    """
    try:
        existing = widget.graphicsEffect()
        if isinstance(existing, QGraphicsOpacityEffect):
            existing.setOpacity(initial_opacity)
            return existing
        # 其它类型 effect 不适合复用，先清理
        if existing is not None:
            widget.setGraphicsEffect(None)
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(initial_opacity)
        widget.setGraphicsEffect(effect)
        return effect
    except Exception:
        return None


class _FadeEffectWrapper:
    """为 widget 应用 QGraphicsOpacityEffect 的包装器（保留兼容旧调用）。"""

    def __init__(self, widget: QWidget) -> None:
        self.widget: QWidget = widget
        self.effect: QGraphicsOpacityEffect = (
            _ensure_opacity_effect(widget, 1.0) or QGraphicsOpacityEffect(widget)
        )
        widget.setGraphicsEffect(self.effect)


__all__ = [
    "_stop_existing_anim",
    "_ensure_opacity_effect",
    "_FadeEffectWrapper",
]
