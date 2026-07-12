# -*- coding: utf-8 -*-
"""用户气泡 UserBubble 与 AI 气泡 AIBubble。"""
from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QSizePolicy,
    QPushButton, QApplication,
)

from src.gui.style import RADII, COLORS, FONT_STACK, FONT_CODE, FONT_WEIGHTS, SIZES, tr
from src.gui._gui_helpers import _load_icon
from src.gui.animations import enter_anim
from src.gui.bubbles.animation_icons import AnimatedWhaleIcon
from src.gui.bubbles.reasoning import ReasoningBubble

# ======================================================================
# UserBubble — 用户气泡
# ======================================================================
class UserBubble(QFrame):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:transparent;")

        # 与 AIBubble 镜像：HBox 布局，右侧头像与左侧鲸鱼对齐
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        # 左侧弹性空间（将气泡推到右侧）
        root.addStretch(1)

        # 气泡容器
        col_container = QWidget()
        col_container.setStyleSheet("background:transparent;")
        col = QVBoxLayout(col_container)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        col.setAlignment(Qt.AlignTop | Qt.AlignRight)
        col_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        bubble = QFrame()
        self._bubble = bubble = QFrame()
        self._text = text
        bubble.setStyleSheet(self._bubble_style())
        bubble.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        bl = QVBoxLayout(bubble)
        bl.setContentsMargins(16, 14, 16, 14)
        bl.setSpacing(4)

        # 空文本保护：用占位符避免气泡塌陷
        safe_text = html.escape(text or " ").replace("\n", "<br>")
        self._lbl = QLabel(safe_text)
        from PySide6.QtGui import QFont
        self._lbl.setFont(QFont("PingFang SC", 14))
        self._lbl.setStyleSheet(self._label_style())
        self._lbl.setWordWrap(True)
        self._lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bl.addWidget(self._lbl)

        col.addWidget(bubble, 0, Qt.AlignRight)
        root.addWidget(col_container, 0, Qt.AlignTop | Qt.AlignRight)

        # 用户头像已隐藏（设计图更简洁，无用户头像）

    def _bubble_style(self):
        return (
            "background:" + COLORS['ACCENT'] + ";"
            "border:none;"
            "border-top-left-radius:" + RADII['bubble'] + ";"
            "border-top-right-radius:" + RADII['tail'] + ";"
            "border-bottom-left-radius:" + RADII['bubble'] + ";"
            "border-bottom-right-radius:" + RADII['bubble'] + ";"
        )

    def _label_style(self):
        return (
            "color:" + COLORS['TEXT_ON_ACCENT'] + ";"
            "font-size:15px; font-weight:" + str(FONT_WEIGHTS['medium']) + "; line-height:1.65;"
            "font-family:" + FONT_STACK + ";"
            "background:transparent;"
            "padding:0px;"
            "min-width:8px;"
        )

    def rebuild_style(self):
        """主题切换后重建样式。"""
        try:
            if hasattr(self, '_bubble'):
                self._bubble.setStyleSheet(self._bubble_style())
            if hasattr(self, '_lbl'):
                self._lbl.setStyleSheet(self._label_style())
        except Exception:
            pass

