# -*- coding: utf-8 -*-
"""输入区组件：双层胶囊形输入框（seed2.1设计）。

布局（从上到下）:
  1. token 实时统计行（流式输出时显示，右对齐小字）
  2. 附件预览药丸行（有附件时显示）
  3. 外层浅蓝色胶囊：
     - 顶部：居中蓝色模型信息文字（可点击）
     - 内层白色圆角输入框：QTextEdit + 索引徽章 + 发送/停止按钮
  4. 免责声明
"""
from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt, Signal, QEvent, QSize
from PySide6.QtGui import QFontMetrics, QPalette, QColor
from PySide6.QtWidgets import (
    QFrame, QLabel, QVBoxLayout, QHBoxLayout, QWidget,
    QPushButton, QTextEdit, QSizePolicy,
)

from src.gui.style import (
    COLORS, FONT_STACK, FONT_WEIGHTS, RADII, SIZES, tr, apply_shadow,
    build_stylesheet, ThemeManager,
)
from src.gui._gui_helpers import _load_icon, enable_touch_scroll

from src.gui.panels.input_area._attachment_manager import AttachmentManagerMixin
from src.gui.panels.input_area._model_tag import ModelTagMixin
from src.gui.panels.input_area._text_handler import TextHandlerMixin
from src.gui.panels.input_area._token_stats import TokenStatsMixin
from src.gui.panels.input_area._toolbar import ToolbarMixin


