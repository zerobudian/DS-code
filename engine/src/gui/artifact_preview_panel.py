# -*- coding: utf-8 -*-
"""Artifact 预览面板 —— 类似 Claude Artifacts。

对外 API：
  - set_artifact(artifact)      # 设置/切换到当前 artifact
  - add_artifact(artifact)      # 添加 artifact 到列表
  - refresh()                   # 重新渲染当前 artifact
  - clear()                     # 清空列表与预览
  - update_artifacts(artifacts) # 批量替换 artifact 列表（兼容旧版集成）
  - has_artifacts() -> bool     # 是否存在 artifact
  - current_document() -> str   # 合并后的 HTML 文档

artifact 字典格式：
  {
    "id": str,           # 唯一标识
    "title": str,        # 列表显示标题
    "type": str,         # html / css / js / svg / mermaid / react / vue / python 等
    "content": str,      # 源码/内容
    "language": str,     # 可选，代码区语法标注
  }

安全说明：
  - 优先使用 QWebEngineView；加载 HTML 前会关闭 local-file 访问权限，
    并通过 CSP 与 sandbox 属性进一步隔离。
  - QWebEngineView 不可用时，平滑降级到 QTextBrowser（仅展示源码，不执行 JS）。
"""

from __future__ import annotations

import html
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QClipboard
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QTextEdit, QStackedWidget,
    QSizePolicy, QTextBrowser, QMessageBox, QApplication,
)

from src.gui.style import COLORS, RADII, FONT_STACK


# ---------------------------------------------------------------------------
# 小工具：类型图标、HTML 包装
# ---------------------------------------------------------------------------

_TYPE_ICONS: Dict[str, str] = {
    "html": "🌐",
    "svg": "🎨",
    "mermaid": "🧜",
    "react": "⚛",
    "vue": "🟩",
    "python": "🐍",
    "javascript": "📜",
    "js": "📜",
    "typescript": "📘",
    "css": "🎨",
    "json": "📋",
    "markdown": "📝",
    "text": "📄",
}


def _type_icon(artifact_type: str) -> str:
    """根据类型返回 emoji 图标。"""
    return _TYPE_ICONS.get((artifact_type or "").lower().strip(), "📦")


def _normalize_artifact(value: Any) -> Optional[Dict[str, Any]]:
    """把任意 artifact 描述归一化为标准字典。"""
    if value is None:
        return None
    if isinstance(value, dict):
        art = dict(value)
    else:
        art = {"content": str(value)}
    if not art.get("id"):
        art["id"] = str(uuid.uuid4())[:8]
    art.setdefault("title", art.get("id", "未命名"))
    art.setdefault("type", "text")
    art.setdefault("content", "")
    art.setdefault("language", art.get("type", "text"))
    return art


def _is_renderable_html(artifact: Optional[Dict[str, Any]]) -> bool:
    """判断当前 artifact 是否可作为 HTML 在浏览器中渲染。"""
    if artifact is None:
        return False
    ctype = str(artifact.get("type", "")).lower().strip()
    return ctype in ("html", "svg", "react", "vue", "javascript", "js", "typescript")


