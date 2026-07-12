# -*- coding: utf-8 -*-
"""添加语言包三步向导对话框

流程:
  Step 1 — 导出英文模板 JSON
  Step 2 — 复制 AI 翻译提示词
  Step 3 — 拖拽 / 选择翻译好的 JSON 文件导入
"""

import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QWidget, QStackedWidget, QSizePolicy, QFileDialog, QMessageBox,
    QApplication, QTextEdit,
)
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QClipboard

from src.gui.style import COLORS, RADII, FONT_STACK, make_label, tr, I18N


# ============================================================
# 内部样式辅助
# ============================================================
from src.gui._gui_helpers import _darken, _prim, _prim_dark, _prim_light, _card, _surface, _border  # noqa: F401


def _text():
    return COLORS["TEXT"]


def _text3():
    return COLORS["TEXT_3"]


def _button(text, primary=False, width=None, height=44):
    btn = QPushButton(text)
    if width is not None:
        btn.setFixedWidth(width)
    btn.setFixedHeight(height)
    btn.setCursor(Qt.PointingHandCursor)
    if primary:
        btn.setStyleSheet(
            "QPushButton{background:" + _prim() + ";color:" + COLORS['CARD'] + ";"
            "border:none;border-radius:" + RADII["lg"] + ";font-size:14px;"
            "font-weight:600;font-family:" + FONT_STACK + ";padding:0 22px;}"
            "QPushButton:hover{background:" + _darken(_prim(), 0.92) + ";color:" + COLORS['CARD'] + ";}"
            "QPushButton:pressed{background:" + _prim_dark() + ";color:" + COLORS['CARD'] + ";}"
            "QPushButton:disabled{background:" + COLORS['PRIM_LIGHT'] + ";color:" + COLORS['CARD'] + ";}"
        )
    else:
        btn.setStyleSheet(
            "QPushButton{background:" + COLORS['CARD'] + ";color:" + COLORS['TEXT'] + ";"
            "border:1px solid " + _border() + ";"
            "border-radius:" + RADII["lg"] + ";font-size:14px;font-weight:500;"
            "font-family:" + FONT_STACK + ";padding:0 22px;}"
            "QPushButton:hover{background:" + _prim_light() + ";"
            "border-color:" + _prim() + ";color:" + _prim_dark() + ";}"
            "QPushButton:pressed{background:" + _prim_light() + ";}"
        )
    return btn


