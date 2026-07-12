# -*- coding: utf-8 -*-
"""Icon maker functions: draw category."""
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QPainter, QPen, QColor, QFont, QPainterPath
from PySide6.QtWidgets import QApplication
from src.gui.icons._helpers import (
    _q, _alpha, _begin, _circle_btn_bg, _set_stroke_pen, _set_fill, _center_rect,
    WHALE_BLUE, WHALE_BLUE_LIGHT, WHITE, FONT_STACK,
)
def draw_bubble(p: QPainter, x: float, y: float, size: float, alpha: float = 1.0):
    """在 p 上画一个与鲸鱼风格协调的浅蓝小气泡。"""
    old_opacity = p.opacity()
    p.setOpacity(min(1.0, max(0.0, alpha)) * old_opacity)

    pad = size * 0.12
    body_w = size - 2 * pad
    body_h = size * 0.62
    bx, by = x + pad, y + (size - body_h) / 2 - pad * 0.3
    radius = size * 0.18

    path = QPainterPath()
    path.addRoundedRect(QRectF(bx, by, body_w, body_h), radius, radius)
    tail_w = size * 0.16
    tail_h = size * 0.12
    path.moveTo(bx + size * 0.22, by + body_h - radius)
    path.lineTo(bx + size * 0.18, by + body_h + tail_h)
    path.lineTo(bx + size * 0.18 + tail_w, by + body_h - radius * 0.6)

    p.setBrush(_alpha(WHALE_BLUE_LIGHT, 0.20 * alpha))
    pen = QPen(_alpha(WHALE_BLUE, alpha), max(1, size * 0.06))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawPath(path)

    p.setOpacity(old_opacity)


