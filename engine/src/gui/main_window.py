# -*- coding: utf-8 -*-
"""主窗口 — 严格按 1124×748 画布 / 两栏 / " + COLORS['BGD'] + " 背景

对外信号:
  sig_send(text)              用户发送一条消息
  sig_new_project()           点击 "新对话"
  sig_project_selected(name)  选择已有项目
  sig_settings_clicked()      点击设置
  sig_stop_clicked()          用户点击停止 / 中断当前执行

对外方法:
  set_projects(names, current=None)  设置左侧项目列表
  add_user_msg(text)                 添加用户气泡
  add_ai_msg(text="")                添加 AI 气泡(返回气泡对象)
  add_tool_msg(name, result)         添加工具调用气泡
  add_plan_msg(plan_text)            显示带"确认 / 取消"的计划气泡
  add_command_confirm(call)          显示命令确认对话框(由 app.py 用)
  clear_chat()                       清空聊天
  set_todos(list[dict])              设置右侧待办
  set_files(list[dict])              设置右侧文件
  show_typing() / hide_typing()      控制打字指示器
  update_status_bar(msg)             更新顶部状态栏
  enable_input(enabled)              启用/禁用输入框(运行中禁用)

动画效果:
  - 气泡淡入 (QGraphicsOpacityEffect + QPropertyAnimation)
  - 平滑滚动到底部
  - 发送按钮按下缩放
  - 输入框 focus glow (focus-in/focus-out)
  - 按钮 hover 色渐变
"""


from __future__ import annotations

import math
import os
from PySide6.QtCore import (
    Qt, QSize, Signal, QPropertyAnimation, QEasingCurve, QTimer, QEvent, QPoint,
    Property,
)
from PySide6.QtGui import QPixmap, QIcon, QColor, QPainter, QPen, QBrush, QTextCursor
from typing import List, Optional

from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
    QPushButton, QTextEdit, QScrollArea, QSplitter,
    QGraphicsOpacityEffect, QLineEdit, QSizePolicy, QMessageBox, QDialog,
)

from src.gui.style import COLORS, RADII, SPACING, FONT_STACK, rgba, tr
from src.gui.icon_factory import get_icon
from src.gui.bubbles import (
    UserBubble, AIBubble, ToolCallBubble, PlanBubble, TypingIndicator, WelcomeCard,
    StatsCard,
)
from src.gui.tab_right_panel import TabRightPanel
from src.gui.file_tree import FileTreeCard
from src.gui.todo_card import TodoCard
from src.gui.slash_popup import SlashPopup
from src.gui.status_popup import StatusPopup
from src.gui.terminal_panel import TerminalPanel

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "assets")


# --- 共享辅助函数 -----------------------------------------------------------
# ``_darken`` / ``_load_icon`` / ``_fade_in`` 原定义于本模块，现抽取到
# ``src.gui._gui_helpers`` 以避免与 ``widgets/`` / ``panels/`` 子包产生循环
# 导入。此处重新导入并对外导出，保留历史导入路径（如
# ``from src.gui.main_window import _fade_in``）继续可用。
from src.gui._gui_helpers import _darken, _load_icon, _fade_in

# --- 抽取到 widgets/ 与 panels/ 子包的组件 ----------------------------------
from src.gui.widgets.buttons import ThreeDotsButton, TrashIcon
from src.gui.widgets.project_dialogs import (
    ConversationContextMenuPopup, DeleteConfirmDialog, RenameDialog,
    ProjectRowWidget,
)
from src.gui.panels.left_panel import LeftPanel
from src.gui.panels.status_bar import StatusBar
from src.gui.panels.chat_view import ChatView
from src.gui.panels.input_area import InputArea


