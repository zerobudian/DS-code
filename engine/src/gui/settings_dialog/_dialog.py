# -*- coding: utf-8 -*-
"""设置弹窗 — SettingsDialog 主类。

从原 src/gui/settings_dialog.py 拆分而来（仅做代码组织，不改变任何公开 API）。
UI 区段构建器与辅助组件位于 _sections.py。
"""
import os
from PySide6.QtCore import Qt, Signal, QPropertyAnimation, QEasingCurve, QEvent
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QLineEdit, QCheckBox, QMessageBox, QWidget, QScrollArea,
    QSizePolicy, QComboBox, QButtonGroup, QSlider, QApplication,
)
from src.gui.style import COLORS, RADII, SIZES, FONT_STACK, make_label, make_line_edit, make_text_edit, tr, apply_theme, get_current_theme, ThemeManager
from src.gui.animations import pop_in, pop_out, fade_in, fade_out, HoverColorButton
from src.gui._gui_helpers import enable_touch_scroll
from src.gui.settings_dialog._sections import (
    ASSETS_DIR, ANIM_DURATION, _OFFICIAL_PROVIDER_TAG, _APP_VERSION,
    _darken, _prim, _prim_dark, _prim_light, _card, _card_alt, _border_input,
    _label, _line_edit, _text_edit, _button, _anim_option_button,
    SegmentedControl, _build_card, _build_group, _new_group, ModelRow,
)

