# -*- coding: utf-8 -*-
"""chat_engine 包共享导入 — 消除 9 个 mixin/base 文件中的重复导入块。

所有 mixin 和 base 通过 ``from src.core.chat_engine._imports import *`` 获取
共享依赖，避免每次修改导入时需要同步 9 个文件。
"""
from __future__ import annotations

import logging
import threading
import json
import time
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable, Tuple, Protocol, TYPE_CHECKING

from src.core.deepseek_api import DeepSeekAPI, APIError
from src.core.system_prompt import SystemPromptBuilder
from src.core.config import Config
from src.core.confirmation import ConfirmationGate, ConfirmationError
from src.core.rate_limiter import get_global_rate_limiter
from src.core.artifact_manager import ArtifactManager
from src.sandbox.project_manager import ProjectSandbox
from src.sandbox.history import ConversationHistory
from src.tools.tool_executor import ToolExecutor
from src.core.watchdog_monitor import FileSystemMonitor
from src.core.kun_runtime import KunRuntime

if TYPE_CHECKING:
    from src.core.auto_memory import AutoMemory
    from src.core.session_memory import SessionMemory
    from src.core.long_term_memory import LongTermMemory
    from src.core.user_profile_memory import UserProfileMemory

logger = logging.getLogger(__name__)

from src.core.chat_engine._protocols import (  # noqa: E402
    TOOL_CALL_THRESHOLD,
    DEFAULT_MAX_TURN_TIME,
    _MCPManagerLike,
    _SklIIIManagerLike,
    _ImageDocumentManagerLike,
    _SubAgentEngineLike,
    _balance_debounce_lock,
    _balance_debounce_last_call,
    _balance_check_running,
    _balance_running_lock,
)

__all__ = [
    # stdlib
    "logging", "threading", "json", "time", "re", "Path",
    "Any", "Dict", "List", "Optional", "Callable", "Tuple", "Protocol",
    "TYPE_CHECKING",
    # project
    "DeepSeekAPI", "APIError", "SystemPromptBuilder", "Config",
    "ConfirmationGate", "ConfirmationError", "get_global_rate_limiter",
    "ArtifactManager", "ProjectSandbox", "ConversationHistory",
    "ToolExecutor", "FileSystemMonitor", "KunRuntime",
    # protocols
    "TOOL_CALL_THRESHOLD", "DEFAULT_MAX_TURN_TIME",
    "_MCPManagerLike", "_SklIIIManagerLike", "_ImageDocumentManagerLike",
    "_SubAgentEngineLike",
    "_balance_debounce_lock", "_balance_debounce_last_call",
    "_balance_check_running", "_balance_running_lock",
    "logger",
]
