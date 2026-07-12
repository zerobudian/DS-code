# -*- coding: utf-8 -*-
"""Icon maker functions: actions category."""
import math
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QPixmap, QPen, QFont, QPainterPath
from src.gui.icons._helpers import (
    _q, _alpha, _begin, _circle_btn_bg, _set_stroke_pen, _set_fill, _center_rect,
    WHALE_BLUE, WHALE_BLUE_LIGHT, PALE_BLUE_GREY, PAPERCLIP_GREY, WHITE, FONT_STACK,
)
def _make_paperclip(size: int) -> QPixmap:
    """附件按钮：灰色/浅蓝回形针轮廓。"""
    p, pm = _begin(size)

    cx, cy = size / 2, size / 2
    w = size * 0.18
    h = size * 0.34
    line = max(1.5, size * 0.055)
    radius = size * 0.06

    outer = QPainterPath()
    outer.moveTo(cx - w + line, cy + h)
    outer.lineTo(cx - w + line, cy - h + radius)
    outer.arcTo(cx - w, cy - h, w * 2, radius * 2, 180, -180)
    outer.lineTo(cx + w - line, cy + h - radius)
    outer.arcTo(cx + w - line * 2, cy + h - radius * 2, line * 2, radius * 2, 0, -180)

    inner = QPainterPath()
    inner.moveTo(cx + w - line * 2, cy + h - line)
    inner.lineTo(cx + w - line * 2, cy - h + radius + line)
    inner.arcTo(cx - w + line, cy - h + line, (w - line) * 2, radius * 2, 0, 180)
    inner.lineTo(cx - w + line, cy + h - radius)
    inner.arcTo(cx - w + line, cy + h - radius * 2, line * 2, radius * 2, 180, 180)

    _set_stroke_pen(p, size, PAPERCLIP_GREY)
    p.setBrush(Qt.NoBrush)
    p.drawPath(outer)
    p.drawPath(inner)

    p.end()
    return pm


def _make_bubble(size: int) -> QPixmap:
    """与鲸鱼风格协调的简洁气泡：蓝色描边 + 浅蓝填充。"""
    p, pm = _begin(size)

    pad = size * 0.12
    body_w = size - 2 * pad
    body_h = size * 0.62
    x = pad
    y = (size - body_h) / 2 - pad * 0.3
    radius = size * 0.18

    path = QPainterPath()
    rect = QRectF(x, y, body_w, body_h)
    path.addRoundedRect(rect, radius, radius)
    tail_w = size * 0.16
    tail_h = size * 0.12
    path.moveTo(x + size * 0.22, y + body_h - radius)
    path.lineTo(x + size * 0.18, y + body_h + tail_h)
    path.lineTo(x + size * 0.18 + tail_w, y + body_h - radius * 0.6)

    p.setBrush(_alpha(WHALE_BLUE_LIGHT, 0.20))
    _set_stroke_pen(p, size, WHALE_BLUE)
    p.drawPath(path)

    p.end()
    return pm


def _make_keyboard(size: int) -> QPixmap:
    """简洁键盘轮廓：蓝色圆角矩形外框 + 蓝色小按键。"""
    p, pm = _begin(size)

    pad = size * 0.12
    body = QRectF(pad, size * 0.22, size - 2 * pad, size * 0.56)
    radius = size * 0.10

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(body, radius, radius)

    p.setBrush(_alpha(WHALE_BLUE, 0.85))
    p.setPen(Qt.NoPen)
    cols = 4
    rows = 3
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

    p.end()
    return pm


def _make_terminal(size: int) -> QPixmap:
    """简洁终端窗口：蓝色圆角边框 + 标题栏 + 命令提示符。"""
    p, pm = _begin(size)

    pad = size * 0.10
    body = QRectF(pad, size * 0.12, size - 2 * pad, size * 0.76)
    radius = size * 0.10
    title_h = size * 0.22

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(body, radius, radius)

    p.setPen(QPen(_alpha(WHALE_BLUE, 0.6), max(1, size * 0.04)))
    y_sep = body.y() + title_h
    p.drawLine(QPointF(body.x() + radius * 0.5, y_sep),
               QPointF(body.right() - radius * 0.5, y_sep))

    dot_r = size * 0.045
    dot_y = body.y() + title_h / 2
    dot_colors = [WHALE_BLUE, WHALE_BLUE_LIGHT, _alpha(WHALE_BLUE, 0.45)]
    for i, c in enumerate(dot_colors):
        dot_x = body.x() + size * 0.18 + i * size * 0.11
        p.setBrush(c)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(dot_x, dot_y), dot_r, dot_r)

    font = QFont(FONT_STACK)
    font.setPixelSize(int(size * 0.24))
    p.setFont(font)
    p.setPen(WHALE_BLUE)
    p.drawText(QPointF(body.x() + size * 0.14, y_sep + size * 0.32), ">_")

    p.end()
    return pm


