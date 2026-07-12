# -*- coding: utf-8 -*-
"""主窗口面板组件（从 main_window.py 抽取）。"""
from src.gui.panels.left_panel import LeftPanel
from src.gui.panels.status_bar import StatusBar
from src.gui.panels.chat_view import ChatView
from src.gui.panels.input_area import InputArea

__all__ = [
    "LeftPanel",
    "StatusBar",
    "ChatView",
    "InputArea",
]
