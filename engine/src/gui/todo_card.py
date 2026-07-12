# -*- coding: utf-8 -*-
"""待办事项卡片（独立组件，供 TabRightPanel 使用）

功能：
  - 展示待办事项列表（带动画）
  - 支持 set_todos / add_todo / clear
  - 完成项灰色显示 + "✓" 前缀，未完成项 "•" 前缀
  - 空态提示
"""

from PySide6.QtCore import Qt, Signal, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QVBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QAbstractItemView, QSizePolicy,
)
from PySide6.QtGui import QColor

from src.gui.style import COLORS, FONT_STACK, FONT_WEIGHTS, RADII, tr, apply_shadow
from src.gui._gui_helpers import _load_icon
from PySide6.QtCore import QSize


class _TodoRow(QFrame):
    """单行待办项（支持滑入/滑出动画）。"""

    def __init__(self, text: str, done: bool = False, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:transparent;")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(8)

        # Checkbox 图标
        self._checkbox_lbl = QLabel()
        self._checkbox_lbl.setFixedSize(16, 16)
        self._checkbox_lbl.setStyleSheet("background:transparent;")
        self._checkbox_lbl.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._checkbox_lbl, 0, Qt.AlignTop)

        # 文字标签
        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setStyleSheet(
            "color:" + COLORS['TEXT_2'] + ";font-size:13px;"
            "font-weight:" + str(FONT_WEIGHTS['regular']) + ";"
            "font-family:" + FONT_STACK + ";line-height:1.5;"
        )
        lay.addWidget(self._label, 1)
        self.set_text(text, done)

    def set_text(self, text: str, done: bool = False):
        self._label.setText(text)
        # Checkbox 图标
        ic = _load_icon("checkbox_checked_18.png" if done else "checkbox_empty_16.png", 16)
        if not ic.isNull():
            self._checkbox_lbl.setPixmap(ic.pixmap(QSize(16, 16)))
        # 文字样式
        text_color = COLORS['TEXT_4'] if done else COLORS['TEXT_2']
        deco = "text-decoration:line-through;" if done else ""
        self._label.setStyleSheet(
            "color:" + text_color + ";font-size:13px;"
            "font-weight:" + str(FONT_WEIGHTS['regular']) + ";"
            "font-family:" + FONT_STACK + ";line-height:1.5;"
            + deco
        )

    def play_add_animation(self):
        """新增滑入+淡入动画"""
        try:
            from src.gui.animations import fade_in
            fade_in(self, duration_ms=180)
        except Exception:
            pass


class TodoCard(QFrame):
    """待办事项卡片组件。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TodoCard")
        self.setStyleSheet(
            "QFrame#TodoCard{background:" + COLORS['CARD'] + ";"
            "border:1px solid " + COLORS['DIVIDER'] + ";"
            "border-radius:" + RADII['xl'] + ";}"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        apply_shadow(self, 'sm')

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        # 标题
        title = QLabel(tr("tab_todos"))
        title.setStyleSheet(
            "color:" + COLORS['TEXT'] + ";font-size:14px;"
            "font-weight:" + str(FONT_WEIGHTS['semibold']) + ";"
            "font-family:" + FONT_STACK + ";"
        )
        root.addWidget(title)

        # 列表
        self.list_widget = QListWidget()
        self.list_widget.setFrameShape(QListWidget.NoFrame)
        self.list_widget.setStyleSheet(
            "QListWidget{background:transparent;border:none;"
            "color:" + COLORS['TEXT_2'] + ";font-size:13px;"
            "font-family:" + FONT_STACK + ";outline:none;}"
            "QListWidget::item{padding:6px 10px;border-radius:" + RADII['sm'] + ";}"
            "QListWidget::item:hover{background:" + COLORS['SURFACE'] + ";}"
        )
        self.list_widget.setSelectionMode(QAbstractItemView.NoSelection)
        self.list_widget.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        root.addWidget(self.list_widget, 1)

        # 空态提示
        self.empty_hint = QLabel(tr("no_todos") + "\n" + tr("no_todos_hint"))
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setStyleSheet(
            "color:" + COLORS['TEXT_4'] + ";font-size:12px;"
            "font-family:" + FONT_STACK + ";padding:24px 12px;"
        )
        root.addWidget(self.empty_hint)

        self._update_empty_state()

    def _update_empty_state(self):
        has_items = self.list_widget.count() > 0
        self.list_widget.setVisible(has_items)
        self.empty_hint.setVisible(not has_items)

    def set_todos(self, todos):
        """批量设置待办列表。todos: [{"text": str, "done": bool}, ...] 或 [str, ...]"""
        self.list_widget.clear()
        if not todos:
            self._update_empty_state()
            return
        for t in todos:
            if isinstance(t, dict):
                text = str(t.get("text", t.get("task", "")))
                done = bool(t.get("done", False))
            else:
                text = str(t)
                done = False
            if not text.strip():
                continue
            self._add_row(text, done, animate=False)
        self._update_empty_state()

    # 兼容别名
    def set_tasks(self, tasks):
        self.set_todos(tasks)

    def add_todo(self, text: str, done: bool = False):
        """添加单个待办事项，带动画。"""
        if not text or not text.strip():
            return
        self._add_row(text, done, animate=True)
        self._update_empty_state()

    # 兼容别名
    def add_task(self, text: str):
        self.add_todo(text)

    def _add_row(self, text: str, done: bool, animate: bool = False):
        row = _TodoRow(text, done)
        item = QListWidgetItem()
        item.setSizeHint(row.sizeHint())
        self.list_widget.addItem(item)
        self.list_widget.setItemWidget(item, row)
        if animate:
            row.play_add_animation()

    def clear(self):
        """清空所有待办。"""
        self.list_widget.clear()
        self._update_empty_state()
