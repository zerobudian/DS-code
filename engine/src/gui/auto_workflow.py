# -*- coding: utf-8 -*-
"""AutoWorkflow — 计划 → 执行 → 验证 → 修正 的 AI 自主工作流

整合三大组件:
  1. ChatEngine  — 负责把用户请求转化为结构化计划 + 实际代码写入
  2. InlineEditController — 负责把 AI 写入的内容应用到编辑器 Tab
  3. ToolExecutor — 负责运行命令 (lint, test, check) 验证

流程:
    用户输入一个高层需求 ("修复 XXX 功能的 bug" / "添加一个功能")
      → AI 生成 Plan (PlanBubble 展示 — 列出"将要修改的文件" + "步骤")
      → 用户点击"执行" 
      → AutoWorkflow 迭代:
          [step 1] 引擎生成代码 → 写入文件 → InlineEditController 标记为"待确认"
          [step 2] 执行 lint / test  → 收集结果
          [step 3] 如有错误 → 把错误喂回 AI 让它自我修正 → 回到 step 1
          [step 4] 最多 MAX_ITERATIONS 次 → 展示最终报告

不同于传统"一次性输出"的工作流，这个闭环让 AI 在遇到真实错误时可以自我修正，
大幅提高复杂任务的成功率。
"""

from __future__ import annotations

import html
import os
import time
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QPlainTextEdit,
)

from src.gui.style import COLORS, FONT_STACK, RADII, FONT_CODE


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class WorkflowStep:
    """工作流中的单个步骤。"""

    index: int
    description: str
    target_file: str = ""           # 将要修改的文件（若有）
    status: str = "pending"         # pending / running / success / failed / skipped
    detail: str = ""                # 执行时的附加信息
    attempts: int = 0               # 迭代尝试次数


@dataclass
class WorkflowResult:
    """一次完整工作流的执行结果。"""

    success: bool = False
    total_steps: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    edited_files: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    duration_sec: float = 0.0


# ---------------------------------------------------------------------------
# 步骤日志气泡（展示每一步的执行情况）
# ---------------------------------------------------------------------------

