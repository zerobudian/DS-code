# -*- coding: utf-8 -*-
"""Shared helpers for icon generation."""
# -*- coding: utf-8 -*-
"""icon_factory.py —— 动态生成统一风格的 PNG 图标。

图标风格与 assets/whale.png 保持一致：
  - 主色调：鲸鱼蓝 #4A86F7
  - 次色调：浅蓝灰 #8FA7D9
  - 状态色：成功绿 #10B981、警告橙 #F59E0B、错误红 #EF4444
  - 按钮图标：蓝色填充圆 + 白色符号（如 arrow_up / stop）
  - 线型图标：统一蓝色/浅蓝灰色轮廓，圆角一致
  - 透明背景、扁平、简洁、非游戏化

自动生成以下缺失图标（均提供 18px / 24px 两个版本）：
  - arrow_up / stop / paperclip / bubble / keyboard / terminal / paper
  - settings(gear) / moon / sun
  - chevron_left / chevron_right / chevron_up / chevron_down
  - history / memory / model / permission / mode
  - trash / refresh / close / copy / check
  - warning / info / search / filter / sort
  - pin / unpin / plus / minus
  - file / folder / question
  - checkbox_checked / checkbox_empty
"""

from typing import Tuple
import math
import os
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import (
    QPixmap, QPainter, QColor, QPen, QPainterPath, QPolygonF, QFont,
)
from PySide6.QtWidgets import QApplication


# 鲸鱼主蓝色调，与 assets/whale.png 协调
WHALE_BLUE = QColor(74, 134, 247)          # #4A86F7
WHALE_BLUE_LIGHT = QColor(120, 169, 250)   # #78A9FA
PALE_BLUE_GREY = QColor(143, 167, 217)     # #8FA7D9 浅蓝灰
PAPERCLIP_GREY = QColor(136, 153, 184)     # #8899B8 偏灰浅蓝
WHITE = QColor(255, 255, 255)
SUCCESS_GREEN = QColor(16, 185, 129)       # #10B981
WARNING_ORANGE = QColor(245, 158, 11)      # #F59E0B
ERROR_RED = QColor(239, 68, 68)            # #EF4444

# 字体栈（与 style.py 保持一致，避免循环导入）
FONT_STACK = 'system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", "Helvetica Neue", Arial, sans-serif'

# ---------------------------------------------------------------------------
# 路径与图标清单
# ---------------------------------------------------------------------------

ASSETS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "assets"
)

# 所有图标基础名（与 _registry._MAKERS 的键一一对应）
_ALL_ICON_NAMES = [
    "arrow_up", "arrow_up_plain", "arrow_up_white", "arrow_up_disabled",
    "arrow_up_black",
    "stop", "stop_white", "stop_black",
    "paperclip", "bubble", "keyboard", "terminal", "paper",
    "settings", "moon", "sun",
    "chevron_left", "chevron_right", "chevron_up", "chevron_down",
    "history", "memory", "model", "permission", "mode",
    "trash", "refresh", "close", "copy", "check",
    "warning", "info", "search", "filter", "sort",
    "pin", "unpin", "plus", "minus",
    "file", "folder", "question", "pencil",
    "checkbox_checked", "checkbox_empty",
]

# 每个图标提供的尺寸
ICONS_18_24 = {name: [18, 24] for name in _ALL_ICON_NAMES}

# 所需图标文件名集合（如 "arrow_up_18.png"）
REQUIRED_ICONS = set()
for _name in _ALL_ICON_NAMES:
    for _size in (18, 24):
        REQUIRED_ICONS.add(f"{_name}_{_size}.png")


# ---------------------------------------------------------------------------
# 绘图工具函数
# ---------------------------------------------------------------------------

def _q(value):
    """转换为 float（QReal），用于 QPainter 宽度等参数。"""
    return float(value)


def _alpha(color: QColor, alpha: float) -> QColor:
    """返回带 alpha 通道的 QColor 副本。"""
    c = QColor(color)
    c.setAlphaF(alpha)
    return c


def _begin(size: int):
    """创建透明背景 QPixmap 并返回 (QPainter, QPixmap)。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    return p, pm


def _circle_btn_bg(p: QPainter, size: int):
    """绘制蓝色圆形按钮背景（用于 arrow_up / stop 等按钮图标）。"""
    p.setBrush(WHALE_BLUE)
    p.setPen(Qt.NoPen)
    p.drawEllipse(0, 0, size, size)


def _set_stroke_pen(p: QPainter, size: int, color: QColor):
    """设置线宽与 size 成比例的画笔。"""
    w = max(1.0, size * 0.06)
    pen = QPen(color, _q(w))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)


def _set_fill(p: QPainter, color: QColor):
    """设置填充画刷并清除画笔。"""
    p.setBrush(color)
    p.setPen(Qt.NoPen)


def _center_rect(size: int, icon_size: float) -> QRectF:
    """返回在 size×size 画布中居中的 QRectF。"""
    margin = (size - icon_size) / 2.0
    return QRectF(margin, margin, icon_size, icon_size)


