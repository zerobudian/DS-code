# -*- coding: utf-8 -*-
"""Kun Runtime 会话持久化与上下文压缩模块

实现 KunSession 和 ContextCompressor 两个核心类：

KunSession — 增强会话管理：
  1. 分支 (fork)  — 从任意消息索引创建子会话
  2. 归档 (archive) — 压缩并保存到磁盘
  3. 恢复 (restore) — 从归档恢复
  4. 压缩 (compress) — 用 LLM 总结旧消息，保留关键信息
  5. 固定约束 (pin) — 标记某些消息不可压缩

ContextCompressor — 智能上下文压缩：
  1. 保留最近 N 轮对话不变
  2. 对更早的消息，用 LLM 生成结构化摘要
  3. 摘要保留：关键决策、错误与修复、文件变更、未解决事项
  4. 固定消息（pinned）不参与压缩，始终保留
"""

import copy
import gzip
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.sandbox.history import ConversationHistory


def generate_session_id() -> str:
    """生成加密安全的会话 ID。

    使用 secrets.token_urlsafe(32) 替代可预测的 uuid/random，
    防御 CWE-208（可观察的时间差异）与 CWE-338（弱 PRNG）。
    """
    return secrets.token_urlsafe(32)


# ============================================================================
# KunSession — 增强会话管理
# ============================================================================


