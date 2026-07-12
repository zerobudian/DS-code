# -*- coding: utf-8 -*-
from __future__ import annotations

import math
import os
from typing import List, Optional

from PySide6.QtCore import (
    Qt, QSize, Signal, QPropertyAnimation, QEasingCurve, QTimer, QEvent,
    QPoint, Property,
)
from PySide6.QtGui import (
    QPixmap, QIcon, QColor, QPainter, QPen, QBrush, QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QFrame, QPushButton, QTextEdit, QScrollArea, QSplitter,
    QGraphicsOpacityEffect, QLineEdit, QSizePolicy, QMessageBox, QDialog,
)
from src.gui.style import COLORS, RADII, SPACING, FONT_STACK, rgba, tr


# ============================================================================
# 三个点按钮 (自定义 QPainter 绘制)
# ============================================================================
class ThreeDotsButton(QPushButton):
    """三个点按钮 - 用 QPainter 绘制小圆点"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(28, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:" + RADII['circle'] + ";}"
            "QPushButton:hover{background:" + COLORS['CARD_HOVER'] + ";}"
        )

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        color = QColor(COLORS['TEXT'])
        pen = QPen(color)
        pen.setWidth(1)
        p.setPen(pen)
        brush = QBrush(color)
        p.setBrush(brush)
        cx = self.width() // 2
        cy = self.height() // 2
        spacing = 5
        r = 2
        p.drawEllipse(cx - spacing - r // 2, cy - r // 2, r, r)
        p.drawEllipse(cx - r // 2, cy - r // 2, r, r)
        p.drawEllipse(cx + spacing - r // 2, cy - r // 2, r, r)
        p.end()



# ============================================================================
# 红色垃圾桶简笔画图标 (QPainter 绘制, 非 emoji)
# ============================================================================
class TrashIcon(QWidget):
    """红色线条画的垃圾桶简笔画"""

    def __init__(self, size=18, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        red = QColor(COLORS['ERROR'])
        pen = QPen(red)
        pen.setWidth(2)
        p.setPen(pen)
        w = self.width()
        h = self.height()
        pad = 3
        p.drawLine(pad + 1, pad + 4, w - pad - 1, pad + 4)
        p.drawLine(pad + 2, pad + 4, pad + 3, h - pad)
        p.drawLine(w - pad - 2, pad + 4, w - pad - 3, h - pad)
        p.drawLine(pad + 3, h - pad, w - pad - 3, h - pad)
        p.drawLine(pad + 4, pad, w - pad - 4, pad)
        p.drawLine(pad + 4, pad, pad + 4, pad + 3)
        p.drawLine(w - pad - 4, pad, w - pad - 4, pad + 3)
        mid_x = w // 2
        p.drawLine(mid_x, pad + 6, mid_x, h - pad - 2)
        p.end()

