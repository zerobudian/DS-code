# -*- coding: utf-8 -*-
"""InputArea 子模块：文件 / 图片附件管理。

负责：
  - 拖拽 (dragEnterEvent / dropEvent) 与剪贴板粘贴 (Ctrl+V)
  - 本地文件 → 图片 data URL 或 @filepath 注入
  - 图片缩略药丸 / 文件药丸 UI（白色药丸 + 关闭按钮）
  - 批量设置文件标签 (set_file_tags)
  - 公共 API: get_uploaded_files / get_images / add_image / clear_all_uploads
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QPoint, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QToolTip, QVBoxLayout,
)

from src.gui._gui_helpers import _load_icon
from src.gui.style import COLORS, FONT_STACK, RADII, rgba, tr


class AttachmentManagerMixin:
    """文件 / 图片附件：拖拽、粘贴、药丸标签、批量设置。"""

    # 单次最多上传图片数量
    _MAX_IMAGES = 10

    # ------------------------------------------------------------------
    # 控件初始化（由 InputArea.__init__ 调用）
    # ------------------------------------------------------------------
    def _init_attachment_widgets(self, parent_lay: QVBoxLayout) -> None:
        """创建 file_wrap（药丸容器）并加入 parent_lay。"""
        # 文件/图片附件标签区（水平排列白色药丸）
        self.file_wrap = QFrame()
        self.file_wrap.setStyleSheet("background:transparent;")
        self.file_lay = QHBoxLayout(self.file_wrap)
        self.file_lay.setContentsMargins(12, 0, 12, 0)
        self.file_lay.setSpacing(6)
        self.file_lay.addStretch(1)
        self.file_wrap.setVisible(False)
        parent_lay.addWidget(self.file_wrap)

    # ==================================================================
    # 拖拽 & 粘贴
    # ==================================================================
    # 注意：``dragEnterEvent`` / ``dropEvent`` 是 Qt 虚函数，由 Shiboken
    # 的 QWidget 在 MRO 中提供，Python Mixin 无法直接覆盖。因此这两个
    # 方法重命名为 ``_handle_drag_enter`` / ``_handle_drop``，由
    # ``InputArea`` 中的 ``dragEnterEvent`` / ``dropEvent`` 包装调用。
    # 返回值约定：True = 已处理（接受拖拽），False = 交给默认处理。
    def _handle_drag_enter(self, event) -> bool:
        """文件拖入时接受。返回 True 表示已处理。"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return True
        elif event.mimeData().hasImage():
            event.acceptProposedAction()
            return True
        return False

    def _handle_drop(self, event) -> bool:
        """文件拖入时处理。返回 True 表示已处理。"""
        md = event.mimeData()
        found = False
        if md.hasUrls():
            for url in md.urls():
                if url.isLocalFile():
                    self._handle_local_file(url.toLocalFile())
                    found = True
        if not found and md.hasImage():
            # 直接拖入的图片对象（剪贴板的图片）
            pixmap = QPixmap(md.imageData())
            if pixmap and not pixmap.isNull():
                self._add_pixmap(pixmap)
                found = True
        if found:
            event.acceptProposedAction()
            # 用户上传 → 隐藏统计卡片
            try:
                self.user_activity.emit()
            except Exception:
                pass
        return found

    # ==================================================================
    # 文件 / 图片处理（内部辅助）
    # ==================================================================
    def _try_paste_from_clipboard(self) -> bool:
        """尝试从剪贴板粘贴文件/图片。返回 True 表示已处理。"""
        try:
            cb = QApplication.clipboard()
            md = cb.mimeData()
            if md.hasUrls():
                found = False
                for url in md.urls():
                    if url.isLocalFile():
                        self._handle_local_file(url.toLocalFile())
                        found = True
                if found:
                    try:
                        self.user_activity.emit()
                    except Exception:
                        pass
                    return True
            if md.hasImage():
                pixmap = QPixmap(md.imageData())
                if pixmap and not pixmap.isNull():
                    self._add_pixmap(pixmap)
                    try:
                        self.user_activity.emit()
                    except Exception:
                        pass
                    return True
        except Exception:
            pass
        return False

    def _handle_local_file(self, filepath: str):
        """处理一个本地文件（图片或一般文件）。"""
        if not filepath or not os.path.isfile(filepath):
            return
        # 文件大小限制：图片不超过 20MB，防止加载超大文件导致 OOM
        try:
            fsize = os.path.getsize(filepath)
            if fsize > 20 * 1024 * 1024:
                self._add_file(filepath)
                return
        except OSError:
            self._add_file(filepath)
            return
        ext = os.path.splitext(filepath)[1].lower()
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".tif"}
        if ext in image_exts:
            # 图片 → 转 data URL 上传 (用于 vision)
            # 使用 QImageReader 安全加载：设置缩放后再读取，避免大图直接加载到内存
            try:
                from PySide6.QtGui import QPixmap as _QP, QImageReader as _QIR
                reader = _QIR(filepath)
                reader.setAutoTransform(True)  # 自动处理 EXIF 旋转
                # 先读取尺寸判断是否需要缩放
                size = reader.size()
                MAX_LONG_EDGE = 2048
                if size.isValid() and (size.width() > MAX_LONG_EDGE or size.height() > MAX_LONG_EDGE):
                    reader.setScaledSize(size.scaled(MAX_LONG_EDGE, MAX_LONG_EDGE, Qt.KeepAspectRatio))
                img = reader.read()
                if not img.isNull():
                    pm = _QP.fromImage(img)
                    if pm and not pm.isNull():
                        self._add_pixmap(pm)
                        return
            except Exception:
                pass
            # QImageReader 失败时回退到直接加载（兼容旧版Qt）
            try:
                from PySide6.QtGui import QPixmap as _QP
                pm = _QP(filepath)
                if pm and not pm.isNull():
                    self._add_pixmap(pm)
                    return
            except Exception:
                pass
        # 其他文件 → 作为 mention 注入
        self._add_file(filepath)

    def _add_pixmap(self, pm: QPixmap):
        """把 QPixmap 转为 base64 data URL 并加入上传列表。
        自动压缩大图：最长边不超过 2048px，避免 base64 过大导致 API 请求失败。
        """
        try:
            import base64
            from PySide6.QtCore import QBuffer, QSize
            # 自动压缩大图：最长边不超过 MAX_LONG_EDGE 像素
            MAX_LONG_EDGE = 2048
            if pm.width() > MAX_LONG_EDGE or pm.height() > MAX_LONG_EDGE:
                pm = pm.scaled(
                    MAX_LONG_EDGE, MAX_LONG_EDGE,
                    Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
            qbuf = QBuffer()
            qbuf.open(QBuffer.ReadWrite)
            try:
                # 用 JPEG 格式（quality=85）替代 PNG 以减小体积；透明通道用 PNG
                has_alpha = pm.hasAlphaChannel()
                if has_alpha:
                    pm.save(qbuf, "PNG")
                else:
                    pm.save(qbuf, "JPEG", 85)
                raw = bytes(qbuf.data())
                b64 = base64.b64encode(raw).decode("ascii")
                mime = "image/png" if has_alpha else "image/jpeg"
                data_url = f"data:{mime};base64,{b64}"
                self.add_image(data_url)
            finally:
                qbuf.close()
        except Exception:
            pass

    def _add_file(self, filepath: str):
        """添加一个非图片文件。显示为白色药丸标签（匹配设计稿），发送时以 @filepath 形式注入。"""
        try:
            size = os.path.getsize(filepath)
            name = os.path.basename(filepath)
            info = {"path": filepath, "name": name, "size": size}
            self._uploaded_files.append(info)

            # 白色药丸样式（白底、圆角、右上角X关闭按钮）
            pill = QFrame()
            pill._file_info = info
            pill.setStyleSheet(
                "QFrame{background:" + COLORS['SURFACE'] + ";"
                "border:none;"
                "border-radius:" + RADII['md'] + ";}"
            )
            # 使用 QGridLayout 实现内容+右上角关闭按钮的叠加
            gl = QGridLayout(pill)
            gl.setContentsMargins(10, 6, 26, 6)
            gl.setSpacing(0)

            # 内容行：图标 + 文件名 + 大小
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)

            ext = os.path.splitext(name)[1].lower()
            if ext in {".pdf"}:
                icon_text = "📄"
            elif ext in {".doc", ".docx", ".txt", ".md"}:
                icon_text = "📝"
            elif ext in {".zip", ".tar", ".gz", ".rar", ".7z"}:
                icon_text = "📦"
            elif ext in {".py", ".js", ".ts", ".html", ".css", ".json", ".java", ".cpp", ".c"}:
                icon_text = "📜"
            else:
                icon_text = "📎"
            icon_lbl = QLabel(icon_text)
            icon_lbl.setFixedSize(20, 20)
            icon_lbl.setAlignment(Qt.AlignCenter)
            icon_lbl.setStyleSheet("font-size:14px;background:transparent;")
            row.addWidget(icon_lbl)

            # 文件名
            name_lbl = QLabel(name)
            name_lbl.setWordWrap(False)
            name_lbl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            name_lbl.setStyleSheet(
                "QLabel{color:" + COLORS['TEXT'] + ";font-size:12px;font-weight:500;"
                "font-family:" + FONT_STACK + ";background:transparent;}"
            )
            row.addWidget(name_lbl, 1)

            # 文件大小
            if size < 1024:
                sz_text = f"{size} B"
            elif size < 1024 * 1024:
                sz_text = f"{size/1024:.0f} KB"
            else:
                sz_text = f"{size/1024/1024:.1f} MB"
            sz_lbl = QLabel(sz_text)
            sz_lbl.setWordWrap(False)
            sz_lbl.setStyleSheet(
                "QLabel{color:" + COLORS['TEXT_3'] + ";font-size:10px;"
                "font-family:" + FONT_STACK + ";background:transparent;"
                "margin-left:4px;}"
            )
            row.addWidget(sz_lbl)

            gl.addLayout(row, 0, 0)

            # 右上角关闭按钮
            close = QPushButton()
            close.setFixedSize(18, 18)
            close.setCursor(Qt.PointingHandCursor)
            close.setIcon(_load_icon("close_16.png", 12))
            close.setIconSize(QSize(12, 12))
            close.setStyleSheet(
                "QPushButton{background:" + rgba(COLORS['TEXT'], 0.06) + ";border:none;border-radius:" + RADII['circle'] + ";"
                "padding:0px;}"
                "QPushButton:hover{background:" + rgba(COLORS['TEXT'], 0.15) + ";}"
            )
            close.clicked.connect(lambda _=False, pw=pill:
                                  self._remove_file_by_widget(pw))
            gl.addWidget(close, 0, 0, Qt.AlignTop | Qt.AlignRight)

            self.file_lay.insertWidget(self.file_lay.count() - 1, pill)
            self.file_wrap.setVisible(True)
            self._update_send_button_state()
        except Exception:
            pass

    def _remove_file(self, index: int, pill_widget):
        try:
            if 0 <= index < len(self._uploaded_files):
                del self._uploaded_files[index]
        except Exception:
            pass
        try:
            pill_widget.setParent(None)
            pill_widget.deleteLater()
        except Exception:
            pass

    def _remove_file_by_widget(self, pill_widget):
        """通过 widget 引用删除文件（避免索引陈旧问题）。"""
        try:
            info = getattr(pill_widget, "_file_info", None)
            if info and info in self._uploaded_files:
                self._uploaded_files.remove(info)
        except Exception:
            pass
        try:
            pill_widget.setParent(None)
            pill_widget.deleteLater()
        except Exception:
            pass
        try:
            # 没有剩余附件就隐藏 file_wrap
            children = [self.file_lay.itemAt(i) for i in range(self.file_lay.count())]
            visible_widgets = [it.widget() for it in children if it and it.widget()]
            if len(visible_widgets) <= 1:
                self.file_wrap.setVisible(False)
        except Exception:
            pass
        self._update_send_button_state()

    # ==================================================================
    # 公共 API
    # ==================================================================
    def get_uploaded_files(self) -> list:
        """返回用户拖入的非图片文件 — 以 @filepath 形式注入消息。"""
        return list(self._uploaded_files)

    def clear_all_uploads(self) -> None:
        """发送后清空所有上传（图片 + 文件）。"""
        self._uploaded_images = []
        self._uploaded_files = []
        try:
            # 清空所有 file_lay 中的 widget
            while self.file_lay.count() > 1:
                item = self.file_lay.takeAt(0)
                if item:
                    w = item.widget()
                    if w:
                        w.setParent(None)
                        w.deleteLater()
            self.file_wrap.setVisible(False)
        except Exception:
            pass
        self._update_send_button_state()

    def get_images(self) -> list:
        """返回当前已上传的图片 base64 data URL 列表"""
        return list(self._uploaded_images)

    def add_image(self, image_data_url: str):
        """添加一张图片（data URL），显示为白色缩略药丸（匹配设计稿）供用户删除"""
        if len(self._uploaded_images) >= self._MAX_IMAGES:
            # 超限提示：在输入框附近显示临时 tooltip
            try:
                btn = getattr(self, 'upload_btn', None)
                if btn is not None:
                    pos = btn.mapToGlobal(QPoint(0, btn.height() + 4))
                    QToolTip.showText(pos, tr("max_images_msg", n=self._MAX_IMAGES), btn)
            except Exception:
                pass
            return
        self._uploaded_images.append(image_data_url)
        # 用户上传图片 → 隐藏统计卡片
        try:
            self.user_activity.emit()
        except Exception:
            pass
        try:
            # 白色药丸样式（匹配文件药丸和设计稿）
            pill = QFrame()
            pill._image_data = image_data_url
            pill._is_image_pill = True
            pill.setStyleSheet(
                "QFrame{background:" + COLORS['SURFACE'] + ";"
                "border:none;"
                "border-radius:" + RADII['md'] + ";}"
            )
            gl = QGridLayout(pill)
            gl.setContentsMargins(6, 6, 6, 6)
            gl.setSpacing(0)

            # 内容容器：缩略图 + 文字
            content = QVBoxLayout()
            content.setContentsMargins(0, 0, 0, 0)
            content.setSpacing(3)
            content.setAlignment(Qt.AlignCenter)

            # 缩略图
            from src.gui.bubbles import ImageBubble
            pm = ImageBubble._parse_data_url(image_data_url)
            thumb = QLabel()
            THUMB_SZ = 56
            if pm and not pm.isNull():
                thumb.setPixmap(pm.scaled(THUMB_SZ, THUMB_SZ, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
            else:
                thumb.setText("📷")
            thumb.setFixedSize(THUMB_SZ, THUMB_SZ)
            thumb.setAlignment(Qt.AlignCenter)
            thumb.setStyleSheet(
                "background:" + COLORS['SURFACE'] + ";"
                "border-radius:" + RADII['sm'] + ";"
            )
            # 裁剪缩略图为圆角（通过 QFrame 包裹）
            thumb.setScaledContents(False)
            content.addWidget(thumb, 0, Qt.AlignHCenter)

            name = QLabel(tr("image_n_fmt", n=len(self._uploaded_images)))
            name.setWordWrap(False)
            name.setAlignment(Qt.AlignCenter)
            name.setStyleSheet(
                "QLabel{color:" + COLORS['TEXT_2'] + ";font-size:10px;"
                "font-family:" + FONT_STACK + ";background:transparent;}"
            )
            content.addWidget(name)
            pill._name_label = name

            gl.addLayout(content, 0, 0)

            # 右上角关闭按钮（圆形小X）
            close = QPushButton()
            close.setFixedSize(18, 18)
            close.setCursor(Qt.PointingHandCursor)
            close.setIcon(_load_icon("close_16.png", 12))
            close.setIconSize(QSize(12, 12))
            close.setStyleSheet(
                "QPushButton{background:" + rgba(COLORS['TEXT'], 0.06) + ";border:none;border-radius:" + RADII['circle'] + ";"
                "padding:0px;}"
                "QPushButton:hover{background:" + rgba(COLORS['TEXT'], 0.15) + ";}"
            )
            close.clicked.connect(lambda _=False, pw=pill: self._remove_image_by_widget(pw))
            gl.addWidget(close, 0, 0, Qt.AlignTop | Qt.AlignRight)

            self.file_lay.insertWidget(self.file_lay.count() - 1, pill)
            self.file_wrap.setVisible(True)
            self._update_send_button_state()
        except Exception:
            pass

    # ==================================================================
    # 图片药丸维护
    # ==================================================================
    def _refresh_image_pill_labels(self):
        """重新编号所有图片 pill 标签（删除后调用以避免编号错乱）。"""
        try:
            img_idx = 0
            for i in range(self.file_lay.count()):
                item = self.file_lay.itemAt(i)
                if not item:
                    continue
                w = item.widget()
                if w and getattr(w, "_is_image_pill", False):
                    img_idx += 1
                    name_lbl = getattr(w, "_name_label", None)
                    if name_lbl is not None:
                        name_lbl.setText(tr("image_n_fmt", n=img_idx))
        except Exception:
            pass

    def _remove_image(self, index: int, pill_widget):
        try:
            if 0 <= index < len(self._uploaded_images):
                del self._uploaded_images[index]
        except Exception:
            pass
        try:
            pill_widget.setParent(None)
            pill_widget.deleteLater()
        except Exception:
            pass
        try:
            # 没有剩余附件就隐藏 file_wrap
            children = [self.file_lay.itemAt(i) for i in range(self.file_lay.count())]
            visible_widgets = [it.widget() for it in children if it and it.widget()]
            if len(visible_widgets) <= 1:
                self.file_wrap.setVisible(False)
        except Exception:
            pass

    def _remove_image_by_widget(self, pill_widget):
        """通过 widget 引用删除图片（避免索引陈旧问题）。"""
        try:
            img_data = getattr(pill_widget, "_image_data", None)
            if img_data and img_data in self._uploaded_images:
                self._uploaded_images.remove(img_data)
        except Exception:
            pass
        self._refresh_image_pill_labels()
        try:
            pill_widget.setParent(None)
            pill_widget.deleteLater()
        except Exception:
            pass
        try:
            children = [self.file_lay.itemAt(i) for i in range(self.file_lay.count())]
            visible_widgets = [it.widget() for it in children if it and it.widget()]
            if len(visible_widgets) <= 1:
                self.file_wrap.setVisible(False)
        except Exception:
            pass
        self._update_send_button_state()

    def set_file_tags(self, file_list):
        """批量设置文件标签（替换当前所有）
        file_list: list[dict] - 每项可选字段: {path, name, size}
        """
        self.clear_all_uploads()
        if not file_list:
            return
        try:
            for f in file_list:
                if not isinstance(f, dict):
                    continue
                path = f.get("path") or f.get("name") or ""
                if path and os.path.isfile(path):
                    self._handle_local_file(path)
                else:
                    # 纯标签 —— 构造一个占位项
                    try:
                        name = f.get("name") or path or tr("file_label")
                        size = f.get("size", 0)
                        self._uploaded_files.append({"path": path, "name": name, "size": size})
                        pill = QFrame()
                        pill.setStyleSheet(
                            "QFrame{background:" + COLORS['PRIM_LIGHT'] + ";"
                            "border:1px solid " + COLORS['BORDER'] + ";"
                            "border-radius:" + RADII['xs'] + ";}"
                        )
                        pl = QHBoxLayout(pill)
                        pl.setContentsMargins(10, 6, 10, 6)
                        pl.setSpacing(8)
                        label = QLabel(name)
                        label.setWordWrap(False)
                        label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
                        label.setStyleSheet(
                            "QLabel{color:" + COLORS['TEXT_2'] + ";font-size:12px;font-family:" + FONT_STACK + ";"
                            "background:transparent;}"
                        )
                        pl.addWidget(label, 1)
                        close = QPushButton()
                        close.setFixedSize(24, 24)
                        close.setCursor(Qt.PointingHandCursor)
                        close.setIcon(_load_icon("close_16.png", 16))
                        close.setIconSize(QSize(16, 16))
                        close.setStyleSheet(
                            "QPushButton{background:transparent;border:none;"
                            "border-radius:" + RADII['xs'] + ";}"
                            "QPushButton:hover{background:" + COLORS['PRIM_LIGHT'] + ";}"
                        )
                        close.clicked.connect(
                            lambda _=False, idx=len(self._uploaded_files) - 1, pw=pill: self._remove_file(idx, pw)
                        )
                        pl.addWidget(close)
                        self.file_lay.insertWidget(self.file_lay.count() - 1, pill)
                        self.file_wrap.setVisible(True)
                    except Exception:
                        pass
        except Exception:
            pass
