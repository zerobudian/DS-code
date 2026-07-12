# -*- coding: utf-8 -*-
"""Icon maker functions: project category."""
import math
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QPixmap, QPen, QPainterPath
from src.gui.icons._helpers import (
    _q, _alpha, _begin, _circle_btn_bg, _set_stroke_pen, _set_fill, _center_rect,
    WHALE_BLUE, WHALE_BLUE_LIGHT, PALE_BLUE_GREY,
)
def _make_moon(size: int) -> QPixmap:
    """月牙：简洁轮廓。"""
    p, pm = _begin(size)
    cx = cy = size / 2
    r = size * 0.30
    pen = QPen(WHALE_BLUE, max(2, size * 0.10), Qt.SolidLine, Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawArc(int(cx - r), int(cy - r), int(r * 2), int(r * 2), 30 * 16, 180 * 16)

    p.end()
    return pm


def _make_sun(size: int) -> QPixmap:
    """太阳：圆心 + 8 条短线光芒。"""
    p, pm = _begin(size)
    cx = cy = size / 2
    core_r = size * 0.14
    ray_inner = size * 0.22
    ray_outer = size * 0.33

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(cx, cy), core_r, core_r)
    for i in range(8):
        angle = math.radians(i * 45)
        p.drawLine(
            QPointF(cx + ray_inner * math.cos(angle), cy + ray_inner * math.sin(angle)),
            QPointF(cx + ray_outer * math.cos(angle), cy + ray_outer * math.sin(angle)),
        )

    p.end()
    return pm


def _make_history(size: int) -> QPixmap:
    """历史：时钟 + 回环箭头。"""
    p, pm = _begin(size)
    cx = cy = size / 2
    r = size * 0.28

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(cx, cy), r, r)

    # 时钟指针指向约 10 点
    p.drawLine(QPointF(cx, cy), QPointF(cx - r * 0.45, cy - r * 0.35))
    p.drawLine(QPointF(cx, cy), QPointF(cx, cy - r * 0.55))

    # 顶部回环箭头
    arrow_r = r + size * 0.06
    pen = QPen(WHALE_BLUE, max(1.5, size * 0.06), Qt.SolidLine, Qt.RoundCap)
    p.setPen(pen)
    p.drawArc(int(cx - arrow_r), int(cy - arrow_r - size * 0.04),
              int(arrow_r * 2), int(arrow_r * 2), 90 * 16, 120 * 16)
    # 箭头小三角
    ax, ay = cx, cy - arrow_r - size * 0.04
    p.drawLine(QPointF(ax, ay), QPointF(ax + size * 0.06, ay + size * 0.08))
    p.drawLine(QPointF(ax, ay), QPointF(ax - size * 0.06, ay + size * 0.08))

    p.end()
    return pm


def _make_memory(size: int) -> QPixmap:
    """记忆：三层堆叠的圆角卡片。"""
    p, pm = _begin(size)
    pad = size * 0.16
    w = size - 2 * pad
    h = size * 0.16
    gap = size * 0.07
    radius = size * 0.05

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    for i in range(3):
        y = pad + i * (h + gap)
        p.drawRoundedRect(QRectF(pad, y, w, h), radius, radius)

    p.end()
    return pm


def _make_model(size: int) -> QPixmap:
    """模型：等距立方体轮廓。"""
    p, pm = _begin(size)
    cx = size / 2
    cy = size * 0.55
    w = size * 0.34
    h = size * 0.28
    d = size * 0.18

    pts = {
        "bl": QPointF(cx - w, cy - h),
        "br": QPointF(cx + w, cy - h),
        "tr": QPointF(cx + w, cy + h),
        "tl": QPointF(cx - w, cy + h),
        "top": QPointF(cx, cy - h - d),
        "right": QPointF(cx + w + d, cy),
    }

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawPolygon([pts["bl"], pts["br"], pts["tr"], pts["tl"]])
    p.drawLine(pts["bl"], pts["top"])
    p.drawLine(pts["br"], pts["top"])
    p.drawLine(pts["tr"], pts["right"])
    p.drawLine(pts["br"], pts["right"])

    p.end()
    return pm


