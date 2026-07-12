# -*- coding: utf-8 -*-
"""全局快捷键系统 + @提及补全

(1) GlobalShortcut: 把 Ctrl/Cmd + <Key> 映射到动作。
    - Ctrl/Cmd + L  — 聚焦聊天输入框
    - Ctrl/Cmd + K  — 打开符号搜索 (SymbolPalette)
    - Ctrl/Cmd + N  — 新对话 / 新项目
    - Ctrl/Cmd + S  — 保存当前文件
    - Ctrl/Cmd + Shift+L — 清空对话
    - Ctrl/Cmd + /  — 设置

(2) MentionCompleter: 在输入框中输入 "@" 时弹补全列表，
    支持按项目符号 / 文件名 / 最近打开文件三种模式。
    这让用户可以快速引用项目中的符号或文件，AI 能拿到更准确的上下文。

依赖:
    - src/gui/symbol_palette (SymbolPalette) — 若存在则绑定
    - src/gui/editor/tab_manager — 若存在则用于文件跳转
    - src/analysis/symbol_indexer (SymbolIndex) — 符号查询
"""

from __future__ import annotations

import os
from typing import Optional, Callable, List, Dict, Any

from PySide6.QtCore import Qt, Signal, QTimer, QEvent, QObject
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QLineEdit, QTextEdit, QPlainTextEdit,
)

from src.gui.style import COLORS, FONT_STACK, RADII


# ---------------------------------------------------------------------------
# GlobalShortcut — 全局快捷键注册表
# ---------------------------------------------------------------------------

class GlobalShortcut:
    """一个轻量的全局快捷键注册表。

    典型用法:
        gs = GlobalShortcut(main_window)
        gs.on("Ctrl+L", lambda: input_edit.setFocus())
        gs.on("Ctrl+K", lambda: symbol_palette.show_at_center())
        gs.on("Ctrl+N", new_project_slot)

    注意: QShortcut 默认只在父 widget 获得焦点时生效。
    若想实现真正的"全局"（即使焦点在子编辑区也能拦截），
    必须把 shortcut 的 parent 设置为主窗口。
    """

    def __init__(self, main_window=None):
        self._window = main_window
        self._shortcuts: List[QShortcut] = []
        self._actions: Dict[str, Callable] = {}

    def on(self, key_sequence: str, callback: Callable[[], None]) -> QShortcut:
        """注册一个快捷键。callback 在主窗口获得焦点或子编辑区时都会触发。"""
        parent = self._window
        sc = QShortcut(QKeySequence(key_sequence), parent)
        sc.setContext(Qt.WindowShortcut)
        sc.activated.connect(callback)
        self._shortcuts.append(sc)
        self._actions[key_sequence] = callback
        return sc

    def on_with(self, key_sequence: str, obj: Any, callback: Callable[[Any], None]) -> QShortcut:
        """注册并把 obj 作为参数传给 callback。"""
        return self.on(key_sequence, lambda o=obj: callback(o))

    def clear(self) -> None:
        for sc in self._shortcuts:
            sc.setEnabled(False)
        self._shortcuts = []
        self._actions = {}

    def list_shortcuts(self) -> List[str]:
        return list(self._actions.keys())


# ---------------------------------------------------------------------------
# MentionPopup — @提及补全浮窗
# ---------------------------------------------------------------------------

class MentionPopup(QFrame):
    """在光标位置显示的补全浮窗。用户按 ↑↓ 选择，Enter 插入。"""

    item_selected = Signal(str)  # 插入的完整文本，例如 "@parse_file"
    cancelled = Signal()  # 用户按 Esc 取消

    def __init__(self, items: List[tuple], parent=None):
        """items: list of (display_text, insert_text)"""
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setStyleSheet(
            f"QFrame{{background:{COLORS['CARD']};"
            f"border:1px solid {COLORS['BORDER']};"
            f"border-radius:{RADII['md']};}}"
        )
        self.setMinimumWidth(320)
        self.setMaximumHeight(280)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(2)

        hint = QLabel("↑↓ 选择 · Enter 插入 · Esc 取消")
        hint.setStyleSheet(
            f"color:{COLORS['TEXT_3']};"
            f"font-size:11px;font-family:{FONT_STACK};padding:4px 8px;"
        )
        lay.addWidget(hint)

        self._list = QListWidget()
        self._list.setFrameShape(QFrame.NoFrame)
        self._list.setFocusPolicy(Qt.NoFocus)
        self._list.setStyleSheet(
            f"QListWidget{{background:transparent;border:none;outline:0px;"
            f"font-family:{FONT_STACK};}}"
            f"QListWidget::item{{height:34px;padding:4px 12px;border-radius:8px;"
            f"color:{COLORS['TEXT']};font-size:13px;}}"
            f"QListWidget::item:selected{{background:{COLORS['PRIM_LIGHT']};}}"
            f"QListWidget::item:hover{{background:{COLORS['CARD_HOVER']};}}"
        )
        for display, insert in items:
            item = QListWidgetItem(display)
            item.setData(Qt.UserRole, insert)
            self._list.addItem(item)
        if items:
            self._list.setCurrentRow(0)
        lay.addWidget(self._list, 1)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key_Escape:
            self.cancelled.emit()
            self.close()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            item = self._list.currentItem()
            if item is not None:
                self.item_selected.emit(item.data(Qt.UserRole))
                self.close()
            return
        if key == Qt.Key_Up:
            self._list.setCurrentRow(max(0, self._list.currentRow() - 1))
            return
        if key == Qt.Key_Down:
            self._list.setCurrentRow(min(self._list.count() - 1, self._list.currentRow() + 1))
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        item = self._list.itemAt(self._list.mapFromGlobal(event.globalPosition().toPoint()))
        if item is not None:
            self.item_selected.emit(item.data(Qt.UserRole))
            self.close()
        else:
            super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# MentionCompleter — 管理 @提及自动补全
