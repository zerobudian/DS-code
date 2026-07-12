# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import (
    Qt, Signal,
)
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
)
from src.gui.style import COLORS, RADII, FONT_STACK, FONT_WEIGHTS, tr


class StatusBar(QFrame):
    """底部紧凑信息栏 — 模型 · 缓存 · 成本 · 余额 · 模式 · 权限。

    整行可点击，打开快捷设置弹窗。
    """

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("StatusBar")
        self.setCursor(Qt.PointingHandCursor)
        self._info_text = "V4 Pro · 普通 · 完全"

        self._lbl = QLabel(self._info_text)
        self._lbl.setAlignment(Qt.AlignCenter)
        self._apply_style()
        self.mousePressEvent = lambda e: self.clicked.emit()

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 5, 12, 5)
        lay.addWidget(self._lbl, 1)

        self._model = "deepseek-v4-pro"
        self._mode = "normal"
        self._perm = "full"
        self._cost = 0.0
        self._cache = 0
        self._balance = 0.0

    def _apply_style(self):
        self.setStyleSheet(
            "QFrame#StatusBar{"
            "background:" + COLORS['SURFACE'] + ";"
            "border:1px solid " + COLORS['DIVIDER'] + ";"
            "border-radius:" + RADII['md'] + ";"
            "}"
            "QFrame#StatusBar:hover{"
            "background:" + COLORS['PRIM_LIGHT'] + ";"
            "border-color:" + COLORS['ACCENT'] + ";"
            "}"
        )
        self._lbl.setStyleSheet(
            "QLabel{color:" + COLORS['TEXT_2'] + ";font-size:11px;"
            "font-weight:" + str(FONT_WEIGHTS['medium']) + ";"
            "font-family:" + FONT_STACK + ";background:transparent;}"
        )

    def _refresh(self):
        m = self._model
        short_m = "V4 Flash" if "flash" in str(m).lower() else ("V4 Pro" if "pro" in str(m).lower() else str(m)[:10])
        short_mode = {"normal": "普通", "plan": "规划", "ultra": "Ultra"}.get(self._mode, self._mode)
        short_perm = {"full": "完全", "coder": "仅编码", "reviewer": "仅审查"}.get(self._perm, self._perm)
        parts = []
        parts.append(short_m)
        parts.append(short_mode)
        parts.append(short_perm)
        if self._cache is not None:
            parts.append(f"缓存 {int(self._cache)}%")
        if self._cost:
            parts.append(f"¥{self._cost:.4f}")
        if self._balance:
            parts.append(f"余额 ¥{self._balance:.2f}")
        self._lbl.setText(" · ".join(parts))

    def set_model(self, v):
        self._model = str(v or "")
        self._refresh()

    def set_permission(self, v):
        self._perm = str(v or "full")
        self._refresh()

    def set_mode(self, v):
        self._mode = str(v or "normal")
        self._refresh()

    def set_usage(self, cost, cache_hit_rate):
        try:
            self._cost = float(cost or 0)
        except (TypeError, ValueError):
            self._cost = 0.0
        try:
            self._cache = float(cache_hit_rate or 0)
        except (TypeError, ValueError):
            self._cache = 0
        self._refresh()

    def set_balance(self, v):
        try:
            self._balance = float(v or 0)
        except (TypeError, ValueError):
            self._balance = 0.0
        self._refresh()

    def set_cache(self, v):
        self.set_cache_rate(v)

    def set_cache_rate(self, rate):
        try:
            self._cache = float(rate or 0)
        except (TypeError, ValueError):
            self._cache = 0
        self._refresh()

    def set_vendor(self, v):
        pass

    def set_message(self, msg):
        self._lbl.setText(str(msg))

    def set_index_status(self, status):
        pass

    def retranslate_ui(self, language):
        self._refresh()