def _build_preview_html(artifact: Dict[str, Any]) -> str:
    """为 QWebEngineView 构建带隔离的 HTML 文档。

    通过以下方式限制权限：
      1. CSP 禁止访问外部资源。
      2. iframe sandbox 运行用户 HTML。
      3. 不暴露本地文件路径；所有资源通过 data URL 内联。
    """
    content = str(artifact.get("content", ""))
    ctype = str(artifact.get("type", "")).lower().strip()

    # SVG 直接作为 body 内容（转义 </script> 防注入）
    if ctype == "svg":
        body = content.replace("</script>", "<\\/script>")
    # React/Vue/JS 先用简单方式包装为可直接执行的 HTML
    elif ctype in ("react", "vue", "javascript", "js", "typescript"):
        # 转义 </script> 防止突破 script 标签执行 XSS
        safe_content = content.replace("</script>", "<\\/script>")
        body = (
            "<div id=\"root\"></div>\n"
            "<script type=\"module\">\n"
            "try {\n"
            + safe_content + "\n"
            "} catch (e) {\n"
            "  document.getElementById('root').innerHTML = "
            "'<pre style=\"color:red\">' + e.toString() + '</pre>';\n"
            "}\n"
            "</script>"
        )
    else:
        body = content.replace("</script>", "<\\/script>")

    # 将用户内容放入沙箱 iframe，进一步与 Qt 上下文隔离
    escaped_body = html.escape(body)
    # 移除 allow-same-origin 防止 iframe 脚本访问父页面 DOM
    sandbox_attrs = "sandbox=\"allow-scripts\""

    doc = (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta http-equiv=\"Content-Security-Policy\" "
        "content=\"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' blob:; "
        "style-src 'unsafe-inline'; img-src data: blob:; connect-src 'none'; "
        "frame-src 'self'; object-src 'none'; base-uri 'none';\">\n"
        "<style>\n"
        "body,html{margin:0;padding:0;width:100%;height:100%;overflow:hidden;"
        "background:" + COLORS['BGD'] + ";font-family:" + FONT_STACK + ";}\n"
        "iframe{border:none;width:100%;height:100%;display:block;}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        "<iframe " + sandbox_attrs + " srcdoc=\"" + escaped_body + "\"></iframe>\n"
        "</body>\n"
        "</html>"
    )
    return doc


def _build_fallback_text(artifact: Optional[Dict[str, Any]]) -> str:
    """为 QTextBrowser 构建只读源码展示文本。"""
    if artifact is None:
        return ""
    return str(artifact.get("content", ""))


def _inject_before_close(html: str, injection: str) -> str:
    """在 ``</body>`` 或 ``</html>`` 之前插入内容。"""
    if not injection:
        return html
    lowered = html.lower()
    for marker in ("</body>", "</html>"):
        idx = lowered.rfind(marker)
        if idx != -1:
            return f"{html[:idx]}\n{injection}\n{html[idx:]}"
    return f"{html}\n{injection}"


def _inject_into_head(html: str, injection: str) -> str:
    """在 ``</head>`` 之前（或 ``<head>`` 之后）插入内容。"""
    if not injection:
        return html
    lowered = html.lower()
    idx = lowered.find("</head>")
    if idx != -1:
        return f"{html[:idx]}\n{injection}\n{html[idx:]}"
    idx = lowered.find("<head>")
    if idx != -1:
        head_end = html.find(">", idx) + 1
        return f"{html[:head_end]}\n{injection}\n{html[head_end:]}"
    return f"{injection}\n{html}"


def _build_preview_document(artifacts: List[Dict[str, Any]]) -> str:
    """把 artifact 列表合并成完整 HTML 文档（兼容 ArtifactManager 合并逻辑）。"""
    html_parts: List[str] = []
    css_parts: List[str] = []
    js_parts: List[str] = []

    for art in artifacts:
        t = str(art.get("type", "")).lower().strip()
        c = str(art.get("content", ""))
        if t == "html":
            html_parts.append(c)
        elif t == "css":
            css_parts.append(c)
        elif t in ("js", "javascript"):
            js_parts.append(c)

    css_block = (
        "\n".join(f"<style>\n{c}\n</style>" for c in css_parts)
        if css_parts
        else ""
    )
    js_block = (
        "\n".join(f"<script>\n{c}\n</script>" for c in js_parts)
        if js_parts
        else ""
    )

    complete_docs = [c for c in html_parts if re.search(r"<html\b", c, re.IGNORECASE)]
    fragments = [c for c in html_parts if not re.search(r"<html\b", c, re.IGNORECASE)]

    if complete_docs:
        base = complete_docs[0]
        # 其余完整 HTML 文档作为片段注入 body，避免内容丢失
        fragments = complete_docs[1:] + fragments
        body_injection = "\n".join(filter(None, ["\n".join(fragments), js_block]))
        if css_block:
            base = _inject_into_head(base, css_block)
        return _inject_before_close(base, body_injection)

    body_content = "\n".join(filter(None, ["\n".join(fragments), js_block]))
    return (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"{css_block}\n"
        "</head>\n"
        "<body>\n"
        f"{body_content}\n"
        "</body>\n"
        "</html>"
    )


