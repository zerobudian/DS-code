# -*- coding: utf-8 -*-
"""基础动画效果 — 淡入淡出 / 模糊 / 滑动 / 滚动 / 缩放 / 呼吸 / 脉冲。

包含函数:
  - fade_in / fade_out:          基础透明度动画
  - blur_fade_in / blur_fade_out:高斯模糊入场/出场
  - slide_in / slide_out:        位置滑动
  - enter_anim / exit_anim:      组合入场/出场 (slide + fade + blur)
  - smooth_scroll:               平滑滚动
  - pop_in / pop_out:            缩放弹入/弹出 (spring-like)
  - animate_background:          背景色渐变
  - breathe / _BreathingCycle:   呼吸光效
  - pulse:                       脉冲动画

全部基于 QPropertyAnimation，无第三方依赖。
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import (
    QPropertyAnimation, QEasingCurve, QTimer, QObject,
    QPoint, QRect,
)
from PySide6.QtWidgets import (
    QWidget, QScrollBar, QGraphicsOpacityEffect, QGraphicsBlurEffect,
)
from PySide6.QtGui import QColor

from src.gui.animations._constants import (
    EASE_SOFT, EASE_SPRING, EASE_ACCENT, EASE_INOUT_Q, EASE_INOUTSINE,
    DURATION_BUBBLE, DURATION_FADE, DURATION_SCROLL, DURATION_BUTTON,
)
from src.gui.animations._helpers import _stop_existing_anim, _ensure_opacity_effect


# ============================================================================
# 1. 淡入 / 淡出 — 最基础动画 — — — — — — — — — — — — — — — — — — — — — —
# ============================================================================

def fade_in(
    widget: QWidget,
    duration_ms: int = DURATION_FADE,
    target_opacity: float = 1.0,
    easing: Optional[QEasingCurve] = None,
) -> None:
    """淡入 widget (0.0 -> target_opacity)。

    Args:
        widget: 目标 widget
        duration_ms: 动画时长 (毫秒)，默认 180ms
        target_opacity: 最终不透明度 (0.0-1.0)，默认 1.0
        easing: 缓动曲线，默认 OutCubic
    """
    if easing is None:
        easing = EASE_SOFT
    _stop_existing_anim(widget, "_ds_fade_anim")
    effect = _ensure_opacity_effect(widget, 0.0)
    if effect is None:
        return

    anim = QPropertyAnimation(effect, b"opacity", parent=widget)
    anim.setDuration(duration_ms)
    anim.setStartValue(0.0)
    anim.setEndValue(target_opacity)
    anim.setEasingCurve(easing)
    widget._ds_fade_anim = anim

    # 动画结束后移除 effect 避免 QScrollArea 子 widget 渲染冲突
    def _cleanup() -> None:
        try:
            widget._ds_fade_anim = None
            if widget:
                widget.setGraphicsEffect(None)
        except Exception:
            pass
    anim.finished.connect(_cleanup)
    anim.start(QPropertyAnimation.DeleteWhenStopped)


def fade_out(
    widget: QWidget,
    duration_ms: int = DURATION_FADE,
    on_finished: Optional[Callable[[], None]] = None,
) -> None:
    """淡出 widget (1.0 -> 0.0)，结束后可选回调。默认 180ms OutCubic。"""
    _stop_existing_anim(widget, "_ds_fade_anim")
    effect = _ensure_opacity_effect(widget, 1.0)
    if effect is None:
        if on_finished:
            try:
                on_finished()
            except Exception:
                pass
        return

    anim = QPropertyAnimation(effect, b"opacity", parent=widget)
    anim.setDuration(duration_ms)
    anim.setStartValue(1.0)
    anim.setEndValue(0.0)
    anim.setEasingCurve(EASE_SOFT)
    widget._ds_fade_anim = anim

    def _cleanup() -> None:
        try:
            widget._ds_fade_anim = None
            if widget:
                widget.setGraphicsEffect(None)
        except Exception:
            pass
        if on_finished:
            try:
                on_finished()
            except Exception:
                pass

    anim.finished.connect(_cleanup)
    anim.start(QPropertyAnimation.DeleteWhenStopped)


# ============================================================================
# 2. 高斯模糊入场/出场 — 组合 opacity + blur（同步动画）
# ============================================================================

def blur_fade_in(
    widget: QWidget,
    duration_ms: int = DURATION_FADE,
    target_opacity: float = 1.0,
    easing: Optional[QEasingCurve] = None,
) -> None:
    """高斯模糊入场 (从透明+模糊 -> 清晰+不透明)。

    技术说明: QGraphicsBlurEffect 不能与 QGraphicsOpacityEffect 同时应用，
    因为一个 widget 只能有一个 graphics effect。这里采用:
      - 用 QGraphicsBlurEffect 实现 blur (radius 12->0)
      - opacity 动画通过 widget 的 windowOpacity 实现 (仅 top-level 有效)
      - fallback: 如果无法设置 windowOpacity，则仅使用 blur
    """
    if easing is None:
        easing = EASE_SPRING

    _stop_existing_anim(widget, "_ds_blur_anim")
    try:
        widget.setGraphicsEffect(None)
    except Exception:
        pass

    # blur 动画
    blur = QGraphicsBlurEffect(widget)
    blur.setBlurRadius(12.0)
    blur.setBlurHints(QGraphicsBlurEffect.QualityHint)
    widget.setGraphicsEffect(blur)

    anim = QPropertyAnimation(blur, b"blurRadius", parent=widget)
    anim.setDuration(duration_ms)
    anim.setStartValue(12.0)
    anim.setEndValue(0.0)
    anim.setEasingCurve(easing)
    widget._ds_blur_anim = anim

    # 清理
    def _cleanup() -> None:
        try:
            if widget and widget.isVisible():
                widget.setGraphicsEffect(None)
                widget._ds_blur_anim = None
        except Exception:
            pass
    anim.finished.connect(_cleanup)
    anim.start(QPropertyAnimation.DeleteWhenStopped)


def blur_fade_out(
    widget: QWidget,
    duration_ms: int = DURATION_FADE,
    on_finished: Optional[Callable[[], None]] = None,
) -> None:
    """高斯模糊出场 (从清晰+不透明->模糊+透明)。默认 180ms OutCubic。"""
    _stop_existing_anim(widget, "_ds_blur_anim")
    try:
        widget.setGraphicsEffect(None)
    except Exception:
        pass

    blur = QGraphicsBlurEffect(widget)
    blur.setBlurRadius(0.0)
    blur.setBlurHints(QGraphicsBlurEffect.QualityHint)
    widget.setGraphicsEffect(blur)

    anim = QPropertyAnimation(blur, b"blurRadius", parent=widget)
    anim.setDuration(duration_ms)
    anim.setStartValue(0.0)
    anim.setEndValue(8.0)
    anim.setEasingCurve(EASE_SOFT)
    widget._ds_blur_anim = anim

    def _cleanup() -> None:
        try:
            if widget and widget.isVisible():
                widget.setGraphicsEffect(None)
                widget._ds_blur_anim = None
        except Exception:
            pass
        if on_finished:
            try:
                on_finished()
            except Exception:
                pass
    anim.finished.connect(_cleanup)
    anim.start(QPropertyAnimation.DeleteWhenStopped)


# ============================================================================
# 3. 滑动入场 / 出场 — 仅改变位置 — — — — — — — — — — — — — — — — — — — —
# ============================================================================

def slide_in(
    widget: QWidget,
    direction: str = "up",
    duration_ms: int = DURATION_BUBBLE,
    distance_px: int = 40,
    easing: Optional[QEasingCurve] = None,
) -> None:
    """widget 从指定方向滑入。

    Args:
        direction: 'left' | 'right' | 'up' | 'down'
        distance_px: 滑动距离 (像素)
    """
    if easing is None:
        easing = EASE_SPRING

    original_pos = widget.pos()
    dx, dy = 0, 0
    if direction == "left":
        dx = -distance_px
    elif direction == "right":
        dx = distance_px
    elif direction == "up":
        dy = -distance_px
    elif direction == "down":
        dy = distance_px

    # 设置初始偏移位置
    widget.move(original_pos.x() + dx, original_pos.y() + dy)

    _stop_existing_anim(widget, "_ds_slide_anim")
    anim = QPropertyAnimation(widget, b"pos", parent=widget)
    anim.setDuration(duration_ms)
    anim.setStartValue(QPoint(original_pos.x() + dx, original_pos.y() + dy))
    anim.setEndValue(original_pos)
    anim.setEasingCurve(easing)
    widget._ds_slide_anim = anim

    def _cleanup() -> None:
        try:
            widget._ds_slide_anim = None
        except Exception:
            pass
    anim.finished.connect(_cleanup)
    anim.start(QPropertyAnimation.DeleteWhenStopped)


def slide_out(
    widget: QWidget,
    direction: str = "right",
    duration_ms: int = DURATION_BUBBLE,
    distance_px: int = 60,
    on_finished: Optional[Callable[[], None]] = None,
) -> None:
    """widget 滑出指定方向。默认 180ms OutCubic。"""
    easing = EASE_ACCENT
    original_pos = widget.pos()
    dx, dy = 0, 0
    if direction == "left":
        dx = -distance_px
    elif direction == "right":
        dx = distance_px
    elif direction == "up":
        dy = -distance_px
    elif direction == "down":
        dy = distance_px

    _stop_existing_anim(widget, "_ds_slide_anim")
    anim = QPropertyAnimation(widget, b"pos", parent=widget)
    anim.setDuration(duration_ms)
    anim.setStartValue(original_pos)
    anim.setEndValue(QPoint(original_pos.x() + dx, original_pos.y() + dy))
    anim.setEasingCurve(easing)
    widget._ds_slide_anim = anim

    def _cleanup() -> None:
        try:
            widget._ds_slide_anim = None
        except Exception:
            pass
    anim.finished.connect(_cleanup)
    if on_finished:
        anim.finished.connect(on_finished)
    anim.start(QPropertyAnimation.DeleteWhenStopped)


# ============================================================================
# 4. 组合入场 / 出场 — slide + fade（同步进行）
# ============================================================================

def enter_anim(
    widget: QWidget,
    direction: str = "up",
    duration_ms: int = DURATION_BUBBLE,
    distance_px: int = 30,
    blur: bool = False,
    easing: Optional[QEasingCurve] = None,
) -> None:
    """高级入场动画: 同时 slide + fade + (可选) blur。默认 180ms。"""
    chosen_easing = easing or EASE_SPRING
    slide_in(widget, direction, duration_ms, distance_px, chosen_easing)
    fade_in(widget, duration_ms)
    if blur:
        blur_fade_in(widget, duration_ms)


def exit_anim(
    widget: QWidget,
    direction: str = "right",
    duration_ms: int = DURATION_FADE,
    distance_px: int = 40,
    blur: bool = False,
    on_finished: Optional[Callable[[], None]] = None,
) -> None:
    """高级出场动画: 同时 slide + fade + (可选) blur。默认 180ms。"""
    fade_out(widget, duration_ms, on_finished)
    slide_out(widget, direction, duration_ms, distance_px)
    if blur:
        blur_fade_out(widget, duration_ms)


# ============================================================================
# 5. 平滑滚动 (chat/scroll 滚动条位置)
# ============================================================================

def smooth_scroll(
    scrollbar: QScrollBar,
    target_value: int,
    duration_ms: int = DURATION_SCROLL,
    easing: Optional[QEasingCurve] = None,
) -> None:
    """平滑滚动 scrollbar 到目标位置（避免跳跃）。默认 250ms。"""
    if easing is None:
        easing = EASE_INOUT_Q

    current = scrollbar.value()
    if abs(target_value - current) < 5:
        scrollbar.setValue(target_value)
        return

    parent = scrollbar.parent() or scrollbar
    _stop_existing_anim(scrollbar, "_ds_scroll_anim")
    anim = QPropertyAnimation(scrollbar, b"value", parent=parent)
    anim.setDuration(duration_ms)
    anim.setStartValue(current)
    anim.setEndValue(target_value)
    anim.setEasingCurve(easing)
    scrollbar._ds_scroll_anim = anim

    def _cleanup() -> None:
        try:
            scrollbar._ds_scroll_anim = None
        except Exception:
            pass
    anim.finished.connect(_cleanup)
    anim.start(QPropertyAnimation.DeleteWhenStopped)


# ============================================================================
# 6. 缩放弹入 / 弹出 (spring-like)
# ============================================================================

def pop_in(
    widget: QWidget,
    duration_ms: int = DURATION_BUBBLE,
    start_scale: float = 0.85,
    easing: Optional[QEasingCurve] = None,
) -> None:
    """widget 从 start_scale 弹出到 1.0，模拟 spring 效果。

    适用于对话、气泡、Slash Popup 等。默认 180ms。"""
    if easing is None:
        easing = EASE_SPRING

    original_geometry = widget.geometry()
    cx = original_geometry.center()

    # 从中心缩小
    scaled_w = int(original_geometry.width() * start_scale)
    scaled_h = int(original_geometry.height() * start_scale)
    start_rect = QRect(
        cx.x() - scaled_w // 2,
        cx.y() - scaled_h // 2,
        scaled_w,
        scaled_h)
    widget.setGeometry(start_rect)

    _stop_existing_anim(widget, "_ds_pop_anim")
    anim = QPropertyAnimation(widget, b"geometry", parent=widget)
    anim.setDuration(duration_ms)
    anim.setStartValue(start_rect)
    anim.setEndValue(original_geometry)
    anim.setEasingCurve(easing)
    widget._ds_pop_anim = anim

    def _cleanup() -> None:
        try:
            widget._ds_pop_anim = None
        except Exception:
            pass
    anim.finished.connect(_cleanup)
    anim.start(QPropertyAnimation.DeleteWhenStopped)


def pop_out(
    widget: QWidget,
    duration_ms: int = DURATION_FADE,
    on_finished: Optional[Callable[[], None]] = None,
) -> None:
    """缩放淡出 (1.0 -> 0.6)。默认 180ms OutCubic。"""
    original_geometry = widget.geometry()
    cx = original_geometry.center()
    end_width = int(original_geometry.width() * 0.6)
    end_height = int(original_geometry.height() * 0.6)
    end_rect = QRect(cx.x() - end_width // 2,
                    cx.y() - end_height // 2,
                    end_width, end_height)

    _stop_existing_anim(widget, "_ds_pop_anim")
    anim = QPropertyAnimation(widget, b"geometry", parent=widget)
    anim.setDuration(duration_ms)
    anim.setStartValue(original_geometry)
    anim.setEndValue(end_rect)
    anim.setEasingCurve(EASE_ACCENT)
    widget._ds_pop_anim = anim

    def _cleanup() -> None:
        try:
            widget._ds_pop_anim = None
        except Exception:
            pass
    anim.finished.connect(_cleanup)
    if on_finished:
        anim.finished.connect(on_finished)
    anim.start(QPropertyAnimation.DeleteWhenStopped)


# ============================================================================
# 7. 背景颜色渐变动画 (通过 QSS 定时器实现)
# ============================================================================

def animate_background(
    widget: QWidget,
    from_color_str: str,
    to_color_str: str,
    duration_ms: int = DURATION_BUTTON,
) -> None:
    """背景颜色渐变动画。默认 80ms，快速响应 hover。

    注: 此函数通过逐帧修改 styleSheet(`background:#XXX`) 实现颜色渐变。
    对已经有复杂 QSS 的 widget 可能会干扰，请确保 widget 仅使用简单的背景色。
    """
    from_c = QColor(from_color_str)
    to_c = QColor(to_color_str)
    steps = max(int(duration_ms / 16), 4)
    interval_ms = duration_ms // steps

    current_step = [0]

    def _step() -> None:
        current_step[0] += 1
        t = current_step[0] / steps
        # OutCubic 缓动: easeOutCubic = 1 - (1-t)^3
        eased_t = 1.0 - (1.0 - t) ** 3.0
        r = int(from_c.red() + (to_c.red() - from_c.red()) * eased_t)
        g = int(from_c.green() + (to_c.green() - from_c.green()) * eased_t)
        b = int(from_c.blue() + (to_c.blue() - from_c.blue()) * eased_t)
        hex_color = "#{:02X}{:02X}{:02X}".format(r, g, b)

        try:
            import re
            current_ss = widget.styleSheet()
            new_ss = re.sub(r'background:\s*#[0-9a-fA-F]{6}',
                              'background:' + hex_color, current_ss)
            if 'background:' not in new_ss:
                new_ss = current_ss.rstrip() + " background:" + hex_color + ";"
            widget.setStyleSheet(new_ss)
        except Exception:
            return

        if current_step[0] < steps:
            QTimer.singleShot(interval_ms, _step)

    QTimer.singleShot(0, _step)


# ============================================================================
# 8. 呼吸光效 — 持续运行的 opacity 呼吸
# ============================================================================

class _BreathingCycle(QObject):
    """呼吸光效 - 持续运行的 opacity 呼吸动画。

    实现方式: 两个阶段 (0.7 -> 1.0) 和 (1.0 -> 0.7) 循环切换。
    每个阶段用 InOutSine 缓动曲线形成自然的呼吸节奏。
    """

    def __init__(self, widget: QWidget, cycle_ms: int = 2400) -> None:
        super().__init__(widget)
        self.widget: QWidget = widget
        self.cycle_ms: int = cycle_ms
        self._active: bool = True

        # 设置 effect
        self.effect: QGraphicsOpacityEffect = QGraphicsOpacityEffect(widget)
        self.effect.setOpacity(0.7)
        widget.setGraphicsEffect(self.effect)

        # 动画
        self._anim: QPropertyAnimation = QPropertyAnimation(self.effect, b"opacity", parent=self)
        self._anim.setDuration(cycle_ms // 2)
        self._anim.setStartValue(0.7)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.InOutSine)
        self._anim.finished.connect(self._on_cycle_finished)
        self._anim.start(QPropertyAnimation.KeepWhenStopped)

    def _on_cycle_finished(self) -> None:
        if not self._active:
            return
        # 反向切换
        if self._anim.startValue() == 0.7:
            self._anim.setStartValue(1.0)
            self._anim.setEndValue(0.7)
        else:
            self._anim.setStartValue(0.7)
            self._anim.setEndValue(1.0)
        self._anim.start(QPropertyAnimation.KeepWhenStopped)

    def stop(self) -> None:
        self._active = False
        try:
            self._anim.stop()
            if self.widget and self.widget.isVisible():
                self.widget.setGraphicsEffect(None)
        except Exception:
            pass


def breathe(widget: QWidget, cycle_ms: int = 2400) -> _BreathingCycle:
    """开启呼吸光效 - 返回管理对象，调用 .stop() 可停止。

    典型用法: 鲸鱼图标 thinking 模式下开启呼吸光效。
    """
    return _BreathingCycle(widget, cycle_ms)


# ============================================================================
# 9. Pulse 脉冲动画（用于 done! 徽章 / 小鲸鱼头像 / 状态指示器）
# ============================================================================

def pulse(
    widget: QWidget,
    duration_ms: int = 1200,
    cycles: int = 2,
    min_opacity: float = 0.4,
) -> None:
    """脉冲动画 —— 用于 done! 徽章 / 状态指示器。

    Args:
        widget: 目标 widget
        duration_ms: 单次脉冲周期时长，默认 1200ms
        cycles: 循环次数（int 或 -1 表示无限循环）
        min_opacity: 最低透明度，默认 0.4
    """
    try:
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(1.0)
        widget.setGraphicsEffect(effect)

        anim = QPropertyAnimation(effect, b"opacity", parent=widget)
        anim.setDuration(int(duration_ms // 2))
        anim.setStartValue(1.0)
        anim.setEndValue(float(min_opacity))
        anim.setEasingCurve(EASE_INOUTSINE)  # 正弦波，呼吸感

        # 配置循环：来回 + 循环次数
        loop_count = int(cycles) * 2 if cycles > 0 else -1
        anim.start(QPropertyAnimation.DeleteWhenStopped if loop_count > 0 else QPropertyAnimation.KeepWhenStopped)

        if loop_count > 1:
            # 用 QTimer 控制多次脉冲
            def _next_pulse(remaining: int = loop_count - 1,
                            current: float = float(min_opacity),
                            target: float = 1.0) -> None:
                if remaining <= 0:
                    try:
                        widget.setGraphicsEffect(None)
                    except Exception:
                        pass
                    return
                try:
                    effect.setOpacity(current)
                except RuntimeError:
                    return
                next_anim = QPropertyAnimation(effect, b"opacity", parent=widget)
                next_anim.setDuration(int(duration_ms // 2))
                next_anim.setStartValue(current)
                next_anim.setEndValue(target)
                next_anim.setEasingCurve(EASE_INOUTSINE)
                next_anim.start(QPropertyAnimation.DeleteWhenStopped)
                next_target = min_opacity if target == 1.0 else 1.0
                next_anim.finished.connect(lambda r=remaining-1, t=next_target, c=target:
                                           _next_pulse(r, c, t))

            anim.finished.connect(lambda: _next_pulse(loop_count - 1, float(min_opacity), 1.0))
        elif loop_count == -1:
            # 无限循环：动画结束后重新启动反向脉冲
            def _loop_pulse(current: float = float(min_opacity),
                            target: float = 1.0) -> None:
                if not widget or not effect:
                    return
                try:
                    effect.setOpacity(current)
                except RuntimeError:
                    return
                loop_anim = QPropertyAnimation(effect, b"opacity", parent=widget)
                loop_anim.setDuration(int(duration_ms // 2))
                loop_anim.setStartValue(current)
                loop_anim.setEndValue(target)
                loop_anim.setEasingCurve(EASE_INOUTSINE)
                loop_anim.start(QPropertyAnimation.DeleteWhenStopped)
                next_target = min_opacity if target == 1.0 else 1.0
                loop_anim.finished.connect(lambda c=target, t=next_target: _loop_pulse(c, t))

            anim.finished.connect(lambda: _loop_pulse(float(min_opacity), 1.0))
        else:
            anim.finished.connect(lambda: (
                widget.setGraphicsEffect(None) if widget else None
            ))
    except Exception:
        pass


__all__ = [
    "fade_in",
    "fade_out",
    "blur_fade_in",
    "blur_fade_out",
    "slide_in",
    "slide_out",
    "enter_anim",
    "exit_anim",
    "smooth_scroll",
    "pop_in",
    "pop_out",
    "animate_background",
    "breathe",
    "pulse",
    "_BreathingCycle",
]
