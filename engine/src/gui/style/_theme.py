# -*- coding: utf-8 -*-
"""主题切换 — apply_theme() 与 ThemeManager。

从原 src/gui/style.py 拆分而来。新增 ThemeManager 单例，提供集中式主题变更
信号，避免各组件手动遍历刷新。
"""
import logging
import os

from PySide6.QtCore import QObject, Signal

from src.gui.style._colors import (
    COLORS, get_colors, _refresh_status_colors, invalidate_style_cache,
)
from src.gui.style._sizes import SIZES

logger = logging.getLogger(__name__)

# 显式记录当前主题名，替代"按背景色字符串前缀猜主题"的脆弱做法
_CURRENT_THEME = "light"


def build_stylesheet(selector: str, **props) -> str:
    """根据 selector 与属性字典构建 QSS 字符串。

    示例：
        build_stylesheet("QFrame#Card", background=COLORS['CARD'],
                         border=f"1px solid {COLORS['BORDER']}",
                         border_radius=RADII['lg'])
    返回：
        "QFrame#Card{background:#FFFFFF;border:1px solid #E5E7EB;border-radius:16px;}"

    属性名中的下划线会自动替换为连字符（border_radius -> border-radius）。
    """
    if not props:
        return f"{selector}{{}}"
    declarations = []
    for key, value in props.items():
        css_key = key.replace("_", "-")
        declarations.append(f"{css_key}:{value}")
    return f"{selector}{{{';'.join(declarations)};}}"


def _detect_system_theme() -> str:
    """探测系统主题。优先使用 Qt 6.5+ 的 QStyleHints，否则回退到环境变量。"""
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt
        app = QApplication.instance()
        if app is not None:
            scheme = app.styleHints().colorScheme()
            if scheme == Qt.ColorScheme.Dark:
                return "dark"
            if scheme == Qt.ColorScheme.Light:
                return "light"
    except Exception:
        pass
    env = os.environ.get("GTK_THEME", "").lower()
    if "dark" in env:
        return "dark"
    return "light"


class ThemeManager(QObject):
    """集中式主题管理单例。

    提供 `theme_changed(str)` 信号，所有需要响应主题变化的组件均可连接此信号。
    """
    theme_changed = Signal(str)

    _instance: "ThemeManager | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        super().__init__()
        if self._initialized:
            return
        self._initialized = True
        self._current = "light"

    @classmethod
    def instance(cls) -> "ThemeManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def current_theme(self) -> str:
        return self._current

    def is_dark(self) -> bool:
        return self._current == "dark"

    def apply_theme(self, theme: str) -> None:
        """切换主题并发射 theme_changed 信号。"""
        global _CURRENT_THEME
        if theme == "system":
            resolved = _detect_system_theme()
        else:
            resolved = "dark" if theme == "dark" else "light"

        self._current = resolved
        _CURRENT_THEME = resolved

        new_colors = get_colors(resolved)
        COLORS.clear()
        COLORS.update(new_colors)
        _refresh_status_colors()
        invalidate_style_cache()
        self._apply_palette()
        self.theme_changed.emit(resolved)

    def _apply_palette(self):
        """将当前主题颜色同步到 QApplication palette。"""
        try:
            from PySide6.QtWidgets import QApplication
            from PySide6.QtGui import QPalette, QColor
            app = QApplication.instance()
            if app is None:
                return
            pal = app.palette()
            bg = QColor(COLORS["BGD"])
            fg = QColor(COLORS["TEXT"])
            pal.setColor(QPalette.Window, bg)
            pal.setColor(QPalette.WindowText, fg)
            pal.setColor(QPalette.Base, QColor(COLORS["CARD"]))
            pal.setColor(QPalette.AlternateBase, QColor(COLORS["SURFACE"]))
            pal.setColor(QPalette.Button, QColor(COLORS["CARD"]))
            pal.setColor(QPalette.ButtonText, fg)
            pal.setColor(QPalette.Text, fg)
            pal.setColor(QPalette.Highlight, QColor(COLORS["ACCENT"]))
            pal.setColor(QPalette.HighlightedText, QColor(COLORS["TEXT_ON_ACCENT"]))
            pal.setColor(QPalette.ToolTipBase, QColor(COLORS["CARD"]))
            pal.setColor(QPalette.ToolTipText, QColor(COLORS["TEXT"]))
            pal.setColor(QPalette.PlaceholderText, QColor(COLORS["TEXT_4"]))
            app.setPalette(pal)
        except Exception:
            logger.warning("ThemeManager: Qt palette 设置失败", exc_info=True)


def is_dark_theme() -> bool:
    """返回当前是否为深色主题。"""
    return _CURRENT_THEME == "dark"


def get_current_theme() -> str:
    """返回当前主题名 ('light' 或 'dark')。"""
    return _CURRENT_THEME


def apply_theme(theme: str) -> None:
    """切换全局主题（兼容旧 API，内部委托给 ThemeManager）。"""
    ThemeManager.instance().apply_theme(theme)