def _make_paper(size: int) -> QPixmap:
    """简洁文档页：蓝色圆角边框 + 折叠角 + 内容线。"""
    p, pm = _begin(size)

    pad = size * 0.12
    page = QRectF(pad, pad, size - 2 * pad, size - 2 * pad)
    radius = size * 0.08
    fold = size * 0.18

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(page, radius, radius)

    p.setPen(Qt.NoPen)
    p.setBrush(WHALE_BLUE_LIGHT)
    fold_poly = [
        QPointF(page.right() - fold, page.bottom()),
        QPointF(page.right(), page.bottom() - fold),
        QPointF(page.right(), page.bottom()),
    ]
    p.drawPolygon(fold_poly)
    p.setPen(QPen(WHITE, max(1, size * 0.03)))
    p.drawLine(QPointF(page.right() - fold, page.bottom() - fold * 0.1),
               QPointF(page.right() - fold * 0.1, page.bottom() - fold))

    p.setPen(QPen(_alpha(WHALE_BLUE, 0.6), max(1, size * 0.035)))
    for i, factor in enumerate([0.30, 0.45, 0.60]):
        y = page.top() + page.height() * factor
        x1 = page.left() + page.width() * 0.18
        x2 = page.left() + page.width() * (0.45 + 0.08 * i)
        p.drawLine(QPointF(x1, y), QPointF(x2, y))

    p.end()
    return pm


def _make_settings(size: int) -> QPixmap:
    """设置齿轮：简洁轮廓，8 齿。"""
    p, pm = _begin(size)
    cx = cy = size / 2
    teeth = 8
    outer_r = size * 0.34
    inner_r = size * 0.24
    hole_r = size * 0.10
    path = QPainterPath()
    for i in range(teeth * 2):
        angle = math.radians(i * 360 / (teeth * 2))
        r = outer_r if i % 2 == 0 else inner_r
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        if i == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)
    p.drawEllipse(QPointF(cx, cy), hole_r, hole_r)

    p.end()
    return pm


def _make_refresh(size: int) -> QPixmap:
    """刷新：环形箭头。"""
    p, pm = _begin(size)
    cx = cy = size / 2
    r = size * 0.26

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawArc(int(cx - r), int(cy - r), int(r * 2), int(r * 2), 30 * 16, 270 * 16)

    # 箭头
    angle = math.radians(30)
    ax = cx + r * math.cos(angle)
    ay = cy - r * math.sin(angle)
    p.drawLine(QPointF(ax, ay), QPointF(ax - size * 0.08, ay + size * 0.06))
    p.drawLine(QPointF(ax, ay), QPointF(ax - size * 0.02, ay + size * 0.10))

    p.end()
    return pm


def _make_close(size: int) -> QPixmap:
    """关闭：X。"""
    p, pm = _begin(size)
    pad = size * 0.26
    _set_stroke_pen(p, size, PALE_BLUE_GREY)
    p.setBrush(Qt.NoBrush)
    p.drawLine(QPointF(pad, pad), QPointF(size - pad, size - pad))
    p.drawLine(QPointF(size - pad, pad), QPointF(pad, size - pad))

    p.end()
    return pm


def _make_copy(size: int) -> QPixmap:
    """复制：两个重叠矩形。"""
    p, pm = _begin(size)
    pad = size * 0.18
    offset = size * 0.12
    w = size - 2 * pad - offset
    h = size - 2 * pad - offset
    radius = size * 0.05

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    # 后方矩形
    p.drawRoundedRect(QRectF(pad + offset, pad, w, h), radius, radius)
    # 前方矩形
    p.drawRoundedRect(QRectF(pad, pad + offset, w, h), radius, radius)

    p.end()
    return pm


def _make_check(size: int) -> QPixmap:
    """勾选：蓝色对勾。"""
    p, pm = _begin(size)
    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawLine(QPointF(size * 0.24, size * 0.52), QPointF(size * 0.42, size * 0.70))
    p.drawLine(QPointF(size * 0.40, size * 0.70), QPointF(size * 0.76, size * 0.30))

    p.end()
    return pm


def _make_plus(size: int) -> QPixmap:
    """加号。"""
    p, pm = _begin(size)
    cx = cy = size / 2
    arm = size * 0.18
    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawLine(QPointF(cx - arm, cy), QPointF(cx + arm, cy))
    p.drawLine(QPointF(cx, cy - arm), QPointF(cx, cy + arm))

    p.end()
    return pm


def _make_minus(size: int) -> QPixmap:
    """减号。"""
    p, pm = _begin(size)
    cx = cy = size / 2
    arm = size * 0.18
    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawLine(QPointF(cx - arm, cy), QPointF(cx + arm, cy))

    p.end()
    return pm