class InputArea(QFrame, AttachmentManagerMixin, ModelTagMixin, TextHandlerMixin,
                TokenStatsMixin, ToolbarMixin):
    """输入区主类 — 双层胶囊形输入框（seed2.1）。"""

    # 信号
    sig_send = Signal(str)
    sig_stop = Signal()
    sig_clear = Signal()
    sig_upload = Signal()
    sig_terminal_clicked = Signal()
    user_activity = Signal()
    sig_model_label_clicked = Signal()
    send_clicked = Signal(str)
    stop_clicked = Signal()

    # ---- Qt 事件转发到 Mixin ----
    def eventFilter(self, obj, event):
        if self._filter_input_event(obj, event):
            return True
        return super().eventFilter(obj, event)

    def dragEnterEvent(self, event):
        if not self._handle_drag_enter(event):
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        if not self._handle_drop(event):
            super().dropEvent(event)

    def __init__(self, parent=None):
        super().__init__(parent)
        # ---- 状态变量 ----
        self._uploaded_files: List[dict] = []
        self._uploaded_images: List[str] = []
        self._project_dir = None
        self._focus_glow = False
        self._running = False
        self._current_model = "deepseek-v4-pro"
        self._current_permission = "full"
        self._current_mode = "normal"
        self._current_reasoning = "high"
        self._history: List[str] = []
        self._history_index = -1
        self._draft_text = ""
        self._max_history = 50
        self._slash_popup = None

        # 兼容 mixin 的按钮属性（已移除UI，保留None防止 AttributeError）
        self.clear_btn = None
        self.git_btn = None
        self.terminal_btn = None
        self.upload_btn = None
        self.attach_btn = None

        # ---- 外层容器 ----
        self.setObjectName("InputArea")
        self.setStyleSheet("QFrame#InputArea{background:transparent;border:none;}")
        self.setMinimumHeight(SIZES["input"]["min_height"])
        self.setMaximumHeight(SIZES["input"]["max_height"])
        self.setAcceptDrops(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        # 1) token 统计行（Mixin创建）
        self._init_token_stats(outer)

        # 2) 附件预览行（由 AttachmentManagerMixin 初始化）
        self._init_attachment_widgets(outer)

        # 3) 极简单层输入框
        self._build_minimal_input(outer)

        # 4) 免责声明
        self._build_disclaimer(outer)

        # 斜杠按钮（隐藏）
        self.slash_btn = QPushButton("/")
        self.slash_btn.setFixedSize(SIZES["input"]["attach_btn"], SIZES["input"]["attach_btn"])
        self.slash_btn.setVisible(False)
        self.slash_btn.setToolTip(tr("slash_tooltip"))
        self.slash_btn.setStyleSheet(self._slash_btn_style())
        self.slash_btn.clicked.connect(self._on_slash_btn_clicked)

        # 集中应用全部样式（替代 __init__ 中分散的 setStyleSheet）
        self._build_styles()

        # 订阅集中式主题变更信号
        try:
            ThemeManager.instance().theme_changed.connect(self._rebuild_styles)
        except Exception:
            pass



    def _build_minimal_input(self, outer):
        """构建极简单层输入框。模式/模型信息在状态栏统一显示。"""
        # ---- 单层输入容器 ----
        self._input_card = QFrame()
        self._input_card.setObjectName("InputCard")
        self._input_card.setMinimumHeight(SIZES["input"]["card_min_height"])
        self._input_card.setCursor(Qt.IBeamCursor)
        self._input_card.mousePressEvent = self._on_outer_card_click
        apply_shadow(self._input_card, 'sm')

        card_lay = QHBoxLayout(self._input_card)
        card_lay.setContentsMargins(SIZES["spacing"]["md"], SIZES["spacing"]["sm"],
                                    SIZES["spacing"]["sm"], SIZES["spacing"]["sm"])
        card_lay.setSpacing(SIZES["spacing"]["sm"])
        card_lay.setAlignment(Qt.AlignVCenter)

        # 左侧附件按钮
        self._build_attach_button(card_lay)

        # 文本输入框
        self._build_text_input(card_lay)

        # 发送/停止按钮
        self._build_send_buttons(card_lay)

        outer.addWidget(self._input_card)

        # 兼容旧属性
        self._inner_card = self._input_card
        self.model_label = None
        self._model_info_bar = None

    def _build_styles(self):
        """集中构建并应用 InputArea 全部 stylesheet。

        主题切换或初始化时调用一次，避免 _rebuild_styles 与 __init__ 重复拼接字符串。
        """
        focused = getattr(self, "_focus_glow", False)
        self._input_card.setStyleSheet(self._input_style(focused))
        if hasattr(self, '_attach_btn'):
            self._attach_btn.setStyleSheet(self._attach_btn_style())
        self.input.setStyleSheet(self._text_input_style())
        self.send_btn.setStyleSheet(self._send_btn_style())
        self.stop_btn.setStyleSheet(self._stop_btn_style())
        self._disclaimer.setStyleSheet(self._disclaimer_style())
        self.slash_btn.setStyleSheet(self._slash_btn_style())
        # 刷新免责声明中的链接颜色
        self._disclaimer.setText(self._disclaimer_text())

    def _attach_btn_style(self) -> str:
        return (
            "QPushButton{background:transparent;border:none;border-radius:" + RADII['circle'] + ";}"
            "QPushButton:hover{background:" + COLORS['SURFACE'] + ";}"
        )

    def _send_btn_style(self) -> str:
        return (
            "QPushButton{border:none;border-radius:" + RADII['circle'] + ";}"
            "QPushButton:disabled{background:" + COLORS['SURFACE'] + ";}"
            "QPushButton:enabled{background:" + COLORS['ACCENT'] + ";}"
            "QPushButton:enabled:hover{background:" + COLORS['ACCENT_HOVER'] + ";}"
            "QPushButton:enabled:pressed{background:" + COLORS['ACCENT_PRESSED'] + ";}"
        )

    def _stop_btn_style(self) -> str:
        return (
            "QPushButton{background:" + COLORS['ACCENT'] + ";border:none;border-radius:" + RADII['circle'] + ";}"
            "QPushButton:hover{background:" + COLORS['ACCENT_HOVER'] + ";}"
            "QPushButton:pressed{background:" + COLORS['ACCENT_PRESSED'] + ";}"
        )

    def _text_input_style(self) -> str:
        return (
            "QTextEdit{background:transparent;border:none;"
            "color:" + COLORS['TEXT'] + ";font-size:14px;font-weight:" + str(FONT_WEIGHTS['regular']) + ";"
            "font-family:" + FONT_STACK + ";padding:0px;margin:0px;"
            "selection-background-color:" + COLORS['ACCENT'] + ";}"
            "QScrollBar:vertical{background:transparent;width:4px;border:none;}"
            "QScrollBar::handle:vertical{background:" + COLORS['BORDER'] + ";border-radius:2px;min-height:20px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0px;}"
        )

    def _disclaimer_style(self) -> str:
        return (
            "QLabel{color:" + COLORS['TEXT_4'] + ";font-size:10px;font-weight:400;"
            "font-family:" + FONT_STACK + ";background:transparent;}"
        )

    def _disclaimer_text(self) -> str:
        from src.gui.style import tr
        return (
            '<a href="https://dscodehelp.pages.dev" '
            'style="color:' + COLORS['TEXT_4'] + ';font-size:10px;font-weight:400;'
            'font-family:' + FONT_STACK + ';text-decoration:none;'
            'letter-spacing:0.2px;">'
            + tr("disclaimer_text") +
            '</a>'
        )

    def _slash_btn_style(self) -> str:
        return (
            "QPushButton{background:transparent;color:" + COLORS['TEXT_3'] + ";"
            "border:none;border-radius:" + RADII['sm'] + ";font-size:14px;font-weight:700;"
            "font-family:" + FONT_STACK + ";}"
        )



    def _input_style(self, focused: bool) -> str:
        """单层输入框样式：胶囊形大圆角。"""
        if focused:
            return (
                "QFrame#InputCard{background:" + COLORS['CARD'] + ";"
                "border:1.5px solid " + COLORS['ACCENT'] + ";"
                "border-radius:" + RADII['2xl'] + ";}"
            )
        return (
            "QFrame#InputCard{background:" + COLORS['CARD'] + ";"
            "border:1px solid " + COLORS['BORDER'] + ";"
            "border-radius:" + RADII['2xl'] + ";}"
        )

    def _on_outer_card_click(self, event):
        """点击输入框区域时聚焦文本输入。"""
        self.input.setFocus()

    def _build_attach_button(self, layout):
        """构建左侧附件按钮（"+"图标，点击触发文件上传）。"""
        btn_sz = SIZES["input"]["attach_btn"]
        self._attach_btn = QPushButton()
        self._attach_btn.setFixedSize(btn_sz, btn_sz)
        self._attach_btn.setCursor(Qt.PointingHandCursor)
        # 优先使用回形针图标，不存在时降级为加号
        _attach_ic = _load_icon("attach_24.png", 18)
        if _attach_ic.isNull():
            _attach_ic = _load_icon("plus_24.png", 18)
        if not _attach_ic.isNull():
            self._attach_btn.setIcon(_attach_ic)
            self._attach_btn.setIconSize(QSize(18, 18))
        else:
            self._attach_btn.setText("+")
        self._attach_btn.setStyleSheet(self._attach_btn_style())
        self._attach_btn.clicked.connect(self._on_attach_clicked)
        layout.addWidget(self._attach_btn, 0, Qt.AlignVCenter)

    def _on_attach_clicked(self):
        """附件按钮点击 → 触发文件选择对话框并交给统一文件处理逻辑。"""
        try:
            from PySide6.QtWidgets import QFileDialog
            from src.core.config import Config
            cfg = Config()
            last_dir = cfg.get("last_upload_dir", "")
            files, _ = QFileDialog.getOpenFileNames(
                self, tr("select_file_title"), last_dir or "", tr("all_files_filter")
            )
            if files:
                for f in files:
                    self._handle_local_file(f)
                cfg.set("last_upload_dir", files[0])
                cfg.save()
                try:
                    self.user_activity.emit()
                except Exception:
                    pass
        except Exception:
            pass

    # ==================================================================
    # 控件构建
    # ==================================================================
    def _build_text_input(self, parent_lay):
        """创建 QTextEdit 文本框。"""
        self.input = QTextEdit()
        self.input.setPlaceholderText(tr("mention_hint"))
        self.input.setAcceptRichText(False)
        self.input.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.input.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.input.setFrameShape(QFrame.NoFrame)
        self.input.setStyleSheet(self._text_input_style())
        # 触控支持
        enable_touch_scroll(self.input)
        try:
            pal = self.input.palette()
            pal.setColor(QPalette.PlaceholderText, QColor(COLORS['TEXT_4']))
            self.input.setPalette(pal)
        except Exception:
            pass
        # 高度设置
        fm = QFontMetrics(self.input.font())
        self._line_h = fm.lineSpacing() + 6
        self.input.setFixedHeight(self._line_h)
        self.input.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.input.document().documentLayout().documentSizeChanged.connect(self._on_doc_resize)
        self.input.installEventFilter(self)
        self.input.textChanged.connect(self._on_text_changed)
        parent_lay.addWidget(self.input, 1)

    def _build_send_buttons(self, parent_lay):
        """发送/停止按钮。"""
        btn_sz = SIZES["input"]["send_btn"]
        self.send_btn = QPushButton()
        self.send_btn.setFixedSize(btn_sz, btn_sz)
        self.send_btn.setCursor(Qt.PointingHandCursor)
        ic = _load_icon("arrow_up_white_24.png", 18)
        if not ic.isNull():
            self.send_btn.setIcon(ic)
            self.send_btn.setIconSize(QSize(18, 18))
        else:
            self.send_btn.setText("\u2191")  # ↑ 箭头作为降级
        self.send_btn.setToolTip(tr("send_tooltip"))
        self.send_btn.setEnabled(False)
        self.send_btn.setStyleSheet(self._send_btn_style())
        self.send_btn.clicked.connect(self._on_send)
        parent_lay.addWidget(self.send_btn)

        self.stop_btn = QPushButton()
        self.stop_btn.setFixedSize(btn_sz, btn_sz)
        self.stop_btn.setCursor(Qt.PointingHandCursor)
        ic_s = _load_icon("stop_white_24.png", 14)
        if not ic_s.isNull():
            self.stop_btn.setIcon(ic_s)
            self.stop_btn.setIconSize(QSize(14, 14))
        else:
            self.stop_btn.setText("\u25a0")  # ■ 方块作为降级
        self.stop_btn.setToolTip(tr("stop"))
        self.stop_btn.setStyleSheet(self._stop_btn_style())
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.setVisible(False)
        parent_lay.addWidget(self.stop_btn)

    def _build_disclaimer(self, outer):
        """免责声明文字（弱化处理：更小字号、更浅颜色）。"""
        self._disclaimer = QLabel(self._disclaimer_text())
        self._disclaimer.setOpenExternalLinks(True)
        self._disclaimer.setCursor(Qt.PointingHandCursor)
        self._disclaimer.setAlignment(Qt.AlignCenter)
        outer.addWidget(self._disclaimer)

    # ==================================================================
    # 焦点处理（由 eventFilter 在 FocusIn/FocusOut 时调用）
    # ==================================================================
    def _on_focus_in(self):
        """输入框获得焦点 → 输入框边框高亮。"""
        self._focus_glow = True
        self._input_card.setStyleSheet(self._input_style(True))

    def _on_focus_out(self):
        """输入框失去焦点 → 输入框恢复。"""
        self._focus_glow = False
        self._input_card.setStyleSheet(self._input_style(False))

    # ==================================================================
    # 自适应高度
    # ==================================================================
    def _on_doc_resize(self, size):
        """文本行数变化时调整输入框高度。"""
        try:
            doc_h = int(size.height())
            text_h = max(self._line_h, min(doc_h, self._line_h * 5))
            self.input.setFixedHeight(text_h)
        except Exception:
            pass

    # ==================================================================
    # 模型信息文字
    # ==================================================================
    def _on_model_info_clicked(self, event):
        """兼容：模型信息栏已移除，此方法仅转发信号。"""
        self.sig_model_label_clicked.emit()

    def _model_short_name(self, model: str) -> str:
        """简化模型名：去掉前缀，保留 pro/flash 等，转为大写。"""
        short = model
        for prefix in ["deepseek-v4-", "deepseek-", "DeepSeek-V4-", "DeepSeek-", "v4-"]:
            if short.startswith(prefix):
                short = short[len(prefix):]
                break
        return short.upper()

    def _reasoning_short_name(self, reasoning: str) -> str:
        """思考深度短名：high→空(标准不显示), max→Max。"""
        r = str(reasoning).lower()
        key_map = {"high": "reasoning_high_short", "max": "reasoning_max_short"}
        if r in key_map:
            try:
                txt = tr(key_map[r])
                return txt
            except Exception:
                pass
        return {"high": "", "max": "Max"}.get(r, "")

    def _permission_display_name(self, perm: str) -> str:
        """权限显示名。支持传入key(full/coder/reviewer)或已翻译的显示名。"""
        perm_lower = str(perm).lower()
        key_map = {"full": "perm_full", "coder": "perm_coder", "reviewer": "perm_reviewer"}
        if perm_lower in key_map:
            try:
                return tr(key_map[perm_lower])
            except Exception:
                pass
        # 已经是显示名或未知值，直接返回
        return str(perm)

    def _update_model_info_text(self):
        """模型信息栏已移除，此方法为兼容空操作。"""
        pass

    def set_current_model(self, model):
        self._current_model = str(model) if model else self._current_model

    def set_current_permission(self, perm):
        self._current_permission = str(perm) if perm else self._current_permission

    def set_current_mode(self, mode):
        self._current_mode = str(mode) if mode else self._current_mode

    def set_current_reasoning(self, reasoning):
        self._current_reasoning = str(reasoning) if reasoning else self._current_reasoning
    # ==================================================================
    # 索引状态
    # ==================================================================
    def set_index_status(self, status: str):
        """索引状态设置（索引徽章已从输入框移除，此方法为兼容保留）。"""
        try:
            status = str(status or "unavailable").lower()
            self._index_status = status
        except Exception:
            pass

    # ==================================================================
    # 公共 API
    # ==================================================================
    def _rebuild_styles(self, theme: str = None):
        """主题切换后重建所有样式表（从当前 COLORS 重新生成）。"""
        try:
            self._build_styles()
        except Exception:
            pass

    def clear(self):
        try:
            self.input.clear()
        except Exception:
            pass
        self.clear_all_uploads()

    def retranslate_ui(self, language: str):
        try:
            self.input.setPlaceholderText(tr("mention_hint"))
        except Exception:
            pass

    def get_images(self):
        return list(self._uploaded_images)

    def get_files(self):
        return list(self._uploaded_files)


    @staticmethod
    def _mode_display_name(mode: str) -> str:
        return {
            "normal": tr("mode_normal_short") if tr("mode_normal_short") != "mode_normal_short" else "普通",
            "plan": tr("mode_plan_short") if tr("mode_plan_short") != "mode_plan_short" else "规划",
            "ultra": "Ultra",
        }.get(str(mode).lower(), str(mode))

    # 发送后清空附件显示
    def _on_send(self):
        text = self.input.toPlainText().strip()
        has_attachments = bool(self._uploaded_images or self._uploaded_files)
        if not text and not has_attachments:
            return
        self.input.clear()
        self._history_append(text)
        self._draft_text = ""
        self._history_index = -1
        try:
            self.input.setFocus()
        except Exception:
            pass
        try:
            self.send_clicked.emit(text)
        except Exception:
            pass
        try:
            self.sig_send.emit(text)
        except Exception:
            pass
        self.clear_all_uploads()

    def set_running(self, running: bool):
        self._running = bool(running)
        self.send_btn.setVisible(not self._running)
        self.stop_btn.setVisible(self._running)
        if not self._running:
            self._update_send_button_state()



__all__ = ["InputArea"]
