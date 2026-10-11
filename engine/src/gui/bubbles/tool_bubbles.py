# -*- coding: utf-8 -*-
"""工具调用卡片 ToolCallBubble 与计划气泡 PlanBubble。"""
from __future__ import annotations

import html

from PySide6.QtCore import (
    Qt, Signal, QSize,
)
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QFrame, QSizePolicy, QPushButton, QWidget,
)

from src.gui.style import RADII, COLORS, FONT_STACK, FONT_CODE, FONT_WEIGHTS, SIZES, tr
from src.gui._gui_helpers import _load_icon
from src.gui.bubbles.animation_icons import AnimatedWhaleIcon
from src.gui.bubbles._helpers import _try_pm

# ======================================================================
# ToolCallBubble — 工具调用卡片（带头顶动效，支持折叠）
# ======================================================================
class ToolCallBubble(QFrame):
    """工具调用气泡：紧凑内联卡片，显示工具名+状态，支持折叠展开。"""

    toggled = Signal(bool)
    content_changed = Signal()

    def __init__(self, tool_name: str, parent=None):
        super().__init__(parent)
        self._tool_name = str(tool_name or "tool")
        self._args_summary = ""
        self._result_summary = ""
        self._done = False
        self._success = False
        self._expanded = True
        self._animating = False
        self.setStyleSheet("background:transparent;")

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 2, 4, 2)
        root.setSpacing(0)

        # 卡片 — 紧凑圆角，左侧彩色边条指示状态
        self._card = QFrame()
        self._card.setMaximumWidth(SIZES["bubble"]["max_width"])
        self._card.setCursor(Qt.PointingHandCursor)
        self._card.mouseReleaseEvent = lambda e: self._toggle()

        cl = QVBoxLayout(self._card)
        cl.setContentsMargins(10, 6, 10, 6)
        cl.setSpacing(4)

        # Header 行：状态点 + 工具名 + 折叠箭头
        hdr = QHBoxLayout()
        hdr.setSpacing(6)

        self._dot = QLabel("●")
        self._dot.setFixedSize(14, 14)
        self._dot.setAlignment(Qt.AlignCenter)
        hdr.addWidget(self._dot)

        self._title = QLabel(self._tool_name)
        self._title.setStyleSheet(
            "color:" + COLORS['TEXT_2'] + ";font-size:12px;font-weight:" + str(FONT_WEIGHTS['semibold']) + ";"
            "font-family:" + FONT_CODE + ";background:transparent;"
        )
        hdr.addWidget(self._title, 1)

        self._arrow = QLabel("▾")
        self._arrow.setFixedSize(14, 14)
        self._arrow.setAlignment(Qt.AlignCenter)
        self._arrow.setStyleSheet("color:" + COLORS['TEXT_3'] + ";font-size:10px;background:transparent;")
        hdr.addWidget(self._arrow)
        cl.addLayout(hdr)

        # Content 区 — 默认展开
        self._content = QFrame()
        self._content.setStyleSheet("background:transparent;")
        cvl = QVBoxLayout(self._content)
        cvl.setContentsMargins(4, 2, 4, 4)
        cvl.setSpacing(2)

        self._args_label = QLabel("")
        self._args_label.setWordWrap(True)
        self._args_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._args_label.setStyleSheet(
            "color:" + COLORS['TEXT_3'] + ";font-size:11px;"
            "font-family:" + FONT_CODE + ";background:transparent;"
        )
        cvl.addWidget(self._args_label)

        self._result_label = QLabel("")
        self._result_label.setWordWrap(True)
        self._result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._result_label.setStyleSheet(
            "color:" + COLORS['TEXT'] + ";font-size:11px;"
            "font-family:" + FONT_STACK + ";background:transparent;"
        )
        cvl.addWidget(self._result_label)
        cl.addWidget(self._content)

        root.addWidget(self._card, 0, Qt.AlignLeft)
        self._apply_style()

    def _apply_style(self):
        """根据当前状态刷新卡片样式。"""
        if self._done:
            c = COLORS['SUCCESS'] if self._success else COLORS['ERROR']
            self._dot.setStyleSheet(f"color:{c};font-size:10px;background:transparent;")
            left = c
        else:
            self._dot.setStyleSheet(f"color:{COLORS['ACCENT']};font-size:10px;background:transparent;")
            left = COLORS['ACCENT']
        self._card.setStyleSheet(
            f"QFrame{{background:{COLORS['CARD']};border:none;"
            f"border-left:3px solid {left};"
            f"border-radius:{RADII['sm']};}}"
            f"QFrame:hover{{background:{COLORS['PRIM_LIGHT']};}}"
        )

    def set_args_summary(self, text: str):
        self._args_summary = str(text or "")[:200]
        self._args_label.setText(self._args_summary)
        self.content_changed.emit()

    def set_result_summary(self, text: str):
        self._result_summary = str(text or "")[:300]
        self._result_label.setText(self._result_summary)
        self.content_changed.emit()

    def set_text(self, text: str):
        self.set_result_summary(text)

    def update_result(self, success: bool, result_summary: str):
        self.set_result_summary(result_summary)
        if not self._expanded:
            self.expand(animate=False)
        self.mark_done(success, collapse=False)

    def mark_done(self, success: bool = True, collapse: bool = False):
        self._done = True
        self._success = bool(success)
        self._title.setText(self._tool_name)
        self._apply_style()
        self._content.setMaximumHeight(16777215)
        self._content.adjustSize()
        self.adjustSize()
        self.content_changed.emit()
        if collapse:
            self.collapse(animate=True)

    def _toggle(self):
        if self._animating:
            return
        if self._expanded:
            self.collapse(animate=True)
        else:
            self.expand(animate=True)

    def collapse(self, animate: bool = True):
        if not self._expanded:
            return
        self._expanded = False
        self._arrow.setText("▸")
        self.toggled.emit(False)
        if animate:
            self._animate_height(self._content, self._content.height() or 1, 0, duration_ms=200,
                                 on_finish=lambda: self._on_collapsed(self._content))
        else:
            self._on_collapsed(self._content)

    def expand(self, animate: bool = True):
        if self._expanded:
            return
        self._expanded = True
        self._arrow.setText("▾")
        self.toggled.emit(True)
        if animate:
            self._content.setVisible(True)
            self._content.setMaximumHeight(16777215)
            self._content.adjustSize()
            end_h = self._content.sizeHint().height() or 1
            start_h = 1 if self._content.height() < 2 else self._content.height()
            self._animate_height(self._content, start_h, end_h,
                                 duration_ms=280,
                                 on_finish=lambda: self._on_expanded(self._content))
        else:
            self._content.setVisible(True)
            self._content.setMaximumHeight(16777215)
            self._animating = False

    def _on_collapsed(self, widget: QFrame):
        widget.setVisible(False)
        widget.setMaximumHeight(0)
        self._animating = False

    def _on_expanded(self, widget: QFrame):
        widget.setMaximumHeight(16777215)
        self._animating = False

    def _animate_height(self, widget, start_h, end_h, duration_ms=260, on_finish=None):
        widget.setMaximumHeight(16777215 if end_h > 0 else 0)
        widget.setVisible(end_h > 0)
        if on_finish:
            try:
                on_finish()
            except Exception:
                pass
        self._animating = False

    def is_expanded(self) -> bool:
        return self._expanded

    def rebuild_style(self):
        """主题切换后重建工具气泡样式。"""
        try:
            self._apply_style()
            self._title.setStyleSheet(
                "color:" + COLORS['TEXT_2'] + ";font-size:12px;font-weight:" + str(FONT_WEIGHTS['semibold']) + ";"
                "font-family:" + FONT_CODE + ";background:transparent;"
            )
            self._args_label.setStyleSheet(
                "color:" + COLORS['TEXT_3'] + ";font-size:11px;"
                "font-family:" + FONT_CODE + ";background:transparent;"
            )
            self._result_label.setStyleSheet(
                "color:" + COLORS['TEXT'] + ";font-size:11px;"
                "font-family:" + FONT_STACK + ";background:transparent;"
            )
            self._dot.setStyleSheet(self._dot.styleSheet())
            self._arrow.setStyleSheet("color:" + COLORS['TEXT_3'] + ";font-size:10px;background:transparent;")

            if self._args_summary:
                self.set_args_summary(self._args_summary)
            if self._result_summary:
                self.set_result_summary(self._result_summary)
            if self._done:
                self._refresh_header()
        except Exception:
            pass

