# -*- coding: utf-8 -*-
"""符号搜索面板 (SymbolPalette)

IDE 风格的快速符号导航：
  - 快捷键 Ctrl+P 或 Cmd+P 弹出
  - 输入关键字实时模糊搜索
  - ↑/↓ 选择，Enter 跳转，Esc 关闭
  - 显示符号名、类型、文件名、行号

依赖: src/analysis/symbol_indexer.py (SymbolIndex)
      src/gui/editor/tab_manager.py (EditorTabManager)
"""

from __future__ import annotations

import os
from typing import List, Optional

from PySide6.QtCore import Qt, Signal, QTimer, QEasingCurve
from PySide6.QtGui import QKeyEvent, QFont
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem,
)

from src.gui.style import COLORS, FONT_STACK, RADII
from src.gui.animations import pop_in, pop_out, fade_in, fade_out, HoverCard

try:
    from src.analysis.symbol_indexer import SymbolIndex, Symbol
except Exception:
    SymbolIndex = None  # type: ignore
    Symbol = None       # type: ignore


# ---------------------------------------------------------------------------
# 搜索输入框
# ---------------------------------------------------------------------------

class _SearchEdit(QLineEdit):
    """搜索输入框 — 支持上下键移动列表焦点。"""

    navigate_up = Signal()
    navigate_down = Signal()
    navigate_enter = Signal()
    close_requested = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key_Up,):
            self.navigate_up.emit()
            return
        if key in (Qt.Key_Down,):
            self.navigate_down.emit()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.navigate_enter.emit()
            return
        if key in (Qt.Key_Escape,):
            self.close_requested.emit()
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# 列表项 widget：带 hover 背景色动画与选中高亮
# ---------------------------------------------------------------------------

class _SymbolListItem(HoverCard):
    """符号结果行：图标 + 名称 + 路径/行号，hover/selected 使用 QPropertyAnimation。"""

    def __init__(self, sym, parent=None):
        super().__init__(
            normal_bg="transparent",
            hover_bg=COLORS['CARD_HOVER'],
            selected_bg=COLORS['PRIM_LIGHT'],
            border_color="transparent",
            border_radius=RADII['xs'],
            parent=parent,
        )
        self.setObjectName("SymbolListItem")
        self.sym = sym
        self.setFixedHeight(36)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(10)

        self.icon_label = QLabel(self._kind_icon(sym))
        self.icon_label.setFixedSize(18, 18)
        self.icon_label.setAlignment(Qt.AlignCenter)
        icon_font = QFont()
        icon_font.setFamilies([FONT_STACK])
        icon_font.setPointSize(10)
        icon_font.setWeight(QFont.Bold)
        self.icon_label.setFont(icon_font)
        self.icon_label.setStyleSheet(f"color:{COLORS['PRIM_DARK']};background:transparent;")
        lay.addWidget(self.icon_label)

        self.name_label = QLabel(str(sym.name))
        name_font = QFont()
        name_font.setFamilies([FONT_STACK])
        name_font.setPointSize(10)
        name_font.setWeight(QFont.DemiBold)
        self.name_label.setFont(name_font)
        self.name_label.setStyleSheet(f"color:{COLORS['TEXT']};background:transparent;")
        lay.addWidget(self.name_label, stretch=1)

        short_path = ""
        line = ""
        if hasattr(sym, "file_path"):
            short_path = os.path.basename(sym.file_path)
        if hasattr(sym, "line"):
            line = str(sym.line)
        meta = f"{short_path}:{line}" if short_path else ""
        self.meta_label = QLabel(meta)
        meta_font = QFont()
        meta_font.setFamilies([FONT_STACK])
        meta_font.setPointSize(9)
        self.meta_label.setFont(meta_font)
        self.meta_label.setStyleSheet(f"color:{COLORS['TEXT_3']};background:transparent;")
        lay.addWidget(self.meta_label)

    @staticmethod
    def _kind_icon(sym):
        kind = getattr(sym, "kind", "")
        return {
            "function": "ƒ",
            "method": "ƒ",
            "class": "◉",
            "constant": "C",
            "variable": "v",
            "import": "↩",
            "key": "🔑",
        }.get(kind, "?")


# ---------------------------------------------------------------------------
# 主面板
# ---------------------------------------------------------------------------

