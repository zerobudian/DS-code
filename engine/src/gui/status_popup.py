# -*- coding: utf-8 -*-
"""输入区顶部「模型 · 权限」标签点击后弹出的三页设置面板。

页面:
- 模型页: API Key / Base URL / 模型选择 / Temperature / Max Tokens
- 思考深度页: 思考深度档位 / 是否展开思考链 / 思考链长度
- 权限模式页: 审查者 / 码农 / 完全体 单选

对外信号:
  sig_model_changed(str)        模型名变更
  sig_thinking_changed(dict)    思考设置变更 {reasoning_effort, show_chain, chain_length}
  sig_permission_changed(str)   权限模式变更 (reviewer/coder/full)
"""

from PySide6.QtCore import Qt, QSize, Signal, QRect, QPoint, QPropertyAnimation, QEasingCurve, QEvent
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFrame, QPushButton,
    QLineEdit, QComboBox, QSlider, QSpinBox, QCheckBox,
    QStackedWidget, QWidget, QButtonGroup, QRadioButton,
    QGraphicsOpacityEffect, QApplication,
)

from src.gui.style import COLORS, RADII, SIZES, FONT_STACK, make_label, make_line_edit, make_section_label, tr, ThemeManager
from src.gui.icon_factory import get_icon
from src.gui.animations import pop_in, pop_out, fade_in, fade_out, HoverCard
from src.core.config import Config


ANIM_DURATION = 200


def _label(text, bold=False, color=None, font_size=None, weight=None):
    fs = font_size or "14px"
    if weight is None:
        weight = "700" if bold else "400"
    return make_label(text, font_size=fs, weight=weight, color=color)


def _section_label(text):
    """分组小标题（带左侧色条）。"""
    return make_section_label(text, bar_width=4, bar_height=20,
                              font_size="13px", font_weight="600")


def _line_edit(default="", echo=None, placeholder=""):
    return make_line_edit(placeholder=placeholder, text=default, echo=echo,
                          height=40, padding="0 12px", font_size="13px")


def _combo_box(editable=True):
    cb = QComboBox()
    cb.setEditable(editable)
    cb.setFixedHeight(40)
    cb.setStyleSheet(
        "QComboBox{background:" + COLORS['CARD'] + ";color:" + COLORS['TEXT'] + ";"
        "border:1px solid " + COLORS['BORDER'] + ";"
        "border-radius:" + RADII['md'] + ";padding:0 12px;font-size:13px;"
        "font-family:" + FONT_STACK + ";}"
        "QComboBox:focus{border:1px solid " + COLORS['PRIM_DARK'] + ";}"
        "QComboBox::drop-down{border:none;width:24px;}"
        "QComboBox::down-arrow{image:none;border:none;}"
        "QComboBox QAbstractItemView{background:" + COLORS['CARD'] + ";"
        "color:" + COLORS['TEXT'] + ";border:1px solid " + COLORS['BORDER'] + ";"
        "border-radius:" + RADII['md'] + ";selection-background-color:" + COLORS['PRIM'] + ";}"
    )
    return cb


def _spin_box(min_val=0, max_val=100, suffix=None):
    sb = QSpinBox()
    sb.setRange(min_val, max_val)
    if suffix:
        sb.setSuffix(suffix)
    sb.setFixedHeight(40)
    sb.setStyleSheet(
        "QSpinBox{background:" + COLORS['CARD'] + ";color:" + COLORS['TEXT'] + ";"
        "border:1px solid " + COLORS['BORDER'] + ";"
        "border-radius:" + RADII['md'] + ";padding:0 12px;font-size:13px;"
        "font-family:" + FONT_STACK + ";}"
        "QSpinBox:focus{border:1px solid " + COLORS['PRIM_DARK'] + ";}"
    )
    return sb


