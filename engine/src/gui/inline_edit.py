# -*- coding: utf-8 -*-
"""InlineEditController — IDE 风格的文件内联编辑

当 AI 要修改一个文件时，不弹对话框而是：
  1. 在 TabManager 中打开（或聚焦到）目标文件
  2. 直接把编辑器内容替换为 AI 提议的新版本
  3. 在左侧 gutter 显示高亮标记：绿色=新增，黄色=修改，红色=删除
  4. 在 Tab 顶部浮动一个 Accept/Reject 操作条

这比传统 "弹窗 + Diff 预览 + 用户确认" 的工作流要快得多，
用户直接在自己的编辑器文件中看到最终效果，像 Cursor/Sonnet 一样。

典型用法:
    ctrl = InlineEditController(tab_manager)
    ctrl.apply_edits([
        { "file": "/path/to/foo.py", "new_content": "...", "reason": "修复 bug" },
        ...
    ])

也可以单文件:
    ctrl.apply_edit("/path/to/foo.py", new_content, reason="优化性能")

快捷键:
    Ctrl+Y / Cmd+Y   接受 (Yes, apply)
    Ctrl+R           拒绝 (Reject)
"""

from __future__ import annotations

import os
import difflib
from typing import List, Optional, Dict

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
)

from src.gui.style import COLORS, FONT_STACK, RADII
from src.gui._gui_helpers import _darken  # noqa: F401  centralized _darken import


# ---------------------------------------------------------------------------
# Inline Edit — 应用单个文件的 AI 提议修改
# ---------------------------------------------------------------------------

class EditProposal:
    """一次修改的完整描述。"""

    def __init__(self, file_path: str, new_content: str,
                 reason: str = "", original_content: Optional[str] = None):
        self.file_path = os.path.abspath(file_path)
        self.new_content = new_content
        self.reason = reason or ""
        self.original_content = original_content
        self.accepted = False
        self.rejected = False


