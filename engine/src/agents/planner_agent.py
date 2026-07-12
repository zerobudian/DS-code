# -*- coding: utf-8 -*-
"""规划代理 (Planner Agent)

负责:
  1. 基于用户需求和项目结构生成初始计划
  2. 在收到评审意见 / 测试失败 / 新发现时修订计划
  3. 判定计划何时算"完成"或"卡住需人工介入"

这是"规划可反复迭代"的核心实现 — 计划不是一次性的，它会在执行中被修订。
"""
import re
from typing import List, Optional

from src.agents.base_agent import Agent, Plan, AgentContext, _ZERO_FAIL_RE
from src.analysis.code_intel import CodeIntel


class PlannerAgent(Agent):
    """规划代理 — 负责制定 / 修订整个任务计划。"""

    ROLE_NAME = "planner"

    def __init__(self, context: AgentContext, executor=None):
        super().__init__(context, executor)
        self.intel = CodeIntel(context.project_dir)

    # ---------- 公开 API ----------

    def create_initial_plan(self, user_goal: str) -> Plan:
        """基于用户需求 + 项目代码洞察，生成初始计划。"""
        self.intel.refresh()
        brief = self.intel.project_brief()

        user_goal = user_goal or ""
        steps = self._generate_steps(user_goal, brief)
        plan = Plan(
            title="计划: " + (user_goal[:60] if len(user_goal) > 60 else user_goal),
            goal=user_goal,
            steps=steps,
        )
        self.ctx.plan = plan
        self.log("created initial plan with " + str(len(steps)) + " steps")
        return plan

    def revise_plan(self, reason: str, new_steps: Optional[List[str]] = None) -> Plan:
        """根据理由修订计划 (比如：测试失败 / 发现新依赖 / 评审意见)。

        参数:
          reason    — 为什么要修订 (会被记录到 plan.revision_notes)
          new_steps — 如果给了，就用这些步骤替代当前计划的剩余步骤；
                      如果没给，就把当前未完成步骤标记为"待修订"。
        """
        plan = self.ctx.plan or Plan(title="(auto-created)", goal="")
        plan.add_revision_note(reason)

        if new_steps:
            # 找到第一个未完成的步骤，替换其后所有
            first_unfinished = len(plan.steps)
            for i in range(len(plan.steps)):
                if plan.step_status.get(i, Plan.STATUS_PENDING) != Plan.STATUS_DONE:
                    first_unfinished = i
                    break
            # 截断未完成部分，加入新步骤
            plan.steps = plan.steps[:first_unfinished] + new_steps
            # 重置新步骤的状态为 pending
            for i in range(first_unfinished, len(plan.steps)):
                plan.step_status[i] = Plan.STATUS_PENDING
            self.log("plan revised, now " + str(len(plan.steps)) + " steps")
        else:
            # 标记未完成步骤为 "待修订"
            for i in range(len(plan.steps)):
                if plan.step_status.get(i, Plan.STATUS_PENDING) != Plan.STATUS_DONE:
                    plan.step_status[i] = Plan.STATUS_REVISE

        plan.version += 1
        self.ctx.plan = plan
        return plan

    def mark_step_done(self, idx: int):
        if self.ctx.plan:
            self.ctx.plan.mark_step(idx, Plan.STATUS_DONE)
            self.log("step " + str(idx + 1) + " done")

    def is_plan_complete(self) -> bool:
        return bool(self.ctx.plan and self.ctx.plan.is_complete())

    # ---------- 计划生成逻辑 (基于代码洞察的启发式) ----------

    def _generate_steps(self, goal: str, brief: str) -> List[str]:
        """根据目标 + 项目摘要，生成一套合理的执行步骤序列。

        策略是结构化的 — 不是让 LLM 自由发挥，而是按以下模板生成:
          1. 了解项目 (读取关键文件)
          2. 设计方案 (如果需要)
          3. 创建 / 修改代码
          4. 验证 (lint + 编译 + 测试)
          5. 总结

        这保证了每个代理都有东西可做。
        """
        goal_lower = (goal or "").lower()

        steps = []

        # Step 1: 探索 & 理解
        steps.append("阅读项目结构，理解现有代码布局 — "
                     + self._top_classes_hint(brief))

        # Step 2: 设计
        if any(k in goal_lower for k in ["新", "创建", "加", "add", "new", "create"]):
            steps.append("设计新增代码的文件/类/方法结构")
        elif any(k in goal_lower for k in ["修", "fix", "bug", "debug"]):
            steps.append("定位 bug 并理解根因")
        elif any(k in goal_lower for k in ["重构", "refactor", "整理"]):
            steps.append("分析重构范围与边界")

        # Step 3: 代码改动
        if any(k in goal_lower for k in ["测试", "test"]):
            steps.append("编写测试用例")
        else:
            steps.append("编写 / 修改核心代码")

        # Step 4: 写测试 (除非本来就是纯测试任务)
        if not any(k in goal_lower for k in ["测试", "test"]):
            steps.append("为改动编写或补充测试")

        # Step 5: 验证
        steps.append("语法检查 (py_compile / import)")
        steps.append("运行静态分析 (pyflakes)")
        steps.append("运行 pytest 测试")

        # Step 6: 影响分析
        if self.ctx.changed_files:
            steps.append("分析改动对其他文件的影响")

        # Step 7: 总结
        steps.append("总结改动，列出变更文件与验证结果")

        return steps

    def _top_classes_hint(self, brief: str) -> str:
        """从 project_brief 中提取关键类名，用于生成 hint。"""
        for line in brief.splitlines():
            if "class" in line and "(" in line:
                m = re.search(r"class\s+(\w+)", line)
                if m:
                    return "例如: " + m.group(1)
        return "识别主要模块"

    # ---------- 自动修订: 根据验证结果 ----------

    def auto_revise_from_validation(self, validation_report: str) -> Plan:
        """根据验证报告，判断是否需要修订计划。"""
        if not self.ctx.plan:
            plan = Plan("empty", "")
            self.ctx.plan = plan
            return plan

        if not validation_report or not isinstance(validation_report, str):
            return self.ctx.plan

        lowered = validation_report.lower()

        def _has_unnegated_occurrence(keyword: str) -> bool:
            """扫描所有出现点；只要有一处前面 20 字符窗口内不含
            否定词，就认为存在失败信号。
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
                    import re as _re_mod
                    if _re_mod.search(r'(^|\D)0\s', prefix):
                        negated = True
                if not negated:
                    return True
                start = idx + kw_len

        # 策略：先扫描所有错误关键词的所有出现点，只要存在一处未被
        # 否定词局部否定的错误，就必须修订计划，避免类似
        # "no errors in A, but 3 failed in B" 的报告被误判为通过。
        error_keywords = [
            "error:", "syntaxerror", "importerror", "modulenotfounderror",
            "nameerror", "typeerror", "failed", "fail", "\u2717",  # "✗"
        ]
        first_hit_label = None
        needs_revise = False
        for kw in error_keywords:
            if _has_unnegated_occurrence(kw):
                needs_revise = True
                first_hit_label = kw
                break

        # 零失败模式（如 "0 failed, 5 passed"）直接视为通过，不修订计划。
        # _has_unnegated_occurrence 已将 "0 " 加入否定前缀，因此 "0 failed"
        # 中的 "failed" 不会被命中，needs_revise 在纯零失败报告中为 False。
        # 此处检查 _ZERO_FAIL_RE 作为额外的通过信号确认。
        if not needs_revise and _ZERO_FAIL_RE.search(lowered):
            return self.ctx.plan

        # 没有任何错误关键词，且报告显示明确的通过信号 —— 不修订
        if not needs_revise and ("0 failed" in lowered and "passed" in lowered):
            return self.ctx.plan
        # 没有错误关键词也没有通过信号 —— 视作无结论，不修订
        if not needs_revise:
            return self.ctx.plan

        reasons: list = []
        # 映射：将小写关键词还原为可读的错误类型标签
        label_map = {
            "error:": "error:",
            "syntaxerror": "SyntaxError",
            "importerror": "ImportError",
            "modulenotfounderror": "ModuleNotFoundError",
            "nameerror": "NameError",
            "typeerror": "TypeError",
            "failed": "failed",
            "fail": "fail",
            "\u2717": "✗",
        }
        if first_hit_label:
            reasons.append("发现 " + label_map.get(first_hit_label, first_hit_label))
        # 兜底：如果没找到可读标签，用通用提示
        if not reasons:
            reasons.append("验证报告包含未通过迹象")

        self.log("auto-revising plan: " + "; ".join(reasons))
        new_steps = [
            "定位错误原因 (基于验证报告)",
            "修复代码问题",
            "重新运行验证",
            "若仍失败，回到计划重审 — " + "; ".join(reasons),
        ]
        return self.revise_plan("验证失败: " + "; ".join(reasons), new_steps)

    def run_step(self) -> bool:
        """Planner 的每个动作回合：检查计划状态，必要时修订。"""
        if not self.ctx.plan:
            return False
        # 有明确失败需要重计划
        if self.ctx.review_notes and self.ctx.plan.needs_revision():
            self.revise_plan("review notes: " + "; ".join(self.ctx.review_notes[-2:]))
            return True
        return False
