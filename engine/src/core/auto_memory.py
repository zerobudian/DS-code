# -*- coding: utf-8 -*-
"""自动记忆（Auto-Memory）。

从用户消息中自动提取长期有效的偏好、约束、项目事实，并在后续对话中召回。
本实现为轻量级启发式版本，不依赖 LLM 调用，保证稳定可用；后续可替换为 LLM 抽取。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import json
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from src.core.memory_manager import MemoryManager


# 偏好/事实触发词（中英）
_PREFERENCE_PATTERNS = [
    # 中文偏好
    (r"我(?:喜欢|想要|希望|需要) ?([^。！?\n]{2,80})", "preference"),
    (r"请(?:总是|一直|尽量|优先) ?([^。！?\n]{2,80})", "preference"),
    (r"(?:不要|别|禁止) ?([^。！?\n]{2,80})", "preference"),
    (r"(?:用|使用) ?([^。！?\n]{2,40}) ?(?:作为|做|当|当为)", "preference"),
    (r"默认(?:使用|语言|框架|工具) ?[是为]? ?([^。！?\n]{2,40})", "preference"),
    # 英文偏好
    (r"I (?:like|want|need|prefer) ?([^\.\!\?\n]{2,80})", "preference"),
    (r"(?:always|never) ?([^\.\!\?\n]{2,80})", "preference"),
    (r"use ([^\.\!\?\n]{2,40}) as ", "preference"),
]

_FACT_PATTERNS = [
    # 项目事实
    (r"(?:项目|工程|这个代码库) ?(?:使用|基于|采用) ?([^。！?\n]{2,60})", "fact"),
    (r"(?:技术栈|框架) ?[是为:] ?([^。！?\n]{2,60})", "fact"),
    (r"this project uses? ([^\.\!\?\n]{2,60})", "fact"),
    (r"tech stack[^\w]*([^\.\!\?\n]{2,60})", "fact"),
]

_MAX_MEMORIES = 256
_MEMORY_TTL_DAYS = 90
_DEDUP_SIMILARITY_THRESHOLD = 0.85


def _token_set(text: str) -> set:
    """把文本拆成 token 集合用于去重/召回。"""
    text = re.sub(r"[^\w\s]", " ", text.lower())
    tokens = set(text.split())
    return {t for t in tokens if len(t) >= 2}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class AutoMemory:
    """自动记忆管理器：抽取、存储、召回。"""

    def __init__(self, memory_manager: Optional[MemoryManager] = None,
                 work_dir: Optional[Path] = None):
        self._mm = memory_manager
        self._work_dir = Path(work_dir) if work_dir else None
        self._lock = threading.RLock()
        # 本地缓存：避免频繁读取磁盘
        self._cache: Dict[str, Dict] = {}
        self._cache_loaded = False

    # ------------------------------------------------------------------
    # 存储后端：优先 MemoryManager，否则本地 JSON
    # ------------------------------------------------------------------
    def _storage_path(self) -> Optional[Path]:
        if self._work_dir is None:
            return None
        p = self._work_dir / ".dscode" / "auto_memory.json"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return p

    def _load_entries(self) -> List[Dict]:
        """加载所有自动记忆条目。"""
        with self._lock:
            if self._cache_loaded:
                return list(self._cache.values())
            entries = []
            if self._mm is not None:
                try:
                    raw = self._mm.list_memories(category="auto")
                    for item in raw:
                        if isinstance(item, dict):
                            entries.append(item)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            path = self._storage_path()
            if path and path.exists():
                try:
                    with path.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        entries.extend(data)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            # 去重并缓存
            seen = set()
            cleaned = []
            for e in entries:
                key = e.get("id") or e.get("text", "")
                if not key or key in seen:
                    continue
                seen.add(key)
                cleaned.append(e)
                self._cache[key] = e
            self._cache_loaded = True
            return cleaned

    def _save_entries(self, entries: List[Dict]) -> bool:
        """保存条目。"""
        with self._lock:
            path = self._storage_path()
            if path is None:
                return False
            try:
                with path.open("w", encoding="utf-8") as f:
                    json.dump(entries, f, ensure_ascii=False, indent=2)
            except Exception:
                return False
            if self._mm is not None:
                try:
                    # 同步写入 MemoryManager：先清理旧 auto 分类再写入
                    existing = self._mm.list_memories(category="auto")
                    for old in existing:
                        if isinstance(old, dict) and old.get("id"):
                            self._mm.delete_memory(old["id"], category="auto")
                    for e in entries:
                        self._mm.add_memory(
                            text=e.get("text", ""),
                            category="auto",
                            tags=e.get("tags", []),
                            metadata=e.get("metadata"),
                        )
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            return True

    # ------------------------------------------------------------------
    # 抽取
    # ------------------------------------------------------------------
    @staticmethod
    def extract(text: str) -> List[Dict]:
        """从用户消息中抽取候选记忆。"""
        results = []
        for pattern, kind in _PREFERENCE_PATTERNS + _FACT_PATTERNS:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                captured = m.group(1).strip()
                # 过滤过短或纯标点的片段
                if len(captured) < 3:
                    continue
                # 去除语气词和结尾标点
                captured = re.sub(r"[。！?\.,;!]+$", "", captured)
                if len(captured) < 3:
                    continue
                entry = {
                    "text": captured,
                    "kind": kind,
                    "tokens": sorted(_token_set(captured)),
                    "source": text[:200],
                    "created_at": time.time(),
                }
                results.append(entry)
        return results

    def remember(self, text: str) -> List[Dict]:
        """从文本抽取记忆并持久化，返回新增/更新的条目。

        线程安全：整个"读取-修改-写入"过程在 _lock 内完成，避免 TOCTOU 竞态。
        """
        if not text or not isinstance(text, str):
            return []
        candidates = self.extract(text)
        if not candidates:
            return []

        with self._lock:
            entries = self._load_entries()
            added = []
            for cand in candidates:
                cand_tokens = set(cand.get("tokens", []))
                dup = False
                for e in entries:
                    e_tokens = set(e.get("tokens", []))
                    if _jaccard(cand_tokens, e_tokens) >= _DEDUP_SIMILARITY_THRESHOLD:
                        # 更新命中次数和时间戳
                        e["hit_count"] = e.get("hit_count", 0) + 1
                        e["updated_at"] = time.time()
                        dup = True
                        break
                if dup:
                    continue
                entry = {
                    "id": f"am_{int(time.time()*1000)}_{len(entries)}",
                    "text": cand["text"],
                    "kind": cand["kind"],
                    "tokens": cand["tokens"],
                    "source": cand["source"],
                    "created_at": cand["created_at"],
                    "updated_at": cand["created_at"],
                    "hit_count": 1,
                }
                entries.append(entry)
                added.append(entry)

            # 限制总量 + 清理过期
            cutoff = time.time() - _MEMORY_TTL_DAYS * 86400
            entries.sort(key=lambda x: (x.get("hit_count", 0), x.get("updated_at", 0)), reverse=True)
            entries = [e for e in entries if e.get("updated_at", e.get("created_at", 0)) >= cutoff]
            if len(entries) > _MAX_MEMORIES:
                entries = entries[:_MAX_MEMORIES]

            if not self._save_entries(entries):
                return []
            # 同步更新缓存
            cache: Dict[str, Dict] = {}
            for e in entries:
                key = e.get("id") or e.get("text", "")
                if key:
                    cache[key] = e
            self._cache = cache
        return added

    # ------------------------------------------------------------------
    # 召回
    # ------------------------------------------------------------------
    def recall(self, query: str, top_k: int = 5, min_score: float = 0.15) -> List[Dict]:
        """根据当前查询召回最相关的自动记忆。"""
        if not query or not isinstance(query, str):
            return []
        query = query.strip()
        entries = self._load_entries()
        if not query or not entries:
            return []
        q_tokens = _token_set(query)
        scored = []
        for e in entries:
            e_tokens = set(e.get("tokens", []))
            score = _jaccard(q_tokens, e_tokens)
            if score >= min_score:
                scored.append((score, e))
        scored.sort(key=lambda x: (x[0], x[1].get("hit_count", 0)), reverse=True)
        return [
            {
                "text": e["text"],
                "kind": e.get("kind", "unknown"),
                "score": round(s, 3),
                "hit_count": e.get("hit_count", 0),
            }
            for s, e in scored[:top_k]
        ]

    def recall_as_text(self, query: str, top_k: int = 5) -> str:
        """返回可直接注入 system prompt 的记忆文本。"""
        memories = self.recall(query, top_k=top_k)
        if not memories:
            return ""
        lines = ["## 自动记忆（来自历史对话）", ""]
        for m in memories:
            prefix = "[偏好]" if m["kind"] == "preference" else "[事实]"
            lines.append(f"- {prefix} {m['text']}")
        return "\n".join(lines)

    def list_all(self) -> List[Dict]:
        """列出所有记忆。"""
        entries = self._load_entries()
        return [
            {
                "id": e.get("id", ""),
                "text": e.get("text", ""),
                "kind": e.get("kind", "unknown"),
                "hit_count": e.get("hit_count", 0),
                "created_at": e.get("created_at", 0),
            }
            for e in entries
        ]

    def delete(self, memory_id: str) -> bool:
        """删除单条记忆。"""
        with self._lock:
            entries = self._load_entries()
            new_entries = [e for e in entries if e.get("id") != memory_id]
            if len(new_entries) == len(entries):
                return False
            self._save_entries(new_entries)
            self._cache = {e.get("id", e.get("text", "")): e for e in new_entries if e.get("id")}
            return True