def _make_permission(size: int) -> QPixmap:
    """权限：盾牌轮廓。"""
    p, pm = _begin(size)
    cx = size / 2
    cy = size * 0.50
    w = size * 0.24
    h = size * 0.30

    path = QPainterPath()
    path.moveTo(cx - w, cy - h + size * 0.04)
    path.quadTo(cx - w, cy - h, cx, cy - h - size * 0.06)
    path.quadTo(cx + w, cy - h, cx + w, cy - h + size * 0.04)
    path.lineTo(cx + w, cy + size * 0.02)
    path.quadTo(cx + w, cy + h * 0.7, cx, cy + h)
    path.quadTo(cx - w, cy + h * 0.7, cx - w, cy + size * 0.02)
    path.closeSubpath()

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)

    p.end()
    return pm


def _make_mode(size: int) -> QPixmap:
    """模式：切换开关（圆角矩形 + 圆点）。"""
    p, pm = _begin(size)
    pad = size * 0.20
    body = QRectF(pad, size * 0.34, size - 2 * pad, size * 0.32)
    radius = body.height() / 2

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(body, radius, radius)

    _set_fill(p, WHALE_BLUE)
    p.drawEllipse(QPointF(body.x() + radius, body.y() + radius), radius * 0.72, radius * 0.72)

    p.end()
    return pm


def _make_trash(size: int) -> QPixmap:
    """删除：垃圾桶轮廓（蓝色，与工具栏图标一致）。"""
    p, pm = _begin(size)
    pad = size * 0.22
    w = size - 2 * pad
    h = size * 0.42
    x = pad
    y = size * 0.34
    radius = size * 0.04

    body = QRectF(x, y, w, h)
    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(body, radius, radius)

    # 桶盖
    lid_w = w + size * 0.10
    lid = QRectF((size - lid_w) / 2, y - size * 0.08, lid_w, size * 0.08)
    p.drawRoundedRect(lid, size * 0.02, size * 0.02)
    # 提手
    handle_w = size * 0.14
    p.drawArc(int(size / 2 - handle_w / 2), int(y - size * 0.18),
              int(handle_w), int(size * 0.14), 0 * 16, 180 * 16)
    # 竖线
    line_y_top = y + size * 0.10
    line_y_bottom = y + h - size * 0.08
    for offset in (-size * 0.08, 0, size * 0.08):
        p.drawLine(QPointF(size / 2 + offset, line_y_top),
                   QPointF(size / 2 + offset, line_y_bottom))

    p.end()
    return pm


def _make_pin(size: int) -> QPixmap:
    """置顶：图钉。"""
    p, pm = _begin(size)
    cx = size / 2
    head_r = size * 0.12
    body_w = size * 0.12
    top = size * 0.18
    bottom = size * 0.72

    _set_stroke_pen(p, size, WHALE_BLUE)
    p.setBrush(Qt.NoBrush)
    # 钉头
    p.drawEllipse(QPointF(cx, top + head_r), head_r, head_r)
    # 钉身
    p.drawLine(QPointF(cx - body_w / 2, top + head_r * 1.8),
               QPointF(cx - size * 0.06, bottom))
    p.drawLine(QPointF(cx + body_w / 2, top + head_r * 1.8),
               QPointF(cx + size * 0.06, bottom))
    # 钉尖
    p.drawLine(QPointF(cx - size * 0.06, bottom),
               QPointF(cx, size * 0.82))
    p.drawLine(QPointF(cx + size * 0.06, bottom),
               QPointF(cx, size * 0.82))

    p.end()
    return pm


def _make_unpin(size: int) -> QPixmap:
    """取消置顶：图钉 + 斜杠。"""
    p, pm = _begin(size)
    # 先画 pin
    pin_pm = _make_pin(size)
    p.drawPixmap(0, 0, pin_pm)
    # 斜杠
    _set_stroke_pen(p, size, PALE_BLUE_GREY)
    p.setBrush(Qt.NoBrush)
    p.drawLine(QPointF(size * 0.20, size * 0.80), QPointF(size * 0.80, size * 0.20))

    p.end()
    return pm