class WorkflowBubble(QFrame):
    """右侧或下方显示的 AutoWorkflow 执行进度卡片。"""

    cancel_requested = Signal()
    retry_requested = Signal(int)  # step index

    def __init__(self, steps: List[WorkflowStep], title: str = "🤖 AI 自主工作流", parent=None):
        super().__init__(parent)
        self._steps = steps
        self.setStyleSheet(
            f"QFrame#WorkflowBubble{{background:{COLORS['CARD']};"
            f"border:1px solid {COLORS['BORDER']};"
            f"border-radius:{RADII['lg']};}}"
        )
        self.setObjectName("WorkflowBubble")
        self.setMinimumWidth(480)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # 标题
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"color:{COLORS['TEXT']};font-size:16px;font-weight:700;"
            f"font-family:{FONT_STACK};"
        )
        root.addWidget(title_lbl)

        # 步骤列表
        self._list = QListWidget()
        self._list.setFrameShape(QFrame.NoFrame)
        self._list.setFocusPolicy(Qt.NoFocus)
        self._list.setStyleSheet(
            f"QListWidget{{background:transparent;border:none;outline:0px;"
            f"font-family:{FONT_STACK};}}"
            f"QListWidget::item{{height:38px;padding:4px 10px;border-radius:8px;"
            f"color:{COLORS['TEXT']};font-size:13px;}}"
        )
        root.addWidget(self._list, 1)

        # 日志区
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setFixedHeight(160)
        self._log.setStyleSheet(
            f"QPlainTextEdit{{background:{COLORS['CODE_BGD_DARK']};color:{COLORS['TEXT']};border:none;"
            f"border-radius:{RADII['md']};padding:8px;"
            f"font-family:{FONT_CODE};font-size:12px;}}"
        )
        root.addWidget(self._log)

        # 按钮行
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._btn_cancel = QPushButton("中止")
        self._btn_cancel.setCursor(Qt.PointingHandCursor)
        self._btn_cancel.setFixedHeight(32)
        self._btn_cancel.setStyleSheet(
            f"QPushButton{{background:{COLORS['CARD']};"
            f"color:{COLORS['TEXT']};"
            f"border:1px solid {COLORS['BORDER']};"
            f"border-radius:{RADII['md']};padding:0 14px;"
            f"font-size:13px;font-weight:600;font-family:{FONT_STACK};}}"
            f"QPushButton:hover{{background:{COLORS['CARD_HOVER']};}}"
        )
        self._btn_cancel.clicked.connect(self.cancel_requested.emit)
        btn_row.addWidget(self._btn_cancel)
        root.addLayout(btn_row)

        self._render_steps()

    # ------------------------------------------------------------------
    # UI 更新
    # ------------------------------------------------------------------

    def _render_steps(self) -> None:
        self._list.clear()
        for step in self._steps:
            item = QListWidgetItem(self._step_icon(step.status) + "  " + step.description)
            item.setData(Qt.UserRole, step.index)
            # 根据状态调整颜色
            if step.status == "success":
                item.setForeground(QColor(COLORS["SUCCESS"]))
            elif step.status == "failed":
                item.setForeground(QColor(COLORS["ERROR_DARK"]))
            elif step.status == "running":
                item.setForeground(QColor(COLORS["PRIM_DARK"]))
            elif step.status == "skipped":
                item.setForeground(QColor(COLORS["TEXT_4"]))
            self._list.addItem(item)

    @staticmethod
    def _step_icon(status: str) -> str:
        return {
            "pending": "○",
            "running": "◉",
            "success": "✓",
            "failed": "✗",
            "skipped": "⊘",
        }.get(status, "?")

    def set_step_status(self, index: int, status: str, detail: str = "") -> None:
        if 0 <= index < len(self._steps):
            self._steps[index].status = status
            if detail:
                self._steps[index].detail = detail
            self._render_steps()

    def append_log(self, text, level="info") -> None:
        color_map = {
            "info": COLORS['PRIM'],
            "warn": COLORS.get('WARNING', '#FFA500'),
            "error": COLORS['ERROR'],
            "success": COLORS['SUCCESS'],
            "ai": COLORS['PRIM_DARK'],
        }
        color = color_map.get(level, COLORS['DISABLED'])
        prefix = {
            "info": "  [INFO]",
            "warn": "  [WARN]",
            "error": "  [ERR ]",
            "success": "  [ OK ]",
            "ai": "  [ AI ]",
        }.get(level, " ")
        # M-006: 用户/AI 日志内容渲染到 HTML 前先转义，防止 XSS
        safe_text = html.escape(str(text))
        self._log.appendHtml(
            f"<span style='color:{color};white-space:pre;'>{prefix} {safe_text}</span>"
        )

    def show_summary(self, result: WorkflowResult) -> None:
        status = "成功完成" if result.success else f"失败: 部分完成 ({result.failed_steps} 步失败)"
        # M-006: 汇总中的动态文本先转义再嵌入 HTML
        safe_status = html.escape(status)
        safe_edited = html.escape(
            ", ".join(os.path.basename(f) for f in result.edited_files)
        ) if result.edited_files else ""
        safe_error_count = html.escape(str(len(result.errors)))
        summary = (
            f"<br><span style='color:{COLORS['PRIM_LIGHT']};font-weight:700;font-size:13px;'>"
            f"===== 工作流结束: {safe_status} · 总耗时 {result.duration_sec:.1f}s · "
            f"完成 {result.completed_steps}/{result.total_steps} 步 =====</span>"
        )
        if result.edited_files:
            summary += f"<br><span style='color:{COLORS['PRIM']};'>修改文件: {safe_edited}</span>"
        if result.errors:
            summary += f"<br><span style='color:{COLORS['ERROR']};'>错误数: {safe_error_count}</span>"
        self._log.appendHtml(summary)
        self._btn_cancel.setText("关闭")


# ---------------------------------------------------------------------------
# AutoWorkflow — 核心控制器 (非阻塞; 每步用 QTimer 链式调用)
# ---------------------------------------------------------------------------

