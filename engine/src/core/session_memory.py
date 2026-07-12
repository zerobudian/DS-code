# -*- coding: utf-8 -*-
"""会话短时记忆（Session Memory）。

保存当前任务的对话历史、代码改动摘要、终端日志摘要；任务结束时自动归档到
``{sandbox}/.dscode/memories/session_YYYYMMDD_HHMMSS.md``。

新增实时 checkpoint 能力：
  - 关键写操作会标记 _dirty；
  - ChatEngine 每次消息收发后显式调用 save_checkpoint()；
  - 后台定时线程每 5 秒检查 _dirty 并原子化写入
    ``{sandbox}/.dscode/memories/.checkpoint.json``；
  - 崩溃后可通过 load_checkpoint() / has_checkpoint() 恢复未归档的会话状态。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import json
import os
import re
import tempfile
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional



CHECKPOINT_FILENAME = ".checkpoint.json"
CHECKPOINT_VERSION = 1
DEFAULT_CHECKPOINT_INTERVAL = 5.0
# P1-2: 无活动时的延长间隔（秒），减少空闲期的磁盘检查
_IDLE_CHECKPOINT_INTERVAL = 60.0
# P1-2: 连续无 dirty 命中次数达到此阈值后切换到空闲间隔
_IDLE_THRESHOLD = 3

# H-009: checkpoint 中需要过滤的敏感模式（API Key / Bearer Token / JSON api_key）
_SENSITIVE_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"Bearer\s+[^\s]+", re.IGNORECASE),
    re.compile(r'"api_key"\s*:\s*"[^"]*"', re.IGNORECASE),
    re.compile(r"Authorization:\s*Bearer\s+[^\s]+", re.IGNORECASE),
]


class SessionMemory:
    """会话级短时记忆。

    生命周期跟随一次任务（从 send_message 开始到 finished/stopped/error 结束），
    结束后自动持久化到沙箱 ``.dscode/memories/`` 目录下的 Markdown 文件。

    在生命周期内，通过 checkpoint 机制把内存状态原子化落盘，以便进程崩溃后恢复。
    """

    def __init__(self, sandbox_root: Path, max_chat: int = 200,
                 max_changes: int = 200, max_logs: int = 500,
                 enable_auto_checkpoint: bool = True,
                 checkpoint_interval: float = DEFAULT_CHECKPOINT_INTERVAL) -> None:
        self.sandbox_root: Path = Path(sandbox_root)
        self._lock: threading.RLock = threading.RLock()
        self._chat_history: List[Dict[str, str]] = []
        self._code_changes: Deque[Dict[str, object]] = deque(maxlen=max_changes)
        self._terminal_logs: Deque[str] = deque(maxlen=max_logs)
        self._start_time: datetime = datetime.now()
        self._end_time: Optional[datetime] = None
        self._status: str = "running"  # running | finished | stopped | error
        self._error_message: Optional[str] = None
        self._max_chat: int = max(1, int(max_chat))

        # checkpoint 状态
        self._dirty: bool = False
        self._closed: bool = False  # P1-2: 归档/关闭后拒绝写入，防止 stop 后数据丢失
        self._checkpoint_interval: float = max(1.0, float(checkpoint_interval))
        self._checkpoint_stop_event: threading.Event = threading.Event()
        self._checkpoint_thread: Optional[threading.Thread] = None
        if enable_auto_checkpoint:
            self._start_auto_checkpoint_thread()

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _checkpoint_path(self) -> Path:
        return self.sandbox_root / ".dscode" / "memories" / CHECKPOINT_FILENAME

    @staticmethod
    def _is_safe_path(path: Path) -> bool:
        """H-009: 拒绝符号链接，防止通过 symlink 把 checkpoint 写到项目外部。"""
        try:
            # 检查目标文件本身及其所有父目录是否包含符号链接
            for p in [path] + list(path.parents):
                if p.exists() and p.lstat().st_mode & 0o170000 == 0o120000:
                    return False
        except OSError:
            return False
        return True

    @staticmethod
    def _redact_sensitive_value(value: Any) -> Any:
        """H-009: 对字符串中的 API Key / Token 进行脱敏；递归处理 list/dict。"""
        if isinstance(value, str):
            result = value
            for pattern in _SENSITIVE_PATTERNS:
                result = pattern.sub("<REDACTED>", result)
            return result
        if isinstance(value, dict):
            return {k: SessionMemory._redact_sensitive_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [SessionMemory._redact_sensitive_value(item) for item in value]
        return value

    def _sanitize_checkpoint_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """H-009: 保存前过滤敏感字段（聊天内容、改动摘要、终端日志、错误消息）。"""
        sanitized: Dict[str, Any] = {}
        for key, value in data.items():
            if key in ("chat_history", "code_changes", "terminal_logs"):
                sanitized[key] = self._redact_sensitive_value(value)
            elif key == "error_message" and isinstance(value, str):
                sanitized[key] = self._redact_sensitive_value(value)
            else:
                sanitized[key] = value
        return sanitized

    def _start_auto_checkpoint_thread(self) -> None:
        """启动后台定时 checkpoint 线程（daemon）。"""
        if self._checkpoint_thread is not None and self._checkpoint_thread.is_alive():
            return
        self._checkpoint_stop_event.clear()
        self._checkpoint_thread = threading.Thread(
            target=self._auto_checkpoint_loop,
            daemon=True,
            name="SessionMemoryCheckpoint",
        )
        self._checkpoint_thread.start()

    def _auto_checkpoint_loop(self) -> None:
        """P1-2: 定时检查 _dirty 并触发 save_checkpoint。

        动态间隔策略：
        - 有活动（dirty=True）时使用默认间隔（5 秒）
        - 连续 _IDLE_THRESHOLD 次无活动后切换到 _IDLE_CHECKPOINT_INTERVAL（60 秒）
        - 一旦再次有活动，立即恢复默认间隔
        """
        idle_count = 0
        current_interval = self._checkpoint_interval
        while not self._checkpoint_stop_event.wait(timeout=current_interval):
            try:
                with self._lock:
                    dirty = self._dirty
                if dirty:
                    self.save_checkpoint()
                    idle_count = 0
                    current_interval = self._checkpoint_interval
                else:
                    idle_count += 1
                    if idle_count >= _IDLE_THRESHOLD:
                        current_interval = _IDLE_CHECKPOINT_INTERVAL
            except Exception as _exc:
                logger.debug("自动 checkpoint 失败: %s", _exc, exc_info=True)

    def stop_auto_checkpoint(self) -> None:
        """停止后台定时 checkpoint 线程。"""
        self._checkpoint_stop_event.set()
        thread = self._checkpoint_thread
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=1.0)
            except Exception as _exc:
                logger.debug("停止 checkpoint 线程失败: %s", _exc, exc_info=True)
        self._checkpoint_thread = None

    # ------------------------------------------------------------------
    # 写入方法
    # ------------------------------------------------------------------

    def add_chat(self, role: str, content: str) -> None:
        """添加一条对话记录（role: user / assistant / system / tool / reasoning）。
        P1-2 修复: 检查 _closed 标志，归档后拒绝写入。
        """
        if not content or not isinstance(content, str):
            return
        role = str(role).strip().lower() or "unknown"
        with self._lock:
            if self._closed:
                return
            self._chat_history.append({
                "role": role,
                "content": content[:4000],  # 单条截断，避免过长
                "timestamp": datetime.now().isoformat(),
            })
            if len(self._chat_history) > self._max_chat:
                self._chat_history = self._chat_history[-self._max_chat:]
            self._dirty = True

    def add_code_change(self, file: str, diff_summary: str) -> None:
        """添加一次代码改动摘要。
        P1-2 修复: 检查 _closed 标志，归档后拒绝写入。
        """
        if not file or not isinstance(file, str):
            return
        if not diff_summary or not isinstance(diff_summary, str):
            return
        with self._lock:
            if self._closed:
                return
            self._code_changes.append({
                "file": file,
                "summary": diff_summary[:2000],
                "timestamp": datetime.now().isoformat(),
            })
            self._dirty = True

    def add_terminal_log(self, lines: str) -> None:
        """追加终端日志；支持多行字符串。
        P1-2 修复: 检查 _closed 标志，归档后拒绝写入。
        """
        if not lines or not isinstance(lines, str):
            return
        with self._lock:
            if self._closed:
                return
            appended = False
            for line in lines.splitlines():
                line = line.rstrip()
                if not line:
                    continue
                self._terminal_logs.append(line)
                appended = True
            if appended:
                self._dirty = True

    # ------------------------------------------------------------------
    # 状态变更
    # ------------------------------------------------------------------

    def mark_finished(self) -> None:
        with self._lock:
            # Bug 4修复: 已关闭会话不再更新状态
            if self._closed:
                return
            self._status = "finished"
            self._end_time = datetime.now()
            self._dirty = True

    def mark_stopped(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._status = "stopped"
            self._end_time = datetime.now()
            self._dirty = True

    def mark_error(self, message: Optional[str] = None) -> None:
        with self._lock:
            if self._closed:
                return
            self._status = "error"
            self._end_time = datetime.now()
            self._error_message = str(message) if message else None
            self._dirty = True

    # ------------------------------------------------------------------
    # checkpoint 实时保存 / 恢复
    # ------------------------------------------------------------------

    def save_checkpoint(self) -> Optional[Path]:
        """把当前状态原子化写入 ``.checkpoint.json``，成功后清除 _dirty。

        整个写入过程持有 _lock，避免并发 add_chat/code_change/terminal_log 导致
        _dirty 标志丢失或序列化状态不一致。

        返回写入的文件路径；失败时返回 None。
        """
        checkpoint_path = self._checkpoint_path()
        data: Dict[str, Any]

        # 在锁内完成数据快照、文件写入与 dirty 清除，防止并发修改导致状态不一致
        # 或 dirty 被错误覆盖。
        with self._lock:
            # 归档后不再写入checkpoint，避免残留过期数据
            if self._closed:
                return None
            data = {
                "version": CHECKPOINT_VERSION,
                "sandbox_root": str(self.sandbox_root),
                "start_time": self._start_time.isoformat(),
                "end_time": self._end_time.isoformat() if self._end_time else None,
                "status": self._status,
                "error_message": self._error_message,
                "chat_history": list(self._chat_history),
                "code_changes": list(self._code_changes),
                "terminal_logs": list(self._terminal_logs),
                "max_chat": self._max_chat,
                "max_changes": self._code_changes.maxlen,
                "max_logs": self._terminal_logs.maxlen,
                "checkpoint_at": datetime.now().isoformat(),
                "archived": False,
            }

            # H-009: 序列化前过滤敏感字段，避免 API Key 落入磁盘
            data = self._sanitize_checkpoint_data(data)

            # H-009: 拒绝符号链接路径
            if not self._is_safe_path(checkpoint_path):
                logger.warning("写入 checkpoint 失败: 路径包含符号链接")
                return None

            try:
                checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                # 原子化写入：先写临时文件再重命名
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(checkpoint_path.parent),
                    prefix=".checkpoint.tmp-",
                    suffix=".json",
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception:
                    try:
                        Path(tmp_path).unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise
                # H-009: 仅所有者可读写
                os.chmod(tmp_path, 0o600)
                Path(tmp_path).replace(checkpoint_path)
                # replace 后权限通常继承自 tmp_path，但显式再 chmod 一次更稳妥
                try:
                    os.chmod(checkpoint_path, 0o600)
                except OSError:
                    pass
                self._dirty = False
                return checkpoint_path
            except OSError as exc:
                logger.warning("写入 checkpoint 失败: %s", exc)
                return None

    @classmethod
    def has_checkpoint(cls, sandbox_root: Path) -> bool:
        """检查指定沙箱是否存在未归档的 checkpoint。"""
        path = Path(sandbox_root) / ".dscode" / "memories" / CHECKPOINT_FILENAME
        if not path.exists():
            return False
        # H-009: 拒绝通过符号链接读取外部文件
        if not cls._is_safe_path(path):
            logger.warning("读取 checkpoint 失败: 路径包含符号链接")
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return isinstance(data, dict) and not data.get("archived", False)
        except Exception as _exc:
            logger.debug("读取 checkpoint 元信息失败: %s", _exc, exc_info=True)
            return False

    @classmethod
    def load_checkpoint(cls, sandbox_root: Path,
                        enable_auto_checkpoint: bool = True,
                        checkpoint_interval: float = DEFAULT_CHECKPOINT_INTERVAL) -> Optional["SessionMemory"]:
        """从 ``.checkpoint.json`` 恢复会话状态。

        仅恢复未归档的 checkpoint；若 checkpoint 已归档或不存在，返回 None。
        返回的 SessionMemory 会保持与原实例相同的配置（max_chat / 队列容量等），
        并继承未保存的 _dirty=True，以便后续定时线程继续持久化。
        """
        path = Path(sandbox_root) / ".dscode" / "memories" / CHECKPOINT_FILENAME
        if not path.exists():
            return None
        # H-009: 拒绝通过符号链接读取外部文件
        if not cls._is_safe_path(path):
            logger.warning("读取 checkpoint 失败: 路径包含符号链接")
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("读取 checkpoint 失败: %s", exc)
            return None

        if not isinstance(data, dict):
            return None
        if data.get("archived"):
            return None

        try:
            instance = cls.__new__(cls)
            instance.sandbox_root = Path(sandbox_root)
            instance._lock = threading.RLock()
            # 优先使用 checkpoint 中保存的容量；老版本未保存时回退到默认值。
            instance._max_chat = max(1, int(data.get("max_chat", 200) or 200))
            instance._chat_history = list(data.get("chat_history", []))
            # 防止损坏/被篡改的 checkpoint 中 chat_history 超过容量
            if len(instance._chat_history) > instance._max_chat:
                instance._chat_history = instance._chat_history[-instance._max_chat:]
            _default_max_changes = int(data.get("max_changes", 200) or 200)
            _default_max_logs = int(data.get("max_logs", 500) or 500)
            instance._code_changes = deque(
                data.get("code_changes", []),
                maxlen=int(data.get("max_changes", _default_max_changes) or 200),
            )
            instance._terminal_logs = deque(
                data.get("terminal_logs", []),
                maxlen=int(data.get("max_logs", _default_max_logs) or 500),
            )
            instance._start_time = cls._parse_iso(data.get("start_time"))
            instance._end_time = cls._parse_iso(data.get("end_time")) if data.get("end_time") else None
            instance._status = str(data.get("status", "running"))
            instance._error_message = data.get("error_message")

            # 恢复后继续允许自动 checkpoint
            instance._dirty = True  # 刚恢复，需要至少再保存一次确保完整
            instance._checkpoint_interval = max(1.0, float(checkpoint_interval))
            instance._checkpoint_stop_event = threading.Event()
            instance._checkpoint_thread = None
            instance._closed = False
            if enable_auto_checkpoint:
                instance._start_auto_checkpoint_thread()
            return instance
        except Exception as exc:
            logger.warning("解析 checkpoint 失败: %s", exc)
            return None

    @staticmethod
    def _parse_iso(value: Any) -> datetime:
        """解析 ISO 格式时间字符串，失败时返回当前时间。"""
        if isinstance(value, datetime):
            return value
        if not value or not isinstance(value, str):
            return datetime.now()
        try:
            # Python 3.10 的 fromisoformat 可处理带 Z 的字符串
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now()

    # ------------------------------------------------------------------
    # 归档
    # ------------------------------------------------------------------

    def archive(self, force_status: Optional[str] = None) -> Optional[Path]:
        """把当前会话归档为 Markdown 文件，返回生成的文件路径。

        如果 force_status 提供，会覆盖当前状态（用于兼容外部调用）。
        归档完成后删除 checkpoint 文件并停止自动 checkpoint 线程。
        """
        with self._lock:
            # Bug 4修复: 已归档/关闭的会话拒绝重复归档，防止产生重复归档文件
            if self._closed:
                return None
            if force_status:
                self._status = force_status
                if self._end_time is None:
                    self._end_time = datetime.now()

            memories_dir = self.sandbox_root / ".dscode" / "memories"
            try:
                memories_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("创建 memories 目录失败: %s", exc)
                return None

            timestamp = self._end_time or datetime.now()
            filename = timestamp.strftime("session_%Y%m%d_%H%M%S.md")
            target = memories_dir / filename

            # H-009: 拒绝通过符号链接把归档写到外部目录
            if not self._is_safe_path(target):
                logger.warning("写入会话归档失败: 路径包含符号链接")
                return None

            lines: List[str] = [
                f"# Session Memory - {self._status.upper()}",
                "",
                f"- 沙箱: `{self.sandbox_root}`",
                f"- 开始时间: {self._start_time.isoformat()}",
                f"- 结束时间: {self._end_time.isoformat() if self._end_time else 'N/A'}",
                f"- 状态: {self._status}",
            ]
            if self._error_message:
                lines += [
                    "",
                    "## Error",
                    "",
                    self._error_message,
                ]

            # 对话历史
            lines += ["", "## Chat History", ""]
            if self._chat_history:
                for entry in self._chat_history:
                    role = entry.get("role", "unknown")
                    content = entry.get("content", "")
                    lines.append(f"### {role.upper()}")
                    lines.append("")
                    lines.append(content)
                    lines.append("")
            else:
                lines.append("（无对话记录）")

            # 代码改动
            lines += ["", "## Code Changes", ""]
            if self._code_changes:
                for change in self._code_changes:
                    ts = change.get("timestamp", "")
                    file = change.get("file", "")
                    summary = change.get("summary", "")
                    lines.append(f"- [{ts}] `{file}`: {summary}")
            else:
                lines.append("（无代码改动）")

            # 终端日志
            lines += ["", "## Terminal Logs", ""]
            if self._terminal_logs:
                for log in self._terminal_logs:
                    lines.append(f"    {log}")
            else:
                lines.append("（无终端日志）")

            text = "\n".join(lines)
            # H-009: 归档文件同样脱敏，避免 API Key 落入 Markdown
            text = self._redact_sensitive_value(text)
            try:
                target.write_text(text, encoding="utf-8")
                os.chmod(target, 0o600)
            except OSError as exc:
                logger.warning("写入会话归档失败: %s", exc)
                return None

            # 归档成功后删除 checkpoint 并停止后台线程
            checkpoint_path = self._checkpoint_path()
            try:
                if checkpoint_path.exists():
                    # 先写归档标记再删除，避免并发读取到半删除状态
                    try:
                        data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                        if isinstance(data, dict):
                            data["archived"] = True
                            data["archived_at"] = datetime.now().isoformat()
                            checkpoint_path.write_text(
                                json.dumps(data, ensure_ascii=False, indent=2),
                                encoding="utf-8",
                            )
                    except Exception:
                        pass
                    checkpoint_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("删除 checkpoint 失败: %s", exc)

            # 必须在释放锁之后再停止后台线程：stop_auto_checkpoint 会 join 线程，
            # 若线程正在 save_checkpoint 内等待 _lock，会导致当前线程死锁。
            # 先在锁内设置停止事件和 _closed 标志，避免归档后到线程真正停止前又写入新 checkpoint。
            self._checkpoint_stop_event.set()
            self._closed = True  # P1-2: 标记已关闭，拒绝后续写入

        self.stop_auto_checkpoint()
        return target

    # ------------------------------------------------------------------
    # 摘要 / 调试
    # ------------------------------------------------------------------

    def summarize(self) -> str:
        """返回一段可供长期记忆抽取的文本摘要。"""
        with self._lock:
            parts: List[str] = []
            parts.append(f"会话状态: {self._status}")
            if self._error_message:
                parts.append(f"错误: {self._error_message}")
            if self._code_changes:
                files = {c.get("file", "") for c in self._code_changes if c.get("file")}
                parts.append(f"改动文件: {', '.join(sorted(files))}")
            # 取最近几条 user/assistant 对话作为上下文
            recent = [e for e in self._chat_history[-6:] if e.get("role") in ("user", "assistant")]
            if recent:
                parts.append("最近对话:")
                for entry in recent:
                    content = re.sub(r"\s+", " ", entry.get("content", ""))[:200]
                    parts.append(f"  [{entry.get('role')}] {content}")
            return "\n".join(parts)

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def dirty(self) -> bool:
        """当前是否有待持久化的修改。"""
        with self._lock:
            return self._dirty

    def to_dict(self) -> Dict:
        """导出当前状态为 dict（便于测试与序列化）。"""
        with self._lock:
            return {
                "sandbox_root": str(self.sandbox_root),
                "start_time": self._start_time.isoformat(),
                "end_time": self._end_time.isoformat() if self._end_time else None,
                "status": self._status,
                "error_message": self._error_message,
                "chat_count": len(self._chat_history),
                "code_change_count": len(self._code_changes),
                "terminal_log_count": len(self._terminal_logs),
            }