class InlineEditController(QObject):
    """管理一批 InlineEditBar（每个被修改的文件一个）。"""

    all_applied = Signal()        # 所有文件都被接受
    all_rejected = Signal()       # 所有文件都被拒绝
    any_changed = Signal()        # 至少一个文件被接受或拒绝

    def __init__(self, tab_manager=None, parent=None):
        super().__init__(parent)
        self._tab_mgr = tab_manager
        self._active_bars: Dict[str, "InlineEditBar"] = {}  # file_path -> bar

    # ------------------------------------------------------------------
    # 核心 API
    # ------------------------------------------------------------------

    def apply_edit(self, file_path: str, new_content: str,
                   reason: str = "") -> Optional["InlineEditBar"]:
        """应用一次单文件编辑。返回对应浮条（供用户 Accept/Reject）。"""
        if self._tab_mgr is None:
            return None
        file_path = os.path.abspath(file_path)

        # 打开 / 聚焦文件
        _is_new_file = False
        try:
            editor = self._tab_mgr.open_file(file_path)
        except Exception:
            editor = None
        if editor is None:
            # open_file 可能返回 None（未知路径）；尝试 open_content fallback
            _is_new_file = True
            try:
                editor = self._tab_mgr.open_content(new_content,
                                                    os.path.basename(file_path))
                editor._file_path = file_path  # 手动关联
            except Exception:
                return None

        # 记录原内容 (新文件原内容为空字符串，已有文件取编辑器当前内容)
        if _is_new_file:
            original = ""
        else:
            try:
                original = editor.toPlainText()
            except Exception:
                original = ""

        # 写入新内容 (但不立即保存文件 — 用户在编辑器中先看效果)
        try:
            cursor = editor.textCursor()
            cursor.select(QTextCursor.Document)
            cursor.insertText(new_content)
            editor.setTextCursor(cursor)
        except Exception:
            try:
                editor.setPlainText(new_content)
            except Exception:
                pass

        # 计算 & 高亮 diff
        markers = self._highlight_diff(editor, original, new_content)

        # 创建接受/拒绝浮条
        bar = InlineEditBar(
            file_path=file_path,
            original_content=original,
            proposed_content=new_content,
            reason=reason,
            parent=editor,
        )
        bar.accepted.connect(lambda fp=file_path, ed=editor, b=bar: self._on_accepted(fp, ed, b))
        bar.rejected.connect(lambda fp=file_path, ed=editor, b=bar: self._on_rejected(fp, ed, b))

        # 关闭同文件的旧浮条，防止状态混乱
        old_bar = self._active_bars.get(file_path)
        if old_bar is not None and old_bar is not bar:
            try:
                old_bar.close()
            except Exception:
                pass
        self._active_bars[file_path] = bar
        bar.show()

        # 把编辑器滚动到第一个修改处 (若有)
        first_changed = sorted(markers.keys())[0] if markers else -1
        if first_changed >= 0:
            try:
                editor.goto_line(first_changed + 1)
            except Exception:
                pass

        return bar

    def apply_edits(self, proposals: List[Dict[str, str]]) -> List["InlineEditBar"]:
        """批量应用。每项需要: {"file", "new_content", reason(可选)}"""
        bars: List[InlineEditBar] = []
        for p in proposals:
            fp = p.get("file") or p.get("path") or p.get("filepath")
            new = p.get("new_content") or p.get("content") or ""
            reason = p.get("reason") or p.get("why") or ""
            if fp and new:
                b = self.apply_edit(fp, new, reason)
                if b is not None:
                    bars.append(b)
        return bars

    def has_active(self) -> bool:
        return len(self._active_bars) > 0

    def active_count(self) -> int:
        return len(self._active_bars)

    def accept_all(self) -> None:
        for bar in list(self._active_bars.values()):
            bar.accept()

    def reject_all(self) -> None:
        for bar in list(self._active_bars.values()):
            bar.reject()

    # ------------------------------------------------------------------
    # 内部回调
    # ------------------------------------------------------------------

    def _on_accepted(self, file_path: str, editor, bar) -> None:
        # 保存到磁盘
        try:
            editor.save_file(file_path)
        except Exception:
            try:
                editor.save_file()
            except Exception:
                pass

        # 清除高亮
        try:
            editor.set_diff_markers({})
            editor.clear_diff_extra_selections()
        except Exception:
            try:
                editor.setExtraSelections([])
            except Exception:
                pass

        self._active_bars.pop(file_path, None)
        self.any_changed.emit()
        if not self._active_bars:
            self.all_applied.emit()

    def _on_rejected(self, file_path: str, editor, bar) -> None:
        # 恢复原内容
        try:
            editor.setPlainText(bar.original_content)
            # 清除修改标记，防止残留 "*" 状态
            editor.document().setModified(False)
            if hasattr(editor, 'file_modified'):
                editor.file_modified.emit(False)
        except Exception:
            pass
        try:
            editor.set_diff_markers({})
            editor.clear_diff_extra_selections()
        except Exception:
            try:
                editor.setExtraSelections([])
            except Exception:
                pass

        self._active_bars.pop(file_path, None)
        self.any_changed.emit()
        if not self._active_bars:
            self.all_rejected.emit()

    # ------------------------------------------------------------------
    # diff 计算与高亮
    # ------------------------------------------------------------------

    @staticmethod
    def _highlight_diff(editor, original: str, new_content: str) -> Dict[int, str]:
        """在 editor 中为差异行着色。返回 line_index(0-based) -> 'added'|'modified'|'deleted'"""
        old_lines = original.splitlines()
        new_lines = new_content.splitlines()

        # 对齐: SequenceMatcher
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)

        markers: Dict[int, str] = {}
        extras: List = []

        added_fmt = QTextCharFormat()
        added_fmt.setBackground(QColor(220, 252, 231, 180))  # 淡绿

        mod_fmt = QTextCharFormat()
        mod_fmt.setBackground(QColor(254, 249, 195, 180))  # 淡黄

        # 为右侧 (new) 计算行级标记
        try:
            from PySide6.QtWidgets import QPlainTextEdit
            _ExtraSel = QPlainTextEdit.ExtraSelection
        except Exception:
            class _ExtraSel:
                format: QTextCharFormat
                cursor: QTextCursor

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag == "insert":
                # new[j1:j2] 是新增行 — 标绿
                for line_idx in range(j1, j2):
                    markers[line_idx] = "added"
                    block = editor.document().findBlockByNumber(line_idx)
                    if block.isValid():
                        sel = _ExtraSel()
                        sel.format = added_fmt
                        sel.cursor = QTextCursor(block)
                        sel.cursor.select(QTextCursor.BlockUnderCursor)
                        extras.append(sel)
            elif tag == "replace":
                # 两侧都有内容 — 标黄右侧
                for line_idx in range(j1, j2):
                    markers[line_idx] = "modified"
                    block = editor.document().findBlockByNumber(line_idx)
                    if block.isValid():
                        sel = _ExtraSel()
                        sel.format = mod_fmt
                        sel.cursor = QTextCursor(block)
                        sel.cursor.select(QTextCursor.BlockUnderCursor)
                        extras.append(sel)

        # 设置 gutter markers
        try:
            editor.set_diff_markers(markers)
        except Exception:
            pass

        # 设置行背景高亮（使用 set_diff_extra_selections 防止光标移动时覆盖）
        try:
            editor.set_diff_extra_selections(extras)
        except Exception:
            try:
                editor.setExtraSelections(extras)
            except Exception:
                pass

        return markers


