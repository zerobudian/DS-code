# -*- coding: utf-8 -*-
"""代理协调器 (AgentCoordinator)

这是整个多代理协作系统的"大脑"。它控制整个迭代流程:

  用户需求
      |
      v
  [Planner]  →  生成初始计划 (Plan)
      |
      v
  [循环 N 次]
    |
    +-- [Engineer]  →  按当前步骤推进 (写代码, 调工具)
    |     |
    |     v
    |   记录改动文件  →  AgentContext
    |
    +-- [Reviewer]   →  检查改动文件的代码质量
    |     |
    |     v
    |   发现问题吗?  → YES → [Planner] 修订计划 (REVISE)
    |                          |
    |                          v
    |                         重新执行 (下一轮循环)
    |                          |
    |                          NO → 继续
    |
    +-- [Tester]    →  验证 (lint / pytest / types)
    |     |
    |     v
    |   失败吗?    → YES → [Planner] 修订计划 (REVISE)
    |                          |
    |                          v
    |                         重试
    |                          |
    |                          NO → 继续
    |
    [退出循环条件]
      * 计划所有步骤都完成了 (is_complete)
      * 或达到最大迭代次数 (max_iterations)
      * 或连续 N 次修订都没有进展

最后 → 生成总结报告

关键: 这是一个"看 / 想 / 做 / 查 / 改"的闭环 — 规划不是一次性的。
"""
import time
from typing import List, Dict, Any

from src.agents.base_agent import AgentContext, Plan, _NEGATION_PATTERNS, _ZERO_FAIL_RE
from src.agents.planner_agent import PlannerAgent
from src.agents.roles import EngineerAgent, ReviewerAgent, TesterAgent


# 从 base_agent 导入的共享正则已在上方，此处不再重复定义


