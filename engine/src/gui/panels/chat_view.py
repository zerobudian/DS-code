# -*- coding: utf-8 -*-
from __future__ import annotations

import math
import os
import logging
from typing import Dict, List, Optional

from PySide6.QtCore import (
    Qt, QSize, Signal, QPropertyAnimation, QEasingCurve, QTimer, QEvent,
    QPoint, Property,
)
from PySide6.QtGui import (
    QPixmap, QIcon, QColor, QPainter, QPen, QBrush, QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QFrame, QPushButton, QTextEdit, QScrollArea, QSplitter,
    QGraphicsOpacityEffect, QLineEdit, QSizePolicy, QMessageBox, QDialog,
)
try:
    from PySide6.QtWidgets import QScroller
except Exception:
    QScroller = None
from src.gui.style import COLORS, RADII, SPACING, SIZES, FONT_STACK, rgba, tr, build_stylesheet, ThemeManager

from src.gui._gui_helpers import enable_touch_scroll
from src.gui.bubbles import (
    UserBubble, AIBubble, ToolCallBubble, PlanBubble, TypingIndicator,
    WelcomeCard, StatsCard,
)


# ============================================================================
# 聊天视图（滚动区 + 气泡管理）
# ============================================================================
class ChatView(QScrollArea):
    """聊天主视图，负责：欢迎页 / 气泡 / 打字指示器 / 淡入动画"""

    sig_suggestion_clicked = Signal(str)
    sig_create_project = Signal()
    sig_open_project = Signal()
    regenerate_requested = Signal(int)  # AI 气泡索引，用于重新生成

    def __init__(self, parent=None, language: str = "zh"):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(self._scroll_style())

        container = QWidget()
        container.setStyleSheet(f"background:{COLORS['BGD']};")
        container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        container.setAttribute(Qt.WA_AcceptTouchEvents, True)
        self._lay = QVBoxLayout(container)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(SIZES["spacing"]["lg"])
        self._lay.addStretch(1)

        # viewport 直接 stylesheet 设背景，不依赖 palette
        self.viewport().setStyleSheet(f"background:{COLORS['BGD']};")

        self.setWidget(container)

        # 欢迎区 + 统计区（默认新对话模式：两者都显示）
        self.welcome = WelcomeCard(language=language)
        self.welcome.sig_suggestion_clicked.connect(self.sig_suggestion_clicked.emit)
        self.welcome.sig_create_project.connect(self.sig_create_project.emit)
        self.welcome.sig_open_project.connect(self.sig_open_project.emit)
        self._lay.insertWidget(0, self.welcome)
        self.welcome.setVisible(True)

        self.stats_card = StatsCard()
        self._lay.insertWidget(1, self.stats_card)

        self._typing = None
        self._current_ai = None
        self._current_tool = None
        self._active_tool_bubbles: Dict[str, ToolCallBubble] = {}
        self._language = language
        # 是否为"新对话模式"：True=显示欢迎+统计; False=已有历史，两者隐藏
        self._is_new_chat: bool = True
        # 智能自动滚动
        self._auto_scroll_enabled: bool = True
        self._prev_scroll_value: int = 0
        self._scroll_anim: Optional[QPropertyAnimation] = None
        self.verticalScrollBar().valueChanged.connect(self._on_scroll_value_changed)

        # 触控支持：启用触屏事件并开启滚动惯性
        self.setAttribute(Qt.WA_AcceptTouchEvents, True)
        if QScroller is not None:
            try:
                QScroller.grabGesture(self.viewport(), QScroller.TouchGesture)
                QScroller.grabGesture(self.viewport(), QScroller.LeftMouseButtonGesture)
            except Exception:
                pass

        # 订阅集中式主题变更信号
        try:
            ThemeManager.instance().theme_changed.connect(self._rebuild_styles)
        except Exception:
            pass

    def _scroll_style(self) -> str:
        return (
            "QScrollArea{background:" + COLORS['BGD'] + ";border:none;}"
            "QScrollBar:vertical{background:transparent;width:6px;margin:2px 0px;}"
            "QScrollBar::handle:vertical{background:" + COLORS['PRIM'] + ";"
            "border-radius:3px;min-height:36px;}"
            "QScrollBar::handle:vertical:hover{background:" + COLORS['PRIM_DARK'] + ";}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0px;}"
            "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}"
        )

    # ---- 新对话 / 历史对话模式切换 ----
    def load_history(self, messages):
        """根据历史消息列表渲染气泡。仅渲染 user/assistant 文本消息。"""
        self.clear_all()
        self.set_new_chat_mode(False)
        for msg in messages:
            try:
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                content = msg.get("content", "")
                # content 可能是 list（多模态格式），提取文本部分
                if isinstance(content, list):
                    text_parts = []
                    for p in content:
                        if isinstance(p, dict) and p.get("type") == "text":
                            text_parts.append(str(p.get("text", "")))
                        elif isinstance(p, str):
                            text_parts.append(p)
                    content = " ".join(text_parts)
                elif not isinstance(content, str):
                    content = str(content)
                if not content:
                    continue
                if role == "user":
                    self.add_user(content)
                elif role == "assistant":
                    b = self.add_ai(content)
                    if b is not None and hasattr(b, 'finalize'):
                        try:
                            b.finalize()
                        except Exception:
                            pass
            except Exception:
                import logging
                logging.getLogger(__name__).warning("加载历史消息失败: %s", msg.get('role','?') if isinstance(msg,dict) else str(msg)[:50])

    def set_new_chat_mode(self, is_new: bool) -> None:
        """设置当前对话模式。
        True  = 新对话，显示欢迎卡 + 统计卡
        False = 已有历史，两者都隐藏
        """
        self._is_new_chat = is_new
        if is_new:
            # 新对话：显示欢迎 + 统计
            if not self.welcome.isVisible():
                self.welcome.setVisible(True)
            # 简化为直接显示，不依赖 _fade_in（已移除以避免渲染冲突）
            if not self.stats_card.is_visible_safe():
                self.stats_card.show_smooth()
        else:
            # 历史对话：隐藏欢迎 + 统计
            if self.welcome.isVisible():
                self.welcome.setVisible(False)
            if self.stats_card.is_visible_safe():
                # stats_card.hide_smooth() 带淡入淡出，直接隐藏即可
                self.stats_card.setVisible(False)

    # ---- 用户开始输入 / 上传文件时调用 ----
    def hide_stats_if_visible(self) -> None:
        """如果统计卡片可见，将其隐藏（用户打字或上传时触发）。
        欢迎卡保持不动 — 它由 _hide_welcome_if_needed 在添加第一条气泡时处理。"""
        try:
            if self.stats_card.is_visible_safe():
                self.stats_card.hide_smooth()
        except Exception:
            pass

    def refresh_greeting(self, language: str = "zh"):
        """重新随机挑一条问候语（点击"新对话"按钮时调用）。
        点击"新对话" → 清空 → 切换到新对话模式（显示欢迎+统计）。"""
        self._language = language
        try:
            self.welcome.refresh(language)
        except Exception:
            pass
        # 清空后视为"新对话" — 同时显示统计卡
        self.set_new_chat_mode(True)

    # ---- 公共 API ----
    def add_user(self, text: str):
        self._hide_welcome_if_needed()
        b = UserBubble(text)
        self._insert(b)
        self.request_scroll_to_bottom()
        return b

    def add_ai(self, text: str = ""):
        try:
            self._hide_welcome_if_needed()
            b = AIBubble()
        except Exception:
            import traceback
            traceback.print_exc()
            return None
        # content_changed 信号连接可能抛异常，放在最先
        try:
            b.content_changed.connect(self.request_scroll_to_bottom)
        except Exception:
            pass
        try:
            b.regenerate_clicked.connect(lambda checked=False, bubble=b: self._on_bubble_regenerate(bubble))
        except Exception:
            pass
        if text:
            b.set_content(text)
        # 先设置 _current_ai，再 _insert —— 因为 _insert 会触发Qt事件处理，
        # 如果在线程环境中 content delta 先到达，_current_ai 必须已就绪
        self._current_ai = b
        self._insert(b)
        return b

    def _on_bubble_regenerate(self, bubble):
        """用户点击 AI 气泡的重新生成按钮。"""
        try:
            # 找到气泡在布局中的索引
            idx = -1
            for i in range(self._lay.count()):
                item = self._lay.itemAt(i)
                if item and item.widget() is bubble:
                    idx = i
                    break
            if idx >= 0:
                self.regenerate_requested.emit(idx)
        except Exception:
            pass

    def add_tool(self, tool_name: str, args_summary: str = ""):
        """添加工具调用气泡。tool_name 显示在头部，args_summary 显示在参数区域。"""
        self._hide_welcome_if_needed()
        b = ToolCallBubble(tool_name)
        if args_summary:
            b.set_args_summary(args_summary)
        self._insert(b)
        self._current_tool = b
        self._active_tool_bubbles[str(tool_name)] = b
        return b

    def add_file_preview(self, filepath: str, content: str):
        from src.gui.bubbles import FilePreviewBubble
        self._hide_welcome_if_needed()
        b = FilePreviewBubble(filepath, content)
        self._insert(b)
        return b

    def add_diff_preview(self, filepath: str, diff_text: str):
        from src.gui.bubbles import DiffPreviewBubble
        self._hide_welcome_if_needed()
        b = DiffPreviewBubble(filepath, diff_text)
        self._insert(b)
        return b

    def add_image(self, image_data_url: str, index: int = 0):
        from src.gui.bubbles import ImageBubble
        self._hide_welcome_if_needed()
        b = ImageBubble(image_data_url, index=index)
        self._insert(b)
        return b

    def add_user_attachments(self, images=None, files=None):
        """添加用户发送的附件气泡（图片+文件统一显示在一个气泡中）。"""
        from src.gui.bubbles import UserAttachmentBubble
        self._hide_welcome_if_needed()
        b = UserAttachmentBubble(images=images, files=files)
        self._insert(b)
        self.request_scroll_to_bottom()
        return b

    def add_streaming_command(self, command: str):
        from src.gui.bubbles import StreamingCommandBubble
        self._hide_welcome_if_needed()
        b = StreamingCommandBubble(command)
        self._insert(b)
        return b

    def add_status_line(self, text: str, status_type: str = "writing"):
        """追加或新建 StreamingStatusBubble，写入一行彩色状态文本"""
        from src.gui.bubbles import StreamingStatusBubble
        self._hide_welcome_if_needed()
        # 尝试复用最近一个 status 气泡
        last_bubble = None
        for i in range(self._lay.count() - 1, -1, -1):
            item = self._lay.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, StreamingStatusBubble):
                last_bubble = w
                break
        if last_bubble is None or (last_bubble._lines and len(last_bubble._lines) >= 200):
            last_bubble = StreamingStatusBubble()
            self._insert(last_bubble)
        last_bubble.append(text, status_type)
        return last_bubble

    def add_plan(self, plan_text: str) -> PlanBubble:
        """显示带按钮的计划气泡 — PlanBubble 自身带 confirm/reject 信号"""
        self._hide_welcome_if_needed()
        b = PlanBubble(plan_text or tr("no_plan_text"))
        self._insert(b)
        return b

    def clear_input(self):
        """清空输入框 — 委托给父级 MainWindow 的 input_area。"""
        parent = self.window()
        if parent and hasattr(parent, 'input_area'):
            parent.input_area.clear()

    def add_user_bubble(self, text: str):
        """兼容 app.py 的别名 — 等同 add_user。"""
        return self.add_user(text)

    def append_content(self, text: str):
        if not self._current_ai or not text:
            return
        try:
            self._current_ai.append_content(text)
        except RuntimeError:
            self._current_ai = None
            self.add_ai()
            if self._current_ai:
                try:
                    self._current_ai.append_content(text)
                except RuntimeError:
                    self._current_ai = None

    def append_reasoning(self, text: str):
        """向当前 AI 气泡追加推理内容。气泡无效时尝试恢复。"""
        if not self._current_ai or not text:
            if text and not self._current_ai:
                self.add_ai()
            if self._current_ai and text:
                try:
                    self._current_ai.append_reasoning(text)
                except RuntimeError:
                    self._current_ai = None
            return
        try:
            self._current_ai.append_reasoning(text)
        except RuntimeError:
            self._current_ai = None
            self.add_ai()
            if self._current_ai:
                try:
                    self._current_ai.append_reasoning(text)
                except RuntimeError:
                    self._current_ai = None

    def add_tool_call_bubble(self, tool_name: str, call: str = ""):
        """兼容 app.py 的别名 — call 作为工具参数摘要传给 add_tool。"""
        return self.add_tool(tool_name, call)

    def update_tool_call_bubble(self, tool_name: str, success: bool, result_summary: str = ""):
        """按工具名更新对应的工具调用气泡；若找不到则回退到最近一个。"""
        b = self._active_tool_bubbles.get(str(tool_name)) or self._current_tool
        if b:
            try:
                if hasattr(b, 'update_result'):
                    b.update_result(success, result_summary)
                elif hasattr(b, 'mark_done'):
                    b.mark_done(bool(success))
            except RuntimeError:
                if b is self._current_tool:
                    self._current_tool = None

    def add_system_message(self, snapshots):
        """添加系统消息气泡（显示快照/系统通知）。"""
        try:
            if isinstance(snapshots, list):
                text = "\n".join(str(s) for s in snapshots)
            else:
                text = str(snapshots)
            bubble = QLabel(text)
            bubble.setWordWrap(True)
            bubble.setStyleSheet(
                build_stylesheet(
                    "QLabel",
                    color=COLORS['TEXT_3'],
                    font_size="12px",
                    font_family=FONT_STACK,
                    padding="8px 16px",
                    background="transparent",
                )
            )
            bubble.setAlignment(Qt.AlignCenter)
            self._insert(bubble)
        except Exception:
            pass

    def update_tool_result(self, tool_name: str, summary: str = ""):
        """更新工具结果（兼容 app.py 调用）。"""
        self.update_tool_call_bubble(tool_name, True, summary)

    def add_plan_bubble(self, plan_text: str):
        """兼容 app.py 的别名 — 等同 add_plan。"""
        return self.add_plan(plan_text)

    def append_command_output(self, line: str):
        """向最近的流式命令气泡追加输出。"""
        if self._current_tool and hasattr(self._current_tool, 'append_output'):
            try:
                self._current_tool.append_output(line)
            except RuntimeError:
                pass

    def finish_current_bubble(self):
        """完成当前 AI 气泡。"""
        if self._current_ai:
            try:
                if hasattr(self._current_ai, 'finish'):
                    self._current_ai.finish()
                elif hasattr(self._current_ai, 'finish_streaming'):
                    self._current_ai.finish_streaming()
                elif hasattr(self._current_ai, 'finalize'):
                    self._current_ai.finalize()
            except RuntimeError:
                pass
            self._current_ai = None

    def add_error_bubble(self, msg: str):
        """添加错误提示气泡 — 带红色左边框以示区分。"""
        try:
            b = self.add_ai(msg)
            if b is not None and hasattr(b, '_bubble'):
                b._bubble.setStyleSheet(
                    b._bubble.styleSheet()
                    + f"border-left:3px solid {COLORS.get('DANGER', '#EF4444')};"
                )
            return b
        except Exception:
            pass

    def show_typing(self):
        if self._typing is None:
            self._typing = TypingIndicator()
            self._insert(self._typing)
        return self._typing

    def hide_typing(self):
        if self._typing:
            old = self._typing
            self._typing = None
            try:
                old.stop()
                old.setParent(None)
                old.deleteLater()
            except (RuntimeError, Exception):
                pass

    def clear_all(self):
        """清空所有气泡，但保留 welcome 和 stats_card。清空后视为"新对话"。"""
        self.hide_typing()
        self._current_ai = None
        self._current_tool = None
        self._active_tool_bubbles.clear()
        for i in range(self._lay.count() - 1, -1, -1):
            item = self._lay.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is None:
                continue
            # 保留欢迎卡 和 统计卡 — 清空后它们会由 set_new_chat_mode 重新显示
            if w is self.welcome:
                continue
            if w is self.stats_card:
                continue
            try:
                w.setParent(None)
                w.deleteLater()
            except (RuntimeError, Exception):
                pass
        # 清空后 = 新对话模式
        self.set_new_chat_mode(True)

    def _hide_welcome_if_needed(self):
        """添加第一个气泡时：切换到历史对话模式，同时隐藏欢迎卡 + 统计卡。"""
        # 统一通过 set_new_chat_mode(False) 隐藏，避免 fade_out 动画失败导致统计卡残留
        if self._is_new_chat:
            self.set_new_chat_mode(False)
        else:
            # 兜底：确保两者均不可见
            if self.welcome.isVisible():
                self.welcome.setVisible(False)
            if self.stats_card.isVisible():
                self.stats_card.setVisible(False)

    # ---- 内部 ----
    def _insert(self, widget):
        # 折叠旧推理块与工具调用气泡（新消息到来时），但保留最近一条 AI 回答
        try:
            from src.gui.bubbles import AIBubble, ReasoningBubble, ToolCallBubble
            last_ai_bubble = None
            for i in range(self._lay.count() - 1, -1, -1):
                item = self._lay.itemAt(i)
                if item and item.widget() and isinstance(item.widget(), AIBubble):
                    last_ai_bubble = item.widget()
                    break
            for rb in self.findChildren(ReasoningBubble):
                # 跳过最近一条 AI 气泡内的推理块
                if last_ai_bubble and last_ai_bubble.isAncestorOf(rb):
                    continue
                try:
                    if rb.is_expanded():
                        rb.collapse(animate=True)
                except Exception:
                    pass
            for tb in self.findChildren(ToolCallBubble):
                if last_ai_bubble and last_ai_bubble.isAncestorOf(tb):
                    continue
                try:
                    if tb.is_expanded():
                        tb.collapse(animate=True)
                except Exception:
                    pass
        except Exception:
            pass

        pos = self._lay.count() - 1
        if pos < 0:
            pos = 0
        self._lay.insertWidget(pos, widget, 0, Qt.AlignTop)
        QTimer.singleShot(50, self._smooth_scroll)

    def _smooth_scroll(self):
        """平滑滚动到底部；如果用户已主动上滑，则停止自动滚动。"""
        vsb = self.verticalScrollBar()
        # 关键：尊重用户滚动意图
        if not self._auto_scroll_enabled:
            return
        try:
            # 停止任何正在运行的旧动画，避免动画叠加卡顿
            if self._scroll_anim is not None:
                try:
                    self._scroll_anim.stop()
                except Exception:
                    pass
            self._scroll_anim = QPropertyAnimation(vsb, b"value", self)
            self._scroll_anim.setDuration(220)
            self._scroll_anim.setStartValue(vsb.value())
            self._scroll_anim.setEndValue(vsb.maximum())
            self._scroll_anim.setEasingCurve(QEasingCurve.OutCubic)
            self._scroll_anim.finished.connect(
                lambda: (
                    setattr(self, '_scroll_anim', None)
                )
            )
            self._scroll_anim.start(QPropertyAnimation.DeleteWhenStopped)
        except Exception:
            vsb.setValue(vsb.maximum())

    # ---- 智能自动滚动 ----
    def _on_scroll_value_changed(self, value: int) -> None:
        """检测用户是否主动上滑来决定是否继续自动滚动。"""
        bar = self.verticalScrollBar()
        max_v = bar.maximum()
        # 触底：恢复自动滚动
        if value >= max_v - 5:
            self._auto_scroll_enabled = True
        # 主动上滑：停止自动滚动
        elif self._prev_scroll_value - value > 20:
            self._auto_scroll_enabled = False
        self._prev_scroll_value = value

    def request_scroll_to_bottom(self) -> None:
        """请求平滑滚动到底部（仅在启用自动滚动时执行）。"""
        if not self._auto_scroll_enabled:
            return
        bar = self.verticalScrollBar()
        target = bar.maximum()

        # 停止旧动画
        if self._scroll_anim is not None:
            try:
                self._scroll_anim.stop()
            except Exception:
                pass

        self._scroll_anim = QPropertyAnimation(bar, b"value", self)
        self._scroll_anim.setDuration(220)
        self._scroll_anim.setStartValue(bar.value())
        self._scroll_anim.setEndValue(target)
        self._scroll_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._scroll_anim.finished.connect(lambda: self._cleanup_scroll_anim())
        self._scroll_anim.start(QPropertyAnimation.DeleteWhenStopped)

    def _cleanup_scroll_anim(self) -> None:
        self._scroll_anim = None

    def enable_auto_scroll(self, enabled: bool = True) -> None:
        """外部可调用：恢复/禁用自动滚动。"""
        self._auto_scroll_enabled = enabled

    def _rebuild_styles(self, theme: str = None):
        """主题切换后重建聊天视图中所有气泡与卡片的样式。"""
        try:
            self.setStyleSheet(self._scroll_style())
            # viewport + container 背景直接 stylesheet 刷新
            try:
                self.viewport().setStyleSheet(f"background:{COLORS['BGD']};")
            except Exception:
                pass
            try:
                w = self.widget()
                if w is not None:
                    w.setStyleSheet(f"background:{COLORS['BGD']};")
            except Exception:
                pass
            # 通用方法：遍历所有子 widget，调用 rebuild_style 如果存在
            for i in range(self._lay.count()):
                item = self._lay.itemAt(i)
                if not item:
                    continue
                w = item.widget()
                if w is not None and hasattr(w, 'rebuild_style'):
                    try:
                        w.rebuild_style()
                    except Exception:
                        pass
            for card in (self.welcome, self.stats_card):
                if hasattr(card, 'rebuild_style'):
                    try:
                        card.rebuild_style()
                    except Exception:
                        pass
        except Exception:
            pass

