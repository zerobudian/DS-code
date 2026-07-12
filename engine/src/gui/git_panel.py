# -*- coding: utf-8 -*-
"""Git 状态弹窗面板。

显示当前分支、modified/staged/untracked 文件列表、diff 预览、
提交消息输入框、Stage / Commit / Refresh 按钮。

交互：
- 双击文件在右侧预览 diff（staged 文件使用 --cached）。
- 勾选 modified/untracked 文件后点击 Stage 进行 git add。
- 填写消息后点击 Commit 提交已 staged 改动。
"""

import os
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QTextEdit, QLineEdit, QMessageBox,
    QGroupBox, QAbstractItemView, QInputDialog,
)

from src.gui.style import COLORS, RADII, FONT_STACK
from src.tools.git_tools import (
    git_status, git_diff, git_add, git_commit, git_branch,
    git_checkout, git_branch_create,
    git_stash, git_reset, GitError,
)


class GitPanelDialog(QDialog):
    """Git 状态弹窗。"""

    def __init__(self, project_dir: str, parent=None):
        super().__init__(parent)
        self.project_dir = project_dir
        self.setWindowTitle("Git")
        self.resize(960, 640)
        self._setup_ui()
        self._refresh()

    def _setup_ui(self):
        self.setStyleSheet(
            f"QDialog{{background:{COLORS['BGD']};font-family:{FONT_STACK};}}"
            f"QLabel{{color:{COLORS['TEXT']};font-family:{FONT_STACK};background:transparent;}}"
            f"QGroupBox{{color:{COLORS['TEXT']};font-weight:600;border:1px solid {COLORS['BORDER']};"
            f"border-radius:{RADII['sm']};margin-top:10px;padding-top:8px;font-family:{FONT_STACK};}}"
            f"QGroupBox::title{{subcontrol-origin:margin;left:8px;padding:0 4px;}}"
            f"QListWidget{{background:{COLORS['CARD']};color:{COLORS['TEXT']};border:1px solid {COLORS['BORDER']};"
            f"border-radius:{RADII['xs']};font-family:{FONT_STACK};}}"
            f"QListWidget::item{{padding:4px;border:none;}}"
            f"QListWidget::item:hover{{background:{COLORS['PRIM_LIGHT']};}}"
            f"QTextEdit{{background:{COLORS['CARD']};color:{COLORS['TEXT']};border:1px solid {COLORS['BORDER']};"
            f"border-radius:{RADII['sm']};padding:10px;font-family:monospace;font-size:13px;}}"
            f"QLineEdit{{background:{COLORS['CARD']};color:{COLORS['TEXT']};border:1px solid {COLORS['BORDER']};"
            f"border-radius:{RADII['md']};padding:0 12px;height:36px;font-family:{FONT_STACK};}}"
            f"QLineEdit:focus{{border:1px solid {COLORS['PRIM_DARK']};}}"
            f"QPushButton{{background:{COLORS['PRIM']};color:{COLORS['TEXT']};border:none;"
            f"border-radius:{RADII['md']};padding:8px 16px;font-weight:600;font-family:{FONT_STACK};}}"
            f"QPushButton:hover{{background:{COLORS['PRIM_LIGHT']};}}"
            f"QPushButton:pressed{{background:{COLORS['PRIM_DARK']};color:{COLORS['CARD']};}}"
            f"QPushButton:disabled{{background:{COLORS['DISABLED']};color:{COLORS['TEXT_4']};}}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # 顶部：分支 + 快捷操作 + 刷新
        header = QHBoxLayout()
        self._branch_label = QLabel("分支: -")
        self._branch_label.setStyleSheet("font-size:14px;font-weight:600;")
        header.addWidget(self._branch_label, 1)

        self._stash_btn = QPushButton("Stash")
        self._stash_btn.setToolTip("暂存当前改动 (git stash push)")
        self._stash_btn.clicked.connect(self._stash_push)
        header.addWidget(self._stash_btn)

        self._stash_pop_btn = QPushButton("Stash Pop")
        self._stash_pop_btn.setToolTip("恢复最近一次 stash (git stash pop)")
        self._stash_pop_btn.clicked.connect(self._stash_pop)
        header.addWidget(self._stash_pop_btn)

        self._new_branch_btn = QPushButton("新建分支")
        self._new_branch_btn.setToolTip("从当前分支创建新分支")
        self._new_branch_btn.clicked.connect(self._create_branch)
        header.addWidget(self._new_branch_btn)

        self._switch_branch_btn = QPushButton("切换分支")
        self._switch_branch_btn.setToolTip("切换到已有分支")
        self._switch_branch_btn.clicked.connect(self._switch_branch)
        header.addWidget(self._switch_branch_btn)

        self._reset_btn = QPushButton("Reset")
        self._reset_btn.setToolTip("撤销所有未提交改动 (git reset --hard HEAD)")
        self._reset_btn.setStyleSheet(
            "QPushButton{background:" + COLORS['ERROR'] + ";color:" + COLORS['CARD'] + ";"
            "border:none;border-radius:" + RADII['md'] + ";padding:8px 16px;"
            "font-weight:600;font-family:" + FONT_STACK + ";}"
            "QPushButton:hover{background:" + COLORS['ERROR_DARK'] + ";}"
        )
        self._reset_btn.clicked.connect(self._reset_hard)
        header.addWidget(self._reset_btn)

        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.setToolTip("刷新 Git 状态")
        self._refresh_btn.clicked.connect(self._refresh)
        header.addWidget(self._refresh_btn)
        root.addLayout(header)

        # 主体：左列表 + 右 diff/提交
        body = QHBoxLayout()
        body.setSpacing(12)

        # 左侧列表
        left = QVBoxLayout()
        left.setSpacing(8)

        self._modified_list = self._create_file_list("modified")
        self._staged_list = self._create_file_list("staged")
        self._untracked_list = self._create_file_list("untracked")

        left.addWidget(self._wrap_group("Modified", self._modified_list))
        left.addWidget(self._wrap_group("Staged", self._staged_list))
        left.addWidget(self._wrap_group("Untracked", self._untracked_list))

        # Stage 按钮
        self._stage_btn = QPushButton("Stage 选中文件")
        self._stage_btn.setToolTip("将勾选的 modified/untracked 文件 git add")
        self._stage_btn.clicked.connect(self._stage_selected)
        left.addWidget(self._stage_btn)

        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setFixedWidth(320)
        body.addWidget(left_widget)

        # 右侧 diff + commit
        right = QVBoxLayout()
        right.setSpacing(8)

        diff_label = QLabel("Diff 预览（双击左侧文件查看）")
        diff_label.setStyleSheet("font-size:13px;font-weight:600;")
        right.addWidget(diff_label)

        self._diff_preview = QTextEdit()
        self._diff_preview.setReadOnly(True)
        self._diff_preview.setPlaceholderText("双击文件查看 diff...")
        right.addWidget(self._diff_preview, 1)

        commit_row = QHBoxLayout()
        self._msg_input = QLineEdit()
        self._msg_input.setPlaceholderText("提交消息...")
        commit_row.addWidget(self._msg_input, 1)

        self._commit_btn = QPushButton("Commit")
        self._commit_btn.setToolTip("提交已 staged 的改动")
        self._commit_btn.clicked.connect(self._commit)
        commit_row.addWidget(self._commit_btn)
        right.addLayout(commit_row)

        right_widget = QWidget()
        right_widget.setLayout(right)
        body.addWidget(right_widget, 1)

        root.addLayout(body, 1)

    def _create_file_list(self, source: str) -> QListWidget:
        lw = QListWidget()
        lw.setSelectionMode(QAbstractItemView.ExtendedSelection)
        lw.setUniformItemSizes(True)
        lw.itemDoubleClicked.connect(lambda item: self._on_file_double_clicked(item, source))
        return lw

    def _wrap_group(self, title: str, widget: QWidget) -> QGroupBox:
        box = QGroupBox(title)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)
        lay.addWidget(widget)
        return box

    def _refresh(self):
        if not self.project_dir:
            self._show_error("未设置项目目录")
            return
        try:
            branch_data = git_branch(self.project_dir)
        except GitError as exc:
            self._show_error(str(exc.message))
            self._set_enabled(False)
            return

        current = branch_data.get("current") or "(detached)"
        self._branch_label.setText(f"分支: {current}")

        try:
            status = git_status(self.project_dir)
        except GitError as exc:
            self._show_error(str(exc.message))
            # 与 git_branch 失败处理保持一致：禁用控件，避免控件停留在
            # 上一次 git_branch 失败留下的禁用状态。
            self._set_enabled(False)
            return

        self._populate(self._modified_list, status.get("modified", []), "modified")
        self._populate(self._staged_list, status.get("staged", []), "staged")
        self._populate(self._untracked_list, status.get("untracked", []), "untracked")

        self._update_group_title(self._modified_list.parent(), "Modified", len(status.get("modified", [])))
        self._update_group_title(self._staged_list.parent(), "Staged", len(status.get("staged", [])))
        self._update_group_title(self._untracked_list.parent(), "Untracked", len(status.get("untracked", [])))

        # 成功刷新后恢复控件可用状态（修复：此前 git_branch 失败时
        # 调用了 _set_enabled(False)，但成功路径从未重新启用，导致
        # 错误恢复后控件永久禁用）。
        self._set_enabled(True)

    def _update_group_title(self, group: QWidget, base: str, count: int):
        if group:
            group.setTitle(f"{base} ({count})")

    def _populate(self, lw: QListWidget, files: list, source: str):
        lw.clear()
        for path in files:
            item = QListWidgetItem(path)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, (source, path))
            lw.addItem(item)

    def _on_file_double_clicked(self, item: QListWidgetItem, source: str):
        if item is None:
            return
        path = item.text()
        if not path or not self.project_dir:
            return
        try:
            if source == "staged":
                text = git_diff(self.project_dir, cached=True, file=path)
            elif source == "modified":
                text = git_diff(self.project_dir, cached=False, file=path)
            elif source == "untracked":
                text = self._read_untracked_file(path)
            else:
                text = ""
        except GitError as exc:
            text = f"[错误] {exc.message}"
        self._diff_preview.setPlainText(text)

    def _read_untracked_file(self, path: str) -> str:
        full = os.path.join(self.project_dir, path)
        try:
            with open(full, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(8000)
            return f"# 新文件: {path}\n\n{content}"
        except Exception as exc:
            return f"# 无法读取文件: {path}\n{exc}"

    def _stage_selected(self):
        files = []
        for lw in (self._modified_list, self._untracked_list):
            for i in range(lw.count()):
                item = lw.item(i)
                if item and item.checkState() == Qt.Checked:
                    files.append(item.text())
        if not files:
            self._show_info("请先勾选要 stage 的文件")
            return
        try:
            ok, out = git_add(self.project_dir, files)
        except GitError as exc:
            self._show_error(str(exc.message))
            return
        self._refresh()
        if ok:
            self._show_info(f"已 stage {len(files)} 个文件")
        else:
            self._show_error(out)

    def _commit(self):
        message = self._msg_input.text().strip()
        if not message:
            self._show_info("请输入提交消息")
            return
        try:
            ok, out = git_commit(self.project_dir, message)
        except GitError as exc:
            self._show_error(str(exc.message))
            return
        self._msg_input.clear()
        self._refresh()
        if ok:
            self._show_info("提交成功")
        else:
            self._show_error(out)

    def _stash_push(self):
        text, ok = QInputDialog.getText(self, "Stash", "输入 stash 说明（可选）:")
        if not ok:
            return
        try:
            out = git_stash(self.project_dir, action="push", message=text or None)
        except GitError as exc:
            self._show_error(str(exc.message))
            return
        self._refresh()
        self._show_info(out or "已 stash 当前改动")

    def _stash_pop(self):
        if QMessageBox.question(
            self, "Stash Pop", "恢复最近一次 stash 到工作区？",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            out = git_stash(self.project_dir, action="pop")
        except GitError as exc:
            self._show_error(str(exc.message))
            return
        self._refresh()
        self._show_info(out or "已恢复 stash")

    def _create_branch(self):
        text, ok = QInputDialog.getText(self, "新建分支", "输入新分支名称:")
        if not ok:
            return
        branch = text.strip()
        if not branch:
            return
        try:
            out = git_branch_create(self.project_dir, branch)
        except GitError as exc:
            self._show_error(str(exc.message))
            return
        self._refresh()
        self._show_info(out or f"已创建分支 {branch}")

    def _switch_branch(self):
        try:
            data = git_branch(self.project_dir)
        except GitError as exc:
            self._show_error(str(exc.message))
            return
        branches = data.get("branches") or []
        if not branches:
            self._show_info("没有可切换的分支")
            return
        branch, ok = QInputDialog.getItem(self, "切换分支", "选择分支:", branches, 0, False)
        if not ok or not branch:
            return
        try:
            out = git_checkout(self.project_dir, branch)
        except GitError as exc:
            self._show_error(str(exc.message))
            return
        self._refresh()
        self._show_info(out or f"已切换到 {branch}")

    def _reset_hard(self):
        if QMessageBox.warning(
            self, "危险操作", "将丢弃所有未提交的改动，确定继续？",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            out = git_reset(self.project_dir, mode="hard", target="HEAD")
        except GitError as exc:
            self._show_error(str(exc.message))
            return
        self._refresh()
        self._show_info(out or "已重置到 HEAD")

    def _set_enabled(self, enabled: bool):
        for w in (
            self._modified_list, self._staged_list, self._untracked_list,
            self._stage_btn, self._commit_btn,
            self._stash_btn, self._stash_pop_btn,
            self._new_branch_btn, self._switch_branch_btn, self._reset_btn,
            self._diff_preview, self._msg_input,
        ):
            w.setEnabled(enabled)
        # 刷新按钮始终保持可用，允许用户从错误状态恢复
        self._refresh_btn.setEnabled(True)

    def _show_info(self, msg: str):
        QMessageBox.information(self, "Git", msg)

    def _show_error(self, msg: str):
        QMessageBox.warning(self, "Git 错误", msg)
