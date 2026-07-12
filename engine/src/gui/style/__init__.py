# -*- coding: utf-8 -*-
"""全局设计系统包 (颜色 / 圆角 / 字体 / i18n / 主题 / 阴影)。

由原 src/gui/style.py 拆分为:
  _colors — COLORS / RADII / SPACING / FONT_STACK / rgba / 样式工厂 / 状态色 / SHADOWS
  _i18n   — I18N / tr / 翻译字典 (zh, en)
  _theme  — apply_theme
对外公开名称与原 style.py 完全一致。
"""
from src.gui.style._colors import (
    COLORS, COLORS_LIGHT, COLORS_DARK, get_colors,
    RADII, BORDER, SPACING, FONT_STACK, FONT_CODE, SHADOWS,
    _HAS_QT, _STYLE_CACHE, _STYLE_CACHE_VERSION,
    invalidate_style_cache, _cached_style,
    make_label, make_line_edit, make_text_edit, make_section_label,
    _build_status_colors, STATUS_COLORS, _refresh_status_colors,
    rgba, qcolor,
)
from src.gui.style._i18n import I18N, tr
from src.gui.style._sizes import SIZES
from src.gui.style._theme import apply_theme, is_dark_theme, get_current_theme, build_stylesheet, ThemeManager

# 字重阶梯
FONT_WEIGHTS = {
    "regular": 400,
    "medium": 500,
    "semibold": 600,
    "bold": 700,
}


def apply_shadow(widget, level='md'):
    """用 CSS border 模拟阴影，彻底避免 QGraphicsEffect 渲染冲突。"""
    try:
        spec = SHADOWS.get(level, SHADOWS.get('sm', (2, 8, 0.08)))
        y, _, _ = spec
        # 用底部 border 模拟阴影（轻量、无 QGraphicsEffect）
        current_ss = widget.styleSheet() or ""
        if "border-bottom" not in current_ss:
            widget.setStyleSheet(
                current_ss +
                f"border-bottom:{max(1,y)}px solid {COLORS['DIVIDER']};"
            )
    except Exception:
        pass


__all__ = [
    "COLORS", "COLORS_LIGHT", "COLORS_DARK", "get_colors",
    "RADII", "BORDER", "SPACING", "SIZES", "FONT_STACK", "FONT_CODE", "SHADOWS",
    "FONT_WEIGHTS", "apply_shadow",
    "invalidate_style_cache", "_cached_style",
    "make_label", "make_line_edit", "make_text_edit", "make_section_label",
    "STATUS_COLORS", "_refresh_status_colors",
    "rgba", "qcolor",
    "I18N", "tr", "apply_theme", "is_dark_theme", "get_current_theme",
    "build_stylesheet", "ThemeManager",
]
