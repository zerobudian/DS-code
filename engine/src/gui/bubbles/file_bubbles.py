# -*- coding: utf-8 -*-
"""文件标签 FilePill、文件预览 FilePreviewBubble 与 Diff 预览 DiffPreviewBubble。"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton, QTextEdit,
)

from src.gui.style import RADII, COLORS, FONT_STACK, FONT_CODE, FONT_WEIGHTS, SIZES
from src.gui._gui_helpers import _load_icon
from src.gui.animations import SmoothColorButton
from src.gui.bubbles._helpers import _try_pm

# ======================================================================
# FilePill — 文件标签 (保留给输入区使用)
# ======================================================================
class FilePill(QFrame):
    remove_clicked = Signal()

    def __init__(self, filename: str, size_bytes: int = 0, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            "background:" + COLORS['CARD'] + ";"
            "border:none;"
            "border-radius:" + RADII['xs'] + ";"
        )

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        icon = QLabel()
        icon.setPixmap(_try_pm(["file_16.png"], 16))
        icon.setStyleSheet("background:transparent;")
        lay.addWidget(icon)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        name = QLabel(str(filename))
        name.setStyleSheet(
            "color:" + COLORS['TEXT'] + ";"
            "font-size:14px; font-weight:" + str(FONT_WEIGHTS['medium']) + "; font-family:" + FONT_STACK + ";"
            "background:transparent;"
        )
        col.addWidget(name)

        if size_bytes and size_bytes > 0:
            kb = size_bytes / 1024.0
            size_txt = f"{kb:.1f} KB" if kb < 1024 else f"{kb/1024:.1f} MB"
            sub = QLabel(size_txt)
            sub.setStyleSheet(
                "color:" + COLORS['TEXT_4'] + ";"
                "font-size:11px; font-family:" + FONT_STACK + ";"
                "background:transparent;"
            )
            col.addWidget(sub)

        lay.addLayout(col, 1)

        btn = QPushButton()
        btn.setFixedSize(20, 20)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setIcon(_load_icon("close_16.png", 12))
        btn.setIconSize(QSize(12, 12))
        btn.setStyleSheet(
            "QPushButton{background:transparent; border:none;}"
            "QPushButton:hover{background:" + COLORS['PRIM_LIGHT'] + "; border-radius:" + RADII['xs'] + ";}"
        )
        btn.clicked.connect(self.remove_clicked.emit)
        lay.addWidget(btn)

# ======================================================================
# FilePreviewBubble — 文件预览 (用于右侧面板或对话区)
# ======================================================================
class FilePreviewBubble(QFrame):
    clicked = Signal()

    def __init__(self, filepath: str, content: str, max_lines: int = 50, parent=None):
        super().__init__(parent)
        self.filepath = filepath
        self.content = content
        self._full = content
        self._collapsed = False

        self.setStyleSheet("background:transparent;")

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        icon = QLabel()
        icon.setPixmap(_try_pm(["file_16.png"], 40))
        icon.setAlignment(Qt.AlignCenter)
        icon.setFixedSize(40, 40)
        icon.setStyleSheet("background:transparent;")
        root.addWidget(icon, 0, Qt.AlignTop)

        card = QFrame()
        card.setStyleSheet(
            "background:" + COLORS['CARD'] + ";"
            "border:none;"
            "border-radius:" + RADII['md'] + ";"
        )
        card.setMaximumWidth(SIZES["bubble"]["max_width"])

        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 10, 14, 12)
        cl.setSpacing(6)

        head = QHBoxLayout()
        title = QLabel(os.path.basename(filepath))
        title.setStyleSheet(
            "color:" + COLORS['TEXT'] + ";font-size:14px;font-weight:" + str(FONT_WEIGHTS['semibold']) + ";"
            "font-family:" + FONT_STACK + ";background:transparent;"
        )
        head.addWidget(title, 1)

        toggle = QPushButton()
        toggle.setFixedSize(22, 22)
        toggle.setCursor(Qt.PointingHandCursor)
        toggle.setIcon(_load_icon("chevron_down_18.png", 14))
        toggle.setIconSize(QSize(14, 14))
        toggle.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:" + RADII['circle'] + ";}"
            "QPushButton:hover{background:" + COLORS['PRIM_LIGHT'] + ";}"
        )
        toggle.clicked.connect(self._toggle)
        head.addWidget(toggle)
        self._toggle_btn = toggle
        cl.addLayout(head)

        self.body = QTextEdit()
        self.body.setReadOnly(True)
        preview = "\n".join(content.split("\n")[:max_lines]) if content else ""
        self.body.setPlainText(preview)
        self.body.setStyleSheet(
            "QTextEdit{background:" + COLORS['CODE_BGD'] + ";color:" + COLORS['TEXT'] + ";"
            "border:1px solid " + COLORS['BORDER'] + ";border-radius:" + RADII['sm'] + ";"
            "font-family:" + FONT_CODE + ";font-size:12px;"
            "padding:6px;}"
        )
        self.body.setMinimumHeight(120)
        self.body.setMaximumHeight(420)
        cl.addWidget(self.body)

        root.addWidget(card, 1, Qt.AlignLeft)

    def _toggle(self):
        self._collapsed = not self._collapsed
        self.body.setVisible(not self._collapsed)
        if self._collapsed:
            self._toggle_btn.setIcon(_load_icon("chevron_right_18.png", 14))
        else:
            self._toggle_btn.setIcon(_load_icon("chevron_down_18.png", 14))
        self._toggle_btn.setIconSize(QSize(14, 14))

# ======================================================================
# DiffPreviewBubble — Diff 预览，带 Apply/Reject 按钮
# ======================================================================
class DiffPreviewBubble(QFrame):
    apply_clicked = Signal(str)   # 传出 filepath
    reject_clicked = Signal(str)

    def __init__(self, filepath: str, diff_text: str, parent=None):
        super().__init__(parent)
        self.filepath = filepath
        self.setStyleSheet("background:transparent;")

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        icon = QLabel()
        icon.setPixmap(_try_pm(["whale_40.png", "whale_36.png", "whale_32.png"], 40))
        icon.setAlignment(Qt.AlignCenter)
        icon.setFixedSize(40, 40)
        icon.setStyleSheet("background:transparent;")
        root.addWidget(icon, 0, Qt.AlignTop)

        card = QFrame()
        card.setStyleSheet(
            "background:" + COLORS['CARD'] + ";"
            "border:none;"
            "border-radius:" + RADII['lg'] + ";"
        )
        card.setMaximumWidth(SIZES["bubble"]["max_width"])

        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(8)

        head_row = QHBoxLayout()
        title = QLabel("变更预览 · " + os.path.basename(filepath))
        title.setStyleSheet(
            "color:" + COLORS['TEXT'] + ";font-size:14px;font-weight:" + str(FONT_WEIGHTS['bold']) + ";"
            "font-family:" + FONT_STACK + ";background:transparent;"
        )
        head_row.addWidget(title, 1)
        cl.addLayout(head_row)

        # Diff 文本区: 按行着色 + 背景色
        self.editor = QTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setMinimumHeight(180)
        self.editor.setMaximumHeight(520)

        doc_html = self._render_diff_html(diff_text)
        self.editor.setHtml(doc_html)
        self.editor.setStyleSheet(
            "QTextEdit{background:" + COLORS['CODE_BGD'] + ";"
            "border:1px solid " + COLORS['BORDER'] + ";border-radius:" + RADII['code'] + ";"
            "font-family:" + FONT_CODE + ";font-size:12px;"
            "padding:8px;}"
        )
        cl.addWidget(self.editor, 1)

        # 按钮行
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        # 取消变更 — 次要风格
        rej_btn = SmoothColorButton("取消变更")
        rej_btn.setFixedHeight(36)
        rej_btn.set_secondary_style(
            bg=COLORS['CARD'],
            text=COLORS['TEXT'],
            border_radius=RADII['lg'],
            border="1px solid " + COLORS['BORDER'],
        )
        rej_btn._hover_bg = COLORS['PRIM_LIGHT']
        rej_btn.clicked.connect(lambda: self.reject_clicked.emit(self.filepath))
        btn_row.addWidget(rej_btn)

        # 应用变更 — 主风格
        ap_btn = SmoothColorButton("应用变更")
        ap_btn.setFixedHeight(36)
        ap_btn.set_primary_style(
            bg=COLORS['PRIM_DARK'],
            text=COLORS['CARD'],
            border_radius=RADII['lg'],
        )
        ap_btn.clicked.connect(lambda: self.apply_clicked.emit(self.filepath))
        btn_row.addWidget(ap_btn)

        cl.addLayout(btn_row)
        root.addWidget(card, 1, Qt.AlignLeft)

    def _render_diff_html(self, diff_text: str) -> str:
        """给 unified diff 着色"""
        lines = (diff_text or "").split("\n")
        html_lines = []
        for line in lines[:400]:  # 防止过长
            if line.startswith("@@"):
                html_lines.append(
                    f'<div style="background:{COLORS["DIFF_HEAD"]};color:{COLORS["TEXT"]};'
                    f'padding:2px 6px;font-weight:{FONT_WEIGHTS["semibold"]};">{self._esc(line)}</div>'
                )
            elif line.startswith("+") and not line.startswith("+++"):
                html_lines.append(
                    f'<div style="background:{COLORS["DIFF_ADD"]};color:{COLORS["DIFF_ADD_TEXT"]};'
                    f'padding:2px 6px;">{self._esc(line)}</div>'
                )
            elif line.startswith("-") and not line.startswith("---"):
                html_lines.append(
                    f'<div style="background:{COLORS["DIFF_REMOVE"]};color:{COLORS["DIFF_REMOVE_TEXT"]};'
                    f'padding:2px 6px;">{self._esc(line)}</div>'
                )
            else:
                html_lines.append(
                    f'<div style="color:{COLORS["TEXT_2"]};padding:2px 6px;">{self._esc(line)}</div>'
                )
        body = "".join(html_lines)
        return (
            '<div style="white-space:pre;font-family:' + FONT_CODE + ';">' + body + '</div>'
        )

    @staticmethod
    def _esc(text: str) -> str:
        import html as _html
        return _html.escape(text)
