# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from typing import List, Optional

from PySide6.QtCore import (
    Qt, QSize, Signal, QPropertyAnimation, QEasingCurve, QEvent,
    QPoint, Property,
)
from PySide6.QtGui import (
    QPixmap, QIcon, QColor, QPainter, QPen, QBrush, QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QFrame, QPushButton, QTextEdit, QScrollArea, QSplitter,
    QGraphicsOpacityEffect, QLineEdit, QSizePolicy, QMessageBox, QDialog,
)
from src.gui.style import (
    COLORS, RADII, SPACING, SIZES, FONT_STACK, FONT_WEIGHTS, rgba, tr,
    apply_shadow, ThemeManager,
)
try:
    from PySide6.QtWidgets import QScroller
except Exception:
    QScroller = None

from src.gui._gui_helpers import _load_icon
from src.gui.widgets.project_dialogs import (
    ProjectRowWidget, DeleteConfirmDialog, RenameDialog,
)


# ============================================================================
# 左栏（对话列表）
# ============================================================================
class LeftPanel(QFrame):
    new_project_clicked = Signal()
    project_selected = Signal(str)
    project_deleted = Signal(str)
    project_pinned = Signal(str, bool)
    project_renamed = Signal(str, str)
    settings_clicked = Signal()
    theme_toggled = Signal(str)  # (new_theme: "light" or "dark")

    def __init__(self, parent=None, language="zh"):
        super().__init__(parent)
        self._language = language if language else "zh"
        self._expanded_width = SIZES["sidebar"]["expanded"]
        self._collapsed_width = SIZES["sidebar"]["collapsed"]
        self._expanded = True
        # 初始化暗色模式状态：使用显式主题变量
        from src.gui.style import is_dark_theme
        self._is_dark = is_dark_theme()

        self.setObjectName("LeftPanel")
        self.setAttribute(Qt.WA_AcceptTouchEvents, True)
        self.setFixedWidth(self._expanded_width)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 顶部折叠/展开按钮
        self._toggle_btn = self._create_button(
            icon_name="chevron_left_18.png", size=SIZES["sidebar"]["toggle_btn"],
            callback=self._on_toggle_sidebar, style_method=self._toggle_btn_style,
        )
        root.addWidget(self._toggle_btn, 0, Qt.AlignCenter)

        # 顶部始终可见的新建对话按钮（折叠态也能用）
        self._compact_new_btn = self._create_button(
            icon_name="plus_20.png", size=SIZES["sidebar"]["compact_new_btn"],
            callback=self.new_project_clicked.emit, tooltip=tr("new_chat"),
            style_method=self._fab_btn_style, shadow="md",
        )
        root.addWidget(self._compact_new_btn, 0, Qt.AlignCenter)

        # 内容容器
        self._content = QFrame()
        self._content.setStyleSheet("QFrame{background:transparent;}")
        lay = QVBoxLayout(self._content)
        lay.setContentsMargins(SIZES["sidebar"]["content_margin"],
                               SIZES["sidebar"]["content_margin"],
                               SIZES["sidebar"]["content_margin"],
                               SIZES["sidebar"]["content_margin"])
        lay.setSpacing(SIZES["spacing"]["md"])

        # 使用简单的 visibility 切换代替 QGraphicsOpacityEffect（避免渲染冲突）
        self._content.setVisible(self._expanded)

        # 顶部：对话列表 标题
        self._title_label = QLabel(tr("conversation_list"))
        self._title_label.setWordWrap(False)
        self._title_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        lay.addWidget(self._title_label)

        # 新对话按钮（蓝色胶囊，带加号图标）
        self._new_btn = self._create_button(
            text=tr("new_chat"), icon_name="plus_20.png",
            height=SIZES["sidebar"]["new_btn_height"],
            callback=self.new_project_clicked.emit, style_method=self._primary_btn_style,
            shadow="md", layout_direction=Qt.LeftToRight,
        )
        lay.addWidget(self._new_btn)

        # 滚动区: 放置项目行
        self._scroll = QScrollArea()
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._list_container = QWidget()
        self._list_container.setStyleSheet("background:transparent;")
        self._list_lay = QVBoxLayout(self._list_container)
        self._list_lay.setContentsMargins(0, SIZES["spacing"]["xs"], 0, SIZES["spacing"]["xs"])
        self._list_lay.setSpacing(SIZES["spacing"]["xs"])
        self._list_lay.setAlignment(Qt.AlignTop)
        self._list_lay.addStretch(1)
        self._scroll.setWidget(self._list_container)
        self._scroll.setAttribute(Qt.WA_AcceptTouchEvents, True)
        if QScroller is not None:
            try:
                QScroller.grabGesture(self._scroll.viewport(), QScroller.LeftMouseButtonGesture)
            except Exception:
                pass
        lay.addWidget(self._scroll, 1)

        # 底部：细分隔线
        self._separator_line = QFrame()
        self._separator_line.setFrameShape(QFrame.HLine)
        lay.addWidget(self._separator_line)

        # 底部：设置 + 深色模式（图标+文字水平排列）
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(SIZES["spacing"]["md"])
        bottom_row.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        bottom_row.setContentsMargins(SIZES["spacing"]["xs"], SIZES["spacing"]["xs"],
                                      SIZES["spacing"]["xs"], SIZES["spacing"]["xs"])

        self._settings_btn = self._create_button(
            icon_name="settings_18.png", icon_size_key="icon_sm",
            height=32, prefix_text=tr("settings"),
            callback=self.settings_clicked.emit, tooltip=tr("settings"),
            style_method=self._bottom_btn_style,
        )
        bottom_row.addWidget(self._settings_btn)

        self.dark_btn = self._create_button(
            icon_name="moon_18.png", icon_size_key="icon_sm",
            height=32, prefix_text=tr("dark_mode_btn"),
            callback=self._on_toggle_dark, tooltip=tr("dark_mode_btn"),
            style_method=self._bottom_btn_style,
        )
        bottom_row.addWidget(self.dark_btn)
        bottom_row.addStretch(1)
        lay.addLayout(bottom_row)

        root.addWidget(self._content, 1)

        self._row_widgets = {}
        self._current_name = None
        self._width_anim: Optional[QPropertyAnimation] = None
        self._fade_anim: Optional[QPropertyAnimation] = None

        # 应用并订阅主题
        self._build_panel_styles()
        try:
            ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 按钮工厂（统一入口，替代 _create_icon_btn / _create_fab_btn /
    # _create_primary_btn / _create_bottom_text_btn）
    # ------------------------------------------------------------------
    def _create_button(
        self, *,
        icon_name: str = "",
        icon_size_key: str = "icon_md",
        size: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        callback,
        text: str = "",
        prefix_text: str = "",
        tooltip: str = "",
        style_method,
        shadow: Optional[str] = None,
        layout_direction: Optional[Qt.LayoutDirection] = None,
    ) -> QPushButton:
        """统一的按钮工厂方法。"""
        btn = QPushButton(text)
        if size is not None:
            btn.setFixedSize(size, size)
        elif width is not None and height is not None:
            btn.setFixedSize(width, height)
        elif height is not None:
            btn.setFixedHeight(height)
        btn.setCursor(Qt.PointingHandCursor)
        if icon_name:
            icon_size = SIZES["sidebar"][icon_size_key]
            ic = _load_icon(icon_name, icon_size)
            if not ic.isNull():
                btn.setIcon(ic)
                btn.setIconSize(QSize(icon_size, icon_size))
        if prefix_text and icon_name:
            btn.setText(" " + prefix_text)
        elif not text and prefix_text:
            btn.setText(prefix_text)
        if tooltip:
            btn.setToolTip(tooltip)
        if layout_direction is not None:
            btn.setLayoutDirection(layout_direction)
        btn.setStyleSheet(style_method())
        btn.clicked.connect(callback)
        if shadow:
            apply_shadow(btn, shadow)
        return btn

    # ------------------------------------------------------------------
    # 样式构建器
    # ------------------------------------------------------------------
    def _panel_frame_style(self) -> str:
        return (
            "QFrame#LeftPanel{background:" + COLORS['BGD'] + ";"
            "border-right:1px solid " + COLORS['DIVIDER'] + ";}"
            "QFrame{background:transparent;}"
        )

    def _toggle_btn_style(self) -> str:
        return (
            "QPushButton{background:transparent;color:" + COLORS['TEXT_3'] + ";"
            "border:none;border-radius:" + RADII['sm'] + ";font-size:16px;"
            "font-family:" + FONT_STACK + ";}"
            "QPushButton:hover{background:" + COLORS['SURFACE'] + ";"
            "color:" + COLORS['TEXT'] + ";}"
        )

    def _fab_btn_style(self) -> str:
        return (
            "QPushButton{background:" + COLORS['ACCENT'] + ";"
            "color:" + COLORS['TEXT_ON_ACCENT'] + ";"
            "border:none;border-radius:" + RADII['circle'] + ";}"
            "QPushButton:hover{background:" + COLORS['ACCENT_HOVER'] + ";}"
            "QPushButton:pressed{background:" + COLORS['ACCENT_PRESSED'] + ";}"
        )

    def _primary_btn_style(self) -> str:
        return (
            "QPushButton{"
            "background:" + COLORS['ACCENT'] + ";"
            "color:" + COLORS['TEXT_ON_ACCENT'] + ";"
            "border:none;"
            "border-radius:" + RADII['pill'] + ";"
            "font-size:14px;"
            "font-weight:" + str(FONT_WEIGHTS['semibold']) + ";"
            "padding:0 16px 0 12px;"
            "font-family:" + FONT_STACK + ";"
            "text-align:left;"
            "}"
            "QPushButton:hover{"
            "background:" + COLORS['ACCENT_HOVER'] + ";"
            "}"
            "QPushButton:pressed{"
            "background:" + COLORS['ACCENT_PRESSED'] + ";"
            "}"
        )

    def _title_label_style(self) -> str:
        return (
            "QLabel{color:" + COLORS['TEXT'] + ";"
            "font-size:18px;font-weight:700;"
            "font-family:" + FONT_STACK + ";"
            "background:transparent;}"
        )

    def _scroll_style(self) -> str:
        return (
            "QScrollArea{background:transparent;border:none;}"
            "QScrollBar:vertical{background:transparent;width:4px;}"
            "QScrollBar::handle:vertical{background:" + COLORS['BORDER'] + ";"
            "border-radius:2px;}"
        )

    def _separator_style(self) -> str:
        return (
            "QFrame{background:" + COLORS['DIVIDER'] + ";"
            "max-height:1px;min-height:1px;border:none;}"
        )

    def _bottom_btn_style(self) -> str:
        return (
            "QPushButton{background:transparent;border:none;border-radius:" + RADII['sm'] + ";"
            "color:" + COLORS['TEXT_3'] + ";font-size:13px;"
            "font-weight:" + str(FONT_WEIGHTS['regular']) + ";"
            "font-family:" + FONT_STACK + ";padding:6px 10px;}"
            "QPushButton:hover{background:" + COLORS['SURFACE'] + ";"
            "color:" + COLORS['TEXT'] + ";}"
        )

    def _build_panel_styles(self):
        """从当前 COLORS 重建所有子控件样式表。"""
        self.setStyleSheet(self._panel_frame_style())
        self._toggle_btn.setStyleSheet(self._toggle_btn_style())
        self._compact_new_btn.setStyleSheet(self._fab_btn_style())
        apply_shadow(self._compact_new_btn, 'md')
        self._title_label.setStyleSheet(self._title_label_style())
        self._new_btn.setStyleSheet(self._primary_btn_style())
        apply_shadow(self._new_btn, 'md')
        self._scroll.setStyleSheet(self._scroll_style())
        self._separator_line.setStyleSheet(self._separator_style())
        bottom_style = self._bottom_btn_style()
        self._settings_btn.setStyleSheet(bottom_style)
        self.dark_btn.setStyleSheet(bottom_style)

    def _on_theme_changed(self, theme: str):
        """响应 ThemeManager 主题变更信号。"""
        self._is_dark = (theme == "dark")
        self._build_panel_styles()
        # 重新加载图标，确保深浅色主题下图标颜色正确
        try:
            toggle_icon = "chevron_left_18.png" if self._expanded else "chevron_right_18.png"
            self._toggle_btn.setIcon(_load_icon(toggle_icon, SIZES["sidebar"]["icon_md"]))
            self._toggle_btn.setIconSize(QSize(SIZES["sidebar"]["icon_md"], SIZES["sidebar"]["icon_md"]))
            self._compact_new_btn.setIcon(_load_icon("plus_20.png", SIZES["sidebar"]["icon_md"]))
            self._compact_new_btn.setIconSize(QSize(SIZES["sidebar"]["icon_md"], SIZES["sidebar"]["icon_md"]))
        except Exception:
            pass
        self.retranslate_ui(self._language)

    # --- 侧边栏宽度属性（QPropertyAnimation 驱动） ---
    def _get_sidebar_width(self) -> int:
        return self.width()

    def _set_sidebar_width(self, width: int) -> None:
        self.setFixedWidth(int(width))

    _sidebar_width = Property(int, _get_sidebar_width, _set_sidebar_width)

    def _on_toggle_sidebar(self):
        if self._expanded:
            self.collapse()
        else:
            self.expand()

    def collapse(self, animate: bool = True):
        if not self._expanded:
            return
        self._expanded = False
        self._toggle_btn.setIcon(_load_icon("chevron_right_18.png", SIZES["sidebar"]["icon_md"]))
        self._toggle_btn.setIconSize(QSize(SIZES["sidebar"]["icon_md"], SIZES["sidebar"]["icon_md"]))
        if animate:
            self._animate_sidebar(self._collapsed_width, show_content=False)
        else:
            self.setFixedWidth(self._collapsed_width)
            self._content.setVisible(False)

    def expand(self, animate: bool = True):
        if not self._expanded:
            return
        self._expanded = True
        self._toggle_btn.setIcon(_load_icon("chevron_left_18.png", SIZES["sidebar"]["icon_md"]))
        self._toggle_btn.setIconSize(QSize(SIZES["sidebar"]["icon_md"], SIZES["sidebar"]["icon_md"]))
        self._content.setVisible(True)
        if animate:
            self._animate_sidebar(self._expanded_width, show_content=True)
        else:
            self.setFixedWidth(self._expanded_width)

    def _animate_sidebar(self, target_width: int, show_content: bool):
        if self._width_anim is not None:
            try:
                self._width_anim.stop()
            except Exception:
                pass
        self._width_anim = QPropertyAnimation(self, b"_sidebar_width", parent=self)
        self._width_anim.setDuration(220)
        self._width_anim.setStartValue(self.width())
        self._width_anim.setEndValue(target_width)
        self._width_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._width_anim.start(QPropertyAnimation.DeleteWhenStopped)
        # 动画结束后直接切换 visible
        QTimer.singleShot(220, lambda: self._content.setVisible(show_content))

    def set_language(self, language: str):
        """动态切换左侧栏语言。"""
        self._language = language if language else "zh"
        self.retranslate_ui(language)

    def retranslate_ui(self, language: str):
        """语言切换后刷新左侧面板文本。"""
        from src.gui.style import tr
        self._language = language
        try:
            self._title_label.setText(tr("conversation_list"))
        except Exception:
            pass
        try:
            self._new_btn.setText(tr("new_chat"))
        except Exception:
            pass
        try:
            is_dark = self._is_dark
            self.dark_btn.setToolTip(tr("light_mode_btn") if is_dark else tr("dark_mode_btn"))
            self.dark_btn.setIcon(_load_icon("sun_18.png" if is_dark else "moon_18.png", SIZES["sidebar"]["icon_sm"]))
            self.dark_btn.setIconSize(QSize(SIZES["sidebar"]["icon_sm"], SIZES["sidebar"]["icon_sm"]))
            self.dark_btn.setText(" " + (tr("light_mode_btn") if is_dark else tr("dark_mode_btn")))
            self._settings_btn.setText(" " + tr("settings"))
        except Exception:
            pass

    def set_projects(self, names, current=None):
        """兼容旧接口：接受目录名列表。"""
        infos = []
        if isinstance(names, list):
            for n in names:
                infos.append({
                    "name": str(n),
                    "display_name": str(n),
                    "pinned": False,
                    "pinned_at": 0,
                })
        self.set_project_infos(infos, current=current)

    def set_project_infos(self, infos, current=None):
        """使用项目信息列表增量更新左侧会话列表，保持滚动位置。"""
        try:
            scroll_bar = self._scroll.verticalScrollBar()
            old_scroll = scroll_bar.value()
        except Exception:
            old_scroll = 0

        infos = [info for info in (infos or []) if isinstance(info, dict) and info.get("name")]
        new_names = {info.get("name") for info in infos}

        # 删除已不存在的行
        for n in list(self._row_widgets.keys()):
            if n not in new_names:
                row = self._row_widgets.pop(n)
                self._list_lay.removeWidget(row)
                row.setParent(None)
                row.deleteLater()

        # 暂将现有行从布局中移除，稍后按新顺序重新加入
        for row in list(self._row_widgets.values()):
            self._list_lay.removeWidget(row)

        self._current_name = None
        for info in infos:
            n = info.get("name")
            display_name = info.get("display_name") or n
            pinned = bool(info.get("pinned", False))
            is_current = bool(current and n == current)

            row = self._row_widgets.get(n)
            if row is None:
                row = ProjectRowWidget(
                    name=n,
                    display_name=display_name,
                    pinned=pinned,
                    language=self._language,
                )
                row.clicked.connect(self._on_row_clicked)
                row.delete_requested.connect(self._on_delete_request)
                row.pin_requested.connect(self._on_pin_request)
                row.rename_requested.connect(self._on_rename_request)
                self._row_widgets[n] = row
            else:
                row.update_info(display_name=display_name, pinned=pinned)
            row.update_info(selected=is_current)
            self._list_lay.addWidget(row)
            if is_current:
                self._current_name = n

        # 保证底部只有一个 stretch
        try:
            for i in range(self._list_lay.count() - 1, -1, -1):
                item = self._list_lay.itemAt(i)
                if item is not None and item.spacerItem() is not None:
                    self._list_lay.removeItem(item)
        except Exception:
            pass
        self._list_lay.addStretch(1)

        # 恢复滚动位置
        try:
            scroll_bar.setValue(old_scroll)
        except Exception:
            pass

    def _on_row_clicked(self, name):
        for n, row in self._row_widgets.items():
            row.set_selected(n == name)
        self._current_name = name
        self.project_selected.emit(name)

    def _on_delete_request(self, name):
        parent = self
        while parent is not None and parent.parent() is not None:
            parent = parent.parent()
        row = self._row_widgets.get(name)
        display_name = row._display_name if row else name
        dlg = DeleteConfirmDialog(display_name, parent)
        if dlg.exec() == QDialog.Accepted:
            self.project_deleted.emit(name)

    def _on_pin_request(self, name):
        row = self._row_widgets.get(name)
        if row is None:
            return
        self.project_pinned.emit(name, not row._pinned)

    def _on_rename_request(self, name):
        row = self._row_widgets.get(name)
        if row is None:
            return
        parent = self
        while parent is not None and parent.parent() is not None:
            parent = parent.parent()
        dlg = RenameDialog(row._display_name, language=self._language, parent=parent)
        if dlg.exec() == QDialog.Accepted:
            new_name = dlg.get_new_name()
            if new_name and new_name != row._display_name:
                self.project_renamed.emit(name, new_name)

    def _on_toggle_dark(self):
        try:
            current = "dark" if getattr(self, "_is_dark", False) else "light"
            new_theme = "dark" if current == "light" else "light"
            self._is_dark = (new_theme == "dark")
            # 更新按钮图标和文字
            if self._is_dark:
                self.dark_btn.setIcon(_load_icon("sun_18.png", SIZES["sidebar"]["icon_sm"]))
                self.dark_btn.setIconSize(QSize(SIZES["sidebar"]["icon_sm"], SIZES["sidebar"]["icon_sm"]))
                self.dark_btn.setText(" " + tr("light_mode_btn"))
                self.dark_btn.setToolTip(tr("light_mode_btn"))
            else:
                self.dark_btn.setIcon(_load_icon("moon_18.png", SIZES["sidebar"]["icon_sm"]))
                self.dark_btn.setIconSize(QSize(SIZES["sidebar"]["icon_sm"], SIZES["sidebar"]["icon_sm"]))
                self.dark_btn.setText(" " + tr("dark_mode_btn"))
                self.dark_btn.setToolTip(tr("dark_mode_btn"))
            self.theme_toggled.emit(new_theme)
        except Exception:
            pass
