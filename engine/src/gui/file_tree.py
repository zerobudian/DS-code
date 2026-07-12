
# -*- coding: utf-8 -*-
"""项目文件树 — QTreeView + QFileSystemModel

特性:
    - 树形展示项目目录
    - 支持搜索 / 过滤 / 右键菜单
    - 双击打开到编辑器
"""

import os
from PySide6.QtCore import Qt, Signal, QTimer, QDir, QModelIndex
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QTreeView, QAbstractItemView, QFileSystemModel, QMenu,
    QInputDialog, QMessageBox
)

from src.gui.style import COLORS, FONT_STACK, FONT_WEIGHTS, RADII, tr, apply_shadow


DEFAULT_IGNORES = [
    "*.git", "*.pyc", "__pycache__", "node_modules",
    "venv", ".venv", ".DS_Store", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".idea", ".vscode",
]


# ======================================================================
# FileTreeCard — 树形文件浏览器卡片
# ======================================================================

class FileTreeCard(QFrame):
    """项目文件树 — 集成搜索 + 右键菜单 + 双击打开。

    对外信号:
        file_opened[str]       - 双击文件时发出 (绝对路径)
        dir_opened[str]      - 双击目录时发出 (发出绝对路径)
        open_in_terminal[str] - 右键"在终端打开"
        file_changed[str, str] - 右键其它动作 (path, action)

    公共 API:
        set_root(path)                - 切换/设置根目录
        set_ignore(list[str])     - 自定义忽略规则
        select_file(path)          - 高亮某文件
        get_selected() -> str      - 当前选中文件
        collapse_all()            - 折叠全部
    """

    file_opened = Signal(str)
    dir_opened = Signal(str)
    open_in_terminal = Signal(str)
    file_changed = Signal(str, str)  # path, action

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_root = ""
        self._custom_ignores = list(DEFAULT_IGNORES)

        self.setObjectName("FileTreeCard")
        self.setStyleSheet(
            "QFrame#FileTreeCard{background:" + COLORS['CARD'] + ";"
            "border:1px solid " + COLORS['DIVIDER'] + ";"
            "border-radius:" + RADII['xl'] + ";}"
        )
        apply_shadow(self, 'sm')

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        # ---- 标题 + 操作按钮
        header = QHBoxLayout()
        header.setSpacing(4)

        title = QLabel(tr("file_tree_title"))
        title.setStyleSheet(
            "color:" + COLORS['TEXT'] + ";"
            "font-size:14px;font-weight:" + str(FONT_WEIGHTS['semibold']) + ";font-family:" + FONT_STACK + ";"
        )
        header.addWidget(title, 1)

        def _tool_btn(tip, cb):
            btn = QPushButton()
            btn.setFixedSize(28, 28)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tip)
            btn.setStyleSheet(
                "QPushButton{background:transparent;color:" + COLORS['TEXT_3'] + ";"
                "border:none;border-radius:" + RADII['sm'] + ";}"
                "QPushButton:hover{background:" + COLORS['SURFACE'] + ";color:" + COLORS['TEXT'] + ";}"
            )
            btn.clicked.connect(cb)
            return btn

        # 刷新按钮
        from src.gui._gui_helpers import _load_icon
        from PySide6.QtCore import QSize
        refresh_btn = _tool_btn("刷新", self.refresh)
        refresh_btn.setIcon(_load_icon("refresh_16.png", 14))
        refresh_btn.setIconSize(QSize(14, 14))
        header.addWidget(refresh_btn)

        # 折叠全部
        col_btn = _tool_btn("折叠全部", self.collapse_all)
        col_btn.setIcon(_load_icon("chevron_up_16.png", 14))
        col_btn.setIconSize(QSize(14, 14))
        header.addWidget(col_btn)

        # 新建
        new_btn = _tool_btn("新建文件", self._on_new_file)
        new_btn.setIcon(_load_icon("plus_16.png", 14))
        new_btn.setIconSize(QSize(14, 14))
        header.addWidget(new_btn)
        root.addLayout(header)

        # 分隔线
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(
            "QFrame{background:" + COLORS['DIVIDER'] + ";max-height:1px;min-height:1px;border:none;}"
        )
        root.addWidget(line)

        # 搜索框
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("搜索文件 / 目录…")
        self.search_box.setFixedHeight(34)
        self.search_box.setStyleSheet(
            "QLineEdit{background:" + COLORS['SURFACE'] + ";"
            "color:" + COLORS['TEXT'] + ";border:none;"
            "border-radius:" + RADII['md'] + ";"
            "padding: 0 12px;"
            "font-size:13px;font-family:" + FONT_STACK + ";}"
            "QLineEdit:focus{background:" + COLORS['PRIM'] + ";}"
        )
        self.search_box.textChanged.connect(self._on_search_changed)
        root.addWidget(self.search_box)

        # 树形视图
        self.tree = QTreeView()
        self.tree.setFrameShape(QFrame.NoFrame)
        self.tree.setFocusPolicy(Qt.NoFocus)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setUniformRowHeights(False)
        self.tree.setAlternatingRowColors(False)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        self.tree.doubleClicked.connect(self._on_double_clicked)
        self.tree.setStyleSheet(
            "QTreeView{background:transparent;border:none;outline:0px;"
            "font-family:" + FONT_STACK + ";font-size:13px;}"
            "QTreeView::item{height:28px;padding:2px 8px;border-radius:" + RADII['sm'] + ";}"
            "QTreeView::item:selected{background:" + COLORS['PRIM'] + ";color:" + COLORS['TEXT'] + ";}"
            "QTreeView::item:hover{background:" + COLORS['SURFACE'] + ";}"
            "QTreeView::branch{background:transparent;}"
            "QScrollBar:vertical{background:transparent;width:6px;margin:2px 0;}"
            "QScrollBar::handle:vertical{background:" + COLORS['BORDER'] + ";border-radius:3px;min-height:36px;}"
            "QScrollBar::handle:vertical:hover{background:" + COLORS['TEXT_4'] + ";}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0px;}"
        )
        root.addWidget(self.tree, 1)

        # 文件系统模型
        self.model = QFileSystemModel(self.tree)
        self.tree.setModel(self.model)

        # 展开/折叠节点时使用平滑动画（QTreeView 内部使用 QPropertyAnimation），禁止硬切
        self.tree.setAnimated(True)

        # 隐藏"大小 / 类型 / 修改时间" 三列，只保留路径
        self.tree.setHeaderHidden(True)
        for col in [1, 2, 3]:
            self.tree.hideColumn(col)

        # 不设置默认根目录（等项目加载后通过 set_root 设置到项目 work 目录）
        self._current_root = None

        # 状态 / 空提示
        self._empty_label = QLabel("暂无文件")
        self._empty_label.setStyleSheet(
            "color:" + COLORS['TEXT_4'] + ";"
            "font-size:13px;font-family:" + FONT_STACK + ";"
        )
        self._empty_label.setAlignment(Qt.AlignCenter)
        root.addWidget(self._empty_label)
        self._empty_label.setVisible(False)

        # 搜索节流 (debounce 200ms)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._apply_search_filter)

    # ---- 公共 API ----
    def set_root(self, path):
        """切换根目录"""
        if not path or not os.path.isdir(path):
            return
        self._current_root = path
        self.model.setRootPath(path)
        self.model.setFilter(
            QDir.AllDirs | QDir.Files | QDir.NoDotAndDotDot
        )
        # 注意：QFileSystemModel.setNameFilters 是白名单（只显示匹配文件），
        # 不应用于忽略。忽略逻辑在 _visit_tree 和目录遍历中处理。
        # 不调用 setNameFilters，避免源文件被禁用。
        self.tree.setRootIndex(self.model.index(path))
        # 隐藏第二三四列
        for col in [1, 2, 3]:
            self.tree.hideColumn(col)
        # 默认展开一层
        QTimer.singleShot(100, self._expand_one_level)

    def set_ignore(self, patterns):
        """自定义忽略模式 (如 ["*_test.py"])"""
        self._custom_ignores = list(DEFAULT_IGNORES) + list(patterns)
        if self._current_root:
            self.set_root(self._current_root)

    def select_file(self, path: str):
        """选中并滚动到指定文件"""
        if not path or not os.path.exists(path):
            return
        idx = self.model.index(path)
        if idx.isValid():
            self.tree.setCurrentIndex(idx)
            self.tree.scrollTo(idx, QTreeView.PositionAtCenter)
            # 向上展开父目录
            parent = idx.parent()
            while parent.isValid():
                self.tree.expand(parent)
                parent = parent.parent()

    def get_selected(self) -> str:
        """当前选中文件的绝对路径"""
        idx = self.tree.currentIndex()
        if idx.isValid():
            return self.model.filePath(idx)
        return ""

    def refresh(self):
        """刷新根目录"""
        if self._current_root:
            self.set_root(self._current_root)

    def collapse_all(self):
        """折叠全部"""
        self.tree.collapseAll()
        if self._current_root:
            self.tree.expand(self.model.index(self._current_root))

    # ---- 内部: 搜索
    def _on_search_changed(self, text: str):
        """搜索改变 → 节流"""
        self._pending_search = text
        self._search_timer.start()

    def _apply_search_filter(self):
        """真正执行: 展开所有匹配文件的父目录并高亮"""
        text = getattr(self, "_pending_search", "").strip()
        if not text:
            # 无搜索: 还原
            self.tree.collapseAll()
            return

        # 简单策略: 遍历当前根目录下所有子项，名字匹配即展开父目录
        root_path = self._current_root
        if not root_path:
            return
        low = text.lower()
        # 递归找匹配的
        self._visit_tree(root_path, low)

    def _visit_tree(self, dir_path: str, query_low: str) -> int:
        """在给定目录下 BFS，找名字包含 query_low 的文件并展开它们的父目录"""
        matches = 0
        try:
            for entry in os.listdir(dir_path):
                full = os.path.join(dir_path, entry)
                if query_low in entry.lower():
                    # 展开此项目录
                    idx = self.model.index(full)
                    if idx.isValid():
                        parent = idx.parent()
                        while parent.isValid():
                            self.tree.expand(parent)
                            parent = parent.parent()
                        self.tree.expand(idx)
                    matches += 1
                    if matches > 50:
                        return matches
                if os.path.isdir(full) and not os.path.islink(full):
                    # 跳过忽略目录
                    dir_name = os.path.basename(full)
                    if dir_name in self._custom_ignores or dir_name.startswith("."):
                        continue
                    sub_matches = self._visit_tree(full, query_low)
                    matches += sub_matches
        except PermissionError:
            pass
        return matches

    # ---- 内部: 事件
    def _on_double_clicked(self, index: QModelIndex):
        """双击: 文件 → 发出 file_opened；目录 → 发出 dir_opened"""
        if not index.isValid():
            return
        path = self.model.filePath(index)
        if os.path.isfile(path):
            self.file_opened.emit(path)
        elif os.path.isdir(path):
            self.dir_opened.emit(path)

    def _on_context_menu(self, pos):
        """右键菜单"""
        index = self.tree.indexAt(pos)
        path = ""
        if index.isValid():
            path = self.model.filePath(index)
        else:
            path = self._current_root

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:" + COLORS['CARD'] + ";"
            "color:" + COLORS['TEXT'] + ";"
            "border:1px solid " + COLORS['BORDER'] + ";"
            "border-radius:" + RADII['lg'] + ";"
            "padding:4px;font-size:13px;"
            "font-family:" + FONT_STACK + ";}"
            "QMenu::item{padding:6px 16px;border-radius:" + RADII['xs'] + ";}"
            "QMenu::item:selected{background:" + COLORS['PRIM_LIGHT'] + ";}"
        )

        act_open = QAction("打开", self)
        act_open.triggered.connect(lambda: self.file_opened.emit(path) if os.path.isfile(path) else None)
        menu.addAction(act_open)

        act_term = QAction("在终端打开", self)
        act_term.triggered.connect(lambda: self.open_in_terminal.emit(os.path.dirname(path) if os.path.isfile(path) else path))
        menu.addAction(act_term)

        menu.addSeparator()

        act_new_file = QAction("新建文件…", self)
        act_new_file.triggered.connect(lambda: self._new_file_dialog(path))
        menu.addAction(act_new_file)

        act_new_dir = QAction("新建目录…", self)
        act_new_dir.triggered.connect(lambda: self._new_dir_dialog(path))
        menu.addAction(act_new_dir)

        menu.addSeparator()

        act_rename = QAction("重命名…", self)
        act_rename.triggered.connect(lambda: self._rename_dialog(path))
        menu.addAction(act_rename)

        act_delete = QAction("删除…", self)
        act_delete.triggered.connect(lambda: self._delete_dialog(path))
        menu.addAction(act_delete)

        # 展示
        global_pos = self.tree.viewport().mapToGlobal(pos)
        menu.exec(global_pos)

    # ---- 新建 / 重命名 / 删除
    def _on_new_file(self):
        base = self._current_root or os.path.expanduser("~")
        self._new_file_dialog(base)

    def _new_file_dialog(self, base_path: str):
        if os.path.isfile(base_path):
            base_path = os.path.dirname(base_path)
        if not base_path or not os.path.isdir(base_path):
            base_path = os.path.expanduser("~")
        name, ok = QInputDialog.getText(self, "新建文件", "文件名:")
        if not ok or not name.strip():
            return
        safe_name = name.strip()
        # 路径遍历防护：拒绝绝对路径和 .. 穿越
        if os.path.isabs(safe_name) or ".." in safe_name.split(os.sep):
            QMessageBox.warning(self, "无效名称", "文件名不能包含绝对路径或 .. 穿越符。")
            return
        full_path = os.path.join(base_path, safe_name)
        # 二次校验：确保最终路径仍在 base_path 内
        if not os.path.realpath(full_path).startswith(os.path.realpath(base_path) + os.sep):
            QMessageBox.warning(self, "无效名称", "文件路径超出项目目录范围。")
            return
        try:
            if os.path.exists(full_path):
                QMessageBox.warning(self, "已存在", f"文件 {name} 已存在。")
                return
            with open(full_path, "w", encoding="utf-8") as f:
                f.write("")
            self.file_changed.emit(full_path, "created")
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"创建失败: {e}")

    def _new_dir_dialog(self, base_path: str):
        if os.path.isfile(base_path):
            base_path = os.path.dirname(base_path)
        if not base_path or not os.path.isdir(base_path):
            base_path = os.path.expanduser("~")
        name, ok = QInputDialog.getText(self, "新建目录", "目录名:")
        if not ok or not name.strip():
            return
        safe_name = name.strip()
        if os.path.isabs(safe_name) or ".." in safe_name.split(os.sep):
            QMessageBox.warning(self, "无效名称", "目录名不能包含绝对路径或 .. 穿越符。")
            return
        full_path = os.path.join(base_path, safe_name)
        if not os.path.realpath(full_path).startswith(os.path.realpath(base_path) + os.sep):
            QMessageBox.warning(self, "无效名称", "目录路径超出项目目录范围。")
            return
        try:
            os.makedirs(full_path, exist_ok=True)
            self.file_changed.emit(full_path, "created_dir")
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"创建失败: {e}")

    def _rename_dialog(self, path: str):
        if not path or not os.path.exists(path):
            return
        base = os.path.dirname(path)
        old_name = os.path.basename(path)
        name, ok = QInputDialog.getText(self, "重命名", "新名称:", text=old_name)
        if not ok or not name.strip() or name == old_name:
            return
        safe_name = name.strip()
        if os.path.isabs(safe_name) or ".." in safe_name.split(os.sep):
            QMessageBox.warning(self, "无效名称", "名称不能包含绝对路径或 .. 穿越符。")
            return
        new_path = os.path.join(base, safe_name)
        if not os.path.realpath(new_path).startswith(os.path.realpath(base) + os.sep):
            QMessageBox.warning(self, "无效名称", "路径超出项目目录范围。")
            return
        try:
            os.rename(path, new_path)
            self.file_changed.emit(new_path, "renamed")
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"重命名失败: {e}")

    def _delete_dialog(self, path: str):
        if not path or not os.path.exists(path):
            return
        name = os.path.basename(path)
        msg = f"确认删除「{name}」?"
        confirm = QMessageBox.question(
            self, "删除确认", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            if os.path.isfile(path):
                os.remove(path)
            else:
                import shutil
                shutil.rmtree(path)
            self.file_changed.emit(path, "deleted")
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"删除失败: {e}")

    # ---- 辅助: 展开一级
    def _expand_one_level(self):
        if not self._current_root:
            return
        idx = self.model.index(self._current_root)
        if idx.isValid():
            self.tree.expand(idx)
