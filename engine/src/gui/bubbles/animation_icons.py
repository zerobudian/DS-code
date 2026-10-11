# -*- coding: utf-8 -*-
"""动画图标：AnimatedWhaleIcon（鲸鱼头顶动效）与 ToolAnimationIcon（工具调用动画）。"""
from __future__ import annotations

import math
from typing import List

from PySide6.QtCore import (
    Qt, Signal, QPropertyAnimation, QEasingCurve, QTimer, QAbstractAnimation,
    QPoint, Property, QObject,
)
from PySide6.QtGui import QPixmap, QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget, QFrame

from src.gui.style import RADII, COLORS, _cached_style
from src.gui.icon_factory import get_icon
from src.gui.animations import pop_in
from src.gui.bubbles._helpers import _img, _try_pm

# ======================================================================
# AnimatedWhaleIcon —— 带头顶动效的鲸鱼图标（用于推理/工具卡片）
# 四种动效模式:
#   'idle':        静态（只是鲸鱼）
#   'thinking':    🫧 头顶持续冒出小气泡上升+淡出 + 轻微浮动
#   'coding':      ⌨️  键盘图标弹跳+脉冲
#   'terminal':    💻  终端图标呼吸+光标闪烁
#   'reading':     📄  白纸图标轻微左右摆动
# ======================================================================
class AnimatedWhaleIcon(QFrame):
    """带 paintEvent 动画的鲸鱼图标。通过 set_mode('thinking') 切换。

    动效增强:
    1. 鲸鱼整体呼吸式浮动 (上下 2px, 18 帧周期)
    2. thinking 模式: 多个小气泡从头顶冒出，非线性上升 + 渐隐
    3. 涟漪扩散: 气泡产生位置有微妙涟漪
    4. coding/terminal/reading: 脉冲式呼吸 + 自然摆动
    5. 所有动画使用 sin 曲线和 OutCubic 非线性，确保自然过渡
    """

    clicked = Signal()

    def __init__(self, size: int = 36, parent=None):
        super().__init__(parent)
        self._size = size
        self._total_h = size + 24  # 顶部留 24px 给动效元素
        self.setFixedHeight(self._total_h)
        self.setFixedWidth(size)
        self.setStyleSheet("background:transparent;")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)

        # 预加载鲸鱼
        self._whale_pm = _try_pm(["whale_36.png", "whale_32.png", "whale.png"], size)
        if self._whale_pm.isNull():
            # 回退：程序画一只鲸鱼
            self._whale_pm = QPixmap(size, size)
            self._whale_pm.fill(Qt.transparent)
            p = QPainter(self._whale_pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setBrush(QColor(183, 220, 251))
            p.setPen(Qt.NoPen)
            p.drawEllipse(int(size * 0.1), int(size * 0.2), int(size * 0.8), int(size * 0.6))
            p.setBrush(QColor(255, 255, 255))
            p.drawEllipse(int(size * 0.6), int(size * 0.35), int(size * 0.1), int(size * 0.1))
            p.end()

        self._mode = "idle"
        self._frame = 0
        self._timer = None
        self._bubble_timer = None
        self._active_bubbles: List[QWidget] = []
        self._start_timer_if_needed()

    # ------------------------------------------------------------------
    def set_mode(self, mode: str):
        m = (mode or "idle").lower()
        if m == self._mode:
            return
        self._mode = m
        self._frame = 0
        if m != "thinking":
            self._cleanup_bubbles()
        self._start_timer_if_needed()

    # ------------------------------------------------------------------
    def _start_timer_if_needed(self):
        if self._mode == "idle":
            if self._timer is not None:
                self._timer.stop()
                self._timer = None
            self._cleanup_bubbles()
            self.update()
            return
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._on_tick)
        self._timer.start(33)  # ~30 FPS, 流畅
        self._stop_bubble_timer()
        self._cleanup_bubbles()

    # ------------------------------------------------------------------
    def _on_tick(self):
        self._frame += 1
        self.update()

    # ------------------------------------------------------------------
    def _start_bubble_timer(self):
        if self._bubble_timer is None:
            self._bubble_timer = QTimer(self)
            self._bubble_timer.timeout.connect(self._spawn_thinking_bubble)
        if not self._bubble_timer.isActive():
            self._bubble_timer.start(260)

    def _stop_bubble_timer(self):
        if self._bubble_timer is not None and self._bubble_timer.isActive():
            self._bubble_timer.stop()

    def _cleanup_bubbles(self):
        for bubble in list(self._active_bubbles):
            try:
                bubble.deleteLater()
            except Exception:
                pass
        self._active_bubbles.clear()
        self._stop_bubble_timer()

    def _spawn_thinking_bubble(self):
        """已禁用——QGraphicsOpacityEffect 在 QScrollArea 内会导致 segfault。"""
        pass

    # ------------------------------------------------------------------
    # 绘制
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        w = self.width()
        h = self.height()

        # --- 鲸鱼呼吸式浮动 ---
        # 使用 sin(0..2π) 曲线: float_offset -2..+2 px
        float_offset = 2.0 * math.sin(2 * math.pi * (self._frame % 60) / 60)
        # 整体呼吸缩放 (0.97..1.03)
        breath_scale = 1.0 + 0.03 * math.sin(2 * math.pi * (self._frame % 60) / 60)

        # 鲸鱼位置 (固定在底部中央, 带浮动)
        whale_y = h - self._size + float_offset
        whale_x = (w - self._size) // 2

        # 根据模式调整绘制（并在顶部画浮动元素）
        mode = self._mode
        frame = self._frame

        if mode == "thinking":
            self._paint_thinking(p, w, h, whale_x, whale_y, frame)
        elif mode == "coding":
            self._paint_coding(p, w, h, whale_x, whale_y, frame)
        elif mode == "terminal":
            self._paint_terminal(p, w, h, whale_x, whale_y, frame)
        elif mode == "reading":
            self._paint_reading(p, w, h, whale_x, whale_y, frame)

        # 绘制鲸鱼（顶部元素叠在鲸鱼上面）
        if not self._whale_pm.isNull():
            # 呼吸式缩放 — 以中心为基准放大/缩小
            s = int(self._size * breath_scale)
            draw_x = whale_x + (self._size - s) // 2
            draw_y = whale_y + (self._size - s) // 2
            scaled = self._whale_pm.scaled(s, s, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            p.drawPixmap(draw_x, draw_y, scaled)
        else:
            # 兜底：蓝色圆
            p.setBrush(QColor(183, 220, 251))
            p.setPen(Qt.NoPen)
            p.drawEllipse(whale_x, whale_y, self._size, self._size)

        p.end()

    # ------------------------------------------------------------------
    def _paint_thinking(self, p, w, h, wx, wy, frame):
        """顶部涟漪扩散（小气泡本身已改为 QPropertyAnimation 子控件）。"""
        cx = w / 2
        top_y = wy + 3  # 顶部位置 (稍微上一点)

        # 涟漪扩散 — 在气泡产生位置画 2 个扩散圆环
        ripple_phase = (frame % 30) / 30
        ripple_r = 4 + ripple_phase * 14
        ripple_alpha = 0.5 * (1.0 - ripple_phase)
        ripple_color = QColor(183, 220, 251)
        ripple_color.setAlphaF(ripple_alpha)
        p.setBrush(Qt.NoBrush)
        pen = QPen(ripple_color)
        pen.setWidthF(1.0)
        p.setPen(pen)
        # 画两条同心圆弧 (形成波纹感)
        p.drawEllipse(int(cx - ripple_r), int(top_y + 3), int(ripple_r * 2), int(ripple_r))

    def _paint_coding(self, p, w, h, wx, wy, frame):
        """键盘图标在右侧弹入+缩放脉冲。"""
        from src.gui.icon_factory import draw_keyboard

        # 使用 sin 曲线做平滑脉冲 (0.95~1.08)
        pulse = 0.95 + 0.08 * math.sin(2 * math.pi * (frame % 24) / 24)
        size = int(self._size * 0.6)
        icon_x = wx + self._size - size * 0.3
        icon_y = wy - size * 0.2

        # alpha 呼吸 (0.85~1.0) — 用 cos 曲线平滑
        alpha = 0.85 + 0.15 * (0.5 + 0.5 * math.sin(2 * math.pi * (frame % 32) / 32))

        s = int(size * pulse)
        draw_keyboard(p, icon_x - (s - size) // 2, icon_y - (s - size) // 2, s, alpha)

    def _paint_terminal(self, p, w, h, wx, wy, frame):
        """终端图标在左侧轻微呼吸+光标闪烁。"""
        from src.gui.icon_factory import draw_terminal

        # 呼吸缩放 0.94~1.08 (呼吸周期 longer = 40 frames, 更自然)
        pulse = 0.94 + 0.1 * (0.5 + 0.5 * math.sin(2 * math.pi * (frame % 40) / 40))
        cursor_on = (frame % 16) < 8

        size = int(self._size * 0.6)
        icon_x = wx - size * 0.3
        icon_y = wy - size * 0.15
        s = int(size * pulse)
        draw_terminal(p, icon_x - (s - size) // 2, icon_y - (s - size) // 2, s, 0.9, cursor_on)

    def _paint_reading(self, p, w, h, wx, wy, frame):
        """白纸图标在头顶附近轻微摆动 — 使用 sin 曲线。"""
        from src.gui.icon_factory import draw_paper

        size = int(self._size * 0.6)
        icon_x = wx + self._size * 0.3
        icon_y = wy - size * 0.15
        # -3~+3 度摆动 — sin 曲线，平滑摆动
        tilt = 3.0 * math.sin(2 * math.pi * (frame % 36) / 36)
        alpha = 0.85 + 0.1 * (0.5 + 0.5 * math.sin(2 * math.pi * (frame % 22) / 22))
        draw_paper(p, icon_x, icon_y, size, alpha, tilt)

    # ------------------------------------------------------------------
    def mouseReleaseEvent(self, event):
        self.clicked.emit()
        super().mouseReleaseEvent(event)


# ======================================================================
# ToolAnimationIcon —— 工具调用过程的小动画图标（统一鲸鱼蓝风格）
# ======================================================================
class ToolAnimationIcon(QFrame):
    """工具调用过程的动画图标。

    模式:
      search : 放大镜旋转
      read   : 文件图标呼吸闪烁
      write  : 铅笔写字摆动
      run    : 终端光标闪烁
      gear   : 齿轮旋转
      done   : 绿色完成勾选
    """

    _ICON_MAP = {
        "search": "search",
        "read": "file",
        "write": "pencil",
        "run": "terminal",
        "gear": "settings",
        "done": "check",
    }

    def __init__(self, mode: str = "gear", size: int = 18, parent=None):
        super().__init__(parent)
        self._size = max(12, size)
        self._mode = (mode or "gear").lower()
        self._frame = 0
        self.setFixedSize(self._size, self._size)
        self.setStyleSheet("background:transparent;")
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        if self._mode == "done":
            self._timer.stop()
        else:
            self._timer.start(60)

    def set_mode(self, mode: str):
        m = (mode or "gear").lower()
        if m == self._mode:
            return
        self._mode = m
        self._frame = 0
        if m == "done":
            self._timer.stop()
        else:
            if not self._timer.isActive():
                self._timer.start(60)
        self.update()

    def _on_tick(self):
        self._frame += 1
        self.update()

    def _color(self, key: str, alpha: float = 1.0) -> QColor:
        hex_s = COLORS.get(key, "#000000")
        try:
            h = hex_s.lstrip("#")
            r = int(h[0:2], 16)
            g = int(h[2:4], 16)
            b = int(h[4:6], 16)
            return QColor(r, g, b, int(255 * max(0.0, min(1.0, alpha))))
        except Exception:
            return QColor(0, 0, 0, int(255 * max(0.0, min(1.0, alpha))))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        w = self.width()
        h = self.height()
        frame = self._frame
        mode = self._mode

        icon_name = self._ICON_MAP.get(mode, "settings")
        pm = get_icon(icon_name, self._size)
        if pm.isNull():
            p.end()
            return

        if mode == "search":
            p.translate(w / 2, h / 2)
            p.rotate((frame * 12) % 360)
            p.translate(-w / 2, -h / 2)
            p.drawPixmap(0, 0, pm)

        elif mode == "read":
            alpha = 0.55 + 0.45 * math.sin(math.radians(frame * 10))
            p.setOpacity(alpha)
            p.drawPixmap(0, 0, pm)
            p.setOpacity(1.0)

        elif mode == "write":
            offset = int(3 * math.sin(math.radians(frame * 12)))
            p.translate(offset, 0)
            p.drawPixmap(0, 0, pm)

        elif mode == "run":
            p.drawPixmap(0, 0, pm)
            if (frame % 12) < 6:
                p.setBrush(self._color("PRIM_LIGHT"))
                p.setPen(Qt.NoPen)
                p.drawRect(int(w * 0.30), int(h * 0.58),
                           max(1, int(w * 0.12)), int(h * 0.16))

        elif mode == "gear":
            p.translate(w / 2, h / 2)
            p.rotate((frame * 8) % 360)
            p.translate(-w / 2, -h / 2)
            p.drawPixmap(0, 0, pm)

        elif mode == "done":
            # 成功态用绿色覆盖绘制对勾
            p.setPen(QPen(self._color("SUCCESS"), max(2, int(w * 0.12)),
                          Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            p.drawLine(int(w * 0.22), int(h * 0.52), int(w * 0.42), int(h * 0.70))
            p.drawLine(int(w * 0.40), int(h * 0.70), int(w * 0.78), int(h * 0.30))

        else:
            p.drawPixmap(0, 0, pm)

        p.end()
