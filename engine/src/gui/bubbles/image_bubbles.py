# -*- coding: utf-8 -*-
"""图片相关气泡：ClickableImageLabel、ImagePreviewDialog、ImageBubble、UserAttachmentBubble。"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QDialog, QSizePolicy,
)

from src.gui.style import RADII, COLORS, FONT_STACK, rgba, FONT_WEIGHTS
from src.gui.bubbles._helpers import _try_pm

# ======================================================================
# ClickableImageLabel — 可点击的图片标签（点击放大预览）
# ======================================================================
class ClickableImageLabel(QLabel):
    """带手型光标的图片标签，点击时发射信号。"""
    clicked = Signal()

    def __init__(self, pixmap=None, parent=None):
        super().__init__(parent)
        self._pixmap = pixmap
        if pixmap and not pixmap.isNull():
            self.setPixmap(pixmap)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

# ======================================================================
# ImagePreviewDialog — 图片放大预览对话框（Lightbox）
# ======================================================================
class ImagePreviewDialog(QDialog):
    """简单的图片放大预览对话框：黑色半透明背景，点击或ESC关闭。"""

    def __init__(self, pixmap: QPixmap, parent=None):
        super().__init__(parent)
        # 无边框 + 覆盖主窗口
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setStyleSheet(f"background:{rgba(COLORS['BLACK'], 0.88)};")
        self.setModal(True)

        # 铺满父窗口或屏幕
        if parent:
            # 将父控件的局部坐标映射为全局坐标
            top_left = parent.mapToGlobal(parent.rect().topLeft())
            self.setGeometry(top_left.x(), top_left.y(), parent.width(), parent.height())
        else:
            screen = self.screen()
            if screen:
                self.setGeometry(screen.availableGeometry())
            else:
                self.resize(1200, 800)

        # 全屏布局
        root = QVBoxLayout(self)
        root.setContentsMargins(40, 40, 40, 40)
        root.setSpacing(8)
        root.setAlignment(Qt.AlignCenter)

        # 提示文字
        hint = QLabel("点击任意位置或按 ESC 关闭")
        hint.setStyleSheet(
            f"color:{rgba(COLORS['CARD'], 0.5)};font-size:12px;"
            f"font-family:{FONT_STACK};background:transparent;"
        )
        hint.setAlignment(Qt.AlignCenter)
        root.addWidget(hint)

        # 图片标签
        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignCenter)
        self._img_lbl.setStyleSheet("background:transparent;")
        self._img_lbl.setCursor(Qt.PointingHandCursor)
        if pixmap and not pixmap.isNull():
            # 缩放到对话框大小的 90%
            area_w = self.width() - 80
            area_h = self.height() - 100
            max_w = max(200, int(area_w * 0.92))
            max_h = max(200, int(area_h * 0.92))
            scaled = pixmap.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._img_lbl.setPixmap(scaled)
            self._full_pixmap = pixmap
        else:
            self._full_pixmap = QPixmap()
        root.addWidget(self._img_lbl, 1)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.close()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def showEvent(self, event):
        """显示时根据实际大小重新缩放图片。"""
        super().showEvent(event)
        if self._full_pixmap and not self._full_pixmap.isNull():
            area_w = self.width() - 80
            area_h = self.height() - 100
            max_w = max(200, int(area_w * 0.92))
            max_h = max(200, int(area_h * 0.92))
            scaled = self._full_pixmap.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._img_lbl.setPixmap(scaled)

def _show_image_preview(pixmap: QPixmap, parent=None):
    """打开图片预览对话框的便捷函数。"""
    if not pixmap or pixmap.isNull():
        return
    try:
        dlg = ImagePreviewDialog(pixmap, parent=parent)
        dlg.exec()
    except Exception:
        pass

# ======================================================================
# ImageBubble — AI 侧图片气泡（鲸鱼头像在左，白底卡片）
# ======================================================================
class ImageBubble(QFrame):
    removed = Signal(int)

    def __init__(self, image_data_url: str, index: int = 0, max_width: int = 420, parent=None):
        super().__init__(parent)
        self.image_data_url = image_data_url
        self.index = index
        self.setStyleSheet("background:transparent;")

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        # 尝试从 data URL 读取图
        pixmap = self._parse_data_url(image_data_url)

        # AI 消息：鲸鱼头像在左，气泡在右
        icon = QLabel()
        icon.setPixmap(_try_pm(["whale_40.png", "whale_36.png", "whale_32.png"], 40))
        icon.setAlignment(Qt.AlignTop | Qt.AlignCenter)
        icon.setFixedSize(40, 40)
        icon.setStyleSheet("background:transparent;")
        root.addWidget(icon, 0, Qt.AlignTop)

        card = QFrame()
        card.setStyleSheet(
            "background:" + COLORS['CARD'] + ";"
            "border:none;"
            "border-radius:" + RADII['lg'] + ";"
        )
        card.setMaximumWidth(max_width)

        cl = QVBoxLayout(card)
        cl.setContentsMargins(12, 10, 12, 12)
        cl.setSpacing(6)

        head = QHBoxLayout()
        lbl = QLabel("📷 图片")
        lbl.setStyleSheet(
            "color:" + COLORS['TEXT'] + ";font-size:14px;font-weight:" + str(FONT_WEIGHTS['semibold']) + ";"
            "font-family:" + FONT_STACK + ";background:transparent;"
        )
        head.addWidget(lbl, 1)

        # 显示图（可点击放大预览）
        self._full_pixmap = pixmap  # 保存原始pixmap用于预览
        if pixmap and not pixmap.isNull():
            scaled = pixmap.scaled(max_width, 500, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            img_lbl = ClickableImageLabel(scaled)
            img_lbl.setStyleSheet(
                "background:transparent;border-radius:10px;"
            )
            img_lbl.clicked.connect(lambda: _show_image_preview(self._full_pixmap, self))
        else:
            img_lbl = QLabel("(无法解码图片)")
            img_lbl.setStyleSheet(
                "background:transparent;border-radius:10px;"
            )
        img_lbl.setAlignment(Qt.AlignCenter)
        cl.addWidget(img_lbl)

        root.addWidget(card, 1, Qt.AlignLeft)

    @staticmethod
    def _parse_data_url(data_url: str):
        try:
            if not data_url or not data_url.startswith("data:"):
                return QPixmap()
            # data:image/png;base64,xxxx
            header, payload = data_url.split(",", 1)
            if "base64" in header:
                import base64
                raw = base64.b64decode(payload.encode('ascii'))
                pm = QPixmap()
                pm.loadFromData(raw)
                return pm
        except Exception:
            pass
        return QPixmap()

# ======================================================================
# UserAttachmentBubble — 用户附件气泡（图片+文件，右侧"我"头像，蓝色气泡）
# ======================================================================
class UserAttachmentBubble(QFrame):
    """用户发送的附件气泡：支持多张图片（网格布局）+ 多个文件（列表布局）。
    与 UserBubble 一致的右对齐 + "我"头像 + 品牌蓝背景。
    """

    def __init__(self, images=None, files=None, max_width: int = 420, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:transparent;")

        images = images or []
        files = files or []
        self._pixmaps = []  # 保存原始pixmaps用于点击预览

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        # 左侧弹性空间（推到右侧）
        root.addStretch(1)

        # 气泡容器（与 UserBubble 一致的不对称圆角风格）
        card = QFrame()
        card.setStyleSheet(
            "background:" + COLORS['PRIM'] + ";"
            "border:none;"
            "border-top-left-radius:" + RADII['bubble'] + ";"
            "border-top-right-radius:" + RADII['tail'] + ";"
            "border-bottom-left-radius:" + RADII['bubble'] + ";"
            "border-bottom-right-radius:" + RADII['bubble'] + ";"
        )
        card.setMaximumWidth(max_width)
        card.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)

        cl = QVBoxLayout(card)
        # 图片附件用稍紧凑的内边距，但仍与文字气泡风格对齐
        cl.setContentsMargins(10, 10, 10, 10)
        cl.setSpacing(6)

        # --- 图片区域 ---
        if images:
            img_grid = self._build_image_grid(images, max_width - 20)
            if img_grid is not None:
                cl.addLayout(img_grid)

        # --- 文件区域 ---
        if files:
            file_list = self._build_file_list(files, max_width - 20)
            if file_list is not None:
                if images:
                    # 分隔线
                    sep = QFrame()
                    sep.setFixedHeight(1)
                    sep.setStyleSheet(
                        "background:" + rgba(COLORS['CARD'], 0.25) + ";border:none;"
                    )
                    cl.addWidget(sep)
                cl.addLayout(file_list)

        root.addWidget(card, 0, Qt.AlignTop | Qt.AlignRight)

        # 右侧用户头像（与 UserBubble 一致）
        me_tag = QLabel("我")
        me_tag.setAlignment(Qt.AlignCenter)
        me_tag.setFixedSize(40, 40)
        me_tag.setStyleSheet(
            "background:" + COLORS['PRIM_DARK'] + ";"
            "color:" + COLORS['CARD'] + ";"
            "border-radius:" + RADII['circle'] + ";"
            "font-size:14px; font-weight:" + str(FONT_WEIGHTS['bold']) + ";"
            "font-family:" + FONT_STACK + ";"
        )
        root.addWidget(me_tag, 0, Qt.AlignTop)

    def _build_image_grid(self, images, container_width):
        """构建图片网格：1张图大尺寸，2张并排，3+张用2列网格。所有图片可点击放大预览。"""
        from PySide6.QtWidgets import QGridLayout
        n = len(images)
        if n == 0:
            return None

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)

        # 解析所有图片
        pixmaps = []
        for url in images:
            pm = ImageBubble._parse_data_url(url)
            if pm and not pm.isNull():
                pixmaps.append(pm)
            else:
                pixmaps.append(None)
        self._pixmaps = pixmaps

        def _make_img_label(full_pm, scaled_pm, radius="8px"):
            """创建可点击的图片标签。"""
            lbl = ClickableImageLabel(scaled_pm)
            lbl.setStyleSheet(f"background:transparent;border-radius:{radius};")
            lbl.setAlignment(Qt.AlignCenter)
            if full_pm and not full_pm.isNull():
                lbl.clicked.connect(lambda pm=full_pm: _show_image_preview(pm, self))
            return lbl

        if n == 1:
            # 单张图：大图展示，最长边不超过 400px
            pm = pixmaps[0]
            if pm:
                scaled = pm.scaled(container_width, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                img_lbl = _make_img_label(pm, scaled, RADII['xs'])
            else:
                img_lbl = QLabel("(无法解码图片)")
                img_lbl.setStyleSheet(
                    "color:" + COLORS['TEXT'] + ";font-size:13px;padding:20px;"
                    "font-family:" + FONT_STACK + ";background:transparent;"
                )
                img_lbl.setAlignment(Qt.AlignCenter)
            grid.addWidget(img_lbl, 0, 0)
        elif n == 2:
            # 两张并排：统一尺寸裁剪填充，保持网格整齐
            thumb_w = (container_width - 4) // 2
            thumb_h = 200
            for i, pm in enumerate(pixmaps):
                if pm:
                    scaled = pm.scaled(thumb_w, thumb_h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                    img_lbl = _make_img_label(pm, scaled, "6px")
                else:
                    img_lbl = QLabel("?")
                    img_lbl.setStyleSheet("background:transparent;border-radius:6px;color:" + rgba(COLORS['CARD'], 0.5) + ";")
                    img_lbl.setAlignment(Qt.AlignCenter)
                img_lbl.setFixedSize(thumb_w, thumb_h)
                grid.addWidget(img_lbl, 0, i)
        else:
            # 3+张：2列网格，每张统一 180x180 裁剪填充
            cols = 2
            thumb_w = (container_width - 4) // cols
            thumb_h = min(180, thumb_w)
            for i, pm in enumerate(pixmaps):
                r, c = divmod(i, cols)
                if pm:
                    scaled = pm.scaled(thumb_w, thumb_h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                    img_lbl = _make_img_label(pm, scaled, "6px")
                else:
                    img_lbl = QLabel("?")
                    img_lbl.setStyleSheet("background:transparent;border-radius:6px;color:" + rgba(COLORS['CARD'], 0.5) + ";")
                    img_lbl.setAlignment(Qt.AlignCenter)
                img_lbl.setFixedSize(thumb_w, thumb_h)
                grid.addWidget(img_lbl, r, c)

        return grid

    def _build_file_list(self, files, container_width):
        """构建文件列表：图标 + 文件名 + 大小。"""
        from PySide6.QtWidgets import QHBoxLayout
        if not files:
            return None

        vbox = QVBoxLayout()
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(3)

        for finfo in files:
            if isinstance(finfo, dict):
                name = finfo.get("name", "文件")
                size = finfo.get("size", 0)
            else:
                name = str(finfo)
                size = 0

            row = QHBoxLayout()
            row.setContentsMargins(4, 4, 4, 4)
            row.setSpacing(8)

            # 文件图标（用 emoji 简单标识）
            ext = os.path.splitext(name)[1].lower() if hasattr(os, 'path') else ""
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
            icon_lbl.setFixedSize(24, 24)
            icon_lbl.setStyleSheet("font-size:16px;background:transparent;")
            icon_lbl.setAlignment(Qt.AlignCenter)
            row.addWidget(icon_lbl)

            # 文件名
            name_lbl = QLabel(name)
            name_lbl.setStyleSheet(
                "color:" + COLORS['TEXT'] + ";font-size:12px;font-weight:" + str(FONT_WEIGHTS['medium']) + ";"
                "font-family:" + FONT_STACK + ";background:transparent;"
            )
            name_lbl.setWordWrap(False)
            name_lbl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            row.addWidget(name_lbl, 1)

            # 文件大小
            if size and isinstance(size, (int, float)) and size > 0:
                if size < 1024:
                    sz_text = f"{size} B"
                elif size < 1024 * 1024:
                    sz_text = f"{size/1024:.1f} KB"
                else:
                    sz_text = f"{size/1024/1024:.1f} MB"
                sz_lbl = QLabel(sz_text)
                sz_lbl.setStyleSheet(
                    "color:" + rgba(COLORS['TEXT'], 0.7) + ";font-size:11px;"
                    "font-family:" + FONT_STACK + ";background:transparent;"
                )
                row.addWidget(sz_lbl)

            vbox.addLayout(row)

        return vbox
