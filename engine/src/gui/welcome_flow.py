# -*- coding: utf-8 -*-
"""
welcome_flow.py — 首次启动引导向导（seed2.1 设计稿）

页面顺序：
  Page 0: 快速开始 — whale 80px + "快速开始" + 黑色圆形箭头按钮
  Page 1: 输入你的 API key — whale + 下划线输入（密码模式，placeholder "sk-……"）+ 箭头按钮
  Page 2: 我们可以怎么称呼您 — whale + 下划线输入（placeholder "填写昵称…"）+ 箭头按钮
  Page 3: 使用须知 — 免责声明 + 复选框确认 + 用户协议链接
  Page 4: 开始使用 — whale 80px + "开始使用" + 黑色圆形箭头按钮

设计规范：
  - 纯白卡片 420x540，24px 圆角，无边框半透明对话框
  - 所有页面内容垂直居中，弹性拉伸
  - Whale logo 居中，优先 _try_pm 加载 whale_80.png，回退 🐋 emoji
  - 标题 22px bold 黑色 #000000 居中
  - 下划线输入框：仅底部边框 1.5px 灰色，聚焦 2px 黑色，16px 居中
  - 黑色圆形按钮 56x56，bg #000，白色 →，28px 圆角，hover #333

收集配置项：api_key, nickname, language (自动检测)
对外信号：completed(dict)
ESC 禁用；reject() 不做任何事（必须完成向导）。
显示/关闭使用 pop_in / pop_out 弹入弹出动画。
"""

import os
import locale
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QSizePolicy, QStackedWidget, QLineEdit, QWidget,
    QMessageBox, QCheckBox,
)
from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve, Signal, QRect, QUrl
from PySide6.QtGui import QPixmap, QKeyEvent, QPainter, QColor, QBrush, QPen, QDesktopServices

from src.gui.style import COLORS, FONT_STACK, RADII, ThemeManager, tr
from src.gui.animations import pop_in, pop_out, fade_in, fade_out, HoverColorButton

# 尝试从 bubbles._helpers 导入统一的 _try_pm 来加载 whale 资源
try:
    from src.gui.bubbles._helpers import _try_pm as _bubbles_try_pm
except Exception:
    _bubbles_try_pm = None

# ============================================================================
# 资源路径
# ============================================================================
_ASSETS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "assets"
)

