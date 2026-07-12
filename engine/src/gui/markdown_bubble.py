# -*- coding: utf-8 -*-
"""MarkdownBubble — 高质量 Markdown 渲染气泡

把 AI 输出的 Markdown 文本做如下处理：
  1. 解析 **粗体** / *斜体* / `行内代码` / [链接]
  2. 识别代码块 ```lang ... ``` → 带语法高亮 + 复制按钮
  3. 列表 / 标题 分级缩进
  4. 漂亮的引用块
"""

from __future__ import annotations

import re
import html
from typing import List

from PySide6.QtCore import Qt, Signal, QTimer, QSize
from PySide6.QtGui import QClipboard
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QPlainTextEdit, QApplication,
)

from src.gui.style import COLORS, RADII, FONT_STACK, FONT_CODE
from src.gui._gui_helpers import _load_icon


# ---------------------------------------------------------------------------
# 轻量级 Markdown -> HTML 解析器
# ---------------------------------------------------------------------------

class _MarkdownRenderer:
    """极简 Markdown 解析器。"""

    _INLINE_CODE = re.compile(r"`([^`]+)`")
    _BOLD = re.compile(r"\*\*([^*]+)\*\*")
    _ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
    _LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

    def render(self, md_text: str) -> str:
        if not md_text:
            return ""
        lines = md_text.split("\n")
        html_parts: List[str] = []

        i = 0
        in_code_block = False
        code_lines: List[str] = []
        code_lang = ""

        while i < len(lines):
            line = lines[i]

            # 代码块开始 / 结束
            if line.strip().startswith("```"):
                if not in_code_block:
                    in_code_block = True
                    code_lines = []
                    code_lang = line.strip()[3:].strip() or "text"
                    i += 1
                    continue
                else:
                    html_parts.append(self._render_code_block(code_lang, code_lines))
                    in_code_block = False
                    code_lines = []
                    code_lang = ""
                    i += 1
                    continue

            if in_code_block:
                code_lines.append(line)
                i += 1
                continue

            # 标题
            if re.match(r"^#{1,6}\s+", line):
                hashes = len(line) - len(line.lstrip("#"))
                hashes = min(hashes, 6)
                font_sizes = {1: 22, 2: 18, 3: 16, 4: 14, 5: 14, 6: 13}
                weights = {1: 800, 2: 700, 3: 700, 4: 600, 5: 600, 6: 500}
                # 注意: 下面的 CSS 里大括号都要用字符串拼接（避免在 f-string ）
                styled = (
                    "<div style='font-size:" + str(font_sizes[hashes]) + "px;font-weight:" + str(weights[hashes]) + ";"
                    "color:" + COLORS['TEXT'] + ";"
                    "margin-top:" + str(10 if hashes <= 2 else 6) + "px;"
                    "margin-bottom:" + str(6 if hashes <= 2 else 4) + "px;"
                    "font-family:" + FONT_STACK + ";'>" + self._inline(line.lstrip('#').strip()) + "</div>"
                )
                html_parts.append(styled)
                i += 1
                continue

            # 引用块
            if line.startswith("> "):
                quote_lines = [line[2:]]
                i += 1
                while i < len(lines) and lines[i].startswith("> "):
                    quote_lines.append(lines[i][2:])
                    i += 1
                block = (
                    "<div style='background:" + COLORS['THINK_BGD'] + ";"
                    "border-left:4px solid " + COLORS['PRIM'] + ";"
                    "padding:10px 14px;border-radius:" + RADII['xs'] + ";"
                    "color:" + COLORS['TEXT_2'] + ";"
                    "font-family:" + FONT_STACK + ";font-size:14px;margin:6px 0;'>"
                    + self._inline("<br>".join(quote_lines)) + "</div>"
                )
                html_parts.append(block)
                continue

            # 水平分割线
            if line.strip() in ("---", "***", "___"):
                line_sep = (
                    "<hr style='border:none;border-top:1px solid "
                    + COLORS['BORDER']
                    + ";margin:10px 0;'/>"
                )
                html_parts.append(line_sep)
                i += 1
                continue

            # 有序列表
            if re.match(r"^\s*\d+\.\s+", line):
                list_items: List[str] = []
                while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                    list_items.append(re.sub(r"^\s*\d+\.\s+", "", lines[i]))
                    i += 1
                html_parts.append(self._render_list("ol", list_items))
                continue

            # 无序列表
            if re.match(r"^\s*[-*•]\s+", line):
                list_items = []
                while i < len(lines) and re.match(r"^\s*[-*•]\s+", lines[i]):
                    list_items.append(re.sub(r"^\s*[-*•]\s+", "", lines[i]))
                    i += 1
                html_parts.append(self._render_list("ul", list_items))
                continue

            # 空行
            if not line.strip():
                html_parts.append("<div style='height:4px;'></div>")
                i += 1
                continue

            # 普通段落
            para = (
                "<p style='color:" + COLORS['TEXT'] + ";"
                "font-size:14px;line-height:1.7;margin:4px 0;"
                "font-family:" + FONT_STACK + ";'>" + self._inline(line) + "</p>"
            )
            html_parts.append(para)
            i += 1

        if in_code_block and code_lines:
            html_parts.append(self._render_code_block(code_lang, code_lines))

        return "".join(html_parts)

    def _inline(self, text: str) -> str:
        # 先对整段文本做 HTML 转义，防止注入任意 HTML 标签
        text = html.escape(text, quote=True)
        # 代码 (先处理，避免 *在代码内被错误处理
        bg = COLORS['THINK_BGD']
        prim = COLORS['PRIM_DARK']
        border = COLORS['BORDER']
        code_tmpl = (
            "<code style='background:" + bg + ";"
            "padding:1px 6px;border-radius:" + RADII['tail'] + ";font-size:12.5px;"
            "font-family:" + FONT_CODE + ";"
            "color:" + prim + ";"
            "border:1px solid " + border + ";'>"
        )
        # 注意：text 已被 html.escape，& < > " ' 已转为实体，正则匹配 * ` [ ] 等不受影响
        text = self._INLINE_CODE.sub(lambda m: code_tmpl + m.group(1) + "</code>", text)
        # 链接 — 增加 URL scheme 白名单校验，阻止 javascript:/data:/file: 等危险协议
        def _link_replace(m):
            url = m.group(2)
            if not re.match(r'^(https?://|mailto:|tel:)', url, re.IGNORECASE):
                return m.group(0)  # 非白名单协议，渲染为纯文本
            return (
                "<a href='" + url + "' style='color:"
                + prim + ";text-decoration:underline;'>" + m.group(1) + "</a>"
            )
        text = self._LINK.sub(_link_replace, text)
        # 粗体
        text = self._BOLD.sub(
            lambda m: "<span style='font-weight:700;color:" + COLORS['TEXT'] + ";'>"
            + m.group(1) + "</span>",
            text
        )
        # 斜体
        text = self._ITALIC.sub(
            lambda m: "<span style='font-style:italic;'>" + m.group(1) + "</span>",
            text
        )
        return text

    def _render_list(self, tag: str, items: List[str]) -> str:
        bullet_dot = {"ul": "•", "ol": None}[tag]
        html_out: List[str] = ["<div style='margin:6px 0;padding-left:6px;'>"]
        for idx, it in enumerate(items, 1):
            prefix = (
                "<span style='color:" + COLORS['PRIM_DARK']
                + ";font-weight:700;margin-right:8px;'>"
                + (str(idx) + "." if bullet_dot is None else bullet_dot) + "</span>"
            )
            line_html = (
                "<div style='color:" + COLORS['TEXT'] + ";"
                "font-size:14px;line-height:1.7;margin:2px 0;"
                "font-family:" + FONT_STACK + ";"
                "padding-left:14px;text-indent:-14px;'>"
                + prefix + self._inline(it) + "</div>"
            )
            html_out.append(line_html)
        html_out.append("</div>")
        return "".join(html_out)

    def _render_code_block(self, lang: str, lines: List[str]) -> str:
        code = "\n".join(lines)
        return (
            "__MD_CODE_BLOCK_START__|" + html.escape(lang or "text") + "|__MD_CODE_BLOCK_START__"
            + html.escape(code) + "__MD_CODE_BLOCK_END__"
        )


