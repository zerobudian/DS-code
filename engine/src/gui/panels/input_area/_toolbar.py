# -*- coding: utf-8 -*-
"""InputArea 子模块：底部工具栏按钮行为（保留部分兼容方法）。

原工具栏控件（clear/upload/git/terminal/send/stop）已迁移或移除，本 Mixin
现在仅保留发送按钮状态更新逻辑，供 InputArea 复用。
"""
from __future__ import annotations

from PySide6.QtCore import QSize

from src.gui._gui_helpers import _load_icon


class ToolbarMixin:
    """底部工具栏：send / stop 按钮状态更新。"""

    def _is_dark_theme(self):
        from src.gui.style import is_dark_theme
        return is_dark_theme()

    def _send_icon_name(self, enabled=True):
        return "arrow_up_white"

    def _stop_icon_name(self):
        return "stop_white"

    def _update_send_icons(self):
        """统一更新发送/停止按钮状态和图标。"""
        try:
            if self._running:
                self.send_btn.setEnabled(False)
                self.send_btn.setIcon(_load_icon(self._send_icon_name() + "_24.png", 18))
                self.send_btn.setIconSize(QSize(18, 18))
                self.stop_btn.setIcon(_load_icon(self._stop_icon_name() + "_24.png", 14))
                self.stop_btn.setIconSize(QSize(14, 14))
                return
            has_text = bool(self.input.toPlainText().strip())
            has_attachments = bool(self._uploaded_images or self._uploaded_files)
            can_send = has_text or has_attachments
            self.send_btn.setEnabled(can_send)
            self.send_btn.setIcon(_load_icon(self._send_icon_name(can_send) + "_24.png", 18))
            self.send_btn.setIconSize(QSize(18, 18))
            self.stop_btn.setIcon(_load_icon(self._stop_icon_name() + "_24.png", 14))
            self.stop_btn.setIconSize(QSize(14, 14))
        except Exception:
            pass

    def _update_send_button_state(self):
        """兼容别名 → 调用统一方法。"""
        self._update_send_icons()

    def _on_stop(self):
        try:
            self.stop_clicked.emit()
        except Exception:
            pass
        try:
            self.sig_stop.emit()
        except Exception:
            pass
