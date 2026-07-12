# -*- coding: utf-8 -*-
"""Icon maker functions: content category."""
import math
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QPixmap, QPen, QPainterPath, QPolygonF
from src.gui.icons._helpers import (
    _q, _alpha, _begin, _circle_btn_bg, _set_stroke_pen, _set_fill, _center_rect,
    WHALE_BLUE, WHALE_BLUE_LIGHT, PALE_BLUE_GREY, WHITE,
)
def _make_file(size: int) -> QPixmap:
    """文件：文档页（与 paper 类似但线条更简洁）。"""
    p, pm = _begin(size)
    pad = size * 0.14
    page = QRectF(pad, pad, size - 2 * pad, size - 2 * pad)
    radius = size * 0.07
    fold = size * 0.18

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(page, radius, radius)

    _set_fill(p, WHALE_BLUE_LIGHT)
    p.drawPolygon([
        QPointF(page.right() - fold, page.bottom()),
        QPointF(page.right(), page.bottom() - fold),
        QPointF(page.right(), page.bottom()),
    ])

    p.setPen(QPen(_alpha(WHALE_BLUE, 0.6), max(1, size * 0.035)))
    for i, factor in enumerate([0.34, 0.50]):
        y = page.top() + page.height() * factor
        x1 = page.left() + page.width() * 0.16
        x2 = page.left() + page.width() * (0.50 + 0.10 * i)
        p.drawLine(QPointF(x1, y), QPointF(x2, y))

    p.end()
    return pm


def _make_folder(size: int) -> QPixmap:
    """文件夹：圆角文件夹轮廓。"""
    p, pm = _begin(size)
    pad = size * 0.16
    tab_w = size * 0.22
    tab_h = size * 0.10
    body = QRectF(pad, pad + tab_h, size - 2 * pad, size - 2 * pad - tab_h)
    radius = size * 0.08

    path = QPainterPath()
    path.moveTo(pad + radius, pad + tab_h)
    path.lineTo(pad + tab_w, pad + tab_h)
    path.lineTo(pad + tab_w + size * 0.08, pad)
    path.lineTo(size - pad - radius, pad)
    path.quadTo(size - pad, pad, size - pad, pad + radius)
    path.lineTo(size - pad, body.bottom() - radius)
    path.quadTo(size - pad, body.bottom(), size - pad - radius, body.bottom())
    path.lineTo(pad + radius, body.bottom())
    path.quadTo(pad, body.bottom(), pad, body.bottom() - radius)
    path.lineTo(pad, pad + tab_h + radius)
    path.quadTo(pad, pad + tab_h, pad + radius, pad + tab_h)

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)

    p.end()
    return pm


def _make_pencil(size: int) -> QPixmap:
    """铅笔/编辑：倾斜铅笔轮廓。"""
    p, pm = _begin(size)
    cx = size / 2
    cy = size / 2
    length = size * 0.50
    width = size * 0.10
    angle = math.radians(45)

    cos_a = math.cos(angle)
    sin_a = math.sin(angle)

    def _rot(dx, dy):
        return QPointF(cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a)

    body = QPolygonF([
        _rot(-length * 0.5, -width / 2),
        _rot(length * 0.3, -width / 2),
        _rot(length * 0.5, 0),
        _rot(length * 0.3, width / 2),
        _rot(-length * 0.5, width / 2),
    ])
    tip = QPolygonF([
        _rot(length * 0.3, -width / 2),
        _rot(length * 0.5, 0),
        _rot(length * 0.3, width / 2),
    ])

    _set_fill(p, WHALE_BLUE)
    p.drawPolygon(body)
    _set_fill(p, PALE_BLUE_GREY)
    p.drawPolygon(tip)

    p.end()
    return pm


def _make_checkbox_empty(size: int) -> QPixmap:
    """空复选框：圆角矩形轮廓。"""
    p, pm = _begin(size)
    pad = size * 0.16
    rect = QRectF(pad, pad, size - 2 * pad, size - 2 * pad)
    radius = size * 0.12

    _set_stroke_pen(p, size, PALE_BLUE_GREY)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(rect, radius, radius)

    p.end()
    return pm


def _make_checkbox_checked(size: int) -> QPixmap:
    """选中复选框：蓝色圆角矩形 + 白色对勾。"""
    p, pm = _begin(size)
    pad = size * 0.16
    rect = QRectF(pad, pad, size - 2 * pad, size - 2 * pad)
    radius = size * 0.12

    _set_fill(p, WHALE_BLUE)
    p.drawRoundedRect(rect, radius, radius)

    p.setPen(QPen(WHITE, max(1.5, size * 0.10), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    p.drawLine(QPointF(size * 0.28, size * 0.52), QPointF(size * 0.44, size * 0.68))
    p.drawLine(QPointF(size * 0.42, size * 0.68), QPointF(size * 0.72, size * 0.34))

    p.end()
    return pm


# ============================================================================
# 运行时 PNG 保障
# ============================================================================