class SymbolPalette(QFrame):
    """IDE 风格的快速符号导航面板。

    用法:
        palette = SymbolPalette(parent=main_window)
        palette.bind(indexer, tab_manager)
        palette.show_at_center()       # 弹出来
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._indexer: Optional[SymbolIndex] = None
        self._tab_mgr = None
        self._current_results: List[Symbol] = []
        self._item_widgets: List[_SymbolListItem] = []
        self._is_closing = False
        self._items_animated = False

        # 无边框 / 顶层弹出 / 模态
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose, False)

        self.setFixedWidth(560)
        self.setMaximumHeight(480)

        self._build_ui()
        self._list.itemSelectionChanged.connect(self._update_selection_styles)

        # 输入变化后延迟搜索 (节流)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(80)
        self._search_timer.timeout.connect(self._do_search)

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # 外层带阴影的容器
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 内容卡片
        card = QFrame()
        card.setObjectName("SymbolPaletteCard")
        card.setStyleSheet(
            "QFrame#SymbolPaletteCard {"
            f"background:{COLORS['CARD']};"
            f"border:1px solid {COLORS['BORDER']};"
            f"border-radius:{RADII['lg']};"
            "}"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)

        # 顶部提示
        header = QLabel("搜索符号 — 函数 / 类 / 常量 (模糊匹配)")
        header.setStyleSheet(
            f"color:{COLORS['TEXT_3']};"
            f"font-size:12px;font-family:{FONT_STACK};"
        )
        lay.addWidget(header)

        # 输入框
        self._edit = _SearchEdit()
        self._edit.setPlaceholderText(
            "输入名称… 例如: 'Symbol', 'chat', 'fn', 'cc' 模糊匹配 CamelCase"
        )
        self._edit.setFixedHeight(40)
        self._edit.setStyleSheet(
            f"QLineEdit{{background:{COLORS['CARD']};"
            f"color:{COLORS['TEXT']};"
            f"border:1px solid {COLORS['BORDER']};"
            f"border-radius:{RADII['md']};"
            f"padding:0 14px;font-size:15px;font-family:{FONT_STACK};"
            f"}}"
            f"QLineEdit:focus{{border:1px solid {COLORS['PRIM_DARK']};}}"
            f"QLineEdit:hover{{border:1px solid {COLORS['PRIM_LIGHT']};}}"
        )
        self._edit.textChanged.connect(lambda _: self._search_timer.start())
        self._edit.navigate_up.connect(lambda: self._move_selection(-1))
        self._edit.navigate_down.connect(lambda: self._move_selection(1))
        self._edit.navigate_enter.connect(self._activate_current)
        self._edit.close_requested.connect(self.hide_palette)
        lay.addWidget(self._edit)

        # 结果列表
        self._list = QListWidget()
        self._list.setFrameShape(QFrame.NoFrame)
        self._list.setFocusPolicy(Qt.NoFocus)
        self._list.setSpacing(1)
        self._list.setStyleSheet(
            f"QListWidget{{background:transparent;border:none;outline:0px;"
            f"font-family:{FONT_STACK};}}"
            f"QListWidget::item{{height:36px;padding:4px 10px;border-radius:{RADII['xs']};"
            f"color:{COLORS['TEXT']};font-size:14px;}}"
            f"QListWidget::item:selected{{background:{COLORS['PRIM_LIGHT']};"
            f"color:{COLORS['TEXT']};}}"
            f"QListWidget::item:hover{{background:{COLORS['CARD_HOVER']};}}"
        )
        self._list.itemDoubleClicked.connect(lambda _: self._activate_current())
        lay.addWidget(self._list, 1)

        # 底部状态栏
        self._status = QLabel("")
        self._status.setStyleSheet(
            f"color:{COLORS['TEXT_3']};"
            f"font-size:11px;font-family:{FONT_STACK};"
        )
        lay.addWidget(self._status)

        outer.addWidget(card)

    # ------------------------------------------------------------------
    # 外部绑定
    # ------------------------------------------------------------------

    def bind(self, indexer, tab_manager=None) -> None:
        self._indexer = indexer
        self._tab_mgr = tab_manager

    # ------------------------------------------------------------------
    # 弹出 / 隐藏
    # ------------------------------------------------------------------

    def show_at_center(self, parent_widget=None) -> None:
        # 重置关闭标志，防止上次关闭动画被中断后 _is_closing 永久为 True
        self._is_closing = False
        target = parent_widget or self.parent()
        if target is not None and hasattr(target, "geometry"):
            geom = target.geometry()
            x = geom.center().x() - self.width() // 2
            y = geom.top() + 80
            self.move(x, y)
        self._edit.clear()
        self._list.clear()
        self._item_widgets.clear()
        self._items_animated = False
        self._status.setText("")
        if self._indexer is not None:
            self._status.setText(self._indexer.stats())
        self.show()
        # pop_in (scale) + fade_in
        try:
            pop_in(self, duration_ms=200, start_scale=0.92, easing=QEasingCurve.OutBack)
            fade_in(self, duration_ms=180, easing=QEasingCurve.OutCubic)
        except Exception:
            pass
        self._edit.setFocus(Qt.OtherFocusReason)
        QTimer.singleShot(20, self._do_search)

    def hide_palette(self) -> None:
        if self._is_closing or not self.isVisible():
            return
        self._is_closing = True
        remaining = [2]

        def _finish() -> None:
            remaining[0] -= 1
            if remaining[0] <= 0:
                self.hide()
                self._is_closing = False

        try:
            pop_out(self, duration_ms=180, on_finished=_finish)
            fade_out(self, duration_ms=150, on_finished=_finish)
        except Exception:
            _finish()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)

    # ------------------------------------------------------------------
    # 搜索逻辑
    # ------------------------------------------------------------------

    def _do_search(self) -> None:
        if self._indexer is None:
            self._list.clear()
            self._status.setText("未绑定索引器")
            return

        query = self._edit.text().strip()
        if not query:
            # 空查询: 显示最近文件的符号 (前 20 个)
            self._current_results = []
            for fp in list(self._indexer.list_all_files())[:3]:
                self._current_results.extend(self._indexer.list_file_symbols(fp)[:10])
            self._current_results = self._current_results[:25]
        else:
            self._current_results = self._indexer.search(query, limit=30)

        self._render_results()

    def _render_results(self) -> None:
        # 先销毁旧的 item widgets，避免内存泄漏（QListWidget.clear 不删除 setItemWidget 的 widget）
        for w in self._item_widgets:
            try:
                w.deleteLater()
            except RuntimeError:
                pass
        self._list.clear()
        self._item_widgets.clear()
        for sym in self._current_results:
            widget = _SymbolListItem(sym, self._list)
            widget.set_selected(False)
            item = QListWidgetItem()
            item.setData(Qt.UserRole, sym)
            item.setSizeHint(widget.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, widget)
            self._item_widgets.append(widget)

        if self._current_results:
            # 默认选中第一项
            self._list.setCurrentRow(0)
            self._update_selection_styles()

        if self._edit.text().strip():
            self._status.setText(
                f"找到 {len(self._current_results)} 个匹配 · "
                f"{self._indexer.total_symbols()} 个符号 / {self._indexer.total_files()} 个文件"
            )
        else:
            self._status.setText(
                f"最近 {len(self._current_results)} 个符号 · "
                f"{self._indexer.total_symbols()} 个符号 / {self._indexer.total_files()} 个文件"
            )

        # 列表项 staggered 入场
        if self.isVisible() and not self._items_animated:
            self._items_animated = True
            for i, widget in enumerate(self._item_widgets):
                QTimer.singleShot(i * 25, lambda w=widget: self._animate_single_item(w))

    def _animate_single_item(self, widget: _SymbolListItem) -> None:
        if widget is None or not widget.isVisible():
            return
        try:
            from src.gui.animations import slide_in
            slide_in(widget, direction="up", duration_ms=180, distance_px=14, easing=QEasingCurve.OutBack)
            fade_in(widget, duration_ms=150, easing=QEasingCurve.OutCubic)
        except Exception:
            pass

    def _update_selection_styles(self) -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            widget = self._list.itemWidget(item)
            if isinstance(widget, _SymbolListItem):
                widget.set_selected(item.isSelected())

    @staticmethod
    def _format_item_text(sym) -> str:
        kind_icon = {
            "function": "ƒ",
            "method": "ƒ",
            "class": "◉",
            "constant": "C",
            "variable": "v",
            "import": "↩",
            "key": "🔑",
        }.get(sym.kind, "?")

        short_path = os.path.basename(sym.file_path)
        sig = ""
        if hasattr(sym, "signature") and sym.signature:
            sig = f"   <span style='color:{COLORS['TEXT_3']};font-size:12px;'>{sym.signature[:60]}</span>"

        return f"{kind_icon}  {sym.name:<32}   {short_path}:{sym.line}{sig}"

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------

    def _move_selection(self, delta: int) -> None:
        current = self._list.currentRow()
        total = self._list.count()
        if total == 0:
            return
        new_row = (current + delta) % total
        self._list.setCurrentRow(new_row)
        self._list.scrollToItem(self._list.item(new_row))

    def _activate_current(self) -> None:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._current_results):
            return
        sym = self._current_results[row]
        self.hide_palette()
        # 跳转到符号位置
        if self._tab_mgr is not None:
            try:
                self._tab_mgr.goto_file(sym.file_path, line=sym.line)
            except Exception:
                # fallback: 手动尝试
                try:
                    self._tab_mgr.open_file(sym.file_path)
                    editor = self._tab_mgr.get_current_editor()
                    if editor and hasattr(editor, "goto_line"):
                        editor.goto_line(sym.line)
                except Exception:
                    pass
        else:
            # 无 tab 管理器: 仅打印位置信息到 stdout 便于调试
            print(f"[SymbolPalette] goto: {sym.file_path}:{sym.line} — {sym.name}")