# ============================================================
# 步骤指示器
# ============================================================
class StepIndicator(QWidget):
    """三个圆点 + 连线，高亮当前步骤。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(40)
        self._step = 0

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addStretch(1)

        self._dots = []
        self._lines = []

        for i in range(3):
            dot = QFrame()
            dot.setFixedSize(28, 28)
            dot._idx = i
            self._dots.append(dot)
            lay.addWidget(dot, 0, Qt.AlignCenter)
            if i < 2:
                line = QFrame()
                line.setFixedSize(48, 2)
                self._lines.append(line)
                lay.addWidget(line, 0, Qt.AlignCenter)

        lay.addStretch(1)
        self._refresh()

    def set_step(self, step: int):
        self._step = max(0, min(2, step))
        self._refresh()

    def _refresh(self):
        for i, dot in enumerate(self._dots):
            if i < self._step:
                # 已完成
                dot.setStyleSheet(
                    "QFrame{background:" + COLORS["SUCCESS"] + ";border-radius:14px;}"
                )
            elif i == self._step:
                # 当前
                dot.setStyleSheet(
                    "QFrame{background:" + _prim() + ";border-radius:14px;}"
                )
            else:
                # 未到达
                dot.setStyleSheet(
                    "QFrame{background:" + _border() + ";border-radius:14px;}"
                )
        for i, line in enumerate(self._lines):
            if i < self._step:
                line.setStyleSheet("QFrame{background:" + COLORS["SUCCESS"] + ";border-radius:1px;}")
            else:
                line.setStyleSheet("QFrame{background:" + _border() + ";border-radius:1px;}")


# ============================================================
# 拖拽区域
# ============================================================
class DropZone(QFrame):
    """虚线边框拖放区域，同时支持点击选择文件。"""

    fileDropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setFixedHeight(180)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(8)
        root.addStretch(1)

        self._icon_label = QLabel("📄")
        self._icon_label.setAlignment(Qt.AlignCenter)
        self._icon_label.setStyleSheet(
            "QLabel{font-size:36px;color:" + _text3() + ";background:transparent;}"
        )
        root.addWidget(self._icon_label)

        self._hint_label = QLabel(tr("lang_pack_drop_hint"))
        self._hint_label.setAlignment(Qt.AlignCenter)
        self._hint_label.setStyleSheet(
            "QLabel{color:" + _text() + ";font-size:15px;font-weight:600;"
            "font-family:" + FONT_STACK + ";background:transparent;}"
        )
        root.addWidget(self._hint_label)

        self._sub_label = QLabel(tr("lang_pack_or_click"))
        self._sub_label.setAlignment(Qt.AlignCenter)
        self._sub_label.setStyleSheet(
            "QLabel{color:" + _text3() + ";font-size:12px;font-weight:400;"
            "font-family:" + FONT_STACK + ";background:transparent;}"
        )
        root.addWidget(self._sub_label)

        root.addStretch(1)
        self._set_normal_style()

    def _set_normal_style(self):
        self.setStyleSheet(
            "DropZone{background:" + _surface() + ";border:2px dashed " + _border() + ";"
            "border-radius:" + RADII["lg"] + ";}"
        )

    def _set_hover_style(self):
        self.setStyleSheet(
            "DropZone{background:" + COLORS["PRIM"] + ";border:2px dashed " + _prim() + ";"
            "border-radius:" + RADII["lg"] + ";}"
        )

    def set_status(self, text: str, sub_text: str = "", status: str = "normal"):
        """更新提示文字。status: normal/success/error/warning"""
        self._hint_label.setText(text)
        if sub_text:
            self._sub_label.setText(sub_text)
            self._sub_label.setVisible(True)
        else:
            self._sub_label.setVisible(False)

        colors = {
            "normal": (_text(), _text3()),
            "success": (COLORS["SUCCESS_DARK"], COLORS["SUCCESS"]),
            "error": (COLORS["ERROR_DARK"], COLORS["ERROR"]),
            "warning": (COLORS["WARNING_DARK"], COLORS["WARNING"]),
        }
        fg, sub_fg = colors.get(status, colors["normal"])
        self._hint_label.setStyleSheet(
            "QLabel{color:" + fg + ";font-size:15px;font-weight:600;"
            "font-family:" + FONT_STACK + ";background:transparent;}"
        )
        self._sub_label.setStyleSheet(
            "QLabel{color:" + sub_fg + ";font-size:12px;font-weight:400;"
            "font-family:" + FONT_STACK + ";background:transparent;}"
        )

    # ---- 拖拽事件 ----
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".json"):
                    event.acceptProposedAction()
                    self._set_hover_style()
                    self._hint_label.setText(tr("lang_pack_drop_hover"))
                    return

    def dragLeaveEvent(self, event):
        self._set_normal_style()
        self._hint_label.setText(tr("lang_pack_drop_hint"))
        self._sub_label.setText(tr("lang_pack_or_click"))

    def dropEvent(self, event: QDropEvent):
        self._set_normal_style()
        for url in event.mimeData().urls():
            if url.isLocalFile():
                path = url.toLocalFile()
                if path.lower().endswith(".json"):
                    self.fileDropped.emit(path)
                    event.acceptProposedAction()
                    return
        self.set_status(tr("lang_pack_json_filter"), status="error")

    def mousePressEvent(self, event):
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("lang_pack_select_file_title"),
            "",
            tr("lang_pack_json_filter"),
        )
        if path:
            self.fileDropped.emit(path)


# ============================================================
# 主对话框
# ============================================================
class LanguagePackDialog(QDialog):
    """添加语言包三步向导。"""

    pack_installed = Signal(str, str)  # code, name

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("lang_pack_dialog_title"))
        self.setMinimumSize(620, 560)
        self.resize(680, 600)
        self.setModal(True)
        self.setStyleSheet(
            "QDialog{background:" + COLORS["BGD"] + ";}"
        )

        self._current_step = 0
        self._imported_pack = None
        self._imported_code = ""
        self._imported_name = ""

        self._build_ui()

    # ------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 24)
        root.setSpacing(16)

        # 标题
        title = QLabel(tr("lang_pack_dialog_title"))
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            "QLabel{color:" + _text() + ";font-size:20px;font-weight:700;"
            "font-family:" + FONT_STACK + ";background:transparent;}"
        )
        root.addWidget(title)

        # 步骤指示器
        self._indicator = StepIndicator()
        root.addWidget(self._indicator)

        # 分隔线
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("QFrame{background:" + _border() + ";}")
        root.addWidget(sep)

        # QStackedWidget — 三个步骤页
        self._stack = QStackedWidget()
        self._build_step1()
        self._build_step2()
        self._build_step3()
        root.addWidget(self._stack, 1)

        # 底部导航
        nav = QHBoxLayout()
        nav.setSpacing(12)

        self._prev_btn = _button(tr("cancel"), primary=False, width=100)
        self._prev_btn.clicked.connect(self._on_prev)
        nav.addWidget(self._prev_btn)

        nav.addStretch(1)

        self._step_label = QLabel("1 / 3")
        self._step_label.setAlignment(Qt.AlignCenter)
        self._step_label.setStyleSheet(
            "QLabel{color:" + _text3() + ";font-size:13px;font-weight:500;"
            "font-family:" + FONT_STACK + ";background:transparent;}"
        )
        nav.addWidget(self._step_label)

        nav.addStretch(1)

        self._next_btn = _button(tr("ok"), primary=True, width=120)
        self._next_btn.clicked.connect(self._on_next)
        nav.addWidget(self._next_btn)

        root.addLayout(nav)
        self._go_step(0)

    # ------------------------------------------------ Step 1
    def _build_step1(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 16, 8, 8)
        lay.setSpacing(16)

        t = QLabel(tr("lang_pack_step1_title"))
        t.setStyleSheet(
            "QLabel{color:" + _text() + ";font-size:16px;font-weight:700;"
            "font-family:" + FONT_STACK + ";background:transparent;}"
        )
        lay.addWidget(t)

        d = QLabel(tr("lang_pack_step1_desc"))
        d.setWordWrap(True)
        d.setStyleSheet(
            "QLabel{color:" + _text3() + ";font-size:13px;font-weight:400;"
            "font-family:" + FONT_STACK + ";background:transparent;line-height:1.6;}"
        )
        lay.addWidget(d)

        lay.addStretch(1)

        # 导出按钮居中
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._export_btn = _button(tr("lang_pack_export_btn"), primary=True, width=220, height=48)
        self._export_btn.clicked.connect(self._on_export)
        btn_row.addWidget(self._export_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        # 导出成功提示
        self._export_status = QLabel("")
        self._export_status.setAlignment(Qt.AlignCenter)
        self._export_status.setWordWrap(True)
        self._export_status.setStyleSheet(
            "QLabel{color:" + COLORS["SUCCESS_DARK"] + ";font-size:12px;font-weight:500;"
            "font-family:" + FONT_STACK + ";background:transparent;padding:8px;"
            "background:" + COLORS["SUCCESS_LIGHT"] + ";border-radius:" + RADII["sm"] + ";}"
        )
        self._export_status.setVisible(False)
        lay.addWidget(self._export_status)

        lay.addStretch(1)
        self._stack.addWidget(page)

    # ------------------------------------------------ Step 2
    def _build_step2(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 16, 8, 8)
        lay.setSpacing(12)

        t = QLabel(tr("lang_pack_step2_title"))
        t.setStyleSheet(
            "QLabel{color:" + _text() + ";font-size:16px;font-weight:700;"
            "font-family:" + FONT_STACK + ";background:transparent;}"
        )
        lay.addWidget(t)

        d = QLabel(tr("lang_pack_step2_desc"))
        d.setWordWrap(True)
        d.setStyleSheet(
            "QLabel{color:" + _text3() + ";font-size:13px;font-weight:400;"
            "font-family:" + FONT_STACK + ";background:transparent;line-height:1.6;}"
        )
        lay.addWidget(d)

        hint = QLabel(tr("lang_pack_prompt_hint"))
        hint.setStyleSheet(
            "QLabel{color:" + _text3() + ";font-size:12px;font-weight:400;"
            "font-family:" + FONT_STACK + ";background:transparent;}"
        )
        lay.addWidget(hint)

        # 只读文本框显示提示词
        self._prompt_edit = QTextEdit()
        self._prompt_edit.setReadOnly(True)
        self._prompt_edit.setPlainText(tr("lang_pack_prompt_text"))
        self._prompt_edit.setStyleSheet(
            "QTextEdit{background:" + _surface() + ";color:" + _text() + ";"
            "border:1px solid " + _border() + ";border-radius:" + RADII["sm"] + ";"
            "padding:12px;font-size:12px;font-family:" + FONT_STACK + ";}"
        )
        self._prompt_edit.setMinimumHeight(180)
        lay.addWidget(self._prompt_edit, 1)

        # 复制按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._copy_btn = _button(tr("lang_pack_copy_prompt"), primary=True, width=180)
        self._copy_btn.clicked.connect(self._on_copy_prompt)
        btn_row.addWidget(self._copy_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        self._stack.addWidget(page)

    # ------------------------------------------------ Step 3
    def _build_step3(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 16, 8, 8)
        lay.setSpacing(12)

        t = QLabel(tr("lang_pack_step3_title"))
        t.setStyleSheet(
            "QLabel{color:" + _text() + ";font-size:16px;font-weight:700;"
            "font-family:" + FONT_STACK + ";background:transparent;}"
        )
        lay.addWidget(t)

        d = QLabel(tr("lang_pack_step3_desc"))
        d.setWordWrap(True)
        d.setStyleSheet(
            "QLabel{color:" + _text3() + ";font-size:13px;font-weight:400;"
            "font-family:" + FONT_STACK + ";background:transparent;line-height:1.6;}"
        )
        lay.addWidget(d)

        self._drop_zone = DropZone()
        self._drop_zone.fileDropped.connect(self._on_file_dropped)
        lay.addWidget(self._drop_zone)

        # 详细状态/错误信息
        self._import_status = QLabel("")
        self._import_status.setWordWrap(True)
        self._import_status.setVisible(False)
        self._import_status.setStyleSheet(
            "QLabel{color:" + COLORS["SUCCESS_DARK"] + ";font-size:12px;font-weight:500;"
            "font-family:" + FONT_STACK + ";background:transparent;padding:10px;"
            "background:" + COLORS["SUCCESS_LIGHT"] + ";border-radius:" + RADII["sm"] + ";}"
        )
        lay.addWidget(self._import_status)

        lay.addStretch(1)
        self._stack.addWidget(page)

    # ------------------------------------------------ 导航
    def _go_step(self, step: int):
        self._current_step = step
        self._indicator.set_step(step)
        self._stack.setCurrentIndex(step)
        self._step_label.setText(f"{step + 1} / 3")

        if step == 0:
            self._prev_btn.setText(tr("cancel"))
            self._next_btn.setText(tr("ok"))
            self._next_btn.setEnabled(True)
        elif step == 1:
            self._prev_btn.setText(tr("cancel"))
            self._next_btn.setText(tr("ok"))
            self._next_btn.setEnabled(True)
        elif step == 2:
            self._prev_btn.setText(tr("cancel"))
            self._next_btn.setText(tr("done"))
            self._next_btn.setEnabled(self._imported_pack is not None)

    def _on_prev(self):
        if self._current_step == 0:
            self.reject()
        else:
            self._go_step(self._current_step - 1)

    def _on_next(self):
        if self._current_step < 2:
            self._go_step(self._current_step + 1)
        else:
            self._on_finish()

    # ------------------------------------------------ Step 1 导出
    def _on_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("lang_pack_export_btn"),
            str(Path.home() / "dscode_lang_en_template.json"),
            tr("lang_pack_json_filter"),
        )
        if not path:
            return
        try:
            written = I18N.export_template(path)
            self._export_status.setText(tr("lang_pack_export_success", path=written))
            self._export_status.setVisible(True)
        except Exception as e:
            QMessageBox.warning(self, tr("error"), str(e))

    # ------------------------------------------------ Step 2 复制提示词
    def _on_copy_prompt(self):
        text = self._prompt_edit.toPlainText()
        try:
            clipboard = QApplication.clipboard()
            clipboard.setText(text, QClipboard.Clipboard)
            original = self._copy_btn.text()
            self._copy_btn.setText("✓ " + (tr("done") if tr("done") != "done" else "Copied!"))
            self._copy_btn.setEnabled(False)
            QTimer.singleShot(1500, lambda: (
                self._copy_btn.setText(original),
                self._copy_btn.setEnabled(True),
            ))
        except Exception as e:
            QMessageBox.warning(self, tr("error"), str(e))

    # ------------------------------------------------ Step 3 文件处理
    def _on_file_dropped(self, file_path: str):
        """处理拖入/选择的 JSON 文件：校验并暂存。"""
        self._imported_pack = None
        self._imported_code = ""
        self._imported_name = ""
        self._next_btn.setEnabled(False)

        try:
            success, code, name, pack, errors, warnings = I18N._load_single_pack(file_path)
        except Exception as e:
            self._drop_zone.set_status(tr("lang_pack_invalid_json", err=str(e)), status="error")
            self._show_import_message(tr("lang_pack_invalid_json", err=str(e)), "error")
            return

        if not success:
            # 处理错误
            err_msg = self._format_errors(errors)
            self._drop_zone.set_status(err_msg[:60], err_msg[60:120] if len(err_msg) > 60 else "", "error")
            self._show_import_message(err_msg, "error")
            return

        # 有警告但成功
        warn_msg = self._format_warnings(warnings)
        if warn_msg:
            self._drop_zone.set_status(
                f"{'✓' if True else ''} {name} ({code})", warn_msg, "warning"
            )
            self._show_import_message(
                tr("lang_pack_load_success", name=name, code=code) + "\n" + warn_msg,
                "warning"
            )
        else:
            self._drop_zone.set_status(
                f"✓ {name} ({code})", tr("lang_pack_drop_hint"), "success"
            )
            self._show_import_message(
                tr("lang_pack_load_success", name=name, code=code),
                "success"
            )

        self._imported_pack = pack
        self._imported_code = code
        self._imported_name = name
        self._next_btn.setEnabled(True)

    def _format_errors(self, errors: list) -> str:
        """将错误码列表转为可读文本。"""
        msgs = []
        for e in errors:
            if e == "missing_meta":
                msgs.append(tr("lang_pack_missing_meta"))
            elif e == "meta_code_empty":
                msgs.append("_meta.code is empty")
            elif e == "meta_code_invalid":
                msgs.append("_meta.code format invalid (expect BCP-47 like ja/ko/fr)")
            elif e == "builtin_protected":
                msgs.append(tr("lang_pack_builtin_protected", name="zh/en"))
            elif e.startswith("invalid_json:"):
                msgs.append(tr("lang_pack_invalid_json", err=e.split(":", 1)[1]))
            elif e.startswith("placeholder_mismatch:"):
                parts = e.split(":", 2)
                if len(parts) >= 3:
                    msgs.append(tr("lang_pack_placeholder_mismatch", key=parts[1], ph=parts[2]))
                else:
                    msgs.append(e)
            else:
                msgs.append(e)
        return "\n".join(msgs) if msgs else "Unknown error"

    def _format_warnings(self, warnings: list) -> str:
        msgs = []
        for w in warnings:
            if w.startswith("missing_keys:"):
                n = w.split(":", 1)[1]
                msgs.append(tr("lang_pack_missing_keys", n=n))
            elif w.startswith("extra_keys:"):
                n = w.split(":", 1)[1]
                msgs.append(f"{n} unrecognized keys will be ignored")
            elif w == "meta_name_empty":
                msgs.append("_meta.name is empty; will use code as name")
        return "\n".join(msgs)

    def _show_import_message(self, text: str, status: str = "normal"):
        """在导入区显示状态消息。"""
        self._import_status.setText(text)
        self._import_status.setVisible(True)

        bg_map = {
            "success": COLORS["SUCCESS_LIGHT"],
            "error": COLORS["ERROR_LIGHT"],
            "warning": COLORS["WARNING_LIGHT"],
            "normal": _surface(),
        }
        fg_map = {
            "success": COLORS["SUCCESS_DARK"],
            "error": COLORS["ERROR_DARK"],
            "warning": COLORS["WARNING_DARK"],
            "normal": _text(),
        }
        bg = bg_map.get(status, _surface())
        fg = fg_map.get(status, _text())
        self._import_status.setStyleSheet(
            "QLabel{color:" + fg + ";font-size:12px;font-weight:500;"
            "font-family:" + FONT_STACK + ";background:" + bg + ";padding:10px;"
            "border-radius:" + RADII["sm"] + ";}"
        )

    # ------------------------------------------------ 完成
    def _on_finish(self):
        if not self._imported_pack:
            return

        code = self._imported_code
        name = self._imported_name
        base_code = code.split("-")[0]

        # 检查文件是否已存在
        locale_dir = I18N.get_locale_dir()
        target = locale_dir / f"{base_code}.json"
        if target.exists():
            reply = QMessageBox.question(
                self,
                tr("lang_pack_dialog_title"),
                tr("lang_pack_file_exists", code=base_code),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        try:
            saved = I18N.save_pack_to_disk(self._imported_pack)
            I18N.register_language(code, self._imported_pack)
        except Exception as e:
            QMessageBox.warning(self, tr("error"), str(e))
            return

        QMessageBox.information(
            self,
            tr("done"),
            tr("lang_pack_load_success", name=name, code=code),
        )
        self.pack_installed.emit(code, name)
        self.accept()