def _check_box(text=""):
    cb = QCheckBox(text)
    cb.setCursor(Qt.PointingHandCursor)
    cb.setStyleSheet(
        "QCheckBox{color:" + COLORS['TEXT'] + ";font-size:13px;"
        "font-family:" + FONT_STACK + ";spacing:8px;}"
        "QCheckBox::indicator{width:18px;height:18px;"
        "border:1px solid " + COLORS['BORDER'] + ";border-radius:5px;"
        "background:" + COLORS['CARD'] + ";}"
        "QCheckBox::indicator:checked{background:" + COLORS['PRIM_DARK'] + ";"
        "border-color:" + COLORS['PRIM_DARK'] + ";}"
    )
    return cb


def _radio_card(title: str, desc: str, group: QButtonGroup):
    """权限模式单选卡片：白色底 + 浅蓝悬停 + 左侧选中条。

    背景色/边框使用 QPropertyAnimation 做 hover 与选中态插值，避免 QTimer 逐帧改 stylesheet。
    """
    card = HoverCard(
        normal_bg=COLORS['CARD'],
        hover_bg=COLORS['PRIM'],
        selected_bg=COLORS['PRIM'],
        border_color=COLORS['BORDER'],
        border_radius=RADII['lg'],
    )
    card.setObjectName("RadioCard")
    card.setCursor(Qt.PointingHandCursor)

    lay = QHBoxLayout(card)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(12)

    radio = QRadioButton()
    radio.setCursor(Qt.PointingHandCursor)
    radio.setStyleSheet(
        "QRadioButton::indicator{width:18px;height:18px;"
        "border:1px solid " + COLORS['BORDER'] + ";border-radius:" + RADII['circle'] + ";"
        "background:" + COLORS['CARD'] + ";}"
        "QRadioButton::indicator:checked{background:" + COLORS['PRIM_DARK'] + ";"
        "border-color:" + COLORS['PRIM_DARK'] + ";}"
    )
    group.addButton(radio)
    lay.addWidget(radio, 0, Qt.AlignTop)

    text_col = QVBoxLayout()
    text_col.setSpacing(4)
    t = _label(title, weight="700", font_size="14px")
    text_col.addWidget(t)
    d = _label(desc, color=COLORS['TEXT_3'], font_size="12px", weight="400")
    d.setWordWrap(True)
    text_col.addWidget(d)
    lay.addLayout(text_col, 1)

    # 点击卡片等同于点击 radio
    def _on_card_click(event):
        radio.setChecked(True)
    card.mousePressEvent = _on_card_click

    return card, radio