# ============================================================================
# 主窗口
# ============================================================================
class MainWindow(QMainWindow):

    sig_send = Signal(str)
    sig_new_project = Signal()
    sig_open_project = Signal()
    sig_project_selected = Signal(str)
    sig_project_deleted = Signal(str)
    sig_project_pinned = Signal(str, bool)
    sig_project_renamed = Signal(str, str)
    sig_settings_clicked = Signal()
    sig_stop_clicked = Signal()
    engine_refresh_requested = Signal()  # 模型/密钥变更后请求刷新引擎 API 客户端

    def __init__(self, language="zh"):
        super().__init__()
        self._language = language if language else "zh"
        self._project_dir = None
        self._init_icons()
        self._init_window_geometry()
        self._init_central_layout()
        self._init_left_panel()
        self._init_middle_area()
        self._init_right_panel()
        self._init_terminal_panel()
        self._init_status_defaults()

    def _init_icons(self):
        """确保图标资源存在（若无则动态生成）。"""
        try:
            from src.gui.icon_factory import ensure_icons
            ensure_icons()
        except Exception:
            pass

    def _init_window_geometry(self):
        """设置窗口标题、默认尺寸与最小尺寸。"""
        self.setWindowTitle("DS Code")
        self.resize(1200, 800)
        self.setMinimumSize(960, 600)
        # 不强制宽高比，不设置最大尺寸——现代桌面应用让用户自由调整

    def _init_central_layout(self):
        """创建中央 QWidget、根布局与两栏水平分割器。"""
        central = QWidget()
        central.setObjectName("central")
        central.setStyleSheet(
            "QWidget#central{background:" + COLORS['BGD'] + ";}"
        )
        self.setCentralWidget(central)
        self._central = central

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._root_layout = root

        # 两栏水平分割器：左栏宽度固定，中间自适应拉伸
        splitter = QSplitter(Qt.Horizontal, central)
        splitter.setHandleWidth(1)
        splitter.setChildrenCollapsible(False)
        splitter.setStyleSheet(
            "QSplitter{background:" + COLORS['BGD'] + ";}"
            "QSplitter::handle{background:" + COLORS['DIVIDER'] + ";width:1px;}"
            "QSplitter::handle:hover{background:" + COLORS['ACCENT'] + ";}"
        )
        self._splitter = splitter

    def _init_left_panel(self):
        """初始化左栏会话列表面板并连接信号。"""
        self.left_panel = LeftPanel(language=self._language)
        self.left_panel.new_project_clicked.connect(self.sig_new_project.emit)
        self.left_panel.project_selected.connect(self.sig_project_selected.emit)
        self.left_panel.project_deleted.connect(self.sig_project_deleted.emit)
        self.left_panel.project_pinned.connect(self.sig_project_pinned.emit)
        self.left_panel.project_renamed.connect(self.sig_project_renamed.emit)
        self.left_panel.settings_clicked.connect(self.sig_settings_clicked.emit)
        self.left_panel.theme_toggled.connect(self.apply_theme_colors)
        self._splitter.addWidget(self.left_panel)

    def _init_middle_area(self):
        """初始化中间主区域：聊天滚动区 + 状态栏 + 输入区。"""
        middle_widget = QWidget()
        middle_widget.setStyleSheet(f"background:{COLORS['BGD']};")
        middle = QVBoxLayout(middle_widget)
        middle.setContentsMargins(20, 16, 20, 16)
        middle.setSpacing(10)

        self.chat = ChatView()
        middle.addWidget(self.chat, 1)

        # 紧凑状态栏 — 对话中显示当前模型/权限/余额信息
        self.status = StatusBar()
        self.status.setFixedHeight(52)
        middle.addWidget(self.status)

        self.input_area = InputArea()

        # 欢迎卡片 suggestion chip 点击 → 填入输入框并聚焦
        self.chat.sig_suggestion_clicked.connect(self._on_suggestion_clicked)
        self.chat.sig_create_project.connect(self.sig_new_project.emit)
        self.chat.sig_open_project.connect(self.sig_open_project.emit)
        self.input_area.send_clicked.connect(self._on_user_input)
        self.input_area.stop_clicked.connect(self.sig_stop_clicked.emit)
        self.input_area.sig_model_label_clicked.connect(self._on_status_popup)
        # 用户上传文件 → 隐藏统计卡片
        self.input_area.user_activity.connect(self.chat.hide_stats_if_visible)
        # 从配置初始化标签显示
        try:
            from src.core.config import Config
            _cfg = Config()
            self.input_area.set_current_model(_cfg.model or "deepseek-v4-pro")
            self.input_area.set_current_permission(
                _cfg.get("permission", "full")
            )
            self.input_area.set_current_mode(_cfg.get("chat_mode", "normal"))
            self.input_area.set_current_reasoning(_cfg.get("reasoning_effort", "high"))
        except Exception:
            pass
        middle.addWidget(self.input_area, 0)

        self._splitter.addWidget(middle_widget)

    def _init_right_panel(self):
        """初始化右栏对象但不再加入可见布局（当前设计为两栏）。"""
        self.right_panel = TabRightPanel()
        self.right_panel.setFixedWidth(280)
        # 创建 FileTreeCard 和 TodoCard 并注入
        self._file_tree_card = FileTreeCard()
        self._todo_card = TodoCard()
        self.right_panel.attach_file_tree(self._file_tree_card)
        self.right_panel.attach_todo_card(self._todo_card)
        # 右侧文件/待办面板当前不显示
        self.right_panel.setVisible(False)

        self._splitter.setSizes([260, 940])
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)

        self._root_layout.addWidget(self._splitter, 1)

    def _init_terminal_panel(self):
        """初始化底部内置终端面板对象但不再加入可见布局（默认隐藏）。"""
        self.terminal_panel = TerminalPanel(cwd=None, parent=self._central)
        self.terminal_panel.setVisible(False)
        self.terminal_panel.setFixedHeight(240)
        # 底部终端面板当前不显示

    def _init_status_defaults(self):
        """设置状态栏默认值。"""
        self.status.set_model("V4 Pro")
        self.status.set_balance(0.0)
        self.status.set_cache_rate(0)
        self.status.set_mode("normal")
        self.status.set_permission("full")

        # 点击状态栏打开快捷设置
        self.status.clicked.connect(self._on_status_popup)

    def resizeEvent(self, event):
        """默认 resize 处理（不再强制宽高比）。"""
        super().resizeEvent(event)

    def closeEvent(self, event):
        """窗口关闭时清理资源：停止终端、终止子进程。"""
        # P1-1: 关闭前 flush 所有待保存的 Config 变更
        try:
            from src.core.config import Config
            Config()._flush_pending_save()
        except Exception:
            pass
        try:
            if hasattr(self, 'terminal_panel') and self.terminal_panel is not None:
                self.terminal_panel.shutdown()
        except Exception:
            pass
        super().closeEvent(event)

    # ---- 外部 API ----
    def set_projects(self, names, current=None):
        """兼容旧接口：接受目录名列表。"""
        self.left_panel.set_projects(names, current)

    @property
    def chat_view(self):
        """兼容 app.py 的 chat_view 别名 — 返回 self.chat。"""
        return self.chat

    def set_project_infos(self, infos, current=None):
        """使用项目信息字典列表设置左侧会话列表（支持显示名、置顶）。"""
        self.left_panel.set_project_infos(infos, current)

    def set_project_list(self, names):
        self.left_panel.set_projects(names, None)

    def set_window_title(self, title: str):
        """设置窗口标题，保留应用名前缀。"""
        text = str(title) if title else "DS Code"
        self.setWindowTitle(f"{text} - DS Code")

    def set_project_dir(self, project_dir: str):
        """设置当前项目目录并同步给输入区 Git 按钮和终端面板。"""
        self._project_dir = str(project_dir) if project_dir else None
        try:
            self.input_area.set_project_dir(project_dir)
        except Exception:
            pass
        try:
            if self._project_dir and os.path.isdir(self._project_dir):
                self.terminal_panel.set_cwd(self._project_dir)
        except Exception:
            pass

    def toggle_terminal(self):
        """显示/隐藏底部终端面板。"""
        try:
            visible = not self.terminal_panel.isVisible()
            self.terminal_panel.setVisible(visible)
            if visible and self._project_dir:
                self.terminal_panel.set_cwd(self._project_dir)
        except Exception:
            pass

    def show_terminal(self):
        """强制显示底部终端面板。"""
        try:
            if not self.terminal_panel.isVisible():
                self.terminal_panel.setVisible(True)
            if self._project_dir:
                self.terminal_panel.set_cwd(self._project_dir)
        except Exception:
            pass

    def add_user_msg(self, text: str):
        self.chat.add_user(text)

    def add_ai_msg(self, text: str = ""):
        return self.chat.add_ai(text)

    def add_tool_msg(self, tool_name: str, result: str = ""):
        return self.chat.add_tool(tool_name, result)

    def add_plan_msg(self, plan_text: str):
        """显示计划气泡（带确认/取消按钮），返回 PlanBubble 以便连接其信号"""
        return self.chat.add_plan(plan_text)

    def clear_chat(self):
        self.chat.clear_all()
        # 清空后重新随机一条问候语 — 让每次点"新对话"都有新鲜感
        try:
            lang = getattr(self, "_current_language", "zh")
            self.chat.refresh_greeting(lang)
        except Exception:
            pass

    def set_greeting_language(self, language: str = "zh"):
        """设置当前界面语言，为后续刷新问候语做准备"""
        self._current_language = language
        try:
            self.chat._language = language
        except Exception:
            pass

    def retranslate_ui(self, language: str = None):
        """语言切换后刷新所有可见 UI 文本。

        先调用 I18N.set_language() 触发已注册的回调，再手动遍历尚未连接
        I18N.on_language_changed 的子面板完成翻译。
        """
        from src.gui.style import tr, I18N
        if language is None:
            language = I18N.current_language()
        self._language = language
        self._current_language = language
        self.setWindowTitle(tr("app_name"))
        # 触发 I18N 语言变更，让已注册回调的子面板自动翻译
        try:
            I18N.set_language(language)
        except Exception:
            pass
        # 以下子面板尚未连接 I18N 信号，仍需手动通知
        _panels = [
            getattr(self, 'left_panel', None),
            getattr(self, 'input_area', None),
            getattr(self, 'status', None),
            getattr(self, 'right_panel', None),
        ]
        for panel in _panels:
            try:
                if panel is not None and hasattr(panel, 'retranslate_ui'):
                    panel.retranslate_ui(language)
            except Exception:
                pass
        # 聊天区域问候语（跨面板逻辑，MainWindow 负责协调）
        try:
            self.chat.refresh_greeting(language)
        except Exception:
            pass

    def show_typing(self):
        self.chat.show_typing()
        self.input_area.set_running(True)
        try:
            self.input_area.show_token_stats()
        except Exception:
            pass

    def hide_typing(self):
        self.chat.hide_typing()
        self.input_area.set_running(False)
        try:
            self.input_area.hide_token_stats()
        except Exception:
            pass

    def update_usage(self, usage: dict):
        """更新 token 统计（每次 API 调用后由引擎回调）。"""
        try:
            completion_tokens = int(usage.get('completion_tokens', 0))
            if completion_tokens > 0:
                self.input_area.add_stream_delta(completion_tokens)
        except Exception:
            pass

    def update_tool_status(self, tool_name: str, status: str):
        """更新工具状态显示。"""
        try:
            self.chat_view.add_tool_status(tool_name, status)
        except Exception:
            pass

    def update_status_bar(self, msg: str):
        """更新状态栏消息 (4 秒后自动恢复为模型名显示)"""
        if not msg:
            return
        try:
            self.status.set_message(str(msg))
            self.status.setVisible(True)
        except Exception:
            pass
        def _restore():
            try:
                if self.isVisible():
                    model_name = getattr(self.input_area, "_current_model", None) or "deepseek-v4-pro"
                    self.status.set_model(model_name)
            except Exception:
                pass
        QTimer.singleShot(4000, _restore)

    def set_model_label(self, model_name: str):
        """设置模型名称（同步到状态栏和输入区）。"""
        try:
            self.status.set_model(str(model_name))
        except Exception:
            pass
        try:
            self.input_area.set_current_model(str(model_name))
        except Exception:
            pass

    def set_current_mode(self, mode: str):
        """同步当前模式到状态栏。"""
        try:
            self.input_area.set_current_mode(str(mode))
        except Exception:
            pass
        try:
            self.status.set_mode(str(mode))
        except Exception:
            pass

    def set_status_usage(self, cost, cache_hit_rate):
        """更新状态栏的用量信息（成本、缓存命中率）。"""
        try:
            self.status.set_usage(cost, cache_hit_rate)
        except Exception:
            pass

    def set_status_permission(self, perm: str):
        """更新状态栏的权限显示。"""
        try:
            self.status.set_permission(str(perm))
        except Exception:
            pass

    def set_code_index_status(self, status: str):
        """更新代码索引状态 badge（状态栏 + 输入卡片右上角）。"""
        try:
            self.status.set_index_status(status)
        except Exception:
            pass
        try:
            self.input_area.set_index_status(status)
        except Exception:
            pass

    def set_status_bar(self, model=None, balance=None, cache_rate=None, vendor=None):
        """一键更新统计卡片（model/balance/cache_rate/vendor 均为可选）。数据路由到可见的 StatsCard。"""
        try:
            stats = {}
            if model is not None:
                stats['model'] = str(model)
            if balance is not None:
                stats['balance'] = float(balance)
            if cache_rate is not None:
                try:
                    stats['hit_rate'] = int(float(cache_rate))
                except (TypeError, ValueError):
                    pass
            if vendor is not None:
                stats['provider'] = str(vendor)
            if stats and hasattr(self.chat, 'stats_card') and self.chat.stats_card:
                self.chat.stats_card.set_stats(stats)
        except Exception:
            pass

    def set_todos(self, todo_list):
        """更新待办事项列表（代理到右侧面板TodoCard）。"""
        try:
            normalized = []
            for t in todo_list or []:
                if isinstance(t, dict):
                    normalized.append({
                        "text": str(t.get("text", t.get("task", ""))),
                        "done": bool(t.get("done", False)),
                    })
                else:
                    normalized.append({"text": str(t), "done": False})
            self.right_panel.set_todos(normalized)
            # 如果有待办，自动切换到待办Tab
            if normalized:
                self.right_panel.show_todos_tab()
        except Exception:
            pass

    def add_todo(self, text: str):
        """添加单个待办事项。"""
        try:
            self.right_panel.add_todo(text)
            self.right_panel.show_todos_tab()
        except Exception:
            pass

    def set_files(self, file_list):
        """set_files 不再使用扁平列表（FileTreeCard 通过 QFileSystemModel 自动展示文件树）。
        此方法保留为兼容空操作，文件展示通过 set_project_root 驱动。"""
        pass

    def set_project_root(self, root_path: str):
        """设置项目根目录，驱动文件树显示。"""
        try:
            self.right_panel.set_project_root(root_path)
        except Exception:
            pass

    def refresh_file_tree(self):
        """触发文件树刷新（供watchdog回调使用）。"""
        try:
            self.right_panel.refresh_file_tree()
        except Exception:
            pass

    # ---- 新气泡 API（计划之外补全功能）----
    def add_file_preview_msg(self, filepath: str, content: str):
        return self.chat.add_file_preview(filepath, content)

    def add_diff_preview_msg(self, filepath: str, diff_text: str):
        return self.chat.add_diff_preview(filepath, diff_text)

    def add_image_msg(self, image_data_url: str):
        return self.chat.add_image(image_data_url)

    def add_user_attachments_msg(self, images=None, files=None):
        return self.chat.add_user_attachments(images=images, files=files)

    def add_streaming_command_msg(self, command: str):
        return self.chat.add_streaming_command(command)

    def add_status_line(self, text: str, status_type: str = "writing"):
        """追加一行彩色状态文本到对话区"""
        return self.chat.add_status_line(text, status_type)

    def get_mentioned_files(self) -> list:
        try:
            return self.input_area.get_mentioned_files()
        except Exception:
            return []

    def get_uploaded_images(self) -> list:
        try:
            return self.input_area.get_images()
        except Exception:
            return []

    def attach_image_to_input(self, image_data_url: str):
        try:
            self.input_area.add_image(image_data_url)
        except Exception:
            pass

    def get_uploaded_files(self) -> list:
        """获取用户拖入的非图片文件列表 — 作为 @filepath 注入。"""
        try:
            return self.input_area.get_uploaded_files()
        except Exception:
            return []

    def clear_all_uploads(self) -> None:
        """发送后清空所有上传。"""
        try:
            self.input_area.clear_all_uploads()
        except Exception:
            pass

    # ---- 主题/语言切换 ----
    def apply_theme_colors(self, theme: str):
        """按 'light' / 'dark' / 'system' 切换全局主题。

        apply_theme() 内部调用 ThemeManager.instance().apply_theme(theme)，
        后者发射 theme_changed 信号；所有子面板（LeftPanel、ChatView、
        InputArea、TabRightPanel、TerminalPanel、StatusPopup 等）均已连接
        该信号并各自 rebuilt_styles()，MainWindow 不再手动遍历。
        """
        try:
            import src.gui.style as _style
            _style.apply_theme(theme)
        except Exception:
            pass
        # 强制刷新主窗口及 central widget 的 Qt 样式缓存
        try:
            cw = self.centralWidget()
            if cw is not None:
                cw.style().unpolish(cw)
                cw.style().polish(cw)
        except Exception:
            pass
        try:
            self.style().unpolish(self)
            self.style().polish(self)
        except Exception:
            pass

    # ---- 内部 ----
    def _on_suggestion_clicked(self, prompt: str):
        """欢迎卡片 suggestion chip 点击：填入输入框并聚焦。"""
        try:
            self.input_area.input.setPlainText(prompt)
            self.input_area.input.moveCursor(self.input_area.input.textCursor().End)
            self.input_area.input.setFocus()
            self.chat.hide_stats_if_visible()
        except Exception:
            pass

    def _on_user_input(self, text: str):
        if text and text.strip():
            self.sig_send.emit(text.strip())

    def _on_status_popup(self):
        """点击输入区标签 → 弹出轻型快捷设置对话框。"""
        try:
            dlg = _QuickSettingsDialog(self)
            dlg.exec()
        except Exception:
            pass

    @staticmethod
    def _permission_display_name(value: str) -> str:
        return {
            "reviewer": tr("perm_reviewer"),
            "coder": tr("perm_coder"),
            "full": tr("perm_full"),
        }.get(str(value).lower(), str(value))

    def keyPressEvent(self, event):
        """全局快捷键：Esc 关闭 slash 弹窗/弹窗；Ctrl+B 切换侧边栏。"""
        try:
            key = event.key()
            modifiers = event.modifiers()

            # Esc：优先关闭 slash 命令面板
            if key == Qt.Key_Escape:
                slash_closed = False
                try:
                    popup = getattr(self.input_area, "_slash_popup", None)
                    if popup is not None and popup.isVisible():
                        popup.hide_popup()
                        slash_closed = True
                except Exception:
                    pass
                if slash_closed:
                    event.accept()
                    return

            # Ctrl+B：折叠/展开左侧边栏
            if key == Qt.Key_B and (modifiers & Qt.ControlModifier):
                try:
                    if self.left_panel._expanded:
                        self.left_panel.collapse()
                    else:
                        self.left_panel.expand()
                    event.accept()
                    return
                except Exception:
                    pass
        except Exception:
            pass
        super().keyPressEvent(event)

    def focus_input(self) -> None:
        """公共接口：让输入框获得焦点。"""
        try:
            self.input_area.focus_input()
        except Exception:
            pass

    def toggle_sidebar(self) -> None:
        """公共接口：切换左侧边栏展开/折叠。"""
        try:
            if self.left_panel._expanded:
                self.left_panel.collapse()
            else:
                self.left_panel.expand()
        except Exception:
            pass


