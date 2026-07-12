"""MCPManager — MCP 服务器管理器。

职责:
  1. 加载/保存 JSON 配置文件（项目级 + 预置开源服务器）
  2. 为每个已配置的服务器提供: start / stop / restart / status
  3. 聚合所有已连接服务器的工具列表，供 ToolExecutor 调用
  4. 为 GUI 提供统一的状态 API

预置开源 MCP 服务器:
  - filesystem: 读写项目目录文件
  - git: git log/diff/status/commit
  - brave-search: 网页搜索 (需 BRAVE_API_KEY)
  - python: 沙箱 Python 解释器
  - playwright: 浏览器自动化 (可选)
  - context7: 文档查询 (可选)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .client import MCPClient

logger = logging.getLogger(__name__)

WORK_DIR_PLACEHOLDER = "{WORK_DIR}"
API_KEY_PLACEHOLDER = "{API_KEY_PLACEHOLDER}"


# ---------------------------------------------------------------------------
# 环境变量安全
# ---------------------------------------------------------------------------
def _mcp_sanitized_env() -> Dict[str, str]:
    """MCP 子进程基础环境变量白名单：剥离敏感 token/密钥，固定 PATH。"""
    safe_env_keys = {
        "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TERM",
        "PATH", "PWD",
        # NODE_OPTIONS 已移除：--require 可注入任意 JS 代码
    }
    env = {k: str(v) for k, v in os.environ.items()
           if k in safe_env_keys
           and not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))}
    env["PATH"] = "/usr/bin:/usr/local/bin:/bin:/usr/sbin:/sbin"
    return env


# ---------------------------------------------------------------------------
# MCPServerConfig
# ---------------------------------------------------------------------------
@dataclass
class MCPServerConfig:
    """单个 MCP 服务器的配置 (数据类，可序列化)。"""

    name: str
    """服务器唯一 ID（也是聚合工具名的前缀）。"""

    display_name: str = ""
    """GUI 显示名。"""

    description: str = ""
    """简介。"""

    command: str = "npx"
    """可执行文件路径，例如 "npx" / "uvx" / "python3"。"""

    args: List[str] = field(default_factory=list)
    """命令行参数，可包含 {WORK_DIR} 等占位符。"""

    env: Dict[str, str] = field(default_factory=dict)
    """环境变量，可包含 {API_KEY_PLACEHOLDER} 占位符。"""

    working_dir: Optional[str] = None
    """None 表示使用项目工作目录；否则指定显式路径。"""

    enabled: bool = False
    """是否随项目启动。"""

    install_hint: str = ""
    """给用户的安装提示 (GUI 侧显示)。"""

    def __post_init__(self) -> None:
        if not self.display_name:
            self.display_name = self.name

    # --------------------- 序列化 ---------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
            "working_dir": self.working_dir,
            "enabled": bool(self.enabled),
            "install_hint": self.install_hint,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MCPServerConfig":
        return cls(
            name=str(d.get("name", "")),
            display_name=str(d.get("display_name", d.get("name", ""))),
            description=str(d.get("description", "")),
            command=str(d.get("command", "npx")),
            args=[str(a) for a in (d.get("args") or [])],
            env={str(k): str(v) for k, v in (d.get("env") or {}).items()},
            working_dir=d.get("working_dir"),
            enabled=bool(d.get("enabled", False)),
            install_hint=str(d.get("install_hint", "")),
        )


# ---------------------------------------------------------------------------
# 预置默认配置
# ---------------------------------------------------------------------------
def _default_server_configs() -> List[MCPServerConfig]:
    """返回开箱即用的开源 MCP 服务器默认配置。"""
    presets = [
        {
            "name": "filesystem",
            "display_name": "文件系统",
            "description": "读写项目目录下的文件",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", WORK_DIR_PLACEHOLDER],
            "env": {},
            "enabled": False,
            "install_hint": "需先安装 Node.js + npm；首次运行 npx 会自动下载",
        },
        {
            "name": "git",
            "display_name": "Git 版本控制",
            "description": "git log / diff / status / commit / branch",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-git", WORK_DIR_PLACEHOLDER],
            "env": {},
            "enabled": False,
            "install_hint": "需先安装 Node.js；项目目录需是 git 仓库",
        },
        {
            "name": "brave-search",
            "display_name": "Brave Search",
            "description": "网页搜索",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-brave-search"],
            "env": {"BRAVE_API_KEY": API_KEY_PLACEHOLDER},
            "enabled": False,
            "install_hint": "需配置 BRAVE_API_KEY 环境变量 (https://api.search.brave.com)",
        },
        {
            "name": "python",
            "display_name": "Python 代码执行",
            "description": "在沙箱 Python 解释器中运行代码",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-python"],
            "env": {},
            "enabled": False,
            "install_hint": "需先安装 Node.js",
        },
        {
            "name": "playwright",
            "display_name": "浏览器自动化",
            "description": "网页浏览 / 截图 / 点击 / 填表",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-playwright"],
            "env": {},
            "enabled": False,
            "install_hint": "需先安装 Node.js；体积较大",
        },
        {
            "name": "context7",
            "display_name": "文档查询 (Context7)",
            "description": "查询任意 SDK 的官方文档",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-context7"],
            "env": {"CONTEXT7_API_KEY": API_KEY_PLACEHOLDER},
            "enabled": False,
            "install_hint": "需在 https://context7.ai 申请 API Key",
        },
    ]
    return [MCPServerConfig.from_dict(p) for p in presets]


# ---------------------------------------------------------------------------
# MCPManager
# ---------------------------------------------------------------------------
class MCPManager:
    """项目级 MCP 管理器。一个 ProjectSandbox 对应一个 MCPManager。

    负责:
      - 读取/写入项目目录下 `.dscode/mcp_servers.json`
      - 管理每个服务器的 MCPClient 实例（按需启动/停止）
      - 聚合已启动服务器的工具列表，供 ToolExecutor 调用
    """

    def __init__(self, config_path: str, project_work_dir: str) -> None:
        """
        Args:
            config_path: 配置文件路径 (JSON)
            project_work_dir: 当前项目的工作目录，filesystem MCP 的根路径
        """
        self._config_path = config_path
        self._work_dir = os.path.abspath(project_work_dir or os.getcwd())

        self._clients: Dict[str, MCPClient] = {}
        self._configs: Dict[str, MCPServerConfig] = {}
        self._tools_cache: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

        self._load_or_init_config()

    # --------------------- 配置持久化 ---------------------
    def _load_or_init_config(self) -> None:
        """读取 JSON 文件；不存在则写入一套默认的开源服务器配置。"""
        try:
            if os.path.isfile(self._config_path):
                with open(self._config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                servers = data.get("servers", []) if isinstance(data, dict) else []
                if not servers:
                    # 配置文件存在但 servers 为空，使用默认配置并持久化
                    servers = [c.to_dict() for c in _default_server_configs()]
                    self._ensure_parent_dir()
                    with open(self._config_path, "w", encoding="utf-8") as f:
                        json.dump({"servers": servers}, f, ensure_ascii=False, indent=2)
                else:
                    # 解密每个 server 的敏感 env 值
                    for srv in servers:
                        if isinstance(srv, dict) and "env" in srv:
                            srv["env"] = self._decrypt_envs(srv.get("env", {}))
            else:
                servers = [c.to_dict() for c in _default_server_configs()]
                self._ensure_parent_dir()
                with open(self._config_path, "w", encoding="utf-8") as f:
                    json.dump({"servers": servers}, f, ensure_ascii=False, indent=2)
        except Exception as exc:  # pragma: no cover
            logger.warning("加载 MCP 配置失败，使用默认配置: %s", exc)
            servers = [c.to_dict() for c in _default_server_configs()]
            # 持久化默认配置，避免下次启动再次失败
            try:
                self._ensure_parent_dir()
                with open(self._config_path, "w", encoding="utf-8") as f:
                    json.dump({"servers": servers}, f, ensure_ascii=False, indent=2)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        with self._lock:
            self._configs = {}
            for raw in servers:
                try:
                    cfg = MCPServerConfig.from_dict(raw)
                    if cfg.name and self._validate_server_config(cfg):
                        self._configs[cfg.name] = cfg
                    elif cfg.name:
                        logger.warning("MCP 服务器配置未通过安全校验，已忽略: %s", cfg.name)
                except Exception as e:
                    logger.warning("解析 MCP 服务器配置失败: %s -> %s", raw, e)
            # 保证默认的预置项都存在（即便用户删了部分），且通过校验
            defaults = _default_server_configs()
            for d in defaults:
                if d.name not in self._configs and self._validate_server_config(d):
                    self._configs[d.name] = d

    def _validate_server_config(self, cfg: "MCPServerConfig") -> bool:
        """校验 MCP 服务器配置的 command/args 是否符合白名单。"""
        import re as _re
        allowed_commands = {"npx", "uvx", "python3", "python", "node"}
        shell_metachar = frozenset({";", "&", "|", "$", "`", "\\", "\n", "\r", "<", ">", "*", "?", "{", "}"})
        pkg_re = _re.compile(r"^[a-zA-Z0-9_.~/@-]+$")

        cmd = (cfg.command or "").strip().lower()
        if cmd not in allowed_commands:
            logger.warning("MCP 配置 command 不在白名单: %s", cfg.command)
            return False

        for a in cfg.args:
            s = str(a)
            # 允许 {WORK_DIR} 占位符（会在启动时展开为实际路径）
            s_check = s.replace(WORK_DIR_PLACEHOLDER, "")
            if any(ch in s_check for ch in shell_metachar):
                logger.warning("MCP 配置 args 含非法字符: %s", s[:80])
                return False

        if cmd in ("npx", "uvx"):
            for a in cfg.args:
                s = str(a)
                if s.startswith("-"):
                    continue
                if not pkg_re.match(s):
                    logger.warning("MCP 配置包名含非法字符: %s", s[:80])
                    return False
        elif cmd in ("python", "python3"):
            for a in cfg.args:
                s = str(a)
                if s.startswith("-"):
                    continue
                if "/" in s or "\\" in s or s.endswith(".py"):
                    logger.warning("MCP 配置 python 参数只允许模块名: %s", s[:80])
                    return False
        return True

    def _ensure_parent_dir(self) -> None:
        parent = os.path.dirname(self._config_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)

    def _sensitive_env_key(self, key: str) -> bool:
        """检查环境变量名是否包含敏感关键词。"""
        lower = key.lower()
        return any(kw in lower for kw in ("key", "token", "secret", "password", "auth"))

    def _encrypt_envs(self, env: Dict[str, str]) -> Dict[str, str]:
        """加密 env 字典中的敏感值。"""
        try:
            from src.core.crypto import encrypt_value
        except ImportError:
            return dict(env)
        result = {}
        for k, v in env.items():
            if self._sensitive_env_key(k) and v:
                result[k] = encrypt_value(v)
            else:
                result[k] = v
        return result

    def _decrypt_envs(self, env: Dict[str, str]) -> Dict[str, str]:
        """解密 env 字典中的敏感值。"""
        try:
            from src.core.crypto import decrypt_value
        except ImportError:
            return dict(env)
        result = {}
        for k, v in env.items():
            if self._sensitive_env_key(k) and v:
                try:
                    decrypted = decrypt_value(v)
                    # 解密返回空串但原值非空 → 可能是明文或密钥变更，保留原值
                    if decrypted:
                        result[k] = decrypted
                    else:
                        result[k] = v
                        logger.warning("[MCP] 敏感配置 %s 解密失败，保留原值", k)
                except Exception:
                    result[k] = v
                    logger.warning("[MCP] 敏感配置 %s 解密异常，保留原值", k, exc_info=True)
            else:
                result[k] = v
        return result

    def save_config(self) -> bool:
        """写入 JSON 文件（敏感 env 值自动加密）。"""
        tmp = None
        try:
            self._ensure_parent_dir()
            with self._lock:
                servers = []
                for cfg in self._configs.values():
                    d = cfg.to_dict()
                    d["env"] = self._encrypt_envs(d.get("env", {}))
                    servers.append(d)
            tmp = self._config_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"servers": servers}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._config_path)
            return True
        except Exception:
            logger.exception("保存 MCP 配置失败")
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            return False

    # --------------------- 配置增改 ---------------------
    def add_server(self, config: MCPServerConfig) -> bool:
        """新增/覆盖服务器配置。"""
        if not config.name:
            return False
        with self._lock:
            self._configs[config.name] = config
        return self.save_config()

    def remove_server(self, name: str) -> bool:
        """停止并移除服务器配置。"""
        with self._lock:
            if name not in self._configs:
                return False
            self.stop_server(name)
            del self._configs[name]
        return self.save_config()

    def update_server(
        self,
        name: str,
        enabled: Optional[bool] = None,
        env: Optional[Dict[str, str]] = None,
        args: Optional[List[str]] = None,
        command: Optional[str] = None,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        working_dir: Optional[str] = "__NO_CHANGE__",
        install_hint: Optional[str] = None,
    ) -> bool:
        """部分更新某个服务器配置。"""
        with self._lock:
            cfg = self._configs.get(name)
            if cfg is None:
                return False
            if enabled is not None:
                cfg.enabled = bool(enabled)
            if env is not None:
                cfg.env = dict(env)
            if args is not None:
                cfg.args = list(args)
            if command is not None:
                cfg.command = command
            if display_name is not None:
                cfg.display_name = display_name
            if description is not None:
                cfg.description = description
            if working_dir != "__NO_CHANGE__":
                cfg.working_dir = working_dir
            if install_hint is not None:
                cfg.install_hint = install_hint
        return self.save_config()

    def get_config(self, name: str) -> Optional[MCPServerConfig]:
        with self._lock:
            return self._configs.get(name)

    # --------------------- 占位符替换 ---------------------
    def _resolve_placeholder(self, value: Any) -> Any:
        if isinstance(value, str):
            if WORK_DIR_PLACEHOLDER in value:
                value = value.replace(WORK_DIR_PLACEHOLDER, self._work_dir)
            return value
        if isinstance(value, list):
            return [self._resolve_placeholder(v) for v in value]
        if isinstance(value, dict):
            return {k: self._resolve_placeholder(v) for k, v in value.items()}
        return value

    def _build_client(self, name: str) -> MCPClient:
        cfg = self._configs[name]
        args: List[str] = [self._resolve_placeholder(a) if isinstance(a, str) else a for a in cfg.args]
        env = _mcp_sanitized_env()
        blocked_env_keys = {
            "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_PROFILE",
            "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH",
            "PYTHONPATH", "PATH", "HOME", "PWD",
            "NODE_OPTIONS",  # --require 可注入任意 JS 代码
        }
        for k, v in cfg.env.items():
            key = str(k).upper()
            if key in blocked_env_keys:
                logger.warning("MCP 配置 env 拒绝危险键: %s", k)
                continue
            resolved = self._resolve_placeholder(v) if isinstance(v, str) else v
            if isinstance(resolved, str) and API_KEY_PLACEHOLDER in resolved:
                # 占位符还没被用户替换 -> 跳过这个 env，避免服务器拿到无效值
                continue
            env[str(k)] = str(resolved)
        # Bug 6 修复: 在传入 _resolve_working_dir 之前，先解析 {WORK_DIR} 等占位符
        raw_wd = self._resolve_placeholder(cfg.working_dir) if isinstance(cfg.working_dir, str) else cfg.working_dir
        working_dir = self._resolve_working_dir(raw_wd)
        return MCPClient(
            server_name=name,
            command=cfg.command,
            args=args,
            env=env,
            working_dir=working_dir,
        )

    def _resolve_working_dir(self, cfg_working_dir: Optional[str]) -> str:
        """解析并校验 MCP 服务器的 working_dir，确保其位于项目工作目录（沙箱）内。

        - 如果 cfg_working_dir 为 None，直接返回项目工作目录。
        - 相对路径先与 self._work_dir 拼接，再用 os.path.realpath 归一化。
        - 绝对路径直接用 os.path.realpath 归一化。
        - 若路径逃逸出沙箱，回退到 self._work_dir 并记录警告。
        """
        if not cfg_working_dir:
            return self._work_dir
        try:
            # Bug 6 修复: 相对路径相对于 self._work_dir 解析，而非 os.getcwd()
            if os.path.isabs(cfg_working_dir):
                abs_path = cfg_working_dir
            else:
                abs_path = os.path.join(self._work_dir, cfg_working_dir)
            resolved = os.path.realpath(abs_path)
            work_dir_real = os.path.realpath(self._work_dir)
            if resolved == work_dir_real or resolved.startswith(work_dir_real + os.sep):
                return resolved
            logger.warning(
                "MCP 服务器 working_dir 不在项目目录内，已回退到默认目录: %s (work_dir=%s)",
                cfg_working_dir, self._work_dir,
            )
        except OSError as exc:
            logger.warning("MCP 服务器 working_dir 解析失败，已回退到默认目录: %s -> %s", cfg_working_dir, exc)
        return self._work_dir

    # --------------------- 生命周期 ---------------------
    def start_all(self) -> List[Dict[str, Any]]:
        """启动所有 enabled=True 的服务器。返回每个服务器的启动结果。"""
        results: List[Dict[str, Any]] = []
        for name in list(self._configs.keys()):
            if self._configs[name].enabled:
                results.append(self.start_server(name))
        return results

    def stop_all(self) -> None:
        """停止所有已启动的服务器。"""
        with self._lock:
            names = list(self._clients.keys())
        for name in names:
            self.stop_server(name)

    def start_server(self, name: str, timeout: float = 30.0) -> Dict[str, Any]:
        """启动单个服务器。返回 {"ok": bool, "message": str, "tools": int, "name": str}。

        线程安全：配置检查与 client 构建在锁内完成，阻塞 I/O 在锁外执行，
        避免长时间持锁导致其他操作阻塞。
        """
        with self._lock:
            if name not in self._configs:
                return {"ok": False, "message": f"未找到服务器配置: {name}", "tools": 0, "name": name}
            # 已在运行：直接返回已有状态
            existing = self._clients.get(name)
            if existing is not None and existing.is_running:
                return {
                    "ok": True,
                    "message": "已在运行",
                    "tools": len(existing.available_tools),
                    "name": name,
                }
            # 构建 client（锁内，快速操作）
            client = self._build_client(name)
        # 锁外执行阻塞 I/O，避免长时间持锁导致其他操作阻塞
        info = client.start(timeout=timeout)
        if not info or not info.get("ok"):
            error_msg = (info or {}).get("error", "未知错误")
            return {"ok": False, "message": error_msg, "tools": 0, "name": name}
        with self._lock:
            self._clients[name] = client
            self._refresh_tools_cache()
        return {
            "ok": True,
            "message": "启动成功",
            "tools": len(client.available_tools),
            "name": name,
            "server_info": info,
        }

    def stop_server(self, name: str) -> Dict[str, Any]:
        with self._lock:
            client = self._clients.pop(name, None)
        if client is None:
            return {"ok": True, "message": "未在运行", "name": name}
        # 锁外执行阻塞 I/O
        try:
            client.stop()
            return {"ok": True, "message": "已停止", "name": name}
        except Exception as exc:
            logger.exception("停止 MCP 服务器 %s 失败", name)
            return {"ok": False, "message": str(exc), "name": name}

    def restart_server(self, name: str) -> Dict[str, Any]:
        self.stop_server(name)
        return self.start_server(name)

    # --------------------- GUI 摘要 ---------------------
    def list_servers(self) -> List[Dict[str, Any]]:
        """返回 GUI 用的服务器列表摘要。"""
        summary: List[Dict[str, Any]] = []
        with self._lock:
            for name, cfg in self._configs.items():
                client = self._clients.get(name)
                running = bool(client and client.is_running)
                tool_count = len(client.available_tools) if client else 0
                summary.append(
                    {
                        "name": name,
                        "display_name": cfg.display_name or name,
                        "description": cfg.description,
                        "enabled": cfg.enabled,
                        "running": running,
                        "tool_count": tool_count,
                        "install_hint": cfg.install_hint,
                    }
                )
        return summary

    def status(self) -> Dict[str, Any]:
        """汇总状态: enabled / running / total_tools。"""
        enabled = 0
        running = 0
        tools = 0
        with self._lock:
            for name, cfg in self._configs.items():
                if cfg.enabled:
                    enabled += 1
                client = self._clients.get(name)
                if client and client.is_running:
                    running += 1
                    tools += len(client.available_tools)
        return {
            "enabled": enabled,
            "running": running,
            "total_tools": tools,
            "total_servers": len(self._configs),
        }

    # --------------------- 工具发现 & 调用 ---------------------
    def _refresh_tools_cache(self) -> None:
        tools: List[Dict[str, Any]] = []
        with self._lock:
            clients = dict(self._clients)
        for name, client in clients.items():
            if not client.is_running:
                continue
            for raw in client.available_tools:
                original_name = raw.get("name", "")
                prefixed = f"{name}_{original_name}"
                tools.append(
                    {
                        "name": prefixed,
                        "server": name,
                        "original_name": original_name,
                        "display_name": raw.get("display_name") or original_name,
                        "description": raw.get("description", ""),
                        "inputSchema": raw.get("inputSchema", {}),
                    }
                )
        with self._lock:
            self._tools_cache = tools

    def get_all_tools(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """聚合所有已启动服务器的工具列表。

        命名规则: serverName_toolName 作为全局唯一工具名。
        """
        if refresh or not self._tools_cache:
            # 从每个服务器重新 list_tools() 一次
            with self._lock:
                clients = list(self._clients.items())
            for name, client in clients:
                if client.is_running:
                    try:
                        client.list_tools()
                    except Exception as exc:
                        logger.warning("刷新 %s 工具列表失败: %s", name, exc)
            self._refresh_tools_cache()
        with self._lock:
            return list(self._tools_cache)

    def call_tool(self, full_tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """调用一个 MCP 工具。full_tool_name = serverName_toolName。"""
        if "_" not in full_tool_name:
            return {
                "ok": False,
                "content": "",
                "error": f"工具名格式错误: {full_tool_name} (应为 serverName_toolName)",
                "isError": True,
                "raw": None,
            }
        # 以最长匹配优先: 先尝试 "serverName_x_toolName" 这类带 '_' 的 name 情况
        server_name, original_name = "", ""
        with self._lock:
            # 从长到短匹配 name 前缀
            candidates = sorted(self._clients.keys(), key=len, reverse=True)
            for name in candidates:
                prefix = name + "_"
                if full_tool_name.startswith(prefix):
                    server_name = name
                    original_name = full_tool_name[len(prefix):]
                    break
            client = self._clients.get(server_name)
        if not server_name:
            return {
                "ok": False,
                "content": "",
                "error": f"未找到对应服务器: {full_tool_name}",
                "isError": True,
                "raw": None,
            }

        if client is None or not client.is_running:
            return {
                "ok": False,
                "content": "",
                "error": f"服务器 {server_name} 未启动",
                "isError": True,
                "raw": None,
            }
        # 锁外执行阻塞 I/O，避免死锁
        return client.call_tool(original_name, arguments or {})

    # --------------------- 便捷属性 ---------------------
    @property
    def config_path(self) -> str:
        return self._config_path

    @property
    def work_dir(self) -> str:
        return self._work_dir

    @property
    def running_servers(self) -> List[str]:
        with self._lock:
            return [name for name, c in self._clients.items() if c.is_running]