class AgentCoordinator:
    """代理协调器 — 管理整个协作流程。"""

    def __init__(self, project_dir, tool_executor=None,
                 max_iterations: int = 10):
        self.ctx = AgentContext(project_dir)
        # 边界校验：防止 max_iterations 为 0 或负数导致永远不迭代
        if max_iterations < 1:
            max_iterations = 10
        self.ctx.max_iterations = max_iterations
        self.planner = PlannerAgent(self.ctx, tool_executor)
        self.engineer = EngineerAgent(self.ctx, tool_executor)
        self.reviewer = ReviewerAgent(self.ctx, tool_executor)
        self.tester = TesterAgent(self.ctx, tool_executor)
        # 历史: 每一次循环的快照
        self.history: List[Dict[str, Any]] = []
        # 连续失败计数 (用于早停)
        self._consecutive_failures = 0

    # ---------- 主流程 ----------

    def run(self, user_goal: str) -> str:
        """运行完整的多代理流程，返回总结报告。"""
        # Step 1: 规划
        self.planner.create_initial_plan(user_goal)
        self._record_snapshot("initial plan")

        # Step 2: 循环执行
        while not self.planner.is_plan_complete():
            if not self.ctx.iteration_ok():
                self.ctx.log_event("coordinator",
                                    "MAX ITERATIONS REACHED — stopping")
                break
            self.ctx.advance_iteration()
            loop_count = self.ctx.iteration

            self.ctx.log_event("coordinator",
                                "=== Iteration " + str(loop_count) + " ===")

            # Engineer 推进当前步骤
            try:
                eng_did = self.engineer.run_step()
            except Exception as e:
                self.ctx.log_event("coordinator", f"Engineer crashed: {e}")
                eng_did = False

            # Reviewer 检查
            try:
                if self.ctx.changed_files:
                    self.reviewer.run_step()
            except Exception as e:
                self.ctx.log_event("coordinator", f"Reviewer crashed: {e}")

            # Tester 验证
            try:
                self.tester.run_step()
            except Exception as e:
                self.ctx.log_event("coordinator", f"Tester crashed: {e}")
                self.ctx.validation_report = f"Tester crashed: {e}"

            # 根据验证结果决定是否修订计划
            if self.ctx.validation_report:
                if self._report_shows_failure(self.ctx.validation_report):
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= 2:
                        old_version = self.ctx.plan.version if self.ctx.plan else 0
                        revised_plan = self.planner.auto_revise_from_validation(
                            self.ctx.validation_report)
                        # 通过版本号判断是否真正发生了修订（revise_plan 原地修改 plan 对象）
                        if revised_plan is not None and revised_plan.version > old_version:
                            self.ctx.log_event(
                                "coordinator",
                                "plan was revised after validation failures")
                            self._consecutive_failures = 0
                            self.ctx.validation_report = None
                            self.ctx.review_notes = []
                            self._record_snapshot("plan revised (iteration " + str(loop_count) + ")")
                            continue
                        else:
                            self.ctx.log_event(
                                "coordinator",
                                "planner decided no revision needed despite failures")
                else:
                    self._consecutive_failures = 0

            # 如果 Engineer 什么也没做，说明卡住了 — 尝试修订
            if not eng_did:
                self.ctx.log_event(
                    "coordinator", "engineer had no progress — revising plan")
                self.planner.revise_plan(
                    "engineer could not advance — new approach needed",
                    ["审视项目结构，寻找新切入点",
                     "尝试更细粒度的步骤",
                     "重新评估目标"])
                self.ctx.validation_report = None
                self.ctx.review_notes = []
                self._record_snapshot("stuck-recovery (iteration " + str(loop_count) + ")")

            self._record_snapshot("iteration " + str(loop_count))

        # Step 3: 总结
        return self._build_summary()

    def run_single_iteration(self, user_goal: str) -> str:
        """只跑一次迭代 (用于前端逐步展示)。"""
        if self.ctx.plan is None:
            self.planner.create_initial_plan(user_goal)
            self._record_snapshot("initial plan")
            return "=== Iteration 1 / Created Plan ===\n" + \
                (self.ctx.plan.to_text() if self.ctx.plan else "")
        # 检查迭代上限，防止无限循环
        if not self.ctx.iteration_ok():
            self.ctx.log_event("coordinator",
                                "MAX ITERATIONS REACHED — stopping")
            return self._build_summary()
        self.ctx.advance_iteration()
        # Engineer
        try:
            self.engineer.run_step()
        except Exception as e:
            self.ctx.log_event("coordinator", f"Engineer crashed: {e}")
        # Reviewer
        if self.ctx.changed_files:
            try:
                self.reviewer.run_step()
            except Exception as e:
                self.ctx.log_event("coordinator", f"Reviewer crashed: {e}")
        # Tester
        try:
            self.tester.run_step()
        except Exception as e:
            self.ctx.log_event("coordinator", f"Tester crashed: {e}")
            self.ctx.validation_report = f"Tester crashed: {e}"
        self._record_snapshot("iteration " + str(self.ctx.iteration))
        return self._build_summary()

    # ---------- 报告 ----------

    def get_plan_text(self) -> str:
        if not self.ctx.plan:
            return "(no plan)"
        return self.ctx.plan.to_text()

    def get_context_summary(self) -> str:
        return self.ctx.summary()

    # ---------- 内部: 判定 / 记录 ----------

    def _report_shows_failure(self, report: str) -> bool:
        """简单启发式：看报告里有没有失败迹象。

        改进：使用预编译正则，避免每次调用重复编译。
        边界处理：报告可能同时包含"no errors"和实际错误行（如
        "no errors in module A, but 3 failed in module B"），此时
        否定词匹配不应完全屏蔽失败判定。

        策略（按优先级）：
          1. 强信号（SyntaxError / NameError 等）→ 扫描所有出现点，
             只要有一处未被否定词局部否定，就判定失败。
          2. 一般失败关键词 → 扫描所有出现点，同上。
          3. 明确全部通过信号 → 返回 False。
          4. 兜底 → 返回 False。
        """
        if not report or not isinstance(report, str):
            return False
        lowered = report.lower()

        def _has_unnegated_occurrence(keyword: str) -> bool:
            """在 lowered 中查找 keyword 的所有出现点，只要有一处
            前面 20 字符窗口内不含否定词就认为存在失败信号。
            """
            if not keyword:
                return False
            start = 0
            kw_len = len(keyword)
            while True:
                idx = lowered.find(keyword, start)
                if idx < 0:
                    return False
                prefix = lowered[max(0, idx - 20):idx]
                # 检查否定前缀：no/not/without 是子串匹配即可；
                # "0 " 必须用词边界匹配，否则 "10 " 中的 "0 " 子串会误匹配
                negated = any(neg in prefix for neg in ("no ", "not ", "without "))
                if not negated:
                    # 检查 "0 " 是否作为独立数字出现（前面是非数字字符或字符串开头）
                    import re as _re_mod
                    if _re_mod.search(r'(^|\D)0\s', prefix):
                        negated = True
                if not negated:
                    return True
                start = idx + kw_len

        # 第一轮：强信号（Python 异常名）—— 任一未被否定命中即失败
        strong_fail = ["syntaxerror", "nameerror", "typeerror",
                       "importerror", "modulenotfounderror"]
        for w in strong_fail:
            if _has_unnegated_occurrence(w):
                return True

        # 零失败模式（如 "0 failed, 5 passed"）视为通过。
        # 必须放在强信号和一般失败关键词之后：否则 "0 failed" 会掩盖
        # 报告中其他位置的真实失败（如 "3 failed in module B"）。
        # 先检查是否有任何非零的失败关键词命中，再判断零失败模式。

        # 第二轮：一般失败关键词（统一小写，避免大小写漏判）
        fail_words = ["failed", "error:", "fail"]
        for w in fail_words:
            if _has_unnegated_occurrence(w):
                return True
        if "\u2717" in lowered:  # "✗"
            # 符号字符本身不区分大小写，直接扫描上下文
            if _has_unnegated_occurrence("\u2717"):
                return True

        # 零失败模式（如 "0 failed, 5 passed"）：放在 fail_words 之后，
        # 确保报告中存在真实失败（如 "3 failed"）时不会被 "0 failed" 掩盖。
        if _ZERO_FAIL_RE.search(lowered):
            return False

        # 第三轮：明确无误的通过信号
        for pat in _NEGATION_PATTERNS:
            if pat.search(lowered):
                return False
        return False

    def _record_snapshot(self, label: str):
        """记录一次循环的快照 (用于 UI 展示进度)。"""
        snap = {
            "label": label,
            "iteration": self.ctx.iteration,
            "changed_files": list(self.ctx.changed_files),
            "review_notes": list(self.ctx.review_notes),
            "validation": self.ctx.validation_report,
            "timestamp": time.time(),
        }
        if self.ctx.plan:
            snap["plan_version"] = self.ctx.plan.version
            snap["plan_steps"] = len(self.ctx.plan.steps)
            snap["plan_status"] = dict(self.ctx.plan.step_status)
        self.history.append(snap)

    def _build_summary(self) -> str:
        lines = []
        lines.append("========== AGENT COLLABORATION SUMMARY ==========")
        lines.append("Iterations: " + str(self.ctx.iteration))
        lines.append("History snapshots: " + str(len(self.history)))
        lines.append("Changed files: " + str(len(self.ctx.changed_files)))
        for f in self.ctx.changed_files:
            lines.append("  * " + f)
        lines.append("")
        if self.ctx.plan:
            lines.append("PLAN:")
            lines.append(self.ctx.plan.to_text())
        if self.ctx.review_notes:
            lines.append("")
            lines.append("REVIEW NOTES:")
            for n in self.ctx.review_notes:
                lines.append("  - " + n[:120])
        if self.ctx.validation_report:
            lines.append("")
            lines.append("VALIDATION REPORT:")
            lines.append(self.ctx.validation_report)
        lines.append("")
        lines.append("====================================================")
        return "\n".join(lines)

    # ---------- 手动 API (供前端直接调用) ----------

    def mark_file_changed(self, path: str):
        """外部代码改动后，告诉协调器有文件改动了。"""
        self.ctx.add_changed_file(path)

    def get_current_step_hint(self) -> str:
        """返回当前计划中第一个未完成步骤 (用于 UI 提示)。"""
        if not self.ctx.plan:
            return ""
        for idx, step in enumerate(self.ctx.plan.steps):
            status = self.ctx.plan.step_status.get(idx, Plan.STATUS_PENDING)
            if status != Plan.STATUS_DONE:
                return "Step " + str(idx + 1) + "/" + \
                    str(len(self.ctx.plan.steps)) + ": " + step
        return "(plan complete)"

    def get_progress(self) -> Dict[str, int]:
        """返回 {total, done, percent} 形式的进度字典。"""
        if not self.ctx.plan or not self.ctx.plan.steps:
            return {"total": 0, "done": 0, "percent": 0}
        total = len(self.ctx.plan.steps)
        # 仅统计当前步骤范围内的 status，忽略 revise_plan 遗留的陈旧条目
        done = sum(1 for i in range(total)
                   if self.ctx.plan.step_status.get(i) == Plan.STATUS_DONE)
        percent = int(done * 100 / total) if total > 0 else 0
        return {"total": total, "done": done, "percent": min(percent, 100)}
