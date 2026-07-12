# -*- coding: utf-8 -*-
"""设置弹窗的 UI 区段构建器与辅助组件。

从原 src/gui/settings_dialog.py 拆分而来（仅做代码组织，不改变任何公开 API）。
包含:
  - 颜色/控件工厂 (_darken / _prim / _card / _label / _button / _anim_option_button ...)
  - SegmentedControl 分段控件
  - 卡片/分组构建器 (_build_card / _build_group / _new_group)
  - ModelRow 单条模型配置行
主类 SettingsDialog 位于 _dialog.py。
"""
import os
from PySide6.QtCore import Qt, Signal, QPropertyAnimation, QEasingCurve, QSize
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QLineEdit, QCheckBox, QMessageBox, QWidget, QScrollArea,
    QSizePolicy, QComboBox, QButtonGroup, QSlider,
)

from src.gui.style import COLORS, RADII, FONT_STACK, make_label, make_line_edit, make_text_edit, tr
from src.gui._gui_helpers import _load_icon
from src.gui.animations import pop_in, pop_out, fade_in, fade_out, HoverColorButton

# NOTE: 本模块位于 src/gui/settings_dialog/ 包内，比原 settings_dialog.py
# 多一层目录，故此处多一级 ".." 以指向项目根 assets/。
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "assets")

ANIM_DURATION = 220

# 内部常量 — 官方供应商在数据模型中的 name 标记（不参与 i18n，保持 "官方" 以兼容旧配置）
_OFFICIAL_PROVIDER_TAG = "官方"
_APP_VERSION = "1.0.2.18"


# 统一复用 _gui_helpers 中的 _darken，避免多处分发重复定义
from src.gui._gui_helpers import _darken, _prim, _prim_dark, _prim_light, _card, _card_alt, _border_input  # noqa: F401


def _label(text, bold=False, color=None, font_size=None, weight=None):
    fs = font_size or "14px"
    if weight is None:
        weight = "700" if bold else "400"
    return make_label(text, font_size=fs, weight=weight, color=color)


def _line_edit(default="", echo=None, password_char=None):
    # password_char 为旧接口占位，实际由 echo 控制
    return make_line_edit(placeholder="", text=default, echo=echo,
                          height=44, padding="0 14px", font_size="14px")


def _text_edit(default="", min_height=120):
    return make_text_edit(text=default, min_height=min_height)


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
            "font-weight:600;font-family:" + FONT_STACK + ";padding:0 18px;}"
            "QPushButton:hover{background:" + _darken(_prim(), 0.92) + ";color:" + COLORS['CARD'] + ";}"
            "QPushButton:pressed{background:" + _prim_dark() + ";color:" + COLORS['CARD'] + ";}"
            "QPushButton:disabled{background:" + COLORS['PRIM_LIGHT'] + ";color:" + COLORS['CARD'] + ";}"
        )
    else:
        btn.setStyleSheet(
            "QPushButton{background:" + COLORS['CARD'] + ";color:" + COLORS['TEXT'] + ";"
            "border:1px solid " + _border_input() + ";"
            "border-radius:" + RADII["lg"] + ";font-size:14px;font-weight:500;"
            "font-family:" + FONT_STACK + ";padding:0 18px;}"
            "QPushButton:hover{background:" + _prim_light() + ";"
            "border-color:" + _prim() + ";color:" + _prim_dark() + ";}"
            "QPushButton:pressed{background:" + _prim_light() + ";}"
        )
    return btn


def _anim_option_button(text, width=None, height=38, padding="0 20px", radius=RADII["md"]):
    """带动画背景色/微缩放的选项按钮（语言/思考深度/权限/模式等）。"""
    btn = HoverColorButton(text)
    btn.set_colors(
        normal_bg=COLORS['CARD'],
        hover_bg=COLORS['PRIM_LIGHT'],
        pressed_bg=COLORS['PRIM'],
        checked_bg=_prim(),
        text_color=COLORS['TEXT'],
        checked_text_color=COLORS['CARD'],
        border_color=_border_input(),
    )
    btn.set_style(border_radius=radius, border_width="1px",
                  font_size="13px", font_weight="600", padding=padding)
    btn.setCheckable(True)
    if width is not None:
        btn.setFixedWidth(width)
    btn.setFixedHeight(height)
    return btn