# ---------------------------------------------------------------------------
# CodeBlock — 带复制按钮的代码块 widget
# ---------------------------------------------------------------------------

class CodeBlock(QFrame):
    """代码块 widget — 带语言标签 + 复制按钮 + 深色主题。"""

    def __init__(self, language: str, code_text: str, parent=None):
        super().__init__(parent)
        self._code = code_text
        self._lang = language
        # 使用普通字符串拼接，避免 Qt stylesheet 的花括号与 f-string 冲突
        self.setStyleSheet(
            "QFrame#CodeBlock{background:" + COLORS['CODE_BGD_DARK'] + ";"
            "border:1px solid " + COLORS['CODE_BGD_DARK'] + ";"
            "border-radius:" + RADII['md'] + ";}"
        )
        self.setObjectName("CodeBlock")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # header
        header = QFrame()
        header.setStyleSheet(
            "QFrame{background:" + COLORS['CODE_BGD_DARK'] + ";"
            "border-top-left-radius:" + RADII['md'] + ";"
            "border-top-right-radius:" + RADII['md'] + ";}"
        )
        hl = QHBoxLayout(header)
        hl.setContentsMargins(12, 6, 12, 6)
        hl.setSpacing(8)

        lang_lbl = QLabel(language or "text")
        lang_lbl.setStyleSheet(
            "color:" + COLORS['TEXT_4'] + ";font-size:11px;font-weight:600;"
            "font-family:" + FONT_STACK + ";"
        )
        hl.addWidget(lang_lbl, 1)

        btn_copy = QPushButton("复制")
        btn_copy.setCursor(Qt.PointingHandCursor)
        btn_copy.setFixedHeight(24)
        btn_copy.setIcon(_load_icon("copy_16.png", 12))
        btn_copy.setIconSize(QSize(12, 12))
        btn_copy.setStyleSheet(
            "QPushButton{background:transparent;color:" + COLORS['TEXT_4'] + ";border:none;"
            "padding:0 8px;font-size:11px;"
            "font-family:" + FONT_STACK + ";}"
            "QPushButton:hover{color:" + COLORS['DISABLED'] + ";}"
        )
        btn_copy.clicked.connect(self._on_copy)
        hl.addWidget(btn_copy)

        outer.addWidget(header)

        # body — 代码区
        code_edit = QPlainTextEdit()
        code_edit.setPlainText(code_text)
        code_edit.setReadOnly(True)
        code_edit.setMinimumHeight(min(400, max(60, 24 + code_text.count("\n") * 16)))
        code_edit.setStyleSheet(
            "QPlainTextEdit{background:" + COLORS['CODE_BGD_DARK'] + ";color:" + COLORS['CODE_TEXT'] + ";border:none;"
            "padding:12px 14px;"
            "font-family:" + FONT_CODE + ";"
            "font-size:12.5px;line-height:1.6;"
            "border-bottom-left-radius:" + RADII['md'] + ";"
            "border-bottom-right-radius:" + RADII['md'] + ";}"
        )
        outer.addWidget(code_edit, 1)
        self._code_edit = code_edit

    def _on_copy(self) -> None:
        cb: QClipboard = QApplication.clipboard()
        cb.setText(self._code)
        # 视觉反馈 (短暂改变按钮文字)
        for btn in self.findChildren(QPushButton):
            if btn.text() == "复制":
                original_text = btn.text()
                original_icon = btn.icon()
                btn.setText("已复制")
                btn.setIcon(_load_icon("check_16.png", 12))
                btn.setIconSize(QSize(12, 12))
                def _restore_text(_b=btn, _ot=original_text, _oi=original_icon):
                    try:
                        _b.setText(_ot)
                        _b.setIcon(_oi)
                    except RuntimeError:
                        pass  # 按钮已被销毁（流式更新触发 _redraw）
                QTimer.singleShot(1200, _restore_text)
                break


