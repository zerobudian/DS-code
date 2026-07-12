# -*- coding: utf-8 -*-
"""实时从模型输出中提取 HTML/CSS/JS artifacts 的管理器。

该模块只负责**提取**与**合并**文本内容，不会执行任何 JS。
UI 层应将提取到的 JS 放入 iframe sandbox 中运行。
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

MAX_ARTIFACT_SIZE = 1024 * 1024  # 1 MiB

# 支持的 markdown 代码块语言 -> artifact 类型
_LANGUAGE_MAP = {
    "html": "html",
    "css": "css",
    "javascript": "js",
    "js": "js",
}

# 完整 markdown 代码块：```lang\ncontent\n```
# 闭包 ``` 前允许没有换行（兼容部分模型输出格式），但开包后仍要求换行。
_CODE_BLOCK_RE = re.compile(
    r"```(?:\s*([a-zA-Z0-9_+-]+)\s*)?\n(.*?)\n?```",
    re.DOTALL,
)

# 未闭合的 markdown 代码块（仅在 flush 时兜底使用）
# 使用非贪婪匹配，避免一个匹配消费整个缓冲区
_OPEN_CODE_BLOCK_RE = re.compile(
    r"```(?:\s*([a-zA-Z0-9_+-]+)\s*)?\n(.*?)(?=\n```|$)",
    re.DOTALL,
)

# 完整 HTML 文档（可选 DOCTYPE，忽略大小写）
_HTML_DOC_RE = re.compile(
    r"(?:<!DOCTYPE\s+html\s*>)?\s*<html\b[^>]*>.*?</html>",
    re.IGNORECASE | re.DOTALL,
)

# <style> 与 <script> 标签，忽略大小写
_STYLE_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
_SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)


@dataclass
class Artifact:
    """一个提取到的代码/文档片段。"""

    id: str
    type: str
    title: str
    content: str
    language: str
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        if self.type not in {"html", "css", "js"}:
            raise ValueError(f"不支持的 artifact 类型: {self.type}")

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "content": self.content,
            "language": self.language,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ArtifactManager:
    """从流式模型输出中增量提取 HTML/CSS/JS artifacts。"""

    def __init__(self, max_size: int = MAX_ARTIFACT_SIZE) -> None:
        self._buffer = ""
        self._artifacts: Dict[Tuple[str, int], Artifact] = {}
        self._max_size = max_size
        # 兼容旧版回调：artifact 列表发生变化时通知上层
        self.on_artifact_updated: Optional[Callable[[List[Dict[str, Any]]], None]] = None
        self._last_notified: List[Dict[str, Any]] = []
        # feed/flush 可能被工作线程调用，同时 GUI 在主线程读取，加锁保护。
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------
    def feed(self, delta: str) -> List[Artifact]:
        """追加新的模型输出 token 并增量解析。"""
        with self._lock:
            if delta:
                self._buffer += delta
            self._parse(final=False)
            return list(self._artifacts.values())

    def flush(self) -> List[Artifact]:
        """流式结束时做最终解析（会兜底处理未闭合的代码块）。"""
        with self._lock:
            self._parse(final=True)
            # 修剪 buffer 防止跨轮次累积导致内存泄漏
            # 只保留最后 4KB 用于跨轮连续性检测
            if len(self._buffer) > 4096:
                self._buffer = self._buffer[-4096:]
            return list(self._artifacts.values())

    def get_artifacts(self) -> List[Artifact]:
        """返回当前所有已提取的 artifacts（按发现顺序）。"""
        with self._lock:
            return list(self._artifacts.values())

    def get_html_document(self) -> str:
        """将 html + css + js 合并成一个完整 HTML 文档。

        CSS 放到 ``<head>`` 中的 ``<style>``，JS 放到 ``</body>`` 前的 ``<script>``。
        """
        with self._lock:
            html_parts: List[str] = []
            css_parts: List[str] = []
            js_parts: List[str] = []

            for art in self._artifacts.values():
                if art.type == "html":
                    html_parts.append(art.content)
                elif art.type == "css":
                    css_parts.append(art.content)
                elif art.type == "js":
                    js_parts.append(art.content)

            css_block = (
                "\n".join(f"<style>\n{c}\n</style>" for c in css_parts)
                if css_parts
                else ""
            )
            js_block = (
                "\n".join(f"<script>\n{c}\n</script>" for c in js_parts)
                if js_parts
                else ""
            )

            complete_docs = [c for c in html_parts if re.search(r"<html\b", c, re.IGNORECASE)]
            fragments = [c for c in html_parts if not re.search(r"<html\b", c, re.IGNORECASE)]

            if complete_docs:
                base = complete_docs[0]
                # 其余完整 HTML 文档作为片段注入 body，避免内容丢失
                fragments = complete_docs[1:] + fragments
                body_injection = "\n".join(filter(None, ["\n".join(fragments), js_block]))
                if css_block:
                    base = self._inject_into_head(base, css_block)
                return self._inject_before_close(base, body_injection)

            body_content = "\n".join(filter(None, ["\n".join(fragments), js_block]))
            return (
                "<!DOCTYPE html>\n"
                "<html>\n"
                "<head>\n"
                '<meta charset="utf-8">\n'
                f"{css_block}\n"
                "</head>\n"
                "<body>\n"
                f"{body_content}\n"
                "</body>\n"
                "</html>"
            )

    # ------------------------------------------------------------------
    # 兼容旧版 API（仍被 chat_engine / artifact_preview_panel 使用）
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """清空缓冲区与 artifact 列表。"""
        with self._lock:
            self._buffer = ""
            self._artifacts = {}
            self._last_notified = []

    def has_artifacts(self) -> bool:
        with self._lock:
            return len(self._artifacts) > 0

    @property
    def artifacts(self) -> List[Dict[str, Any]]:
        """返回当前 artifact 列表的 dict 副本（兼容旧 API）。"""
        with self._lock:
            return [a.to_dict() for a in self._artifacts.values()]

    def build_preview_document(self) -> str:
        """旧版合并接口，等价于 get_html_document。"""
        return self.get_html_document()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _truncate(self, content: str) -> str:
        """将单个 artifact 内容限制在最大尺寸内，按 UTF-8 字节截断。"""
        encoded = content.encode("utf-8")
        if len(encoded) <= self._max_size:
            return content
        truncated = encoded[: self._max_size]
        # 避免截断在多字节 UTF-8 字符中间
        while truncated:
            try:
                return truncated.decode("utf-8")
            except UnicodeDecodeError:
                truncated = truncated[:-1]
        return ""

    @staticmethod
    def _inject_before_close(html: str, injection: str) -> str:
        """在 ``</body>`` 或 ``</html>`` 之前插入内容。"""
        if not injection:
            return html
        lowered = html.lower()
        for marker in ("</body>", "</html>"):
            idx = lowered.rfind(marker)
            if idx != -1:
                return f"{html[:idx]}\n{injection}\n{html[idx:]}"
        return f"{html}\n{injection}"

    @staticmethod
    def _inject_into_head(html: str, injection: str) -> str:
        """在 ``</head>`` 之前（或 ``<head>`` 之后）插入内容。"""
        if not injection:
            return html
        lowered = html.lower()
        idx = lowered.find("</head>")
        if idx != -1:
            return f"{html[:idx]}\n{injection}\n{html[idx:]}"
        idx = lowered.find("<head>")
        if idx != -1:
            head_end = html.find(">", idx) + 1
            return f"{html[:head_end]}\n{injection}\n{html[head_end:]}"
        # 既没有 <head> 也没有 </head>，在文档开头插入
        return f"{injection}\n{html}"

    @staticmethod
    def _inside_any(pos: int, intervals: List[Tuple[int, int]]) -> bool:
        for start, end in intervals:
            if start <= pos < end:
                return True
        return False

    def _notify(self) -> None:
        """触发旧版 artifact 更新回调（内容变化时）。"""
        if self.on_artifact_updated is None:
            return
        current = [a.to_dict() for a in self._artifacts.values()]
        # 仅当列表长度或任一 artifact 内容变化时才通知
        if len(current) != len(self._last_notified):
            changed = True
        else:
            changed = any(
                c.get("type") != l.get("type") or c.get("content") != l.get("content")
                for c, l in zip(current, self._last_notified)
            )
        if changed:
            self._last_notified = current
            self.on_artifact_updated([dict(d) for d in current])

    def _parse(self, final: bool) -> None:
        """扫描缓冲区，更新/创建 artifacts。

        同一类型、同一出现位置的 artifact 会被更新而不是新建。
        """
        buffer = self._buffer
        discovered: List[Tuple[int, str, str, str, str]] = []  # start, type, lang, content, title
        code_intervals: List[Tuple[int, int]] = []

        # 1) markdown 代码块
        for match in _CODE_BLOCK_RE.finditer(buffer):
            lang = (match.group(1) or "").strip().lower()
            if lang not in _LANGUAGE_MAP:
                continue
            artifact_type = _LANGUAGE_MAP[lang]
            content = match.group(2)
            code_intervals.append((match.start(), match.end()))
            discovered.append(
                (
                    match.start(),
                    artifact_type,
                    lang,
                    content,
                    f"{artifact_type.upper()} snippet",
                )
            )

        # 2) 兜底：flush 时处理未闭合的代码块
        if final:
            for match in _OPEN_CODE_BLOCK_RE.finditer(buffer):
                lang = (match.group(1) or "").strip().lower()
                if lang not in _LANGUAGE_MAP:
                    continue
                start = match.start()
                # 已被闭合代码块覆盖的部分跳过
                if self._inside_any(start, code_intervals):
                    continue
                artifact_type = _LANGUAGE_MAP[lang]
                content = match.group(2)
                code_intervals.append((start, len(buffer)))
                discovered.append(
                    (
                        start,
                        artifact_type,
                        lang,
                        content,
                        f"{artifact_type.upper()} snippet",
                    )
                )

        # 3) 完整 HTML 文档（排除代码块内部）
        for match in _HTML_DOC_RE.finditer(buffer):
            if self._inside_any(match.start(), code_intervals):
                continue
            discovered.append(
                (
                    match.start(),
                    "html",
                    "html",
                    match.group(0),
                    "HTML document",
                )
            )

        # 4) <style> 标签（排除代码块内部）
        for match in _STYLE_RE.finditer(buffer):
            if self._inside_any(match.start(), code_intervals):
                continue
            discovered.append(
                (
                    match.start(),
                    "css",
                    "css",
                    match.group(1),
                    "CSS block",
                )
            )

        # 5) <script> 标签（排除代码块内部）
        for match in _SCRIPT_RE.finditer(buffer):
            if self._inside_any(match.start(), code_intervals):
                continue
            discovered.append(
                (
                    match.start(),
                    "js",
                    "js",
                    match.group(1),
                    "JS block",
                )
            )

        discovered.sort(key=lambda item: item[0])

        # 按类型出现顺序分配稳定的 key
        type_counters: Dict[str, int] = {}
        new_artifacts: Dict[Tuple[str, int], Artifact] = {}
        now = time.time()

        for _start, artifact_type, language, content, title in discovered:
            content = self._truncate(content)
            index = type_counters.get(artifact_type, 0)
            key = (artifact_type, index)
            type_counters[artifact_type] = index + 1

            existing = self._artifacts.get(key)
            if existing:
                existing.content = content
                existing.language = language
                existing.title = title
                existing.updated_at = now
                new_artifacts[key] = existing
            else:
                new_artifacts[key] = Artifact(
                    id=str(uuid.uuid4()),
                    type=artifact_type,
                    title=title,
                    content=content,
                    language=language,
                    created_at=now,
                    updated_at=now,
                )

        self._artifacts = new_artifacts
        self._notify()