# ======================================================================
# AIBubble — AI 气泡 (流式输出 + 推理块折叠)
# ======================================================================
class AIBubble(QFrame):
    """AI 回答气泡。左侧带动效的小鲸鱼头像，右侧推理块+文字气泡。"""

    content_changed = Signal()
    copy_clicked = Signal()
    regenerate_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._content_buf = ""
        self.setStyleSheet("background:transparent;")
        self._init_layout()
        self._init_whale_icon()
        self._init_content_column()

    def _init_layout(self):
        """创建主水平布局（鲸鱼头像 + 内容列）。"""
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        self._root = root

    def _init_whale_icon(self):
        """创建左侧带动效的小鲸鱼头像 40x40（思考时头顶持续冒泡）。"""
        self._whale_icon = AnimatedWhaleIcon(40)
        self._root.addWidget(self._whale_icon, 0, Qt.AlignTop)

    def _init_content_column(self):
        """创建右侧纵向内容列：推理块 + 主体文字气泡。"""
        # 右侧：纵向堆叠（推理块 + 主体文字气泡）
        col_container = QWidget()
        col_container.setStyleSheet("background:transparent;")
        self._col = QVBoxLayout(col_container)
        self._col.setContentsMargins(0, 0, 0, 0)
        self._col.setSpacing(10)
        self._col.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        col_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        # 推理块（可折叠）
        self._reasoning = ReasoningBubble()
        self._reasoning.content_changed.connect(self.content_changed.emit)
        self._col.addWidget(self._reasoning, 0, Qt.AlignLeft)

        # 主体气泡 — 浅灰卡片 + 不对称圆角
        self._bubble = QFrame()
        self._bubble.setStyleSheet(self._bubble_style())
        self._bubble.setMaximumWidth(SIZES["bubble"]["max_width"])
        self._bubble.setMinimumWidth(80)
        self._bubble.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        bl = QVBoxLayout(self._bubble)
        bl.setContentsMargins(16, 14, 16, 14)
        bl.setSpacing(6)

        self._content_lbl = QLabel("")
        self._content_lbl.setWordWrap(True)
        self._content_lbl.setMinimumWidth(60)
        self._content_lbl.setMinimumHeight(18)
        self._content_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._content_lbl.setTextFormat(Qt.PlainText)
        self._content_lbl.setOpenExternalLinks(False)
        # 显式设置 QFont 确保 macOS 文本渲染
        from PySide6.QtGui import QFont
        f = QFont("PingFang SC", 14)
        f.setStyleHint(QFont.SansSerif)
        self._content_lbl.setFont(f)
        self._content_lbl.setStyleSheet(self._label_style())
        bl.addWidget(self._content_lbl)
        self._col.addWidget(self._bubble, 0, Qt.AlignLeft)

        # 操作栏：复制 + 重新生成（初始隐藏，内容生成后才显示）
        self._actions = QWidget()
        self._actions.setStyleSheet("background:transparent;")
        act_lay = QHBoxLayout(self._actions)
        act_lay.setContentsMargins(0, 4, 0, 0)
        act_lay.setSpacing(8)
        act_lay.addStretch(1)

        self._copy_btn = QPushButton(tr("action_copy"))
        self._copy_btn.setIcon(_load_icon("copy_18.png", 16))
        self._copy_btn.setIconSize(QSize(16, 16))
        self._copy_btn.setStyleSheet(self._action_btn_style())
        self._copy_btn.setCursor(Qt.PointingHandCursor)
        self._copy_btn.clicked.connect(self._on_copy)
        act_lay.addWidget(self._copy_btn)

        self._regen_btn = QPushButton(tr("action_regenerate"))
        self._regen_btn.setIcon(_load_icon("refresh_18.png", 16))
        self._regen_btn.setIconSize(QSize(16, 16))
        self._regen_btn.setStyleSheet(self._action_btn_style())
        self._regen_btn.setCursor(Qt.PointingHandCursor)
        self._regen_btn.clicked.connect(self._on_regenerate)
        act_lay.addWidget(self._regen_btn)

        self._actions.setVisible(False)
        self._col.addWidget(self._actions, 0, Qt.AlignLeft)

        self._root.addWidget(col_container, 1)

    def _bubble_style(self):
        return (
            "background:" + COLORS['SURFACE'] + ";"
            "border:none;"
            "border-top-left-radius:" + RADII['bubble'] + ";"
            "border-top-right-radius:" + RADII['bubble'] + ";"
            "border-bottom-left-radius:" + RADII['tail'] + ";"
            "border-bottom-right-radius:" + RADII['bubble'] + ";"
        )

    def _label_style(self):
        return (
            "color:" + COLORS['TEXT'] + ";"
            "font-size:15px; font-weight:" + str(FONT_WEIGHTS['medium']) + "; line-height:1.65;"
            "font-family:" + FONT_STACK + ";"
            "background:transparent;"
        )

    def _action_btn_style(self):
        return (
            "QPushButton{background:transparent;border:none;border-radius:6px;"
            "color:" + COLORS['TEXT_3'] + ";font-size:12px;"
            "font-weight:" + str(FONT_WEIGHTS['medium']) + ";"
            "font-family:" + FONT_STACK + ";padding:4px 8px;}"
            "QPushButton:hover{background:" + COLORS['PRIM_LIGHT'] + ";"
            "color:" + COLORS['PRIM_DARK'] + ";}"
            "QPushButton:pressed{background:" + COLORS['PRIM'] + ";"
            "color:" + COLORS['TEXT_ON_ACCENT'] + ";}"
        )

    def _on_copy(self):
        text = self._content_buf.strip() if hasattr(self, '_content_buf') else ""
        if text:
            cb = QApplication.clipboard()
            if cb:
                cb.setText(text)
        # 复制成功反馈：图标变勾、文字变“已复制”，1.5 秒后恢复
        try:
            self._copy_btn.setIcon(_load_icon("check_18.png", 16))
            self._copy_btn.setText(tr("action_copied"))
            from PySide6.QtCore import QTimer
            QTimer.singleShot(1500, self._restore_copy_btn)
        except Exception:
            pass
        self.copy_clicked.emit()

    def _restore_copy_btn(self):
        """恢复复制按钮原始图标和文字。"""
        try:
            self._copy_btn.setIcon(_load_icon("copy_18.png", 16))
            self._copy_btn.setText(tr("action_copy"))
        except Exception:
            pass

    def _on_regenerate(self):
        self.regenerate_clicked.emit()

    def rebuild_style(self):
        """主题切换后重建样式。"""
        try:
            if hasattr(self, '_bubble'):
                self._bubble.setStyleSheet(self._bubble_style())
            if hasattr(self, '_content_lbl'):
                self._content_lbl.setStyleSheet(self._label_style())
            if hasattr(self, '_copy_btn'):
                self._copy_btn.setStyleSheet(self._action_btn_style())
            if hasattr(self, '_regen_btn'):
                self._regen_btn.setStyleSheet(self._action_btn_style())
        except Exception:
            pass

    def showEvent(self, event):
        """AI 气泡整体入场：向上滑入 + 淡入（弹窗外不加高斯模糊）。"""
        super().showEvent(event)
        if not getattr(self, '_entered', False):
            self._entered = True
            enter_anim(self, direction="up", blur=False)

    # ---------- 安全文本与渲染 ----------
    @staticmethod
    def _safe_text(text) -> str:
        """将任意输入降级为安全字符串；None/异常返回空字符串。"""
        if text is None:
            return ""
        try:
            return str(text)
        except Exception:
            return ""

    @staticmethod
    def _escape_html(s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    @staticmethod
    def _render_inline_code(escaped_text: str) -> str:
        """对已经过 HTML 转义的普通文本，统一渲染 `inline code` 样式。"""
        parts = escaped_text.split("`")
        if len(parts) < 3:
            return escaped_text
        out = []
        for i, part in enumerate(parts):
            if i % 2 == 0:
                out.append(part)
            else:
                out.append(
                    f'<code style="background:{COLORS["THINK_BGD"]};'
                    f'border:1px solid {COLORS["BORDER"]};border-radius:{RADII["xs"]};'
                    f'padding:1px 5px;font-size:12.5px;font-family:{FONT_CODE};'
                    f'color:{COLORS["PRIM_DARK"]};">{part}</code>'
                )
        return "".join(out)

    def _render_content(self) -> str:
        """解析代码块、行内代码并渲染为 HTML；任何异常都回退到安全纯文本。"""
        text = self._content_buf
        if not text:
            return ""
        try:
            if "```" not in text:
                return self._render_inline_code(self._escape_html(text).replace("\n", "<br>"))
            parts = text.split("```")
            html_parts = []
            for i, part in enumerate(parts):
                if i % 2 == 0:
                    if part:
                        html_parts.append(
                            self._render_inline_code(
                                self._escape_html(part).replace("\n", "<br>")
                            )
                        )
                    continue
                # 代码块：第一行可选语言，其余为代码
                lines = part.split("\n", 1)
                code = lines[1] if len(lines) > 1 else ""
                html_parts.append(
                    f'<pre style="margin:6px 0;padding:12px 14px;background:{COLORS["CODE_BGD"]};'
                    f'border:1px solid {COLORS["BORDER"]};border-radius:{RADII["code"]};'
                    f'font-family:{FONT_CODE};font-size:12.5px;line-height:1.6;'
                    f'color:{COLORS["TEXT"]};white-space:pre-wrap;'
                    f'overflow-wrap:anywhere;">'
                    f'<code>{self._escape_html(code)}</code></pre>'
                )
            return "".join(html_parts)
        except Exception:
            # 解析失败时降级为安全纯文本，并在头部添加用户可见提示
            hint = tr("render_fallback_hint")
            return f'<span style="color:{COLORS["TEXT_3"]};font-size:12px;">{hint}</span><br>' + self._escape_html(text).replace("\n", "<br>")

    # ---------- 流式内容 ----------
    def append_content(self, delta):
        delta = self._safe_text(delta)
        if not delta:
            return
        self._content_buf += delta
        self._content_lbl.setText(self._content_buf)
        # 强制重新计算布局链，确保气泡尺寸跟随内容增长
        self._content_lbl.adjustSize()
        self._bubble.adjustSize()
        self.adjustSize()
        try:
            self.content_changed.emit()
        except Exception:
            pass

    def set_content(self, text):
        self._content_buf = self._safe_text(text)
        self._content_lbl.setText(self._content_buf or " ")
        # 直接设置完整内容时，若内容非空则显示操作按钮
        try:
            if hasattr(self, '_actions') and bool(self._content_buf.strip()):
                self._actions.setVisible(True)
        except Exception:
            pass
        try:
            self.content_changed.emit()
        except Exception:
            pass

    # ---------- 推理块 ----------
    def append_reasoning(self, delta: str):
        self._reasoning.append_reasoning(delta)

    def set_reasoning(self, text: str):
        self._reasoning.set_reasoning(text)

    def set_token_stats(self, input_tokens: int = 0, output_tokens: int = 0, reasoning_tokens: int = 0):
        self._reasoning.set_token_stats(input_tokens, output_tokens, reasoning_tokens)

    def append_tool_status(self, status_type: str, text: str):
        self._reasoning.append_tool_status(status_type, text)

    def set_mode(self, mode: str):
        m = mode or "idle"
        self._whale_icon.set_mode(m)
        self._reasoning.set_mode(m)

    def mark_reasoning_done(self):
        self._whale_icon.set_mode("idle")
        self._reasoning.mark_done()

    def finalize(self) -> None:
        """本轮 AI 对话完全结束。整体折叠推理区，并显示操作按钮。"""
        self._whale_icon.set_mode("idle")
        self._reasoning.mark_done()
        # 内容非空时显示复制/重新生成按钮
        try:
            if hasattr(self, '_actions') and bool(self._content_buf.strip()):
                self._actions.setVisible(True)
        except Exception:
            pass

    def get_reasoning(self):
        return self._reasoning
