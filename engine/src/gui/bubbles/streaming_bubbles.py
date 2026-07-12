# -*- coding: utf-8 -*-
"""流式输出气泡：StreamingCommandBubble 与 StreamingStatusBubble。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QFrame, QTextEdit,
)

from src.gui.style import RADII, COLORS, FONT_STACK, FONT_CODE, FONT_WEIGHTS, SIZES
from src.gui.bubbles._helpers import _try_pm

# ======================================================================
# StreamingCommandBubble — 命令实时输出气泡
# ======================================================================
class StreamingCommandBubble(QFrame):
    cancel_clicked = Signal()

    def __init__(self, command: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:transparent;")
        self._exit_code = None
        self._start_tick = 0

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
        cl.setContentsMargins(14, 10, 14, 12)
        cl.setSpacing(6)

        head = QHBoxLayout()
        tag = QLabel("$ " + str(command))
        tag.setStyleSheet(
            "color:" + COLORS['TEXT'] + ";font-size:13px;font-weight:" + str(FONT_WEIGHTS['bold']) + ";"
            "font-family:" + FONT_CODE + ";background:transparent;"
        )
        head.addWidget(tag, 1)

        self.status_lbl = QLabel("运行中…")
        self.status_lbl.setStyleSheet(
            "color:" + COLORS['TEXT_3'] + ";font-size:12px;"
            "font-family:" + FONT_STACK + ";background:transparent;"
        )
        head.addWidget(self.status_lbl)
        cl.addLayout(head)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(120)
        self.output.setMaximumHeight(420)
        self.output.setStyleSheet(
            "QTextEdit{background:" + COLORS['CODE_BGD'] + ";color:" + COLORS['TEXT'] + ";"
            "border:1px solid " + COLORS['BORDER'] + ";border-radius:" + RADII['code'] + ";"
            "font-family:" + FONT_CODE + ";font-size:12px;"
            "padding:8px;}"
        )
        cl.addWidget(self.output, 1)

        root.addWidget(card, 1, Qt.AlignLeft)

    def append_output(self, text: str):
        if not text:
            return
        cursor = self.output.textCursor()
        cursor.movePosition(cursor.End)
        cursor.insertText(text)
        sb = self.output.verticalScrollBar()
        sb.setValue(sb.maximum())

    def set_exit_code(self, code: int):
        self._exit_code = int(code)
        if code == 0:
            self.status_lbl.setText("完成 (exit 0)")
        else:
            self.status_lbl.setText(f"结束 (exit {code})")

# ======================================================================
# StreamingStatusBubble — 轻量状态行气泡 (coding/reading/writing/done/error)
# 多行追加；等宽 13px；按 status_type 给前缀上色。
# 颜色来自 style.STATUS_COLORS（与 ReasoningBubble 保持一致）
# ======================================================================
_STATUS_PREFIX_MAP = {
    "coding": "[CODE] ",
    "reading": "[READ] ",
    "writing": "[WRITE] ",
    "done": "[DONE] ",
    "error": "[ERR] ",
    "terminal": "[RUN] ",
    "think": "[THINK] ",
    "thinking": "[THINK] ",
}

class StreamingStatusBubble(QFrame):
    """显示多行轻量状态 (coding/reading/writing/done/error)。
    用法: b = StreamingStatusBubble(); b.append("读取文件", "reading")"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:transparent;")
        self._lines: list = []

        root = QHBoxLayout(self)
        root.setContentsMargins(4, 2, 4, 2)
        root.setSpacing(8)

        icon = QLabel()
        icon.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        icon.setFixedHeight(22)
        icon.setStyleSheet(
            "background:" + COLORS['THINK_BGD'] + ";"
            "border-radius:11px;color:" + COLORS['TEXT_2'] + ";"
            "font-family:" + FONT_STACK + ";font-size:11px;"
            "padding:3px 6px;"
        )
        icon.setText("·")
        icon.setTextInteractionFlags(Qt.NoTextInteraction)
        root.addWidget(icon, 0, Qt.AlignTop)

        self.body = QLabel("")
        self.body.setStyleSheet(
            "color:" + COLORS['TEXT'] + ";"
            "font-family:" + FONT_CODE + ";"
            "font-size:13px;line-height:1.6;"
            "background:transparent;"
        )
        self.body.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.body.setTextFormat(Qt.RichText)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setOpenExternalLinks(False)
        root.addWidget(self.body, 1)

    # ----- public -----
    def append(self, text: str, status_type: str = "writing"):
        """追加一行；按 status_type 上色前缀"""
        from src.gui.style import STATUS_COLORS
        status_type = str(status_type or "writing").lower()
        if status_type not in _STATUS_PREFIX_MAP:
            status_type = "writing"
        color = STATUS_COLORS.get(status_type, COLORS['TEXT'])
        prefix = _STATUS_PREFIX_MAP.get(status_type, "[WRITE] ")
        safe = self._escape(str(text or ""))
        html_line = (
            '<span style="color:' + color + ';font-weight:' + str(FONT_WEIGHTS['semibold']) + ';">'
            + self._escape(prefix) + '</span>'
            + '<span style="color:' + COLORS['TEXT'] + ';">' + safe + '</span>'
        )
        self._lines.append(html_line)
        self._rerender()

    def append_raw(self, html_line: str):
        """直接追加已有的 HTML 行"""
        if html_line:
            self._lines.append(str(html_line))
            self._rerender()

    def clear(self):
        self._lines = []
        self._rerender()

    # ----- internal -----
    @staticmethod
    def _escape(s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("\n", "<br/>")
        )

    def _rerender(self):
        joined = "<br/>".join(self._lines) if self._lines else ""
        self.body.setText(joined)
