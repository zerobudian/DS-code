# -*- coding: utf-8 -*-
"""长期全局记忆（Long-term Memory）。

以结构化 Markdown 文件（项目根目录 ``MEMORY.md`` 或 ``.dscode/MEMORY.md``）持久化
项目级知识，包含：项目配置、报错解决方案、环境参数、踩坑经验、技术细节。

提供 ``remember(category, entry)`` / ``recall(query, top_k=3)`` / ``save()`` / ``load()``。
同时支持轻量级语义检测：当聊天中出现报错或“解决了/踩坑”等关键词时，自动抽取并写入。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


DEFAULT_CATEGORIES = [
    "项目配置",
    "报错解决方案",
    "环境参数",
    "踩坑经验",
    "技术细节",
]

_CATEGORY_ALIASES: Dict[str, str] = {
    "config": "项目配置",
    "configuration": "项目配置",
    "配置": "项目配置",
    "error": "报错解决方案",
    "bugfix": "报错解决方案",
    "fix": "报错解决方案",
    "报错": "报错解决方案",
    "错误": "报错解决方案",
    "env": "环境参数",
    "environment": "环境参数",
    "环境": "环境参数",
    "pitfall": "踩坑经验",
    "踩坑": "踩坑经验",
    "坑": "踩坑经验",
    "解决了": "踩坑经验",
    "tech": "技术细节",
    "detail": "技术细节",
    "技术": "技术细节",
}

# 用于自动检测聊天中是否值得写入长期记忆的关键词/模式
_PITFALL_PATTERNS = [
    r"解决了\s*[：:]\s*([^。！\n]{4,200})",
    r"踩坑\s*[：:]\s*([^。！\n]{4,200})",
    r"坑点\s*[：:]\s*([^。！\n]{4,200})",
    r"(?:注意|小心)\s*[：:]\s*([^。！\n]{4,200})",
    r"(?:error|exception)\s*[:：]\s*([^。！\n]{4,200})",
    r"(?:fix|fixed|solve|solved)\s*[:：]\s*([^。！\n]{4,200})",
]

_ERROR_CODE_PATTERNS = [
    r"\b[A-Z][A-Za-z0-9_]*Error\b",
    r"\bExit code \d+\b",
    r"\berror code[: ]*\d+\b",
    r"Traceback \(most recent call last\)",
]


def _normalize_category(category: str) -> str:
    cat = str(category).strip().lower()
    return _CATEGORY_ALIASES.get(cat, category.strip())


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _tokenize(text: str) -> set:
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return {t for t in text.split() if len(t) >= 2}


class LongTermMemory:
    """基于 Markdown 章节的长期记忆。

    每个章节下面维护一个条目列表，每个条目包含时间戳、标签、内容。
    """

    def __init__(self, project_root: Path, prefer_hidden: bool = True) -> None:
        self.project_root: Path = Path(project_root)
        self.prefer_hidden: bool = prefer_hidden
        self._lock: threading.RLock = threading.RLock()
        self._entries: Dict[str, List[Dict[str, str]]] = {cat: [] for cat in DEFAULT_CATEGORIES}
        self._load()

    # ------------------------------------------------------------------
    # 路径选择
    # ------------------------------------------------------------------

    def _memory_path(self) -> Path:
        hidden = self.project_root / ".dscode" / "MEMORY.md"
        root_file = self.project_root / "MEMORY.md"
        if self.prefer_hidden:
            return hidden if hidden.exists() else root_file
        return root_file if root_file.exists() else hidden

    # ------------------------------------------------------------------
    # 解析 / 写入 Markdown
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_markdown(text: str) -> Dict[str, List[Dict[str, str]]]:
        data: Dict[str, List[Dict[str, str]]] = {cat: [] for cat in DEFAULT_CATEGORIES}
        current_category: Optional[str] = None
        current_entry: Optional[Dict[str, str]] = None

        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if not line:
                continue
            # 二级标题作为分类
            if line.startswith("## "):
                cat = line[3:].strip()
                if cat in data:
                    current_category = cat
                    current_entry = None
                continue
            # 三级标题作为条目开始
            if line.startswith("### "):
                if current_category and current_entry:
                    # 非末尾条目：设置 content 字段，确保 recall 搜索能匹配到正文
                    if not current_entry.get("content") and current_entry.get("lines"):
                        current_entry["content"] = "\n".join(current_entry["lines"])
                    data[current_category].append(current_entry)
                title = line[4:].strip()
                current_entry = {"title": title, "lines": [], "tags": "", "timestamp": ""}
                continue
            if current_entry is None:
                continue
            # 元信息行
            if line.startswith("- 时间:"):
                current_entry["timestamp"] = line.split(":", 1)[1].strip()
                continue
            if line.startswith("- 标签:"):
                current_entry["tags"] = line.split(":", 1)[1].strip()
                continue
            if line.startswith("- "):
                # 普通列表项也作为内容
                current_entry["lines"].append(line[2:].strip())
                continue
            current_entry["lines"].append(line)

        if current_category and current_entry:
            # 把解析到的 lines 回填为 content，便于外部读取
            if not current_entry.get("content") and current_entry.get("lines"):
                current_entry["content"] = "\n".join(current_entry["lines"])
            data[current_category].append(current_entry)
        return data

    @staticmethod
    def _render_markdown(data: Dict[str, List[Dict[str, str]]]) -> str:
        lines = ["# MEMORY.md - 长期记忆", ""]
        lines.append("<!-- 本文件由 DSCode 自动维护，也可手动编辑。 -->")
        lines.append("")
        for category in DEFAULT_CATEGORIES:
            lines.append(f"## {category}")
            lines.append("")
            entries = data.get(category, [])
            if not entries:
                lines.append("（暂无记录）")
                lines.append("")
                continue
            for entry in entries:
                title = entry.get("title", "记录")
                lines.append(f"### {title}")
                ts = entry.get("timestamp") or _now_str()
                lines.append(f"- 时间: {ts}")
                tags = entry.get("tags", "")
                if tags:
                    lines.append(f"- 标签: {tags}")
                content = entry.get("content") or "\n".join(entry.get("lines", []))
                if content:
                    for para in content.splitlines():
                        if para.strip():
                            lines.append(f"- {para.strip()}")
                lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def load(self) -> None:
        """从 MEMORY.md 加载长期记忆。"""
        with self._lock:
            path = self._memory_path()
            if not path.exists():
                return
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("读取 MEMORY.md 失败: %s", exc)
                return
            parsed = self._parse_markdown(text)
            self._entries = parsed

    def _load(self) -> None:
        """__init__ 使用的兼容包装。"""
        self.load()

    def save(self) -> Optional[Path]:
        """把当前记忆写入 MEMORY.md（优先 .dscode/MEMORY.md）。"""
        with self._lock:
            path = self._memory_path()
            if self.prefer_hidden and not path.exists():
                path = self.project_root / ".dscode" / "MEMORY.md"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                text = self._render_markdown(self._entries)
                # 原子写入：临时文件 + rename，避免进程崩溃导致 MEMORY.md 损坏
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(path.parent), prefix=".memory_", suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(text)
                    os.replace(tmp_path, str(path))
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
                return path
            except OSError as exc:
                logger.warning("写入 MEMORY.md 失败: %s", exc)
                return None

    # ------------------------------------------------------------------
    # 写入 / 召回 API
    # ------------------------------------------------------------------

    def remember(self, category: str, entry: str,
                 title: Optional[str] = None, tags: Optional[List[str]] = None) -> bool:
        """在指定分类下写入一条长期记忆。

        Args:
            category: 分类名或别名，会归一化为默认分类。
            entry: 记忆正文。
            title: 可选标题；不提供则取正文前 30 字符。
            tags: 可选标签列表。
        """
        category = _normalize_category(category)
        if category not in self._entries:
            category = "技术细节"
        entry_text = str(entry).strip()
        if not entry_text:
            return False
        if title:
            title_text = str(title).strip()
        else:
            title_text = (entry_text[:30] + "...") if len(entry_text) > 30 else entry_text
        tag_str = ", ".join(str(t).strip() for t in (tags or []) if str(t).strip())

        with self._lock:
            # 简单去重：正文完全一致的旧条目更新时间戳
            for old in self._entries[category]:
                if old.get("content", "").strip() == entry_text:
                    old["timestamp"] = _now_str()
                    old["tags"] = tag_str or old.get("tags", "")
                    self.save()
                    return True
            self._entries[category].append({
                "title": title_text,
                "content": entry_text,
                "timestamp": _now_str(),
                "tags": tag_str,
                "lines": [],
            })
            self.save()
        return True

    def recall(self, query: str, top_k: int = 3) -> List[Dict[str, str]]:
        """根据查询召回最相关的记忆条目。

        使用简单的 Jaccard 相似度对 query 与标题+正文做匹配。
        """
        query = str(query or "").strip()
        if not query:
            return []
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []

        scored: List[Tuple[float, Dict[str, str]]] = []
        with self._lock:
            for cat, entries in self._entries.items():
                for e in entries:
                    haystack = f"{e.get('title', '')} {e.get('content', '')} {e.get('tags', '')}"
                    h_tokens = _tokenize(haystack)
                    if not h_tokens:
                        continue
                    inter = len(q_tokens & h_tokens)
                    union = len(q_tokens | h_tokens)
                    score = inter / union if union else 0.0
                    if score > 0.0:
                        scored.append((score, {**e, "category": cat}))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored[:max(1, int(top_k))]]

    def recall_as_text(self, query: str, top_k: int = 3) -> str:
        """把召回结果格式化为可注入 system prompt 的文本。"""
        results = self.recall(query, top_k=top_k)
        if not results:
            return ""
        lines = ["## 长期记忆（来自 MEMORY.md）", ""]
        for r in results:
            cat = r.get("category", "")
            title = r.get("title", "")
            content = r.get("content", "")
            lines.append(f"- [{cat}] {title}: {content}")
        return "\n".join(lines)

    def list_all(self) -> Dict[str, List[Dict[str, str]]]:
        """返回所有记忆条目的副本。"""
        with self._lock:
            return {cat: [dict(e) for e in entries] for cat, entries in self._entries.items()}

    def get_categories(self) -> List[str]:
        return list(DEFAULT_CATEGORIES)

    # ------------------------------------------------------------------
    # 自动抽取
    # ------------------------------------------------------------------

    @staticmethod
    def should_remember(text: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """判断文本是否值得写入长期记忆。

        Returns:
            (是否值得, 推荐分类, 提取内容)
        """
        if not text or not isinstance(text, str):
            return False, None, None
        # 报错模式 -> 报错解决方案
        for pattern in _ERROR_CODE_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                snippet = re.sub(r"\s+", " ", text)[:300]
                return True, "报错解决方案", snippet
        # 踩坑/解决模式
        for pattern in _PITFALL_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                captured = m.group(1).strip()
                # 尽量把整句话也带进来
                full = re.sub(r"\s+", " ", text)[:300]
                return True, "踩坑经验", f"{full}（关键：{captured}）"
        return False, None, None

    def remember_from_message(self, message: str) -> bool:
        """对单条消息做语义检测，命中则自动写入长期记忆。"""
        should, category, content = self.should_remember(message)
        if should and category and content:
            return self.remember(category, content, title="自动抽取")
        return False
