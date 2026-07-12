# -*- coding: utf-8 -*-
"""SubAgentMixin — 子代理工具接口（拆分自 chat_engine.py）。"""
from __future__ import annotations

from src.core.chat_engine._imports import *  # noqa: F401, F403




class SubAgentMixin(object):
    """子代理工具接口方法。"""

    # ========================================================================
    # 子代理工具接口 — 由主模型通过 JSON 工具调用
    # ========================================================================

    def tool_start_subagent(self, call: dict):
        """启动一个子代理（并行执行子任务）

        JSON 格式:
            {"tool":"start_subagent","id":"unique_id","task_description":"...","model":"deepseek-v4-flash"}

        - id: 子代理唯一标识（用于查询/停止），可不传则自动生成
        - task_description: 必填，具体子任务描述
        - model: 可选，"deepseek-v4-pro" 或 "deepseek-v4-flash"（默认由配置决定）
        - 可在一个 turn 中多次调用此工具，子代理并行执行
        """
        agent_id = str(call.get("id", "") or "") or f"agent_{self._gen_id()}"
        task = str(call.get("task_description", "") or "").strip()
        model = str(call.get("model", "") or "").strip() or None
        if not task:
            return False, "[start_subagent] 缺少 task_description"
        # 模型白名单校验，防止无效/恶意模型字符串导致错误请求
        allowed_models = {"deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-ultra",
                          "deepseek-chat", "deepseek-reasoner"}
        if model is not None and model not in allowed_models:
            return False, f"[start_subagent] 不支持的模型: {model}"
        if self._subagent is None:
            return False, "[start_subagent] 子代理引擎未初始化"

        ok, msg = self._subagent.start_subagent(agent_id, task, model)
        return ok, msg

    def tool_list_subagents(self, call=None):
        """列出所有子代理及其状态"""
        if self._subagent is None:
            return True, "(子代理引擎未初始化)"
        agents = self._subagent.list_agents()
        if not agents:
            return True, "(暂无子代理)"
        lines = []
        for a in agents:
            lines.append(f"- [{a.get('status', '?')}] {a.get('id', '?')} (model={a.get('model', '?')}, {a.get('running_seconds', 0)}s)")
            lines.append(f"    {a.get('task', '')}")
        return True, "\n".join(lines)

    def tool_get_subagent_results(self, call=None):
        """获取子代理结果汇总"""
        if self._subagent is None:
            return True, "(子代理引擎未初始化)"
        return True, self._subagent.get_results()

    def tool_stop_subagent(self, call: dict):
        """停止一个子代理"""
        agent_id = str(call.get("id", "") or "")
        if not agent_id:
            return False, "[stop_subagent] 缺少 id"
        if self._subagent is None:
            return False, "[stop_subagent] 子代理引擎未初始化"
        return self._subagent.stop_agent(agent_id)

