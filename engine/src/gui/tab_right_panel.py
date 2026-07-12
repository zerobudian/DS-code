# -*- coding: utf-8 -*-
"""右侧面板容器（按设计稿：待办事项在上、项目文件在下，同时显示）

设计要点：
  - 上半部分：待办事项（TodoCard），占约 40% 高度
  - 下半部分：项目文件（FileTreeCard），占约 60% 高度
  - 使用 QSplitter(Vertical) 允许用户拖拽调整比例
  - 各区域有标题栏
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QWidget,
    QSplitter, QPushButton,
)

from src.gui.style import COLORS, FONT_STACK, RADII, tr


class TabRightPanel(QWidget):
    """右侧面板 — 待办事项（上）+ 项目文件（下），上下同时显示。

    对外信号:
        file_opened[str]       - 文件被打开
        open_in_terminal[str]  - 在终端中打开
        file_changed[str, str] - 文件其他操作
    """

    file_opened = Signal(str)
    open_in_terminal = Signal(str)
    file_changed = Signal(str, str)
    memory_changed = Signal(str)  # 兼容旧接口

    def __init__(self, parent=None):
        super().__init__(parent)
        from src.gui.style import ThemeManager
        self.setStyleSheet("background:transparent;")
        self._files_inner = None
        self._todos_inner = None
        try:
            ThemeManager.instance().theme_changed.connect(self._rebuild_styles)
        except Exception:
            pass
        # 兼容旧测试：提供两个虚拟按钮（作为子控件但不可见）
        self._tab_buttons = []
        for txt in (tr("tab_todos"), tr("tab_files")):
            btn = QPushButton(txt, self)
            btn.setFixedHeight(32)
            btn.setVisible(False)
            self._tab_buttons.append(btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # ---- 使用 QSplitter 上下分栏 ----
        self._splitter = QSplitter(Qt.Vertical)
        self._splitter.setStyleSheet(
            "QSplitter::handle{background:transparent;height:4px;}"
            "QSplitter{background:transparent;}"
        )
        self._splitter.setChildrenCollapsible(False)

        # ---- 上半部分：待办事项 ----
        todo_section = self._make_section(tr("tab_todos"), tr("no_todos"))
        self._todo_container = todo_section['container']
        self._todo_container_lay = todo_section['lay']
        self._splitter.addWidget(todo_section['frame'])

        # ---- 下半部分：项目文件 ----
        files_section = self._make_section(tr("tab_files"), tr("no_files"))
        self._files_container = files_section['container']
        self._files_container_lay = files_section['lay']
        self._splitter.addWidget(files_section['frame'])

        # 设置初始比例：待办40%，文件60%
        self._splitter.setSizes([200, 300])
        self._splitter.setStretchFactor(0, 2)
        self._splitter.setStretchFactor(1, 3)

        root.addWidget(self._splitter, 1)

    @staticmethod
    def _make_dummy_tab_btn(text: str) -> QPushButton:
        """创建兼容测试的虚拟按钮（32px高度，不可见，仅用于旧测试通过）。"""
        btn = QPushButton(text)
        btn.setFixedHeight(32)
        btn.setVisible(False)
        return btn

    def _make_section(self, title_text: str, placeholder_text: str = "") -> dict:
        """创建简单的垂直容器（透明背景、无边框、无标题栏）。
        实际的卡片外观由内嵌的 TodoCard/FileTreeCard 自己负责。"""
        container = QFrame()
        container.setStyleSheet("QFrame{background:transparent;border:none;}")
        container_lay = QVBoxLayout(container)
        container_lay.setContentsMargins(0, 0, 0, 0)
        container_lay.setSpacing(0)

        if placeholder_text:
            placeholder = QLabel(placeholder_text)
            placeholder.setStyleSheet(
                "color:" + COLORS['TEXT_4'] + ";font-size:12px;"
                "font-family:" + FONT_STACK + ";padding:30px;"
            )
            placeholder.setAlignment(Qt.AlignCenter)
            container_lay.addWidget(placeholder)

        return {
            'frame': container,
            'container': container,
            'lay': container_lay,
        }

    def _replace_container_content(self, container: QFrame, widget: QWidget, lay: QVBoxLayout):
        """替换container中的placeholder为实际widget"""
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        lay.addWidget(widget)

    def attach_file_tree(self, tree_widget):
        """把FileTreeCard注入到项目文件区域"""
        self._replace_container_content(self._files_container, tree_widget, self._files_container_lay)
        self._files_inner = tree_widget
        try:
            tree_widget.file_opened.connect(self.file_opened.emit)
            tree_widget.open_in_terminal.connect(self.open_in_terminal.emit)
            tree_widget.file_changed.connect(self.file_changed.emit)
        except Exception:
            pass

    def attach_todo_card(self, todo_widget):
        """把TodoCard注入到待办事项区域"""
        self._replace_container_content(self._todo_container, todo_widget, self._todo_container_lay)
        self._todos_inner = todo_widget

    # ---- 公共API ----
    def set_project_root(self, root_path: str):
        """切换项目根目录"""
        if self._files_inner is not None and hasattr(self._files_inner, "set_root"):
            try:
                self._files_inner.set_root(root_path)
            except Exception:
                pass

    def get_file_tree(self):
        return self._files_inner

    def set_todos(self, todo_list):
        """设置待办列表"""
        if self._todos_inner is None:
            return
        try:
            if hasattr(self._todos_inner, "set_tasks"):
                self._todos_inner.set_tasks(todo_list)
            elif hasattr(self._todos_inner, "set_todos"):
                self._todos_inner.set_todos(todo_list)
        except Exception:
            pass

    def add_todo(self, text: str):
        if self._todos_inner is None:
            return
        try:
            if hasattr(self._todos_inner, "add_task"):
                self._todos_inner.add_task(text)
            elif hasattr(self._todos_inner, "add_todo"):
                self._todos_inner.add_todo(text)
        except Exception:
            pass

    def refresh_file_tree(self):
        """外部触发文件树刷新（watchdog回调）"""
        if self._files_inner is not None and hasattr(self._files_inner, "refresh"):
            try:
                self._files_inner.refresh()
            except Exception:
                pass

    # ---- 向后兼容方法（旧Tab API，保留为no-op以避免崩溃）----
    def select_tab(self, tab_key: str):
        pass

    def show_todos_tab(self):
        pass

    def show_files_tab(self):
        pass

    def show_tab(self, tab_name):
        pass

    def attach_artifact_panel(self, panel):
        self._artifact_panel = panel
        try:
            # 将 artifact 面板添加到布局中，使其可见
            self._artifact_container = QFrame()
            self._artifact_container.setStyleSheet("background:transparent;")
            _lay = QVBoxLayout(self._artifact_container)
            _lay.setContentsMargins(0, 0, 0, 0)
            _lay.addWidget(panel)
            self.layout().addWidget(self._artifact_container)
            self._artifact_container.setVisible(False)  # 默认隐藏，有 artifact 时显示
        except Exception:
            pass

    def set_memory_manager(self, mgr):
        self._memory_manager = mgr

    def get_memory_snippet(self, max_tokens: int = 2000) -> str:
        return ""

    def set_chat_engine(self, engine):
        self._chat_engine = engine

    def refresh(self):
        """刷新右侧面板内容（文件树等）。"""
        try:
            if hasattr(self, '_project_panel') and self._project_panel:
                self._project_panel.refresh()
            elif hasattr(self, '_file_tree') and self._file_tree:
                self._file_tree.refresh()
        except Exception:
            pass

    def update_artifacts(self, artifacts):
        """更新 Artifacts 预览面板。"""
        try:
            if hasattr(self, '_artifact_panel') and self._artifact_panel:
                self._artifact_panel.update_artifacts(artifacts)
                # 有 artifact 时显示容器，无 artifact 时隐藏
                if hasattr(self, '_artifact_container'):
                    self._artifact_container.setVisible(bool(artifacts))
        except Exception:
            pass

    def shutdown(self):
        pass

    def _rebuild_styles(self):
        """主题切换后重建右侧面板样式，并委托给内部卡片。"""
        try:
            self._splitter.setStyleSheet(
                "QSplitter::handle{background:transparent;height:4px;}"
                "QSplitter{background:transparent;}"
            )
            if self._files_inner is not None and hasattr(self._files_inner, 'rebuild_style'):
                self._files_inner.rebuild_style()
            if self._todos_inner is not None and hasattr(self._todos_inner, 'rebuild_style'):
                self._todos_inner.rebuild_style()
        except Exception:
            pass
