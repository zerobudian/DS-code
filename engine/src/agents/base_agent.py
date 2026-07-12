# -*- coding: utf-8 -*-
"""代理基类 — 所有角色代理的共同基础

核心抽象:
  AgentContext  — 共享上下文（项目目录 / 计划 / 已改动文件 / 工具执行结果）
  Agent         — 所有角色代理的公共父类
"""
from __future__ import annotations
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Any


# ============================================================
# 共享模式：报告失败判定正则（被 coordinator 和 planner 共用）
# 在 base_agent 中定义以避免循环导入
# ============================================================
_NEGATION_PATTERNS = [
    re.compile(r"\bno\s+errors?\b"),
    re.compile(r"\bno\s+fail(?:ures?|ed)?\b"),
    re.compile(r"\berror[- ]?free\b"),
    re.compile(r"\bwithout\s+errors?\b"),
    re.compile(r"\b0\s+errors?\b"),
    re.compile(r"\b0\s+fail(?:ed|ures?)?\b"),
    re.compile(r"\bnot\s+failed\b"),
]
_ZERO_FAIL_RE = re.compile(r"\b0\s+fail(?:ed|ures?)?\b")


class AgentContext:
    """代理共享上下文 — 像一块共享白板

    所有代理都读写这个对象；它是协作的媒介。
    """

    def __init__(self, project_dir):
        self.project_dir = Path(project_dir)
        # 当前计划 (可被 Planner 多次修订)
        self.plan: Optional[Plan] = None
        # 已改动文件列表 (Engineer 写入, Reviewer/Tester 读取)
        self.changed_files: List[str] = []
        # 工具执行历史 (name -> result)
        self.tool_results: List[Dict[str, Any]] = []
        # 测试 / 验证报告
        self.validation_report: Optional[str] = None
        # 测试报告（独立于 Reviewer 的 validation_report，避免覆盖）
        self.test_report: Optional[str] = None
        # 评审意见
        self.review_notes: List[str] = []
        # 对话日志 (用于调试)
        self.log: List[str] = []
        # 循环保护 (避免无限迭代)
        self.iteration: int = 0
        self.max_iterations: int = 10

    def log_event(self, agent_name: str, message: str):
        ts = time.strftime("%H:%M:%S")
        entry = "[{ts}] [{agent}] {msg}".format(ts=ts, agent=agent_name, msg=message)
        self.log.append(entry)

    def add_changed_file(self, path: str):
        if path not in self.changed_files:
            self.changed_files.append(path)

    def add_tool_result(self, name: str, result: str):
        self.tool_results.append({"name": name, "result": result, "ts": time.time()})

    def iteration_ok(self) -> bool:
        """检查是否还能继续（不修改计数器）"""
        return self.iteration < self.max_iterations

    def advance_iteration(self) -> bool:
        """推进一个迭代，返回是否还能继续"""
        self.iteration += 1
        return self.iteration < self.max_iterations

    def summary(self) -> str:
        lines = ["=== AGENT CONTEXT SUMMARY ==="]
        lines.append("Iteration: " + str(self.iteration))
        lines.append("Changed files: " + str(self.changed_files))
        lines.append("Tool calls: " + str(len(self.tool_results)))
        lines.append("Review notes: " + str(len(self.review_notes)))
        if self.plan:
            lines.append("Plan: " + self.plan.title)
            for i, step in enumerate(self.plan.steps):
                status = self.plan.step_status.get(i, "pending")
                lines.append("  [" + status + "] " + step)
        return "\n".join(lines)


class Plan:
    """一个可执行、可迭代、可修订的计划。"""

    STATUS_PENDING = "pending"
    STATUS_DONE = "done"
    STATUS_REVISE = "revise"

    def __init__(self, title: str, goal: str = "",
                 steps: Optional[List[str]] = None):
        self.title = title
        self.goal = goal
        self.steps: List[str] = steps or []
        self.step_status: Dict[int, str] = {}  # step_idx -> status
        self.revision_notes: List[str] = []
        self.version: int = 1
        self.created_at = time.time()

    def mark_step(self, idx: int, status: str):
        self.step_status[idx] = status

    def is_complete(self) -> bool:
        if not self.steps:
            return False
        return all(self.step_status.get(i, "") == self.STATUS_DONE
                   for i in range(len(self.steps)))

    def needs_revision(self) -> bool:
        return any(self.step_status.get(i) == self.STATUS_REVISE
                   for i in range(len(self.steps)))

    def insert_step(self, before_idx: int, step_text: str):
        """在步骤 before_idx 前插入一个新步骤。"""
        if before_idx < 0 or before_idx > len(self.steps):
            return  # 边界保护：拒绝越界插入
        self.steps.insert(before_idx, step_text)
        # 重排已有状态
        new_status: Dict[int, str] = {}
        for idx, _ in enumerate(self.steps):
            if idx < before_idx:
                new_status[idx] = self.step_status.get(idx, self.STATUS_PENDING)
            elif idx == before_idx:
                new_status[idx] = self.STATUS_PENDING
            else:
                new_status[idx] = self.step_status.get(idx - 1, self.STATUS_PENDING)
        self.step_status = new_status
        self.version += 1

    def replace_step(self, idx: int, step_text: str):
        if 0 <= idx < len(self.steps):
            self.steps[idx] = step_text
            self.step_status[idx] = self.STATUS_PENDING
            self.version += 1

    def add_revision_note(self, note: str):
        self.revision_notes.append(note)

    def to_text(self) -> str:
        lines = ["=== PLAN v" + str(self.version) + ": " + self.title + " ==="]
        if self.goal:
            lines.append("Goal: " + self.goal)
        if self.revision_notes:
            lines.append("Revision notes:")
            for n in self.revision_notes:
                lines.append("  - " + n)
        lines.append("")
        lines.append("Steps:")
        for i, step in enumerate(self.steps):
            status = self.step_status.get(i, self.STATUS_PENDING)
            lines.append("  [" + status + "] " + str(i + 1) + ". " + step)
        return "\n".join(lines)


class Agent:
    """代理基类 — 每个角色继承它。"""

    ROLE_NAME = "agent"

    def __init__(self, context: AgentContext, executor=None):
        self.ctx = context
        # executor 是 ToolExecutor，让代理可以直接调用工具
        self.tool_exec = executor
        self.name = self.ROLE_NAME

    def log(self, message: str):
        self.ctx.log_event(self.name, message)

    def can_act(self) -> bool:
        """是否还可以继续执行 (防无限循环)。"""
        return self.ctx.iteration_ok()

    def run_step(self) -> bool:
        """执行一个"动作回合"，返回 True 表示有产出，False 表示无事可做。
        子类必须重写此方法。
        """
        raise NotImplementedError
