# -*- coding: utf-8 -*-
"""推理气泡 ReasoningBubble、呼吸小点 _BreathingDot 与打字指示器 TypingIndicator。"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, Signal, QTimer, QObject, QSize, Property, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QPushButton, QSizePolicy,
)

from src.gui.style import RADII, COLORS, FONT_STACK, FONT_CODE, STATUS_COLORS, FONT_WEIGHTS
from src.gui._gui_helpers import _load_icon
from src.gui.animations import fade_in, slide_in
from src.gui.bubbles.animation_icons import AnimatedWhaleIcon, ToolAnimationIcon
from src.gui.bubbles._helpers import _try_pm

# ======================================================================
# ReasoningBubble —— 两层折叠推理+工具卡片
#   - 思考中: 默认展开 CoT, 工具调用展开; header 显示 thinking N tokens
#   - 思考结束: 停止冒泡并把内容折叠为单行 Thinking（带展开按钮）
#   - 用户点击 header: 切换整包展开/折叠
# ======================================================================
class ReasoningBubble(QFrame):
    """两层折叠推理+工具卡片。"""

    toggled = Signal(bool)
    content_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thinking = True
        self._user_interacted_overall = False
        self._reasoning_expanded = False
        self._overall_expanded = True
        self._token_count = 0
        self._call_count = 0
        self._reasoning_text = ""
        self._tool_lines: List[Tuple[str, str]] = []
        self._tool_rows: List[QFrame] = []
        self._last_tool_texts: List[str] = []
        self._is_sub_agent = False
        # 输入 / 输出 / 推理 token 统计（优先由外部传入，否则按字符数回退）
        self._input_tokens = 0
        self._output_tokens = 0
        self._reasoning_tokens = 0
        self._displayed_tokens = 0  # 当前显示的 token 数字（用于动画插值）
        self._token_bridge: Optional[QObject] = None
        self.setStyleSheet("background:transparent;")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- Header Bar（整行可点击）----
        self._header_bar = QFrame()
        self._header_bar.setCursor(Qt.PointingHandCursor)
        self._header_bar.setStyleSheet(
            "QFrame{background:transparent;border-radius:" + RADII['sm'] + ";}"
            "QFrame:hover{background:" + COLORS['PRIM_LIGHT'] + ";}"
        )
        hl = QHBoxLayout(self._header_bar)
        hl.setContentsMargins(0, 6, 0, 6)
        hl.setSpacing(10)
        hl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        # 无独立图标——图标已在 AIBubble 中显示，这里保持紧凑
        hl.addSpacing(4)

        self._title_label = QLabel()
        self._title_label.setWordWrap(False)
        self._title_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self._title_label.setStyleSheet(
            "QLabel{color:" + COLORS['TEXT_3'] + ";font-size:13px;"
            "font-family:" + FONT_STACK + ";background:transparent;}"
        )
        hl.addWidget(self._title_label, 1, Qt.AlignVCenter | Qt.AlignLeft)

        # 箭头按钮
        self._arrow_btn = QPushButton()
        self._arrow_btn.setFixedSize(24, 24)
        self._arrow_btn.setCursor(Qt.PointingHandCursor)
        self._arrow_btn.setIcon(_load_icon("chevron_down_18.png", 14))
        self._arrow_btn.setIconSize(QSize(14, 14))
        self._arrow_btn.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:" + RADII['xs'] + ";}"
            "QPushButton:hover{background:" + COLORS['PRIM_LIGHT'] + ";}"
        )
        self._arrow_btn.clicked.connect(self._on_header_clicked)
        hl.addWidget(self._arrow_btn, 0, Qt.AlignTop)

        root.addWidget(self._header_bar)

        # 安装事件过滤器，让 header_bar 整行点击
        self._header_bar.installEventFilter(self)

        # ---- Content Area（透明背景，无卡片，紧贴文字）----
        self._content_area = QFrame()
        self._content_area.setStyleSheet("background:transparent;border:none;")
        cl = QVBoxLayout(self._content_area)
        # 内容区左边缘与 header 标题对齐（36px 图标 + 10px 间距）
        cl.setContentsMargins(46, 2, 0, 4)
        cl.setSpacing(4)

        # CoT 区（单独折叠，展开时显示浅灰背景）
        self._cot_frame = QFrame()
        self._cot_frame.setCursor(Qt.PointingHandCursor)
        self._cot_frame.setStyleSheet(
            "background:" + COLORS['THINK_BGD'] + ";"
            "border-radius:" + RADII['xs'] + ";"
        )
        cotl = QVBoxLayout(self._cot_frame)
        cotl.setContentsMargins(10, 8, 10, 8)
        cotl.setSpacing(4)

        self._cot_header = QLabel(
            '<span style="color:' + COLORS['TEXT_3']
            + ';font-size:12px;font-weight:' + str(FONT_WEIGHTS['semibold']) + ';">推理过程</span>'
        )
        self._cot_header.setStyleSheet("background:transparent;")
        cotl.addWidget(self._cot_header)

        self._reasoning_label = QLabel("")
        self._reasoning_label.setWordWrap(True)
        self._reasoning_label.setTextFormat(Qt.PlainText)
        self._reasoning_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._reasoning_label.setStyleSheet(
            "color:" + COLORS['TEXT_2'] + ";"
            "font-size:13px;line-height:1.65;font-family:" + FONT_STACK + ";"
            "background:transparent;"
        )
        cotl.addWidget(self._reasoning_label)
        cl.addWidget(self._cot_frame)
        self._cot_frame.installEventFilter(self)

        # 工具区（透明背景，每行带一个动画图标）
        self._tool_frame = QFrame()
        self._tool_frame.setStyleSheet("background:transparent;")
        self._tool_rows_layout = QVBoxLayout(self._tool_frame)
        self._tool_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._tool_rows_layout.setSpacing(4)
        self._tool_rows_layout.setAlignment(Qt.AlignTop)
        cl.addWidget(self._tool_frame)

        root.addWidget(self._content_area)

        # 默认：CoT 折叠，工具+整体展开；思考中收到内容后会自动展开 CoT
        self._cot_frame.setMaximumHeight(0)
        self._cot_frame.setVisible(False)
        self._reasoning_label.setVisible(False)

        # 初始标题
        self._refresh_title()

    # ---- 事件过滤：header 整行 / CoT 整行点击 ----
    def eventFilter(self, obj, event):
        try:
            header = self._header_bar
            cot = self._cot_frame
        except AttributeError:
            return super().eventFilter(obj, event)
        if obj is header:
            if event.type() == event.Type.MouseButtonRelease:
                self._on_header_clicked()
                return True
        elif obj is cot:
            if event.type() == event.Type.MouseButtonRelease:
                self._toggle_cot()
                return True
        return super().eventFilter(obj, event)

    # ---- Token 格式化 ----
    @staticmethod
    def _fmt_tokens(n: int) -> str:
        if n < 1000:
            return str(n)
        return f"{n / 1000:.1f}k"

    def _refresh_title(self):
        """刷新标题（token 数字用 QPropertyAnimation + OutCubic 滚动）。"""
        target = self._token_count

        if target <= self._displayed_tokens:
            self._displayed_tokens = target
            self._render_title(target)
            self._stop_token_anim()
            return

        self._stop_token_anim()

        start_val = float(self._displayed_tokens)
        end_val = float(target)

        class _TokenBridge(QObject):
            def __init__(self, bubble):
                super().__init__(bubble)
                self._bubble = bubble
                self._value = start_val

            def _get_value(self):
                return self._value

            def _set_value(self, v):
                self._value = v
                self._bubble._displayed_tokens = int(v)
                self._bubble._render_title(int(v))

            value = Property(float, _get_value, _set_value)

        self._token_bridge = _TokenBridge(self)
        self._token_anim = QPropertyAnimation(self._token_bridge, b"value", self)
        self._token_anim.setDuration(400)
        self._token_anim.setStartValue(start_val)
        self._token_anim.setEndValue(end_val)
        self._token_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._token_anim.start(QPropertyAnimation.DeleteWhenStopped)

    def _stop_token_anim(self):
        if self._token_anim is not None:
            try:
                self._token_anim.stop()
            except Exception:
                pass
            self._token_anim = None
        if self._token_bridge is not None:
            try:
                self._token_bridge.deleteLater()
            except Exception:
                pass
            self._token_bridge = None

    def _render_title(self, tokens: int):
        """按 tokens 值渲染 HTML 标题文字；同时展示输入/输出/推理 token 数。"""
        tok = self._fmt_tokens(int(tokens))
        in_t = self._fmt_tokens(int(self._input_tokens))
        out_t = self._fmt_tokens(int(self._output_tokens))
        reason_t = self._fmt_tokens(int(self._reasoning_tokens))
        if self._thinking:
            self._title_label.setText(
                '<span style="color:' + COLORS['TEXT_3'] + ';">thinking </span>'
                '<span style="color:' + COLORS['PRIM_DARK']
                + ';font-weight:' + str(FONT_WEIGHTS['bold']) + ';font-size:14px;">' + tok + '</span>'
                '<span style="color:' + COLORS['TEXT_3'] + ';"> tokens · in '
                + in_t + ' / out ' + out_t + ' / reason ' + reason_t + '</span>'
            )
        else:
            # 思考完成后折叠成单行 Thinking（带展开按钮），同时保留 tokens 与调用次数
            self._title_label.setText(
                '<span style="color:' + COLORS['TEXT_3']
                + ';font-weight:' + str(FONT_WEIGHTS['semibold']) + ';">Thinking · </span>'
                '<span style="color:' + COLORS['PRIM_DARK']
                + ';font-weight:' + str(FONT_WEIGHTS['bold']) + ';font-size:14px;">' + tok + '</span>'
                '<span style="color:' + COLORS['TEXT_3'] + ';"> tokens · in '
                + in_t + ' / out ' + out_t + ' / reason ' + reason_t
                + ' · ' + str(self._call_count) + ' 调用</span>'
            )

    # ---- CoT 折叠/展开 ----
    def _toggle_cot(self):
        if getattr(self, '_cot_animating', False):
            return  # 动画进行中忽略点击，避免状态竞态
        if self._reasoning_expanded:
            self._collapse_cot(animate=True)
        else:
            self._expand_cot(animate=True)

    def _expand_cot(self, animate: bool = True):
        if self._reasoning_expanded:
            return
        self._reasoning_expanded = True
        self._cot_header.setText(
            '<span style="color:' + COLORS['TEXT_3']
            + ';font-size:12px;font-weight:' + str(FONT_WEIGHTS['semibold']) + ';">推理过程</span>'
        )
        if not animate:
            self._cot_frame.setVisible(True)
            self._reasoning_label.setVisible(True)
            self._cot_frame.setMaximumHeight(16777215)
            self._cot_animating = False
            return
        self._cot_animating = True
        self._cot_frame.setVisible(True)
        self._reasoning_label.setVisible(True)
        self._cot_frame.setMaximumHeight(16777215)
        self._cot_frame.adjustSize()
        end_h = self._cot_frame.sizeHint().height() or 1
        start_h = 1 if self._cot_frame.height() < 2 else self._cot_frame.height()
        self._animate_height(self._cot_frame, start_h, end_h,
                             duration_ms=320,
                             on_finish=lambda w=self._cot_frame: self._on_cot_expanded(w))

    def _collapse_cot(self, animate: bool = True):
        if not self._reasoning_expanded:
            return
        self._reasoning_expanded = False
        self._cot_header.setText(
            '<span style="color:' + COLORS['TEXT_3']
            + ';font-size:12px;font-weight:' + str(FONT_WEIGHTS['semibold']) + ';">推理过程</span>'
        )
        if not animate:
            self._on_cot_collapsed(self._cot_frame)
            return
        self._cot_animating = True
        start_h = self._cot_frame.height() or 1
        self._animate_height(self._cot_frame, start_h, 0,
                             duration_ms=280,
                             on_finish=lambda w=self._cot_frame: self._on_cot_collapsed(w))

    def _on_cot_collapsed(self, widget: QFrame):
        widget.setVisible(False)
        widget.setMaximumHeight(0)
        self._cot_animating = False

    def _on_cot_expanded(self, widget: QFrame):
        # 关键：释放 maximumHeight，让后续 append_reasoning 时能继续增长
        widget.setMaximumHeight(16777215)
        self._cot_animating = False

    # ---- Header 点击 / 整体折叠/展开 ----
    def _on_header_clicked(self):
        if getattr(self, '_overall_animating', False):
            return  # 动画进行中忽略点击
        self._user_interacted_overall = True
        self._toggle_overall()

    def _toggle_overall(self):
        if self._overall_expanded:
            start_h = self._content_area.height() or 1
            self._overall_expanded = False
            self._overall_animating = True
            self._arrow_btn.setIcon(_load_icon("chevron_right_18.png", 14))
            self._arrow_btn.setIconSize(QSize(14, 14))
            self.toggled.emit(False)
            self._animate_height(self._content_area, start_h, 0,
                                 duration_ms=280,
                                 on_finish=lambda w=self._content_area: self._on_overall_collapsed(w))
        else:
            self._overall_expanded = True
            self._overall_animating = True
            self._content_area.setVisible(True)
            self._content_area.setMaximumHeight(16777215)
            self._content_area.adjustSize()
            end_h = self._content_area.sizeHint().height() or 1
            start_h = 1 if self._content_area.height() < 2 else self._content_area.height()
            self._arrow_btn.setIcon(_load_icon("chevron_down_18.png", 14))
            self._arrow_btn.setIconSize(QSize(14, 14))
            self.toggled.emit(True)
            self._animate_height(self._content_area, start_h, end_h,
                                 duration_ms=320,
                                 on_finish=lambda w=self._content_area: self._on_overall_expanded(w))

    def _on_overall_collapsed(self, widget: QFrame):
        widget.setVisible(False)
        widget.setMaximumHeight(0)
        self._overall_animating = False

    def _on_overall_expanded(self, widget: QFrame):
        # 释放 maximumHeight，允许内容自动增长
        widget.setMaximumHeight(16777215)
        self._overall_animating = False

    # ---- 通用高度动画 ----
    def _animate_height(self, widget, start_h, end_h, duration_ms=320, on_finish=None):
        # 动画已禁用——直接即时切换
        widget.setMaximumHeight(16777215 if end_h > 0 else 0)
        widget.setVisible(end_h > 0)
        if on_finish:
            try:
                on_finish()
            except Exception:
                pass
        self._animating = False

    # ---- API: 追加推理文字 ----
    def append_reasoning(self, text: str):
        if not text:
            return
        self._reasoning_text += text
        if len(self._reasoning_text) > 20000:
            self._reasoning_text = self._reasoning_text[-20000:]
        self._reasoning_label.setText(self._reasoning_text)
        # 以字符数近似推理 token 数量
        self._reasoning_tokens = len(self._reasoning_text)
        self._token_count = self._input_tokens + self._output_tokens + self._reasoning_tokens
        # 思考中默认展开 CoT，方便用户实时查看推理链
        if self._thinking and not self._reasoning_expanded:
            self._expand_cot(animate=True)
        self._refresh_title()
        self.content_changed.emit()

    def set_reasoning(self, text: str):
        self._reasoning_text = text or ""
        self._reasoning_label.setText(self._reasoning_text)
        self._reasoning_tokens = len(self._reasoning_text)
        self._token_count = self._input_tokens + self._output_tokens + self._reasoning_tokens
        self._refresh_title()
        self.content_changed.emit()

    def set_token_stats(self, input_tokens: int = 0, output_tokens: int = 0, reasoning_tokens: int = 0):
        """外部传入精确的输入/输出/推理 token 数；传入 -1 表示不更新该项。"""
        if input_tokens >= 0:
            self._input_tokens = int(input_tokens)
        if output_tokens >= 0:
            self._output_tokens = int(output_tokens)
        if reasoning_tokens >= 0:
            self._reasoning_tokens = int(reasoning_tokens)
        self._token_count = self._input_tokens + self._output_tokens + self._reasoning_tokens
        self._refresh_title()

    # ---- API: 追加工具状态 ----
    def append_tool_status(self, status_type: str, text: str):
        if not text:
            return
        status_type = (status_type or "code").lower()
        self._tool_lines.append((status_type, str(text)))
        self._call_count += 1
        if len(self._tool_lines) > 80:
            self._tool_lines = self._tool_lines[-80:]
        self._rerender_tool_status()
        # icon removed from ReasoningBubble — AIBubble._whale_icon handles animation
        self._refresh_title()
        self.content_changed.emit()

    def set_mode(self, mode: str):
        pass  # AIBubble._whale_icon handles animation

    @staticmethod
    def _tool_mode_for_line(status_type: str, text: str) -> str:
        """根据状态类型和文本内容选择对应的工具动画模式。"""
        s = (status_type or "").lower()
        t = (text or "").lower()
        if s == "done":
            return "done"
        if "search" in s or "codebase_search" in t or "search" in t:
            return "search"
        if s in ("read", "reading", "fetch") or "read_file" in t:
            return "read"
        if s in ("write", "writing", "edit", "coding") or "write_file" in t or "edit_file" in t:
            return "write"
        if s in ("run", "terminal", "command") or "run_command" in t:
            return "run"
        return "gear"

    def _rerender_tool_status(self):
        from src.gui.style import STATUS_COLORS
        color_map = STATUS_COLORS
        prefix_map = {
            "coding": "[CODE] ",
            "reading": "[READ] ",
            "writing": "[WRITE] ",
            "done": "[DONE] ",
            "error": "[ERR] ",
            "terminal": "[RUN] ",
            "think": "[THINK] ",
            "thinking": "[THINK] ",
        }
        lines = self._tool_lines[-40:]
        # 复用或创建行 widget（每行是一个 QFrame，便于整体做入场动画）
        while len(self._tool_rows) < len(lines):
            row = QFrame()
            row.setStyleSheet("background:transparent;")
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(0, 0, 0, 0)
            row_lay.setSpacing(8)
            row_lay.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            icon = ToolAnimationIcon("gear", 18)
            icon.setFixedSize(18, 18)
            row_lay.addWidget(icon, 0, Qt.AlignVCenter)
            lbl = QLabel("")
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lbl.setStyleSheet(
                "color:" + COLORS['TEXT'] + ";"
                "font-family:" + FONT_CODE + ";"
                "font-size:13px;line-height:1.6;background:transparent;"
            )
            row_lay.addWidget(lbl, 1)
            # 方便后续访问
            row.icon = icon
            row.lbl = lbl
            self._tool_rows_layout.addWidget(row)
            self._tool_rows.append(row)
        # 隐藏多余的行
        for i, row in enumerate(self._tool_rows):
            if i < len(lines):
                row.setVisible(True)
            else:
                row.setVisible(False)
                row.icon.set_mode("gear")
        # 更新可见行的内容与动画
        for i, (stype, s) in enumerate(lines):
            row = self._tool_rows[i]
            mode = self._tool_mode_for_line(stype, s)
            row.icon.set_mode(mode)
            color = color_map.get(stype, COLORS['TEXT_3'])
            prefix = prefix_map.get(stype, "[INFO] ")
            safe = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            old_text = self._last_tool_texts[i] if i < len(self._last_tool_texts) else ""
            changed = old_text != s
            row.lbl.setText(
                '<span style="color:' + color + ';font-weight:' + str(FONT_WEIGHTS['semibold']) + ';">' + prefix + '</span>'
                + '<span style="color:' + COLORS['TEXT'] + ';">' + safe + '</span>'
            )
            if changed:
                self._animate_tool_row(row)
        self._last_tool_texts = [s for _, s in lines]

    def _animate_tool_row(self, row: QFrame):
        """新增/更新工具状态行：从右侧滑入 + 淡入。"""
        def _run():
            try:
                if not row or not row.isVisible():
                    return
            except Exception:
                return
            slide_in(row, direction="right", duration_ms=180, distance_px=30)
            fade_in(row, duration_ms=180)
        QTimer.singleShot(0, _run)

    # ---- API: 完成 ----
    def mark_done(self):
        """推理结束，停止鲸鱼冒泡动画，把 CoT 折叠为单行 Thinking（带展开按钮）。"""
        self._thinking = False
        # AIBubble._whale_icon.set_mode("idle") is called from AIBubble.finalize()
        # 工具调用全部显示完成勾选
        for row in self._tool_rows:
            row.icon.set_mode("done")

        is_sub = getattr(self, '_is_sub_agent', False)
        # 子代理气泡必须保持完整展开状态（包括 CoT 与整体内容区）
        if is_sub:
            if not self._reasoning_expanded:
                self._expand_cot(animate=False)
            if not self._overall_expanded:
                self.expand(animate=False)
            self._refresh_title()
            return

        # 折叠 CoT 但保留 header 可见可点击
        self._collapse_cot(animate=False)
        # 不再自动折叠整体内容区——用户可点击 header 随时查看/收起
        self._refresh_title()

    # ---- 公开 API: 折叠/展开（供外部调用，main_window 等）----
    def collapse(self, animate: bool = True):
        """外部强制折叠。用户没点击过时，不影响_user_interacted_overall。"""
        if self._overall_expanded:
            if animate:
                # 直接走内部动画逻辑
                if getattr(self, '_overall_animating', False):
                    return
                self._toggle_overall()
            else:
                # 瞬时折叠（无动画）
                self._overall_expanded = False
                self._arrow_btn.setIcon(_load_icon("chevron_right_18.png", 14))
                self._arrow_btn.setIconSize(QSize(14, 14))
                self.toggled.emit(False)
                self._content_area.setVisible(False)
                self._content_area.setMaximumHeight(0)
                self._overall_animating = False

    def expand(self, animate: bool = True):
        """外部强制展开。"""
        if not self._overall_expanded:
            if animate:
                if getattr(self, '_overall_animating', False):
                    return
                self._toggle_overall()
            else:
                # 瞬时展开（无动画）
                self._overall_expanded = True
                self._arrow_btn.setIcon(_load_icon("chevron_down_18.png", 14))
                self._arrow_btn.setIconSize(QSize(14, 14))
                self.toggled.emit(True)
                self._content_area.setVisible(True)
                self._content_area.setMaximumHeight(16777215)
                self._overall_animating = False

    # ---- 查询状态 ----
    def is_expanded(self) -> bool:
        return self._overall_expanded

    def has_content(self) -> bool:
        return bool(self._reasoning_text or self._tool_lines)

    # ---- 子代理标记：保持展开状态
    # ---- 子代理专用标记：子代理默认展开，而且保持展开（不被自动折叠）
    def mark_as_sub_agent(self):
        """将本气泡标记为子代理输出，永远不被自动折叠，并且永远保持展开。"""
        self._is_sub_agent = True
        self._user_interacted_overall = True  # 防止主对话插入时不被折叠

# ======================================================================
# _BreathingDot — QPropertyAnimation 驱动的呼吸小点
# ======================================================================
class _BreathingDot:
    """用样式表颜色切换实现呼吸效果，避免 QGraphicsOpacityEffect 冲突。"""

    def __init__(self, dot: QFrame, cycle_ms: int = 800, start_high: bool = True):
        self.dot = dot
        self._forward = start_high
        self._timer = QTimer(dot)
        self._timer.setInterval(cycle_ms // 2)
        self._timer.timeout.connect(self._next)
        self._next()
        self._timer.start()

    def _next(self):
        c = COLORS['TEXT_3'] if self._forward else COLORS['BORDER']
        self.dot.setStyleSheet(f"background:{c};border-radius:3px;")
        self._forward = not self._forward

    def stop(self):
        try:
            self._timer.stop()
        except Exception:
            pass

# ======================================================================
# TypingIndicator — 打字指示器 (三个呼吸小圆点)
# ======================================================================
class TypingIndicator(QFrame):
    def __init__(self, parent=None, language: str = "zh"):
        super().__init__(parent)
        self.setStyleSheet("background:transparent;")
        self._breathing = []
        self._lang = language

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        # 与 AIBubble 的鲸鱼/气泡间距保持一致（40px 图标 + 12px 间距）
        root.setSpacing(12)

        whale = QLabel()
        whale.setPixmap(_try_pm(["whale_40.png", "whale_36.png", "whale_48.png", "whale_32.png", "whale.png"], 40))
        whale.setAlignment(Qt.AlignCenter)
        whale.setFixedSize(40, 40)
        whale.setStyleSheet("background:transparent;")
        root.addWidget(whale, 0, Qt.AlignTop)

        card = QFrame()
        card.setStyleSheet(
            "background:transparent; border:none;"
        )
        card.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)

        cl = QHBoxLayout(card)
        # 左内边距与 AIBubble 主体气泡左 padding 对齐，保证文字起点一致
        cl.setContentsMargins(16, 8, 16, 8)
        cl.setSpacing(8)

        # 文字: 正在回复…（多语言）
        self._text_lbl = QLabel(self._label_text(self._lang))
        self._text_lbl.setStyleSheet(
            "color:" + COLORS['TEXT_3'] + ";"
            "font-size:13px; font-weight:" + str(FONT_WEIGHTS['medium']) + "; font-family:" + FONT_STACK + ";"
            "background:transparent;"
        )
        cl.addWidget(self._text_lbl)

        self.dots = []
        for _ in range(3):
            dot = QFrame()
            dot.setFixedSize(8, 8)
            dot.setStyleSheet(
                "background:" + COLORS['PRIM_DARK'] + ";border-radius:" + RADII['tail'] + ";")
            cl.addWidget(dot)
            self.dots.append(dot)

        root.addWidget(card, 0, Qt.AlignLeft)

        # 用 QPropertyAnimation 呼吸效果替代 QTimer 逐帧刷新
        for i, dot in enumerate(self.dots):
            bd = _BreathingDot(dot, cycle_ms=960, start_high=(i % 2 == 0))
            self._breathing.append(bd)

    @staticmethod
    def _label_text(language: str) -> str:
        if language == "en":
            return "Thinking…"
        if language == "wenyan":
            return "思之…"
        return "正在回复…"

    def set_language(self, language: str):
        self._lang = language
        self._text_lbl.setText(self._label_text(language))

    def stop(self):
        for bd in self._breathing:
            bd.stop()
        self._breathing.clear()

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)
