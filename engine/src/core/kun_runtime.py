# -*- coding: utf-8 -*-
"""Kun Runtime — 统一运行时边界模块

Kun 的设计理念：所有 agent 调用都走同一套 runtime，它作为 GUI 与 agent loop
之间的唯一边界，统一管理 session、审批和用量。

本模块实现：
  - KunSession       — 会话数据模型
  - KunRuntime       — 统一运行时（单例模式）

职责：
  1. Session 管理 — 创建、切换、归档、恢复会话
  2. 审批策略 — 统一的工具调用审批流程
  3. 用量追踪 — 跨会话的 token 统计和费用估算
  4. 运行时遥测 — cache hit rate, 工具调用频率, 性能指标
  5. 边界保护 — 确保所有 agent 调用都经过同一套安全检查

参考 Kun 的架构：
  - Kun 是 TypeScript 包，启动 HTTP/SSE 服务作为 boundary
  - 在 Python 中，我们不启动独立服务，而是用单例模式提供相同边界
"""

import logging

logger = logging.getLogger(__name__)

import gzip
import json
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.core.kun_session import KunSession, generate_session_id

# ---------------------------------------------------------------------------
# 定价常量（DeepSeek 官方定价，单位：CNY / token）
# ---------------------------------------------------------------------------
# Pro:  input=2.0, cached=0.25, output=6.0  CNY / 百万 token
# Flash: input=1.0, cached=0.1,  output=2.0  CNY / 百万 token
PRICING = {
    "pro": {
        "input": 2.0 / 1_000_000,
        "cached": 0.25 / 1_000_000,
        "output": 6.0 / 1_000_000,
    },
    "flash": {
        "input": 1.0 / 1_000_000,
        "cached": 0.1 / 1_000_000,
        "output": 2.0 / 1_000_000,
    },
}

# ---------------------------------------------------------------------------
# 安全工具列表 — 只读、无副作用的工具
# 这些工具在 "auto" 审批策略下会被自动批准
# ---------------------------------------------------------------------------
SAFE_TOOLS: set = {
    # 文件只读
    "read_file", "list_files",
    # 代码搜索
    "search_code", "find_symbol", "find_files", "find_todo",
    "find_fixme",
    # 代码分析
    "analyze_file", "find_imports", "show_call_chain",
    "code_metrics", "show_dependencies",
    "project_brief", "file_intel", "find_related",
    "analyze_symbol", "impact_analysis", "all_symbols",
    "complexity_report", "risk_files",
    # 图片只读
    "view_image", "list_uploaded_images",
    "parse_document_text", "parse_document_images",
    "parse_document",
    # Git 只读
    "git_status", "git_diff", "git_log", "git_branch",
    # 网络只读
    "web_search", "web_fetch", "docs_lookup",
    # 代理只读
    "list_subagents", "get_subagent_results",
    "list_snapshots", "show_plan", "agent_progress",
    # sklii 只读
    "sklii_list",
    # 验证：validate_all 会运行 pytest（执行任意测试代码），不视为安全工具
    "validate_file", "quick_syntax_check",
}

# ---------------------------------------------------------------------------
# 速率限制
# ---------------------------------------------------------------------------
MAX_TOOL_CALLS_PER_MINUTE = 50  # 每分钟最多 50 次工具调用
MAX_ARG_STRING_LENGTH = 10_000  # 参数最大字符串长度


# ============================================================================
# KunRuntime — 统一运行时（单例模式）
# ============================================================================

