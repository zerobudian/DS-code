# -*- coding: utf-8 -*-
"""动画友好的 widget 组件 — 自动入场/出场 / 颜色过渡 / hover 卡片。

包含组件:
  - AnimatedWidget:      自动淡入/淡出的 QWidget
  - AnimatedButton:      hover 微放大 + 按下缩小 的 QPushButton
  - _ButtonPixmapCache:  AnimatedButton 默认外观的简易缓存
  - SmoothColorButton:   颜色平滑过渡按钮 (QTimer + OutCubic)
  - HoverColorButton:    QPropertyAnimation 插值背景色的按钮
  - HoverCard:           hover / selected 态背景色动画卡片
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from PySide6.QtCore import (
    QPropertyAnimation, QEasingCurve, QTimer, Property, Qt, Signal, QEvent,
)
from PySide6.QtWidgets import (
    QWidget, QPushButton, QFrame,
)
from PySide6.QtGui import (
    QColor, QPainter, QPixmap,
    QShowEvent, QCloseEvent, QEnterEvent, QMouseEvent, QPaintEvent,
)

from src.gui.style import COLORS, FONT_STACK
from src.gui.animations._constants import (
    EASE_SOFT, EASE_SPRING, EASE_INOUT_Q,
    DURATION_BUBBLE, DURATION_BUTTON, DURATION_FADE, DURATION_PANEL,
)
from src.gui.animations._effects import fade_in, fade_out


# ============================================================================
# 1. 动画友好的 QWidget 封装 — 自动入场/出场
# ============================================================================

class AnimatedWidget(QWidget):
    """动画友好的 QWidget — 自动淡入/淡出。"""
    clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

    def showEvent(self, event: QShowEvent) -> None:
        fade_in(self, duration_ms=DURATION_FADE)
        super().showEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        fade_out(self, duration_ms=DURATION_FADE)
        super().closeEvent(event)


# ============================================================================
# 2. 动画按钮 — 悬停微放大 + 按下缩小
# ============================================================================

class AnimatedButton(QPushButton):
    """动画友好的按钮。

    关键改进（对比旧版 geometry 动画）：
    1. 使用 QGraphicsOpacityEffect 控制"高光"透明度（hover: 1.0->1.08感觉）
    2. 使用 paintEvent 内部缩放绘制，不改变 widget 位置，不影响布局
    3. hover / press / release 都是非线性缓动（OutBack / OutCubic）
    4. 颜色过渡通过 QTimer 插值 styleSheet，避免硬切换
    """

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        # 不透明绘制，减少 flicker；缩放动画在 paintEvent 内完成
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

        # 内部 scale 属性 — 用于 paintEvent 缩放绘制
        self._scale: float = 1.0
        self._scale_anim: QPropertyAnimation = QPropertyAnimation(self, b"_scale_prop", parent=self)
        self._scale_anim.setDuration(DURATION_BUTTON)
        self._scale_anim.setEasingCurve(EASE_SPRING)

        # 背景颜色插值（hover / pressed 时用）
        self._hover_factor: float = 0.0  # 0.0 = normal, 1.0 = hover
        self._hover_anim: QPropertyAnimation = QPropertyAnimation(self, b"_hover_prop", parent=self)
        self._hover_anim.setDuration(DURATION_BUTTON)
        self._hover_anim.setEasingCurve(EASE_SOFT)

    # --- scale property (供动画使用) ---
    def _get_scale(self) -> float:
        return self._scale

    def _set_scale(self, value: float) -> None:
        self._scale = value
        self.update()

    _scale_prop = Property(float, _get_scale, _set_scale)

    # --- hover factor property ---
    def _get_hover(self) -> float:
        return self._hover_factor

    def _set_hover(self, value: float) -> None:
        self._hover_factor = value
        self.update()

    _hover_prop = Property(float, _get_hover, _set_hover)

    def _animate_scale(
        self,
        target: float,
        duration_ms: int = 180,
        easing: Optional[QEasingCurve] = None,
    ) -> None:
        self._scale_anim.stop()
        self._scale_anim.setDuration(duration_ms)
        self._scale_anim.setStartValue(self._scale)
        self._scale_anim.setEndValue(target)
        if easing is not None:
            self._scale_anim.setEasingCurve(easing)
        else:
            self._scale_anim.setEasingCurve(EASE_SPRING)
        self._scale_anim.start(QPropertyAnimation.KeepWhenStopped)

    def _animate_hover(self, target: float, duration_ms: int = 150) -> None:
        self._hover_anim.stop()
        self._hover_anim.setDuration(duration_ms)
        self._hover_anim.setStartValue(self._hover_factor)
        self._hover_anim.setEndValue(target)
        self._hover_anim.setEasingCurve(EASE_SOFT)
        self._hover_anim.start(QPropertyAnimation.KeepWhenStopped)

    # --- 事件 ---
    def enterEvent(self, event: QEnterEvent) -> None:
        super().enterEvent(event)
        self._animate_scale(1.02, DURATION_BUTTON, EASE_SPRING)
        self._animate_hover(1.0, DURATION_BUTTON)

    def leaveEvent(self, event: QEvent) -> None:
        super().leaveEvent(event)
        self._animate_scale(1.0, DURATION_BUTTON, EASE_INOUT_Q)
        self._animate_hover(0.0, DURATION_BUTTON)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        super().mousePressEvent(event)
        self._animate_scale(0.96, DURATION_BUTTON, QEasingCurve.OutQuad)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        self._animate_scale(1.02, DURATION_BUTTON, EASE_SPRING)
        QTimer.singleShot(DURATION_BUTTON, lambda: self._animate_scale(1.0, DURATION_BUTTON, EASE_INOUT_Q))

    def paintEvent(self, event: QPaintEvent) -> None:
        """以中心为基准缩放绘制。"""
        if abs(self._scale - 1.0) < 0.001:
            # 无缩放 — 用默认绘制
            super().paintEvent(event)
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        # 以中心为基准缩放
        p.translate(cx, cy)
        p.scale(self._scale, self._scale)
        p.translate(-cx, -cy)
        # 让 Qt 继续绘制按钮本身（通过 grab）
        # 简化方案：调用父类用另一个 painter 绘制到当前 painter 的 transformed 区域
        # 由于无法直接复用 super().paintEvent 到已 transformed 的 painter，
        # 这里通过临时 pixmap 抓取默认绘制，再缩放绘制
        pm = _ButtonPixmapCache.get(self)
        if pm is None:
            pm = QPixmap(w, h)
            pm.fill(Qt.transparent)
            pp = QPainter(pm)
            pp.setRenderHint(QPainter.Antialiasing, True)
            # 调用默认 QStyle 绘制
            from PySide6.QtWidgets import QStyle, QStyleOptionButton
            opt = QStyleOptionButton()
            opt.initFrom(self)
            opt.text = self.text()
            # hover 状态标记 — 用于 style sheet
            if self._hover_factor > 0.5:
                opt.state |= QStyle.State_MouseOver
            self.style().drawControl(QStyle.CE_PushButton, opt, pp, self)
            pp.end()
            _ButtonPixmapCache.put(self, pm, w, h)
        p.drawPixmap(0, 0, pm)
        p.end()


class _ButtonPixmapCache:
    """按钮默认外观的简易缓存。避免每帧都重新绘制。"""
    _cache: Dict[int, Tuple[Optional[QPixmap], int, int]] = {}  # key(id(widget)) -> (pm, w, h)

    @classmethod
    def get(cls, widget: QWidget) -> Optional[QPixmap]:
        key = id(widget)
        entry = cls._cache.get(key)
        if entry is None:
            return None
        pm, w, h = entry
        if pm is None or pm.width() != widget.width() or pm.height() != widget.height():
            return None
        return pm

    @classmethod
    def put(cls, widget: QWidget, pm: QPixmap, w: int, h: int) -> None:
        # 简单清理：超过 20 个就清掉一半
        if len(cls._cache) > 20:
            # 保留最新的 10 个
            items = list(cls._cache.keys())
            for k in items[:-10]:
                del cls._cache[k]
        cls._cache[id(widget)] = (pm, w, h)

    @classmethod
    def invalidate(cls, widget: QWidget) -> None:
        key = id(widget)
        if key in cls._cache:
            del cls._cache[key]


# ============================================================================
# 3. SmoothColorButton — 颜色平滑过渡按钮（推荐用于主要操作按钮）
# ============================================================================

class SmoothColorButton(QPushButton):
    """带颜色平滑过渡的按钮。

    用法：
        btn = SmoothColorButton("确认并执行")
        btn.set_colors(
            normal_bg=COLORS['PRIM_DARK'],
            hover_bg=_darken(COLORS['PRIM_DARK'], 0.88),
            pressed_bg=_darken(COLORS['PRIM_DARK'], 0.76),
            normal_text=COLORS['CARD'],
            border_radius="16px",
            border="none",
        )
        # 或直接用 secondary 风格：
        btn.set_secondary_style(bg=COLORS['CARD'], text=COLORS['TEXT'])

    特性：
    1. hover / press 背景色平滑插值（QTimer + OutCubic）
    2. 鼠标悬停时轻微放大（1.0 -> 1.02），按下时缩小（0.96）
    3. 所有动画都是异步的，不阻塞主线程
    4. 不改变 widget 实际 geometry，只在 paintEvent 内缩放
    """

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        # 不透明绘制减少 flicker；颜色过渡在 paintEvent/styleSheet 内完成
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

        # 默认色 — 用 primary 风格
        self._normal_bg: Optional[str] = None
        self._hover_bg: Optional[str] = None
        self._pressed_bg: Optional[str] = None
        self._text_color: str = COLORS['CARD']
        self._border_radius: str = "16px"
        self._border: str = "none"
        # 动画状态
        self._bg_anim: Optional[QColor] = None      # 当前背景色（QColor）
        self._bg_timer: Optional[QTimer] = None     # 背景色插值定时器
        self._bg_steps: int = 0
        self._bg_step_i: int = 0
        self._bg_from: Optional[QColor] = None
        self._bg_to: Optional[QColor] = None

        # 缩放（用于 hover/press 的微妙效果）
        self._scale: float = 1.0
        self._scale_anim: QPropertyAnimation = QPropertyAnimation(self, b"_sbtn_scale", parent=self)
        self._scale_anim.setDuration(DURATION_BUTTON)
        self._scale_anim.setEasingCurve(EASE_SPRING)

        # 文本阴影 — hover 时加深，作为"呼吸"效果
        self._opacity: float = 1.0

        # 初始状态 — 不配置色也能工作
        self._apply_style_sheet(self._text_color, None, self._border_radius, self._border)

    # --- scale property ---
    def _get_scale_sbtn(self) -> float:
        return self._scale

    def _set_scale_sbtn(self, value: float) -> None:
        self._scale = value
        self.update()

    _sbtn_scale = Property(float, _get_scale_sbtn, _set_scale_sbtn)

    # --- 配置 ---
    def set_colors(
        self,
        normal_bg: str,
        hover_bg: str,
        pressed_bg: str,
        normal_text: str = COLORS['CARD'],
        border_radius: str = "16px",
        border: str = "none",
    ) -> None:
        self._normal_bg = normal_bg
        self._hover_bg = hover_bg
        self._pressed_bg = pressed_bg
        self._text_color = normal_text
        self._border_radius = border_radius
        self._border = border
        self._apply_style_sheet(normal_text, normal_bg, border_radius, border)

    def set_primary_style(
        self,
        bg: str,
        hover_darken: float = 0.88,
        pressed_darken: float = 0.76,
        text: str = COLORS['CARD'],
        border_radius: str = "16px",
    ) -> None:
        """设置主按钮样式（蓝色大按钮风格）。"""
        def _h(hex_col: str, factor: float) -> str:
            c = hex_col.lstrip("#")
            r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
            r = max(0, min(255, int(r * factor)))
            g = max(0, min(255, int(g * factor)))
            b = max(0, min(255, int(b * factor)))
            return "#{:02X}{:02X}{:02X}".format(r, g, b)
        self.set_colors(
            normal_bg=bg,
            hover_bg=_h(bg, hover_darken),
            pressed_bg=_h(bg, pressed_darken),
            normal_text=text,
            border_radius=border_radius,
            border="none",
        )

    def set_secondary_style(
        self,
        bg: str,
        text: str = COLORS['TEXT'],
        hover_lighten: float = 1.08,
        pressed_lighten: float = 0.92,
        border_radius: str = "16px",
        border: str = "1px solid " + COLORS['BORDER'] + "",
    ) -> None:
        """设置次要按钮样式（白底灰边，悬停时略亮）。"""
        def _l(hex_col: str, factor: float) -> str:
            c = hex_col.lstrip("#")
            r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
            r = max(0, min(255, int(r * factor)))
            g = max(0, min(255, int(g * factor)))
            b = max(0, min(255, int(b * factor)))
            return "#{:02X}{:02X}{:02X}".format(r, g, b)
        self.set_colors(
            normal_bg=bg,
            hover_bg=_l(bg, hover_lighten),
            pressed_bg=_l(bg, pressed_lighten),
            normal_text=text,
            border_radius=border_radius,
            border=border,
        )

    # --- 内部 ---
    def _apply_style_sheet(
        self,
        text_color: str,
        bg_color: Optional[str],
        border_radius: str,
        border: str,
    ) -> None:
        bg = bg_color if bg_color else "transparent"
        self.setStyleSheet(
            "QPushButton{background:" + bg + ";"
            "color:" + text_color + ";"
            "border:" + border + ";"
            "border-radius:" + border_radius + ";"
            "padding:0 16px;"
            "font-size:14px;font-weight:500;"
            "font-family:" + FONT_STACK + ";}"
            "QPushButton:disabled{opacity:0.5;}"
        )

    def _animate_to_bg(
        self,
        target_color: str,
        duration_ms: int = DURATION_BUTTON,
    ) -> None:
        """平滑过渡背景色（使用 QTimer 逐帧插值）。默认 80ms。"""
        if target_color is None:
            return

        # 当前实际背景色 — 从 styleSheet 解析，若无法解析则用 normal_bg
        current_hex = self._parse_current_bg()
        if current_hex is None:
            current_hex = self._normal_bg or target_color

        if current_hex.lower() == target_color.lower():
            # 已经是目标色，无需动画
            return

        # 初始化定时器（若未初始化）
        if self._bg_timer is None:
            self._bg_timer = QTimer(self)
            self._bg_timer.timeout.connect(self._on_bg_step)

        self._bg_timer.stop()
        self._bg_from = QColor(current_hex)
        self._bg_to = QColor(target_color)
        # 16ms 每步 ~60fps
        self._bg_steps = max(int(duration_ms / 16), 3)
        self._bg_step_i = 0
        self._bg_timer.start(16)

    def _parse_current_bg(self) -> Optional[str]:
        ss = self.styleSheet()
        # 找第一个 background: #RRGGBB
        import re
        m = re.search(r"background\s*:\s*(#[0-9a-fA-F]{6})", ss)
        if m:
            return m.group(1)
        return None

    def _on_bg_step(self) -> None:
        self._bg_step_i += 1
        if self._bg_step_i > self._bg_steps or self._bg_from is None or self._bg_to is None:
            if self._bg_timer is not None:
                self._bg_timer.stop()
            # 动画结束 — 设置最终色
            if self._bg_to is not None:
                final_hex = "#{:02X}{:02X}{:02X}".format(
                    self._bg_to.red(), self._bg_to.green(), self._bg_to.blue())
                self._apply_style_sheet(self._text_color, final_hex,
                                        self._border_radius, self._border)
            return
        # 非线性缓动：easeOutCubic = 1 - (1-t)^3
        t = self._bg_step_i / self._bg_steps
        eased = 1.0 - (1.0 - t) ** 3.0
        r = int(self._bg_from.red() + (self._bg_to.red() - self._bg_from.red()) * eased)
        g = int(self._bg_from.green() + (self._bg_to.green() - self._bg_from.green()) * eased)
        b = int(self._bg_from.blue() + (self._bg_to.blue() - self._bg_from.blue()) * eased)
        r = max(0, min(255, r))
        g = max(0, min(255, g))
        b = max(0, min(255, b))
        hex_color = "#{:02X}{:02X}{:02X}".format(r, g, b)
        self._apply_style_sheet(self._text_color, hex_color,
                                self._border_radius, self._border)

    def _animate_scale(
        self,
        target: float,
        duration_ms: int = 160,
        easing: Optional[QEasingCurve] = None,
    ) -> None:
        self._scale_anim.stop()
        self._scale_anim.setDuration(duration_ms)
        self._scale_anim.setStartValue(self._scale)
        self._scale_anim.setEndValue(target)
        self._scale_anim.setEasingCurve(easing or EASE_SPRING)
        self._scale_anim.start(QPropertyAnimation.KeepWhenStopped)

    # --- 事件 ---
    def enterEvent(self, event: QEnterEvent) -> None:
        super().enterEvent(event)
        self._animate_to_bg(self._hover_bg, DURATION_BUTTON)
        self._animate_scale(1.02, DURATION_BUTTON, EASE_SPRING)

    def leaveEvent(self, event: QEvent) -> None:
        super().leaveEvent(event)
        self._animate_to_bg(self._normal_bg, DURATION_BUTTON)
        self._animate_scale(1.0, DURATION_BUTTON, EASE_INOUT_Q)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        super().mousePressEvent(event)
        self._animate_to_bg(self._pressed_bg, DURATION_BUTTON)
        self._animate_scale(0.96, DURATION_BUTTON, QEasingCurve.OutQuad)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        # 返回 hover 色（若鼠标仍在按钮内）或 normal 色
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        inside = self.rect().contains(pos)
        target = self._hover_bg if inside else self._normal_bg
        self._animate_to_bg(target, DURATION_BUTTON)
        self._animate_scale(1.02, DURATION_BUTTON, EASE_SPRING)
        QTimer.singleShot(DURATION_BUTTON, lambda: self._animate_scale(1.0, DURATION_BUTTON, EASE_INOUT_Q))

    def paintEvent(self, event: QPaintEvent) -> None:
        if abs(self._scale - 1.0) < 0.002:
            super().paintEvent(event)
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        p.translate(cx, cy)
        p.scale(self._scale, self._scale)
        p.translate(-cx, -cy)
        # 用 QStyle 绘制
        from PySide6.QtWidgets import QStyle, QStyleOptionButton
        opt = QStyleOptionButton()
        opt.initFrom(self)
        opt.text = self.text()
        self.style().drawControl(QStyle.CE_PushButton, opt, p, self)
        p.end()


# ============================================================================
# 4. 通用 hover 动画组件（无 QTimer 逐帧改 stylesheet）
# ============================================================================

class HoverColorButton(QPushButton):
    """带动画背景色的按钮：hover/press/check 态用 QPropertyAnimation 插值背景色。

    与 SmoothColorButton 的区别：颜色过渡通过 QPropertyAnimation 的 QColor 属性完成，
    不使用 QTimer 逐帧修改 stylesheet，避免高并发/快速切换时的性能抖动。
    """

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)

        self._normal_bg: str = COLORS["CARD"]
        self._hover_bg: str = COLORS["PRIM_LIGHT"]
        self._pressed_bg: str = COLORS["PRIM"]
        self._checked_bg: str = COLORS["PRIM_DARK"]
        self._text_color: str = COLORS["TEXT"]
        self._checked_text_color: str = COLORS["CARD"]
        self._border_color: str = COLORS["BORDER"]

        self._border_radius: str = "16px"
        self._border_width: str = "1px"
        self._font_size: str = "13px"
        self._font_weight: str = "600"
        self._padding: str = "0 16px"

        self._hovered: bool = False
        self._bg_color: QColor = QColor(self._normal_bg)

        self._anim: QPropertyAnimation = QPropertyAnimation(self, b"_bg_color_prop", self)
        self._anim.setDuration(DURATION_BUTTON)
        self._anim.setEasingCurve(EASE_SOFT)

        self.toggled.connect(self._on_toggled)
        self._apply_style()

    def set_colors(
        self,
        normal_bg: str,
        hover_bg: str,
        pressed_bg: str,
        checked_bg: Optional[str] = None,
        text_color: Optional[str] = None,
        checked_text_color: Optional[str] = None,
        border_color: Optional[str] = None,
    ) -> None:
        self._normal_bg = normal_bg
        self._hover_bg = hover_bg
        self._pressed_bg = pressed_bg
        if checked_bg is not None:
            self._checked_bg = checked_bg
        if text_color is not None:
            self._text_color = text_color
        if checked_text_color is not None:
            self._checked_text_color = checked_text_color
        if border_color is not None:
            self._border_color = border_color
        # 修复：将 _bg_color 同步到新的 normal_bg，避免初始渲染使用过时的默认颜色
        self._bg_color = QColor(self._normal_bg)
        self._apply_style()

    def set_style(
        self,
        border_radius: str = "16px",
        border_width: str = "1px",
        font_size: str = "13px",
        font_weight: str = "600",
        padding: str = "0 16px",
    ) -> None:
        self._border_radius = border_radius
        self._border_width = border_width
        self._font_size = font_size
        self._font_weight = font_weight
        self._padding = padding
        self._apply_style()

    def _get_bg(self) -> QColor:
        return self._bg_color

    def _set_bg(self, color: QColor) -> None:
        self._bg_color = color
        self._apply_style()

    _bg_color_prop = Property(QColor, _get_bg, _set_bg)

    def _target_bg(self) -> str:
        if self.isCheckable() and self.isChecked():
            return self._checked_bg
        return self._hover_bg if self._hovered else self._normal_bg

    def _target_text(self) -> str:
        if self.isCheckable() and self.isChecked():
            return self._checked_text_color
        return self._text_color

    def _apply_style(self) -> None:
        bg = self._bg_color.name()
        text = self._target_text()
        self.setStyleSheet(
            f"QPushButton{{background:{bg};color:{text};"
            f"border:{self._border_width} solid {self._border_color};"
            f"border-radius:{self._border_radius};"
            f"font-size:{self._font_size};font-weight:{self._font_weight};"
            f"font-family:{FONT_STACK};padding:{self._padding};}}"
            f"QPushButton:disabled{{opacity:0.5;}}"
        )

    def enterEvent(self, event: QEnterEvent) -> None:
        self._hovered = True
        super().enterEvent(event)
        self._animate_to(self._target_bg())

    def leaveEvent(self, event: QEvent) -> None:
        self._hovered = False
        super().leaveEvent(event)
        self._animate_to(self._target_bg())

    def mousePressEvent(self, event: QMouseEvent) -> None:
        super().mousePressEvent(event)
        self._animate_to(self._pressed_bg)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        self._animate_to(self._target_bg())

    def _on_toggled(self, checked: bool) -> None:
        self._animate_to(self._target_bg())

    def _animate_to(self, target_hex: str) -> None:
        target = QColor(target_hex)
        self._anim.stop()
        self._anim.setStartValue(self._bg_color)
        self._anim.setEndValue(target)
        self._anim.start(QPropertyAnimation.KeepWhenStopped)


class HoverCard(QFrame):
    """带动画背景色的卡片容器：hover / selected 态用 QPropertyAnimation 插值背景色。

    用法：
        card = HoverCard(COLORS["CARD"], COLORS["PRIM"], COLORS["PRIM_LIGHT"],
                         COLORS["BORDER"], RADII["lg"])
        card.setObjectName("MyCard")
        layout = QVBoxLayout(card)
        ...
        card.set_selected(True)
    """

    def __init__(
        self,
        normal_bg: str,
        hover_bg: str,
        selected_bg: str,
        border_color: str,
        border_radius: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        # 背景不透明，减少 flicker（HoverCard 不使用 opacity effect）
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self._normal_bg: str = normal_bg
        self._hover_bg: str = hover_bg
        self._selected_bg: str = selected_bg
        self._border_color: str = border_color
        self._border_radius: str = border_radius
        self._selected: bool = False
        self._hovered: bool = False
        self._bg_color: QColor = QColor(normal_bg)

        self._anim: QPropertyAnimation = QPropertyAnimation(self, b"_bg_color_prop", self)
        self._anim.setDuration(120)
        self._anim.setEasingCurve(EASE_SOFT)
        self.setAttribute(Qt.WA_Hover, True)
        self._apply_style()

    def _get_bg(self) -> QColor:
        return self._bg_color

    def _set_bg(self, color: QColor) -> None:
        self._bg_color = color
        self._apply_style()

    _bg_color_prop = Property(QColor, _get_bg, _set_bg)

    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self._animate_to(self._target_bg())

    def is_selected(self) -> bool:
        return self._selected

    def _target_bg(self) -> str:
        if self._selected:
            return self._selected_bg
        return self._hover_bg if self._hovered else self._normal_bg

    def _apply_style(self) -> None:
        name = self.objectName() or "HoverCard"
        self.setStyleSheet(
            f"QFrame#{name}{{background:{self._bg_color.name()};"
            f"border:1px solid {self._border_color};"
            f"border-radius:{self._border_radius};}}"
        )

    def enterEvent(self, event: QEnterEvent) -> None:
        self._hovered = True
        super().enterEvent(event)
        self._animate_to(self._target_bg())

    def leaveEvent(self, event: QEvent) -> None:
        self._hovered = False
        super().leaveEvent(event)
        self._animate_to(self._target_bg())

    def _animate_to(self, target_hex: str) -> None:
        target = QColor(target_hex)
        self._anim.stop()
        self._anim.setStartValue(self._bg_color)
        self._anim.setEndValue(target)
        self._anim.start(QPropertyAnimation.KeepWhenStopped)


__all__ = [
    "AnimatedWidget",
    "AnimatedButton",
    "_ButtonPixmapCache",
    "SmoothColorButton",
    "HoverColorButton",
    "HoverCard",
]