# ======================================================================
# PlanBubble — 计划气泡 (Planning 阶段展示，带 "确认" / "重写" 按钮)
# ======================================================================
class PlanBubble(QFrame):
    confirm_clicked = Signal()
    reject_clicked = Signal()

    def __init__(self, plan_text: str, parent=None):
        super().__init__(parent)
        self._plan_text = plan_text
        self.setStyleSheet("background:transparent;")
        self.setFocusPolicy(Qt.StrongFocus)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        # 使用 28px 清单图标替代鲸鱼头像，降低视觉重量
        icon = QLabel()
        icon.setPixmap(_try_pm(["checkbox_empty_24.png", "info_24.png"], 24))
        icon.setAlignment(Qt.AlignCenter)
        icon.setFixedSize(28, 28)
        icon.setStyleSheet("background:transparent;")
        root.addWidget(icon, 0, Qt.AlignTop)

        card = QFrame()
        card.setStyleSheet(
            "background:" + COLORS['CARD'] + ";"
            "border:none;"
            "border-radius:" + RADII['card'] + ";"
        )
        card.setMaximumWidth(SIZES["bubble"]["max_width"])

        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(10)

        header = QLabel(tr("plan_bubble_title"))
        header.setStyleSheet(
            "color:" + COLORS['TEXT'] + ";"
            "font-size:16px; font-weight:" + str(FONT_WEIGHTS['bold']) + "; font-family:" + FONT_STACK + ";"
            "background:transparent;"
        )
        cl.addWidget(header)

        # 将计划文本解析为带序号的步骤列表
        steps_widget = self._build_steps_widget(plan_text)
        cl.addWidget(steps_widget, 1)

        # 按钮行
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        try:
            from src.gui.settings_dialog._sections import _button
            reject_btn = _button(tr("plan_bubble_cancel"), primary=False, height=36, width=90)
            confirm_btn = _button(tr("plan_bubble_confirm"), primary=True, height=36, width=120)
        except Exception:
            reject_btn = QPushButton(tr("plan_bubble_cancel"))
            reject_btn.setFixedHeight(36)
            confirm_btn = QPushButton(tr("plan_bubble_confirm"))
            confirm_btn.setFixedHeight(36)

        reject_btn.clicked.connect(self.reject_clicked.emit)
        confirm_btn.clicked.connect(self.confirm_clicked.emit)
        btn_row.addWidget(reject_btn)
        btn_row.addWidget(confirm_btn)

        cl.addLayout(btn_row)
        root.addWidget(card, 1, Qt.AlignLeft)

    def _build_steps_widget(self, plan_text: str):
        """把计划文本按行解析为步骤列表。"""
        container = QWidget()
        container.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        steps = [s.strip() for s in (plan_text or "").splitlines() if s.strip()]
        if not steps:
            empty = QLabel(tr("plan_bubble_empty"))
            empty.setWordWrap(True)
            empty.setStyleSheet(
                "color:" + COLORS['TEXT_2'] + ";font-size:14px;line-height:1.7;"
                "font-family:" + FONT_STACK + ";background:transparent;"
            )
            lay.addWidget(empty)
            return container

        for i, step in enumerate(steps, 1):
            row = QHBoxLayout()
            row.setSpacing(8)
            row.setContentsMargins(0, 0, 0, 0)
            idx_lbl = QLabel(f"{i}.")
            idx_lbl.setStyleSheet(
                "color:" + COLORS['ACCENT'] + ";font-size:14px;font-weight:" + str(FONT_WEIGHTS['bold']) + ";"
                "font-family:" + FONT_STACK + ";background:transparent;"
            )
            idx_lbl.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            idx_lbl.setFixedWidth(24)
            body_lbl = QLabel(html.escape(step))
            body_lbl.setWordWrap(True)
            body_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            body_lbl.setStyleSheet(
                "color:" + COLORS['TEXT'] + ";font-size:14px;line-height:1.7;"
                "font-family:" + FONT_STACK + ";background:transparent;"
            )
            row.addWidget(idx_lbl, 0, Qt.AlignTop)
            row.addWidget(body_lbl, 1)
            lay.addLayout(row)
        return container

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter:
            self.confirm_clicked.emit()
        elif event.key() == Qt.Key_Escape:
            self.reject_clicked.emit()
        else:
            super().keyPressEvent(event)

    def rebuild_style(self):
        """主题切换后重建计划气泡内所有 QLabel 的颜色。"""
        try:
            for lbl in self.findChildren(QLabel):
                # 标题
                if lbl.text() == tr("plan_bubble_title"):
                    lbl.setStyleSheet(
                        "color:" + COLORS['TEXT'] + ";"
                        "font-size:16px; font-weight:" + str(FONT_WEIGHTS['bold']) + "; font-family:" + FONT_STACK + ";"
                        "background:transparent;"
                    )
                # 序号
                elif lbl.text().endswith(".") and len(lbl.text()) <= 4:
                    lbl.setStyleSheet(
                        "color:" + COLORS['ACCENT'] + ";font-size:14px;font-weight:" + str(FONT_WEIGHTS['bold']) + ";"
                        "font-family:" + FONT_STACK + ";background:transparent;"
                    )
                # 步骤正文 / 空提示
                else:
                    lbl.setStyleSheet(
                        "color:" + COLORS['TEXT'] + ";font-size:14px;line-height:1.7;"
                        "font-family:" + FONT_STACK + ";background:transparent;"
                    )
        except Exception:
            pass