class KunSession:
    """增强会话，支持分支、归档、压缩和固定约束。

    在 ConversationHistory 的基础上新增：
    - 会话分支 (fork)
    - 会话归档 (archive)
    - 归档恢复 (restore)
    - 上下文压缩 (compress)
    - 消息固定 (pin/unpin)

    属性:
        session_id: 会话唯一标识
        work_dir: 会话文件存储目录
        parent_session: 父会话 ID（来自 fork 时为非空）
        fork_point: 从父会话的第几条消息 fork
        pinned_messages: 不可压缩的消息索引集合
        archived: 是否已归档
        metrics: 会话统计（消息数、工具调用数、token 用量）
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        work_dir: Path = Path("."),
        kun_runtime: Any = None,
    ):
        """初始化 KunSession。

        Args:
            session_id: 会话唯一标识；未提供时使用加密安全的随机 ID
            work_dir: 会话文件存储目录
            kun_runtime: Kun 运行时引用（用于调用 LLM 做摘要压缩等）
        """
        self.session_id = str(session_id) if session_id else generate_session_id()
        self.work_dir = Path(work_dir) if work_dir is not None else Path(".")
        self.work_dir.mkdir(parents=True, exist_ok=True)

        # 内部使用 ConversationHistory 管理消息的持久化
        self._history = ConversationHistory(self.work_dir, skip_load=True)
        self._history.current_session_file = self.work_dir / f"session_{self.session_id}.json"

        # 线程安全锁
        self._lock = threading.RLock()

        # 分支关系
        self.parent_session: Optional[str] = None  # 父会话 ID
        self.fork_point: Optional[int] = None      # 从父会话哪条消息开始 fork

        # 固定消息（不可压缩），索引基于 messages 列表
        self.pinned_messages: Set[int] = set()

        # 归档状态
        self.archived: bool = False

        # 会话生命周期（兼容 kun_runtime 接口）
        self.status: str = "active"
        self.created_at: float = time.time()
        self.archived_at: Optional[float] = None

        # 任意附加元数据
        self.metadata: Dict[str, Any] = {}

        # 会话统计
        self.metrics: Dict[str, Any] = {
            "message_count": 0,
            "tool_call_count": 0,
            "total_tokens_used": 0,
            "created_at": time.time(),
            "last_activity": time.time(),
        }

        # Kun 运行时引用
        self._kun_runtime = kun_runtime

        # 上下文压缩器（懒加载）
        self._compressor: Optional["ContextCompressor"] = None

    # ------------------------------------------------------------------
    # 属性代理 — 方便外部像使用 ConversationHistory 一样使用 KunSession
    # ------------------------------------------------------------------

    @property
    def messages(self) -> List[Dict]:
        """获取消息列表（线程安全：返回快照）。"""
        with self._lock:
            if not isinstance(self._history.messages, list):
                self._history.messages = []
            return list(self._history.messages)

    @messages.setter
    def messages(self, value: List[Dict]):
        """设置消息列表。"""
        with self._lock:
            self._history.messages = list(value) if isinstance(value, list) else []

    @property
    def parent_id(self) -> Optional[str]:
        """父会话 ID（兼容 kun_runtime 接口，等同于 parent_session）。"""
        return self.parent_session

    @parent_id.setter
    def parent_id(self, value: Optional[str]):
        self.parent_session = value

    # ------------------------------------------------------------------
    # 序列化 / 反序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典（用于持久化，兼容 kun_runtime 接口）。"""
        with self._lock:
            return {
                "session_id": self.session_id,
                "work_dir": str(self.work_dir),
                "status": self.status,
                "created_at": self.created_at,
                "archived_at": self.archived_at,
                "parent_id": self.parent_session,
                "parent_session": self.parent_session,
                "fork_point": self.fork_point,
                "messages": list(self._history.messages),
                "pinned": list(self.pinned_messages),
                "metrics": dict(self.metrics),
                "metadata": dict(self.metadata),
            }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], kun_runtime: Any = None) -> "KunSession":
        """从字典反序列化（兼容 kun_runtime 接口）。"""
        session_id = data.get("session_id") or data.get("id") or ""
        if not session_id:
            raise ValueError("KunSession.from_dict: 缺少 session_id")
        work_dir = data.get("work_dir") or os.getcwd()
        session = cls(
            session_id=session_id,
            work_dir=Path(work_dir),
            kun_runtime=kun_runtime,
        )
        session.status = data.get("status", "active")
        session.created_at = data.get("created_at", time.time())
        session.archived_at = data.get("archived_at")
        session.parent_session = data.get("parent_id") or data.get("parent_session")
        session.fork_point = data.get("fork_point")
        # 消息列表通过 messages setter 设置
        msgs = data.get("messages", [])
        if isinstance(msgs, list):
            session.messages = list(msgs)
        # 恢复 pinned
        pinned = data.get("pinned", [])
        if isinstance(pinned, list):
            session.pinned_messages = set(pinned)
        # 恢复 metrics
        m = data.get("metrics", {})
        if isinstance(m, dict):
            session.metrics.update(m)
        # 恢复 metadata
        session.metadata = data.get("metadata", {})
        if not isinstance(session.metadata, dict):
            session.metadata = {}
        return session

    # ------------------------------------------------------------------
    # 委托给 ConversationHistory 的常用方法
    # ------------------------------------------------------------------

    def add_user_message(self, content: str) -> Dict:
        """添加用户消息并更新指标。"""
        # P2-005 修复: _history 调用加锁保护, 防止并发数据竞争
        with self._lock:
            msg = self._history.add_user_message(content)
            self.metrics["message_count"] = len(self._history.messages)
            self.metrics["last_activity"] = time.time()
        return msg

    def add_assistant_message(
        self, content: str, reasoning_content: str = "",
        tool_calls: Optional[List[Dict]] = None
    ) -> Dict:
        """添加助手消息并更新指标。"""
        # P2-005 修复: _history 调用加锁保护
        with self._lock:
            msg = self._history.add_assistant_message(content, reasoning_content, tool_calls)
            self.metrics["message_count"] = len(self._history.messages)
            if isinstance(tool_calls, list) and tool_calls:
                self.metrics["tool_call_count"] += len(tool_calls)
            self.metrics["last_activity"] = time.time()
        return msg

    def add_tool_result(self, tool_name: str, result_text: str) -> Dict:
        """添加工具调用结果并更新指标。"""
        # P2-005 修复: _history 调用加锁保护
        with self._lock:
            msg = self._history.add_tool_result(tool_name, result_text)
            self.metrics["message_count"] = len(self._history.messages)
            self.metrics["last_activity"] = time.time()
        return msg

    def save(self):
        """保存当前会话到磁盘。"""
        # P2-005 修复: _history 调用加锁保护
        with self._lock:
            self._history.save()

    def to_api_messages(self, system_prompt: str, language: str = "zh") -> List[Dict]:
        """转换为 API 消息格式。"""
        # P2-005 修复: _history 调用加锁保护
        with self._lock:
            return self._history.to_api_messages(system_prompt, language=language)

    # ------------------------------------------------------------------
    # 分支 (fork) — 从指定消息索引创建子会话
    # ------------------------------------------------------------------

    def fork(self, new_session_id: str, at_message_index: int) -> "KunSession":
        """从指定消息索引处创建分支。

        新会话共享父会话的 0..at_message_index 消息，
        但后续消息独立。

        Args:
            new_session_id: 新会话 ID（必须唯一）
            at_message_index: 分支点（包含此消息及之前的所有消息）

        Returns:
            新的 KunSession 实例
        """
        if at_message_index < 0:
            raise ValueError(f"分支点索引不能为负数: {at_message_index}")

        # 创建新会话
        child = KunSession(
            session_id=new_session_id,
            work_dir=self.work_dir,
            kun_runtime=self._kun_runtime,
        )

        # 设置分支关系
        child.parent_session = self.session_id
        child.fork_point = at_message_index

        with self._lock:
            # 复制父会话的 0..at_message_index 消息
            parent_msgs = list(self._history.messages)
            if at_message_index >= len(parent_msgs):
                at_message_index = len(parent_msgs) - 1

            # 深拷贝消息，避免共享嵌套可变对象（如 tool_calls 列表）
            copied = []
            for i in range(at_message_index + 1):
                if i < len(parent_msgs):
                    copied.append(copy.deepcopy(parent_msgs[i]))

            child._history.replace_messages(copied)

            # 继承父会话的 pinned 消息（但只继承在分支点内的）
            for idx in self.pinned_messages:
                if idx <= at_message_index:
                    child.pinned_messages.add(idx)

            # 复制部分指标
            child.metrics["message_count"] = len(copied)
            child.metrics["created_at"] = time.time()

        # 保存新会话
        child.save()
        return child

    # ------------------------------------------------------------------
    # 归档 (archive) — 压缩并保存到磁盘
    # ------------------------------------------------------------------

    def archive(self) -> str:
        """归档当前会话。

        1. 将消息列表压缩为 JSON 归档文件
        2. 文件路径：{work_dir}/archive/{session_id}.json.gz
        3. 标记 self.archived = True
        4. 返回归档文件路径

        Returns:
            归档文件的绝对路径
        """
        archive_dir = self.work_dir / ".dscode" / "archives"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"{self.session_id}.json.gz"

        with self._lock:
            # 构建归档数据
            payload = {
                "session_id": self.session_id,
                "parent_session": self.parent_session,
                "fork_point": self.fork_point,
                "pinned_messages": sorted(list(self.pinned_messages)),
                "metrics": dict(self.metrics),
                "messages": list(self._history.messages),
                "archived_at": time.time(),
            }

        # 使用 gzip 压缩写入
        try:
            json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            with gzip.open(archive_path, "wb", compresslevel=6) as f:
                f.write(json_bytes)
        except Exception as e:
            print(f"[KunSession] 归档失败 ({self.session_id}): {e}")
            raise

        with self._lock:
            self.archived = True

        return str(archive_path)

    @classmethod
    def restore(cls, session_id: str, work_dir: Path) -> Optional["KunSession"]:
        """从归档文件恢复会话。

        Args:
            session_id: 会话 ID
            work_dir: 归档目录

        Returns:
            恢复的 KunSession，如果归档不存在则返回 None
        """
        archive_path = Path(work_dir) / ".dscode" / "archives" / f"{session_id}.json.gz"
        if not archive_path.exists():
            return None

        try:
            with gzip.open(archive_path, "rb") as f:
                raw = f.read().decode("utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
        except (OSError, json.JSONDecodeError, gzip.BadGzipFile) as e:
            print(f"[KunSession] 恢复归档失败 ({session_id}): {e}")
            return None

        # 创建新实例
        session = cls(session_id=session_id, work_dir=work_dir)

        # 恢复数据
        session.parent_session = data.get("parent_session")
        session.fork_point = data.get("fork_point")
        session.pinned_messages = set(data.get("pinned_messages", []))
        session.metrics = data.get("metrics", {})
        session.metrics.setdefault("created_at", time.time())
        session.metrics.setdefault("last_activity", time.time())
        session.archived = False  # 恢复后视为未归档（活跃状态）

        # 恢复消息列表
        msgs = data.get("messages", [])
        if isinstance(msgs, list):
            session._history.replace_messages(msgs)

        session.save()
        return session

    # ------------------------------------------------------------------
    # 固定消息 (pin/unpin)
    # ------------------------------------------------------------------

    def pin(self, message_index: int):
        """固定消息，使其在压缩时不被移除。

        Args:
            message_index: 消息在 messages 列表中的索引
        """
        with self._lock:
            if 0 <= message_index < len(self._history.messages):
                self.pinned_messages.add(message_index)

    def unpin(self, message_index: int):
        """取消固定消息。

        Args:
            message_index: 消息在 messages 列表中的索引
        """
        with self._lock:
            self.pinned_messages.discard(message_index)

    # ------------------------------------------------------------------
    # 上下文摘要 (get_context_summary)
    # ------------------------------------------------------------------

    def get_context_summary(self) -> str:
        """获取当前会话的上下文摘要（用于 fork 到子代理时）。

        返回格式：任务目标 + 最近 3 条关键消息的摘要。

        Returns:
            上下文摘要字符串
        """
        with self._lock:
            msgs = list(self._history.messages)

        if not msgs:
            return "(空会话)"

        lines = []
        lines.append(f"[会话 {self.session_id}] 共 {len(msgs)} 条消息")

        # 提取任务目标：找第一条 user 消息
        for m in msgs:
            if m.get("role") == "user":
                content = str(m.get("content", ""))
                goal = content[:200].replace("\n", " ")
                if len(content) > 200:
                    goal += "..."
                lines.append(f"任务目标: {goal}")
                break

        # 最近 3 条关键消息摘要
        recent = msgs[-3:]
        for m in recent:
            role = m.get("role", "")
            content = str(m.get("content", ""))
            if role == "tool":
                name = m.get("name", "tool")
                snippet = content[:120].replace("\n", " ")
                lines.append(f"[工具调用] {name}: {snippet}...")
            elif role == "user":
                snippet = content[:160].replace("\n", " ")
                lines.append(f"[用户] {snippet}")
            elif role == "assistant":
                first_line = content.split("\n")[0][:120]
                lines.append(f"[助手] {first_line}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 上下文压缩 (compress)
    # ------------------------------------------------------------------

    def compress(self, keep_recent: int = 5) -> List[Dict]:
        """压缩当前会话的消息列表。

        使用 ContextCompressor 对消息进行压缩。

        Args:
            keep_recent: 保留最近 N 轮对话不压缩（必须 >=1）

        Returns:
            压缩后的消息列表
        """
        keep_recent = max(1, int(keep_recent))
        if self._compressor is None:
            self._compressor = ContextCompressor(self._kun_runtime)

        with self._lock:
            msgs = list(self._history.messages)
            # 传 pinned_messages 的快照副本，避免并发 pin/unpin 导致迭代异常
            pinned_copy = set(self.pinned_messages)

        compressed = self._compressor.compress(
            messages=msgs,
            keep_recent=keep_recent,
            pinned_indices=pinned_copy,
        )

        # 更新内存中的消息列表
        with self._lock:
            # 合并压缩期间新添加的消息，避免丢失
            current_msgs = list(self._history.messages)
            new_msgs = current_msgs[len(msgs):] if len(current_msgs) > len(msgs) else []
            self._history.replace_messages(compressed + new_msgs)
            # 更新 pinned_messages 索引（压缩后消息列表长度变化）
            self.pinned_messages = set()
            self.metrics["message_count"] = len(self._history.messages)

        self.save()
        return compressed

    def should_compress(self, max_tokens: int = 60000) -> bool:
        """判断当前会话是否需要压缩。

        Args:
            max_tokens: 最大 token 阈值（必须 >=0）

        Returns:
            是否需要压缩
        """
        max_tokens = max(0, int(max_tokens))
        if self._compressor is None:
            self._compressor = ContextCompressor(self._kun_runtime)

        with self._lock:
            msgs = list(self._history.messages)
        return self._compressor.should_compress(msgs, max_tokens)


# ============================================================================
# ContextCompressor — 智能上下文压缩
# ============================================================================


class ContextCompressor:
    """智能上下文压缩器，使用 LLM 摘要或文本截断。

    策略:
    1. 保留最近 N 轮对话（默认 5 轮）不变
    2. 对更早的消息，用 LLM 生成结构化摘要
    3. 摘要保留：关键决策、错误与修复、文件变更、未解决事项
    4. 压缩后的上下文注入为 system message 或第一条 user message

    参考 Kun 的 context compaction 设计：
    - 保留 pinned constraints
    - 对超长工具结果做边界压缩
    - 保留代码、路径、错误、决策和未解决事项
    """

    def __init__(self, kun_runtime: Any = None):
        """初始化压缩器。

        Args:
            kun_runtime: Kun 运行时引用，用于调用 LLM 做摘要。
                         如果为 None，则使用简单的文本截断。
        """
        self._kun_runtime = kun_runtime

    def compress(
        self,
        messages: List[Dict],
        keep_recent: int = 5,
        pinned_indices: Optional[Set[int]] = None,
    ) -> List[Dict]:
        """压缩消息列表。

        Args:
            messages: 完整消息列表
            keep_recent: 保留最近 N 轮对话不压缩
            pinned_indices: 固定消息索引集合（这些消息始终保留）

        Returns:
            压缩后的消息列表（更短）
        """
        if not messages:
            return []

        pinned = pinned_indices or set()
        total = len(messages)

        # 如果消息太少，不压缩
        if total <= keep_recent * 2:
            return list(messages)

        # 分区：最近的消息 + 更早的消息
        # keep_recent 轮对话 ≈ keep_recent * 2 条消息（user + assistant 配对）
        # 但实际中有 tool 消息，所以用更宽松的估算
        keep_count = min(keep_recent * 3, total // 2)
        keep_start = max(0, total - keep_count)

        # 检查 pinned 消息是否在「旧消息」区域；如果是，将它们提升到保留区
        pinned_in_old = sorted([i for i in pinned if i < keep_start])
        if pinned_in_old:
            # 将 pinned 消息移到保留区之前
            keep_start = max(0, pinned_in_old[0])

        # 分离旧消息和保留消息
        old_messages = messages[:keep_start]
        recent_messages = messages[keep_start:]

        # 如果旧消息为空，无需压缩
        if not old_messages:
            return list(recent_messages)

        # 生成摘要
        summary = self._summarize_old_messages(old_messages)

        # 构建压缩后的消息列表
        compressed: List[Dict] = []

        # 第一条消息：注入历史摘要
        compressed.append({
            "role": "system",
            "content": summary,
        })

        compressed.extend(recent_messages)

        return compressed

    def _summarize_old_messages(self, old_messages: List[Dict]) -> str:
        """用 LLM 总结旧消息，生成结构化摘要。

        如果 kun_runtime 不可用（无 LLM 连接），则使用简单的文本截断。

        Args:
            old_messages: 需要被总结的旧消息列表

        Returns:
            结构化摘要字符串
        """
        if not old_messages:
            return "[历史摘要] (无历史消息)"

        # 如果 kun_runtime 可用，尝试调用 LLM 做摘要
        if self._kun_runtime is not None:
            try:
                return self._llm_summarize(old_messages)
            except Exception as e:
                print(f"[ContextCompressor] LLM 摘要失败，回退到文本截断: {e}")

        # 回退：文本截断方式
        return self._simple_truncate_summarize(old_messages)

    def _llm_summarize(self, messages: List[Dict]) -> str:
        """使用 LLM 生成结构化摘要。

        Args:
            messages: 旧消息列表

        Returns:
            LLM 生成的结构化摘要
        """
        # 构建摘要 prompt
        conversation_text = self._messages_to_text(messages)

        prompt = (
            "请对以下对话历史进行结构化摘要，严格按以下格式输出：\n\n"
            "[历史摘要]\n"
            "- 任务目标: {从对话中提取的核心任务目标}\n"
            "- 关键决策:\n"
            "  1. {决策内容及原因}\n"
            "  2. ...\n"
            "- 文件变更:\n"
            "  - {文件路径}: {变更描述}\n"
            "- 错误与修复:\n"
            "  - {错误类型}: {错误描述} → {修复方式}\n"
            "- 未解决事项:\n"
            "  - {待完成的事项}\n"
            "- 工具调用统计: {共 N 次调用，主要使用 tool1, tool2, ...}\n\n"
            "对话历史：\n" + conversation_text
        )

        # 尝试调用 kun_runtime 的 LLM
        try:
            # 兼容不同的 kun_runtime 接口
            if hasattr(self._kun_runtime, "call_llm"):
                result = self._kun_runtime.call_llm(prompt)
            elif hasattr(self._kun_runtime, "chat"):
                result = self._kun_runtime.chat(prompt)
            elif hasattr(self._kun_runtime, "api"):
                api = self._kun_runtime.api
                if hasattr(api, "chat"):
                    result = api.chat(
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=2000,
                    )
                    if isinstance(result, dict):
                        result = result.get("content", "")
                else:
                    raise AttributeError("无法找到可用的 LLM 调用方法")
            else:
                raise AttributeError("kun_runtime 没有可用的 LLM 调用接口")

            if isinstance(result, str) and result.strip():
                return result.strip()
            else:
                raise ValueError("LLM 返回了空结果")
        except Exception as e:
            raise RuntimeError(f"LLM 摘要调用失败: {e}")

    def _simple_truncate_summarize(self, messages: List[Dict]) -> str:
        """简单的文本截断摘要（无 LLM 时的回退方案）。

        压缩规则：
        - COT 直接砍掉
        - 工具调用只写名字，不写内容
        - 保留每条消息的前 200 字符

        Args:
            messages: 旧消息列表

        Returns:
            截断摘要字符串
        """
        lines = ["[历史摘要 - 自动截断]"]

        # 提取任务目标
        for m in messages:
            if m.get("role") == "user":
                content = str(m.get("content", ""))
                goal = content[:200].replace("\n", " ")
                lines.append(f"- 任务目标: {goal}")
                break

        # 统计工具调用 — 只统计名字，不写内容
        tool_count = 0
        tool_names: Dict[str, int] = {}
        for m in messages:
            role = m.get("role", "")
            if role == "tool":
                tool_count += 1
                name = m.get("name", "tool")
                tool_names[name] = tool_names.get(name, 0) + 1
            elif role == "assistant":
                tc = m.get("tool_calls")
                if isinstance(tc, list):
                    tool_count += len(tc)
                    for call in tc:
                        if isinstance(call, dict):
                            name = call.get("tool", "tool")
                            tool_names[name] = tool_names.get(name, 0) + 1

        lines.append(f"- 工具调用统计: 共 {tool_count} 次调用，"
                      f"主要使用 {', '.join(sorted(tool_names, key=tool_names.get, reverse=True)[:8])}")

        # 截断每条消息 — COT 砍掉，工具只写名字
        lines.append("- 消息摘要:")
        for i, m in enumerate(messages):
            role = m.get("role", "")
            content = str(m.get("content", ""))
            if role == "tool":
                name = m.get("name", "tool")
                lines.append(f"  [{i}] 工具调用: {name}")
            elif role == "assistant":
                # COT 砍掉，只保留正文
                if len(content) > 200:
                    content = content[:200] + "[...truncated...]"
                content = content.replace("\n", " ")
                lines.append(f"  [{i}] 助手: {content}")
            elif role == "user":
                if len(content) > 200:
                    content = content[:200] + "[...truncated...]"
                content = content.replace("\n", " ")
                lines.append(f"  [{i}] 用户: {content}")
            else:
                lines.append(f"  [{i}] {role}: {str(content)[:100]}")

        return "\n".join(lines)

    def _messages_to_text(self, messages: List[Dict]) -> str:
        """将消息列表转换为适合 LLM 阅读的文本格式。

        上下文压缩规则：
        - COT（reasoning_content）直接砍掉，不保留
        - 工具调用结果只写工具名字，不写调用内容
        - 用户消息和助手消息保留摘要

        Args:
            messages: 消息列表

        Returns:
            格式化后的文本
        """
        lines = []
        for i, m in enumerate(messages):
            role = m.get("role", "")
            content = str(m.get("content", ""))
            if role == "tool":
                name = m.get("name", "tool")
                lines.append(f"[{i}] 工具调用: {name}")
            elif role == "assistant":
                # 砍掉 COT，只保留正文
                content_snippet = content[:200].replace("\n", " ")
                if len(content) > 200:
                    content_snippet += "...[截断]"
                lines.append(f"[{i}] 助手: {content_snippet}")
            elif role == "user":
                content_snippet = content[:200].replace("\n", " ")
                if len(content) > 200:
                    content_snippet += "...[截断]"
                lines.append(f"[{i}] 用户: {content_snippet}")
            else:
                lines.append(f"[{i}] {role}: {content[:100]}")
        return "\n".join(lines)

    def _estimate_token_count(self, messages: List[Dict]) -> int:
        """粗略估算消息的 token 数。

        中文约 1.5 字符/token，英文约 4 字符/token。
        采用混合估算：2 字符/token 作为折中。

        Args:
            messages: 消息列表

        Returns:
            估算的 token 数量
        """
        total_chars = 0
        for m in messages:
            if not isinstance(m, dict):
                continue
            content = str(m.get("content", ""))
            total_chars += len(content)
            # 也计入 reasoning_content
            reasoning = str(m.get("reasoning_content", ""))
            total_chars += len(reasoning)
            # 计入 tool_calls 的 JSON 序列化
            tool_calls = m.get("tool_calls")
            if isinstance(tool_calls, list):
                try:
                    total_chars += len(json.dumps(tool_calls, ensure_ascii=False))
                except (TypeError, ValueError):
                    total_chars += len(str(tool_calls))
        # 折中估算：2 字符 ≈ 1 token
        return total_chars // 2

    def should_compress(self, messages: List[Dict], max_tokens: int = 60000) -> bool:
        """判断是否需要压缩。

        Args:
            messages: 消息列表
            max_tokens: 最大 token 阈值

        Returns:
            是否需要压缩
        """
        estimated = self._estimate_token_count(messages)
        return estimated > max_tokens


# ============================================================================
# 便捷函数
# ============================================================================


def create_session(
    session_id: Optional[str] = None,
    work_dir: Path = Path("."),
    kun_runtime: Any = None,
) -> KunSession:
    """创建新的 KunSession 实例。

    Args:
        session_id: 会话唯一标识；未提供时自动生成加密安全 ID
        work_dir: 会话文件存储目录
        kun_runtime: Kun 运行时引用

    Returns:
        新的 KunSession 实例
    """
    return KunSession(session_id=session_id, work_dir=work_dir, kun_runtime=kun_runtime)


def restore_session(session_id: str, work_dir: Path) -> Optional[KunSession]:
    """从归档恢复会话。

    Args:
        session_id: 会话 ID
        work_dir: 归档目录

    Returns:
        恢复的 KunSession，如果归档不存在则返回 None
    """
    return KunSession.restore(session_id, work_dir)