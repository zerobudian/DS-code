# -*- coding: utf-8 -*-
"""共享 GUI 辅助函数：颜色加深 / 图标加载 / 淡入动画。

这些函数原定义于 ``src.gui.main_window``，现抽取到独立模块，供
``widgets/`` 与 ``panels/`` 子包直接导入，避免与 ``main_window`` 产生循环
导入。``main_window.py`` 仍通过 ``from src.gui._gui_helpers import ...``
重新导出它们，以保持 ``from src.gui.main_window import _fade_in`` 等历史
导入路径继续可用。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QGraphicsOpacityEffect

try:
    from PySide6.QtWidgets import QScroller
except Exception:
    QScroller = None

from src.gui.icon_factory import get_icon


def _darken(hex_color: str, factor: float = 0.85) -> str:
    """按比例加深十六进制颜色；用于按钮 hover / pressed 状态。
    factor=0.88 → 保留 88% 亮度；factor 越小越深。"""
    try:
        h = hex_color.lstrip("#")
        if len(h) != 6:
            return hex_color
        r = max(0, min(255, int(int(h[0:2], 16) * factor)))
        g = max(0, min(255, int(int(h[2:4], 16) * factor)))
        b = max(0, min(255, int(int(h[4:6], 16) * factor)))
        return "#{:02X}{:02X}{:02X}".format(r, g, b)
    except Exception:
        return hex_color



def _load_icon(name, size=18):
    """加载 assets/ 中的图标；缺失时回退到 icon_factory 动态生成。"""
    if name.endswith(".png"):
        base = name[:-4]
        parts = base.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            size = int(parts[1])
            icon_name = parts[0]
        else:
            icon_name = base
    else:
        icon_name = name
    pm = get_icon(icon_name, size)
    if not pm.isNull():
        return QIcon(pm)
    return QIcon()


# ---- 共享颜色快捷函数 (settings_dialog, language_pack_dialog 等共用) ----
# 这些不能简单替换为 COLORS[...] 直读，因为调用方依赖每次调用时
# 动态读取当前主题的 COLORS（主题切换后值会变），所以保留为函数。
def _prim():
    """返回当前主题的主强调色，供 QSS hover/pressed 构建。"""
    from src.gui.style import COLORS
    return COLORS["PRIM_DARK"]


def _prim_dark():
    """主强调色加深 80%，用于按钮 pressed 态。"""
    from src.gui.style import COLORS
    return _darken(COLORS["PRIM_DARK"], 0.8)


def _prim_light():
    """浅天蓝高亮背景，用于 hover 态。"""
    from src.gui.style import COLORS
    return COLORS["PRIM_LIGHT"]


def _card():
    from src.gui.style import COLORS
    return COLORS["CARD"]


def _card_alt():
    from src.gui.style import COLORS
    return COLORS["PRIM"]


def _surface():
    from src.gui.style import COLORS
    return COLORS["SURFACE"]


def _border():
    from src.gui.style import COLORS
    return COLORS["BORDER"]


def _border_input():
    from src.gui.style import COLORS
    return COLORS["BORDER"]


def enable_touch_scroll(widget):
    """为可滚动 widget 启用触屏滚动与鼠标手势滚动。"""
    if widget is None:
        return
    try:
        widget.setAttribute(Qt.WA_AcceptTouchEvents, True)
    except Exception:
        pass
    if QScroller is None:
        return
    try:
        viewport = widget.viewport() if hasattr(widget, "viewport") else widget
        QScroller.grabGesture(viewport, QScroller.TouchGesture)
        QScroller.grabGesture(viewport, QScroller.LeftMouseButtonGesture)
    except Exception:
        pass


def _fade_in(widget, duration_ms=180):
    """给 widget 加一个淡入动画 — 动画结束自动清理 effect。

    优化：先停止旧动画、移除旧 effect，避免快速重复调用导致动画/effect 堆积。
    """
    try:
        old = getattr(widget, '_mw_fade_anim', None)
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass
        old_eff = widget.graphicsEffect()
        if isinstance(old_eff, QGraphicsOpacityEffect):
            try:
                widget.setGraphicsEffect(None)
            except Exception:
                pass

        eff = QGraphicsOpacityEffect(widget)
        eff.setOpacity(0.0)
        widget.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", parent=widget)
        anim.setDuration(duration_ms)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        widget._mw_fade_anim = anim

        def _cleanup():
            try:
                if widget:
                    widget.setGraphicsEffect(None)
                    widget._mw_fade_anim = None
            except (RuntimeError, Exception):
                pass
        anim.finished.connect(_cleanup)
        anim.start(QPropertyAnimation.DeleteWhenStopped)
    except Exception:
        pass

