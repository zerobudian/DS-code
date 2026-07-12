"""MCP (Model Context Protocol) 客户端 & 服务器管理器。

封装:
  - MCP stdio 客户端 (通过 subprocess 启动 MCP 服务器)
  - MCP 服务器管理器 (配置文件管理 + 启动/停止 + 工具发现)
  - 将 MCP tools 桥接到 ToolExecutor

依赖:
  官方 MCP Python SDK (>= 1.27): `from mcp import ClientSession`
  若 SDK 不可用，所有类仍可导入，调用时返回结构化错误结果。
"""

from __future__ import annotations

from typing import Any, Dict

# 对外暴露的类型别名: 工具/工具调用结果 (字典)
MCPTool = Dict[str, Any]
MCPToolResult = Dict[str, Any]

# 延迟导入核心类，避免 MCP SDK 缺失时 import 直接失败
from .client import MCPClient
from .manager import MCPManager, MCPServerConfig

__all__ = [
    "MCPClient",
    "MCPToolResult",
    "MCPTool",
    "MCPManager",
    "MCPServerConfig",
]
