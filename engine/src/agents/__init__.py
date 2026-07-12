# -*- coding: utf-8 -*-
"""多代理协作系统

子模块:
  - base_agent       : AgentContext / Plan / Agent
  - planner_agent    : PlannerAgent (生成 & 迭代计划)
  - roles            : EngineerAgent / ReviewerAgent / TesterAgent
  - coordinator      : AgentCoordinator (流程编排)
"""
from src.agents.base_agent import AgentContext, Plan, Agent
from src.agents.planner_agent import PlannerAgent
from src.agents.roles import EngineerAgent, ReviewerAgent, TesterAgent
from src.agents.coordinator import AgentCoordinator

__all__ = [
    "AgentContext", "Plan", "Agent",
    "PlannerAgent", "EngineerAgent", "ReviewerAgent", "TesterAgent",
    "AgentCoordinator",
]