# ---------------------------------------------------------------------------
# MarkdownBubble — 主气泡
# ---------------------------------------------------------------------------

class MarkdownBubble(QFrame):
    """把 Markdown 渲染为漂亮的富文本气泡。"""

    clicked = Signal()

    def __init__(self, markdown_text: str = "", parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            "QFrame#MarkdownBubble{background:" + COLORS['CARD'] + ";"
            "border:1px solid " + COLORS['BORDER'] + ";"
            "border-radius:" + RADII['lg'] + ";}"
        )
        self.setObjectName("MarkdownBubble")

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(6)

        self._renderer = _MarkdownRenderer()
        self.set_markdown(markdown_text or "")

    def set_markdown(self, text: str) -> None:
        # 清空旧内容
        while self.layout().count() > 0:
            item = self.layout().takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

        rendered = self._renderer.render(text or "")
        segments: List[tuple] = []
        rest = rendered
        while True:
            m = re.search(
                r"__MD_CODE_BLOCK_START__\|([^|]+)\|__MD_CODE_BLOCK_START__",
                rest,
            )
            if not m:
                if rest.strip():
                    segments.append(("text", rest))
                break
            before = rest[:m.start()]
            if before.strip():
                segments.append(("text", before))
            lang = m.group(1)
            after_marker = rest[m.end():]
            end = after_marker.find("__MD_CODE_BLOCK_END__")
            if end < 0:
                code = after_marker
                rest = ""
            else:
                code = after_marker[:end]
                rest = after_marker[end + len("__MD_CODE_BLOCK_END__"):]
            segments.append(("code", lang, code))
            if not rest.strip():
                break

        for seg in segments:
            if seg[0] == "text":
                lbl = QLabel()
                lbl.setTextFormat(Qt.RichText)
                lbl.setText(seg[1])
                lbl.setWordWrap(True)
                lbl.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
                lbl.setOpenExternalLinks(True)
                lbl.setStyleSheet(
                    "background:transparent;color:" + COLORS['TEXT'] + ";"
                    "font-family:" + FONT_STACK + ";"
                )
                self.layout().addWidget(lbl)
            else:
                _, lang, code = seg
                cb = CodeBlock(html.unescape(lang), html.unescape(code))
                self.layout().addWidget(cb)

    def mouseReleaseEvent(self, event) -> None:
        self.clicked.emit()
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# StreamingMarkdownBubble — 流式输出气泡
# ---------------------------------------------------------------------------