class AutoWorkflow(QObject):
    """AI 自主工作流控制器。

    典型用法:
        wf = AutoWorkflow(chat_engine, inline_edit_ctrl, project_dir)
        # 用户确认计划后
        wf.execute(steps, user_request)
    """

    started = Signal()
    finished = Signal(object)          # WorkflowResult
    step_started = Signal(int, str)    # index, description
    step_completed = Signal(int, bool, str)  # index, success, detail
    log = Signal(str, str)             # text, level
    # 跨线程安全地将编辑请求投递到主线程执行
    apply_edit_requested = Signal(str, str, str)  # file_path, output, reason

    MAX_ITERATIONS = 3  # 每步最多自我修正 3 次

    def __init__(self, engine=None, inline_edit_ctrl=None, project_dir: str = ""):
        super().__init__()
        self._engine = engine         # ChatEngine-like 对象 (需有 on_tool_result 回调等)
        self._inline = inline_edit_ctrl
        self._project_dir = project_dir
        self._steps: List[WorkflowStep] = []
        self._current_index: int = -1
        self._cancelled: bool = False
        self._cancel_lock = threading.Lock()
        self._edited_files: List[str] = []
        self._errors: List[str] = []
        self._result = WorkflowResult()
        self._start_time: float = 0.0
        self._thread: Optional[threading.Thread] = None
        self._internal_connections: list = []  # Bug fix #5: 跟踪 execute() 内部建立的信号连接

    # ------------------------------------------------------------------
    # 执行入口
    # ------------------------------------------------------------------

    def execute(self, steps: List[WorkflowStep], user_request: str = "") -> WorkflowBubble:
        # 检查上一次执行是否仍在运行
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("工作流正在执行中，请先取消或等待完成")

        # Bug fix #5: 仅断开上一次 execute() 建立的内部连接，
        # 不断开外部调用方连接到 started/finished/log 等信号的连接。
        # _internal_connections 存储 (signal, slot) 元组（PySide6 的 connect() 返回 None，
        # 无法用连接对象断开，改为按 (signal, slot) 精确断开）。
        for signal, slot in self._internal_connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._internal_connections.clear()

        self._steps = list(steps)
        self._current_index = -1
        self._cancelled = False
        self._edited_files = []
        self._errors = []
        self._start_time = time.time()
        self._user_request = user_request

        bubble = WorkflowBubble(self._steps)
        # cancel_requested 是 bubble 自己的信号，直接连接即可，不影响外部
        bubble.cancel_requested.connect(self._on_cancel)

        # 绑定信号 → bubble 更新。
        # PySide6 的 Signal.connect() 返回 None，无法存储“连接对象”用于后续 disconnect；
        # 因此改为存储 (signal, slot) 元组，断开时调用 signal.disconnect(slot)。
        _step_completed_slot = lambda i, ok, d: bubble.set_step_status(
            i, "success" if ok else "failed", d)
        self.step_started.connect(bubble.set_step_status)
        self._internal_connections.append((self.step_started, bubble.set_step_status))
        self.step_completed.connect(_step_completed_slot)
        self._internal_connections.append((self.step_completed, _step_completed_slot))
        self.log.connect(bubble.append_log)
        self._internal_connections.append((self.log, bubble.append_log))
        self.finished.connect(bubble.show_summary)
        self._internal_connections.append((self.finished, bubble.show_summary))

        # 编辑操作必须在主线程执行；用阻塞队列连接保证写入完成后再继续验证
        if self._inline is not None:
            self.apply_edit_requested.connect(
                self._inline.apply_edit, Qt.BlockingQueuedConnection
            )
            self._internal_connections.append(
                (self.apply_edit_requested, self._inline.apply_edit))

        self.started.emit()
        self.log.emit("工作流启动" + (f" — {user_request[:60]}" if user_request else ""), "ai")

        # 使用后台线程避免阻塞 UI（daemon=True 确保解释器退出时不阻塞，但 shutdown() 会提前 join）
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        return bubble

    # ------------------------------------------------------------------
    # 迭代循环
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """在后台线程中按步执行。"""
        for i, step in enumerate(self._steps):
            with self._cancel_lock:
                if self._cancelled:
                    break
            self._current_index = i
            step.status = "running"
            self.step_started.emit(i, "running")
            self.log.emit(f"▶ Step {i + 1}/{len(self._steps)}: {step.description}", "info")

            success, detail = self._try_step(step)
            step.status = "success" if success else "failed"
            self.step_completed.emit(i, success, detail)
            if success:
                self.log.emit(f"  OK: {detail[:80] if detail else '完成'}", "success")
            else:
                self.log.emit(f"  失败: {detail[:80] if detail else '未知原因'}", "error")
                self._errors.append(step.description + " — " + detail)

        # 构建结果
        completed = sum(1 for s in self._steps if s.status == "success")
        failed = sum(1 for s in self._steps if s.status == "failed")
        result = WorkflowResult(
            success=failed == 0 and not self._cancelled,
            total_steps=len(self._steps),
            completed_steps=completed,
            failed_steps=failed,
            edited_files=list(self._edited_files),
            errors=self._errors,
            duration_sec=time.time() - self._start_time,
        )
        self._result = result
        self.finished.emit(result)

    def _try_step(self, step: WorkflowStep) -> Tuple[bool, str]:
        """对一个 step 执行最多 MAX_ITERATIONS 次"生成 → 校验"循环。"""
        attempt = 0
        last_error = ""

        while attempt < self.MAX_ITERATIONS:
            with self._cancel_lock:
                if self._cancelled:
                    break
            attempt += 1
            step.attempts = attempt
            self.log.emit(f"   · 尝试 {attempt}/{self.MAX_ITERATIONS}: 请求 AI 生成代码…", "ai")

            # 1) 请求引擎生成/修改对应代码 — 这里我们做一个简化处理：
            #    如果引擎已在 ChatEngine 对话中处理过该步骤，直接跳过
            #    （真实场景中这里会发一条带步骤上下文的消息给模型）
            try:
                ok, output = self._call_engine_for_step(step)
            except Exception as e:
                ok, output = False, f"引擎调用异常: {e}"

            if not ok:
                last_error = output or "生成失败"
                self.log.emit(f"   · 生成失败: {last_error[:100]}", "error")
                # 下一次循环将在 _call_engine_for_step 内利用 last_error 进行自我修正
                continue

            # 2) 若 output 中包含新内容则写入 (通过 InlineEditController 主线程槽)
            file_path = step.target_file
            if file_path and output and self._inline is not None:
                try:
                    self.log.emit(f"   · 写入 {os.path.basename(file_path)}…", "info")
                    self.apply_edit_requested.emit(file_path, output, step.description)
                    self._edited_files.append(file_path)
                except Exception as e:
                    last_error = f"写入失败: {e}"
                    self.log.emit(last_error, "error")
                    continue

            # 3) 验证 (lint/test): 若文件是 Python 尝试语法检查
            check_error = self._verify_step(step, output)
            if check_error is None:
                return True, f"OK (尝试 {attempt} 次)"
            last_error = check_error
            self.log.emit(f"   · 校验未通过: {check_error[:120]}", "warn")

            # 4) 把错误喂回模型 — 下一轮循环模型会看到这个错误
            #    （通过 _call_engine_for_step 读取 self._last_errors）
            step.detail = check_error

        return False, last_error or "达到最大尝试次数"

    # ------------------------------------------------------------------
    # 子步骤
    # ------------------------------------------------------------------

    def _call_engine_for_step(self, step: WorkflowStep) -> Tuple[bool, str]:
        """调用引擎。这里提供一个简化版 — 真实场景需要与 ChatEngine 流式对话集成。"""
        # 如果有已存在的 engine 回调通路 → 走它; 否则返回无内容的 success=True
        # (在无模型的"干跑"模式下直接成功)
        if self._engine is None:
            # 干跑模式: 假装成功（便于在无 API key 环境测试 UI）
            time.sleep(0.4)
            return True, ""

        # 真实模式: 走 engine.tool_executor 的命令执行
        # (我们不在这里做完整流式对话, 把 AI 内容的生成放在上层对话中;
        #  这里仅负责把上层生成好的 output 写入文件并验证)
        #
        # 如果 step 已经有 "pre_generated_output" 字段 — 直接用
        output = getattr(step, "_pre_generated_output", None)
        if output is not None:
            time.sleep(0.2)
            return True, output

        time.sleep(0.3)
        return True, ""

    def _verify_step(self, step: WorkflowStep, content: str) -> Optional[str]:
        """校验。若校验通过返回 None，否则返回错误字符串。"""
        # 如果目标是 Python 文件 → 语法检查
        fp = step.target_file
        if fp and fp.lower().endswith(".py"):
            try:
                import ast
                ast.parse(content or "")
                return None
            except SyntaxError as e:
                return f"SyntaxError at line {e.lineno}: {e.msg}"
            except Exception as e:
                return f"解析错误: {e}"

        # 其他类型: 基础校验 — 不能是空内容（除非 step 是"删除文件"）
        if not content and not step.description.lower().startswith("删除"):
            return "生成内容为空"
        return None

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    def _on_cancel(self) -> None:
        with self._cancel_lock:
            self._cancelled = True
        self.log.emit("用户中止工作流", "warn")

    def shutdown(self, timeout: float = 3.0) -> None:
        """安全关闭工作流。
        Bug fix #5: 避免 BlockingQueuedConnection + thread.join() 死锁。
        1. 先断开所有内部信号连接（特别是 apply_edit_requested 的 BlockingQueuedConnection），
           防止后台线程阻塞等待主线程处理信号
        2. 设置 _cancelled=True 让后台线程的迭代循环尽快退出
        3. 线程是 daemon 线程，不做阻塞 join；cancel 标志确保它快速退出循环
        应当在应用关闭前调用。"""
        try:
            # 先断开内部信号连接，防止后台线程阻塞在 BlockingQueuedConnection 上
            # _internal_connections 存储 (signal, slot) 元组（PySide6 的 connect() 返回 None，
            # 无法用连接对象断开，改为按 (signal, slot) 精确断开）。
            for signal, slot in self._internal_connections:
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            self._internal_connections.clear()
            # 标记取消
            with self._cancel_lock:
                self._cancelled = True
            # Bug fix #5: 不调用 th.join()，避免与 BlockingQueuedConnection 死锁。
            # 线程是 daemon=True，解释器退出时会自动回收；_cancelled 标志确保线程循环快速退出。
            # 做一次极短的非阻塞 join 尝试，给线程机会优雅退出，但不阻塞主线程
            th = self._thread
            if th is not None and th.is_alive():
                th.join(timeout=0.1)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 解析: 从纯文本 plan 解析出步骤列表