def _asset_path(*names):
    for name in names:
        for sub in ["assets", "data", "resources", "."]:
            base_dir = Path(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
            path = os.path.join(base_dir, sub, name)
            if os.path.exists(path):
                return path
    return ""

# ============================================================================
# 蓝色鲸鱼 Logo
# ============================================================================
class _WhaleLogo(QLabel):
    def __init__(self, size=72, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedHeight(int(size * 1.2))
        pm = QPixmap()
        # 优先使用 bubbles._helpers._try_pm 统一资源加载
        if _bubbles_try_pm is not None:
            candidates = [f"whale_{size}.png"] if size else []
            candidates += ["whale_80.png", "whale_96.png", "whale_128.png",
                           "whale_64.png", "whale_48.png", "whale_32.png", "whale.png"]
            # 去重保持顺序
            seen = set()
            uniq = []
            for c in candidates:
                if c not in seen:
                    seen.add(c)
                    uniq.append(c)
            pm = _bubbles_try_pm(uniq, size)
        if pm.isNull():
            logo_path = _asset_path("whale_80.png", "whale_128.png", "whale_64.png",
                                    "whale_32.png", "whale.png")
            if logo_path:
                pm = QPixmap(logo_path)
                if not pm.isNull():
                    pm = pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if not pm.isNull():
            self.setPixmap(pm)
            return
        self.setText("🐋")
        self.setStyleSheet(f"font-size:{int(size * 0.75)}px;")

# ============================================================================
# 黑色圆形箭头按钮
# ============================================================================
def _circle_arrow_btn(parent=None) -> HoverColorButton:
    btn = HoverColorButton("→", parent)
    btn.set_colors(
        normal_bg=COLORS["PRIM_DARK"],
        hover_bg=COLORS["PRIM_LIGHT"],
        pressed_bg=COLORS["PRIM"],
        text_color=COLORS["CARD"],
        border_color="transparent",
    )
    btn.set_style(border_radius="28px", border_width="0px",
                  font_size="22px", font_weight="700", padding="0px")
    btn.setFixedSize(56, 56)
    btn.setCursor(Qt.PointingHandCursor)
    return btn

# ============================================================================
# 可点击文本选项（极简居中文字，选中变蓝加粗）
# ============================================================================
class _TextOption(QLabel):
    """极简文字选项：居中显示，点击发出 clicked(key) 信号。"""
    clicked = Signal(str)

    def __init__(self, text: str, key: str, parent=None):
        super().__init__(text, parent)
        self.key = key
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setWordWrap(True)
        self._selected = False
        self._apply_style()

    def set_selected(self, selected: bool):
        self._selected = selected
        self._apply_style()

    def _apply_style(self):
        color = COLORS["ACCENT"] if self._selected else COLORS.get("TEXT", "#111827")
        weight = "700" if self._selected else "400"
        self.setStyleSheet(
            f"QLabel{{color:{color};font-size:16px;font-weight:{weight};"
            f"font-family:{FONT_STACK};background:transparent;"
            f"padding:8px 0;}}"
        )

    def mousePressEvent(self, event):
        self.clicked.emit(self.key)

# ============================================================================
# 极简输入框（仅底部边框样式）
# ============================================================================
class _UnderlineEdit(QLineEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrame(False)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(44)
        self._rebuild_style()

    def _rebuild_style(self):
        border_color = COLORS.get("BORDER", "#E5E7EB")
        text_color = COLORS.get("TEXT", "#111827")
        ph_color = COLORS.get("TEXT_4", "#9CA3AF")
        focus_color = COLORS.get("PRIM_DARK", "#000000")
        self.setStyleSheet(
            "QLineEdit{"
            "background:transparent;"
            f"color:{text_color};"
            "border:none;"
            f"border-bottom:1.5px solid {border_color};"
            "font-size:16px;font-weight:400;"
            "padding:8px 4px;"
            f"font-family:{FONT_STACK};"
            "}"
            f"QLineEdit:focus{{"
            f"border-bottom:2px solid {focus_color};"
            "}"
            f"QLineEdit[placeholderText]{{color:{ph_color};}}"
        )

# ============================================================================
# 自定义开关按钮（带白色滑块）
# ============================================================================
class _ToggleSwitch(QPushButton):
    """44x24 开关：选中黑底白圆钮在右；未选灰底白圆钮在左。"""

    def __init__(self, parent=None, checked=True):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFixedSize(44, 24)
        self.setCursor(Qt.PointingHandCursor)
        self.clicked.connect(self._on_clicked)
        self._update_style()

    def _on_clicked(self):
        self._update_style()

    def _update_style(self):
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        checked = self.isChecked()
        bg_color = QColor(COLORS.get("PRIM_DARK", "#000000")) if checked else QColor(COLORS.get("BORDER", "#E5E7EB"))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(bg_color))
        p.drawRoundedRect(0, 0, self.width(), self.height(), 12, 12)
        # 滑块颜色跟随卡片文字色，保证深浅主题均可见
        knob_color = QColor(COLORS.get("CARD", "#FFFFFF"))
        knob_d = 18
        knob_x = self.width() - knob_d - 3 if checked else 3
        knob_y = (self.height() - knob_d) // 2
        p.setBrush(QBrush(knob_color))
        p.drawEllipse(knob_x, knob_y, knob_d, knob_d)
        p.end()

# ============================================================================
# 页面构建辅助
# ============================================================================
def _page_title(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("page_title")
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setStyleSheet(
        f"QLabel{{color:{COLORS.get('TEXT', '#000000')};font-size:22px;font-weight:700;"
        f"font-family:{FONT_STACK};background:transparent;}}"
    )
    lbl.setWordWrap(True)
    return lbl

def _page_desc(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setStyleSheet(
        f"QLabel{{color:{COLORS.get('TEXT_3', '#6B7280')};font-size:14px;font-weight:400;"
        f"font-family:{FONT_STACK};background:transparent;line-height:1.6;}}"
    )
    lbl.setWordWrap(True)
    return lbl

# ============================================================================
# 主向导对话框
# ============================================================================
class WelcomeFlow(QDialog):
    """首次启动引导向导 — seed2.1 极简设计。"""

    completed = Signal(dict)

    # 固定页面索引（简化为4页：Start → API Key → Nickname → Done）
    PAGE_START = 0
    PAGE_APIKEY = 1
    PAGE_NICKNAME = 2
    PAGE_DISCLAIMER = 3
    PAGE_DONE = 4

    def __init__(self, parent=None, existing_nickname=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setFixedSize(420, 560)
        self._is_closing = False
        self._shown_once = False

        # 简化配置：仅收集 API Key 和昵称，模型/权限/思考深度在主界面设置
        self._config = {
            "nickname": existing_nickname or "",
            "api_key": "",
        }

        # 自动检测系统语言（不显示 UI，仅设置默认值）
        sys_lang = locale.getdefaultlocale()[0] or "zh_CN"
        if sys_lang.startswith("zh"):
            if "TW" in sys_lang.upper() or "HK" in sys_lang.upper() or "HANT" in sys_lang.upper():
                self._config["language"] = "zh-Hant"
            else:
                self._config["language"] = "zh"
        elif sys_lang.startswith("en"):
            self._config["language"] = "en"
        else:
            self._config["language"] = "zh"

        self._build()
        try:
            ThemeManager.instance().theme_changed.connect(lambda _theme: self.rebuild_style())
        except Exception:
            pass

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # 纯白卡片（无边框，大圆角）
        self._card = QFrame()
        self._card.setObjectName("WizCard")
        self._card.setStyleSheet(
            "QFrame#WizCard{"
            f"background:{COLORS['CARD']};"
            "border:none;"
            "border-radius:24px;"
            "}"
        )
        self._card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(48, 48, 48, 48)
        card_lay.setSpacing(0)

        # QStackedWidget
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background:transparent;")
        card_lay.addWidget(self._stack, 1)

        outer.addWidget(self._card)

        # 构建所有固定页面
        self._build_pages()

    # ------------------------------------------------------------------
    # 主题适配
    # ------------------------------------------------------------------
    def rebuild_style(self):
        """主题变化时重建向导内所有硬编码样式控件。"""
        # 纯白卡片背景
        self._card.setStyleSheet(
            "QFrame#WizCard{"
            f"background:{COLORS['CARD']};"
            "border:none;"
            "border-radius:24px;"
            "}"
        )
        # 页面标题
        for w in self.findChildren(QLabel):
            if w.objectName() == "page_title":
                w.setStyleSheet(
                    f"QLabel{{color:{COLORS.get('TEXT', '#000000')};font-size:22px;font-weight:700;"
                    f"font-family:{FONT_STACK};background:transparent;}}"
                )
        # 箭头按钮
        for w in self.findChildren(HoverColorButton):
            if w.objectName() == "arrow_btn":
                w.set_colors(
                    normal_bg=COLORS["PRIM_DARK"],
                    hover_bg=COLORS["PRIM_LIGHT"],
                    pressed_bg=COLORS["PRIM"],
                    text_color=COLORS["CARD"],
                    border_color="transparent",
                )
        # 文本选项
        for w in self.findChildren(_TextOption):
            w._apply_style()
        # 下划线输入框
        for w in self.findChildren(_UnderlineEdit):
            w._rebuild_style()
        # 开关
        for w in self.findChildren(_ToggleSwitch):
            w._update_style()
        # 协议链接按钮
        for w in self.findChildren(QPushButton):
            if w.objectName() == "agreement_link":
                w.setStyleSheet(
                    f"QPushButton{{background:transparent;border:none;"
                    f"color:{COLORS.get('ACCENT', '#3B82F6')};font-size:12px;"
                    f"font-weight:400;font-family:{FONT_STACK};text-decoration:underline;}}"
                    f"QPushButton:hover{{color:{COLORS.get('ACCENT_HOVER', '#2563EB')};}}"
                )
        # 复选框
        for w in self.findChildren(QCheckBox):
            w.setStyleSheet(
                f"QCheckBox{{color:{COLORS.get('TEXT', '#111827')};font-size:13px;"
                f"font-weight:600;font-family:{FONT_STACK};background:transparent;"
                f"spacing:8px;}}"
                f"QCheckBox::indicator{{width:18px;height:18px;"
                f"border:1.5px solid {COLORS.get('BORDER', '#D1D5DB')};"
                f"border-radius:4px;background:{COLORS.get('CARD', '#FFFFFF')};}}"
                f"QCheckBox::indicator:checked{{"
                f"background:{COLORS.get('PRIM_DARK', '#111827')};"
                f"border-color:{COLORS.get('PRIM_DARK', '#111827')};}}"
            )

    # ------------------------------------------------------------------
    # 页面构建基础
    # ------------------------------------------------------------------
    def _make_page(self):
        page = QWidget()
        page.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._stack.addWidget(page)
        return page, lay

    def _add_whale(self, lay, size=72):
        whale_row = QHBoxLayout()
        whale_row.addStretch(1)
        whale_row.addWidget(_WhaleLogo(size=size))
        whale_row.addStretch(1)
        lay.addLayout(whale_row)
        lay.addSpacing(20)

    def _add_arrow_btn(self, lay, callback):
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn = _circle_arrow_btn()
        btn.setObjectName("arrow_btn")
        btn.clicked.connect(callback)
        btn_row.addWidget(btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)
        return btn

    def _build_pages(self):
        self._build_page_start()        # 0: 欢迎
        self._build_page_apikey()       # 1: API Key
        self._build_page_nickname()     # 2: 昵称
        self._build_page_disclaimer()   # 3: 使用须知
        self._build_page_done()         # 4: 开始使用

    # --- Page 0: 快速开始 ---
    def _build_page_start(self):
        page, lay = self._make_page()
        lay.addStretch(2)
        self._add_whale(lay, size=80)
        lay.addWidget(_page_title("快速开始"))
        lay.addStretch(1)
        self._add_arrow_btn(lay, self._go_next)
        lay.addStretch(1)

    # --- Page: API Key ---
    def _build_page_apikey(self):
        page, lay = self._make_page()
        lay.addStretch(2)
        self._add_whale(lay, size=72)
        lay.addWidget(_page_title("输入你的 API key"))
        lay.addSpacing(32)
        self._apikey_input = _UnderlineEdit()
        self._apikey_input.setPlaceholderText("sk-……")
        self._apikey_input.setEchoMode(QLineEdit.Password)
        self._apikey_input.returnPressed.connect(self._on_apikey_done)
        lay.addWidget(self._apikey_input)
        lay.addStretch(2)
        self._add_arrow_btn(lay, self._on_apikey_done)
        lay.addStretch(1)

    def _on_apikey_done(self):
        key = self._apikey_input.text().strip()
        if not key:
            return  # 不允许空 API Key 跳过
        self._config["api_key"] = key
        self._go_next()

    # --- Page: 昵称 ---
    def _build_page_nickname(self):
        page, lay = self._make_page()
        lay.addStretch(2)
        self._add_whale(lay, size=72)
        lay.addWidget(_page_title("我们可以怎么称呼您"))
        lay.addSpacing(32)
        self._nickname_input = _UnderlineEdit()
        self._nickname_input.setPlaceholderText("填写昵称…")
        if self._config["nickname"]:
            self._nickname_input.setText(self._config["nickname"])
        self._nickname_input.returnPressed.connect(self._on_nickname_done)
        lay.addWidget(self._nickname_input)
        lay.addStretch(2)
        self._add_arrow_btn(lay, self._on_nickname_done)
        lay.addStretch(1)

    def _on_nickname_done(self):
        nickname = self._nickname_input.text().strip()
        if nickname:
            self._config["nickname"] = nickname
        self._go_next()

    # --- Page 3: 使用须知 ---
    def _build_page_disclaimer(self):
        page, lay = self._make_page()
        lay.addStretch(1)
        self._add_whale(lay, size=48)
        lay.addWidget(_page_title("在使用前快速确认以下内容"))
        lay.addSpacing(16)

        items_text = (
            "1. DS Code 是一款 AI 工具，可能生成不准确内容，请注意辨别\n"
            "2. 不对使用本工具产生的任何后果承担责任，请勿违反当地法律\n"
            "3. 请合理使用 AI，对输出内容保持审慎判断"
        )
        items_label = QLabel(items_text)
        items_label.setAlignment(Qt.AlignLeft)
        items_label.setWordWrap(True)
        items_label.setStyleSheet(
            f"QLabel{{color:{COLORS.get('TEXT_2', '#374151')};font-size:13px;"
            f"font-weight:400;font-family:{FONT_STACK};background:transparent;"
            f"line-height:1.8;padding:0 12px;}}"
        )
        lay.addWidget(items_label)
        lay.addSpacing(12)

        # 用户协议链接
        agreement_row = QHBoxLayout()
        agreement_row.addStretch(1)
        agreement_btn = QPushButton("查看用户协议 →")
        agreement_btn.setObjectName("agreement_link")
        agreement_btn.setCursor(Qt.PointingHandCursor)
        agreement_btn.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;"
            f"color:{COLORS.get('ACCENT', '#3B82F6')};font-size:12px;"
            f"font-weight:400;font-family:{FONT_STACK};text-decoration:underline;}}"
            f"QPushButton:hover{{color:{COLORS.get('ACCENT_HOVER', '#2563EB')};}}"
        )
        agreement_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://dscodehelp.pages.dev"))
        )
        agreement_row.addWidget(agreement_btn)
        agreement_row.addStretch(1)
        lay.addLayout(agreement_row)
        lay.addSpacing(20)

        # 复选框
        cb_row = QHBoxLayout()
        cb_row.addStretch(1)
        self._disclaimer_cb = QCheckBox("我已阅读并同意以上内容")
        self._disclaimer_cb.setCursor(Qt.PointingHandCursor)
        self._disclaimer_cb.setStyleSheet(
            f"QCheckBox{{color:{COLORS.get('TEXT', '#111827')};font-size:13px;"
            f"font-weight:600;font-family:{FONT_STACK};background:transparent;"
            f"spacing:8px;}}"
            f"QCheckBox::indicator{{width:18px;height:18px;"
            f"border:1.5px solid {COLORS.get('BORDER', '#D1D5DB')};"
            f"border-radius:4px;background:{COLORS.get('CARD', '#FFFFFF')};}}"
            f"QCheckBox::indicator:checked{{"
            f"background:{COLORS.get('PRIM_DARK', '#111827')};"
            f"border-color:{COLORS.get('PRIM_DARK', '#111827')};}}"
        )
        self._disclaimer_cb.stateChanged.connect(self._on_disclaimer_changed)
        cb_row.addWidget(self._disclaimer_cb)
        cb_row.addStretch(1)
        lay.addLayout(cb_row)
        lay.addSpacing(12)

        self._disclaimer_arrow = self._add_arrow_btn(lay, self._go_next)
        self._disclaimer_arrow.setEnabled(False)  # 未勾选时禁用
        lay.addStretch(1)

    def _on_disclaimer_changed(self, state):
        self._disclaimer_arrow.setEnabled(bool(state))

    # --- Page 4: 开始使用 ---
    def _build_page_done(self):
        page, lay = self._make_page()
        lay.addStretch(2)
        self._add_whale(lay, size=80)
        lay.addWidget(_page_title("开始使用"))
        lay.addStretch(1)
        self._add_arrow_btn(lay, self._finish)
        lay.addStretch(1)

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------
    def _go_next(self):
        idx = self._stack.currentIndex()
        if idx < self._stack.count() - 1:
            self._stack.setCurrentIndex(idx + 1)
            # 页面切换后聚焦输入框（如果有）
            w = self._stack.currentWidget()
            for child in w.findChildren(QLineEdit):
                child.setFocus()
                break

    def _finish(self):
        if self._is_closing:
            return
        self.completed.emit(dict(self._config))
        self.accept()

    # ------------------------------------------------------------------
    # 动画与生命周期
    # ------------------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        if self._shown_once:
            return
        self._shown_once = True
        try:
            pop_in(self._card, duration_ms=200, start_scale=0.92,
                   easing=QEasingCurve.OutBack)
            fade_in(self._card, duration_ms=180, easing=QEasingCurve.OutCubic)
        except Exception:
            pass

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Escape:
            return  # 禁止 ESC 关闭
        super().keyPressEvent(event)

    def accept(self):
        if self._is_closing:
            return
        self._is_closing = True
        remaining = [2]

        def _finish_accept():
            remaining[0] -= 1
            if remaining[0] <= 0:
                try:
                    super(WelcomeFlow, self).accept()
                except Exception:
                    pass

        try:
            pop_out(self._card, duration_ms=180, on_finished=_finish_accept)
        except Exception:
            _finish_accept()
        try:
            fade_out(self._card, duration_ms=150, on_finished=_finish_accept)
        except Exception:
            _finish_accept()

    def reject(self):
        # 允许用户跳过向导（需确认）
        try:
            reply = QMessageBox.question(
                self,
                tr("welcome_skip_title"),
                tr("welcome_skip_msg"),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                super().reject()
        except Exception:
            super().reject()

__all__ = ["WelcomeFlow"]