class KunRuntime:
    """统一运行时边界 — 所有 agent 调用的唯一入口。

    职责：
    1. Session 管理 — 创建、切换、归档、恢复会话
    2. 审批策略 — 统一的工具调用审批流程
    3. 用量追踪 — 跨会话的 token 统计和费用估算
    4. 运行时遥测 — cache hit rate, 工具调用频率, 性能指标
    5. 边界保护 — 确保所有 agent 调用都经过同一套安全检查

    隔离机制说明（MEDIUM-006）：
    - KunRuntime 本身不直接执行本地代码；它作为边界对工具调用进行审批、校验与速率限制，
      实际执行委托给 src.tools.tool_executor.ToolExecutor / src.tools.command_tools。
    - command_tools 中本地代码（python 分支）通过 subprocess.Popen 启动独立子进程执行，
      配合 preexec_fn 设置 CPU/内存/进程数资源限制，并使用固定低权限 PATH 与环境变量白名单。
    - 因此 KunRuntime 已通过“不直接 exec/eval + 委托独立子进程执行”实现隔离，符合安全要求。
    """

    _instance: Optional["KunRuntime"] = None
    _lock: threading.RLock = threading.RLock()

    def __new__(cls) -> "KunRuntime":
        """双检锁单例模式。

        线程安全的双重检查锁定，确保全局只有一个 KunRuntime 实例。
        在锁内完成完整初始化后再赋值 _instance，避免其他线程拿到未完成初始化的实例。
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = True
                    # ── Session 管理 ──
                    # 注意：__new__ 中不使用类型注解赋值，避免 mypy 报非 self 属性类型声明错误；
                    # 类型信息已在类声明与 __init__ 中体现。
                    instance._sessions = {}
                    instance._active_session_id = None
                    instance._session_lock = threading.RLock()
                    # ── 审批策略 ──
                    instance._approval_policy = "auto"
                    instance._pending_approvals = {}
                    instance._approval_lock = threading.RLock()
                    # ── 用量追踪 ──
                    instance._session_usage = {}
                    instance._total_tokens = 0
                    instance._total_cost_cny = 0.0
                    instance._usage_lock = threading.RLock()
                    # ── 遥测 ──
                    instance._telemetry = {
                        "total_sessions": 0,
                        "active_session": None,
                        "total_api_calls": 0,
                        "total_tool_calls": 0,
                        "total_tokens": 0,
                        "total_cost_cny": 0.0,
                        "cache_hit_rate": 0.0,
                        "tool_call_frequency": {},
                        "start_time": time.time(),
                        "error_count": 0,
                        "last_errors": [],
                    }
                    instance._telemetry_lock = threading.RLock()
                    # ── 速率限制 ──
                    instance._tool_call_timestamps = []
                    instance._rate_limit_lock = threading.RLock()
                    cls._instance = instance
        return cls._instance

    def __init__(self):
        """初始化 KunRuntime。

        初始化已移至 __new__ 中完成，此处仅作为兼容保留。
        若单例已初始化则直接返回，避免覆盖累计状态。
        """
        if getattr(self, "_initialized", False):
            return

        # 单例首次创建时的兜底初始化（理论上由 __new__ 完成）
        # ── Session 管理 ──
        self._sessions: Dict[str, KunSession] = {}
        self._active_session_id: Optional[str] = None
        self._session_lock = threading.RLock()
        # ── 审批策略 ──
        self._approval_policy: str = "auto"
        self._pending_approvals: Dict[str, dict] = {}
        self._approval_lock = threading.RLock()
        # ── 用量追踪 ──
        self._session_usage: Dict[str, dict] = {}
        self._total_tokens: int = 0
        self._total_cost_cny: float = 0.0
        self._usage_lock = threading.RLock()
        # ── 遥测 ──
        self._telemetry: dict = {
            "total_sessions": 0,
            "active_session": None,
            "total_api_calls": 0,
            "total_tool_calls": 0,
            "total_tokens": 0,
            "total_cost_cny": 0.0,
            "cache_hit_rate": 0.0,
            "tool_call_frequency": {},
            "start_time": time.time(),
            "error_count": 0,
            "last_errors": [],
        }
        self._telemetry_lock = threading.RLock()
        # ── 速率限制 ──
        self._tool_call_timestamps: List[float] = []
        self._rate_limit_lock = threading.RLock()
        self._initialized = True

    # ========================================================================
    # Session 管理
    # ========================================================================

    def create_session(
        self, session_id: Optional[str] = None, work_dir: Optional[Path] = None
    ) -> KunSession:
        """创建新会话。

        Args:
            session_id: 会话 ID（若不指定则自动生成 UUID）
            work_dir: 工作目录（若不指定则使用当前目录）

        Returns:
            新创建的 KunSession 对象
        """
        if session_id is None:
            session_id = generate_session_id()  # 加密安全随机 ID，防御会话 ID 预测
        if work_dir is None:
            work_dir = Path.cwd()

        with self._session_lock:
            # 若已存在同 ID 会话则抛出错误
            if session_id in self._sessions:
                existing = self._sessions[session_id]
                if existing.status == "active":
                    raise ValueError(
                        f"会话 {session_id} 已存在且处于活跃状态，"
                        f"请先归档或关闭"
                    )

            session = KunSession(session_id=session_id, work_dir=work_dir)
            self._sessions[session_id] = session
            self._active_session_id = session_id

            # 初始化用量追踪
            self._session_usage[session_id] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 0,
                "total_tokens": 0,
                "cost_cny": 0.0,
                "api_calls": 0,
            }

        # 更新遥测
        with self._telemetry_lock:
            self._telemetry["total_sessions"] += 1
            self._telemetry["active_session"] = session_id

        return session

    def get_session(self, session_id: str) -> Optional[KunSession]:
        """获取会话。

        Args:
            session_id: 会话 ID

        Returns:
            KunSession 对象，若不存在则返回 None
        """
        with self._session_lock:
            return self._sessions.get(session_id)

    def get_active_session(self) -> Optional[KunSession]:
        """获取当前活跃会话。"""
        with self._session_lock:
            if self._active_session_id is None:
                return None
            return self._sessions.get(self._active_session_id)

    def activate_session(self, session_id: str) -> KunSession:
        """激活会话（切换当前活跃会话）。

        Args:
            session_id: 要激活的会话 ID

        Returns:
            激活的 KunSession 对象

        Raises:
            ValueError: 会话不存在或已归档
        """
        with self._session_lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ValueError(f"会话 {session_id} 不存在")
            if session.status == "archived":
                raise ValueError(
                    f"会话 {session_id} 已归档，请先调用 restore_session 恢复"
                )
            self._active_session_id = session_id

        with self._telemetry_lock:
            self._telemetry["active_session"] = session_id

        return session

    def fork_session(
        self, source_id: str, new_id: Optional[str] = None, at_message: int = 0
    ) -> KunSession:
        """从现有会话 fork 新会话。

        新会话会复制源会话截至 at_message 的所有消息。

        Args:
            source_id: 源会话 ID
            new_id: 新会话 ID（若不指定则自动生成）
            at_message: fork 时的消息序号（0-based，不含此条消息）

        Returns:
            新创建的 KunSession 对象

        Raises:
            ValueError: 源会话不存在
        """
        with self._session_lock:
            source = self._sessions.get(source_id)
            if source is None:
                raise ValueError(f"源会话 {source_id} 不存在")

            if new_id is None:
                new_id = f"{source_id}_fork_{generate_session_id()}"

            # 使用 KunSession.fork() 方法委托
            # at_message 是 0-based 的"不含此条消息"的索引：
            #   at_message=0 表示新会话不应包含任何源消息；
            #   at_message>0 时复制源会话的 [0, at_message-1] 闭区间消息。
            if at_message <= 0:
                new_session = source.fork(new_id, 0)
                new_session.messages = []
                new_session.metrics["message_count"] = 0
            else:
                fork_index = at_message - 1
                new_session = source.fork(new_id, fork_index)
            new_session.fork_point = at_message
            self._sessions[new_id] = new_session
            self._active_session_id = new_id

            # 初始化用量追踪
            self._session_usage[new_id] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 0,
                "total_tokens": 0,
                "cost_cny": 0.0,
                "api_calls": 0,
            }

        with self._telemetry_lock:
            self._telemetry["total_sessions"] += 1
            self._telemetry["active_session"] = new_id

        return new_session

    def archive_session(self, session_id: str, archive_dir: Optional[Path] = None) -> str:
        """归档会话，将序列化数据压缩保存。

        Args:
            session_id: 要归档的会话 ID
            archive_dir: 归档目录（默认为 work_dir/.dscode/archives）

        Returns:
            归档文件的路径字符串

        Raises:
            ValueError: 会话不存在
        """
        with self._session_lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ValueError(f"会话 {session_id} 不存在")

            if archive_dir is None:
                archive_dir = session.work_dir / ".dscode" / "archives"
            archive_dir = Path(archive_dir)
            archive_dir.mkdir(parents=True, exist_ok=True)

            archive_path = archive_dir / f"{session_id}.json.gz"
            data = json.dumps(session.to_dict(), ensure_ascii=False, indent=2)
            try:
                with gzip.open(archive_path, "wt", encoding="utf-8") as f:
                    f.write(data)
            except OSError:
                # 写盘失败：清理半成品文件，不改状态
                try:
                    archive_path.unlink()
                except OSError:
                    pass
                raise

            # 写盘成功后再置位，避免写盘失败导致会话卡死
            session.status = "archived"
            session.archived_at = time.time()

            # 如果归档的是活跃会话，清除活跃状态
            if self._active_session_id == session_id:
                self._active_session_id = None

        return str(archive_path)

    def restore_session(
        self, session_id: str, work_dir: Optional[Path] = None
    ) -> Optional[KunSession]:
        """从归档文件恢复会话。

        会查找 .dscode/archives/{session_id}.json.gz 文件。

        Args:
            session_id: 要恢复的会话 ID
            work_dir: 工作目录（若不指定则使用当前目录）

        Returns:
            恢复的 KunSession 对象，若归档文件不存在则返回 None
        """
        if work_dir is None:
            work_dir = Path.cwd()
        work_dir = Path(work_dir)

        archive_path = work_dir / ".dscode" / "archives" / f"{session_id}.json.gz"
        if not archive_path.exists():
            # 也尝试在会话原工作目录查找
            with self._session_lock:
                existing = self._sessions.get(session_id)
                if existing and existing.status == "archived":
                    archive_path = (
                        existing.work_dir / ".dscode" / "archives" / f"{session_id}.json.gz"
                    )

        if not archive_path.exists():
            return None

        try:
            with gzip.open(archive_path, "rt", encoding="utf-8") as f:
                data = json.loads(f.read())
            session = KunSession.from_dict(data)
            session.status = "active"
            session.archived_at = None

            with self._session_lock:
                self._sessions[session_id] = session
                self._active_session_id = session_id

                # 如果用量追踪不存在则初始化
                if session_id not in self._session_usage:
                    self._session_usage[session_id] = {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "cache_hit_tokens": 0,
                        "cache_miss_tokens": 0,
                        "total_tokens": 0,
                        "cost_cny": 0.0,
                        "api_calls": 0,
                    }

            with self._telemetry_lock:
                self._telemetry["active_session"] = session_id

            return session
        except (OSError, gzip.BadGzipFile, json.JSONDecodeError, KeyError):
            return None

    def list_sessions(self) -> List[dict]:
        """列出所有会话及其状态。

        Returns:
            包含每个会话摘要信息的字典列表
        """
        # P2-005 修复: list_sessions() 同时持有 _session_lock 和 _usage_lock,
        # 避免 _session_usage 被 record_usage() 并发修改导致数据竞争
        with self._session_lock, self._usage_lock:
            result = []
            for sid, session in self._sessions.items():
                usage = self._session_usage.get(sid, {})
                result.append({
                    "session_id": sid,
                    "work_dir": str(session.work_dir),
                    "status": session.status,
                    "is_active": sid == self._active_session_id,
                    "created_at": session.created_at,
                    "archived_at": session.archived_at,
                    "message_count": len(session.messages),
                    "parent_id": session.parent_id,
                    "total_tokens": usage.get("total_tokens", 0),
                    "cost_cny": usage.get("cost_cny", 0.0),
                    "api_calls": usage.get("api_calls", 0),
                })
            return result

    # ========================================================================
    # 审批策略
    # ========================================================================

    def set_approval_policy(self, policy: str):
        """设置审批策略。

        Args:
            policy: "auto" | "ask" | "strict"
                - auto: 只读工具自动批准，写/执行工具需确认
                - ask: 所有工具调用前弹出确认框
                - strict: 所有工具都需确认，且不可跳过

        Raises:
            ValueError: 策略名称无效
        """
        valid_policies = ("auto", "ask", "strict")
        if policy not in valid_policies:
            raise ValueError(
                f"无效的审批策略: {policy}，必须是 {'/'.join(valid_policies)} 之一"
            )
        self._approval_policy = policy

    def get_approval_policy(self) -> str:
        """获取当前审批策略。"""
        return self._approval_policy

    def request_approval(self, tool_name: str, tool_args: dict) -> bool:
        """请求工具调用审批。

        根据当前审批策略决定：
        - auto: 安全工具自动批准，危险工具需确认
        - ask: 所有工具都需确认
        - strict: 所有工具都需确认，且不可跳过（外部需实现确认逻辑）

        Args:
            tool_name: 工具名称
            tool_args: 工具参数

        Returns:
            True 表示批准，False 表示拒绝
        """
        policy = self._approval_policy

        if policy == "auto":
            # 安全工具自动批准
            if self.is_safe_tool(tool_name):
                return True
            # 非安全工具需确认（本方法返回 False，由外部 GUI 处理）
            approval_id = generate_session_id()
            with self._approval_lock:
                self._pending_approvals[approval_id] = {
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "timestamp": time.time(),
                    "resolved": False,
                }
            # 返回 False 表示需要外部确认
            return False

        elif policy == "ask":
            # 所有工具都需确认
            approval_id = generate_session_id()
            with self._approval_lock:
                self._pending_approvals[approval_id] = {
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "timestamp": time.time(),
                    "resolved": False,
                }
            return False

        elif policy == "strict":
            # 所有工具都需确认（不可跳过）
            approval_id = generate_session_id()
            with self._approval_lock:
                self._pending_approvals[approval_id] = {
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "timestamp": time.time(),
                    "resolved": False,
                    "strict": True,
                }
            return False

        # 默认拒绝
        return False

    def resolve_approval(self, approval_id: str, approved: bool):
        """解析待审批的请求。

        Args:
            approval_id: 审批 ID
            approved: 是否批准
        """
        with self._approval_lock:
            if approval_id in self._pending_approvals:
                self._pending_approvals[approval_id]["resolved"] = True
                self._pending_approvals[approval_id]["approved"] = approved

    def get_pending_approvals(self) -> List[dict]:
        """获取所有未解析的审批请求。"""
        with self._approval_lock:
            return [
                {"id": aid, **info}
                for aid, info in self._pending_approvals.items()
                if not info.get("resolved", False)
            ]

    def is_safe_tool(self, tool_name: str) -> bool:
        """判断工具是否安全（只读、无副作用）。

        Args:
            tool_name: 工具名称

        Returns:
            True 表示安全工具
        """
        return tool_name in SAFE_TOOLS

    # ========================================================================
    # 用量追踪
    # ========================================================================

    def record_usage(
        self,
        session_id: str,
        usage: dict,
        model: str = "pro",
    ):
        """记录一次 API 调用的 token 用量。

        Args:
            session_id: 会话 ID
            usage: API 返回的 usage 对象，应包含：
                   prompt_tokens, completion_tokens,
                   prompt_cache_hit_tokens, prompt_cache_miss_tokens
            model: 模型名称（完整名如 "deepseek-v4-pro" 或简写 "pro"/"flash"），用于费用估算
        """
        # 提取用量数据（防御非字典输入，避免 AttributeError）
        if not isinstance(usage, dict):
            usage = {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        cache_hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        cache_miss = int(usage.get("prompt_cache_miss_tokens", 0) or 0)

        total_call_tokens = prompt_tokens + completion_tokens

        # 归一化模型名到定价档位：完整名如 "deepseek-v4-flash" -> "flash"，否则默认 "pro"
        model_key = "flash" if model and "flash" in str(model).lower() else "pro"
        pricing = PRICING.get(model_key, PRICING["pro"])
        cost = (
            cache_hit * pricing["cached"]
            + cache_miss * pricing["input"]
            + completion_tokens * pricing["output"]
        )

        # 更新会话级用量
        with self._usage_lock:
            if session_id not in self._session_usage:
                self._session_usage[session_id] = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cache_hit_tokens": 0,
                    "cache_miss_tokens": 0,
                    "total_tokens": 0,
                    "cost_cny": 0.0,
                    "api_calls": 0,
                }
            su = self._session_usage[session_id]
            su["prompt_tokens"] += prompt_tokens
            su["completion_tokens"] += completion_tokens
            su["cache_hit_tokens"] += cache_hit
            su["cache_miss_tokens"] += cache_miss
            su["total_tokens"] += total_call_tokens
            su["cost_cny"] = round(su["cost_cny"] + cost, 6)
            su["api_calls"] += 1

            # 更新全局用量
            self._total_tokens += total_call_tokens
            self._total_cost_cny = round(self._total_cost_cny + cost, 6)

            # 更新遥测中的用量（需同时持有 _telemetry_lock 保证读一致性）
            with self._telemetry_lock:
                self._telemetry["total_api_calls"] += 1
                self._telemetry["total_tokens"] = self._total_tokens
                self._telemetry["total_cost_cny"] = self._total_cost_cny

                # 计算全局缓存命中率
                total_cached = sum(
                    s.get("cache_hit_tokens", 0) for s in self._session_usage.values()
                )
                total_all_prompt = sum(
                    s.get("cache_hit_tokens", 0) + s.get("cache_miss_tokens", 0)
                    for s in self._session_usage.values()
                )
                if total_all_prompt > 0:
                    self._telemetry["cache_hit_rate"] = round(
                        total_cached / total_all_prompt, 4
                    )

    def get_session_usage(self, session_id: str) -> dict:
        """获取会话的用量统计。

        Args:
            session_id: 会话 ID

        Returns:
            包含该会话所有用量指标的字典
        """
        with self._usage_lock:
            if session_id in self._session_usage:
                return dict(self._session_usage[session_id])
            return {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 0,
                "total_tokens": 0,
                "cost_cny": 0.0,
                "api_calls": 0,
            }

    def get_total_usage(self) -> dict:
        """获取全局用量统计。

        Returns:
            包含所有会话汇总的用量指标字典
        """
        with self._usage_lock:
            # 汇总所有会话的 token 统计
            total_prompt = sum(
                s.get("prompt_tokens", 0) for s in self._session_usage.values()
            )
            total_completion = sum(
                s.get("completion_tokens", 0) for s in self._session_usage.values()
            )
            total_cached = sum(
                s.get("cache_hit_tokens", 0) for s in self._session_usage.values()
            )
            total_miss = sum(
                s.get("cache_miss_tokens", 0) for s in self._session_usage.values()
            )
            total_api = sum(
                s.get("api_calls", 0) for s in self._session_usage.values()
            )

            # 全局缓存命中率
            total_all_prompt = total_cached + total_miss
            hit_rate = (
                round(total_cached / total_all_prompt, 4)
                if total_all_prompt > 0
                else 0.0
            )

            return {
                "total_tokens": self._total_tokens,
                "prompt_tokens": total_prompt,
                "completion_tokens": total_completion,
                "cache_hit_tokens": total_cached,
                "cache_miss_tokens": total_miss,
                "total_cost_cny": self._total_cost_cny,
                "total_api_calls": total_api,
                "cache_hit_rate": hit_rate,
                "session_count": len(self._session_usage),
            }

    # ========================================================================
    # 遥测
    # ========================================================================

    def record_tool_call(self, tool_name: str):
        """记录一次工具调用。

        同时进行速率限制检查。

        Args:
            tool_name: 被调用的工具名称
        """
        # 更新工具调用频率
        with self._telemetry_lock:
            self._telemetry["total_tool_calls"] += 1
            freq = self._telemetry["tool_call_frequency"]
            freq[tool_name] = freq.get(tool_name, 0) + 1

        # 记录调用时间戳（用于速率限制）
        with self._rate_limit_lock:
            now = time.time()
            self._tool_call_timestamps.append(now)
            # 清理超过 60 秒的旧记录
            cutoff = now - 60
            self._tool_call_timestamps = [
                ts for ts in self._tool_call_timestamps if ts > cutoff
            ]

    def check_rate_limit(self) -> Tuple[bool, str]:
        """检查是否超过速率限制。

        Returns:
            (is_ok, message) — is_ok 为 True 表示未超限
        """
        with self._rate_limit_lock:
            now = time.time()
            cutoff = now - 60
            self._tool_call_timestamps = [
                ts for ts in self._tool_call_timestamps if ts > cutoff
            ]
            count = len(self._tool_call_timestamps)
            if count > MAX_TOOL_CALLS_PER_MINUTE:
                return False, (
                    f"速率限制：每分钟最多 {MAX_TOOL_CALLS_PER_MINUTE} 次工具调用，"
                    f"当前已有 {count} 次"
                )
            return True, ""

    def record_error(self, error_info: dict):
        """记录错误。

        Args:
            error_info: 错误信息字典，应包含 "code" 和 "message"
        """
        with self._telemetry_lock:
            self._telemetry["error_count"] += 1
            error_info["timestamp"] = time.time()
            self._telemetry["last_errors"].append(error_info)
            # 只保留最近 20 条错误
            if len(self._telemetry["last_errors"]) > 20:
                self._telemetry["last_errors"] = self._telemetry["last_errors"][-20:]

    def get_telemetry(self) -> dict:
        """获取完整遥测数据。

        Returns:
            包含所有运行时指标的字典
        """
        with self._telemetry_lock:
            result = dict(self._telemetry)
            # 计算运行时长
            result["uptime_seconds"] = round(
                time.time() - self._telemetry["start_time"], 2
            )
            # 深拷贝 tool_call_frequency
            result["tool_call_frequency"] = dict(
                self._telemetry["tool_call_frequency"]
            )
            # 深拷贝 last_errors
            result["last_errors"] = list(self._telemetry["last_errors"])
            return result

    # ========================================================================
    # 边界保护
    # ========================================================================

    def validate_tool_call(
        self,
        tool_name: str,
        tool_args: dict,
        is_subagent: bool = False,
        forbid_commands: bool = False,
    ) -> Tuple[bool, str]:
        """验证工具调用是否合法 — 最后一道防线。

        检查：
        1. 工具名是否在 TOOL_NAMES 中
        2. 子代理是否被禁止调用命令类工具
        3. 是否超过速率限制
        4. 参数是否合理

        Args:
            tool_name: 工具名称
            tool_args: 工具参数
            is_subagent: 是否来自子代理
            forbid_commands: 是否禁止命令类工具

        Returns:
            (is_valid, error_message) — is_valid 为 True 表示验证通过
        """
        # 1. 检查工具名是否合法
        #    从 ToolExecutor 导入 TOOL_NAMES 进行校验
        try:
            from src.tools.tool_executor import ToolExecutor  # noqa: F811
            if tool_name not in ToolExecutor.TOOL_NAMES:
                return False, f"未知工具: {tool_name}"
        except (ImportError, AttributeError):
            pass  # ToolExecutor 不可用或缺少 TOOL_NAMES 时跳过工具名校验

        # 2. 命令类工具检查
        #    execute_custom 可运行 shell/python，属于命令执行能力，必须纳入管控。
        command_tools = {
            "run_command", "run_command_stream", "execute_custom", "calculator",
        }
        if forbid_commands and tool_name in command_tools:
            return False, (
                f"子代理禁止调用命令类工具: {tool_name}"
            )

        # 3. 速率限制检查
        with self._rate_limit_lock:
            now = time.time()
            cutoff = now - 60
            self._tool_call_timestamps = [
                ts for ts in self._tool_call_timestamps if ts > cutoff
            ]
            if len(self._tool_call_timestamps) > MAX_TOOL_CALLS_PER_MINUTE:
                return False, (
                    f"速率限制：每分钟最多 {MAX_TOOL_CALLS_PER_MINUTE} "
                    f"次工具调用"
                )
            # 注意：时间戳由 record_tool_call 统一记录，此处不重复追加，
            # 避免每次工具调用被计数两次导致速率限制实际减半。

        # 4. 参数合理性检查
        if not isinstance(tool_args, dict):
            return False, "工具参数必须是字典类型"

        for key, value in tool_args.items():
            if isinstance(value, str):
                # 空字符串检查（某些工具允许空字符串，所以只警告）
                if value == "" and key not in ("cached", "path"):
                    # 允许部分字段为空
                    pass
                # 超长字符串检查
                if len(value) > MAX_ARG_STRING_LENGTH:
                    return False, (
                        f"参数 '{key}' 长度 {len(value)} 超过上限 "
                        f"{MAX_ARG_STRING_LENGTH}"
                    )

        return True, ""

    # ========================================================================
    # 便捷方法
    # ========================================================================

    @classmethod
    def get_instance(cls) -> "KunRuntime":
        """获取（或创建）全局单例实例。

        等同于 KunRuntime()，但语义更清晰。
        """
        return cls()

    @classmethod
    def reset_instance(cls):
        """重置单例（仅用于测试）。

        清除全局实例和所有状态，允许重新初始化。

        P2-005 修复: 清除所有残留状态, 包括 _total_tokens, _total_cost_cny,
        _telemetry, _tool_call_timestamps, _active_session_id。
        """
        with cls._lock:
            if cls._instance is not None:
                inst = cls._instance
                # 在各内部锁保护下清理状态，避免并发数据竞争
                with inst._session_lock:
                    inst._sessions.clear()
                with inst._usage_lock:
                    inst._session_usage.clear()
                with inst._approval_lock:
                    inst._pending_approvals.clear()
                inst._total_tokens = 0
                inst._total_cost_cny = 0.0
                inst._active_session_id = None
                with inst._telemetry_lock:
                    inst._telemetry = {
                        "total_sessions": 0,
                        "active_session": None,
                        "total_api_calls": 0,
                        "total_tool_calls": 0,
                        "total_tokens": 0,
                        "total_cost_cny": 0.0,
                        "cache_hit_rate": 0.0,
                        "tool_call_frequency": {},
                        "start_time": time.time(),
                        "error_count": 0,
                        "last_errors": [],
                    }
                with inst._rate_limit_lock:
                    inst._tool_call_timestamps = []
            cls._instance = None