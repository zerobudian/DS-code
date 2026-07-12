# -*- coding: utf-8 -*-
"""
使用前引导对话框（单页确认）

- 鲸鱼Logo + 欢迎标题
- AI免责声明
- 法律合规提示
- 用户协议复选框（链接 dscodehelp.pages.dev）
- 开始使用按钮（勾选后可用）

设计要点：
  - 大卡片 (520×auto)
  - 主色: COLORS['PRIM_DARK'] (#000000)
  - 按钮: 黑色圆形/胶囊按钮
"""

from PySide6.QtCore import Qt, Signal, QLocale, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QPixmap, QColor, QPainter
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox,
    QPushButton, QFrame, QSizePolicy, QGraphicsOpacityEffect,
)

from src.gui.style import COLORS, RADII, FONT_STACK, make_label, I18N, ThemeManager, tr
from src.gui.animations import pop_in, pop_out, fade_in, fade_out


# ============================================================================
# 工具：根据系统语言自动检测并设置I18N
#   英语系统 → en
#   其他（中文、日语、韩语等）→ zh
# ============================================================================
def _detect_system_language() -> str:
    """根据系统 locale 自动检测语言。英语系统 → en，其他 → zh。"""
    try:
        lang = QLocale.system().name().lower()  # 如 "zh_cn", "en_us", "ja_jp"
        if lang.startswith("en"):
            return "en"
        return "zh"
    except Exception:
        return "zh"  # 失败时默认中文


def _apply_system_language():
    """自动检测系统语言并设置到I18N（不在UI中暴露语言选择）。"""
    lang = _detect_system_language()
    try:
        I18N.set_language(lang)
    except Exception:
        pass
    return lang


# ============================================================================
# 工具：所有界面底部 footer（非对话界面用）
#   文字：DS code  Powered by Budian Ultra
#   点击跳转：https://dscode.pages.dev
# ============================================================================
def _build_footer(parent=None) -> QFrame:
    """构建底部 footer。灰色小字，点击跳转到 dscode.pages.dev。"""
    footer = QFrame(parent)
    footer.setStyleSheet("QFrame{background:transparent;}")
    fl = QHBoxLayout(footer)
    fl.setContentsMargins(24, 8, 24, 12)
    fl.setSpacing(0)
    fl.addStretch(1)

    link = QLabel(
        '<a href="https://dscode.pages.dev" '
        'style="color:' + COLORS['TEXT_3'] + ';font-size:11px;font-weight:500;'
        'font-family:' + FONT_STACK + ';text-decoration:none;'
        'letter-spacing:0.3px;">'
        'DS code &nbsp;&nbsp; Powered by Budian Ultra'
        '</a>'
    )
    link.setOpenExternalLinks(True)
    link.setCursor(Qt.PointingHandCursor)
    fl.addWidget(link, 0)

    fl.addStretch(1)
    return footer


# ============================================================================
# 工具：对话界面底部 footer（提示 AI 会犯错）
#   文字：DS Code is AI and can make mistakes. Please double-check responses.
#   点击跳转：https://dscode.pages.dev
# ============================================================================
def _build_chat_footer(parent=None) -> QFrame:
    """对话界面底部 footer —— 提示 AI 会犯错。"""
    footer = QFrame(parent)
    footer.setStyleSheet("QFrame{background:transparent;}")
    fl = QHBoxLayout(footer)
    fl.setContentsMargins(16, 8, 16, 8)
    fl.setSpacing(0)
    fl.addStretch(1)

    text_4 = COLORS.get('TEXT_4', COLORS['TEXT_3'])
    link = QLabel(
        '<a href="https://dscode.pages.dev" '
        'style="color:' + text_4 + ';font-size:11px;font-weight:500;'
        'font-family:' + FONT_STACK + ';text-decoration:none;'
        'letter-spacing:0.2px;">'
        'DS Code is AI and can make mistakes. Please double-check responses.'
        '</a>'
    )
    link.setOpenExternalLinks(True)
    link.setCursor(Qt.PointingHandCursor)
    fl.addWidget(link, 0)

    fl.addStretch(1)
    return footer


