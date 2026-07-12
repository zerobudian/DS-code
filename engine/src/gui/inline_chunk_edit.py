# -*- coding: utf-8 -*-
"""内联编辑 - 逐块级审查 (Inline Diff Hunk by Hunk)

每个 Hunk 独立接受/拒绝。
"""

import os
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QWidget, QTextEdit, QScrollArea
)

from src.gui.style import COLORS, FONT_STACK, RADII


class InlineDiffEditor(QFrame):
    """逐块审查编辑器 - 按 Hunk 接受/拒绝。

    构造参数:
        file_path: 文件路径
        old_content: 原内容
        new_content: AI 修改后内容
        reason: 修改原因
        parent: 父控件

    对外信号:
        file_accepted[str]  文件被接受
        file_rejected[str]  文件被拒绝
        hunk_applied[int, bool]  单块被应用/拒绝 (hunk_index, accepted)

    对外 API:
        apply_all() -> bool
        reject_all() -> bool
        get_result_content() -> str
    """

    file_accepted = Signal(str)
    file_rejected = Signal(str)
    hunk_applied = Signal(int, bool)

    def __init__(self, file_path, old_content, new_content, reason="", parent=None):
        super().__init__(parent)
        self._file_path = file_path
        self._old = old_content or ""
        self._new = new_content or ""
        self._reason = reason
        self._hunk_widgets = []

        self.setObjectName("InlineDiffEditor")
        self.setStyleSheet(
            "QFrame#InlineDiffEditor{background:" + COLORS['CARD'] + ";"
            "border-radius:" + RADII['full'] + ";}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        # 头部: 文件名
        header = QHBoxLayout()
        header.setSpacing(12)

        file_label = QLabel("  " + os.path.basename(str(file_path or "文件")))
        file_label.setStyleSheet(
            "color:" + COLORS['TEXT'] + ";font-size:16px;font-weight:700;"
            "font-family:" + FONT_STACK + ";"
        )
        header.addWidget(file_label, 1)
        root.addLayout(header)

        # 修改原因
        if reason:
            reason_label = QLabel("  " + reason)
            reason_label.setStyleSheet(
                "color:" + COLORS['PRIM_DARK'] + ";"
                "font-size:13px;font-weight:500;"
                "font-family:" + FONT_STACK + ";"
                "background:" + COLORS['PRIM_LIGHT'] + ";"
                "border-radius:" + RADII['lg'] + ";"
                "padding:8px 14px;"
            )
            reason_label.setWordWrap(True)
            root.addWidget(reason_label)

        # 计算 Hunks
        self._hunks = self._compute_hunks(self._old, self._new)
        self._hunk_states = ["pending"] * len(self._hunks)

        # 总体状态
        self._overall_status = QLabel(self._status_text())
        self._overall_status.setStyleSheet(
            "color:" + COLORS['TEXT_3'] + ";"
            "font-size:13px;font-family:" + FONT_STACK + ";"
        )
        root.addWidget(self._overall_status)

        # Hunk 列表 - 滚动区
        hunk_container = QWidget()
        hunk_layout = QVBoxLayout(hunk_container)
        hunk_layout.setContentsMargins(0, 0, 0, 0)
        hunk_layout.setSpacing(8)

        if not self._hunks:
            empty = QLabel("  当前文件与原内容完全一致,无需修改。")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(
                "color:" + COLORS['TEXT_4'] + ";font-size:14px;"
                "font-family:" + FONT_STACK + ";"
                "padding:30px;"
                "background:" + COLORS['CARD'] + ";"
                "border-radius:" + RADII['lg'] + ";"
            )
            hunk_layout.addWidget(empty)
        else:
            for idx, h in enumerate(self._hunks):
                frame = self._render_hunk(idx, h)
                hunk_layout.addWidget(frame)

        hunk_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(hunk_container)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollBar:vertical{background:transparent;width:8px;}"
            "QScrollBar::handle:vertical{background:" + COLORS['BORDER'] + ";"
            "border-radius:4px;min-height:30px;}"
        )
        root.addWidget(scroll, 1)

        # 底部: 接受全部 / 拒绝全部
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        bottom.addStretch(1)

        accept_all = QPushButton("  全部接受")
        accept_all.setFixedHeight(36)
        accept_all.setCursor(Qt.PointingHandCursor)
        accept_all.setStyleSheet(
            "QPushButton{background:" + COLORS['SUCCESS'] + ";color:" + COLORS['CARD'] + ";"
            "border:none;border-radius:" + RADII['lg'] + ";"
            "font-size:14px;font-weight:700;padding:0 18px;"
            "font-family:" + FONT_STACK + ";}"
            "QPushButton:hover{background:" + COLORS['PRIM'] + ";}"
        )
        accept_all.clicked.connect(self.apply_all)
        bottom.addWidget(accept_all)

        reject_all = QPushButton("  全部拒绝")
        reject_all.setFixedHeight(36)
        reject_all.setCursor(Qt.PointingHandCursor)
        reject_all.setStyleSheet(
            "QPushButton{background:" + COLORS['ERROR'] + ";color:" + COLORS['CARD'] + ";"
            "border:none;border-radius:" + RADII['lg'] + ";"
            "font-size:14px;font-weight:700;padding:0 18px;"
            "font-family:" + FONT_STACK + ";}"
            "QPushButton:hover{background:" + COLORS['ERROR_DARK'] + ";}"
        )
        reject_all.clicked.connect(self.reject_all)
        bottom.addWidget(reject_all)
        root.addLayout(bottom)

    def _compute_hunks(self, old, new):
        """解析 unified diff 得到 Hunks"""
        import difflib
        hunks = []
        try:
            diff_lines = list(difflib.unified_diff(
                (old or "").splitlines(keepends=False),
                (new or "").splitlines(keepends=False),
                fromfile="old", tofile="new", lineterm=""
            ))
        except Exception:
            diff_lines = []

        current = None
        for line in diff_lines or []:
            if line.startswith("@@"):
                parts = line.split()
                try:
                    old_part = [p for p in parts if p.startswith("-")][0]
                    new_part = [p for p in parts if p.startswith("+")][0]
                    old_start = int(old_part.lstrip("-").split(",")[0])
                    new_start = int(new_part.lstrip("+").split(",")[0])
                    current = {
                        "old_start": old_start,
                        "new_start": new_start,
                        "lines": [],
                    }
                    hunks.append(current)
                except (ValueError, IndexError):
                    current = None
            elif current is not None:
                if line.startswith("+"):
                    current["lines"].append(("+", line[1:]))
                elif line.startswith("-"):
                    current["lines"].append(("-", line[1:]))
                elif line.startswith(" "):
                    current["lines"].append((" ", line[1:]))

        return hunks

    def _render_hunk(self, idx, hunk):
        frame = QFrame()
        obj_name = "Hunk_" + str(idx)
        frame.setObjectName(obj_name)
        frame.setStyleSheet(
            "QFrame#" + obj_name + "{"
            "background:" + COLORS['CODE_BGD'] + ";"
            "border:1px solid " + COLORS['BORDER'] + ";"
            "border-radius:" + RADII['lg'] + ";"
            "}"
        )
        v = QVBoxLayout(frame)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(8)

        # 标题: 位置 + 状态
        header = QHBoxLayout()
        title = QLabel("  Line " + str(hunk["old_start"]) + " -> " + str(hunk["new_start"]))
        title.setStyleSheet(
            "color:" + COLORS['TEXT'] + ";font-size:13px;font-weight:700;"
            "font-family:" + FONT_STACK + ";"
        )
        header.addWidget(title, 1)

        status_label = QLabel("待审查")
        status_label.setStyleSheet(
            "color:" + COLORS['TEXT_3'] + ";font-size:12px;"
            "font-weight:500;font-family:" + FONT_STACK + ";"
        )
        header.addWidget(status_label)
        v.addLayout(header)

        # 内容: 带颜色的 diff 视图
        viewer = QTextEdit()
        viewer.setReadOnly(True)
        viewer.setStyleSheet(
            "QTextEdit{background:" + COLORS['CODE_BGD'] + ";"
            "border:none;color:" + COLORS['TEXT'] + ";"
            "font-family:'SF Mono',Menlo,Consolas,monospace;"
            "font-size:13px;border-radius:" + RADII['md'] + ";}"
        )

        content_html = []
        for tag, text in hunk["lines"]:
            if tag == "-":
                line = ("<span style='background:" + COLORS['DIFF_REMOVE']
                        + ";color:" + COLORS['TEXT'] + ";'>-" + self._html_escape(text) + "</span>")
            elif tag == "+":
                line = ("<span style='background:" + COLORS['DIFF_ADD']
                        + ";color:" + COLORS['TEXT'] + ";'>+" + self._html_escape(text) + "</span>")
            else:
                line = ("<span style='color:" + COLORS['TEXT_3'] + ";'> "
                        + self._html_escape(text) + "</span>")
            content_html.append(line + "<br>")

        viewer.setHtml(
            "<pre style='margin:0;padding:6px 8px;line-height:1.8;'>"
            + "".join(content_html) + "</pre>"
        )
        viewer.setMinimumHeight(100 + len(hunk["lines"]) * 28)
        viewer.setMaximumHeight(420)
        v.addWidget(viewer)

        # 按钮: 接受此块 / 拒绝此块
        btns = QHBoxLayout()
        btns.addStretch(1)

        btn_accept = QPushButton("  接受此块")
        btn_accept.setFixedHeight(32)
        btn_accept.setCursor(Qt.PointingHandCursor)
        btn_accept.setStyleSheet(
            "QPushButton{background:" + COLORS['DIFF_ADD'] + ";color:" + COLORS['CARD'] + ";"
            "border:none;border-radius:10px;"
            "font-size:13px;font-weight:600;"
            "padding:0 14px;font-family:" + FONT_STACK + ";}"
        )
        btn_accept.clicked.connect(
            lambda checked=False, _d=idx, b=status_label: self._on_hunk_action(_d, True, b)
        )
        btns.addWidget(btn_accept)

        btn_reject = QPushButton("  拒绝此块")
        btn_reject.setFixedHeight(32)
        btn_reject.setCursor(Qt.PointingHandCursor)
        btn_reject.setStyleSheet(
            "QPushButton{background:" + COLORS['DIFF_REMOVE'] + ";color:" + COLORS['CARD'] + ";"
            "border:none;border-radius:10px;"
            "font-size:13px;font-weight:600;"
            "padding:0 14px;font-family:" + FONT_STACK + ";}"
        )
        btn_reject.clicked.connect(
            lambda checked=False, _d=idx, b=status_label: self._on_hunk_action(_d, False, b)
        )
        btns.addWidget(btn_reject)
        v.addLayout(btns)

        return frame

    def _on_hunk_action(self, idx, accept, status_label):
        self._hunk_states[idx] = "accepted" if accept else "rejected"
        if accept:
            status_label.setText("已接受  ")
            status_label.setStyleSheet(
                "color:" + COLORS['SUCCESS'] + ";"
                "font-size:12px;font-weight:700;"
                "font-family:" + FONT_STACK + ";"
            )
        else:
            status_label.setText("已拒绝  ")
            status_label.setStyleSheet(
                "color:" + COLORS['ERROR'] + ";"
                "font-size:12px;font-weight:700;"
                "font-family:" + FONT_STACK + ";"
            )
        self._overall_status.setText(self._status_text())
        self.hunk_applied.emit(idx, accept)

    def _status_text(self):
        if not self._hunks:
            return ""
        acc = sum(1 for s in self._hunk_states if s == "accepted")
        rej = sum(1 for s in self._hunk_states if s == "rejected")
        pending = len(self._hunks) - acc - rej
        return "状态: {a} 已接受 / {r} 已拒绝 / {p} 待审查".format(a=acc, r=rej, p=pending)

    def apply_all(self):
        for i in range(len(self._hunks)):
            self._hunk_states[i] = "accepted"
        self.refresh()
        try:
            self.file_accepted.emit(self._file_path)
        except Exception:
            pass
        return True

    def reject_all(self):
        for i in range(len(self._hunks)):
            self._hunk_states[i] = "rejected"
        self.refresh()
        try:
            self.file_rejected.emit(self._file_path)
        except Exception:
            pass
        return True

    def refresh(self):
        if getattr(self, "_overall_status", None) is not None:
            self._overall_status.setText(self._status_text())

    def get_result_content(self):
        """基于当前 hunk 接受/拒绝状态,构造最终内容。
        
        对 accepted 的 hunk 使用新内容，对 rejected/pending 的 hunk 使用旧内容，
        实现逐 hunk 合并而非全量回退。
        """
        if not self._hunks:
            return self._new
        
        old_lines = (self._old or "").splitlines(keepends=False)
        result = []
        old_idx = 0  # 0-based index into old_lines
        
        for i, hunk in enumerate(self._hunks):
            state = self._hunk_states[i]
            lines = hunk["lines"]
            
            # 分离旧内容行（- 和 context）和新内容行（+ 和 context）
            old_hunk_lines = [content for sign, content in lines if sign in ("-", " ")]
            new_hunk_lines = [content for sign, content in lines if sign in ("+", " ")]
            
            # 添加 hunk 之前的未变更旧行
            hunk_old_start = max(0, hunk["old_start"] - 1)  # 转为 0-based
            while old_idx < hunk_old_start and old_idx < len(old_lines):
                result.append(old_lines[old_idx])
                old_idx += 1
            
            if state == "accepted":
                result.extend(new_hunk_lines)
            else:
                # rejected 或 pending：使用旧内容
                result.extend(old_hunk_lines)
            
            # 跳过此 hunk 覆盖的旧行
            old_idx += len(old_hunk_lines)
        
        # 添加最后一个 hunk 之后的剩余旧行
        while old_idx < len(old_lines):
            result.append(old_lines[old_idx])
            old_idx += 1
        
        return "\n".join(result)

    def _html_escape(self, text):
        return (text.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .replace('"', "&quot;"))