# ------------------------------------------------ SettingsDialog
class SettingsDialog(QDialog):
    """三卡片设置弹窗"""

    save_clicked = Signal(dict)
    export_requested = Signal()
    import_requested = Signal()
    add_language_pack_requested = Signal()

    def __init__(self, config=None, sklii_mgr=None, current_project: str = "", parent=None, initial_tab: str = "basic"):
        super().__init__(parent)
        self.setWindowTitle(tr("settings_title"))
        self.setMinimumSize(SIZES["popup"]["dialog_min_width"], SIZES["popup"]["dialog_min_height"])
        self.resize(880, 720)
        self.setStyleSheet(
            "QDialog{background:" + COLORS['BGD'] + ";"
            "border-radius:" + RADII['panel'] + ";}"
        )

        self.cfg = config or {}
        self.sklii_manager = sklii_mgr
        self.current_project = str(current_project or "")
        self._is_closing = False
        self._shown_once = False
        self._container_anim = None

        # 当前选中语言
        self._selected_language = self.cfg.get("language", "zh") or "zh"

        # 多供应商数据（内存模型）
        self._providers = []
        self._active_provider_index = 0
        self._syncing_ui = False
        self._load_providers_from_config()

        # 单供应商下的单条模型行（每家供应商仅一个模型）
        self._model_row = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        # 顶部标题
        header = QFrame()
        header.setFixedHeight(SIZES["popup"]["header_height"])
        header.setStyleSheet(
            "QFrame{background:" + COLORS['CARD'] + ";"
            "border-bottom:1px solid " + COLORS['DIVIDER'] + ";"
            "border-top-left-radius:16px;border-top-right-radius:16px;}"
        )
        hl = QVBoxLayout(header)
        hl.setContentsMargins(28, 22, 28, 14)
        hl.setSpacing(4)
        hl.addWidget(_label(tr("settings_title"), weight="700", font_size="22px"))
        sub = _label(tr("settings_subtitle"), color=COLORS['TEXT_3'], font_size="12px")
        hl.addWidget(sub)
        outer.addWidget(header)

        # Tab 导航栏
        self._tab_bar = SegmentedControl(
            [("basic", tr("basic_settings_tab")), ("provider", tr("provider_card_title")), ("about", tr("about_card_title"))],
            value_key="basic",
        )
        self._tab_bar.setFixedHeight(SIZES["popup"]["tab_height"])
        self._tab_bar.valueChanged.connect(self._on_tab_changed)
        tab_wrapper = QWidget()
        tab_wrapper.setStyleSheet("background:" + COLORS['BGD'] + ";")
        twl = QHBoxLayout(tab_wrapper)
        twl.setContentsMargins(24, 16, 24, 0)
        twl.addWidget(self._tab_bar)
        twl.addStretch(1)
        outer.addWidget(tab_wrapper)

        # 滚动容器
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea{background:" + COLORS['BGD'] + ";border:none;}"
            "QScrollBar:vertical{background:transparent;width:10px;}"
            "QScrollBar::handle:vertical{background:" + COLORS['BORDER'] + ";"
            "border-radius:5px;min-height:30px;}"
            "QScrollBar::handle:vertical:hover{background:" + COLORS['PRIM_DARK'] + ";}"
        )

        scroll_content = QWidget()
        scroll_content.setStyleSheet("background:" + COLORS['BGD'] + ";")
        content_lay = QVBoxLayout(scroll_content)
        content_lay.setContentsMargins(24, 16, 24, 20)
        content_lay.setSpacing(18)

        # 三张卡片，分别放入可切换的容器
        self._basic_card = self._build_basic_card()
        self._provider_card = self._build_provider_card()
        self._about_card = self._build_about_card()
        self._pages = {
            "basic": self._basic_card,
            "provider": self._provider_card,
            "about": self._about_card,
        }
        for w in self._pages.values():
            w.setVisible(False)
            content_lay.addWidget(w)
        self._show_tab(initial_tab if initial_tab in ("basic", "provider", "about") else "basic")

        content_lay.addStretch(1)
        scroll.setWidget(scroll_content)
        enable_touch_scroll(scroll)
        outer.addWidget(scroll, 1)

        # 底部保存按钮
        footer = QWidget()
        footer.setStyleSheet("background:" + COLORS['BGD'] + ";")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(24, 14, 24, 12)
        fl.addStretch(1)

        cancel = _button(tr("cancel"), primary=False, width=110)
        cancel.clicked.connect(self.reject)
        fl.addWidget(cancel)

        save = _button(tr("save_settings"), primary=True, width=140)
        save.clicked.connect(self._on_save)
        fl.addWidget(save)
        outer.addWidget(footer)

        # 最底部 footer 文字（点击跳转 dscode.pages.dev）
        try:
            from src.gui.welcome_screen import _build_footer
            self.footer = _build_footer(self)
            outer.addWidget(self.footer)
        except Exception:
            # 降级：直接添加普通 footer
            pass

        # 模式初始化
        saved_mode = self.cfg.get("chat_mode", "normal")
        self._set_mode(saved_mode)

        # 记录进入弹窗时的主题，用于取消时回滚
        self._initial_theme = get_current_theme()

        # 安装全局事件过滤器：点击弹窗外部空白处关闭
        try:
            QApplication.instance().installEventFilter(self)
        except Exception:
            pass

        # 订阅集中式主题变更信号，自动重建弹窗自身样式
        try:
            ThemeManager.instance().theme_changed.connect(self._rebuild_styles)
        except Exception:
            pass

    def eventFilter(self, obj, event):
        """点击弹窗外部任意空白处时关闭设置弹窗。"""
        try:
            if self.isVisible() and event.type() == QEvent.MouseButtonPress:
                pos = event.globalPosition().toPoint()
                if not self.geometry().contains(pos):
                    self.reject()
                    return True
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def _rebuild_styles(self):
        """主题切换后重建设置弹窗样式。"""
        try:
            self.setStyleSheet("QDialog{background:" + COLORS['BGD'] + ";}")
            for card in getattr(self, '_pages', {}).values():
                if card:
                    card.setStyleSheet(
                        "QFrame{background:" + COLORS['CARD'] + ";"
                        "border:1px solid " + COLORS['BORDER'] + ";"
                        "border-radius:" + RADII['card'] + ";}"
                    )
        except Exception:
            pass

    def _on_toggle_api_key_visibility(self):
        """切换 API Key 输入框的密码显示/隐藏。"""
        if self.api_key_edit.echoMode() == QLineEdit.Password:
            self.api_key_edit.setEchoMode(QLineEdit.Normal)
            self.api_key_toggle_btn.setText(tr("hide_password"))
        else:
            self.api_key_edit.setEchoMode(QLineEdit.Password)
            self.api_key_toggle_btn.setText(tr("show_password"))

    # ------------------------- Tab 切换
    def _on_tab_changed(self, key: str):
        self._show_tab(key)

    def _show_tab(self, key: str):
        for k, w in self._pages.items():
            w.setVisible(k == key)
        self._tab_bar.set_value(key)

    # ------------------------- 卡片1: 基础设置
    def _build_basic_card(self):
        card, lay = _build_card(tr("basic_settings_tab"), tr("basic_settings_subtitle"))


        # 分组 - 主题
        _, g = _build_group(lay)
        g.addWidget(_label(tr("theme_label"), weight="600", font_size="14px"))
        init_theme = self._resolve_initial_theme()
        self.theme_control = SegmentedControl(
            [("light", tr("theme_light")), ("dark", tr("theme_dark")), ("system", tr("theme_system"))],
            value_key=init_theme,
        )
        self.theme_control.valueChanged.connect(self._on_theme_preview)
        g.addWidget(self.theme_control)

        # 分组 - 语言
        _, lg = _build_group(lay)
        lg.addWidget(_label(tr("language_label"), weight="600", font_size="14px"))
        lang_row = QHBoxLayout()
        lang_row.setSpacing(10)

        self.language_combo = QComboBox()
        self.language_combo.setFixedHeight(40)
        self.language_combo.setStyleSheet(
            "QComboBox{background:" + _card() + ";color:" + COLORS['TEXT'] + ";"
            "border:1px solid " + _border_input() + ";border-radius:" + RADII["md"] + ";"
            "padding:0 12px;font-size:14px;font-family:" + FONT_STACK + ";min-width:180px;}"
            "QComboBox::drop-down{border:none;width:24px;}"
            "QComboBox::down-arrow{image:none;border-left:5px solid transparent;"
            "border-right:5px solid transparent;border-top:5px solid " + COLORS['TEXT_3'] + ";}"
            "QComboBox QAbstractItemView{background:" + _card() + ";color:" + COLORS['TEXT'] + ";"
            "border:1px solid " + _border_input() + ";selection-background-color:" + _prim_light() + ";}"
        )
        self._populate_language_combo()
        self.language_combo.currentIndexChanged.connect(self._on_language_selected)
        lang_row.addWidget(self.language_combo)

        self.add_lang_btn = _button(tr("add_language_pack"), primary=False, height=40)
        self.add_lang_btn.clicked.connect(lambda: self.add_language_pack_requested.emit())
        lang_row.addWidget(self.add_lang_btn)
        lang_row.addStretch(1)
        lg.addLayout(lang_row)

        # 分组 - 思考深度（QSlider: high ↔ max，两档）
        _, g = _build_group(lay)
        g.addWidget(_label(tr("thinking_depth_label"), weight="600", font_size="14px"))

        # Slider 行
        slider_row = QHBoxLayout()
        slider_row.setSpacing(12)
        self._reasoning_slider = QSlider(Qt.Horizontal)
        self._reasoning_slider.setRange(0, 1)  # 0=high, 1=max
        self._reasoning_slider.setFixedHeight(32)
        self._reasoning_slider.setStyleSheet(
            "QSlider::groove:horizontal{background:" + COLORS['BORDER'] + ";"
            "height:4px;border-radius:2px;}"
            "QSlider::handle:horizontal{background:" + _prim() + ";"
            "width:18px;height:18px;margin:-7px 0;border-radius:9px;"
            "border:2px solid " + COLORS['CARD'] + ";}"
            "QSlider::sub-page:horizontal{background:" + _prim() + ";"
            "height:4px;border-radius:2px;}"
        )
        init_reasoning = self.cfg.get("reasoning_effort", "high")
        self._reasoning_slider.setValue(1 if init_reasoning == "max" else 0)
        slider_row.addWidget(self._reasoning_slider, 1)
        g.addLayout(slider_row)

        # Slider 标签
        label_row = QHBoxLayout()
        label_row.setSpacing(0)
        lbl_high = _label(tr("thinking_high"), color=COLORS['TEXT_3'], font_size="12px", weight="500")
        lbl_max = _label(tr("thinking_max"), color=COLORS['TEXT_3'], font_size="12px", weight="500")
        label_row.addWidget(lbl_high)
        label_row.addStretch(1)
        label_row.addWidget(lbl_max)
        g.addLayout(label_row)

        # 保留 _reasoning_buttons/_reasoning_values 为兼容接口（内部从slider读取）
        self._reasoning_values = ["high", "max"]

        # 分组 - 权限
        _, g = _build_group(lay)
        g.addWidget(_label(tr("permission_label"), weight="600", font_size="14px"))
        perm_row = QHBoxLayout()
        perm_row.setSpacing(8)
        self._perm_buttons = []
        self._perm_values = ["reviewer", "coder", "full"]
        perm_group = QButtonGroup(self)
        perm_group.setExclusive(True)
        for i, label in enumerate([tr("perm_reviewer"), tr("perm_coder"), tr("perm_full")]):
            b = _anim_option_button(label, padding="0 18px")
            perm_group.addButton(b)
            perm_row.addWidget(b)
            self._perm_buttons.append(b)
        perm_row.addStretch(1)
        g.addLayout(perm_row)
        # 初始化权限选中（优先新版 permission_mode，旧版 permission_level 向后兼容）
        init_perm = self.cfg.get("permission_mode") or self.cfg.get("permission_level", "full")
        if init_perm not in self._perm_values:
            init_perm = "full"
        for i, pv in enumerate(self._perm_values):
            if pv == init_perm:
                self._perm_buttons[i].setChecked(True)
                break

        # 分组 - 模型选择（flash/pro 快捷按钮）
        _, mg = _build_group(lay)
        mg.addWidget(_label(tr("model_label"), weight="600", font_size="14px"))
        model_row = QHBoxLayout()
        model_row.setSpacing(8)
        self._model_buttons = []
        self._model_values = ["deepseek-v4-flash", "deepseek-v4-pro"]
        model_group = QButtonGroup(self)
        model_group.setExclusive(True)
        for label in [tr("model_flash"), tr("model_pro")]:
            b = _anim_option_button(label, padding="0 18px")
            model_group.addButton(b)
            model_row.addWidget(b)
            self._model_buttons.append(b)
        model_row.addStretch(1)
        mg.addLayout(model_row)
        init_model = self.cfg.get("model", "deepseek-v4-flash")
        if init_model not in self._model_values:
            init_model = "deepseek-v4-flash"
        for i, mv in enumerate(self._model_values):
            if mv == init_model:
                self._model_buttons[i].setChecked(True)
                break

        # 分组 - 模式
        mode_subtitle = "{nm}:{nd} · {pm}:{pd} · {um}:{ud}".format(
            nm=tr("mode_normal"), nd=tr("mode_normal_desc"),
            pm=tr("mode_plan"), pd=tr("mode_plan_desc"),
            um=tr("mode_ultra"), ud=tr("mode_ultra_desc"),
        )
        mode_g = _new_group(
            tr("mode_label"),
            mode_subtitle,
        )
        lay.addWidget(mode_g)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self._mode_buttons = []
        self._mode_values = []
        mode_options = [(tr("mode_normal"), "normal"), (tr("mode_plan"), "plan"), (tr("mode_ultra"), "ultra")]
        mode_group = QButtonGroup(self)
        mode_group.setExclusive(True)
        for label, val in mode_options:
            b = _anim_option_button(label, padding="0 16px")
            mode_group.addButton(b)
            mode_row.addWidget(b)
            self._mode_buttons.append(b)
            self._mode_values.append(val)
        mode_row.addStretch(1)
        mode_g.layout().addLayout(mode_row)

        # 分组 - 数据管理
        _, g = _build_group(lay)
        g.addWidget(_label(tr("data_management"), weight="600", font_size="14px"))
        row = QHBoxLayout()
        row.setSpacing(10)
        b_import = _button(tr("import_project"), primary=False)
        b_import.clicked.connect(lambda: self.import_requested.emit())
        b_export = _button(tr("export_project"), primary=False)
        b_export.clicked.connect(lambda: self.export_requested.emit())
        row.addWidget(b_import)
        row.addWidget(b_export)
        row.addStretch(1)
        g.addLayout(row)

        # 分组 - 扩展工具
        _, g = _build_group(lay)
        g.addWidget(_label(tr("extension_tools"), weight="600", font_size="14px"))
        row2 = QHBoxLayout()
        row2.setSpacing(10)
        b_mcp = _button(tr("custom_mcp"), primary=False)
        b_mcp.clicked.connect(lambda: QMessageBox.information(self, tr("coming_soon"), tr("mcp_coming_soon")))
        b_skill = _button(tr("add_skill"), primary=False)
        b_skill.clicked.connect(lambda: QMessageBox.information(self, tr("coming_soon"), tr("skill_coming_soon")))
        row2.addWidget(b_mcp)
        row2.addWidget(b_skill)
        row2.addStretch(1)
        g.addLayout(row2)

        # 分组 - 链接
        _, g = _build_group(lay)
        row3 = QHBoxLayout()
        row3.setSpacing(24)
        row3.addWidget(self._link_label(tr("user_agreement"), "https://dscodehelp.pages.dev"))
        row3.addWidget(self._link_label(tr("feedback"), "https://dscode.pages.dev"))
        row3.addStretch(1)
        g.addLayout(row3)

        # 分组 - 版本信息
        _, g = _build_group(lay)
        version_lbl = _label(tr("about_version_fmt", ver=_APP_VERSION), color=COLORS['TEXT_3'], font_size="12px")
        version_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        g.addWidget(version_lbl)

        return card

    def _link_label(self, text, url):
        lbl = QLabel('<a href="' + url + '" style="color:' + _prim() + ';'
                     'text-decoration:underline;font-weight:500;font-size:13px;'
                     'font-family:' + FONT_STACK + ';">' + text + '</a>')
        lbl.setTextFormat(Qt.RichText)
        lbl.setTextInteractionFlags(Qt.TextBrowserInteraction)
        lbl.setOpenExternalLinks(True)
        lbl.setCursor(Qt.PointingHandCursor)
        lbl.setStyleSheet("background:transparent;")
        return lbl

    # ------------------------- 卡片2: 关于你
    def _build_about_card(self):
        card, lay = _build_card(tr("about_card_title"), tr("about_card_subtitle"))

        # 分组 - 昵称
        _, g = _build_group(lay)
        g.addWidget(_label(tr("nickname_label"), weight="600", font_size="14px"))
        self.nickname_edit = _line_edit(str(self.cfg.get("nickname", "") or ""))
        self.nickname_edit.setPlaceholderText(tr("nickname_placeholder"))
        g.addWidget(self.nickname_edit)

        # 分组 - 自定义规则
        _, g = _build_group(lay)
        g.addWidget(_label(tr("custom_rules_label"), weight="600", font_size="14px"))
        self.custom_rules_edit = _text_edit(str(self.cfg.get("custom_rules", "") or ""), min_height=140)
        self.custom_rules_edit.setPlaceholderText(tr("custom_rules_placeholder"))
        g.addWidget(self.custom_rules_edit)
        hint = _label(
            tr("custom_rules_hint"),
            color=COLORS['TEXT_3'], font_size="12px"
        )
        hint.setWordWrap(True)
        g.addWidget(hint)

        return card

    # ------------------------- 卡片3: 供应商
    def _build_provider_card(self):
        card, lay = _build_card(tr("provider_card_title"), tr("provider_card_subtitle"))

        # 分组0 - 官方/非官方切换
        _, g0 = _build_group(lay)
        provider_init = self.cfg.get("provider_type", "official")
        if provider_init not in ("official", "custom"):
            provider_init = "official"
        self.provider_control = SegmentedControl(
            [("official", tr("provider_official")), ("custom", tr("provider_custom"))],
            value_key=provider_init,
        )
        self.provider_control.valueChanged.connect(self._on_provider_changed)
        g0.addWidget(self.provider_control)

        # 分组1 - 供应商选择器（下拉 + 增删按钮）
        _, g1 = _build_group(lay)
        selector_row = QHBoxLayout()
        selector_row.setSpacing(10)

        self.provider_combo = QComboBox()
        self.provider_combo.setFixedHeight(40)
        self.provider_combo.setStyleSheet(
            "QComboBox{background:" + _card() + ";color:" + COLORS['TEXT'] + ";"
            "border:1px solid " + _border_input() + ";border-radius:" + RADII["md"] + ";"
            "padding:0 12px;font-size:14px;font-family:" + FONT_STACK + ";}"
            "QComboBox::drop-down{border:none;width:24px;}"
            "QComboBox::down-arrow{image:none;border-left:5px solid transparent;"
            "border-right:5px solid transparent;border-top:5px solid " + COLORS['TEXT_3'] + ";}"
            "QComboBox QAbstractItemView{background:" + _card() + ";color:" + COLORS['TEXT'] + ";"
            "border:1px solid " + _border_input() + ";selection-background-color:" + _prim_light() + ";}"
        )
        self.provider_combo.currentIndexChanged.connect(self._on_provider_selection_changed)
        selector_row.addWidget(self.provider_combo, 1)

        self.add_provider_btn = _button(tr("add_provider_btn"), primary=False)
        self.add_provider_btn.setFixedHeight(40)
        self.add_provider_btn.clicked.connect(self._on_add_provider)
        selector_row.addWidget(self.add_provider_btn)

        self.remove_provider_btn = _button(tr("remove_provider_btn"), primary=False)
        self.remove_provider_btn.setFixedHeight(40)
        self.remove_provider_btn.clicked.connect(self._on_remove_provider)
        selector_row.addWidget(self.remove_provider_btn)
        g1.addLayout(selector_row)

        # 分组2 - API Key (通用，值跟随当前供应商)
        _, g2 = _build_group(lay)
        g2.addWidget(_label(tr("api_key"), weight="600", font_size="14px"))
        api_key_row = QHBoxLayout()
        api_key_row.setSpacing(8)
        self.api_key_edit = _line_edit("", echo=QLineEdit.Password)
        self.api_key_edit.setPlaceholderText(tr("api_key_placeholder"))
        api_key_row.addWidget(self.api_key_edit, 1)

        self.api_key_toggle_btn = _button(tr("show_password"), primary=False, width=60, height=36)
        self.api_key_toggle_btn.setCursor(Qt.PointingHandCursor)
        self.api_key_toggle_btn.clicked.connect(self._on_toggle_api_key_visibility)
        api_key_row.addWidget(self.api_key_toggle_btn)
        g2.addLayout(api_key_row)

        # 分组3 - 自定义供应商容器 (通过动画折叠/展开)
        self.custom_container = QFrame()
        self.custom_container.setStyleSheet(
            "QFrame{background:" + _card_alt() + ";border-radius:" + RADII["lg"] + ";}"
        )
        self.custom_lay = QVBoxLayout(self.custom_container)
        self.custom_lay.setContentsMargins(18, 16, 18, 16)
        self.custom_lay.setSpacing(12)

        # Base URL
        self.custom_lay.addWidget(_label(tr("base_url"), weight="600", font_size="14px"))
        self.base_url_edit = _line_edit("https://api.deepseek.com")
        self.base_url_edit.setPlaceholderText("https://api.deepseek.com")
        self.base_url_edit.setToolTip("https://api.deepseek.com")
        self.custom_lay.addWidget(self.base_url_edit)

        # 模型（每家供应商仅一个）
        self.custom_lay.addWidget(_label(tr("custom_model"), weight="600", font_size="14px"))
        self._model_row = ModelRow(name="", long_context=False, deletable=False)
        self.custom_lay.addWidget(self._model_row)

        # 底部提示
        bottom_hint = _label(
            tr("openai_compatible_hint"),
            color=COLORS['TEXT_4'], font_size="11px", weight="400"
        )
        bottom_hint.setAlignment(Qt.AlignRight)
        self.custom_lay.addWidget(bottom_hint)

        lay.addWidget(self.custom_container)

        # 连接实时同步信号
        self.api_key_edit.textChanged.connect(self._sync_provider_from_ui)
        self.base_url_edit.textChanged.connect(self._sync_provider_from_ui)
        if self._model_row is not None:
            self._model_row.name_edit.textChanged.connect(self._sync_provider_from_ui)
            self._model_row.long_ctx.stateChanged.connect(self._sync_provider_from_ui)

        # 刷新下拉框并加载当前供应商数据到 UI
        self._refresh_provider_combo()
        self._sync_ui_to_provider(self._active_provider_index)

        # 初始状态（官方/非官方）
        self._on_provider_changed(self.provider_control.value(), animate=False)

        return card

    # ---------------- 多供应商数据加载/同步
    def _load_providers_from_config(self):
        """从 self.cfg 构造多供应商内存模型，并保持向后兼容的扁平字段。"""
        providers = self.cfg.get("providers")
        if isinstance(providers, list) and providers:
            self._providers = []
            for p in providers[:2]:
                if not isinstance(p, dict):
                    continue
                self._providers.append({
                    "name": str(p.get("name", "") or ""),
                    "base_url": str(p.get("base_url", "") or ""),
                    "api_key": str(p.get("api_key", "") or ""),
                    "model": str(p.get("model", "") or ""),
                    "long_context": bool(p.get("long_context", False)),
                })
            active = self.cfg.get("active_provider_index", 0)
            if isinstance(active, int) and 0 <= active < len(self._providers):
                self._active_provider_index = active
            else:
                self._active_provider_index = 0

        if not self._providers:
            provider_type = self.cfg.get("provider_type", "official")
            if provider_type not in ("official", "custom"):
                provider_type = "official"
            api_key = str(self.cfg.get("api_key", "") or "")
            base_url = str(self.cfg.get("base_url") or "https://api.deepseek.com")
            model = str(self.cfg.get("model") or "deepseek-v4-pro")
            long_context = False
            models_cfg = self.cfg.get("models")
            if isinstance(models_cfg, list) and models_cfg:
                model = str(models_cfg[0].get("name", "") or model)
                long_context = bool(models_cfg[0].get("long_context", False))
            if provider_type == "official":
                self._providers = [{
                    "name": _OFFICIAL_PROVIDER_TAG,
                    "base_url": "https://api.deepseek.com",
                    "api_key": api_key,
                    "model": model,
                    "long_context": long_context,
                }]
            else:
                self._providers = [{
                    "name": tr("custom_provider_default", n=1),
                    "base_url": base_url,
                    "api_key": api_key,
                    "model": model,
                    "long_context": long_context,
                }]
            self._active_provider_index = 0

    def _refresh_provider_combo(self):
        """根据 self._providers 刷新下拉框，保持当前选中项。"""
        if self.provider_combo is None:
            return
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        for idx, p in enumerate(self._providers):
            raw_name = p.get("name", "")
            if raw_name == _OFFICIAL_PROVIDER_TAG:
                display_name = tr("provider_official")
            elif raw_name:
                display_name = raw_name
            else:
                display_name = tr("provider_default_name", n=idx + 1)
            self.provider_combo.addItem(display_name)
        self.provider_combo.setCurrentIndex(self._active_provider_index)
        self.provider_combo.blockSignals(False)

    def _sync_ui_to_provider(self, index):
        """将 index 对应的供应商数据加载到 UI 控件。"""
        if not (0 <= index < len(self._providers)):
            return
        self._active_provider_index = index
        p = self._providers[index]
        self._syncing_ui = True
        try:
            self.api_key_edit.setText(p.get("api_key", ""))
            self.base_url_edit.setText(p.get("base_url", ""))
            if self._model_row is not None:
                self._model_row.name_edit.setText(p.get("model", ""))
                self._model_row.long_ctx.setChecked(bool(p.get("long_context", False)))
            self._refresh_provider_combo()
        finally:
            self._syncing_ui = False

    def _sync_provider_from_ui(self):
        """将当前 UI 值写回当前激活供应商（实时更新，保存按钮之前即生效）。"""
        if self._syncing_ui:
            return
        if not (0 <= self._active_provider_index < len(self._providers)):
            return
        p = self._providers[self._active_provider_index]
        p["api_key"] = self.api_key_edit.text().strip()
        p["base_url"] = self.base_url_edit.text().strip()
        if self._model_row is not None:
            p["model"] = self._model_row.name_edit.text().strip()
            p["long_context"] = bool(self._model_row.long_ctx.isChecked())
        # 同步下拉框显示名（若用户未手动改 name 则保持不变）
        self._refresh_provider_combo()

    def _on_provider_selection_changed(self, index):
        """用户切换下拉框选项。"""
        if not (0 <= index < len(self._providers)) or index == self._active_provider_index:
            return
        # 切走前先把当前 UI 值写回旧供应商
        self._sync_provider_from_ui()
        self._sync_ui_to_provider(index)

    def _on_add_provider(self):
        """添加一家自定义供应商，最多 2 家。"""
        if len(self._providers) >= 2:
            QMessageBox.information(self, tr("tip"), tr("max_providers_msg"))
            return
        if self.provider_control.value() != "custom":
            return
        self._sync_provider_from_ui()
        new_name = tr("custom_provider_default", n=len(self._providers) + 1)
        self._providers.append({
            "name": new_name,
            "base_url": "https://api.deepseek.com",
            "api_key": "",
            "model": "deepseek-v4-pro",
            "long_context": False,
        })
        self._sync_ui_to_provider(len(self._providers) - 1)
        self._update_provider_buttons()

    def _on_remove_provider(self):
        """删除当前供应商，至少保留 1 家。"""
        if len(self._providers) <= 1:
            QMessageBox.information(self, tr("tip"), tr("min_providers_msg"))
            return
        if self.provider_control.value() != "custom":
            return
        idx = self._active_provider_index
        del self._providers[idx]
        new_idx = max(0, min(idx, len(self._providers) - 1))
        self._sync_ui_to_provider(new_idx)
        self._update_provider_buttons()

    def _update_provider_buttons(self):
        """根据官方/自定义状态与供应商数量更新按钮可用性。"""
        is_custom = self.provider_control.value() == "custom"
        self.add_provider_btn.setEnabled(is_custom and len(self._providers) < 2)
        self.remove_provider_btn.setEnabled(is_custom and len(self._providers) > 1)
        self.provider_combo.setEnabled(True)

    # ---------------- 供应商切换与模型管理
    def _on_provider_changed(self, value, animate=True):
        # 直接设置最终状态，避免快速切换时动画竞态导致可见性与预期不一致
        if value == "official":
            self.custom_container.setMaximumHeight(0)
            self.custom_container.setVisible(False)
        else:
            self.custom_container.setMaximumHeight(16777215)
            self.custom_container.setVisible(True)
        # 清理可能正在运行的旧动画引用
        if self._container_anim is not None:
            try:
                self._container_anim.stop()
            except Exception:
                pass
            self._container_anim = None
        # 切换到官方时确保只有一家官方供应商；切换到自定义时确保至少有一家自定义供应商
        self._sync_provider_from_ui()
        if value == "official":
            # 保留当前激活供应商的 key/model 作为官方配置
            active = self._providers[self._active_provider_index] if self._providers else {}
            self._providers = [{
                "name": _OFFICIAL_PROVIDER_TAG,
                "base_url": "https://api.deepseek.com",
                "api_key": active.get("api_key", ""),
                "model": active.get("model", "deepseek-v4-pro"),
                "long_context": active.get("long_context", False),
            }]
            self._active_provider_index = 0
        else:
            # 切回自定义时，由官方配置迁移为一家自定义供应商（保留官方模式继承的 key/model）
            if len(self._providers) == 1 and self._providers[0].get("name") == _OFFICIAL_PROVIDER_TAG:
                official = self._providers[0]
                self._providers = [{
                    "name": tr("custom_provider_default", n=1),
                    "base_url": official.get("base_url", "https://api.deepseek.com"),
                    "api_key": official.get("api_key", ""),
                    "model": official.get("model", "deepseek-v4-pro"),
                    "long_context": official.get("long_context", False),
                }]
            if not self._providers:
                self._providers.append({
                    "name": tr("custom_provider_default", n=1),
                    "base_url": "https://api.deepseek.com",
                    "api_key": "",
                    "model": "deepseek-v4-pro",
                    "long_context": False,
                })
        self._active_provider_index = max(0, min(self._active_provider_index, len(self._providers) - 1))
        self._sync_ui_to_provider(self._active_provider_index)
        self._update_provider_buttons()

    def _animate_container(self, container, expand=True):
        if self._container_anim is not None:
            try:
                self._container_anim.stop()
            except Exception:
                pass
            self._container_anim = None
        if expand:
            container.setVisible(True)
            target_h = container.sizeHint().height()
            # 计算自然高度：用当前布局
            container.setMaximumHeight(0)
            anim = QPropertyAnimation(container, b"maximumHeight", self)
            anim.setDuration(ANIM_DURATION)
            anim.setStartValue(0)
            anim.setEndValue(max(target_h, 380))
            anim.setEasingCurve(QEasingCurve.InOutCubic)
            def _finish_expand():
                container.setMaximumHeight(16777215)
                self._container_anim = None
            anim.finished.connect(_finish_expand)
            anim.start()
        else:
            current = container.height()
            anim = QPropertyAnimation(container, b"maximumHeight", self)
            anim.setDuration(ANIM_DURATION)
            anim.setStartValue(max(current, 100))
            anim.setEndValue(0)
            anim.setEasingCurve(QEasingCurve.InOutCubic)
            def _finish_collapse():
                container.setVisible(False)
                self._container_anim = None
            anim.finished.connect(_finish_collapse)
            anim.start()
        self._container_anim = anim

    # ---------------- 初始化主题
    def _on_theme_preview(self, theme: str):
        """主题切换即时预览：应用到全局并由 ThemeManager 信号统一刷新（不保存）。"""
        try:
            apply_theme(theme)
        except Exception:
            pass

    def _resolve_initial_theme(self):
        t = self.cfg.get("theme") or ""
        if t in ("light", "dark", "system"):
            return t
        if self.cfg.get("dark_mode"):
            return "dark"
        return "light"

    # ---------------- 模式选择
    def _get_selected_mode(self) -> str:
        for i, b in enumerate(self._mode_buttons):
            if b.isChecked():
                return self._mode_values[i]
        return "normal"

    def _set_mode(self, val: str):
        # 确保 val 合法，默认 normal
        # 兼容历史别名：plan_iterate / agent / deep → ultra
        if val in ("plan_iterate", "agent", "deep"):
            val = "ultra"
        if val not in ("normal", "plan", "ultra"):
            val = "normal"
        # 根据 val 找到对应按钮并 checked
        for i, v in enumerate(self._mode_values):
            if v == val and i < len(self._mode_buttons):
                self._mode_buttons[i].setChecked(True)
            else:
                if i < len(self._mode_buttons):
                    self._mode_buttons[i].setChecked(False)

    # ---------------- 思考深度选择（从QSlider读取）
    def _get_selected_reasoning(self) -> str:
        try:
            return "max" if self._reasoning_slider.value() >= 1 else "high"
        except Exception:
            return "high"

    # ---------------- 权限级别选择
    def _get_selected_permission(self) -> str:
        for i, b in enumerate(self._perm_buttons):
            if b.isChecked() and i < len(self._perm_values):
                return self._perm_values[i]
        return "full"

    # ---------------- 模型选择（flash/pro 按钮）
    def _get_selected_model(self) -> str:
        for i, b in enumerate(self._model_buttons):
            if b.isChecked() and i < len(self._model_values):
                return self._model_values[i]
        return "deepseek-v4-flash"

    # ---------------- 进入动画
    def showEvent(self, event):
        super().showEvent(event)
        if self._shown_once:
            return
        self._shown_once = True
        try:
            pop_in(self, duration_ms=200, start_scale=0.95, easing=QEasingCurve.OutBack)
            fade_in(self, duration_ms=180, easing=QEasingCurve.OutCubic)
        except Exception:
            pass

    def done(self, result):
        """弹窗关闭时移除全局事件过滤器并断开主题信号，避免生命周期泄漏。"""
        try:
            QApplication.instance().removeEventFilter(self)
        except Exception:
            pass
        try:
            ThemeManager.instance().theme_changed.disconnect(self._rebuild_styles)
        except Exception:
            pass
        super().done(result)

    def accept(self):
        if self._is_closing:
            return
        self._is_closing = True
        self._finish_called = False
        remaining = [2]

        def _finish():
            if self._finish_called:
                return
            remaining[0] -= 1
            if remaining[0] <= 0:
                self._finish_called = True
                try:
                    super(SettingsDialog, self).accept()
                except Exception:
                    pass

        try:
            pop_out(self, duration_ms=180, on_finished=_finish)
            fade_out(self, duration_ms=150, on_finished=_finish)
        except Exception:
            _finish()

    def reject(self):
        if self._is_closing:
            return
        # 取消时回滚到进入弹窗前的主题
        try:
            apply_theme(self._initial_theme)
        except Exception:
            pass
        self._is_closing = True
        self._finish_called = False
        remaining = [2]

        def _finish():
            if self._finish_called:
                return
            remaining[0] -= 1
            if remaining[0] <= 0:
                self._finish_called = True
                try:
                    super(SettingsDialog, self).reject()
                except Exception:
                    pass

        try:
            pop_out(self, duration_ms=180, on_finished=_finish)
            fade_out(self, duration_ms=150, on_finished=_finish)
        except Exception:
            _finish()

    # ---------------- 语言选择
    def _populate_language_combo(self):
        """填充语言下拉框（内置 + 自定义包）。"""
        from src.gui.style import I18N
        self.language_combo.blockSignals(True)
        self.language_combo.clear()
        self._lang_codes = []
        current = self._selected_language or "zh"
        selected_idx = 0
        for i, (code, name, is_builtin) in enumerate(I18N.available_languages()):
            label = name if is_builtin else f"{name} ({code})"
            self.language_combo.addItem(label, code)
            self._lang_codes.append(code)
            if code == current or (current and current.startswith(code)):
                selected_idx = i
        self.language_combo.setCurrentIndex(selected_idx)
        self.language_combo.blockSignals(False)

    def _on_language_selected(self, idx):
        if 0 <= idx < len(self._lang_codes):
            self._selected_language = self._lang_codes[idx]

    def refresh_languages(self):
        """外部安装新语言包后刷新下拉选项。"""
        prev = self._selected_language
        self._populate_language_combo()
        # 尝试恢复之前的选择
        from src.gui.style import I18N
        for i, code in enumerate(self._lang_codes):
            if code == prev:
                self.language_combo.setCurrentIndex(i)
                self._selected_language = code
                break

    # ---------------- 对外：读取用户配置
    def get_config(self):
        # 确保当前 UI 值已同步到内存模型
        self._sync_provider_from_ui()

        provider_type = self.provider_control.value()
        # 官方API模式下使用flash/pro按钮选择模型；自定义模式使用model_row文本框
        if provider_type == "official":
            model_name = self._get_selected_model()
            long_context = False
        else:
            # 防止索引越界（理论上不会，但防御性处理避免真实运行时异常）
            if self._providers:
                self._active_provider_index = max(0, min(self._active_provider_index, len(self._providers) - 1))
                active = self._providers[self._active_provider_index]
            else:
                self._active_provider_index = 0
                active = {}
            model_name = active.get("model", "") or "deepseek-v4-pro"
            long_context = bool(active.get("long_context", False))

        # 向后兼容：保留单供应商时期的扁平字段
        models = []
        if model_name:
            models.append({"name": model_name, "long_context": long_context})

        theme_mode = self.theme_control.value()
        # 获取当前API key（官方/自定义都从providers内存模型读取）
        if self._providers:
            self._active_provider_index = max(0, min(self._active_provider_index, len(self._providers) - 1))
            cur_p = self._providers[self._active_provider_index]
        else:
            cur_p = {}
        data = {
            "theme_mode": theme_mode,
            "theme": theme_mode,
            "chat_mode": self._get_selected_mode(),
            "reasoning_effort": self._get_selected_reasoning(),
            "permission_level": self._get_selected_permission(),
            "permission_mode": self._get_selected_permission(),
            "nickname": self.nickname_edit.text().strip(),
            "custom_rules": self.custom_rules_edit.toPlainText().strip(),
            "provider_type": provider_type,
            "api_key": cur_p.get("api_key", ""),
            "base_url": cur_p.get("base_url", "") if provider_type == "custom" else "https://api.deepseek.com",
            "models": models,
            # 新增多供应商结构
            "providers": list(self._providers),
            "active_provider_index": self._active_provider_index,
            "language": self._selected_language,
            "dark_mode": (theme_mode == "dark"),
            # 主模型取当前激活供应商的模型名
            "model": model_name,
        }
        return data

    # ---------------- 保存
    def _on_save(self):
        data = self.get_config()
        self.save_clicked.emit(data)
        self.accept()