# ============================================================================
# 工具：绘制鲸鱼Logo（居中大尺寸）
# ============================================================================
def _whale_pixmap(size_w=200, size_h=140) -> QPixmap:
    """画一个鲸鱼Logo图。"""
    pm = QPixmap(size_w, size_h)
    pm.fill(QColor(0, 0, 0, 0))  # 透明
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)

    # 鲸鱼身体 (椭圆)
    body_c = QColor(COLORS["PRIM_DARK"])
    p.setBrush(body_c)
    p.setPen(Qt.NoPen)
    p.drawEllipse(int(size_w * 0.15), int(size_h * 0.3),
                  int(size_w * 0.55), int(size_h * 0.45))

    # 鲸鱼尾巴 (三角形)
    from PySide6.QtGui import QPolygon
    from PySide6.QtCore import QPoint
    tail = QPolygon([
        QPoint(int(size_w * 0.68), int(size_h * 0.3)),
        QPoint(int(size_w * 0.92), int(size_h * 0.15)),
        QPoint(int(size_w * 0.92), int(size_h * 0.6)),
    ])
    p.drawPolygon(tail)

    # 白色眼睛
    p.setBrush(QColor(COLORS['CARD']))
    p.drawEllipse(int(size_w * 0.52), int(size_h * 0.45), 10, 10)

    # 小黑眼珠
    p.setBrush(QColor(COLORS["TEXT"]))
    p.drawEllipse(int(size_w * 0.55), int(size_h * 0.47), 4, 4)

    # 喷水 (3 个小椭圆)
    p.setBrush(QColor(COLORS["PRIM"]))
    p.drawEllipse(int(size_w * 0.32), int(size_h * 0.08), 7, 12)
    p.drawEllipse(int(size_w * 0.4), int(size_h * 0.02), 5, 16)
    p.drawEllipse(int(size_w * 0.47), int(size_h * 0.08), 7, 12)

    p.end()
    return pm


