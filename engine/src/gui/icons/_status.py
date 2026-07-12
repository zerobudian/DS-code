# -*- coding: utf-8 -*-
"""Icon maker functions: status category."""
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QPixmap, QPen, QFont, QPainterPath
from src.gui.icons._helpers import (
    _q, _alpha, _begin, _circle_btn_bg, _set_stroke_pen, _set_fill, _center_rect,
    WHALE_BLUE, WHALE_BLUE_LIGHT, WARNING_ORANGE, FONT_STACK,
)
def _make_warning(size: int) -> QPixmap:
    """警告：橙色三角形 + 感叹号。"""
    p, pm = _begin(size)
    pad = size * 0.18
    top = QPointF(size / 2, pad)
    left = QPointF(pad, size - pad)
    right = QPointF(size - pad, size - pad)

    _set_stroke_pen(p, size, WARNING_ORANGE)
    p.setBrush(Qt.NoBrush)
    p.drawPolygon([top, right, left])

    # 感叹号
    p.setPen(QPen(WARNING_ORANGE, max(1.5, size * 0.08), Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(size / 2, pad + size * 0.22), QPointF(size / 2, size * 0.58))
    _set_fill(p, WARNING_ORANGE)
    p.drawEllipse(QPointF(size / 2, size * 0.72), size * 0.04, size * 0.04)

    p.end()
    return pm


def _make_info(size: int) -> QPixmap:
    """信息：蓝色圆圈 + i。"""
    p, pm = _begin(size)
    cx = cy = size / 2
    r = size * 0.28

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(cx, cy), r, r)

    font = QFont(FONT_STACK)
    font.setPixelSize(int(size * 0.42))
    font.setBold(True)
    p.setFont(font)
    p.setPen(WHALE_BLUE)
    p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, "i")

    p.end()
    return pm


def _make_search(size: int) -> QPixmap:
    """搜索：放大镜。"""
    p, pm = _begin(size)
    cx = cy = size * 0.42
    r = size * 0.22

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(cx, cy), r, r)
    p.drawLine(QPointF(cx + r * 0.65, cy + r * 0.65),
               QPointF(size * 0.78, size * 0.78))

    p.end()
    return pm


def _make_filter(size: int) -> QPixmap:
    """筛选：漏斗。"""
    p, pm = _begin(size)
    pad = size * 0.20
    bottom_w = size * 0.18
    bottom = size * 0.72

    path = QPainterPath()
    path.moveTo(pad, pad)
    path.lineTo(size - pad, pad)
    path.lineTo(size / 2 + bottom_w / 2, bottom)
    path.lineTo(size / 2 - bottom_w / 2, bottom)
    path.closeSubpath()

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)

    p.end()
    return pm


def _make_sort(size: int) -> QPixmap:
    """排序：三条长度递减的横线。"""
    p, pm = _begin(size)
    y_positions = [0.30, 0.50, 0.70]
    widths = [0.60, 0.45, 0.30]
    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    pad = size * 0.18
    for y_fac, w_fac in zip(y_positions, widths):
        y = size * y_fac
        x1 = pad
        x2 = pad + size * w_fac
        p.drawLine(QPointF(x1, y), QPointF(x2, y))

    p.end()
    return pm


def _make_question(size: int) -> QPixmap:
    """帮助：圆圈 + ?。"""
    p, pm = _begin(size)
    cx = cy = size / 2
    r = size * 0.28

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(cx, cy), r, r)

    font = QFont(FONT_STACK)
    font.setPixelSize(int(size * 0.42))
    font.setBold(True)
    p.setFont(font)
    p.setPen(WHALE_BLUE)
    p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, "?")

    p.end()
    return pm


