# -*- coding: utf-8 -*-
"""对话历史管理

原则: 完整保存原始消息内容 - 完整还原给 API 以命中缓存

- 用户消息: 完整保存原文 (role=user, content=完整文本)
- 助手消息: 完整保存 reasoning_content + content, 不压缩
- 工具调用: 完整保存工具调用的完整结果
- 新代理切换前, 上下文完整保留
- 重新启动软件时, 自动加载最近一次完整对话

这样 deepseek 的 prompt_cache_hit_tokens 才能命中前缀缓存, 降低成本。
"""
import logging

logger = logging.getLogger(__name__)

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Dict, Optional


def _atomic_write_history(path: Path, payload: Dict) -> bool:
    """原子化写入 history JSON：先写临时文件 -> fsync -> os.replace。

    避免进程在写入中途被杀导致 session 文件残留半拉 JSON（不可恢复）。
    """
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception as _exc:
        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=".history_", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            try:
                os.replace(tmp_name, str(target))
            except OSError:
                shutil.move(tmp_name, str(target))
            return True
        except Exception:
            try:
                os.unlink(tmp_name)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            raise
    except Exception as e:
        print(f"[History] 原子写入失败 ({target}): {e}")
        return False


class ConversationHistory:
    """单项目对话历史 - 完整保留原文"""

    # 消息列表最大长度，防止无限增长导致内存/磁盘问题
    _MAX_MESSAGES = 2000

    def __init__(self, history_dir: Path, skip_load: bool = False):
        self.dir = Path(history_dir)
        self._lock = threading.RLock()  # 保护 messages / tool_call_counter 的并发读写
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        # 完整消息列表, 每项: {"role": ..., "content": ..., "reasoning_content": ..., ...}
        self.messages: List[Dict] = []
        self.current_session_file = self.dir / f"session_{time.strftime('%Y%m%d_%H%M%S')}.json"
        self.tool_call_counter = 0  # 本轮会话中的工具调用次数 (用于新代理判定)
        self._dirty = False  # 标记是否有未保存的修改
        self._dirty_version = 0  # P0-2: 单调递增的脏版本号，用于检测快照后的并发修改
        self._last_save_time = 0.0  # 上次保存时间戳
        self._save_interval = 2.0  # 最小保存间隔（秒），避免频繁磁盘写入
        if not skip_load:
            try:
                self._load_latest()
            except Exception as e:
                print(f"[History] 加载最新 session 失败: {e}")
                # 异常时仍保持空状态，防止影响上层流程
                with self._lock:
                    self.messages = []
                    self.tool_call_counter = 0

    # ---------- 持久化 ----------
    def _load_latest(self):
        files = sorted(self.dir.glob("session_*.json"))
        if not files:
            return
        # 取最近一个 session（从后向前找第一个能解析的，避免尾部坏文件直接让 history 变空）
        loaded = False
        latest_path = None
        final_messages: List[Dict] = []
        final_tool_counter = 0
        for candidate in reversed(files):
            latest_path = candidate
            try:
                # 一次性读入，避免文件被外部截断读到空
                raw_bytes = candidate.read_bytes()
                if not raw_bytes:
                    continue
                data = json.loads(raw_bytes.decode("utf-8"))
                if not isinstance(data, dict):
                    continue
                msgs = data.get("messages")
                if not isinstance(msgs, list):
                    continue
                # 过滤非 dict 条目，防止下游遍历直接炸
                cleaned: List[Dict] = [m for m in msgs if isinstance(m, dict)]
                final_messages = cleaned
                loaded = True
                # 统计工具调用次数：每个 assistant 消息中的 tool_calls 数组长度之和
                final_tool_counter = 0
                for m in cleaned:
                    if m.get("role") == "assistant":
                        tc = m.get("tool_calls")
                        if isinstance(tc, list):
                            final_tool_counter += len(tc)
                break
            except (OSError, ValueError, json.JSONDecodeError) as e:
                print(f"[History] 解析 session {candidate.name} 失败，尝试前一个: {e}")
                continue
            except Exception as e:
                print(f"[History] 未预期异常: {e}")
                continue

        # 所有共享状态的更新统一在锁内完成
        with self._lock:
            if loaded:
                self.messages = final_messages
                self.tool_call_counter = final_tool_counter
                if latest_path is not None:
                    self.current_session_file = latest_path
            else:
                # 没有可解析的 session 文件 — 记录警告并创建新文件
                # 不批量重命名所有文件为 .corrupted，避免误伤仅部分损坏的旧文件
                if files:
                    print(f"[History] 警告: {len(files)} 个 session 文件无法加载，创建新会话")
                self.messages = []
                self.tool_call_counter = 0
                self.current_session_file = self.dir / f"session_{time.strftime('%Y%m%d_%H%M%S')}.json"

    def replace_messages(self, messages: list):
        """直接替换内存中的消息列表，不调用 _load_latest；并立刻重算 tool_call_counter。

        调用方（如 load_chat）加载历史后不应立即覆盖原文件。
        线程安全：持锁写入。
        """
        if isinstance(messages, list):
            cleaned = [m for m in messages if isinstance(m, dict)]
        else:
            cleaned = []
        tool_counter = 0
        for m in cleaned:
            if m.get("role") == "assistant":
                tc = m.get("tool_calls")
                if isinstance(tc, list):
                    tool_counter += len(tc)
        with self._lock:
            self.messages = cleaned
            self.tool_call_counter = tool_counter
            self._dirty = True
            self._dirty_version += 1

    def set_current_session_file(self, filepath: str):
        """将 current_session_file 指向指定的聊天文件路径（必须位于 history_dir 下）。
        P0-4 修复: 加锁保护 current_session_file 的读写，防止并发写入到错误文件。
        """
        try:
            p = Path(filepath).resolve()
            root = Path(self.dir).resolve()
            if not str(p).startswith(str(root) + os.sep) and p != root:
                print(f"[History] set_current_session_file({filepath!r}) 拒绝: 路径必须在 history_dir 下")
                return
            p.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                self.current_session_file = p
                self._dirty = True  # 切换文件后标记脏，确保下次保存写入新文件
                self._dirty_version += 1
        except Exception as e:
            print(f"[History] set_current_session_file({filepath!r}) 失败: {e}")

    def save(self):
        """实时保存: 把完整 messages 写入 JSON（原子化）。

        线程安全：对 messages 持锁读取后才写入。
        优化：使用保存间隔防止频繁磁盘写入，添加消息数量上限保护。
        P0-2 修复: 使用版本号机制防止 _dirty 标志竞态导致消息丢失。
        """
        try:
            now = time.time()
            # P0-2 修复: 在锁内完成所有状态检查和快照，同时记录 dirty 版本号
            # P0-7 修复: 持锁覆盖整个写盘过程，序列化并发 save，保证最后写者
            # 持有最新快照，避免旧快照在线程交错下覆盖新数据。
            with self._lock:
                # 防抖：如果距离上次保存不足 _save_interval 秒且无新脏数据，跳过
                if not self._dirty and (now - self._last_save_time) < self._save_interval:
                    return
                if not isinstance(self.messages, list):
                    self.messages = []
                # 消息数量上限保护：超过上限时截断最早的消息
                # 只截断磁盘写入的副本，不修改内存中的 self.messages（B-08）
                snapshot = list(self.messages[-self._MAX_MESSAGES:])
                # 记录快照时 current_session_file（在锁内读取，防止并发修改）
                target_file = self.current_session_file
                # 记录保存前的 dirty 版本号
                save_version = self._dirty_version

                payload = {"messages": snapshot, "saved_at": time.time()}
                ok = _atomic_write_history(target_file, payload)
                if not ok:
                    # 回退也使用原子写入（临时文件+rename），避免进程崩溃导致会话文件损坏
                    import tempfile as _tmp
                    try:
                        target_file.parent.mkdir(parents=True, exist_ok=True)
                        fd, tmp_path = _tmp.mkstemp(
                            dir=str(target_file.parent),
                            prefix=".history_",
                            suffix=".tmp",
                        )
                        try:
                            with os.fdopen(fd, "w", encoding="utf-8") as f:
                                json.dump(payload, f, ensure_ascii=False, indent=2)
                            os.replace(tmp_path, str(target_file))
                        except Exception:
                            try:
                                os.unlink(tmp_path)
                            except OSError:
                                pass
                            raise
                    except Exception as e2:
                        print(f"[History] fallback 写入也失败: {e2}")
                        return  # 写入失败，不更新 _dirty，下次重试

                # P0-2 修复: 在锁内更新时间戳和脏标志，使用版本号精确检测并发修改
                self._last_save_time = time.time()
                # 只有版本号未变（快照后没有新消息）才清除 _dirty
                # 如果期间有 add_xxx 调用，_dirty_version 已递增，保持 _dirty=True
                if self._dirty_version == save_version:
                    self._dirty = False
                # 如果版本号不同，说明有新消息追加，保持 _dirty=True 以便下次保存
        except Exception as e:
            print(f"[History] 保存失败: {e}")

    # ---------- 消息操作 (完整保存) ----------
    def _trim_messages(self) -> None:
        """内存中消息上限裁剪，防止长期对话占用无限内存。"""
        if len(self.messages) > self._MAX_MESSAGES:
            self.messages = self.messages[-self._MAX_MESSAGES:]

    def add_user_message(self, content: str) -> Dict:
        """保存完整用户消息（线程安全）。"""
        msg = {"role": "user", "content": str(content or ""), "timestamp": time.time()}
        with self._lock:
            if not isinstance(self.messages, list):
                self.messages = []
            self.messages.append(msg)
            self._trim_messages()
            self._dirty = True
            self._dirty_version += 1
        try:
            self.save()
        except Exception as e:
            print(f"[History] add_user_message 落盘失败: {e}")
        return msg

    def add_assistant_message(self, content: str, reasoning_content: str = "", tool_calls: Optional[List[Dict]] = None) -> Dict:
        """保存完整助手回复（线程安全）。"""
        msg = {
            "role": "assistant",
            "content": str(content or ""),
            "reasoning_content": str(reasoning_content or ""),
            "timestamp": time.time(),
        }
        cleaned_calls: List[Dict] = []
        if isinstance(tool_calls, list) and tool_calls:
            # 过滤非 dict 项，避免下游遍历炸
            cleaned_calls = [c for c in tool_calls if isinstance(c, dict)]
            msg["tool_calls"] = cleaned_calls
        with self._lock:
            if not isinstance(self.messages, list):
                self.messages = []
            self.messages.append(msg)
            self._trim_messages()
            if cleaned_calls:
                self.tool_call_counter += len(cleaned_calls)
            self._dirty = True
            self._dirty_version += 1
        try:
            self.save()
        except Exception as e:
            print(f"[History] add_assistant_message 落盘失败: {e}")
        return msg

    def add_tool_result(self, tool_name: str, result_text: str, tool_call_id: str = "") -> Dict:
        """保存完整工具调用结果（线程安全）。"""
        msg = {
            "role": "tool",
            "name": str(tool_name or "tool"),
            "content": str(result_text or ""),
            "timestamp": time.time(),
        }
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        with self._lock:
            if not isinstance(self.messages, list):
                self.messages = []
            self.messages.append(msg)
            self._trim_messages()
            self._dirty = True
            self._dirty_version += 1
        try:
            self.save()
        except Exception as e:
            print(f"[History] add_tool_result 落盘失败: {e}")
        return msg

    def clear(self):
        """清空当前会话, 新建一个 session 文件（线程安全）。
        P1-3 修复: 在锁内设置 _dirty=True 并递增版本号，确保空列表被保存。
        """
        new_path = self.dir / f"session_{time.strftime('%Y%m%d_%H%M%S')}.json"
        with self._lock:
            self.messages = []
            self.tool_call_counter = 0
            self.current_session_file = new_path
            self._dirty = True
            self._dirty_version += 1
        try:
            self.save()
        except Exception as e:
            print(f"[History] clear/save 失败: {e}")

    def reset_tool_counter(self):
        """新代理开始后, 重置工具计数（线程安全）。"""
        with self._lock:
            self.tool_call_counter = 0

    # ---------- 发送给 API 的完整消息 ----------
    def to_api_messages(self, system_prompt: str, language: str = "zh") -> List[Dict]:
        """完整还原消息给 API, 命中 deepseek 缓存（线程安全：先持锁快照）。

        - system prompt
        - user: 完整原文
        - assistant: 完整 content (reasoning 不放入 content 以确保缓存命中)
        - tool: 完整结果, 以 role=user 形式包装 (因为 deepseek 原生不支持 tool role)

        参数:
            language: 界面语言，用于工具结果包装前缀（B-15）。
        """
        # 对 messages 做一次锁内快照，避免遍历期间被并发修改
        with self._lock:
            if not isinstance(self.messages, list):
                self.messages = []
            snapshot = list(self.messages)

        messages = [{"role": "system", "content": system_prompt, "id": "msg_sys"}]
        for i, m in enumerate(snapshot, 1):
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "tool":
                tc_id = m.get("tool_call_id", "")
                if tc_id:
                    # 原生 function calling: 使用标准 role=tool + tool_call_id
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": content, "id": f"msg_{i}"})
                else:
                    # 文本提取或子代理: 回退到 role=user 包装
                    name = m.get("name", "tool")
                    if language and language.lower().startswith("en"):
                        wrapper = f"[Tool Result - {name}]\n{content}"
                    else:
                        wrapper = f"[工具调用结果 - {name}]\n{content}"
                    messages.append({"role": "user", "content": wrapper, "id": f"msg_{i}"})
            elif role == "assistant":
                msg = {"role": "assistant", "content": content, "id": f"msg_{i}"}
                # DeepSeek V4 要求工具调用后的轮次必须回传 reasoning_content
                rc = m.get("reasoning_content", "")
                tc = m.get("tool_calls")
                if rc:
                    msg["reasoning_content"] = rc
                if tc:
                    msg["tool_calls"] = tc
                messages.append(msg)
            elif role == "user":
                messages.append({"role": "user", "content": content, "id": f"msg_{i}"})
        return messages

    # ---------- 上下文摘要 (仅给新代理启动用) ----------
    def compress_context(self) -> str:
        """仅用于"新代理启动日志/摘要展示"（线程安全：先持锁快照）。

        压缩规则：COT 砍掉，工具调用只写名字，不写内容。
        """
        with self._lock:
            if not isinstance(self.messages, list):
                self.messages = []
            snapshot = list(self.messages[-30:])
        lines = []
        for m in snapshot:
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "tool":
                name = m.get("name", "tool")
                lines.append(f"[工具调用] {name}")
            elif role == "user":
                snippet = content[:160].replace("\n", " ")
                lines.append(f"[用户] {snippet}")
            elif role == "assistant":
                # COT 砍掉，只保留正文第一行
                first_line = content.split("\n")[0][:120]
                lines.append(f"[助手] {first_line}")
        return "\n".join(lines)