# ---------------------------------------------------------------------------
# InlineEditBar — 编辑器顶部浮动条 (Accept / Reject)
# ---------------------------------------------------------------------------

class InlineEditBar(QFrame):
    """浮动在编辑器顶部的 Accept/Reject 操作条。"""

    accepted = Signal()
    rejected = Signal()

    def __init__(self, file_path: str, original_content: str,
                 proposed_content: str, reason: str = "", parent=None):
        super().__init__(parent)
        self.file_path = file_path
        self.original_content = original_content
        self.proposed_content = proposed_content
        self._editor = parent  # Bug fix #4: 保存编辑器引用以便 close 时移除 event filter

        self.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)  # Bug fix #6: close() 时自动删除，防止资源泄漏

        stats = self._compute_stats(original_content, proposed_content)
        self._build_ui(reason, stats)

        # 随父窗口移动
        QTimer.singleShot(30, self._reposition)
        if parent is not None:
            try:
                parent.installEventFilter(self)
            except Exception:
                pass

        # 快捷键: Ctrl+Y accept, Ctrl+R reject
        try:
            from PySide6.QtGui import QKeySequence
            from PySide6.QtWidgets import QShortcut
            sc_y = QShortcut(QKeySequence("Ctrl+Y"), self)
            sc_y.activated.connect(self.accept)
            sc_r = QShortcut(QKeySequence("Ctrl+R"), self)
            sc_r.activated.connect(self.reject)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self, reason: str, stats: str) -> None:
        self.setFixedHeight(56)
        self.setStyleSheet(
            "InlineEditBar{"
            f"background:{COLORS['PRIM']};"
            f"border:1px solid {COLORS['PRIM_DARK']};"
            f"border-radius:{RADII['md']};"
            "}"
        )

        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 8, 14, 8)
        outer.setSpacing(12)

        # 图标
        icon = QLabel("🤖")
        icon.setStyleSheet("font-size:22px;")
        outer.addWidget(icon, 0, Qt.AlignVCenter)

        # 文字说明
        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        title = QLabel(f"AI 建议修改: {os.path.basename(self.file_path)}")
        title.setStyleSheet(
            f"color:{COLORS['TEXT']};"
            f"font-size:13px;font-weight:600;font-family:{FONT_STACK};"
        )
        text_col.addWidget(title)

        sub_text = stats
        if reason:
            sub_text = reason + "   ·   " + sub_text
        sub = QLabel(sub_text[:140])
        sub.setStyleSheet(
            f"color:{COLORS['TEXT_2']};"
            f"font-size:12px;font-weight:400;font-family:{FONT_STACK};"
        )
        text_col.addWidget(sub)

        outer.addLayout(text_col, 1)

        # 按钮
        btn_reject = QPushButton("拒绝 (Esc)")
        btn_reject.setCursor(Qt.PointingHandCursor)
        btn_reject.setFixedHeight(32)
        btn_reject.setStyleSheet(
            f"QPushButton{{background:{COLORS['CARD']};"
            f"color:{COLORS['TEXT']};border:1px solid {COLORS['BORDER']};"
            f"border-radius:{RADII['md']};padding:0 14px;"
            f"font-size:13px;font-weight:600;font-family:{FONT_STACK};}}"
            f"QPushButton:hover{{background:{COLORS['CARD_HOVER']};}}"
        )
        btn_reject.clicked.connect(self.reject)
        outer.addWidget(btn_reject, 0, Qt.AlignVCenter)

        btn_accept = QPushButton("接受 (Ctrl+Y)")
        btn_accept.setCursor(Qt.PointingHandCursor)
        btn_accept.setFixedHeight(32)
        btn_accept.setStyleSheet(
            f"QPushButton{{background:{COLORS['SUCCESS']};color:{COLORS['CARD']};border:none;"
            f"border-radius:{RADII['md']};padding:0 14px;"
            f"font-size:13px;font-weight:600;font-family:{FONT_STACK};}}"
            f"QPushButton:hover{{background:{_darken(COLORS['SUCCESS'], 0.88)};}}"
        )
        btn_accept.clicked.connect(self.accept)
        outer.addWidget(btn_accept, 0, Qt.AlignVCenter)

    def _compute_stats(self, original: str, new: str) -> str:
        old_lines = original.splitlines()
        new_lines = new.splitlines()
        # 使用 SequenceMatcher opcodes 精确统计 added/removed/modified 行数，与 _highlight_diff 保持一致
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        added = 0
        removed = 0
        modified = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "insert":
                added += (j2 - j1)
            elif tag == "delete":
                removed += (i2 - i1)
            elif tag == "replace":
                old_len = i2 - i1
                new_len = j2 - j1
                common = min(old_len, new_len)
                modified += common
                if new_len > old_len:
                    added += (new_len - old_len)
                elif old_len > new_len:
                    removed += (old_len - new_len)
        changed = modified + added
        total = changed + removed
        return f"{changed} 行变更 · {removed} 行删除 · {total} 行差异"

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------

    def accept(self) -> None:
        self.accepted.emit()
        self.close()

    def reject(self) -> None:
        self.rejected.emit()
        self.close()

    # ------------------------------------------------------------------
    # 位置调整 (跟随父 editor 位置)
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (event.Resize, event.Move):
            QTimer.singleShot(10, self._reposition)
        return super().eventFilter(obj, event)

    def _reposition(self) -> None:
        parent = self.parent()
        if parent is None or not hasattr(parent, "mapToGlobal"):
            return
        try:
            top_left = parent.mapToGlobal(parent.rect().topLeft())
            w = min(parent.width(), 640)
            self.setGeometry(top_left.x() + 8, top_left.y() + 8, w, 56)
        except Exception:
            pass

    def closeEvent(self, event):
        """Bug fix #4: 关闭时从编辑器 viewport 移除 event filter，防止泄漏。"""
        try:
            if self._editor is not None:
                try:
                    self._editor.viewport().removeEventFilter(self)
                except Exception:
                    pass
                try:
                    self._editor.removeEventFilter(self)
                except Exception:
                    pass
        except Exception:
            pass
        super().closeEvent(event)
