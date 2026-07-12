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
    QFontMetrics,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QFrame, QPushButton, QTextEdit, QScrollArea, QSplitter,
    QGraphicsOpacityEffect, QLineEdit, QSizePolicy, QMessageBox, QDialog,
)
from src.gui.style import COLORS, RADII, SPACING, FONT_STACK, rgba, tr

from src.gui.widgets.buttons import ThreeDotsButton


# ============================================================================
# 会话上下文菜单（三点菜单）
# ============================================================================
class ConversationContextMenuPopup(QFrame):
    """点击三点按钮后弹出的操作菜单：置顶 / 重命名 / 删除。"""

    pin_clicked = Signal(str)
    rename_clicked = Signal(str)
    delete_clicked = Signal(str)

    def __init__(self, project_name, pinned=False, language="zh", parent=None):
        super().__init__(parent)
        self._name = str(project_name)
        self._pinned = bool(pinned)
        self.setWindowFlags(Qt.Popup)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        base = QFrame()
        base.setObjectName("ContextMenuPopupBase")
        base.setStyleSheet(
            "QFrame#ContextMenuPopupBase{"
            "background:" + COLORS['CARD'] + ";"
            "border:1px solid " + COLORS['BORDER'] + ";"
            "border-radius:" + RADII['lg'] + ";"
            "}"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(base)

        v = QVBoxLayout(base)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(2)

        # 使用 tr() 获取 UI 翻译（UI 语言由 I18N.current_language() 控制）
        pin_text = tr("unpin_chat") if self._pinned else tr("pin_chat")
        rename_text = tr("rename")
        delete_text = tr("delete_chat")

        # 置顶 / 取消置顶
        self._pin_label = self._make_row(pin_text)
        self._pin_label.mousePressEvent = lambda e: self._emit(self.pin_clicked)
        v.addWidget(self._pin_label)

        # 分隔线
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("background:" + COLORS['BORDER'] + ";max-height:1px;min-height:1px;")
        v.addWidget(line)

        # 重命名
        rename_label = self._make_row(rename_text)
        rename_label.mousePressEvent = lambda e: self._emit(self.rename_clicked)
        v.addWidget(rename_label)

        # 分隔线
        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        line2.setStyleSheet("background:" + COLORS['BORDER'] + ";max-height:1px;min-height:1px;")
        v.addWidget(line2)

        # 删除（红色）
        delete_label = self._make_row(delete_text, color=COLORS['ERROR'])
        delete_label.mousePressEvent = lambda e: self._emit(self.delete_clicked)
        v.addWidget(delete_label)

    def _make_row(self, text, color=None):
        label = QLabel(text)
        label.setCursor(Qt.PointingHandCursor)
        label.setStyleSheet(
            "QLabel{"
            "color:" + (color or COLORS['TEXT']) + ";"
            "font-size:13px;font-weight:600;font-family:" + FONT_STACK + ";"
            "padding:8px 12px;border-radius:" + RADII['md'] + ";}"
            "QLabel:hover{background:" + COLORS['PRIM'] + ";"
            "color:" + COLORS['TEXT'] + ";}"
        )
        return label

    def _emit(self, signal):
        signal.emit(self._name)
        self.close()



# ============================================================================
# 删除确认对话框 (带 3 秒倒计时红色按钮)
# ============================================================================
class DeleteConfirmDialog(QDialog):
    """居中确认对话框: 确认删除后右下角按钮 3 秒倒计时"""

    def __init__(self, project_name, parent=None):
        super().__init__(parent)
        self._name = str(project_name)
        self._countdown = 3
        self._can_delete = False
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setFixedSize(380, 180)

        content = QFrame()
        content.setObjectName("DeleteConfirmContent")
        content.setStyleSheet(
            "QFrame#DeleteConfirmContent{"
            "background:" + COLORS['CARD'] + ";"
            "border:1px solid " + COLORS['BORDER'] + ";"
            "border-radius:" + RADII['panel'] + ";"
            "}"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(content)

        v = QVBoxLayout(content)
        v.setContentsMargins(24, 16, 24, 16)
        v.setSpacing(12)

        title = QLabel(tr("delete_chat_title"))
        title.setStyleSheet(
            "QLabel{color:" + COLORS['TEXT'] + ";"
            "font-size:18px;font-weight:700;font-family:" + FONT_STACK + ";}"
        )
        v.addWidget(title)

        msg = QLabel(tr("delete_chat_msg"))
        msg.setWordWrap(True)
        msg.setStyleSheet(
            "QLabel{color:" + COLORS['TEXT_3'] + ";"
            "font-size:13px;font-family:" + FONT_STACK + ";line-height:1.6;}"
        )
        v.addWidget(msg)

        name_label = QLabel('"' + self._name + '"')
        name_label.setStyleSheet(
            "QLabel{color:" + COLORS['ERROR'] + ";"
            "font-size:13px;font-weight:600;font-family:" + FONT_STACK + ";}"
        )
        v.addWidget(name_label)

        v.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        cancel_btn = QPushButton(tr("cancel"))
        cancel_btn.setFixedHeight(36)
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setStyleSheet(
            "QPushButton{background:transparent;color:" + COLORS['TEXT'] + ";"
            "border:1px solid " + COLORS['BORDER'] + ";"
            "border-radius:" + RADII['xs'] + ";"
            "padding:0 16px;font-size:13px;font-weight:600;font-family:" + FONT_STACK + ";}"
        )
        cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(cancel_btn)

        self.delete_btn = QPushButton(tr("delete_countdown_fmt", n=3))
        self.delete_btn.setFixedHeight(36)
        self.delete_btn.setCursor(Qt.ForbiddenCursor)
        self.delete_btn.setEnabled(False)
        self.delete_btn.setStyleSheet(
            "QPushButton{background:" + COLORS['ERROR_DARK'] + ";"
            "color:" + COLORS['CARD'] + ";border:none;border-radius:" + RADII['xs'] + ";"
            "padding:0 16px;font-size:13px;font-weight:700;font-family:" + FONT_STACK + ";}"
        )
        self.delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(self.delete_btn)
        v.addLayout(btn_row)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self._center_on_parent()

    def _center_on_parent(self):
        if self.parent() and hasattr(self.parent(), 'geometry'):
            pg = self.parent().geometry()
            cx = pg.center().x() - self.width() // 2
            cy = pg.center().y() - self.height() // 2
            self.move(cx, cy)
        else:
            try:
                screen = QApplication.primaryScreen().geometry()
                cx = screen.center().x() - self.width() // 2
                cy = screen.center().y() - self.height() // 2
                self.move(cx, cy)
            except Exception:
                pass

    def _tick(self):
        self._countdown -= 1
        if self._countdown <= 0:
            self._timer.stop()
            self._can_delete = True
            self.delete_btn.setText(tr("delete"))
            self.delete_btn.setCursor(Qt.PointingHandCursor)
            self.delete_btn.setEnabled(True)
            self.delete_btn.setStyleSheet(
                "QPushButton{background:" + COLORS['ERROR'] + ";"
                "color:" + COLORS['CARD'] + ";border:none;border-radius:" + RADII['xs'] + ";"
                "padding:0 18px;font-size:13px;font-weight:700;font-family:" + FONT_STACK + ";}"
            )
        else:
            self.delete_btn.setText(tr("delete_countdown_fmt", n=self._countdown))

    def _on_cancel(self):
        self._timer.stop()
        self.reject()

    def _on_delete(self):
        if self._can_delete:
            self._timer.stop()
            self.accept()

    def done(self, result):
        """对话框关闭时停止定时器，防止隐藏后仍继续运行。"""
        self._timer.stop()
        super().done(result)



# ============================================================================
# 重命名对话框
# ============================================================================
class RenameDialog(QDialog):
    """输入新对话名称的模态对话框。"""

    def __init__(self, current_name, language="zh", parent=None):
        super().__init__(parent)
        self._language = language if language else "zh"
        self._new_name = None
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setFixedSize(380, 180)

        content = QFrame()
        content.setObjectName("RenameDialogContent")
        content.setStyleSheet(
            "QFrame#RenameDialogContent{"
            "background:" + COLORS['CARD'] + ";"
            "border:1px solid " + COLORS['BORDER'] + ";"
            "border-radius:" + RADII['panel'] + ";"
            "}"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(content)

        v = QVBoxLayout(content)
        v.setContentsMargins(24, 16, 24, 16)
        v.setSpacing(12)

        is_en = self._language.startswith("en")
        title_text = tr("rename_chat")
        placeholder = tr("enter_new_name")
        save_text = tr("save")
        cancel_text = tr("cancel")

        title = QLabel(title_text)
        title.setStyleSheet(
            "QLabel{color:" + COLORS['TEXT'] + ";"
            "font-size:18px;font-weight:700;font-family:" + FONT_STACK + ";}"
        )
        v.addWidget(title)

        self._edit = QLineEdit()
        self._edit.setText(str(current_name))
        self._edit.setPlaceholderText(placeholder)
        self._edit.setFixedHeight(36)
        self._edit.setStyleSheet(
            "QLineEdit{"
            "background:" + COLORS['BGD'] + ";"
            "color:" + COLORS['TEXT'] + ";"
            "border:1px solid " + COLORS['BORDER'] + ";"
            "border-radius:" + RADII['panel'] + ";"
            "padding:0 12px;font-size:13px;font-family:" + FONT_STACK + ";}"
            "QLineEdit:focus{border:1px solid " + COLORS['PRIM_DARK'] + ";}"
        )
        v.addWidget(self._edit)
        v.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        cancel_btn = QPushButton(cancel_text)
        cancel_btn.setFixedHeight(36)
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setStyleSheet(
            "QPushButton{background:transparent;color:" + COLORS['TEXT'] + ";"
            "border:1px solid " + COLORS['BORDER'] + ";"
            "border-radius:" + RADII['xs'] + ";"
            "padding:0 16px;font-size:13px;font-weight:600;font-family:" + FONT_STACK + ";}"
        )
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton(save_text)
        save_btn.setFixedHeight(36)
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setStyleSheet(
            "QPushButton{background:" + COLORS['PRIM_DARK'] + ";"
            "color:" + COLORS['CARD'] + ";border:none;border-radius:" + RADII['xs'] + ";"
            "padding:0 16px;font-size:13px;font-weight:700;font-family:" + FONT_STACK + ";}"
        )
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        v.addLayout(btn_row)

        self._center_on_parent()

    def _center_on_parent(self):
        if self.parent() and hasattr(self.parent(), 'geometry'):
            pg = self.parent().geometry()
            cx = pg.center().x() - self.width() // 2
            cy = pg.center().y() - self.height() // 2
            self.move(cx, cy)
        else:
            try:
                screen = QApplication.primaryScreen().geometry()
                cx = screen.center().x() - self.width() // 2
                cy = screen.center().y() - self.height() // 2
                self.move(cx, cy)
            except Exception:
                pass

    def _on_save(self):
        text = self._edit.text().strip()
        if text:
            self._new_name = text
            self.accept()

    def get_new_name(self):
        return self._new_name



# ============================================================================
# 单个项目行 (项目名 + 3 点按钮)
# ============================================================================
class ProjectRowWidget(QFrame):
    """项目行: 左边项目名(显示名), 右边三点按钮"""

    clicked = Signal(str)
    delete_requested = Signal(str)
    pin_requested = Signal(str)
    rename_requested = Signal(str)

    def __init__(self, name, display_name=None, pinned=False, language="zh", parent=None):
        super().__init__(parent)
        self._name = str(name)
        self._display_name = str(display_name) if display_name else self._name
        self._pinned = bool(pinned)
        self._full_text = ("📌 " if self._pinned else "") + self._display_name
        self._language = language if language else "zh"
        self._selected = False
        # 使用统一的 objectName，避免中文/特殊字符导致 CSS 选择器失效
        self.setObjectName("ProjectRow")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(44)

        self._row_layout = QHBoxLayout(self)
        self._row_layout.setContentsMargins(12, 0, 12, 0)
        self._row_layout.setSpacing(8)

        self._label = QLabel(self._full_text)
        self._label.setWordWrap(False)
        self._label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._label.setStyleSheet(
            "QLabel{color:" + COLORS['TEXT'] + ";"
            "font-size:14px;font-weight:500;font-family:" + FONT_STACK + ";"
            "background:transparent;}"
        )
        # 让文字对鼠标事件透明，确保点击整行都能触发切换
        self._label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._label.setToolTip(self._full_text)
        self._row_layout.addWidget(self._label, 1)

        self._dots = ThreeDotsButton()
        self._dots.clicked.connect(self._on_dots_clicked)
        self._row_layout.addWidget(self._dots)

        self._popup = None
        self._refresh_style()

    def set_selected(self, selected):
        self._selected = selected
        self._refresh_style()

    def update_info(self, display_name=None, pinned=None, selected=None):
        """增量更新行数据，避免整行重建。"""
        if display_name is not None and display_name != self._display_name:
            self._display_name = str(display_name)
            self._full_text = ("📌 " if self._pinned else "") + self._display_name
            self._label.setToolTip(self._full_text)
            self._update_elided_text()
        if pinned is not None and pinned != self._pinned:
            self._pinned = bool(pinned)
            self._full_text = ("📌 " if self._pinned else "") + self._display_name
            self._label.setToolTip(self._full_text)
            self._update_elided_text()
        if selected is not None:
            self.set_selected(selected)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided_text()

    def _update_elided_text(self):
        margins = self._row_layout.contentsMargins()
        spacing = self._row_layout.spacing()
        available = (
            self.width()
            - margins.left()
            - margins.right()
            - spacing
            - self._dots.width()
            - 4  # 安全边距
        )
        available = max(0, available)
        fm = QFontMetrics(self._label.font())
        elided = fm.elidedText(self._full_text, Qt.ElideRight, available)
        self._label.setText(elided)

    def _refresh_style(self):
        # === 选中态：浅蓝背景 + 黑色文字；未选中：透明 + 辅助色文字
        # === hover态：浅蓝悬停色；圆角使用 md (12px)，与卡片圆角 token 保持一致
        selected_bg = COLORS['PRIM']
        selected_fg = COLORS['TEXT']
        hover_bg = COLORS['PRIM_LIGHT']
        pressed_bg = COLORS['PRIM_DARK']
        # 选中态使用 600 字重，避免 700 导致布局抖动
        weight = "600" if self._selected else "500"
        bg = selected_bg if self._selected else "transparent"
        fg = selected_fg if self._selected else COLORS['TEXT_3']
        # 使用类选择器，避免 objectName 中的特殊字符破坏样式表
        self.setStyleSheet(
            "QFrame{"
            "background:" + bg + ";"
            "border:none;border-radius:" + RADII['md'] + ";}"
            "QFrame:hover{"
            "background:" + (selected_bg if self._selected else hover_bg) + ";"
            "}"
            "QFrame:pressed{"
            "background:" + pressed_bg + ";"
            "}"
        )
        self._label.setStyleSheet(
            "QLabel{color:" + fg + ";"
            "font-size:14px;font-weight:" + weight + ";font-family:" + FONT_STACK + ";}"
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._name)
        super().mousePressEvent(event)

    def _on_dots_clicked(self):
        if self._popup is None or not self._popup.isVisible():
            self._popup = ConversationContextMenuPopup(
                self._name, pinned=self._pinned, language=self._language, parent=self)
            self._popup.pin_clicked.connect(self._on_pin)
            self._popup.rename_clicked.connect(self._on_rename)
            self._popup.delete_clicked.connect(self.delete_requested.emit)
            pos = self._dots.mapToGlobal(QPoint(self._dots.width() + 4, -4))
            popup_w = 180
            self._popup.setFixedWidth(popup_w)
            try:
                screen_right = QApplication.primaryScreen().geometry().right()
            except Exception:
                screen_right = pos.x() + 400
            if pos.x() + popup_w > screen_right:
                pos = self._dots.mapToGlobal(QPoint(-popup_w + self._dots.width() - 4, -4))
            self._popup.move(pos.x(), pos.y())
            self._popup.show()

    def _on_pin(self, name):
        self.pin_requested.emit(name)

    def _on_rename(self, name):
        self.rename_requested.emit(name)