class StreamingMarkdownBubble(QFrame):
    """AI 流式输出时使用：收集增量文本，每次刷新渲染。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            "QFrame#StreamingMarkdownBubble{background:" + COLORS['CARD'] + ";"
            "border:1px solid " + COLORS['BORDER'] + ";"
            "border-radius:" + RADII['lg'] + ";}"
        )
        self.setObjectName("StreamingMarkdownBubble")

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(6)

        self._container = QVBoxLayout()
        self._container.setContentsMargins(0, 0, 0, 0)
        self._container.setSpacing(6)
        root.addLayout(self._container, 1)

        self._buffer = ""
        self._renderer = _MarkdownRenderer()

    def append(self, delta: str) -> None:
        """追加流式增量。"""
        self._buffer += delta
        self._redraw()

    def set_text(self, text: str) -> None:
        """重置为完整文本。"""
        self._buffer = text
        self._redraw()

    def get_text(self) -> str:
        return self._buffer

    def _redraw(self) -> None:
        # 清空旧内容
        while self._container.count() > 0:
            item = self._container.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

        rendered = self._renderer.render(self._buffer)
        rest = rendered
        while True:
            m = re.search(
                r"__MD_CODE_BLOCK_START__\|([^|]+)\|__MD_CODE_BLOCK_START__",
                rest,
            )
            if not m:
                if rest.strip():
                    self._add_html_label(rest)
                break
            before = rest[:m.start()]
            if before.strip():
                self._add_html_label(before)
            lang = m.group(1)
            after_marker = rest[m.end():]
            end = after_marker.find("__MD_CODE_BLOCK_END__")
            if end < 0:
                code = after_marker
                rest = ""
            else:
                code = after_marker[:end]
                rest = after_marker[end + len("__MD_CODE_BLOCK_END__"):]
            cb = CodeBlock(html.unescape(lang), html.unescape(code))
            self._container.addWidget(cb)
            if not rest.strip():
                break

    def _add_html_label(self, html_text: str) -> None:
        lbl = QLabel()
        lbl.setTextFormat(Qt.RichText)
        lbl.setText(html_text)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        lbl.setOpenExternalLinks(True)
        lbl.setStyleSheet(
            "background:transparent;color:" + COLORS['TEXT'] + ";"
            "font-family:" + FONT_STACK + ";"
        )
        self._container.addWidget(lbl)
