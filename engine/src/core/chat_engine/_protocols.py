# -*- coding: utf-8 -*-
"""对话引擎协议接口与模块级常量（拆分自 chat_engine.py）。

架构设计见 ``chat_engine/__init__.py``。本模块只包含协议类与模块级常量/辅助，
不依赖其它子模块，可被任意 mixin 安全导入。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple, Protocol, TYPE_CHECKING

logger = logging.getLogger(__name__)

# 单轮工具调用次数上限：不再主动截断，让模型自己决定何时停止
TOOL_CALL_THRESHOLD = 999999

# 单轮对话最大运行时间（秒），超过后自动触发停止，防止长任务挂死 UI
DEFAULT_MAX_TURN_TIME = 1800  # 30 分钟

# 余额查询防抖：模块级锁 + 运行中标志，避免每次状态刷新都创建新线程
_balance_debounce_lock = threading.Lock()
_balance_debounce_last_call = 0.0
_balance_check_running = False
_balance_running_lock = threading.Lock()


# 动态注入/懒加载管理器的协议接口，避免 mypy 将属性标注为 object 后报未知方法调用。
class _MCPManagerLike(Protocol):
    def list_servers(self) -> Optional[List[Dict[str, Any]]]: ...
    def get_all_tools(self) -> Optional[List[Dict[str, Any]]]: ...
    def start_all(self) -> Optional[List[Dict[str, Any]]]: ...


class _SklIIIManagerLike(Protocol):
    def list_packages(self) -> Optional[List[Dict[str, Any]]]: ...
    def get_tool_definitions(self) -> Optional[List[Dict[str, Any]]]: ...


class _ImageDocumentManagerLike(Protocol):
    def store_from_data_url(self, data_url: str, source_doc: str = "") -> Optional[str]: ...


class _SubAgentEngineLike(Protocol):
    def start_subagent(self, agent_id: str, task_description: str,
                       model: Optional[str] = None) -> Tuple[bool, str]: ...
    def list_agents(self) -> List[Dict[str, Any]]: ...
    def get_results(self) -> str: ...
    def stop_agent(self, agent_id: str) -> Tuple[bool, str]: ...
    def wait_for_all(self, timeout: float = 600) -> None: ...
