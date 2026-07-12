# -*- coding: utf-8 -*-
"""Icon registry: _MAKERS, _MAKER_ALIASES, resolution, and public API."""
import os
from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from src.gui.icons._helpers import ASSETS_DIR, ICONS_18_24, REQUIRED_ICONS
from src.gui.icons._navigation import (
    _make_arrow_up, _make_arrow_up_plain, _make_arrow_up_white,
    _make_arrow_up_disabled, _make_arrow_up_black,
    _make_stop, _make_stop_white, _make_stop_black,
    _make_chevron_left, _make_chevron_right, _make_chevron_up, _make_chevron_down,
)
from src.gui.icons._actions import (
    _make_paperclip, _make_bubble, _make_keyboard, _make_terminal, _make_paper,
    _make_settings, _make_refresh, _make_close, _make_copy, _make_check,
    _make_plus, _make_minus,
)
from src.gui.icons._status import (
    _make_warning, _make_info, _make_search, _make_filter, _make_sort, _make_question,
)
from src.gui.icons._content import (
    _make_file, _make_folder, _make_pencil, _make_checkbox_empty, _make_checkbox_checked,
)
from src.gui.icons._project import (
    _make_moon, _make_sun, _make_history, _make_memory, _make_model,
    _make_permission, _make_mode, _make_trash, _make_pin, _make_unpin,
)

_MAKERS = {
    "arrow_up": _make_arrow_up,
    "arrow_up_plain": _make_arrow_up_plain,
    "arrow_up_white": _make_arrow_up_white,
    "arrow_up_disabled": _make_arrow_up_disabled,
    "arrow_up_black": _make_arrow_up_black,
    "stop": _make_stop,
    "stop_white": _make_stop_white,
    "stop_black": _make_stop_black,
    "paperclip": _make_paperclip,
    "bubble": _make_bubble,
    "keyboard": _make_keyboard,
    "terminal": _make_terminal,
    "paper": _make_paper,
    "settings": _make_settings,
    "moon": _make_moon,
    "sun": _make_sun,
    "chevron_left": _make_chevron_left,
    "chevron_right": _make_chevron_right,
    "chevron_up": _make_chevron_up,
    "chevron_down": _make_chevron_down,
    "history": _make_history,
    "memory": _make_memory,
    "model": _make_model,
    "permission": _make_permission,
    "mode": _make_mode,
    "trash": _make_trash,
    "refresh": _make_refresh,
    "close": _make_close,
    "copy": _make_copy,
    "check": _make_check,
    "warning": _make_warning,
    "info": _make_info,
    "search": _make_search,
    "filter": _make_filter,
    "sort": _make_sort,
    "pin": _make_pin,
    "unpin": _make_unpin,
    "plus": _make_plus,
    "minus": _make_minus,
    "file": _make_file,
    "folder": _make_folder,
    "question": _make_question,
    "pencil": _make_pencil,
    "checkbox_checked": _make_checkbox_checked,
    "checkbox_empty": _make_checkbox_empty,
}

_MAKER_ALIASES = {
    "gear": "settings",
    "folder_open": "folder",
}

def _resolve_maker(name: str):
    """解析图标名，支持别名。"""
    if name in _MAKERS:
        return _MAKERS[name]
    alias = _MAKER_ALIASES.get(name)
    if alias:
        return _MAKERS.get(alias)
    return None


def ensure_icons(assets_dir: str = None) -> bool:
    """确保所有需要的 PNG 存在于 assets/ 目录中。

    在 offscreen / headless 平台下，QPainter 绘制 QPixmap 可能触发段错误，
    因此直接跳过动态生成，避免真实运行时崩溃。
    """
    try:
        app = QApplication.instance()
        if app is not None and app.platformName() == "offscreen":
            return False
    except Exception:
        pass

    target_dir = assets_dir or ASSETS_DIR
    if not os.path.isdir(target_dir):
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception:
            return False

    all_ok = True
    for fname in REQUIRED_ICONS:
        path = os.path.join(target_dir, fname)
        parts = fname.replace(".png", "").rsplit("_", 1)
        if len(parts) != 2:
            all_ok = False
            continue
        name, size_s = parts
        try:
            size = int(size_s)
        except ValueError:
            all_ok = False
            continue
        maker = _resolve_maker(name)
        if not maker:
            all_ok = False
            continue
        try:
            pm = maker(size)
            pm.save(path, "PNG")
        except Exception:
            all_ok = False
    return all_ok


def get_icon(name: str, size: int) -> QPixmap:
    """运行时获取指定图标。优先读文件，缺失则动态生成。"""
    filename = f"{name}_{size}.png"
    path = os.path.join(ASSETS_DIR, filename)
    if os.path.isfile(path):
        pm = QPixmap(path)
        if not pm.isNull():
            return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    maker = _resolve_maker(name)
    if maker:
        try:
            return maker(size)
        except Exception:
            pass
    return QPixmap()


# ============================================================================
# 动态（非文件）小图标 — 用于 paintEvent 中直接绘制
# ============================================================================


# Verify all icon names in REQUIRED_ICONS have a registered maker
_required_names = {fname.replace(".png", "").rsplit("_", 1)[0] for fname in REQUIRED_ICONS}
assert set(_MAKERS.keys()) == _required_names, f"Mismatch: missing={_required_names - set(_MAKERS.keys())} extra={set(_MAKERS.keys()) - _required_names}"
