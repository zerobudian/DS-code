# -*- coding: utf-8 -*-
"""数字滚动 — Number Ticker 动画（用于 2x2 统计框 / tokens 计数）。

包含:
  - _NumberTickerBridge: 数字滚动的内部桥梁对象 (Qt 动画需要 QObject 作为 target)
  - number_ticker:       数字滚动动画
  - token_ticker:        tokens 计数专用 (超过 1000 自动以 '2.9k' 形式显示)
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, QPropertyAnimation, Property
from PySide6.QtWidgets import QWidget, QLabel

from src.gui.animations._constants import EASE_SOFT


class _NumberTickerBridge(QObject):
    """数字滚动的内部桥梁对象（Qt 动画需要 QObject 作为 target）。"""

    def __init__(
        self,
        widget: QLabel,
        start_val: float,
        end_val: float,
        decimals: int,
        prefix: str,
        suffix: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._widget: QLabel = widget
        self._start: float = float(start_val)
        self._end: float = float(end_val)
        self._decimals: int = int(decimals)
        self._prefix: str = prefix
        self._suffix: str = suffix
        self._current: float = self._start
        self._anim: Optional[QPropertyAnimation] = None

    def _get_current(self) -> float:
        return self._current

    def _set_current(self, val: float) -> None:
        self._current = float(val)
        # 根据数值大小格式化显示（超过 1000 用 k 表示）
        display = val
        if abs(display) >= 1000:
            text = f"{self._prefix}{display/1000:.{max(0,self._decimals)}f}k{self._suffix}"
        else:
            text = f"{self._prefix}{display:.{self._decimals}f}{self._suffix}"
        try:
            self._widget.setText(text)
        except Exception:
            pass

    current_value = Property(float, _get_current, _set_current)


def number_ticker(
    widget: QLabel,
    start_val: float,
    end_val: float,
    duration_ms: int = 600,
    decimals: int = 0,
    prefix: str = "",
    suffix: str = "",
) -> Optional[_NumberTickerBridge]:
    """数字滚动动画 — Number Ticker 效果。

    用于 2x2 统计框 (API余额/缓存命中/tokens) 的数值刷新。

    Args:
        widget: 目标 QLabel 或支持 setText(str) 的 widget
        start_val: 起始数值
        end_val: 目标数值
        duration_ms: 动画时长，默认 600ms（设计稿要求）
        decimals: 保留小数位数，整数使用 0
        prefix: 前缀文本（如 ""）
        suffix: 后缀文本（如 "CNY"、"%"）

    Returns:
        返回内部桥接对象引用（保活用），失败返回 None。

    缓动: OutCubic (ease-out) — 符合设计稿非线性要求
    """
    try:
        bridge = _NumberTickerBridge(
            widget, start_val, end_val, decimals, prefix, suffix,
            parent=widget,
        )
        anim = QPropertyAnimation(bridge, b"current_value", parent=widget)
        anim.setDuration(int(duration_ms))
        anim.setStartValue(float(start_val))
        anim.setEndValue(float(end_val))
        anim.setEasingCurve(EASE_SOFT)  # OutCubic — ease-out
        anim.start(QPropertyAnimation.DeleteWhenStopped)
        bridge._anim = anim
        return bridge
    except Exception:
        # 回退：直接设置终值
        try:
            widget.setText(f"{prefix}{end_val:.{decimals}f}{suffix}")
        except Exception:
            pass
        return None


def token_ticker(
    widget: QLabel,
    start_count: int,
    end_count: int,
    duration_ms: int = 800,
) -> Optional[_NumberTickerBridge]:
    """tokens 计数专用 —— 超过 1000 自动以 '2.9k' 形式显示。"""
    return number_ticker(
        widget, float(start_count), float(end_count),
        duration_ms=duration_ms, decimals=1, suffix="",
    )


__all__ = [
    "_NumberTickerBridge",
    "number_ticker",
    "token_ticker",
]
