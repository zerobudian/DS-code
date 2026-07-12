# -*- coding: utf-8 -*-
"""InputArea 子模块：文本输入处理。

负责：
  - 键盘事件过滤 (eventFilter)：Ctrl+V 粘贴、Enter/Ctrl+Enter 发送、
    Shift+Enter 换行、Up/Down 历史导航、Slash 弹窗键盘控制
  - 输入历史 (最近 50 条) 与草稿保护
  - Slash 命令弹窗的显示 / 过滤 / 选择
  - 公共 API: focus_input / get_mentioned_files / set_placeholder

注意：输入框控件创建（QTextEdit / slash_btn）已迁移至
InputArea._build_text_input（input_area/__init__.py）；focus glow
(_on_focus_in/_on_focus_out) 与发送 (_on_send) 亦由 InputArea 覆盖，
本 Mixin 仅保留事件过滤、历史导航、Slash 弹窗等行为方法。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication

from src.gui._gui_helpers import _load_icon
from src.gui.slash_popup import SlashPopup


class TextHandlerMixin:
    """文本输入：键盘事件、历史、Slash 弹窗、focus glow。"""

    # ------------------------------------------------------------------
    # 控件创建已迁移至 InputArea._build_text_input
    # （见 input_area/__init__.py），原 _init_text_handler() 已移除。
    # ------------------------------------------------------------------

    # ==================================================================
    # 事件过滤：键盘 / 焦点
    # ==================================================================
    def _filter_input_event(self, obj, event):
        if obj is self.input:
            et = event.type()
            if et == QEvent.KeyPress:
                # 如果斜杠弹窗显示，优先处理其键盘事件
                if self._slash_popup is not None and self._slash_popup.isVisible():
                    key = event.key()
                    if key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_Enter, Qt.Key_Return, Qt.Key_Escape):
                        if key == Qt.Key_Up:
                            self._slash_popup.navigate(-1)
                            return True
                        elif key == Qt.Key_Down:
                            self._slash_popup.navigate(1)
                            return True
                        elif key in (Qt.Key_Enter, Qt.Key_Return):
                            self._slash_popup.confirm_current()
                            return True
                        elif key == Qt.Key_Escape:
                            self._slash_popup.hide_popup()
                            return True
                # Ctrl+V 粘贴文件/图片
                if event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_V:
                    if self._try_paste_from_clipboard():
                        return True
                # Ctrl+Enter / Enter 发送；Shift+Enter 插入换行
                if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                    if event.modifiers() & Qt.ShiftModifier:
                        return False
                    if event.modifiers() & Qt.ControlModifier or not event.modifiers():
                        _txt = self.input.toPlainText().strip()
                        _has_att = bool(self._uploaded_images or self._uploaded_files)
                        if _txt or _has_att:
                            self._on_send()
                        return True
                # 上下箭头浏览输入历史
                if event.key() == Qt.Key_Up and self._should_navigate_history(Qt.Key_Up):
                    self._history_navigate(-1)
                    return True
                if event.key() == Qt.Key_Down and self._should_navigate_history(Qt.Key_Down):
                    self._history_navigate(1)
                    return True
            elif et == QEvent.FocusIn:
                self._on_focus_in()
            elif et == QEvent.FocusOut:
                self._on_focus_out()
        return False

    # ==================================================================
    # focus glow — 保持浅蓝底，聚焦时略加深边框
    # _on_focus_in() / _on_focus_out() 已由 InputArea 覆盖
    # （见 input_area/__init__.py，使用 RADII['2xl'] 替代硬编码半径）。
    # ==================================================================

    # ==================================================================
    # 文本变更回调
    # ==================================================================
    def _on_text_changed(self):
        # 委托给统一的 _update_send_icons 方法，避免三处重复逻辑
        self._update_send_icons()
        # 注意：不在打字时隐藏 StatsCard，改为第一条消息发送后隐藏
        self._update_slash_popup_for_text()

    # ==================================================================
    # 发送
    # _on_send() 已由 InputArea._on_send() 覆盖
    # （见 input_area/__init__.py，含发送后清空附件逻辑）。
    # ==================================================================

    # ==================================================================
    # 输入历史
    # ==================================================================
    def _history_append(self, text: str) -> None:
        try:
            self._history.append(str(text))
            if len(self._history) > self._max_history:
                self._history.pop(0)
        except Exception:
            pass

    def _should_navigate_history(self, key: int) -> bool:
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ShiftModifier:
            return False
        cursor = self.input.textCursor()
        doc = self.input.document()
        block_count = doc.blockCount() if doc else 1
        if block_count <= 1:
            return True
        if key == Qt.Key_Up:
            return cursor.atStart()
        if key == Qt.Key_Down:
            return cursor.atEnd()
        return False

    def _history_navigate(self, direction: int) -> None:
        if not self._history:
            return
        if direction == -1:
            if self._history_index == -1:
                self._draft_text = self.input.toPlainText()
                self._history_index = len(self._history) - 1
            else:
                self._history_index = max(0, self._history_index - 1)
        else:
            if self._history_index == -1:
                return
            self._history_index += 1
            if self._history_index >= len(self._history):
                self._history_index = -1

        if self._history_index == -1:
            new_text = self._draft_text
        else:
            new_text = self._history[self._history_index]
        self.input.setPlainText(new_text)
        try:
            cursor = self.input.textCursor()
            cursor.movePosition(QTextCursor.End)
            self.input.setTextCursor(cursor)
        except Exception:
            pass

    # ==================================================================
    # 公共 API
    # ==================================================================
    def focus_input(self) -> None:
        try:
            if not self.isVisible() or not self.input.isVisible():
                return
            self.input.setFocus()
        except Exception:
            pass

    def set_placeholder(self, text: str):
        try:
            self.input.setPlaceholderText(text or "")
        except Exception:
            pass

    def get_mentioned_files(self) -> list:
        text = self.input.toPlainText()
        results = []
        import re as _re
        for m in _re.finditer(r'@([\w\-./\\][\w\-./\\\s]*?)(?:\s|$|,|\.|;|:)', text):
            path = m.group(1).strip()
            if path:
                results.append((path, path))
        lines = text.split("\n")
        for line in lines:
            for token in line.split(" "):
                if token.startswith("@") and len(token) > 1:
                    path = token[1:].strip()
                    if path and (path, path) not in results:
                        results.append((path, path))
        return results

    # ==================================================================
    # 斜杠命令弹窗
    # ==================================================================
    def _on_slash_btn_clicked(self, _checked=False):
        self.input.setFocus()
        cursor = self.input.textCursor()
        cursor.select(cursor.Document)
        cursor.insertText("/")
        self._ensure_slash_popup()
        if self._slash_popup:
            self._slash_popup.set_filter_text("")
            self._slash_popup.show_near(self.input)

    def _ensure_slash_popup(self):
        if self._slash_popup is None:
            self._slash_popup = SlashPopup()
            self._slash_popup.item_selected.connect(self._on_slash_command_selected)
            self._slash_popup.installEventFilter(self)

    def _on_slash_command_selected(self, command: str):
        full_text = self.input.toPlainText().strip()
        if full_text.startswith("/"):
            self.input.clear()
        self.sig_send.emit("/" + command)
        if self._slash_popup:
            self._slash_popup.hide_popup()

    def _update_slash_popup_for_text(self):
        text = self.input.toPlainText()
        if text.startswith("/"):
            self._ensure_slash_popup()
            if self._slash_popup:
                rest = text[1:]
                self._slash_popup.set_filter_text(rest)
                if not self._slash_popup.isVisible():
                    self._slash_popup.show_near(self.input)
        else:
            if self._slash_popup and self._slash_popup.isVisible():
                self._slash_popup.hide_popup()
