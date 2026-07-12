# -*- coding: utf-8 -*-
"""设置弹窗包。

由原 src/gui/settings_dialog.py 拆分:
  _sections — UI 区段构建器与辅助组件
  _dialog   — SettingsDialog 主类
对外公开名称与原 settings_dialog.py 完全一致。
"""
from src.gui.settings_dialog._dialog import SettingsDialog, QMessageBox
from src.gui.settings_dialog._sections import (
    SegmentedControl, ModelRow, ASSETS_DIR, ANIM_DURATION,
)

__all__ = [
    "SettingsDialog", "QMessageBox",
    "SegmentedControl", "ModelRow", "ASSETS_DIR", "ANIM_DURATION",
]