# ------------------------------------------------ Segmented control
class SegmentedControl(QWidget):
    """多选项分段控件；激活项在 PRIM_DARK 背景，其他透明。"""
    valueChanged = Signal(str)

    def __init__(self, options, value_key=None, parent=None):
        super().__init__(parent)
        self._options = list(options)  # list[(key, label)]
        self._value_key = value_key or self._options[0][0]
        self._buttons = []

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        self._container = QFrame()
        self._container.setStyleSheet(
            "QFrame{background:" + COLORS['BORDER'] + ";"
            "border-radius:" + RADII['lg'] + ";}"
        )
        inner = QHBoxLayout(self._container)
        inner.setContentsMargins(4, 4, 4, 4)
        inner.setSpacing(2)

        for key, label in self._options:
            b = QPushButton(label)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(36)
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: self._set_value(k))
            inner.addWidget(b, 1)
            self._buttons.append((key, b))

        root.addWidget(self._container)
        self._refresh_styles()

    def value(self):
        return self._value_key

    def set_value(self, key):
        if key != self._value_key:
            self._value_key = key
            self._refresh_styles()

    def _set_value(self, key):
        if key == self._value_key:
            return
        self._value_key = key
        self._refresh_styles()
        self.valueChanged.emit(key)

    def _refresh_styles(self):
        active_ss = (
            "QPushButton{background:" + _prim() + ";color:" + COLORS['CARD'] + ";"
            "border:none;border-radius:" + RADII["md"] + ";font-size:13px;"
            "font-weight:600;font-family:" + FONT_STACK + ";}"
            "QPushButton:hover{background:" + _darken(_prim(), 0.92) + ";color:" + COLORS['CARD'] + ";}"
        )
        inactive_ss = (
            "QPushButton{background:transparent;color:" + COLORS['TEXT'] + ";"
            "border:none;border-radius:" + RADII["md"] + ";font-size:13px;"
            "font-weight:500;font-family:" + FONT_STACK + ";}"
            "QPushButton:hover{background:" + _prim_light() + ";color:" + _prim_dark() + ";}"
        )
        for key, b in self._buttons:
            b.setStyleSheet(active_ss if key == self._value_key else inactive_ss)


# ------------------------------------------------ Card / Group builders
def _build_card(title, subtitle):
    """返回 (card_frame, card_layout)。纯白大卡片，圆角 20px，外框无边。"""
    card = QFrame()
    card.setStyleSheet(
        "QFrame{background:" + COLORS['CARD'] + ";border-radius:" + RADII["card"] + ";}"
    )
    card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    lay = QVBoxLayout(card)
    lay.setContentsMargins(26, 22, 26, 24)
    lay.setSpacing(16)

    # 标题行：左侧竖条 + 标题 + 副标题
    header = QHBoxLayout()
    header.setSpacing(10)

    bar = QFrame()
    bar.setFixedWidth(4)
    bar.setFixedHeight(20)
    bar.setStyleSheet(
        "QFrame{background:" + _prim() + ";border-radius:" + RADII["sm"] + ";}"
    )
    header.addWidget(bar)

    title_col = QVBoxLayout()
    title_col.setSpacing(2)
    t = _label(title, weight="700", font_size="18px")
    title_col.addWidget(t)
    s = _label(subtitle, color=COLORS['TEXT_3'], font_size="12px", weight="400")
    s.setWordWrap(True)
    title_col.addWidget(s)
    header.addLayout(title_col, 1)

    lay.addLayout(header)
    return card, lay


