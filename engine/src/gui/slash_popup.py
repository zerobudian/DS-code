# -*- coding: utf-8 -*-
"""斜杠命令 (Slash Commands) 选择器弹窗。

当用户在输入框中键入 "/" 时触发，显示一个可搜索、可键盘导航
的命令列表。用户点击或按下 Enter 后发送 item_selected 信号，
信号参数为命令名称（不含斜杠 "/"）。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import (
    Property,
    QPropertyAnimation,
    QEasingCurve,
    QPoint,
    QRect,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.gui.style import COLORS, FONT_STACK, RADII
from src.gui.icon_factory import get_icon
from src.gui.animations import pop_in, pop_out, fade_in, fade_out, HoverCard


# ============================================================================
# 设计色值 — 统一引用 style.py COLORS（与主色系统完全一致）
# ============================================================================

_SP_PRIM = COLORS["PRIM_DARK"]          # 主色（深蓝紫）
_SP_PRIM_TEXT = COLORS["PRIM_DARK"]           # 选中项文字色（与主色一致，hover 可通过降低饱和度区分）
_SP_SELECTED_BGD = COLORS["PRIM_LIGHT"]     # 选中/悬停浅蓝背景
_SP_ICON_BGD = COLORS["PRIM_LIGHT"]     # 图标容器底色
_SP_ICON_BGD_SELECTED = _SP_PRIM           # 选中态图标容器底色


# ============================================================================
# 命令列表（与设计要求一致）。图标使用 ASCII / Unicode 字符，
# 由 SlashItemFrame 统一渲染为 22x22 圆形标签，颜色 PRIM_DARK。
# ============================================================================

SLASH_COMMANDS: List[Tuple[str, str, str]] = [
        # (命令名, 描述, 图标字符) —— 使用 ASCII/Unicode，保证跨平台一致
        ("plan",          "规划模式：先规划再执行",      "P"),
        ("ultra",         "DS Code Ultra：规划验证交付", "U"),
        ("memory_add",    "打开记忆面板添加",            "+"),
        ("memory_search", "在记忆中搜索",                "?"),
        ("new",           "创建新项目",                  "N"),
        ("import",        "导入对话",                    "↑"),
        ("export",        "导出当前对话",                "↓"),
        ("theme",         "切换主题 (浅/深)",            "icon:sun"),
        ("clear",         "清空对话",                    "×"),
        ("settings",      "打开设置面板",                "S"),
        ("stop",          "停止当前对话",                "■"),
    ]


# ============================================================================
# 单项 Frame：图标 + 命令 + 描述，支持悬停/选中高亮
# ============================================================================

class SlashItemFrame(HoverCard):
    """单个斜杠命令条目（带 hover/selected 背景色动画与图标微缩放）。"""

    def __init__(self, command: str, description: str, icon: str,
                 parent: Optional[QWidget] = None):
        super().__init__(
            normal_bg="transparent",
            hover_bg=_SP_SELECTED_BGD,
            selected_bg=_SP_SELECTED_BGD,
            border_color="transparent",
            border_radius=RADII['md'],
            parent=parent,
        )
        self.command = command
        self.description = description
        self.icon_char = icon
        self._icon_size = 22
        self._selected = False

        self.setObjectName("SlashItemFrame")
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(40)
        self.setContentsMargins(8, 2, 8, 2)
        self.setAttribute(Qt.WA_Hover, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 12, 4)
        layout.setSpacing(12)

        # 统一风格图标：22x22 圆角方块 + 深蓝字 / 选中态反白
        self.icon_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.icon_label.setFixedSize(22, 22)
        if self.icon_char.startswith("icon:"):
            pm = get_icon(self.icon_char.split(":", 1)[1], 18)
            if not pm.isNull():
                self.icon_label.setPixmap(pm)
        else:
            self.icon_label.setText(self.icon_char)
            icon_font = QFont()
            icon_font.setFamilies([FONT_STACK])
            icon_font.setPointSize(10)
            icon_font.setWeight(QFont.Bold)
            self.icon_label.setFont(icon_font)
        layout.addWidget(self.icon_label)

        self.cmd_label = QLabel(f"/{command}")
        cmd_font = QFont()
        cmd_font.setFamilies([FONT_STACK])
        cmd_font.setPointSize(10)
        cmd_font.setWeight(QFont.DemiBold)
        self.cmd_label.setFont(cmd_font)
        self.cmd_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self.cmd_label)

        # 弹簧
        layout.addStretch(1)

        self.desc_label = QLabel(description)
        desc_font = QFont()
        desc_font.setFamilies([FONT_STACK])
        desc_font.setPointSize(9)
        self.desc_label.setFont(desc_font)
        self.desc_label.setStyleSheet(f"color: {COLORS['TEXT_3']};")
        self.desc_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.desc_label)

        self._icon_anim = QPropertyAnimation(self, b"_icon_size_prop", self)
        self._icon_anim.setDuration(150)
        self._icon_anim.setEasingCurve(QEasingCurve.OutBack)

        self._update_look()

    def _get_icon_size(self):
        return self._icon_size

    def _set_icon_size(self, value: int):
        self._icon_size = value
        self.icon_label.setFixedSize(value, value)
        # 统一图标圆角为 6px（22x22 小图标使用）
        self.icon_label.setStyleSheet(
            f"background: {self._icon_bg()}; color: {self._icon_fg()};"
            f" border-radius: 6px;"
        )

    _icon_size_prop = Property(int, _get_icon_size, _set_icon_size)

    def set_selected(self, value: bool) -> None:
        self._selected = bool(value)
        super().set_selected(self._selected)
        self._update_look()

    def is_selected(self) -> bool:
        return self._selected

    def _icon_bg(self):
        return _SP_PRIM if self._selected else _SP_ICON_BGD

    def _icon_fg(self):
        return COLORS['CARD'] if self._selected else _SP_PRIM

    def _update_look(self) -> None:
        if self._selected:
            border = _SP_PRIM
            text_color = _SP_PRIM_TEXT
        elif self._hovered:
            border = "transparent"
            text_color = COLORS['TEXT']
        else:
            border = "transparent"
            text_color = COLORS['TEXT']

        # 背景色由 HoverCard 的动画属性驱动；这里同步边框与文字色
        self._border_color = border
        self._apply_style()
        self.cmd_label.setStyleSheet(f"color: {text_color};")
        self.icon_label.setStyleSheet(
            f"background: {self._icon_bg()}; color: {self._icon_fg()};"
            f" border-radius: 6px;"
        )

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._update_look()
        self._animate_icon(24)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._update_look()
        self._animate_icon(22)

    def _animate_icon(self, target: int) -> None:
        self._icon_anim.stop()
        self._icon_anim.setStartValue(self._icon_size)
        self._icon_anim.setEndValue(target)
        self._icon_anim.start(QPropertyAnimation.KeepWhenStopped)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            # 向上传递到 popup
            popup = self._find_popup()
            if popup is not None:
                popup._select_item(self)
        super().mousePressEvent(event)

    def _find_popup(self) -> Optional["SlashPopup"]:
        parent = self.parent()
        while parent is not None:
            if isinstance(parent, SlashPopup):
                return parent
            parent = parent.parent() if hasattr(parent, "parent") else None
        return None


# ============================================================================
# 主 Popup
# ============================================================================

class SlashPopup(QFrame):
    """斜杠命令选择器弹窗。

    对外信号:
        item_selected(str): 当用户选择某个命令后发出，
                            参数为命令名（不含 "/"），例如 "plan"。
    """

    item_selected = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.StrongFocus)

        self._items: List[SlashItemFrame] = []
        self._visible_items: List[SlashItemFrame] = []
        self._selected_index: int = 0
        self._filter_text: str = ""
        self._is_closing = False
        self._items_animated = False

        # 固定尺寸（内容会随滚动自适应）
        self.setFixedWidth(380)
        self.setMaximumHeight(360)
        self.setMinimumHeight(180)

        self._build_ui()
        self._populate_items()
        self._apply_base_style()
        # 默认选中第一项
        self._set_selected_index(0)

    # ------------------------------------------------------------------
    # UI 结构
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(8)

        # 标题
        self.title_label = QLabel("可用的斜杠命令 · Slash Commands")
        title_font = QFont()
        title_font.setFamilies([FONT_STACK])
        title_font.setPointSize(10)
        title_font.setWeight(QFont.Bold)
        self.title_label.setFont(title_font)
        self.title_label.setStyleSheet(f"color: {COLORS['TEXT']};")
        self.title_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        root.addWidget(self.title_label)

        # 分隔线
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color: {COLORS['BORDER']}; background: {COLORS['BORDER']};")
        line.setFixedHeight(1)
        root.addWidget(line)

        # 滚动区
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setStyleSheet(
            f"QScrollArea {{ background: transparent; border: none; }}"
            f"QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}"
            f"QScrollBar::handle:vertical {{ background: {COLORS['TEXT_5']};"
            f" border-radius: 4px; min-height: 24px; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}"
        )

        self.content_widget = QWidget()
        self.content_widget.setStyleSheet("background: transparent;")

        self.item_layout = QVBoxLayout(self.content_widget)
        self.item_layout.setContentsMargins(0, 0, 0, 0)
        self.item_layout.setSpacing(4)
        self.item_layout.addStretch(1)

        self.scroll_area.setWidget(self.content_widget)
        root.addWidget(self.scroll_area, stretch=1)

        # 底部提示
        self.filter_info_label = QLabel("\u2191\u2193 选择 \u00b7 Enter 确认 \u00b7 Esc 关闭")
        info_font = QFont()
        info_font.setFamilies([FONT_STACK])
        info_font.setPointSize(9)
        self.filter_info_label.setFont(info_font)
        self.filter_info_label.setStyleSheet(f"color: {COLORS['TEXT_3']};")
        self.filter_info_label.setAlignment(Qt.AlignCenter)
        root.addWidget(self.filter_info_label)

    def _populate_items(self) -> None:
        # 清除占位的 stretch，重新填充
        # item_layout 最后一项是 stretch
        while self.item_layout.count() > 0:
            item = self.item_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

        self._items.clear()
        for command, description, icon in SLASH_COMMANDS:
            frame = SlashItemFrame(command, description, icon, self.content_widget)
            self._items.append(frame)
            self.item_layout.addWidget(frame)
        self.item_layout.addStretch(1)
        self._visible_items = list(self._items)
        for it in self._items:
            it.show()

    def _apply_base_style(self) -> None:
        # 由 paintEvent 绘制圆角卡片，这里仅保证透明
        self.setStyleSheet("SlashPopup { background: transparent; }")

    # ------------------------------------------------------------------
    # 圆角卡片背景
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        rect = self.rect().adjusted(1, 1, -1, -1)
        radius = int(RADII['lg'].rstrip('px'))  # 统一使用大卡片圆角 24px
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, QColor(COLORS["CARD"]))
        pen_color = QColor(_SP_PRIM)
        pen_color.setAlphaF(0.35)
        painter.setPen(pen_color)
        painter.drawPath(path)
        painter.end()
        super().paintEvent(event)

    # ------------------------------------------------------------------
    # 对外 API
    # ------------------------------------------------------------------

    def show_near(self, parent_widget: QWidget) -> None:
        """在给定 widget 的下方显示弹窗；如空间不足则在上方显示。"""
        if parent_widget is None:
            self.show()
            self._after_show()
            return
        global_pos = parent_widget.mapToGlobal(QPoint(0, 0))
        width = max(self.width(), 380)
        self.setFixedWidth(width)

        # 尝试下方
        below_pos = QPoint(global_pos.x(), global_pos.y() + parent_widget.height() + 6)
        screen = parent_widget.screen() if hasattr(parent_widget, "screen") else None
        available = screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1080)

        # 预估高度
        estimated_h = min(360, 70 + 42 * len(self._visible_items))
        if below_pos.y() + estimated_h > available.bottom() - 10:
            # 放在上方
            pos = QPoint(global_pos.x(), global_pos.y() - estimated_h - 6)
        else:
            pos = below_pos

        # 水平不超出屏幕
        if pos.x() + width > available.right() - 10:
            pos.setX(max(available.left() + 6, available.right() - width - 10))

        self.move(pos)
        self.show()
        self.raise_()
        self.activateWindow()
        self._after_show()

    def _after_show(self) -> None:
        """显示后执行弹窗 pop_in + 列表项 staggered 入场。"""
        if self._is_closing:
            # 关闭动画进行中被重新打开：重置标志并停止残留出场动画
            self._is_closing = False
            _stop_existing_anim(self, "geometry")
            _stop_existing_anim(self, "opacity")
        # pop_in (scale) + fade_in — 与同类弹窗统一为 200/180ms
        try:
            pop_in(self, duration_ms=200, start_scale=0.92, easing=QEasingCurve.OutBack)
            fade_in(self, duration_ms=180, easing=QEasingCurve.OutCubic)
        except Exception:
            pass
        # 列表项 staggered slide_in + fade_in
        self._run_item_entrance()

    def _run_item_entrance(self) -> None:
        """可见列表项依次执行 slide_in + fade_in，间隔 25ms。"""
        if self._items_animated:
            return
        self._items_animated = True
        for i, item in enumerate(self._visible_items):
            item.setGraphicsEffect(None)
            QTimer.singleShot(i * 25, lambda it=item: self._animate_single_item(it))

    def _animate_single_item(self, item: SlashItemFrame) -> None:
        if item is None or not item.isVisible():
            return
        try:
            from src.gui.animations import slide_in
            slide_in(item, direction="up", duration_ms=180, distance_px=16, easing=QEasingCurve.OutBack)
            fade_in(item, duration_ms=150, easing=QEasingCurve.OutCubic)
        except Exception:
            pass

    def hide_popup(self) -> None:
        """关闭弹窗（带动画）。"""
        if not self.isVisible() or self._is_closing:
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
        except Exception:
            _finish()
        try:
            fade_out(self, duration_ms=150, on_finished=_finish)
        except Exception:
            _finish()

    def set_filter_text(self, text: str) -> None:
        """设置过滤用的输入文本；"/plan" 会过滤出 plan 开头的命令。"""
        cleaned = text.strip()
        self._filter_text = cleaned
        self.filter_items(cleaned)

    def filter_items(self, text: str) -> None:
        """根据输入文字过滤命令列表；支持模糊匹配（子序列）。"""
        token = text.strip().lower()
        if token.startswith("/"):
            token = token[1:]

        # 简单模糊匹配：连续子串优先，否则检查每个字符子序列
        matched: List[SlashItemFrame] = []
        for it in self._items:
            haystack = (it.command + " " + it.description).lower()
            if not token or token in haystack:
                matched.append(it)
            else:
                # 子序列匹配（按顺序包含）
                idx = 0
                ok = True
                for ch in token:
                    j = haystack.find(ch, idx)
                    if j == -1:
                        ok = False
                        break
                    idx = j + 1
                if ok:
                    matched.append(it)

        self._visible_items = matched
        # 隐藏/显示
        visible_set = set(id(m) for m in matched)
        for it in self._items:
            it.setVisible(id(it) in visible_set)

        if self._visible_items:
            self._set_selected_index(0)
        else:
            self._selected_index = -1

        # 更新底部提示
        if token:
            total = len(self._items)
            shown = len(self._visible_items)
            self.filter_info_label.setText(
                f"\u8fc7\u6ee4 '{token}' \u00b7 {shown}/{total} \u00b7 \u2191\u2193 Enter Esc"
            )
        else:
            self.filter_info_label.setText(
                "\u2191\u2193 \u9009\u62e9 \u00b7 Enter \u786e\u8ba4 \u00b7 Esc \u5173\u95ed"
            )

        # 根据可见项调整高度
        count = len(self._visible_items)
        if count == 0:
            self.setFixedHeight(140)
        else:
            estimated = 70 + 42 * count
            self.setFixedHeight(min(360, max(180, estimated)))

    def navigate(self, direction: int) -> None:
        """+1 下一项 / -1 上一项；越界循环。"""
        if not self._visible_items:
            return
        new_idx = (self._selected_index + direction) % len(self._visible_items)
        self._set_selected_index(new_idx)

    def confirm_current(self) -> None:
        """确认当前选中项并发出 item_selected 信号。"""
        if self._is_closing or not self.isVisible():
            return
        if not self._visible_items or self._selected_index < 0:
            return
        item = self._visible_items[self._selected_index]
        # 标记关闭态防止重复发射
        self._is_closing = True
        self.item_selected.emit(item.command)
        # 隐藏后重置标志，确保弹窗复用时入场动画正常
        self.hide()
        self._is_closing = False
        self._items_animated = False

    # ------------------------------------------------------------------
    # 内部：选中/滚动/动画
    # ------------------------------------------------------------------

    def _set_selected_index(self, index: int) -> None:
        for i, it in enumerate(self._visible_items):
            it.set_selected(i == index)
        self._selected_index = index
        if 0 <= index < len(self._visible_items):
            self._ensure_visible(self._visible_items[index])

    def _ensure_visible(self, item: QWidget) -> None:
        sa = self.scroll_area
        viewport = sa.viewport()
        top = item.mapTo(viewport, QPoint(0, 0)).y()
        bottom = top + item.height()
        vbar = sa.verticalScrollBar()
        if top < 0:
            vbar.setValue(vbar.value() + top)
        elif bottom > viewport.height():
            vbar.setValue(vbar.value() + (bottom - viewport.height()))

    def _select_item(self, item: SlashItemFrame) -> None:
        if self._is_closing:
            return
        try:
            index = self._visible_items.index(item)
        except ValueError:
            return
        self._set_selected_index(index)
        QTimer.singleShot(60, self.confirm_current)

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key_Up, Qt.Key_Left):
            self.navigate(-1)
            event.accept()
            return
        if key in (Qt.Key_Down, Qt.Key_Right):
            self.navigate(1)
            event.accept()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.confirm_current()
            event.accept()
            return
        if key == Qt.Key_Escape:
            self.hide_popup()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        # 失焦自动关闭，但要给点击/动画一个缓冲
        QTimer.singleShot(120, self._maybe_close_on_focus_loss)
        super().focusOutEvent(event)

    def _maybe_close_on_focus_loss(self) -> None:
        if not self.isVisible():
            return
        # 如果当前焦点不在本弹窗及其子控件中则关闭
        focus = self.focusWidget()
        if focus is None or (focus is not self and not self.isAncestorOf(focus)):
            # 不强制关闭（因为输入框可能会接管焦点），让外部显式 hide_popup
            pass


# ============================================================================
# 便捷工厂函数（可选，供主窗口直接使用）
# ============================================================================

def create_slash_popup(parent_widget: QWidget, callback) -> SlashPopup:
    """创建一个 SlashPopup 并绑定 item_selected 到 callback。"""
    popup = SlashPopup(parent_widget)
    popup.item_selected.connect(callback)
    popup.show_near(parent_widget)
    return popup


__all__ = ["SlashPopup", "SlashItemFrame", "SLASH_COMMANDS", "create_slash_popup"]
