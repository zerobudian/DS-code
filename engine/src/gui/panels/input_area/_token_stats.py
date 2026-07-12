# -*- coding: utf-8 -*-
"""InputArea 子模块：实时 token 统计与瞬时速率显示。

负责：
  - 流式输出期间的 token 累加
  - 每秒刷新一次瞬时速率（token/s）与累计总数
  - 兼容旧版 ``update_token_stats`` 直接设置接口

混入 ``InputArea`` 后通过 ``_init_token_stats(outer_layout)`` 创建对应控件，
并对外提供 ``show_token_stats`` / ``hide_token_stats`` / ``add_stream_delta``
/ ``update_token_stats`` 公共方法。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel, QVBoxLayout

from src.gui.style import COLORS, FONT_STACK, tr


class TokenStatsMixin:
    """token 统计行 + 每秒刷新定时器。"""

    # ------------------------------------------------------------------
    # 控件初始化（由 InputArea.__init__ 调用）
    # ------------------------------------------------------------------
    def _init_token_stats(self, outer_layout: QVBoxLayout) -> None:
        """创建 stats_label 与定时器，并把 stats_label 加入 outer_layout。"""
        # 实时 token 统计（请求中显示，结束后隐藏）
        self.stats_label = QLabel()
        self.stats_label.setVisible(False)
        self.stats_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.stats_label.setStyleSheet(
            "QLabel{color:" + COLORS['TEXT_3'] + ";"
            "font-size:11px;font-weight:500;font-family:" + FONT_STACK + ";"
            "letter-spacing:0.3px;padding:0px 4px;}"
        )
        outer_layout.addWidget(self.stats_label)

        # ---- token 速率定时器（每秒刷新瞬时速率）----
        self._stream_total_tokens = 0
        self._stream_window_tokens = 0   # 最近1秒窗口内的token数
        self._stream_active = False
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(1000)  # 每秒刷新
        self._stats_timer.timeout.connect(self._on_stats_tick)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------
    def show_token_stats(self) -> None:
        """显示 token 统计行并启动每秒刷新定时器。"""
        try:
            self._stream_total_tokens = 0
            self._stream_window_tokens = 0
            self._stream_active = True
            self.stats_label.setVisible(True)
            self.stats_label.setText(tr("token_stats_fmt", rate=0, total=0))
            self._stats_timer.start()
        except Exception:
            pass

    def hide_token_stats(self) -> None:
        """隐藏 token 统计行并停止定时器。"""
        try:
            self._stream_active = False
            self._stats_timer.stop()
            self.stats_label.setVisible(False)
            self.stats_label.setText("")
        except Exception:
            pass

    def add_stream_delta(self, tokens: int) -> None:
        """流式输出增量到来时调用（由 app.py 触发），累加 token 计数。"""
        try:
            if not self._stream_active:
                return
            self._stream_total_tokens += max(0, tokens)
            self._stream_window_tokens += max(0, tokens)
        except Exception:
            pass

    def update_token_stats(self, tokens_per_sec: float, total_tokens: int) -> None:
        """兼容旧接口：直接设置速率和总数（供非流式场景使用）。"""
        try:
            if tokens_per_sec < 0:
                tokens_per_sec = 0.0
            if total_tokens < 0:
                total_tokens = 0
            self.stats_label.setText(
                tr("token_stats_fmt", rate=int(tokens_per_sec), total=total_tokens)
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 内部回调
    # ------------------------------------------------------------------
    def _on_stats_tick(self) -> None:
        """每秒定时器：用最近1秒窗口的token数作为瞬时速率。"""
        try:
            if not self._stream_active:
                return
            rate = self._stream_window_tokens  # 最近1秒的token数 = token/s
            total = self._stream_total_tokens
            self._stream_window_tokens = 0  # 重置窗口计数
            self.stats_label.setText(tr("token_stats_fmt", rate=rate, total=total))
        except Exception:
            pass