def draw_arrow_up_white(p: QPainter, x: float, y: float, size: float, alpha: float = 1.0):
    """纯白色向上箭头（∧ + 竖线），支持 alpha，用于深色/彩色按钮上直接绘制。"""
    old_opacity = p.opacity()
    p.setOpacity(min(1.0, max(0.0, alpha)) * old_opacity)

    cx = x + size / 2
    cy = y + size / 2
    ah = size * 0.32
    aw = size * 0.18
    stroke = max(2.0, size * 0.10)

    pen = QPen(_alpha(WHITE, alpha), stroke, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    path = QPainterPath()
    path.moveTo(cx, cy - ah)
    path.lineTo(cx - aw, cy + ah * 0.2)
    path.lineTo(cx + aw, cy + ah * 0.2)
    path.moveTo(cx, cy - ah + size * 0.08)
    path.lineTo(cx, cy + ah * 0.55)
    p.drawPath(path)

    p.setOpacity(old_opacity)


def draw_arrow_up_black(p: QPainter, x: float, y: float, size: float, alpha: float = 1.0):
    """纯黑色向上箭头（∧ + 竖线），支持 alpha，用于浅色/白色按钮上直接绘制。"""
    old_opacity = p.opacity()
    p.setOpacity(min(1.0, max(0.0, alpha)) * old_opacity)

    cx = x + size / 2
    cy = y + size / 2
    ah = size * 0.32
    aw = size * 0.18
    stroke = max(2.0, size * 0.10)

    pen = QPen(_alpha(QColor(0, 0, 0), alpha), stroke, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    path = QPainterPath()
    path.moveTo(cx, cy - ah)
    path.lineTo(cx - aw, cy + ah * 0.2)
    path.lineTo(cx + aw, cy + ah * 0.2)
    path.moveTo(cx, cy - ah + size * 0.08)
    path.lineTo(cx, cy + ah * 0.55)
    p.drawPath(path)

    p.setOpacity(old_opacity)


def draw_keyboard(p: QPainter, x: float, y: float, size: float, alpha: float = 1.0):
    """键盘图标：蓝色圆角矩形外框 + 蓝色小按键。"""
    old_opacity = p.opacity()
    p.setOpacity(min(1.0, max(0.0, alpha)) * old_opacity)

    pad = size * 0.12
    body = QRectF(x + pad, y + size * 0.22, size - 2 * pad, size * 0.56)
    radius = size * 0.10

    pen = QPen(_alpha(WHALE_BLUE, alpha), max(1, size * 0.06))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(body, radius, radius)

    p.setBrush(_alpha(WHALE_BLUE, 0.85 * alpha))
    p.setPen(Qt.NoPen)
    cols, rows = 4, 3
    gap = size * 0.045
    key_w = (body.width() - gap * (cols + 1)) / cols
    key_h = (body.height() - gap * (rows + 1)) / rows
    key_r = size * 0.03
    start_x = body.x() + gap
    start_y = body.y() + gap
    for row in range(rows):
        for col in range(cols):
            kx = start_x + col * (key_w + gap)
            ky = start_y + row * (key_h + gap)
            p.drawRoundedRect(QRectF(kx, ky, key_w, key_h), key_r, key_r)

    p.setOpacity(old_opacity)


def draw_terminal(p: QPainter, x: float, y: float, size: float, alpha: float = 1.0,
                  cursor_on: bool = True):
    """小终端图标：蓝色圆角边框 + 标题栏 + 提示符。"""
    old_opacity = p.opacity()
    p.setOpacity(min(1.0, max(0.0, alpha)) * old_opacity)

    pad = size * 0.10
    body = QRectF(x + pad, y + size * 0.12, size - 2 * pad, size * 0.76)
    radius = size * 0.10
    title_h = size * 0.22

    pen = QPen(_alpha(WHALE_BLUE, alpha), max(1, size * 0.06))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(body, radius, radius)

    p.setPen(QPen(_alpha(WHALE_BLUE, 0.6 * alpha), max(1, size * 0.04)))
    y_sep = body.y() + title_h
    p.drawLine(QPointF(body.x() + radius * 0.5, y_sep),
               QPointF(body.right() - radius * 0.5, y_sep))

    dot_r = size * 0.045
    dot_y = body.y() + title_h / 2
    dots = [WHALE_BLUE, WHALE_BLUE_LIGHT, _alpha(WHALE_BLUE, 0.45)]
    for i, c in enumerate(dots):
        dot_x = body.x() + size * 0.18 + i * size * 0.11
        p.setBrush(_alpha(c, alpha))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(dot_x, dot_y), dot_r, dot_r)

    font = QFont(FONT_STACK)
    font.setPixelSize(int(size * 0.24))
    p.setFont(font)
    p.setPen(_alpha(WHALE_BLUE, alpha))
    p.drawText(QPointF(body.x() + size * 0.14, y_sep + size * 0.32), ">_")

    if cursor_on:
        cw = max(1, int(size * 0.08))
        ch = int(size * 0.18)
        cx = int(body.x() + size * 0.44)
        cy = int(y_sep + size * 0.12)
        p.setBrush(_alpha(WHALE_BLUE, alpha))
        p.setPen(Qt.NoPen)
        p.drawRect(cx, cy, cw, ch)

    p.setOpacity(old_opacity)


def draw_paper(p: QPainter, x: float, y: float, size: float, alpha: float = 1.0,
               tilt_deg: float = 0.0):
    """白纸图标：蓝色圆角边框 + 折叠角 + 内容线。"""
    old_opacity = p.opacity()
    old_transform = p.worldTransform()

    if tilt_deg:
        cx, cy = x + size / 2, y + size / 2
        p.translate(cx, cy)
        p.rotate(tilt_deg)
        p.translate(-cx, -cy)

    p.setOpacity(min(1.0, max(0.0, alpha)) * old_opacity)

    pad = size * 0.15
    page = QRectF(x + pad, y + pad, size - 2 * pad, size - 2 * pad)
    radius = size * 0.08
    fold = size * 0.18

    pen = QPen(_alpha(WHALE_BLUE, alpha), max(1, size * 0.06))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(page, radius, radius)

    p.setPen(Qt.NoPen)
    p.setBrush(_alpha(WHALE_BLUE_LIGHT, alpha))
    fold_poly = [
        QPointF(page.right() - fold, page.bottom()),
        QPointF(page.right(), page.bottom() - fold),
        QPointF(page.right(), page.bottom()),
    ]
    p.drawPolygon(fold_poly)

    p.setPen(QPen(_alpha(WHITE, alpha), max(1, size * 0.03)))
    p.drawLine(QPointF(page.right() - fold, page.bottom() - fold * 0.1),
               QPointF(page.right() - fold * 0.1, page.bottom() - fold))

    p.setPen(QPen(_alpha(WHALE_BLUE, 0.6 * alpha), max(1, size * 0.035)))
    for i, factor in enumerate([0.30, 0.45, 0.60]):
        ly = page.top() + page.height() * factor
        ls = page.left() + page.width() * 0.18
        le = page.left() + page.width() * (0.45 + 0.08 * i)
        p.drawLine(QPointF(ls, ly), QPointF(le, ly))

    p.setWorldTransform(old_transform)
    p.setOpacity(old_opacity)


# ============================================================================
# __main__ 快速生成
# ============================================================================

if __name__ == "__main__":
    import sys
    import os
    from src.gui.icons._registry import ensure_icons
    from src.gui.icons._helpers import REQUIRED_ICONS, ASSETS_DIR
    app = QApplication.instance() or QApplication(sys.argv)
    ok = ensure_icons()
    print(f"ensure_icons -> {ok}")
    for f in REQUIRED_ICONS:
        p = os.path.join(ASSETS_DIR, f)
        if os.path.isfile(p):
            print(f"  OK {f} ({os.path.getsize(p)} bytes)")
        else:
            print(f"  MISSING {f}")
