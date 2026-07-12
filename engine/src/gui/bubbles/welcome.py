# -*- coding: utf-8 -*-
"""欢迎卡片 WelcomeCard 与统计卡片 StatsCard。

欢迎卡片（seed2.1 设计）：鲸鱼图标 + "你好，我是 DS code" 问候语 + 一个"创建项目"大尺寸快捷操作卡片。
模型/思考深度/权限调节通过 /settings 斜杠命令或侧栏设置按钮打开。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QFrame,
)
from PySide6.QtGui import QEnterEvent

from src.gui.style import RADII, COLORS, FONT_STACK, FONT_WEIGHTS, tr
from src.gui.bubbles._helpers import _try_pm
from src.gui._gui_helpers import _load_icon
from src.gui.widgets.stats_card import StatsCard


# ======================================================================
# ActionCard — 大尺寸快捷操作卡片（创建项目 / 终端）
# ======================================================================
class ActionCard(QFrame):
    """浅蓝色圆角大卡片按钮，图标在上文字在下，居中排列，带 hover 效果。

    用于欢迎页的「创建项目」快捷入口。点击时发出 sig_clicked() 信号。
    """
    sig_clicked = Signal()

    def __init__(self, icon_name: str, label: str, parent=None):
        super().__init__(parent)
        self.setObjectName("WelcomeActionCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(72)
        self.setFixedWidth(180)
        self._hovered = False

        # 内部垂直布局：图标 + 文字
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(6)
        lay.setAlignment(Qt.AlignCenter)

        # 图标（使用 _load_icon 加载 proper 图标）
        icon_lbl = QLabel()
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setFixedSize(28, 28)
        icon_lbl.setStyleSheet("background:transparent;")
        ic = _load_icon(icon_name, 24)
        if not ic.isNull():
            icon_lbl.setPixmap(ic.pixmap(QSize(24, 24)))
        lay.addWidget(icon_lbl, 0, Qt.AlignCenter)

        self._text_lbl = QLabel(label)
        self._text_lbl.setAlignment(Qt.AlignCenter)
        self._text_lbl.setStyleSheet(
            "QLabel{color:" + COLORS['TEXT'] + ";"
            "font-size:15px;font-weight:" + str(FONT_WEIGHTS['medium']) + ";"
            "font-family:" + FONT_STACK + ";background:transparent;border:none;}"
        )
        lay.addWidget(self._text_lbl, 0, Qt.AlignCenter)

        self._apply_style(False)

    def _apply_style(self, hovered: bool):
        bg = COLORS['PRIM_LIGHT'] if hovered else COLORS['PRIM']
        self.setStyleSheet(
            "QFrame#WelcomeActionCard{"
            "background:" + bg + ";"
            "border:none;"
            "border-radius:16px;"
            "}"
        )

    def set_text(self, text: str):
        self._text_lbl.setText(text)

    # ---- 事件 ----
    def enterEvent(self, event: QEnterEvent):
        self._hovered = True
        self._apply_style(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._apply_style(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.sig_clicked.emit()
        super().mousePressEvent(event)


# ======================================================================
# WelcomeCard — 欢迎卡片（透明背景 + 鲸鱼 + 问候 + 创建项目快捷卡）
# ======================================================================
class WelcomeCard(QFrame):
    """新对话欢迎卡（seed2.1）：鲸鱼图标 + 问候语 + 「创建项目」快捷卡。

    模型配置通过输入区标签弹窗调节，不在欢迎卡内提供配置控件。
    """

    # 保留旧信号以兼容既有连接
    model_selected = Signal(str)
    reasoning_changed = Signal(str)
    permission_changed = Signal(str)
    mode_selected = Signal(str)
    sig_suggestion_clicked = Signal(str)

    # 新信号：快捷操作
    sig_create_project = Signal()
    sig_open_project = Signal()

    def __init__(self, parent=None, language: str = "zh"):
        super().__init__(parent)
        self.setObjectName("WelcomeCard")
        self.setStyleSheet("QFrame#WelcomeCard{background:transparent;}")
        self._lang = language

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 32, 0, 8)
        outer.setSpacing(0)
        outer.setAlignment(Qt.AlignHCenter | Qt.AlignTop)

        # 居中容器（限宽 420px）
        self._center_wrap = QHBoxLayout()
        self._center_wrap.setContentsMargins(0, 0, 0, 0)
        self._center_wrap.addStretch(1)

        # 内容容器（透明，无边框无阴影，宽 420px）
        self._content = QFrame()
        self._content.setObjectName("WelcomeContent")
        self._content.setFixedWidth(420)
        self._content.setStyleSheet("QFrame#WelcomeContent{background:transparent;}")

        card_lay = QVBoxLayout(self._content)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)
        card_lay.setAlignment(Qt.AlignHCenter)

        # 鲸鱼图标（64px，居中）
        whale = QLabel()
        whale.setAlignment(Qt.AlignCenter)
        pm = _try_pm(["whale_80.png", "whale_64.png", "whale_32.png", "whale.png"], 64)
        if pm is None or pm.isNull():
            whale.setText("🐋")
            whale.setStyleSheet("QLabel{font-size:56px;background:transparent;}")
        else:
            whale.setPixmap(pm)
        card_lay.addWidget(whale)

        # whale → title 间距 12px
        card_lay.addSpacing(12)

        # 问候标题（22px 粗体黑色）
        self.title_label = QLabel("你好，我是 DS code")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet(
            "QLabel{color:" + COLORS['TEXT'] + ";"
            "font-size:22px;font-weight:" + str(FONT_WEIGHTS['bold']) + ";"
            "font-family:" + FONT_STACK + ";background:transparent;"
            "}"
        )
        card_lay.addWidget(self.title_label)

        # 副标题（13px 灰色，距标题8px）
        self.subtitle_label = QLabel(tr("welcome_subtitle"))
        self.subtitle_label.setAlignment(Qt.AlignCenter)
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setStyleSheet(
            "QLabel{color:" + COLORS['TEXT_3'] + ";"
            "font-size:13px;font-weight:" + str(FONT_WEIGHTS['regular']) + ";"
            "font-family:" + FONT_STACK + ";background:transparent;"
            "}"
        )
        card_lay.addSpacing(8)
        card_lay.addWidget(self.subtitle_label)

        # title → action cards 间距 24px
        card_lay.addSpacing(24)

        # 大尺寸快捷操作卡片：创建项目 + 打开项目（居中，卡片固定 180px 宽）
        actions_row = QHBoxLayout()
        actions_row.setContentsMargins(0, 0, 0, 0)
        actions_row.setSpacing(16)
        actions_row.addStretch(1)

        self._card_create = ActionCard("folder_24.png", tr("create_project"))
        self._card_create.sig_clicked.connect(self._on_create_project)
        actions_row.addWidget(self._card_create)

        self._card_open = ActionCard("folder_open_24.png", tr("open_project"))
        self._card_open.sig_clicked.connect(self._on_open_project)
        actions_row.addWidget(self._card_open)

        actions_row.addStretch(1)

        card_lay.addLayout(actions_row)

        self._center_wrap.addWidget(self._content)
        self._center_wrap.addStretch(1)
        outer.addLayout(self._center_wrap)
        outer.addStretch(1)

        self.refresh(language)

    # ---- 快捷卡点击处理 ----
    def _on_create_project(self):
        self.sig_create_project.emit()
        # 向后兼容：同时以默认提示触发 suggestion 信号
        self.sig_suggestion_clicked.emit("请帮我创建一个新项目。")

    def _on_open_project(self):
        self.sig_open_project.emit()

    # ---- i18n ----
    def refresh(self, language: str = "zh"):
        self._lang = language if language in ("zh", "en", "wenyan") else "zh"
        if self._lang == "en":
            self.title_label.setText("Hi, I'm DS code")
            self.subtitle_label.setText(tr("welcome_subtitle"))
            self._card_create.set_text(tr("create_project"))
            self._card_open.set_text(tr("open_project"))
        elif self._lang == "wenyan":
            self.title_label.setText("汝好，吾乃 DS code")
            self.subtitle_label.setText(tr("welcome_subtitle"))
            self._card_create.set_text("創建項目")
            self._card_open.set_text("打開項目")
        else:
            self.title_label.setText("你好，我是 DS code")
            self.subtitle_label.setText(tr("welcome_subtitle"))
            self._card_create.set_text(tr("create_project"))
            self._card_open.set_text(tr("open_project"))

    # 兼容旧 API（空操作，配置通过弹窗调节）
    def set_selected_model(self, *a, **kw): pass
    def set_selected_reasoning(self, *a, **kw): pass
    def set_selected_permission(self, *a, **kw): pass
    def set_selected_mode(self, *a, **kw): pass
    def get_selected_model(self): return "deepseek-v4-flash"
    def get_selected_reasoning(self): return "high"
    def get_selected_permission(self): return "full"
    def get_selected_mode(self): return "plan"
    def on_suggestion(self, slot):
        try:
            self.sig_suggestion_clicked.connect(slot)
        except Exception:
            pass


# StatsCard 已拆分到 src.gui.widgets.stats_card，此处重新导出以保持兼容
