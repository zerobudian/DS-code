# -*- coding: utf-8 -*-
"""内置终端面板：底部可折叠的交互式 shell。"""

from PySide6.QtCore import Qt, QTimer, Signal, QSize
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QLineEdit,
    QPushButton, QLabel, QSizePolicy
)

from src.gui.style import COLORS, FONT_STACK, FONT_CODE, RADII
from src.gui._gui_helpers import _load_icon
from src.tools.terminal_manager import TerminalManager


class TerminalPanel(QWidget):
    """内置终端面板，定时读取后端输出并显示。"""

    sig_closed = Signal()

    def __init__(self, cwd: str = None, parent=None):
        super().__init__(parent)
        self._cwd = cwd
        self._manager = TerminalManager(cwd=cwd)
        self._history: list = []
        self._history_idx = 0
        self._setup_ui()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_output)
        self._timer.start(100)
        self._started = self._manager.start()
        if not self._started:
            self.output.appendPlainText("[终端] 无法启动 shell，请检查系统环境。")
        try:
            from src.gui.style import ThemeManager
            ThemeManager.instance().theme_changed.connect(self._rebuild_styles)
        except Exception:
            pass

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(8)

        # 标题栏
        header = QHBoxLayout()
        header.setSpacing(8)
        self.title_label = QLabel("终端")
        self.title_label.setStyleSheet(
            f"QLabel{{color:{COLORS['TEXT_3']};font-size:12px;font-weight:600;"
            f"font-family:{FONT_STACK};}}"
        )
        header.addWidget(self.title_label)
        header.addStretch(1)
        self.clear_btn = QPushButton("清空")
        self.clear_btn.setFixedSize(44, 24)
        self.clear_btn.setStyleSheet(
            f"QPushButton{{background:{COLORS['SURFACE']};border:1px solid {COLORS['BORDER']};"
            f"border-radius:{RADII['xs']};color:{COLORS['TEXT_3']};font-size:11px;"
            f"font-family:{FONT_STACK};}}"
            f"QPushButton:hover{{background:{COLORS['PRIM_LIGHT']};}}"
        )
        self.clear_btn.clicked.connect(self._clear_output)
        header.addWidget(self.clear_btn)
        self.close_btn = QPushButton()
        self.close_btn.setFixedSize(24, 24)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setIcon(_load_icon("close_16.png", 12))
        self.close_btn.setIconSize(QSize(12, 12))
        self.close_btn.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;border-radius:{RADII['circle']};}}"
            f"QPushButton:hover{{background:{COLORS['ERROR_LIGHT']};}}"
        )
        self.close_btn.clicked.connect(self._on_close)
        header.addWidget(self.close_btn)
        root.addLayout(header)

        # 输出区
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFrameShape(QPlainTextEdit.NoFrame)
        self.output.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.output.setStyleSheet(
            f"QPlainTextEdit{{background:{COLORS['SURFACE']};color:{COLORS['TEXT']};"
            f"border:1px solid {COLORS['BORDER']};border-radius:{RADII['code']};"
            f"padding:10px;font-family:{FONT_CODE};font-size:13px;}}"
        )
        root.addWidget(self.output, 1)

        # 输入行
        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self.prompt = QLabel("$")
        self.prompt.setStyleSheet(
            f"QLabel{{color:{COLORS['PRIM_DARK']};font-size:13px;font-weight:700;"
            f"font-family:{FONT_STACK};padding-left:4px;}}"
        )
        input_row.addWidget(self.prompt)
        self.input = QLineEdit()
        self.input.setPlaceholderText("输入命令并回车…")
        self.input.setStyleSheet(
            f"QLineEdit{{background:{COLORS['CARD']};color:{COLORS['TEXT']};"
            f"border:1px solid {COLORS['BORDER']};border-radius:{RADII['xs']};"
            f"padding:0px 10px;font-family:{FONT_CODE};font-size:13px;}}"
            f"QLineEdit:focus{{border:1px solid {COLORS['PRIM']};}}"
        )
        self.input.setFixedHeight(32)
        self.input.returnPressed.connect(self._on_return)
        self.input.installEventFilter(self)
        input_row.addWidget(self.input, 1)
        root.addLayout(input_row)

    def _poll_output(self):
        if not self.isVisible():
            return
        if not self._manager.is_running():
            return
        text = self._manager.read_output(clear=True)
        if text:
            self.output.moveCursor(QTextCursor.End)
            self.output.insertPlainText(text)
            self.output.moveCursor(QTextCursor.End)

    def showEvent(self, event):
        """面板显示时启动轮询定时器。"""
        super().showEvent(event)
        if not self._timer.isActive():
            self._timer.start(100)

    def hideEvent(self, event):
        """面板隐藏时停止轮询定时器，减少空转 CPU 占用。"""
        super().hideEvent(event)
        self._timer.stop()

    def _on_return(self):
        cmd = self.input.text().strip()
        if not cmd:
            return
        self.output.appendPlainText(f"$ {cmd}")
        self._history.append(cmd)
        self._history_idx = len(self._history)
        self._manager.write(cmd)
        self.input.clear()

    def eventFilter(self, obj, event):
        if obj is self.input and event.type() == event.Type.KeyPress:
            key = event.key()
            if key == Qt.Key_Up and self._history:
                self._history_idx = max(0, self._history_idx - 1)
                self.input.setText(self._history[self._history_idx])
                return True
            if key == Qt.Key_Down and self._history:
                self._history_idx = min(len(self._history), self._history_idx + 1)
                if self._history_idx < len(self._history):
                    self.input.setText(self._history[self._history_idx])
                else:
                    self.input.clear()
                return True
        return super().eventFilter(obj, event)

    def _clear_output(self):
        self.output.clear()

    def _on_close(self):
        self.hide()
        self.sig_closed.emit()

    def closeEvent(self, event):
        """面板关闭时终止 shell 子进程，防止资源泄漏。"""
        self.shutdown()
        super().closeEvent(event)

    def set_cwd(self, cwd: str):
        if cwd and cwd != self._cwd:
            self._cwd = cwd
            self._manager.set_cwd(cwd)
            self.title_label.setText(f"终端 — {cwd}")

    def show_hint(self, text: str):
        """在终端输出区显示一条引导提示（不进入命令历史）。"""
        self.output.appendPlainText(f"[提示] {text}")
        self.output.moveCursor(QTextCursor.End)

    def shutdown(self):
        self._timer.stop()
        # Bug fix #9: 移除 eventFilter，防止 shutdown 后仍接收输入事件
        try:
            self.input.removeEventFilter(self)
        except Exception:
            pass
        self._manager.terminate()

    def _rebuild_styles(self, _theme: str = ""):
        """主题切换后重建终端面板所有子控件样式。"""
        try:
            self.title_label.setStyleSheet(
                f"QLabel{{color:{COLORS['TEXT_3']};font-size:12px;font-weight:600;"
                f"font-family:{FONT_STACK};}}"
            )
            self.clear_btn.setStyleSheet(
                f"QPushButton{{background:{COLORS['SURFACE']};border:1px solid {COLORS['BORDER']};"
                f"border-radius:{RADII['xs']};color:{COLORS['TEXT_3']};font-size:11px;"
                f"font-family:{FONT_STACK};}}"
                f"QPushButton:hover{{background:{COLORS['PRIM_LIGHT']};}}"
            )
            self.close_btn.setStyleSheet(
                f"QPushButton{{background:transparent;border:none;border-radius:{RADII['circle']};}}"
                f"QPushButton:hover{{background:{COLORS['ERROR_LIGHT']};}}"
            )
            self.output.setStyleSheet(
                f"QPlainTextEdit{{background:{COLORS['SURFACE']};color:{COLORS['TEXT']};"
                f"border:1px solid {COLORS['BORDER']};border-radius:{RADII['code']};"
                f"padding:10px;font-family:{FONT_CODE};font-size:13px;}}"
            )
            self.prompt.setStyleSheet(
                f"QLabel{{color:{COLORS['PRIM_DARK']};font-size:13px;font-weight:700;"
                f"font-family:{FONT_STACK};padding-left:4px;}}"
            )
            self.input.setStyleSheet(
                f"QLineEdit{{background:{COLORS['CARD']};color:{COLORS['TEXT']};"
                f"border:1px solid {COLORS['BORDER']};border-radius:{RADII['xs']};"
                f"padding:0px 10px;font-family:{FONT_CODE};font-size:13px;}}"
                f"QLineEdit:focus{{border:1px solid {COLORS['PRIM']};}}"
            )
        except Exception:
            pass