def _build_group(card_layout):
    """在卡片中创建一个分组容器（浅蓝 PRIM 底 + 大圆角）。

    统一用浅蓝卡片包裹内容，视觉更干净。
    返回 group_frame, group_layout。
    """
    g = QFrame()
    g.setStyleSheet(
        "QFrame{background:" + _card_alt() + ";border-radius:" + RADII["lg"] + ";}"
    )
    gl = QVBoxLayout(g)
    gl.setContentsMargins(22, 18, 22, 18)
    gl.setSpacing(14)
    card_layout.addWidget(g)
    return g, gl


def _new_group(title, subtitle, parent=None):
    """创建一个带标题+副标题的分组容器。返回 group_frame。"""
    g = QFrame(parent)
    g.setStyleSheet(
        "QFrame{background:" + _card_alt() + ";border-radius:" + RADII["lg"] + ";}"
    )
    gl = QVBoxLayout(g)
    gl.setContentsMargins(22, 18, 22, 18)
    gl.setSpacing(12)

    title_row = QHBoxLayout()
    title_row.setSpacing(10)

    bar = QFrame()
    bar.setFixedWidth(4)
    bar.setFixedHeight(20)
    bar.setStyleSheet(
        "QFrame{background:" + _prim() + ";border-radius:" + RADII["sm"] + ";}"
    )
    title_row.addWidget(bar)

    col = QVBoxLayout()
    col.setSpacing(2)
    col.addWidget(_label(title, weight="700", font_size="14px"))
    if subtitle:
        sub = _label(subtitle, color=COLORS['TEXT_3'], font_size="12px", weight="400")
        sub.setWordWrap(True)
        col.addWidget(sub)
    title_row.addLayout(col, 1)

    gl.addLayout(title_row)
    # 给调用方一个可以继续往下加控件的地方,通过 g.layout().addWidget(...)
    if parent is not None and hasattr(parent, 'layout') and parent.layout() is not None:
        parent.layout().addWidget(g)
    return g


# ------------------------------------------------ Model row widget
class ModelRow(QFrame):
    """单条模型配置行：名称输入 + 长上下文开关 + 可选删除按钮。"""
    deleteRequested = Signal(object)

    def __init__(self, name="", long_context=False, deletable=True, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            "QFrame{background:" + _card() + ";border:1px solid " + _border_input() + ";"
            "border-radius:" + RADII["md"] + ";}"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(12)

        self.name_edit = _line_edit(name)
        self.name_edit.setPlaceholderText(tr("model_name_placeholder"))
        lay.addWidget(self.name_edit, 2)

        # 长上下文开关
        switch_row = QHBoxLayout()
        switch_row.setSpacing(6)
        lbl = _label(tr("long_context_label"), color=COLORS['TEXT_3'], font_size="12px")
        switch_row.addWidget(lbl)

        self.long_ctx = QCheckBox()
        self.long_ctx.setChecked(bool(long_context))
        self.long_ctx.setCursor(Qt.PointingHandCursor)
        switch_row.addWidget(self.long_ctx)
        lay.addLayout(switch_row)

        # 删除按钮
        self.del_btn = QPushButton()
        self.del_btn.setFixedSize(32, 32)
        self.del_btn.setCursor(Qt.PointingHandCursor)
        self.del_btn.setIcon(_load_icon("close_18.png", 14))
        self.del_btn.setIconSize(QSize(14, 14))
        self.del_btn.setStyleSheet(
            "QPushButton{background:transparent;color:" + COLORS['TEXT_3'] + ";"
            "border:none;border-radius:" + RADII['xs'] + ";}"
            "QPushButton:hover{background:" + COLORS['ERROR'] + ";color:" + COLORS['CARD'] + ";}"
        )
        self.del_btn.clicked.connect(lambda: self.deleteRequested.emit(self))
        lay.addWidget(self.del_btn)
        if not deletable:
            self.del_btn.setVisible(False)

    def data(self):
        return {"name": self.name_edit.text().strip(), "long_context": bool(self.long_ctx.isChecked())}

