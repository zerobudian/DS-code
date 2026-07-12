# -*- coding: utf-8 -*-
"""统计卡片 StatsCard（从 welcome.py 拆分）。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout, QLabel, QFrame

from src.gui.style import COLORS, RADII, SIZES, FONT_STACK, FONT_WEIGHTS, apply_shadow
from src.gui.animations import fade_in, fade_out


class StatsCard(QFrame):
    """2×2 统计卡片：API余额/缓存命中/供应商/当前模型。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("StatsCard")
        self._hidden = False
        self._last_values = {"balance": 0.0, "hit_rate": 0}
        self._value_labels = {}
        self._ticker_refs = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, SIZES["spacing"]["lg"])
        root.setSpacing(0)

        container = QFrame()
        container.setObjectName("StatsCardContainer")
        container.setStyleSheet(
            "QFrame#StatsCardContainer{"
            "background:" + COLORS['CARD'] + ";"
            "border:1px solid " + COLORS['DIVIDER'] + ";"
            "border-radius:" + RADII['xl'] + ";"
            "}"
        )
        container.setMinimumWidth(SIZES["stats_card"]["min_width"])
        container.setMaximumWidth(SIZES["stats_card"]["max_width"])
        apply_shadow(container, 'sm')

        c_lay = QVBoxLayout(container)
        c_lay.setContentsMargins(SIZES["spacing"]["lg"], SIZES["spacing"]["lg"],
                                 SIZES["spacing"]["lg"], SIZES["spacing"]["lg"])
        c_lay.setSpacing(SIZES["spacing"]["sm"])

        self.cells = []

        def _make_cell(idx: int, label: str, initial_value: str, key: str) -> QFrame:
            cell = QFrame()
            cell.setObjectName("SC_" + str(idx))
            cell.setStyleSheet(
                "QFrame#SC_" + str(idx) + "{"
                "background:" + COLORS['SURFACE'] + ";"
                "border:none;border-radius:" + RADII['md'] + ";"
                "}"
            )
            cell.setMinimumHeight(SIZES["stats_card"]["cell_height"])

            v = QVBoxLayout(cell)
            v.setContentsMargins(SIZES["spacing"]["md"], SIZES["spacing"]["sm"],
                                 SIZES["spacing"]["md"], SIZES["spacing"]["sm"])
            v.setSpacing(SIZES["spacing"]["xs"])

            num_label = QLabel(initial_value)
            num_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            num_label.setStyleSheet(
                "QLabel{color:" + COLORS['TEXT'] + ";"
                "font-size:18px;font-weight:" + str(FONT_WEIGHTS['bold']) + ";"
                "font-family:" + FONT_STACK + ";background:transparent;}"
            )
            v.addWidget(num_label, 1)

            desc_label = QLabel(label)
            desc_label.setAlignment(Qt.AlignLeft)
            desc_label.setStyleSheet(
                "QLabel{color:" + COLORS['TEXT_3'] + ";"
                "font-size:11px;font-weight:" + str(FONT_WEIGHTS['medium']) + ";"
                "font-family:" + FONT_STACK + ";background:transparent;}"
            )
            v.addWidget(desc_label)

            self.cells.append(num_label)
            self._value_labels[key] = num_label
            return cell

        gap = SIZES["spacing"]["sm"]
        row1 = QHBoxLayout()
        row1.setSpacing(gap)
        row1.addWidget(_make_cell(0, "API 余额", "0.00 CNY", "balance"), 1)
        row1.addWidget(_make_cell(1, "缓存命中", "0%", "hit_rate"), 1)
        c_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(gap)
        row2.addWidget(_make_cell(2, "供应商", "DeepSeek", "provider"), 1)
        row2.addWidget(_make_cell(3, "当前模型", "deepseek-v4-pro", "model"), 1)
        c_lay.addLayout(row2)

        wrap = QHBoxLayout()
        wrap.addStretch(1)
        wrap.addWidget(container)
        wrap.addStretch(1)
        root.addLayout(wrap)

        QTimer.singleShot(50, self.show_smooth)

    def refresh(self, language: str = "zh"):
        pass

    def set_stats(self, stats: dict) -> None:
        self._ticker_refs.clear()
        try:
            from src.gui.animations import number_ticker as _nt
        except Exception:
            _nt = None

        if "balance" in stats and "balance" in self._value_labels:
            try:
                new_val = float(stats["balance"])
                start_val = self._last_values["balance"]
                lbl = self._value_labels["balance"]
                if _nt is not None and lbl is not None and abs(new_val - start_val) > 0.001:
                    ref = _nt(lbl, start_val, new_val,
                              duration_ms=400, decimals=2,
                              prefix="", suffix=" CNY")
                    self._ticker_refs.append(ref)
                else:
                    lbl.setText(f"{new_val:.2f} CNY")
                self._last_values["balance"] = new_val
            except (TypeError, ValueError):
                pass

        if "hit_rate" in stats and "hit_rate" in self._value_labels:
            try:
                new_val = float(stats["hit_rate"])
                start_val = self._last_values["hit_rate"]
                lbl = self._value_labels["hit_rate"]
                if _nt is not None and lbl is not None and abs(new_val - start_val) > 0.5:
                    ref = _nt(lbl, start_val, new_val,
                              duration_ms=400, decimals=0,
                              prefix="", suffix="%")
                    self._ticker_refs.append(ref)
                else:
                    lbl.setText(f"{int(new_val)}%")
                self._last_values["hit_rate"] = new_val
            except (TypeError, ValueError):
                pass

        if "provider" in stats and "provider" in self._value_labels:
            self._value_labels["provider"].setText(str(stats["provider"]))

        if "model" in stats and "model" in self._value_labels:
            self._value_labels["model"].setText(str(stats["model"]))

    def show_smooth(self) -> None:
        if self.isVisible():
            return
        self.setVisible(True)
        fade_in(self, duration_ms=180)

    def hide_smooth(self) -> None:
        if not self.isVisible():
            return
        fade_out(self, duration_ms=180, on_finished=lambda: self.setVisible(False))

    def is_visible_safe(self) -> bool:
        return self.isVisible()
