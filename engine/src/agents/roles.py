# -*- coding: utf-8 -*-
"""Agent 角色定义 — 能力边界说明

EngineerAgent:
  实际能力: 记录任务状态，不直接生成代码。
  代码生成由主 ChatEngine 驱动 ToolExecutor 完成。

ReviewerAgent:
  实际能力: 基于正则规则检查代码风格和常见问题。
  不支持语义审查或架构级 CR。

TesterAgent:
  实际能力: 封装 AutoValidator 调用，运行预定义的测试/检查命令。
  不自动生成测试用例。

CoordinatorAgent (AgentCoordinator):
  实际能力: 编排多步计划的状态流转，分派步骤给 Engineer/Reviewer/Tester。
  不自行决策执行细节。
"""

import os
import re
from pathlib import Path
from typing import List

from src.agents.base_agent import Agent, AgentContext, Plan
from src.analysis.code_intel import CodeIntel
from src.analysis.auto_validation import AutoValidator


class EngineerAgent(Agent):
    """工程师代理 — 按计划步骤执行代码改动。

    注意：本代理并不直接调用 LLM 来写代码 — 它负责:
      1. 根据当前计划步骤，决定应该对哪些文件做什么动作
      2. 调用 ToolExecutor 执行实际的文件修改 / 代码生成
      3. 记录改动文件到 context
    """

    ROLE_NAME = "engineer"

    def __init__(self, context: AgentContext, executor=None):
        super().__init__(context, executor)
        self.intel = CodeIntel(context.project_dir)

    def mark_file_changed(self, file_path: str):
        """记录一个文件被改动。"""
        self.ctx.add_changed_file(file_path)
        self.log("changed file: " + file_path)

    def run_step(self) -> bool:
        """工程师的一个动作回合：找当前未完成的第一个步骤，想办法推进它。"""
        plan = self.ctx.plan
        if not plan:
            return False
        for idx, step in enumerate(plan.steps):
            if plan.step_status.get(idx, Plan.STATUS_PENDING) == Plan.STATUS_DONE:
                continue
            # 这是当前步骤 — 尝试推进
            progress = self._execute_step(idx, step)
            if progress:
                plan.mark_step(idx, Plan.STATUS_DONE)
                self.log("step completed: " + step[:60])
                return True
            return False
        return False

    def _execute_step(self, step_idx: int, step_text: str) -> bool:
        """尝试推进一个步骤。

        策略：如果这个步骤是"探索 / 分析"型，我们就做一次代码洞察；
              如果它是"修改 / 创建"型，我们就标记它（具体文件内容由
              ToolExecutor 或上层 AI 决定写什么）。
        """
        step_lower = step_text.lower()
        # 探索类步骤
        if any(k in step_lower for k in ["阅读", "理解", "结构", "explore",
                                          "read", "探索"]):
            self.ctx.add_tool_result(
                "project_brief", self.intel.project_brief())
            return True

        if any(k in step_lower for k in ["分析", "影响", "impact", "依赖"]):
            files = self.ctx.changed_files or list(self._project_files()[:5])
            if files:
                self.ctx.add_tool_result("impact", self.intel.impact(files))
            return True

        if any(k in step_lower for k in ["定位", "bug", "debug", "原因"]):
            if self.ctx.changed_files:
                first = self.ctx.changed_files[-1]
                self.ctx.add_tool_result(
                    "file_intel_" + first, self.intel.file_intel(first))
            return True

        if any(k in step_lower for k in ["设计", "方案", "design"]):
            # 记录：由上层 AI 决定具体设计
            self.ctx.add_tool_result("design_hint",
                                      "待上层 AI 写出详细设计 / 伪代码")
            return True

        # 代码修改 / 创建类步骤 — 交给上层处理 (我们只记录)
        if any(k in step_lower for k in ["写", "修改", "编辑", "代码",
                                          "编写", "实现", "code", "write", "edit"]):
            # 工程师本身不直接写代码，只是记录
            self.ctx.add_tool_result(
                "engineer_ready",
                "Engineer agent ready — awaiting ToolExecutor write-file calls")
            return True

        if any(k in step_lower for k in ["测试", "test", "test case"]):
            self.ctx.add_tool_result("tester_ready",
                                      "Engineer notes: tests to be written")
            return True

        # 其他类型：默认推进
        self.ctx.add_tool_result("step_" + str(step_idx),
                                  "handled: " + step_text[:80])
        return True

    def _project_files(self) -> List[str]:
        out = []
        for root, dirs, files in os.walk(self.ctx.project_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for f in files:
                if f.endswith(".py"):
                    fp = os.path.relpath(os.path.join(root, f),
                                          self.ctx.project_dir)
                    out.append(fp)
        return out[:30]


class ReviewerAgent(Agent):
    """评审代理 — 静态检查改动文件的质量。"""

    ROLE_NAME = "reviewer"

    _RAW_RULES = [
        # (检查名, 正则, 问题描述, 建议等级 high/low)
        ("TODO left", r"\bTODO\b|\bFIXME\b", "文件中还有未完成的 TODO/FIXME", "low"),
        ("print debug", r"^\s*print\s*\(", "保留了 print 调试语句", "low"),
        ("long line", r".{140,}", "存在超过 140 列的行", "low"),
        ("bare except", r"except\s*:\s*$", "使用了裸 except — 应该捕获具体异常", "high"),
        ("mutable default", r"def\s+\w+\([^)]*=\s*(\[\]|\{\})", "函数参数用了可变默认值", "high"),
        ("assert in prod", r"^\s*assert\b", "assert 语句 (生产环境可能被 -O 跳过)", "medium"),
    ]
    # 预编译正则，避免每次 run_step 调用时重复编译
    RULES = [
        (name, re.compile(pattern, re.MULTILINE), desc, level)
        for name, pattern, desc, level in _RAW_RULES
    ]

    def __init__(self, context: AgentContext, executor=None):
        super().__init__(context, executor)

    def run_step(self) -> bool:
        """对改动过的文件做一次静态规则检查 + AST 校验。"""
        files = self.ctx.changed_files
        if not files:
            return False
        findings = []
        for fp in files:
            full_path = (Path(self.ctx.project_dir) / fp).resolve()
            # 路径安全校验: 确保文件在项目目录内
            try:
                full_path.relative_to(Path(self.ctx.project_dir).resolve())
            except ValueError:
                self.log("skipped out-of-project file: " + fp)
                continue
            if not full_path.exists():
                continue
            try:
                content = full_path.read_text(encoding="utf-8", errors="ignore")
            except (OSError, UnicodeDecodeError) as e:
                self.log("cannot read " + fp + ": " + str(e))
                continue
            # 规则检查 — 使用预编译正则，避免每次调用重复编译
            for rule_name, pattern, desc, level in self.RULES:
                matches = pattern.finditer(content)
                count = sum(1 for _ in matches)
                if count > 0:
                    findings.append("[{lv}] {f}: {n}x {rule} — {desc}".format(
                        lv=level, f=fp, n=count, rule=rule_name, desc=desc))
            # AST 校验
            if fp.endswith(".py"):
                try:
                    import ast
                    ast.parse(content)
                except SyntaxError as e:
                    findings.append("[high] " + fp + ": SyntaxError — " + str(e))
                except (RecursionError, MemoryError) as e:
                    findings.append("[high] " + fp + ": AST parse error — " + str(e))
        if findings:
            report = "REVIEW FINDINGS:\n" + "\n".join(findings[:20])
            self.ctx.validation_report = report
            # 限制 review_notes 累积数量，避免无限增长
            self.ctx.review_notes.extend(findings[:10])
            if len(self.ctx.review_notes) > 50:
                self.ctx.review_notes = self.ctx.review_notes[-50:]
            self.log("review found " + str(len(findings)) + " issue(s)")
        else:
            self.ctx.validation_report = "REVIEW: no issues found"
            self.ctx.review_notes = []  # 无新问题时清理旧的评审记录，避免误导 Planner
            self.log("review passed, no issues")
        return True


class TesterAgent(Agent):
    """测试代理 — 触发自动验证循环并记录报告。"""

    ROLE_NAME = "tester"

    def __init__(self, context: AgentContext, executor=None):
        super().__init__(context, executor)
        self.validator = AutoValidator(context.project_dir)

    def run_step(self) -> bool:
        """运行一次完整验证 (语法 + pyflakes + pytest)。"""
        targets = self.ctx.changed_files or None
        results = self.validator.validate_all(targets)
        report = self.validator.report(results)
        # 使用独立字段 test_report，避免覆盖 Reviewer 的 validation_report
        self.ctx.test_report = report
        # 合并到 validation_report（保留 Reviewer 的发现）
        existing = getattr(self.ctx, "validation_report", "") or ""
        if existing and "REVIEW FINDINGS" in existing:
            self.ctx.validation_report = existing + "\n\nTEST REPORT:\n" + report
        else:
            self.ctx.validation_report = report
        passed = sum(1 for r in results if r.passed)
        failed = len(results) - passed
        self.log("validation: " + str(passed) + " passed, " +
                 str(failed) + " failed")
        return True

    def quick_check(self, file_path: str) -> str:
        return self.validator.quick_check_file(file_path)