# ============================================================================
# 主对话框 — 单页确认（AI免责+法律合规+协议→开始使用）
# ============================================================================
class WelcomeScreenDialog(QDialog):
    """使用前快速确认对话框。

    内容：
      - 鲸鱼Logo（居中）
      - 标题：欢迎使用 DS Code
      - AI免责声明（浅蓝卡片）
      - 法律合规提示（浅蓝卡片）
      - 用户协议复选框（链接 dscodehelp.pages.dev）
      - 开始使用按钮（勾选复选框后可用）

    对外信号: accepted (QDialog.accept)
    """

    def __init__(self, language: str = None, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)

        # 自动检测系统语言并设置I18N（不在UI中暴露语言选择）
        if language is None or language not in ("zh", "en"):
            language = _apply_system_language()
        else:
            try:
                I18N.set_language(language)
            except Exception:
                pass
        self._lang = language
        self._can_finish = False
        self._is_closing = False
        self._shown_once = False

        self._build()
        try:
            ThemeManager.instance().theme_changed.connect(lambda _theme: self.rebuild_style())
        except Exception:
            pass

    # ----- UI 构建 -----
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # 白色卡片容器
        self._card = QFrame()
        self._card.setObjectName("welcomeCard")
        self._card.setStyleSheet(
            "QFrame#welcomeCard{"
            "background:" + COLORS["CARD"] + ";"
            "border:1px solid " + COLORS["BORDER"] + ";"
            "border-radius:" + RADII["xl"] + ";"
            "}"
        )
        self._card.setFixedWidth(480)
        self._card.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.MinimumExpanding)

        root = QVBoxLayout(self._card)
        root.setContentsMargins(40, 48, 40, 32)
        root.setSpacing(0)

        # —— 鲸鱼Logo（居中）——
        logo_row = QHBoxLayout()
        logo_row.addStretch(1)
        logo_label = QLabel()
        logo_label.setAlignment(Qt.AlignCenter)
        logo_label.setPixmap(_whale_pixmap(160, 120))
        logo_label.setStyleSheet("QLabel{background:transparent;}")
        logo_row.addWidget(logo_label)
        logo_row.addStretch(1)
        root.addLayout(logo_row)
        root.addSpacing(24)

        # —— 标题 ——
        title_text = tr("welcome_greeting")
        title = make_label(title_text, font_size="24px", weight="800",
                           align=Qt.AlignCenter)
        root.addWidget(title)
        root.addSpacing(6)

        # 副标题
        sub_text = tr("welcome_greeting_sub")
        sub = make_label(sub_text, font_size="14px", weight="500",
                         color=COLORS["TEXT_3"], align=Qt.AlignCenter)
        root.addWidget(sub)
        root.addSpacing(28)

        # —— AI免责声明卡片 ——
        disclaimer = self._build_info_card(
            tr("disclaimer_text"),
            icon_color=COLORS["WARNING"]
        )
        root.addWidget(disclaimer)
        root.addSpacing(10)

        # —— 法律合规卡片 ——
        legal = self._build_info_card(
            tr("legal_text"),
            icon_color=COLORS["PRIM_DARK"]
        )
        root.addWidget(legal)
        root.addSpacing(24)

        # —— 用户协议复选框 ——
        cb_row = QHBoxLayout()
        cb_row.setSpacing(8)
        cb_row.addStretch(1)

        agreement_text = tr("agreement_checkbox")
        link_text = tr("agreement_link")

        self.checkbox = QCheckBox()
        self.checkbox.setCursor(Qt.PointingHandCursor)
        self.checkbox.setStyleSheet(
            "QCheckBox{spacing:10px;}"
            "QCheckBox::indicator{width:20px;height:20px;border-radius:6px;"
            "border:2px solid " + COLORS["BORDER"] + ";"
            "background:" + COLORS["CARD"] + ";}"
            "QCheckBox::indicator:hover{border-color:" + COLORS["PRIM_DARK"] + ";}"
            "QCheckBox::indicator:checked{background:" + COLORS["PRIM_DARK"] + ";"
            "border-color:" + COLORS["PRIM_DARK"] + ";}"
        )
        self.checkbox.toggled.connect(self._on_agreed)
        cb_row.addWidget(self.checkbox)

        cb_label = QLabel(
            agreement_text
            + '<a href="https://dscodehelp.pages.dev" style="color:'
            + COLORS["PRIM_DARK"]
            + ';text-decoration:underline;font-weight:700;">'
            + link_text
            + '</a>'
        )
        cb_label.setOpenExternalLinks(True)
        cb_label.setCursor(Qt.PointingHandCursor)
        cb_label.setStyleSheet(
            "QLabel{color:" + COLORS["TEXT"] + ";font-size:14px;font-weight:500;"
            "font-family:" + FONT_STACK + ";background:transparent;}"
        )
        cb_row.addWidget(cb_label)
        cb_row.addStretch(1)
        root.addLayout(cb_row)
        root.addSpacing(28)

        # —— 开始使用按钮（黑色胶囊按钮）——
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        self.start_btn = QPushButton(
            tr("start_using") + "  →"
        )
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.setFixedHeight(50)
        self.start_btn.setMinimumWidth(200)
        self.start_btn.setEnabled(False)
        self.start_btn.setStyleSheet(
            "QPushButton{background:" + COLORS["PRIM_DARK"] + ";"
            "color:" + COLORS['CARD'] + ";border:none;border-radius:" + RADII["pill"] + ";"
            "padding:0 36px;font-size:16px;font-weight:700;"
            "font-family:" + FONT_STACK + ";}"
            "QPushButton:hover{opacity:0.85;}"
            "QPushButton:pressed{opacity:0.7;}"
            "QPushButton:disabled{background:" + COLORS["BORDER"] + ";"
            "color:" + COLORS['TEXT_3'] + ";}"
        )
        self.start_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.start_btn)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        root.addSpacing(20)

        # 底部 footer
        footer = _build_footer(self._card)
        root.addWidget(footer)

        outer.addWidget(self._card)

    def rebuild_style(self):
        """主题变化时重建欢迎对话框样式。"""
        self._card.setStyleSheet(
            "QFrame#welcomeCard{"
            "background:" + COLORS["CARD"] + ";"
            "border:1px solid " + COLORS["BORDER"] + ";"
            "border-radius:" + RADII["xl"] + ";"
            "}"
        )
        self.start_btn.setStyleSheet(
            "QPushButton{background:" + COLORS["PRIM_DARK"] + ";"
            "color:" + COLORS['CARD'] + ";border:none;border-radius:" + RADII["pill"] + ";"
            "padding:0 36px;font-size:16px;font-weight:700;"
            "font-family:" + FONT_STACK + ";}"
            "QPushButton:hover{opacity:0.85;}"
            "QPushButton:pressed{opacity:0.7;}"
            "QPushButton:disabled{background:" + COLORS["BORDER"] + ";"
            "color:" + COLORS['TEXT_3'] + ";}"
        )
        self.checkbox.setStyleSheet(
            "QCheckBox{spacing:10px;}"
            "QCheckBox::indicator{width:20px;height:20px;border-radius:6px;"
            "border:2px solid " + COLORS["BORDER"] + ";"
            "background:" + COLORS["CARD"] + ";}"
            "QCheckBox::indicator:hover{border-color:" + COLORS["PRIM_DARK"] + ";}"
            "QCheckBox::indicator:checked{background:" + COLORS["PRIM_DARK"] + ";"
            "border-color:" + COLORS["PRIM_DARK"] + ";}"
        )
        for w in self._card.findChildren(QFrame):
            if w.objectName() == "info_card":
                w.setStyleSheet(
                    "QFrame{background:" + COLORS["PRIM"] + ";"
                    "border-radius:" + RADII["lg"] + ";}"
                )
            elif w.objectName() == "info_card_bar":
                # 色条颜色在创建时由 icon_color 决定，这里保持原值
                pass
        for w in self._card.findChildren(QLabel):
            style = w.styleSheet()
            if "font-size:24px" in style:
                w.setStyleSheet(
                    "QLabel{color:" + COLORS["TEXT"] + ";font-size:24px;font-weight:800;"
                    "font-family:" + FONT_STACK + ";background:transparent;}"
                )
            elif "font-size:14px" in style and "font-weight:500" in style and "background:transparent" in style:
                w.setStyleSheet(
                    "QLabel{color:" + COLORS["TEXT_3"] + ";font-size:14px;font-weight:500;"
                    "font-family:" + FONT_STACK + ";background:transparent;}"
                )

    def _build_info_card(self, text: str, icon_color: str = None) -> QFrame:
        """构建信息提示卡片（浅蓝背景+左侧色条）。"""
        card = QFrame()
        card.setObjectName("info_card")
        card.setStyleSheet(
            "QFrame{background:" + COLORS["PRIM"] + ";"
            "border-radius:" + RADII["lg"] + ";}"
        )
        lay = QHBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(12)

        # 左侧竖条
        bar = QLabel()
        bar.setObjectName("info_card_bar")
        bar.setFixedWidth(4)
        bar.setStyleSheet(
            "QLabel{background:" + (icon_color or COLORS["PRIM_DARK"]) + ";"
            "border-radius:2px;min-height:20px;}"
        )
        lay.addWidget(bar)

        # 文字
        lbl = make_label(text, font_size="13px", weight="500",
                         color=COLORS["TEXT"], word_wrap=True)
        lay.addWidget(lbl, 1)

        return card

    def showEvent(self, event):
        super().showEvent(event)
        if self._shown_once:
            return
        self._shown_once = True
        try:
            pop_in(self._card, duration_ms=200, start_scale=0.92, easing=QEasingCurve.OutBack)
            fade_in(self._card, duration_ms=180, easing=QEasingCurve.OutCubic)
        except Exception:
            pass

    def accept(self):
        if self._is_closing:
            return
        self._is_closing = True
        remaining = [2]

        def _finish():
            remaining[0] -= 1
            if remaining[0] <= 0:
                try:
                    super(WelcomeScreenDialog, self).accept()
                except Exception:
                    pass

        try:
            pop_out(self._card, duration_ms=180, on_finished=_finish)
        except Exception:
            _finish()
        try:
            fade_out(self._card, duration_ms=150, on_finished=_finish)
        except Exception:
            _finish()

    def reject(self):
        if self._is_closing:
            return
        self._is_closing = True
        remaining = [2]

        def _finish():
            remaining[0] -= 1
            if remaining[0] <= 0:
                try:
                    super(WelcomeScreenDialog, self).reject()
                except Exception:
                    pass

        try:
            pop_out(self._card, duration_ms=180, on_finished=_finish)
        except Exception:
            _finish()
        try:
            fade_out(self._card, duration_ms=150, on_finished=_finish)
        except Exception:
            _finish()

    def _on_agreed(self, checked: bool):
        self._can_finish = bool(checked)
        self.start_btn.setEnabled(self._can_finish)