# ---------------------------------------------------------------------------
# 三页弹窗
# ---------------------------------------------------------------------------
class StatusPopup(QDialog):
    sig_model_changed = Signal(str)
    sig_thinking_changed = Signal(dict)
    sig_permission_changed = Signal(str)

    _REASONING_OPTIONS = [
        ("high", None),  # text set via tr() at build time
        ("max", None),
    ]

    _PERMISSION_OPTIONS = [
        ("reviewer", None, None),
        ("coder", None, None),
        ("full", None, None),
    ]

    def _get_reasoning_label(self, key: str) -> str:
        return {"high": tr("reasoning_high"), "max": tr("reasoning_max")}.get(key, key)

    def _get_perm_info(self, value: str):
        return {
            "reviewer": (tr("popup_perm_reviewer_title"), tr("popup_perm_reviewer_desc")),
            "coder": (tr("popup_perm_coder_title"), tr("popup_perm_coder_desc")),
            "full": (tr("popup_perm_full_title"), tr("popup_perm_full_desc")),
        }.get(value, (value, ""))

    def __init__(self, parent=None, language="zh"):
        super().__init__(parent)
        self._lang = language if language else "zh"
        self.cfg = Config()
        self._loading = True  # 初始化期间避免重复写入配置
        self._is_closing = False
        self._shown_once = False
        self._tab_anim = None

        self.setWindowTitle("")
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setFixedSize(SIZES["popup"]["status_width"], SIZES["popup"]["status_height"])

        # 根容器：白色大卡片，24px 圆角
        self._root = QFrame()
        self._root.setObjectName("StatusPopupRoot")
        self._root.setStyleSheet(
            "QFrame#StatusPopupRoot{background:" + COLORS['CARD'] + ";"
            "border:1px solid " + COLORS['BORDER'] + ";"
            "border-radius:" + RADII['xl'] + ";}"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.addWidget(self._root)

        v = QVBoxLayout(self._root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # 顶部标题 + Tab
        header = QWidget()
        header.setStyleSheet("background:transparent;")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(24, 20, 24, 14)
        hl.setSpacing(14)

        title_row = QHBoxLayout()
        title_row.addWidget(_label(tr("popup_title"), weight="700", font_size="20px"))
        title_row.addStretch(1)
        self.mode_label = _label(tr("popup_mode_label") + "：" + tr("popup_mode_plan"), color=COLORS['TEXT_3'], font_size="12px", weight="600")
        title_row.addWidget(self.mode_label)
        title_row.addSpacing(8)
        close_btn = QPushButton()
        close_btn.setFixedSize(SIZES["popup"]["close_btn"], SIZES["popup"]["close_btn"])
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setIcon(get_icon("close", SIZES["button"]["icon_sm"]))
        close_btn.setIconSize(QSize(SIZES["button"]["icon_sm"], SIZES["button"]["icon_sm"]))
        close_btn.setStyleSheet(
            "QPushButton{background:transparent;color:" + COLORS['TEXT_3'] + ";"
            "border:none;border-radius:" + RADII['circle'] + ";font-size:14px;font-weight:700;}"
            "QPushButton:hover{background:" + COLORS['PRIM'] + ";color:" + COLORS['TEXT'] + ";}"
        )
        close_btn.clicked.connect(self.reject)
        title_row.addWidget(close_btn)
        hl.addLayout(title_row)

        self._tabs = []
        tab_row = QHBoxLayout()
        tab_row.setSpacing(6)
        tab_names = [tr("popup_tab_model"), tr("popup_tab_thinking"), tr("popup_tab_permission")]
        for i, name in enumerate(tab_names):
            btn = QPushButton(name)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(36)
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, idx=i: self._set_page(idx))
            tab_row.addWidget(btn, 1)
            self._tabs.append(btn)
        hl.addLayout(tab_row)
        v.addWidget(header)

        # 分隔线
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(
            "QFrame{background:" + COLORS['BORDER'] + ";max-height:1px;min-height:1px;border:none;}"
        )
        v.addWidget(line)

        # 页面堆叠
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background:transparent;")
        v.addWidget(self._stack, 1)

        self._build_model_page()
        self._build_thinking_page()
        self._build_permission_page()

        # 底部提示
        footer = QWidget()
        footer.setStyleSheet("background:transparent;")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(24, 10, 24, 16)
        hint = _label(tr("popup_footer"), color=COLORS['TEXT_3'], font_size="11px", weight="400")
        fl.addWidget(hint)
        fl.addStretch(1)
        v.addWidget(footer)

        # 初始选中第一页
        self._set_page(0, animate=False)
        self._refresh_tab_styles()

        self._loading = False

        # 显示当前工作模式
        self.set_current_mode(self.cfg.get("chat_mode", "normal"))

        # 入场动画在 showEvent 中执行，确保 geometry 已确定
        self._center_on_parent()

        # 安装全局事件过滤器：点击弹窗外部空白处关闭
        try:
            QApplication.instance().installEventFilter(self)
        except Exception:
            pass

        # 订阅集中式主题变更信号
        try:
            ThemeManager.instance().theme_changed.connect(self._rebuild_styles)
        except Exception:
            pass

    # ---------------- 页面构建 ----------------
    def _build_model_page(self):
        page = QWidget()
        page.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(16)

        # API Key
        lay.addWidget(_section_label(tr("popup_api_key")))
        self.api_key_edit = _line_edit(self.cfg.api_key or "", echo=QLineEdit.Password, placeholder="sk-...")
        self.api_key_edit.textChanged.connect(self._on_api_key_changed)
        lay.addWidget(self.api_key_edit)

        # Base URL
        lay.addWidget(_section_label(tr("popup_base_url")))
        self.base_url_edit = _line_edit(
            self.cfg.get("base_url") or "https://api.deepseek.com",
            placeholder="https://api.deepseek.com"
        )
        self.base_url_edit.textChanged.connect(self._on_base_url_changed)
        lay.addWidget(self.base_url_edit)

        # 模型选择
        lay.addWidget(_section_label(tr("popup_model_select")))
        self.model_combo = _combo_box(editable=True)
        models = self.cfg.get_available_models() or []
        current_model = self.cfg.model or "deepseek-v4-pro"
        model_names = []
        for m in models:
            if isinstance(m, dict) and m.get("name"):
                model_names.append(str(m["name"]))
        if not model_names:
            model_names = ["deepseek-v4-pro", "deepseek-v4-flash"]
        if current_model not in model_names:
            model_names.insert(0, current_model)
        for name in model_names:
            self.model_combo.addItem(name)
        self.model_combo.setCurrentText(current_model)
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        lay.addWidget(self.model_combo)

        # Temperature + Max Tokens 横向
        row = QHBoxLayout()
        row.setSpacing(16)

        temp_col = QVBoxLayout()
        temp_col.setSpacing(6)
        temp_lbl = _label(tr("popup_temperature"), weight="600", font_size="13px")
        temp_lbl.setToolTip(tr("popup_temperature_tooltip"))
        temp_col.addWidget(temp_lbl)
        temp_row = QHBoxLayout()
        temp_row.setSpacing(10)
        self.temp_slider = QSlider(Qt.Horizontal)
        self.temp_slider.setRange(0, 20)
        self.temp_slider.setValue(int(self.cfg.get("temperature", 1.0) * 10))
        self.temp_slider.setToolTip(tr("popup_temperature_tooltip"))
        self.temp_slider.setStyleSheet(
            "QSlider::groove:horizontal{height:6px;background:" + COLORS['BORDER'] + ";"
            "border-radius:3px;}"
            "QSlider::handle:horizontal{width:16px;height:16px;background:" + COLORS['PRIM_DARK'] + ";"
            "border-radius:" + RADII['circle'] + ";margin:-5px 0;}"
            "QSlider::sub-page:horizontal{background:" + COLORS['PRIM_DARK'] + ";border-radius:3px;}"
        )
        self.temp_value = _label(f"{self.temp_slider.value() / 10.0:.1f}", weight="700", font_size="13px")
        self.temp_slider.valueChanged.connect(self._on_temperature_changed)
        temp_row.addWidget(self.temp_slider, 1)
        temp_row.addWidget(self.temp_value)
        temp_col.addLayout(temp_row)
        row.addLayout(temp_col, 1)

        token_col = QVBoxLayout()
        token_col.setSpacing(6)
        token_lbl = _label(tr("popup_max_tokens"), weight="600", font_size="13px")
        token_lbl.setToolTip(tr("popup_max_tokens_tooltip"))
        token_col.addWidget(token_lbl)
        self.max_tokens_spin = _spin_box(1, 128000)
        self.max_tokens_spin.setToolTip(tr("popup_max_tokens_tooltip"))
        self.max_tokens_spin.setValue(int(self.cfg.get("max_tokens", 8192)))
        self.max_tokens_spin.valueChanged.connect(self._on_max_tokens_changed)
        token_col.addWidget(self.max_tokens_spin)
        row.addLayout(token_col, 1)

        lay.addLayout(row)
        lay.addStretch(1)
        self._stack.addWidget(page)

    def _build_thinking_page(self):
        page = QWidget()
        page.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(18)

        # 思考深度
        lay.addWidget(_section_label(tr("popup_reasoning_label")))
        depth_frame = QFrame()
        depth_frame.setStyleSheet(
            "QFrame{background:" + COLORS['PRIM'] + ";border-radius:" + RADII['lg'] + ";}"
        )
        depth_lay = QHBoxLayout(depth_frame)
        depth_lay.setContentsMargins(6, 6, 6, 6)
        depth_lay.setSpacing(4)
        self._depth_buttons = []
        current_depth = self.cfg.reasoning_effort or "high"
        for key, _lbl_text in self._REASONING_OPTIONS:
            btn = QPushButton(self._get_reasoning_label(key))
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(36)
            btn.clicked.connect(lambda checked, k=key: self._on_depth_clicked(k))
            depth_lay.addWidget(btn, 1)
            self._depth_buttons.append((key, btn))
        lay.addWidget(depth_frame)
        self._set_depth_button(current_depth)

        # 展开思考链
        chain_row = QHBoxLayout()
        chain_row.setSpacing(10)
        self.show_chain_cb = _check_box(tr("popup_show_chain"))
        self.show_chain_cb.setChecked(bool(self.cfg.get("show_thinking_chain", True)))
        self.show_chain_cb.stateChanged.connect(self._on_show_chain_changed)
        chain_row.addWidget(self.show_chain_cb)
        chain_row.addStretch(1)
        lay.addLayout(chain_row)

        # 思考链长度
        length_row = QHBoxLayout()
        length_row.setSpacing(12)
        length_row.addWidget(_label(tr("popup_chain_length"), weight="600", font_size="13px"))
        self.chain_length_spin = _spin_box(1, 20)
        self.chain_length_spin.setValue(int(self.cfg.get("thinking_chain_length", 3)))
        self.chain_length_spin.valueChanged.connect(self._on_chain_length_changed)
        length_row.addWidget(self.chain_length_spin)
        length_row.addStretch(1)
        lay.addLayout(length_row)

        # 提示卡
        hint_card = QFrame()
        hint_card.setStyleSheet(
            "QFrame{background:" + COLORS['PRIM'] + ";border-radius:" + RADII['lg'] + ";}"
        )
        hint_lay = QVBoxLayout(hint_card)
        hint_lay.setContentsMargins(16, 14, 16, 14)
        hint_lay.setSpacing(6)
        hint_lay.addWidget(_label("提示", weight="600", font_size="13px"))
        hint_lay.addWidget(_label(
            "思考链深度越高，模型会生成更详细的内部推理过程；"
            "展开显示会在对话区呈现思考链内容。",
            color=COLORS['TEXT_3'], font_size="12px", weight="400"
        ))
        lay.addWidget(hint_card)
        lay.addStretch(1)
        self._stack.addWidget(page)

    def _build_permission_page(self):
        page = QWidget()
        page.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(14)

        lay.addWidget(_section_label(tr("popup_tab_permission")))

        self._perm_group = QButtonGroup(self)
        self._perm_cards = []
        current_perm = self.cfg.get("permission", "full")
        for value, _perm_title, _perm_desc in self._PERMISSION_OPTIONS:
            title, desc = self._get_perm_info(value)
            card, radio = _radio_card(title, desc, self._perm_group)
            card.setProperty("perm_value", value)
            radio.toggled.connect(lambda checked, v=value: self._on_permission_toggled(v, checked))
            lay.addWidget(card)
            self._perm_cards.append((value, card, radio))
            if value == current_perm:
                radio.setChecked(True)
        self._refresh_perm_card_styles()

        lay.addStretch(1)
        self._stack.addWidget(page)

    # ---------------- 事件处理 ----------------
    def _set_page(self, idx: int, animate=True):
        for i, btn in enumerate(self._tabs):
            btn.blockSignals(True)
            btn.setChecked(i == idx)
            btn.blockSignals(False)
        self._refresh_tab_styles()

        old_idx = self._stack.currentIndex()
        if old_idx == idx or old_idx < 0:
            self._stack.setCurrentIndex(idx)
            return

        # 快速切换时若已有动画在运行，直接切页避免动画叠加
        if self._tab_anim is not None:
            try:
                self._tab_anim.stop()
            except Exception:
                pass
            self._tab_anim = None
            self._cleanup_page_effects()
            self._stack.setCurrentIndex(idx)
            return

        if not animate:
            self._stack.setCurrentIndex(idx)
            return

        old_widget = self._stack.widget(old_idx)
        new_widget = self._stack.widget(idx)
        if old_widget is None or new_widget is None:
            self._stack.setCurrentIndex(idx)
            return

        # 旧页淡出
        old_eff = QGraphicsOpacityEffect(old_widget)
        old_eff.setOpacity(1.0)
        old_widget.setGraphicsEffect(old_eff)
        old_anim = QPropertyAnimation(old_eff, b"opacity", self)
        old_anim.setDuration(ANIM_DURATION)
        old_anim.setStartValue(1.0)
        old_anim.setEndValue(0.0)
        old_anim.setEasingCurve(QEasingCurve.InCubic)

        def _after_old():
            self._tab_anim = None
            try:
                old_widget.setGraphicsEffect(None)
            except Exception:
                pass
            self._stack.setCurrentIndex(idx)
            self._animate_new_page(new_widget)

        old_anim.finished.connect(_after_old)
        old_anim.start(QPropertyAnimation.DeleteWhenStopped)
        self._tab_anim = old_anim

    def _animate_new_page(self, widget):
        """新页淡入 + 从下向上轻微滑入。"""
        try:
            eff = QGraphicsOpacityEffect(widget)
            eff.setOpacity(0.0)
            widget.setGraphicsEffect(eff)
            fade = QPropertyAnimation(eff, b"opacity", self)
            fade.setDuration(ANIM_DURATION)
            fade.setStartValue(0.0)
            fade.setEndValue(1.0)
            fade.setEasingCurve(QEasingCurve.OutCubic)

            end_geom = widget.geometry()
            start_geom = QRect(end_geom).translated(0, 12)
            widget.setGeometry(start_geom)
            slide = QPropertyAnimation(widget, b"geometry", self)
            slide.setDuration(ANIM_DURATION)
            slide.setStartValue(start_geom)
            slide.setEndValue(end_geom)
            slide.setEasingCurve(QEasingCurve.OutCubic)

            def _cleanup():
                try:
                    widget.setGraphicsEffect(None)
                    widget.setGeometry(end_geom)
                except Exception:
                    pass
                self._tab_anim = None

            fade.finished.connect(_cleanup)
            # 将新页动画也存入 _tab_anim，防止快速切换 tab 时动画叠加
            self._tab_anim = fade
            fade.start(QPropertyAnimation.DeleteWhenStopped)
            slide.start(QPropertyAnimation.DeleteWhenStopped)
        except Exception:
            pass

    def _cleanup_page_effects(self):
        """清理所有页签的 graphics effect，用于快速切换或关闭弹窗时。"""
        try:
            for i in range(self._stack.count()):
                w = self._stack.widget(i)
                if w is not None:
                    w.setGraphicsEffect(None)
        except Exception:
            pass

    def _refresh_tab_styles(self):
        active_ss = (
            "QPushButton{background:" + COLORS['PRIM_DARK'] + ";color:" + COLORS['CARD'] + ";"
            "border:none;border-radius:" + RADII['md'] + ";font-size:13px;"
            "font-weight:700;font-family:" + FONT_STACK + ";}"
        )
        inactive_ss = (
            "QPushButton{background:" + COLORS['PRIM'] + ";color:" + COLORS['TEXT'] + ";"
            "border:none;border-radius:" + RADII['md'] + ";font-size:13px;"
            "font-weight:600;font-family:" + FONT_STACK + ";}"
            "QPushButton:hover{background:" + COLORS['PRIM_LIGHT'] + ";}"
        )
        for i, btn in enumerate(self._tabs):
            btn.setStyleSheet(active_ss if btn.isChecked() else inactive_ss)

    def _set_depth_button(self, value: str):
        value = str(value).lower()
        for key, btn in self._depth_buttons:
            btn.blockSignals(True)
            btn.setChecked(key == value)
            btn.blockSignals(False)
        self._refresh_depth_styles()

    def _refresh_depth_styles(self):
        active_ss = (
            "QPushButton{background:" + COLORS['PRIM_DARK'] + ";color:" + COLORS['CARD'] + ";"
            "border:none;border-radius:" + RADII['md'] + ";font-size:13px;"
            "font-weight:700;font-family:" + FONT_STACK + ";}"
        )
        inactive_ss = (
            "QPushButton{background:" + COLORS['CARD'] + ";color:" + COLORS['TEXT'] + ";"
            "border:none;border-radius:" + RADII['md'] + ";font-size:13px;"
            "font-weight:600;font-family:" + FONT_STACK + ";}"
        )
        for key, btn in self._depth_buttons:
            btn.setStyleSheet(active_ss if btn.isChecked() else inactive_ss)

    def _on_depth_clicked(self, key: str):
        if self._loading:
            return
        self._set_depth_button(key)
        self.cfg.reasoning_effort = key
        self._emit_thinking()

    def _on_show_chain_changed(self, state):
        if self._loading:
            return
        self.cfg.set("show_thinking_chain", bool(state == Qt.Checked))
        self._emit_thinking()

    def _on_chain_length_changed(self, value: int):
        if self._loading:
            return
        self.cfg.set("thinking_chain_length", int(value))
        self._emit_thinking()

    def _on_api_key_changed(self, text: str):
        if self._loading:
            return
        self.cfg.api_key = text.strip()

    def _on_base_url_changed(self, text: str):
        if self._loading:
            return
        self.cfg.set("base_url", text.strip() or "https://api.deepseek.com")

    def _on_model_changed(self, text: str):
        if self._loading:
            return
        model = text.strip()
        if model:
            self.cfg.model = model
            self.sig_model_changed.emit(model)

    def _on_temperature_changed(self, value: int):
        if self._loading:
            return
        temp = value / 10.0
        self.temp_value.setText(f"{temp:.1f}")
        self.cfg.set("temperature", temp)

    def _on_max_tokens_changed(self, value: int):
        if self._loading:
            return
        self.cfg.set("max_tokens", int(value))

    def _on_permission_toggled(self, value: str, checked: bool):
        if self._loading:
            return
        if checked:
            self.cfg.set("permission", value)
            self.sig_permission_changed.emit(value)
            self._refresh_perm_card_styles()

    def _refresh_perm_card_styles(self):
        selected = None
        for value, card, radio in self._perm_cards:
            if radio.isChecked():
                selected = value
                break
        for value, card, radio in self._perm_cards:
            is_sel = (value == selected)
            # HoverCard 负责 hover/selected 背景色动画
            if isinstance(card, HoverCard):
                card.set_selected(is_sel)
                # 选中态使用深色边框，未选中使用普通边框
                card._border_color = COLORS['PRIM_DARK'] if is_sel else COLORS['BORDER']
                card._apply_style()

    def _emit_thinking(self):
        current_depth = "high"
        for key, btn in self._depth_buttons:
            if btn.isChecked():
                current_depth = key
                break
        self.sig_thinking_changed.emit({
            "reasoning_effort": current_depth,
            "show_chain": bool(self.show_chain_cb.isChecked()),
            "chain_length": int(self.chain_length_spin.value()),
        })

    # ---------------- 主题适配 ----------------
    def _rebuild_styles(self):
        """主题切换后重建所有控件样式表（从当前 COLORS 重新生成）。"""
        try:
            # 根容器
            self._root.setStyleSheet(
                "QFrame#StatusPopupRoot{background:" + COLORS['CARD'] + ";"
                "border:1px solid " + COLORS['BORDER'] + ";"
                "border-radius:" + RADII['xl'] + ";}"
            )
            # Tab 按钮样式
            self._refresh_tab_styles()
            # 深度按钮样式
            self._refresh_depth_styles()
            # 分隔线
            for child in self._root.findChildren(QFrame):
                if child.isFrameType() and child.frameShape() == QFrame.HLine:
                    child.setStyleSheet(
                        "QFrame{background:" + COLORS['BORDER'] + ";"
                        "max-height:1px;min-height:1px;border:none;}"
                    )
            # 关闭按钮
            close_btns = self._root.findChildren(QPushButton)
            for btn in close_btns:
                if btn.toolTip() == "" or btn.icon() is not None:
                    btn.setStyleSheet(
                        "QPushButton{background:transparent;color:" + COLORS['TEXT_3'] + ";"
                        "border:none;border-radius:" + RADII['circle'] + ";font-size:14px;font-weight:700;}"
                        "QPushButton:hover{background:" + COLORS['PRIM'] + ";color:" + COLORS['TEXT'] + ";}"
                    )
        except Exception:
            pass

    # ---------------- 动画 / 定位 ----------------
    def showEvent(self, event):
        super().showEvent(event)
        if self._shown_once:
            return
        self._shown_once = True
        self._center_on_parent()
        # pop_in (scale) + fade_in，使用 OutBack 弹簧缓动
        try:
            pop_in(self._root, duration_ms=200, start_scale=0.92, easing=QEasingCurve.OutBack)
            fade_in(self._root, duration_ms=180, easing=QEasingCurve.OutCubic)
        except Exception:
            pass

    def reject(self):
        if self._is_closing:
            return
        self._is_closing = True
        # 关闭前停止所有页签动画并清理 graphics effect
        if self._tab_anim is not None:
            try:
                self._tab_anim.stop()
            except Exception:
                pass
            self._tab_anim = None
        self._cleanup_page_effects()

        remaining = [2]

        def _finish():
            remaining[0] -= 1
            if remaining[0] <= 0:
                try:
                    super(StatusPopup, self).reject()
                except Exception:
                    pass

        try:
            pop_out(self._root, duration_ms=180, on_finished=_finish)
        except Exception:
            _finish()
        try:
            fade_out(self._root, duration_ms=150, on_finished=_finish)
        except Exception:
            _finish()

    def _center_on_parent(self):
        if self.parent() and hasattr(self.parent(), 'geometry'):
            pg = self.parent().geometry()
            cx = pg.center().x() - self.width() // 2
            cy = pg.center().y() - self.height() // 2
            self.move(cx, cy)
        else:
            try:
                from PySide6.QtWidgets import QApplication
                screen = QApplication.primaryScreen().geometry()
                cx = screen.center().x() - self.width() // 2
                cy = screen.center().y() - self.height() // 2
                self.move(cx, cy)
            except Exception:
                pass

    def eventFilter(self, obj, event):
        """点击弹窗外部空白处时关闭状态弹窗。"""
        try:
            if self.isVisible() and event.type() == QEvent.MouseButtonPress:
                pos = event.globalPosition().toPoint()
                # 以白色卡片 _root 为边界，避免 12px 阴影边距被算入内部
                root_top_left = self._root.mapToGlobal(QPoint(0, 0))
                root_rect = QRect(root_top_left, self._root.size())
                if not root_rect.contains(pos):
                    self.reject()
                    return True
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)

    def done(self, result):
        """弹窗关闭时移除全局事件过滤器、停止动画并断开主题信号。"""
        if self._tab_anim is not None:
            try:
                self._tab_anim.stop()
            except Exception:
                pass
            self._tab_anim = None
        self._cleanup_page_effects()
        try:
            QApplication.instance().removeEventFilter(self)
        except Exception:
            pass
        try:
            ThemeManager.instance().theme_changed.disconnect(self._rebuild_styles)
        except Exception:
            pass
        super().done(result)

    # ---------------- 公共 API ----------------
    def get_model(self) -> str:
        return self.model_combo.currentText().strip()

    def get_reasoning_effort(self) -> str:
        for key, btn in self._depth_buttons:
            if btn.isChecked():
                return key
        return "high"

    def get_permission(self) -> str:
        for value, _card, radio in self._perm_cards:
            if radio.isChecked():
                return value
        return "full"

    def set_current_mode(self, mode: str):
        """在弹窗顶部显示当前工作模式（普通 / 规划 / Ultra）。"""
        mode_names = {
            "normal": tr("popup_mode_normal"),
            "plan": tr("popup_mode_plan"),
            "ultra": tr("popup_mode_ultra"),
        }
        name = mode_names.get(str(mode).lower(), str(mode))
        if getattr(self, "mode_label", None) is not None:
            self.mode_label.setText(tr("popup_mode_label") + "：" + name)