# ==================================================================
# 轻型快捷设置对话框（内联，不依赖 status_popup.py）
# ==================================================================
class _QuickSettingsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("快捷设置")
        self.setFixedSize(300, 230)
        self.setModal(True)

        from src.core.config import Config as _Config
        self.cfg = parent.cfg if hasattr(parent, 'cfg') else _Config()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(6)

        lay.addWidget(self._section("模型"))
        lay.addWidget(self._row(["deepseek-v4-flash", "deepseek-v4-pro"], "model",
                                labels=["V4 Flash (快)", "V4 Pro (强)"]))
        lay.addWidget(self._section("思考深度"))
        lay.addWidget(self._row(["high", "max"], "reasoning", labels=["高", "最大"]))
        lay.addWidget(self._section("权限"))
        lay.addWidget(self._row(["full", "coder", "reviewer"], "permission",
                                labels=["完全", "仅编码", "仅审查"]))
        lay.addWidget(self._section("模式"))
        lay.addWidget(self._row(["normal", "plan", "ultra"], "mode",
                                labels=["普通", "规划", "Ultra"]))

        # 关闭按钮
        close_btn = QPushButton("关闭")
        close_btn.setFixedHeight(24)
        close_btn.setStyleSheet(
            "QPushButton{background:transparent;color:" + COLORS['TEXT_3'] + ";border:1px solid "
            + COLORS['BORDER'] + ";border-radius:4px;font-size:11px;padding:0 16px;}"
            "QPushButton:hover{background:" + COLORS['PRIM_LIGHT'] + ";}"
        )
        close_btn.clicked.connect(self.reject)
        lay.addWidget(close_btn, 0, Qt.AlignRight)

        # 居中于父窗口
        if parent:
            pg = parent.geometry()
            self.move(pg.center() - self.rect().center())

    def _section(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "color:" + COLORS['TEXT_3'] + ";font-size:10px;font-weight:600;"
            "font-family:" + FONT_STACK + ";background:transparent;"
        )
        return lbl

    def _row(self, options, key, labels=None):
        w = QWidget()
        w.setStyleSheet("background:transparent;")
        r = QHBoxLayout(w)
        r.setContentsMargins(0, 0, 0, 0)
        r.setSpacing(4)
        cur = {"model": self.cfg.model or "deepseek-v4-pro",
               "reasoning": self.cfg.reasoning_effort or "high",
               "permission": self.cfg.permission_mode or "full",
               "mode": self.cfg.get("chat_mode", "normal")}.get(key, options[0])
        for i, val in enumerate(options):
            btn = QPushButton(labels[i] if labels and i < len(labels) else val)
            btn.setCheckable(True)
            btn.setChecked(str(val) == str(cur))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(28)
            btn.setStyleSheet(
                "QPushButton{background:" + COLORS['SURFACE'] + ";color:" + COLORS['TEXT_2'] + ";"
                "border:1px solid " + COLORS['BORDER'] + ";border-radius:5px;"
                "font-size:11px;font-family:" + FONT_STACK + ";padding:0 12px;}"
                "QPushButton:checked{background:" + COLORS['ACCENT'] + ";color:" + COLORS['TEXT_ON_ACCENT'] + ";"
                "border-color:" + COLORS['ACCENT'] + ";}"
                "QPushButton:hover:!checked{background:" + COLORS['PRIM_LIGHT'] + ";}"
            )
            btn.clicked.connect(lambda chk, v=val, k=key: self._apply(k, v))
            r.addWidget(btn, 1)
        return w

    def _apply(self, key, val):
        if key == "model":
            self.cfg.model = str(val)
        elif key == "reasoning":
            self.cfg.data["reasoning_effort"] = str(val)
        elif key == "permission":
            self.cfg.permission_mode = str(val)
        elif key == "mode":
            self.cfg.data["chat_mode"] = str(val)
        try:
            self.cfg.save()
        except Exception:
            pass
        w = self.parentWidget()
        if w and hasattr(w, 'engine_refresh_requested'):
            w.engine_refresh_requested.emit()
        if w and hasattr(w, 'input_area'):
            w.input_area.set_current_model(self.cfg.model or "deepseek-v4-pro")
            w.input_area.set_current_permission(self.cfg.permission_mode or "full")
            w.input_area.set_current_mode(self.cfg.get("chat_mode", "normal"))
            w.input_area.set_current_reasoning(self.cfg.reasoning_effort or "high")
        # 同步刷新状态栏
        if w and hasattr(w, 'status'):
            w.status.set_model(self.cfg.model or "deepseek-v4-pro")
            w.status.set_mode(self.cfg.get("chat_mode", "normal"))
            w.status.set_permission(self.cfg.permission_mode or "full")
        self.accept()


__all__ = [
    "MainWindow",
    "InputArea",
    "ChatView",
    "StatusBar",
    "LeftPanel",
    "ThreeDotsButton",
    "TrashIcon",
    "ConversationContextMenuPopup",
    "DeleteConfirmDialog",
    "RenameDialog",
    "ProjectRowWidget",
    "_fade_in",
    "_load_icon",
    "_darken",
]