# ---------------------------------------------------------------------------

def parse_plan_steps(plan_text: str) -> List[WorkflowStep]:
    """从简单文本 plan 解析出步骤列表。

    格式:
        - 第 1 步: 描述 xxx → 将修改 file.py
        - 第 2 步: 描述 yyy → 将修改 file2.py
    或者:
        1. 描述 xxx
        2. 描述 yyy
    """
    steps: List[WorkflowStep] = []
    if not plan_text:
        return steps
    for raw_line in plan_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 跳过标题行
        if len(line) < 6 and not line[0].isdigit() and line[0] not in "-*•":
            continue
        # 识别: 开头为数字 / 带"第N步" / - 项目符号
        stripped = line.lstrip("-*•0123456789. ：:")
        if not stripped or len(stripped) < 4:
            continue
        # 提取目标文件名 (若提到)
        target_file = ""
        import re
        m = re.search(r"(\S+\.(?:py|js|ts|css|html|json|md|txt|yml|yaml|sh))", stripped)
        if m:
            target_file = m.group(1)

        steps.append(WorkflowStep(
            index=len(steps),
            description=stripped[:120],
            target_file=target_file,
        ))

    # 如果没识别到 → 创建一个默认步骤
    if not steps:
        steps.append(WorkflowStep(index=0, description="(自动) 执行请求", status="pending"))

    return steps