# ---------------------------------------------------------------------------
# 列表项组件
# ---------------------------------------------------------------------------

class _ArtifactListItem(QFrame):
    """Artifact 列表项：图标 + 标题 + 类型标签。"""

    def __init__(self, artifact: Dict[str, Any], parent=None):
        super().__init__(parent)
        self._artifact_id = str(artifact.get("id", ""))
        self.setStyleSheet(
            "QFrame{background:transparent;border:none;border-radius:" + RADII['md'] + ";}"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        icon_lbl = QLabel(_type_icon(artifact.get("type", "text")))
        icon_lbl.setStyleSheet(
            "QLabel{font-size:16px;background:transparent;}"
        )
        lay.addWidget(icon_lbl, 0)

        title = str(artifact.get("title", "未命名"))
        # M-006: artifact 标题可能来自用户/AI 输出，渲染为 QLabel 文本前做 HTML 转义
        self._title_lbl = QLabel(html.escape(title))
        self._title_lbl.setTextFormat(Qt.PlainText)
        self._title_lbl.setStyleSheet(
            "QLabel{color:" + COLORS['TEXT'] + ";font-size:13px;font-weight:600;"
            "font-family:" + FONT_STACK + ";background:transparent;}"
        )
        self._title_lbl.setWordWrap(False)
        lay.addWidget(self._title_lbl, 1)

        type_name = str(artifact.get("type", "text")).upper()
        type_lbl = QLabel(type_name)
        type_lbl.setStyleSheet(
            "QLabel{color:" + COLORS['TEXT_4'] + ";font-size:10px;"
            "font-family:" + FONT_STACK + ";background:transparent;}"
        )
        lay.addWidget(type_lbl, 0)

    @property
    def artifact_id(self) -> str:
        return self._artifact_id

    def set_title(self, title: str) -> None:
        # M-006: 标题写入 QLabel 前保持 HTML 转义
        self._title_lbl.setText(html.escape(str(title)))


# ---------------------------------------------------------------------------
# Tab 切换条（代码 / 预览）
# ---------------------------------------------------------------------------

class _PreviewTabBar(QFrame):
    """简洁的「代码 / 预览」切换条。"""

    tab_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ArtifactPreviewTabBar")
        self.setStyleSheet("QFrame#ArtifactPreviewTabBar{background:transparent;}")
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        self._buttons: List[QPushButton] = []
        for i, label in enumerate(("代码", "预览")):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(30)
            btn.setStyleSheet(
                "QPushButton{background:transparent;color:" + COLORS['TEXT_3'] + ";"
                "border:none;border-radius:" + RADII['md'] + ";"
                "padding:0 14px;font-size:12px;font-weight:600;"
                "font-family:" + FONT_STACK + ";}"
                "QPushButton:hover{background:" + COLORS['PRIM_LIGHT'] + ";"
                "color:" + COLORS['TEXT'] + ";}"
                "QPushButton:checked{background:" + COLORS['PRIM'] + ";"
                "color:" + COLORS['TEXT'] + ";"
                "border:1px solid " + COLORS['PRIM_DARK'] + ";}"
            )
            btn.clicked.connect(lambda _checked, idx=i: self._on_clicked(idx))
            root.addWidget(btn)
            self._buttons.append(btn)

        root.addStretch(1)
        if self._buttons:
            self._buttons[0].setChecked(True)

    def _on_clicked(self, idx: int) -> None:
        for i, btn in enumerate(self._buttons):
            btn.blockSignals(True)
            btn.setChecked(i == idx)
            btn.blockSignals(False)
        self.tab_changed.emit(idx)

    def set_tab(self, idx: int) -> None:
        if 0 <= idx < len(self._buttons):
            self._on_clicked(idx)

    def current_index(self) -> int:
        for i, btn in enumerate(self._buttons):
            if btn.isChecked():
                return i
        return 0


# ---------------------------------------------------------------------------
# 主面板
# ---------------------------------------------------------------------------

class ArtifactPreviewPanel(QFrame):
    """Artifact 预览面板。

    左侧 artifact 列表，右侧代码/预览双 Tab 内容区。
    """

    artifact_changed = Signal(str)  # 当前选中 artifact id

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ArtifactPreviewPanel")
        self.setStyleSheet(
            f"QFrame#ArtifactPreviewPanel{{background:{COLORS['BGD']};}}"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._artifacts: List[Dict[str, Any]] = []
        self._current_artifact: Optional[Dict[str, Any]] = None
        self._temp_dir: Optional[Path] = None
        self._web_view: Optional[Any] = None
        self._use_webengine = False
        self._merged_doc: str = ""

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(12)

        # ---------------- 左侧：artifact 列表 ----------------
        left_frame = QFrame()
        left_frame.setStyleSheet(
            f"QFrame{{background:{COLORS['SURFACE']};border-radius:{RADII['lg']};}}"
        )
        left_lay = QVBoxLayout(left_frame)
        left_lay.setContentsMargins(12, 12, 12, 12)
        left_lay.setSpacing(8)

        list_title = QLabel("Artifacts")
        list_title.setStyleSheet(
            f"QLabel{{color:{COLORS['TEXT']};font-size:16px;font-weight:700;"
            f"font-family:{FONT_STACK};background:transparent;}}"
        )
        left_lay.addWidget(list_title)

        self._list = QListWidget()
        self._list.setFrameShape(QFrame.NoFrame)
        self._list.setFocusPolicy(Qt.NoFocus)
        self._list.setSpacing(4)
        self._list.setStyleSheet(
            f"QListWidget{{background:transparent;border:none;outline:0px;}}"
            f"QListWidget::item{{background:{COLORS['CARD']};"
            f"border-radius:{RADII['md']};padding:0px;}}"
            f"QListWidget::item:selected{{background:{COLORS['PRIM_LIGHT']};}}"
            f"QListWidget::item:hover:!selected{{background:{COLORS['PRIM_LIGHT']};}}"
        )
        self._list.currentItemChanged.connect(self._on_list_selection_changed)
        left_lay.addWidget(self._list, 1)

        self._empty_hint = QLabel("暂无 Artifact")
        self._empty_hint.setAlignment(Qt.AlignCenter)
        self._empty_hint.setWordWrap(True)
        self._empty_hint.setStyleSheet(
            f"QLabel{{color:{COLORS['TEXT_4']};font-size:13px;"
            f"font-family:{FONT_STACK};background:transparent;}}"
        )
        left_lay.addWidget(self._empty_hint)

        root.addWidget(left_frame, 1)

        # ---------------- 右侧：工具栏 + Tab 内容区 ----------------
        right_frame = QFrame()
        right_frame.setStyleSheet(
            f"QFrame{{background:{COLORS['SURFACE']};border-radius:{RADII['lg']};}}"
        )
        right_lay = QVBoxLayout(right_frame)
        right_lay.setContentsMargins(12, 12, 12, 12)
        right_lay.setSpacing(8)

        # 工具栏
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self._tab_bar = _PreviewTabBar()
        self._tab_bar.tab_changed.connect(self._on_tab_changed)
        toolbar.addWidget(self._tab_bar)

        toolbar.addStretch(1)

        self._copy_btn = QPushButton("复制代码")
        self._copy_btn.setCursor(Qt.PointingHandCursor)
        self._copy_btn.setFixedHeight(30)
        self._copy_btn.setStyleSheet(self._tool_btn_style())
        self._copy_btn.clicked.connect(self._on_copy_code)
        toolbar.addWidget(self._copy_btn)

        self._open_btn = QPushButton("新窗口打开")
        self._open_btn.setCursor(Qt.PointingHandCursor)
        self._open_btn.setFixedHeight(30)
        self._open_btn.setStyleSheet(self._tool_btn_style())
        self._open_btn.clicked.connect(self._on_open_external)
        toolbar.addWidget(self._open_btn)

        self._refresh_btn = QPushButton("刷新预览")
        self._refresh_btn.setCursor(Qt.PointingHandCursor)
        self._refresh_btn.setFixedHeight(30)
        self._refresh_btn.setStyleSheet(self._tool_btn_style(primary=True))
        self._refresh_btn.clicked.connect(self.refresh)
        toolbar.addWidget(self._refresh_btn)

        right_lay.addLayout(toolbar)

        # 内容堆叠
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("QStackedWidget{background:transparent;border:none;}")
        right_lay.addWidget(self._stack, 1)

        # 代码区
        self._code_edit = QTextEdit()
        self._code_edit.setReadOnly(True)
        self._code_edit.setAcceptRichText(False)
        self._code_edit.setStyleSheet(
            f"QTextEdit{{background:{COLORS['CARD']};color:{COLORS['TEXT']};"
            f"border:1px solid {COLORS['BORDER']};border-radius:{RADII['md']};"
            f"padding:10px;font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, "
            f"'Source Code Pro', 'Courier New', monospace;font-size:12px;}}"
            f"QTextEdit:focus{{border:1px solid {COLORS['PRIM_DARK']};}}"
        )
        self._stack.addWidget(self._code_edit)

        # 预览区
        self._preview_container = QFrame()
        self._preview_container.setStyleSheet(
            f"QFrame{{background:{COLORS['CARD']};border-radius:{RADII['md']};}}"
        )
        preview_lay = QVBoxLayout(self._preview_container)
        preview_lay.setContentsMargins(1, 1, 1, 1)
        preview_lay.setSpacing(0)
        self._preview_stack = QStackedWidget(self._preview_container)
        self._preview_stack.setStyleSheet("QStackedWidget{background:transparent;border:none;}")
        preview_lay.addWidget(self._preview_stack)
        self._stack.addWidget(self._preview_container)

        # 浏览器/降级组件延迟初始化
        self._init_preview_engine()

        root.addWidget(right_frame, 2)

        self._update_empty_state()
        self._update_toolbar_state()

    # ------------------------------------------------------------------
    # 预览引擎初始化
    # ------------------------------------------------------------------

    def _init_preview_engine(self) -> None:
        """初始化预览引擎：优先 QWebEngineView，否则 QTextBrowser。"""
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
            from PySide6.QtWebEngineCore import QWebEngineSettings

            view = QWebEngineView()
            view.setStyleSheet("QWebEngineView{background:" + COLORS['CARD'] + ";}")
            page = view.page()
            if page is not None:
                settings = page.settings()
                if settings is not None:
                    # 禁止本地预览页面访问任意 file:// URL，防止信息泄露
                    settings.setAttribute(
                        QWebEngineSettings.LocalContentCanAccessFileUrls, False
                    )
                    settings.setAttribute(
                        QWebEngineSettings.LocalContentCanAccessRemoteUrls, False
                    )
            self._web_view = view
            self._preview_stack.addWidget(view)
            self._use_webengine = True
        except Exception:
            self._web_view = None
            self._use_webengine = False

        # 降级：纯文本浏览器（显示源码）
        self._text_browser = QTextBrowser()
        self._text_browser.setStyleSheet(
            f"QTextBrowser{{background:{COLORS['CARD']};color:{COLORS['TEXT']};"
            f"border:none;border-radius:{RADII['md']};"
            f"padding:10px;font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, "
            f"'Source Code Pro', 'Courier New', monospace;font-size:12px;}}"
        )
        self._text_browser.setOpenExternalLinks(False)
        self._text_browser.setOpenLinks(False)
        self._preview_stack.addWidget(self._text_browser)

        self._show_active_preview_widget()

    def _show_active_preview_widget(self) -> None:
        """根据当前引擎能力，决定显示 webview 还是 text browser。"""
        if self._use_webengine and self._web_view is not None:
            self._preview_stack.setCurrentWidget(self._web_view)
        else:
            self._preview_stack.setCurrentWidget(self._text_browser)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def add_artifact(self, artifact: Any) -> str:
        """添加 artifact 到列表并返回其 id。"""
        art = _normalize_artifact(artifact)
        if art is None:
            return ""
        self._artifacts.append(art)
        self._add_list_item(art)
        self._update_empty_state()
        return str(art["id"])

    def remove_artifact(self, artifact_id: str) -> bool:
        """按 id 删除 artifact。"""
        artifact_id = str(artifact_id)
        for i, art in enumerate(self._artifacts):
            if str(art.get("id", "")) == artifact_id:
                self._artifacts.pop(i)
                # 先销毁 setItemWidget 设置的 widget，避免内存泄漏
                item = self._list.item(i)
                if item is not None:
                    w = self._list.itemWidget(item)
                    if w is not None:
                        try:
                            w.deleteLater()
                        except RuntimeError:
                            pass
                self._list.takeItem(i)
                self._update_empty_state()
                if self._current_artifact is not None and str(
                    self._current_artifact.get("id", "")
                ) == artifact_id:
                    self._current_artifact = None
                    self._update_toolbar_state()
                    self._render_current_artifact()
                return True
        return False

    def set_artifact(self, artifact: Any) -> None:
        """设置当前 artifact（新增或更新已有项）。"""
        art = _normalize_artifact(artifact)
        if art is None:
            return

        existing_idx = None
        for i, a in enumerate(self._artifacts):
            if str(a.get("id", "")) == str(art.get("id", "")):
                existing_idx = i
                break

        if existing_idx is not None:
            self._artifacts[existing_idx] = art
            item = self._list.item(existing_idx)
            if item is not None:
                widget = self._list.itemWidget(item)
                if isinstance(widget, _ArtifactListItem):
                    widget.set_title(str(art.get("title", "未命名")))
            # 如果正是当前选中项，平滑刷新内容
            if self._current_artifact is not None and str(
                self._current_artifact.get("id", "")
            ) == str(art.get("id", "")):
                self._current_artifact = art
                self._render_current_artifact()
        else:
            self.add_artifact(art)
            # 选中新添加的项
            self._select_by_id(str(art.get("id", "")))

    def set_artifact_by_id(self, artifact_id: str) -> None:
        """按 id 切换到指定 artifact。"""
        self._select_by_id(str(artifact_id))

    def current_artifact(self) -> Optional[Dict[str, Any]]:
        """返回当前 artifact 字典。"""
        return self._current_artifact

    def refresh(self) -> None:
        """重新渲染当前 artifact（保留当前 Tab）。"""
        self._render_current_artifact()

    def clear(self) -> None:
        """清空所有 artifact。"""
        # 先销毁 setItemWidget 设置的 widget，避免内存泄漏
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is not None:
                w = self._list.itemWidget(item)
                if w is not None:
                    try:
                        w.deleteLater()
                    except RuntimeError:
                        pass
        self._artifacts.clear()
        self._list.clear()
        self._current_artifact = None
        self._merged_doc = ""
        self._update_empty_state()
        self._update_toolbar_state()
        self._render_current_artifact()

    # ------------------------------------------------------------------
    # 兼容旧版 ArtifactManager 集成 API
    # ------------------------------------------------------------------

    def update_artifacts(self, artifacts: List[Any]) -> None:
        """批量替换 artifact 列表；无 artifact 时隐藏面板。"""
        self.clear()
        normalized = [_normalize_artifact(a) for a in artifacts]
        normalized = [a for a in normalized if a is not None]
        for art in normalized:
            self._artifacts.append(art)
            self._add_list_item(art)

        self._merged_doc = _build_preview_document(self._artifacts)
        if self._artifacts:
            # 默认选中第一个 HTML 或第一个可渲染项
            selected = next(
                (a for a in self._artifacts if _is_renderable_html(a)),
                self._artifacts[0],
            )
            self._select_by_id(str(selected.get("id", "")))
        else:
            self._current_artifact = None

        self.setVisible(bool(self._artifacts))
        self._update_empty_state()
        self._update_toolbar_state()
        self._render_current_artifact()

    def has_artifacts(self) -> bool:
        """是否存在 artifact。"""
        return len(self._artifacts) > 0

    def current_document(self) -> str:
        """返回合并后的 HTML 文档。"""
        if self._merged_doc:
            return self._merged_doc
        if self._current_artifact is not None:
            return _build_preview_html(self._current_artifact)
        return ""

    # ------------------------------------------------------------------
    # 内部：列表与渲染
    # ------------------------------------------------------------------

    def _add_list_item(self, artifact: Dict[str, Any]) -> None:
        item = QListWidgetItem()
        item.setData(Qt.UserRole, str(artifact.get("id", "")))
        widget = _ArtifactListItem(artifact)
        self._list.addItem(item)
        self._list.setItemWidget(item, widget)
        item.setSizeHint(widget.sizeHint())

    def _select_by_id(self, artifact_id: str) -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is not None and item.data(Qt.UserRole) == artifact_id:
                self._list.setCurrentRow(i)
                return

    def _on_list_selection_changed(
        self, current: Optional[QListWidgetItem], _previous: Optional[QListWidgetItem]
    ) -> None:
        if current is None:
            self._current_artifact = None
        else:
            aid = current.data(Qt.UserRole)
            matched = None
            for art in self._artifacts:
                if str(art.get("id", "")) == str(aid):
                    matched = art
                    break
            # 若列表项对应 artifact 已不存在，必须清空当前选中状态，避免 stale 内容。
            self._current_artifact = matched
        self._merged_doc = _build_preview_document(self._artifacts)
        self._update_toolbar_state()
        self._render_current_artifact()
        if self._current_artifact is not None:
            self.artifact_changed.emit(str(self._current_artifact.get("id", "")))

    def _on_tab_changed(self, idx: int) -> None:
        self._stack.setCurrentIndex(idx)
        if idx == 1 and self._current_artifact is not None:
            # 切到预览 Tab 时确保内容已渲染
            self._render_preview()

    def _render_current_artifact(self) -> None:
        """同时更新代码区与预览区，但保持当前 Tab 可见。"""
        self._render_code()
        self._render_preview()
        self._update_toolbar_state()

    def _render_code(self) -> None:
        text = _build_fallback_text(self._current_artifact)
        if self._code_edit.toPlainText() != text:
            self._code_edit.setPlainText(text)

    def _render_preview(self) -> None:
        """渲染预览区；平滑更新，避免整面板闪烁。"""
        if self._current_artifact is None:
            if self._use_webengine and self._web_view is not None:
                self._web_view.setHtml("")
            self._text_browser.clear()
            return

        if not _is_renderable_html(self._current_artifact):
            # 不可渲染类型在预览区也展示源码提示
            self._text_browser.setPlainText(
                "当前类型不支持可视化预览：\n" + self._current_artifact.get("type", "text")
                + "\n\n" + _build_fallback_text(self._current_artifact)
            )
            self._preview_stack.setCurrentWidget(self._text_browser)
            return

        self._show_active_preview_widget()

        html_doc = _build_preview_html(self._current_artifact)

        if self._use_webengine and self._web_view is not None:
            # 写到隔离的临时目录，并通过本地文件加载；已关闭 local file URL 访问
            self._ensure_temp_dir()
            # 刷新前先清理旧的预览文件，避免频繁刷新导致磁盘泄漏
            self._cleanup_preview_files()
            temp_path = self._temp_dir / f"preview_{uuid.uuid4().hex}.html"
            try:
                temp_path.write_text(html_doc, encoding="utf-8")
                self._web_view.setUrl(QUrl.fromLocalFile(str(temp_path.resolve())))
            except Exception:
                # 写文件失败则回退到 setHtml
                self._web_view.setHtml(html_doc, QUrl("about:blank"))
        else:
            self._text_browser.setPlainText(_build_fallback_text(self._current_artifact))

    def _ensure_temp_dir(self) -> None:
        if self._temp_dir is None:
            base = Path(tempfile.gettempdir()) / "dscode_artifact_preview"
            try:
                shutil.rmtree(base, ignore_errors=True)
            except Exception:
                pass
            base.mkdir(parents=True, exist_ok=True)
            self._temp_dir = base

    def _cleanup_preview_files(self) -> None:
        """删除本面板创建的过期预览 HTML 文件，只保留当前正在显示的文件。"""
        if self._temp_dir is None:
            return
        current_url = ""
        if self._use_webengine and self._web_view is not None:
            try:
                current_url = self._web_view.url().toString()
            except Exception:
                pass
        for f in self._temp_dir.glob("preview_*.html"):
            try:
                if current_url and str(f.resolve()) in current_url:
                    continue
                f.unlink()
            except Exception:
                pass
        # artifact_*.html 由 _on_open_external 写出供外部浏览器使用，
        # 不在此处清理——否则用户切换 artifact 时浏览器中的文件会被删除。
        # 这些文件在 closeEvent 的 shutil.rmtree(self._temp_dir) 中统一清理。

    # ------------------------------------------------------------------
    # 工具按钮
    # ------------------------------------------------------------------

    def _on_copy_code(self) -> None:
        text = _build_fallback_text(self._current_artifact)
        if not text:
            return
        app = QApplication.instance()
        if app is not None:
            clipboard = app.clipboard()
            if clipboard is not None:
                clipboard.setText(text, QClipboard.Clipboard)

    def _on_open_external(self) -> None:
        if self._current_artifact is None:
            return
        content = str(self._current_artifact.get("content", ""))
        ctype = str(self._current_artifact.get("type", "text")).lower().strip()

        if ctype == "svg":
            # SVG 支持 <script> 和事件处理器，必须用 CSP+sandbox 包装
            html_doc = _build_preview_html(self._current_artifact)
        elif ctype in ("html", "react", "vue", "javascript", "js", "typescript"):
            html_doc = _build_preview_html(self._current_artifact)
        else:
            html_doc = "<pre>" + html.escape(content) + "</pre>"

        self._ensure_temp_dir()
        temp_path = self._temp_dir / f"artifact_{uuid.uuid4().hex}.html"
        try:
            temp_path.write_text(html_doc, encoding="utf-8")
            import webbrowser
            webbrowser.open(f"file://{temp_path.resolve()}")
        except Exception as exc:
            QMessageBox.warning(self, "打开失败", f"无法在新窗口打开：{exc}")

    # ------------------------------------------------------------------
    # UI 状态
    # ------------------------------------------------------------------

    def _update_empty_state(self) -> None:
        has_items = self._list.count() > 0
        self._list.setVisible(has_items)
        self._empty_hint.setVisible(not has_items)

    def _update_toolbar_state(self) -> None:
        has = self._current_artifact is not None
        self._copy_btn.setEnabled(has)
        self._open_btn.setEnabled(has)
        self._refresh_btn.setEnabled(has)

    # ------------------------------------------------------------------
    # 样式
    # ------------------------------------------------------------------

    @staticmethod
    def _tool_btn_style(primary: bool = False) -> str:
        if primary:
            return (
                f"QPushButton{{background:{COLORS['PRIM']};color:{COLORS['TEXT']};"
                f"border:1px solid {COLORS['PRIM_DARK']};"
                f"border-radius:{RADII['md']};padding:0 12px;"
                f"font-family:{FONT_STACK};font-size:12px;font-weight:700;}}"
                f"QPushButton:hover{{background:{COLORS['PRIM_DARK']};"
                f"color:{COLORS['CARD']};}}"
                f"QPushButton:disabled{{color:{COLORS['TEXT_5']};"
                f"background:{COLORS['PRIM_LIGHT']};border:1px solid {COLORS['BORDER']};}}"
            )
        return (
            f"QPushButton{{background:{COLORS['CARD']};color:{COLORS['TEXT']};"
            f"border:1px solid {COLORS['BORDER']};border-radius:{RADII['md']};"
            f"padding:0 12px;font-family:{FONT_STACK};"
            f"font-size:12px;font-weight:600;}}"
            f"QPushButton:hover{{background:{COLORS['PRIM_LIGHT']};"
            f"color:{COLORS['TEXT']};border:1px solid {COLORS['PRIM_DARK']};}}"
            f"QPushButton:disabled{{color:{COLORS['TEXT_5']};"
            f"background:{COLORS['CARD']};}}"
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        """关闭时清理临时目录。"""
        if self._temp_dir is not None:
            try:
                shutil.rmtree(self._temp_dir, ignore_errors=True)
            except Exception:
                pass
            self._temp_dir = None
        super().closeEvent(event)