# ---------------------------------------------------------------------------

class MentionCompleter(QObject):
    """把一个输入控件（QLineEdit / QTextEdit / QPlainTextEdit）接入 @提及补全。

    在用户输入 "@" 时弹出 MentionPopup，按用户输入过滤候选，选中后插入文本。

    提供两种候选源：
      1) symbol_index — 项目符号 (SymbolIndex) — 优先级最高
      2) project_root + tab_manager — 项目内文件名、最近打开文件

    典型用法:
        mc = MentionCompleter(chat_input_edit)
        mc.bind_index(symbol_indexer_instance)     # 可选
        mc.bind_tab_manager(tab_manager)             # 可选
        mc.bind_project_root("/path/to/project")     # 可选
    """

    def __init__(self, text_widget, parent=None):
        super().__init__(parent or text_widget)
        self._widget = text_widget
        self._indexer = None
        self._tab_manager = None
        self._project_root: Optional[str] = None
        self._popup: Optional[MentionPopup] = None

        # 安装事件过滤器（捕获按键）
        text_widget.installEventFilter(self)

    # ------------------------------------------------------------------
    # 绑定数据源
    # ------------------------------------------------------------------

    def bind_index(self, indexer) -> "MentionCompleter":
        self._indexer = indexer
        return self

    def bind_tab_manager(self, tab_manager) -> "MentionCompleter":
        self._tab_manager = tab_manager
        return self

    def bind_project_root(self, root: str) -> "MentionCompleter":
        self._project_root = root
        return self

    # ------------------------------------------------------------------
    # 事件过滤
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.KeyPress:
            if self._popup is not None:
                # 让 popup 处理导航键
                self._popup.keyPressEvent(event)
                return True
            if event.text() == "@":
                # 在下次事件循环处理后弹浮窗（等 @ 已经写入 widget）
                QTimer.singleShot(10, self._open_popup)
        elif event.type() == QEvent.FocusOut:
            if self._popup is not None:
                self._popup.close()
                self._popup = None
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # 打开浮窗
    # ------------------------------------------------------------------

    def _open_popup(self) -> None:
        # 若已有 popup 存在，先关闭并销毁，避免重复输入 "@" 产生孤儿浮窗
        if self._popup is not None:
            try:
                self._popup.deleteLater()
            except RuntimeError:
                pass
            self._popup = None

        # 读取光标前后的内容，判断是不是 "@" 后跟空格
        text_before = self._widget_text_before_cursor()
        if not text_before or not text_before.rstrip(" ").endswith("@"):
            return

        # 构造候选
        items: List[tuple] = []

        # 1) 符号 (Top 20)
        if self._indexer is not None:
            syms = self._indexer.search("", limit=20)
            for sym in syms:
                kind_icon = {"function": "ƒ", "class": "◉", "method": "m",
                            "constant": "C", "variable": "v", "key": "k",
                            "import": "↩"}.get(getattr(sym, "kind", ""), "?")
                display = f"{kind_icon} {sym.name:<28} ({os.path.basename(sym.file_path)}:{sym.line})"
                insert = f"@{sym.name}"
                items.append((display, insert))

        # 2) 最近打开文件
        if self._tab_manager is not None and hasattr(self._tab_manager, "get_open_files"):
            try:
                files = self._tab_manager.get_open_files() or []
                for fp in files[:10]:
                    base = os.path.basename(fp)
                    items.append((f"📄 {base:<30} ({fp})", f"@file:{fp}"))
            except Exception:
                pass

        # 3) 项目文件 (project_root 下的前 10 个 py/js 文件)
        if self._project_root and os.path.isdir(self._project_root):
            try:
                added = 0
                for root, dirs, fnames in os.walk(self._project_root):
                    dirs[:] = [d for d in dirs if not d.startswith(".")
                               and d not in ("node_modules", "__pycache__")]
                    for fname in fnames:
                        if fname.startswith(".") or not fname.lower().endswith((".py", ".js", ".ts", ".json", ".md", ".css", ".html")):
                            continue
                        if added >= 10:
                            break
                        full = os.path.join(root, fname)
                        items.append((f"📁 {fname:<30} ({full})", f"@file:{full}"))
                        added += 1
                    if added >= 10:
                        break
            except Exception:
                pass

        if not items:
            items = [("(无可用的符号或文件 — 先让项目跑起来)", " ")]

        popup = MentionPopup(items, parent=self._widget)
        popup.item_selected.connect(self._on_item_selected)
        popup.cancelled.connect(self._on_popup_cancelled)
        self._popup = popup

        # 定位到光标
        cursor_pos = self._cursor_global_pos()
        if cursor_pos is not None:
            popup.move(cursor_pos.x(), cursor_pos.y() + 4)
        popup.show()

    def _on_item_selected(self, text: str) -> None:
        """用户选中一项 → 把 widget 中最后一个 "@xxx" 替换为插入文本。"""
        self._popup = None
        if not text or text.isspace():
            return
        w = self._widget
        # 不同控件的处理方式
        if isinstance(w, QLineEdit):
            cur = w.cursorPosition()
            txt = w.text()
            # 找到光标前最后一个 "@"
            last_at = txt[:cur].rfind("@")
            if last_at < 0:
                return
            new = txt[:last_at] + text + txt[cur:]
            w.setText(new)
            w.setCursorPosition(last_at + len(text))
        elif isinstance(w, (QTextEdit, QPlainTextEdit)):
            cursor = w.textCursor()
            pos = cursor.position()
            content = w.toPlainText() if isinstance(w, QPlainTextEdit) else w.toPlainText()
            # 找到 pos 前最后一个 "@"
            last_at = content[:pos].rfind("@")
            if last_at < 0:
                return
            # 删除 "@" 和光标之间的字符，再插入选中文本
            cursor.setPosition(last_at)
            cursor.setPosition(pos, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
            cursor.insertText(text)
            w.setTextCursor(cursor)
        else:
            # fallback
            pass

    def _on_popup_cancelled(self) -> None:
        """用户按 Esc 取消 → 清理 popup 引用，避免僵尸状态阻断输入"""
        self._popup = None

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _widget_text_before_cursor(self) -> str:
        w = self._widget
        if isinstance(w, QLineEdit):
            return w.text()[: w.cursorPosition()]
        if isinstance(w, (QTextEdit, QPlainTextEdit)):
            tc = w.textCursor()
            pos = tc.position()
            full = w.toPlainText()
            return full[:pos]
        return ""

    def _cursor_global_pos(self):
        w = self._widget
        try:
            if isinstance(w, QLineEdit):
                # 粗略处理 — 在 widget 底部显示
                return w.mapToGlobal(w.rect().bottomLeft())
            if isinstance(w, (QTextEdit, QPlainTextEdit)):
                rect = w.cursorRect()
                return w.mapToGlobal(rect.bottomLeft())
        except Exception:
            pass
        return w.mapToGlobal(w.rect().topLeft()) if hasattr(w, "mapToGlobal") else None


# ---------------------------------------------------------------------------
# 便捷入口 — 给主窗口一键注册默认快捷键
# ---------------------------------------------------------------------------

def register_default_shortcuts(
    main_window,
    *,
    focus_input=None,
    show_symbol_palette=None,
    new_project=None,
    save_current=None,
    clear_chat=None,
    open_settings=None,
) -> GlobalShortcut:
    """为常见场景注册一组默认快捷键。"""
    gs = GlobalShortcut(main_window)
    if focus_input is not None:
        gs.on("Ctrl+L", focus_input)
    if show_symbol_palette is not None:
        gs.on("Ctrl+K", show_symbol_palette)
    if new_project is not None:
        gs.on("Ctrl+N", new_project)
    if save_current is not None:
        gs.on("Ctrl+S", save_current)
    if clear_chat is not None:
        gs.on("Ctrl+Shift+L", clear_chat)
    if open_settings is not None:
        gs.on("Ctrl+,", open_settings)
    return gs
