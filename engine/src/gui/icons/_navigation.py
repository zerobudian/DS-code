# -*- coding: utf-8 -*-
"""Icon maker functions: navigation category."""
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QPixmap, QPen, QColor, QPainterPath
from src.gui.icons._helpers import (
    _q, _alpha, _begin, _circle_btn_bg, _set_stroke_pen, _set_fill, _center_rect,
    WHALE_BLUE, WHALE_BLUE_LIGHT, PALE_BLUE_GREY, WHITE,
)
def _make_arrow_up(size: int) -> QPixmap:
    """发送按钮：蓝色圆底 + 白色向上箭头（旧版，保留兼容）。"""
    p, pm = _begin(size)
    _circle_btn_bg(p, size)

    cx = cy = size / 2
    ah = size * 0.30
    aw = size * 0.16

    path = QPainterPath()
    path.moveTo(cx, cy - ah)
    path.lineTo(cx - aw, cy + ah * 0.25)
    path.moveTo(cx, cy - ah)
    path.lineTo(cx + aw, cy + ah * 0.25)
    path.moveTo(cx, cy - ah + size * 0.08)
    path.lineTo(cx, cy + ah * 0.6)

    _set_stroke_pen(p, size, WHITE)
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)

    p.end()
    return pm


def _make_arrow_up_plain(size: int) -> QPixmap:
    """纯蓝色向上箭头（不带圆形背景，用于天蓝色发送按钮）。"""
    p, pm = _begin(size)
    cx = cy = size / 2
    ah = size * 0.32
    aw = size * 0.18
    stroke = max(2.0, size * 0.10)

    pen = QPen(WHALE_BLUE, stroke, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    # 向上箭头：∧ 形状 + 竖线
    path = QPainterPath()
    path.moveTo(cx, cy - ah)
    path.lineTo(cx - aw, cy + ah * 0.2)
    path.lineTo(cx + aw, cy + ah * 0.2)
    path.moveTo(cx, cy - ah + size * 0.08)
    path.lineTo(cx, cy + ah * 0.55)
    p.drawPath(path)

    p.end()
    return pm


def _make_arrow_up_white(size: int) -> QPixmap:
    """纯白色向上箭头（不带圆形背景，用于深色/彩色按钮上）。"""
    p, pm = _begin(size)
    cx = cy = size / 2
    ah = size * 0.32
    aw = size * 0.18
    stroke = max(2.0, size * 0.10)

    pen = QPen(WHITE, stroke, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    # 向上箭头：∧ 形状 + 竖线
    path = QPainterPath()
    path.moveTo(cx, cy - ah)
    path.lineTo(cx - aw, cy + ah * 0.2)
    path.lineTo(cx + aw, cy + ah * 0.2)
    path.moveTo(cx, cy - ah + size * 0.08)
    path.lineTo(cx, cy + ah * 0.55)
    p.drawPath(path)

    p.end()
    return pm


def _make_arrow_up_disabled(size: int) -> QPixmap:
    """纯灰色向上箭头（不带圆形背景，用于禁用状态）。"""
    p, pm = _begin(size)
    cx = cy = size / 2
    ah = size * 0.32
    aw = size * 0.18
    stroke = max(2.0, size * 0.10)

    pen = QPen(PALE_BLUE_GREY, stroke, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    path = QPainterPath()
    path.moveTo(cx, cy - ah)
    path.lineTo(cx - aw, cy + ah * 0.2)
    path.lineTo(cx + aw, cy + ah * 0.2)
    path.moveTo(cx, cy - ah + size * 0.08)
    path.lineTo(cx, cy + ah * 0.55)
    p.drawPath(path)

    p.end()
    return pm


def _make_arrow_up_black(size: int) -> QPixmap:
    """纯黑色向上箭头（不带圆形背景，用于浅色/白色按钮上）。"""
    p, pm = _begin(size)
    cx = cy = size / 2
    ah = size * 0.32
    aw = size * 0.18
    stroke = max(2.0, size * 0.10)

    pen = QPen(QColor(0, 0, 0), stroke, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    # 向上箭头：∧ 形状 + 竖线
    path = QPainterPath()
    path.moveTo(cx, cy - ah)
    path.lineTo(cx - aw, cy + ah * 0.2)
    path.lineTo(cx + aw, cy + ah * 0.2)
    path.moveTo(cx, cy - ah + size * 0.08)
    path.lineTo(cx, cy + ah * 0.55)
    p.drawPath(path)

    p.end()
    return pm


def _make_stop(size: int) -> QPixmap:
    """停止图标：蓝色填充圆角正方形（不带圆形背景）。"""
    p, pm = _begin(size)
    pad = size * 0.30
    square = size - 2 * pad
    r = square * 0.18

    p.setPen(Qt.NoPen)
    p.setBrush(QColor(WHALE_BLUE))
    p.drawRoundedRect(int(pad), int(pad), int(square), int(square), r, r)

    p.end()
    return pm


def _make_stop_white(size: int) -> QPixmap:
    """停止图标：白色填充圆角正方形（不带圆形背景，用于天蓝色按钮）。"""
    p, pm = _begin(size)
    pad = size * 0.30
    square = size - 2 * pad
    r = square * 0.15

    p.setPen(Qt.NoPen)
    p.setBrush(QColor(WHITE))
    p.drawRoundedRect(int(pad), int(pad), int(square), int(square), r, r)

    p.end()
    return pm


def _make_stop_black(size: int) -> QPixmap:
    """停止图标：黑色填充圆角正方形（不带圆形背景，用于浅色/白色按钮上）。"""
    p, pm = _begin(size)
    pad = size * 0.30
    square = size - 2 * pad
    r = square * 0.15

    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0))
    p.drawRoundedRect(int(pad), int(pad), int(square), int(square), r, r)

    p.end()
    return pm


def _make_chevron(direction: str, size: int) -> QPixmap:
    """折叠箭头：left / right / up / down。"""
    p, pm = _begin(size)
    cx = cy = size / 2
    arm = size * 0.18
    _set_stroke_pen(p, size, PALE_BLUE_GREY)
    p.setBrush(Qt.NoBrush)

    if direction == "left":
        p.drawLine(QPointF(cx + arm, cy - arm), QPointF(cx - arm, cy))
        p.drawLine(QPointF(cx - arm, cy), QPointF(cx + arm, cy + arm))
    elif direction == "right":
        p.drawLine(QPointF(cx - arm, cy - arm), QPointF(cx + arm, cy))
        p.drawLine(QPointF(cx + arm, cy), QPointF(cx - arm, cy + arm))
    elif direction == "up":
        p.drawLine(QPointF(cx - arm, cy + arm), QPointF(cx, cy - arm))
        p.drawLine(QPointF(cx, cy - arm), QPointF(cx + arm, cy + arm))
    else:  # down
        p.drawLine(QPointF(cx - arm, cy - arm), QPointF(cx, cy + arm))
        p.drawLine(QPointF(cx, cy + arm), QPointF(cx + arm, cy - arm))

    p.end()
    return pm


def _make_chevron_left(size: int) -> QPixmap:
    return _make_chevron("left", size)


def _make_chevron_right(size: int) -> QPixmap:
    return _make_chevron("right", size)


def _make_chevron_up(size: int) -> QPixmap:
    return _make_chevron("up", size)


def _make_chevron_down(size: int) -> QPixmap:
    return _make_chevron("down", size)


