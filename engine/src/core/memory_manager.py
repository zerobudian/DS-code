"""
长期记忆管理器（Long-term Memory Manager）。

本模块提供一个纯 Python 的长期记忆管理组件，用于在 DSCode 项目中持久化
长期知识（架构决策、已知问题、代码风格、API 约定等）。

主要特性：
    - 每条记忆以独立 JSON 文件存储，配合中央 index.json 提供快速索引；
    - 完整的增删改查、关键词/分类/标签检索、重要度排序；
    - 线程安全（threading.Lock）；
    - 无任何 Qt 或第三方依赖，可直接在 core 中使用。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from src.core.log_sanitizer import sanitize_for_log


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_CATEGORIES = [
    "架构决策",
    "已知问题",
    "代码风格",
    "API约定",
    "其他",
]

ITEMS_DIR_NAME = "items"
INDEX_FILE_NAME = "index.json"

# 记忆内容脱敏时使用较大的 max_length，避免正常长文本被截断。
_MEMORY_SANITIZE_MAX_LEN = 10 * 1024 * 1024


def _safe_importance(value, default: int = 3) -> int:
    """安全地将 importance 转换为 int，非数字值返回默认值"""
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """返回当前时区感知的 ISO 时间字符串（UTC）。"""
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    """把 ISO 字符串解析为带时区的 datetime；失败时回退到 UTC now。"""
    try:
        # Python 3.11+ 原生支持 Z，低版本兼容处理。
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError, AttributeError):
        return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class MemoryManager:
    """长期记忆管理器。

    所有写入操作都会立刻 flush 到磁盘；读取时使用内存中的索引以加速。
    """

    def __init__(self, memory_dir: Path) -> None:
        self.memory_dir: Path = Path(memory_dir)
        self.items_dir: Path = self.memory_dir / ITEMS_DIR_NAME
        self.index_path: Path = self.memory_dir / INDEX_FILE_NAME

        self._lock: threading.RLock = threading.RLock()
        self._items: Dict[str, Dict[str, Any]] = {}  # id -> memory item
        self._categories: List[str] = list(DEFAULT_CATEGORIES)
        self._next_id: int = 1
        self._loaded: bool = False

        # 搜索结果缓存：写入操作会清空，避免重复查询时全量遍历。
        # M-015: 缓存键使用 SHA-256 哈希，防止用户输入构造同键导致缓存投毒。
        self._search_cache_hash: Optional[str] = None
        self._search_cache_result: Optional[List[Dict[str, Any]]] = None

        self._ensure_dirs()
        # 启动时尝试懒加载一次（失败则空集）。
        try:
            self.load()
        except Exception as exc:  # noqa: BLE001
            print(f"[MemoryManager] 初始化加载失败: {exc}")

    # ------------------------------------------------------------------
    # 目录 / 磁盘读写辅助
    # ------------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        try:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            self.items_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[MemoryManager] 创建目录失败: {exc}")

    def _item_path(self, mem_id: str) -> Path:
        return self.items_dir / f"{mem_id}.json"

    _MAX_JSON_SIZE = 10 * 1024 * 1024  # 10 MB

    def _read_json(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            if not path.exists():
                return None
            size = path.stat().st_size
            if size > self._MAX_JSON_SIZE:
                print(f"[MemoryManager] 读取 {path.name} 失败: 文件 {size} 字节超过上限")
                return None
            with path.open("r", encoding="utf-8-sig") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[MemoryManager] 读取 {path.name} 失败: {exc}")
            return None

    def _write_json(self, path: Path, data: Dict[str, Any]) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.flush()
            tmp.replace(path)
            return True
        except (OSError, TypeError) as exc:
            print(f"[MemoryManager] 写入 {path.name} 失败: {exc}")
            return False

    # ------------------------------------------------------------------
    # 索引 / 懒加载
    # ------------------------------------------------------------------

    def save(self) -> None:
        """把索引写入 index.json。"""
        with self._lock:
            index: Dict[str, Any] = {
                "next_id": self._next_id,
                "categories": self._categories,
                "items": {
                    mem_id: {
                        "title": item.get("title"),
                        "category": item.get("category"),
                        "tags": item.get("tags", []),
                        "importance": item.get("importance", 3),
                        "created_at": item.get("created_at"),
                        "updated_at": item.get("updated_at"),
                    }
                    for mem_id, item in self._items.items()
                },
            }
            self._write_json(self.index_path, index)

    def load(self) -> None:
        """从 index.json 加载索引，并补全缺失的条目。"""
        with self._lock:
            self._items.clear()
            self._invalidate_search_cache()
            self._categories = list(DEFAULT_CATEGORIES)
            self._next_id = 1

            index = self._read_json(self.index_path)
            if index is None:
                self._loaded = True
                return

            try:
                self._next_id = int(index.get("next_id", 1) or 1)
            except (TypeError, ValueError):
                self._next_id = 1
            self._categories = list(index.get("categories") or DEFAULT_CATEGORIES)

            index_items = index.get("items") or {}

            # 先把目录中所有 JSON 文件读入；index.json 里没有的也补进来。
            seen_ids: Set[str] = set()
            if self.items_dir.exists():
                for child in self.items_dir.iterdir():
                    if not child.is_file() or child.suffix != ".json":
                        continue
                    mem_id = child.stem
                    data = self._read_json(child)
                    if not isinstance(data, dict):
                        continue
                    data.setdefault("id", mem_id)
                    self._items[mem_id] = data
                    seen_ids.add(mem_id)

            # index.json 中提到但磁盘缺失的条目（防御性忽略。
            for mem_id in index_items:
                if mem_id in seen_ids:
                    continue
                data = self._read_json(self._item_path(mem_id))
                if isinstance(data, dict):
                    data.setdefault("id", mem_id)
                    self._items[mem_id] = data

            # 修正 next_id 不小于最大已有 id 数值。
            if self._items:
                max_numeric = 0
                for mem_id in self._items:
                    try:
                        max_numeric = max(max_numeric, int(mem_id))
                    except ValueError as _exc:
                        logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
                self._next_id = max(self._next_id, max_numeric + 1)

            self._loaded = True

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _invalidate_search_cache(self) -> None:
        """任何会改变 _items 的操作都应调用，避免返回过期结果。"""
        self._search_cache_hash = None
        self._search_cache_result = None

    @staticmethod
    def _hash_search_key(key: tuple) -> str:
        """把搜索条件元组转为稳定的 SHA-256 哈希键。"""
        try:
            stable = json.dumps(key, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except TypeError:
            stable = str(key)
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()

    def _new_id(self) -> str:
        new_id = str(self._next_id)
        self._next_id += 1
        return new_id

    @staticmethod
    def _normalize_tags(tags: Optional[Iterable[str]]) -> List[str]:
        if not tags:
            return []
        cleaned: List[str] = []
        seen: Set[str] = set()
        for t in tags:
            if not isinstance(t, str):
                continue
            t = t.strip()
            if not t:
                continue
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(t)
        return cleaned

    # ------------------------------------------------------------------
    # 写入 API
    # ------------------------------------------------------------------

    def add(
        self,
        title: str,
        content: str,
        category: str = "其他",
        tags: Optional[Iterable[str]] = None,
        importance: int = 3,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """新增一条记忆，返回新 ID。"""
        with self._lock:
            self._ensure_loaded()
            self._invalidate_search_cache()
            now = _now_iso()
            mem_id = self._new_id()
            importance = max(1, min(5, int(importance)))
            item: Dict[str, Any] = {
                "id": mem_id,
                "title": str(title).strip(),
                "content": str(content),
                "category": str(category).strip() or "其他",
                "tags": self._normalize_tags(tags),
                "importance": importance,
                "created_at": now,
                "updated_at": now,
                "embedding": None,
            }
            if metadata:
                item["metadata"] = dict(metadata)
            # MEDIUM-007: 写入前对 content 脱敏，防止 API Key / 密码等长期保存。
            item["content"] = sanitize_for_log(
                item["content"], max_length=_MEMORY_SANITIZE_MAX_LEN
            )
            if self._write_json(self._item_path(mem_id), item):
                self._items[mem_id] = item
                if item["category"] not in self._categories:
                    self._categories.append(item["category"])
                self.save()
                return mem_id
            return ""

    def update(self, mem_id: str, **fields: Any) -> bool:
        """更新字段：title/content/category/tags/importance。"""
        with self._lock:
            self._ensure_loaded()
            self._invalidate_search_cache()
            item = self._items.get(mem_id)
            if item is None:
                return False
            changed = False
            allowed = {"title", "content", "category", "tags", "importance"}
            for key, value in fields.items():
                if key not in allowed:
                    continue
                if key == "importance":
                    try:
                        value = max(1, min(5, int(value)))
                    except (TypeError, ValueError) as _exc:
                        logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
                elif key == "tags":
                    value = self._normalize_tags(value)
                elif key == "content":
                    # MEDIUM-007: 更新 content 时同样先脱敏。
                    value = sanitize_for_log(
                        str(value), max_length=_MEMORY_SANITIZE_MAX_LEN
                    )
                elif key in ("title", "category"):
                    value = str(value).strip()
                    if not value and key == "category":
                        value = "其他"
                if item.get(key) != value:
                    item[key] = value
                    changed = True
            if changed:
                item["updated_at"] = _now_iso()
                if self._write_json(self._item_path(mem_id), item):
                    self.save()
                    return True
                return False
            return True  # 字段没有变化视为成功

    def delete(self, mem_id: str) -> bool:
        """删除一条记忆。"""
        with self._lock:
            self._ensure_loaded()
            self._invalidate_search_cache()
            if mem_id not in self._items:
                return False
            self._items.pop(mem_id, None)
            path = self._item_path(mem_id)
            try:
                if path.exists():
                    path.unlink()
            except OSError as exc:
                print(f"[MemoryManager] 删除 {path.name} 失败: {exc}")
            self.save()
            return True

    # ------------------------------------------------------------------
    # 读取 API
    # ------------------------------------------------------------------

    def get(self, mem_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            item = self._items.get(mem_id)
            return dict(item) if item is not None else None

    def list_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            items = [dict(it) for it in self._items.values()]
            items.sort(key=lambda it: it.get("updated_at", ""), reverse=True)
            return items

    def search(
        self,
        keyword: Optional[str] = None,
        category: Optional[str] = None,
        tag: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            kw = (keyword or "").strip().lower() if keyword else ""
            cat = (category or "").strip() if category else ""
            tg = (tag or "").strip().lower() if tag else ""
            key = (kw, cat, tg, int(limit))
            cache_hash = self._hash_search_key(key)

            if self._search_cache_hash == cache_hash and self._search_cache_result is not None:
                return [dict(it) for it in self._search_cache_result]

            results: List[Dict[str, Any]] = []
            for item in self._items.values():
                if cat and item.get("category") != cat:
                    continue
                if tg:
                    tags_low = [t.lower() for t in (item.get("tags") or [])]
                    if tg not in tags_low:
                        continue
                if kw:
                    haystack = f"{item.get('title', '')}\n{item.get('content', '')}".lower()
                    if kw not in haystack:
                        continue
                results.append(dict(item))

            results.sort(key=lambda it: it.get("updated_at", ""), reverse=True)
            results = results[: max(0, int(limit))]
            self._search_cache_hash = cache_hash
            self._search_cache_result = results
            return [dict(it) for it in results]

    def get_top_for_context(self, max_tokens: int = 2000) -> List[Dict[str, Any]]:
        """按 importance * 新鲜度 排序，按 content 字符数累加直到接近 max_tokens*3。"""
        with self._lock:
            self._ensure_loaded()
            now = datetime.now(timezone.utc)
            scored: List[Tuple[float, Dict[str, Any]]] = []
            for item in self._items.values():
                updated = _parse_iso(item.get("updated_at") or item.get("created_at") or "")
                days = max(0.0, (now - updated).total_seconds() / 86400.0)
                freshness = 1.0 / (1.0 + days / 30.0)
                importance = float(_safe_importance(item.get("importance"), 3))
                score = importance * freshness
                scored.append((score, item))
            scored.sort(key=lambda pair: pair[0], reverse=True)

            char_limit = max(0, int(max_tokens)) * 3
            picked: List[Dict[str, Any]] = []
            consumed = 0
            for _, item in scored:
                content_len = len(str(item.get("content", "")))
                if consumed + content_len > char_limit and picked:
                    continue
                picked.append(dict(item))
                consumed += content_len
                if consumed >= char_limit:
                    break
            return picked

    # ------------------------------------------------------------------
    # 分类 / 标签
    # ------------------------------------------------------------------

    def add_category(self, category_name: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            name = str(category_name).strip()
            if not name:
                return False
            if name in self._categories:
                return False
            self._categories.append(name)
            self.save()
            return True

    def get_categories(self) -> List[str]:
        with self._lock:
            self._ensure_loaded()
            return list(self._categories)

    def get_tags(self) -> List[str]:
        with self._lock:
            self._ensure_loaded()
            seen: Set[str] = set()
            result: List[str] = []
            for item in self._items.values():
                for t in item.get("tags") or []:
                    if t and t.lower() not in seen:
                        seen.add(t.lower())
                        result.append(t)
            return result

    def mark_important(self, mem_id: str, is_important: bool) -> None:
        """将 importance 设为 5（重要）或 3（普通）。"""
        with self._lock:
            self.update(mem_id, importance=5 if bool(is_important) else 3)

    # ------------------------------------------------------------------
    # 统计 / 清理 / 导入导出
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            total = len(self._items)
            by_category: Dict[str, int] = {}
            total_importance = 0
            for item in self._items.values():
                cat = item.get("category") or "其他"
                by_category[cat] = by_category.get(cat, 0) + 1
                total_importance += _safe_importance(item.get("importance"), 3)
            avg = (total_importance / total) if total else 0.0
            return {
                "total": total,
                "by_category": dict(by_category),
                "avg_importance": round(avg, 2),
                "categories": list(self._categories),
                "tags_count": len(self.get_tags()),
            }

    def cleanup_unused(self, max_age_days: int = 90) -> int:
        """清理 importance <=2 且超过 max_age_days 未更新的条目。"""
        with self._lock:
            self._ensure_loaded()
            cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, int(max_age_days)))
            removed = 0
            for mem_id in list(self._items.keys()):
                item = self._items[mem_id]
                if _safe_importance(item.get("importance"), 3) > 2:
                    continue
                updated = _parse_iso(item.get("updated_at") or item.get("created_at") or "")
                if updated < cutoff:
                    if self.delete(mem_id):
                        removed += 1
            return removed

    def export_all(self, path: Path) -> Path:
        with self._lock:
            self._ensure_loaded()
            target = Path(path)
            payload: Dict[str, Any] = {
                "version": 1,
                "exported_at": _now_iso(),
                "categories": list(self._categories),
                "items": [dict(it) for it in self.list_all()],
            }
            if self._write_json(target, payload):
                return target
            raise OSError(f"无法导出到 {target}")

    @staticmethod
    def _sanitize_importance(value: Any) -> int:
        """把任意 importance 输入规范化为 1-5 的整数，异常时回退到 3。"""
        try:
            return max(1, min(5, int(value)))
        except (TypeError, ValueError):
            return 3

    def import_from(self, path: Path, merge: bool = True) -> int:
        with self._lock:
            self._ensure_loaded()
            src = Path(path)
            data = self._read_json(src)
            if not isinstance(data, dict):
                return 0
            items = data.get("items") or []
            if not isinstance(items, list):
                return 0
            added = 0
            existing_titles: Dict[str, str] = {}
            if merge:
                for item in self._items.values():
                    existing_titles[(item.get("title") or "").strip().lower()] = item["id"]
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                title = str(raw.get("title") or "").strip()
                if not title:
                    continue
                category = str(raw.get("category") or "其他").strip()
                if not category:
                    category = "其他"
                importance = self._sanitize_importance(raw.get("importance"))
                if merge:
                    existing = existing_titles.get(title.lower())
                    if existing:
                        self.update(
                            existing,
                            content=str(raw.get("content", "")),
                            category=category,
                            tags=list(raw.get("tags") or []),
                            importance=importance,
                        )
                        added += 1
                        continue
                new_id = self.add(
                    title=title,
                    content=str(raw.get("content", "")),
                    category=category,
                    tags=list(raw.get("tags") or []),
                    importance=importance,
                )
                if new_id:
                    existing_titles[title.lower()] = new_id
                    added += 1
            # 同步分类（来源列表中出现的分类自动补全）。
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                cat = str(raw.get("category") or "").strip()
                if cat and cat not in self._categories:
                    self._categories.append(cat)
            self.save()
            return added

    # ------------------------------------------------------------------
    # AutoMemory / 旧 API 兼容别名
    # ------------------------------------------------------------------
    def list_memories(
        self,
        category: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """与 AutoMemory 兼容的列表接口。"""
        return self.search(keyword=keyword, category=category, limit=limit)

    def add_memory(
        self,
        text: str,
        category: str = "其他",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Optional[str]:
        """与 AutoMemory 兼容的新增接口。"""
        title = kwargs.get("title") or text[:60]
        content = text
        merged_metadata = dict(metadata or {})
        merged_metadata.update(kwargs)
        return self.add(
            title=str(title),
            content=str(content),
            category=category,
            tags=tags or [],
            metadata=merged_metadata or None,
        )

    def update_memory(self, mem_id: str, **fields: Any) -> bool:
        """与 AutoMemory 兼容的更新接口。"""
        return self.update(mem_id, **fields)

    def get_memory(self, mem_id: str) -> Optional[Dict[str, Any]]:
        """与 AutoMemory 兼容的查询接口。"""
        return self.get(mem_id)

    def delete_memory(self, mem_id: str, category: Optional[str] = None) -> bool:
        """与 AutoMemory 兼容的删除接口。"""
        # 如果指定了 category，先校验条目属于该分类
        if category:
            item = self.get(mem_id)
            if not item or item.get("category") != category:
                return False
        return self.delete(mem_id)


# ---------------------------------------------------------------------------
# 工具调用兼容：向 AI 的 system prompt 注入一段工具说明。
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def build_tool_call_context() -> str:
    """为 AI 工具调用（例如 add_memory / search_memory 等）生成注入到 system prompt 的工具说明。

    返回一段说明文本，供外部在 system prompt 末尾附加。
    """
    return (
        "【长期记忆工具说明】本项目存在一个本地长期记忆库（Long-term Memory），"
        "用于保存跨会话的项目级知识。你可以通过以下高层工具操作该记忆库：\n"
        "- add_memory(title, content, category='其他', tags=[], importance=3)：新增一条记忆；"
        "category 可选：架构决策 / 已知问题 / 代码风格 / API约定 / 其他；"
        "importance 为 1-5 的整数，5 表示最高优先。\n"
        "- update_memory(id, **fields)：更新已有记忆的 title/content/category/tags/importance。\n"
        "- delete_memory(id)：删除指定记忆。\n"
        "- search_memory(keyword=None, category=None, tag=None, limit=50)：按关键字、分类或标签检索。\n"
        "- list_memory()：列出全部记忆，按更新时间倒序。\n"
        "- get_memory(id)：获取单条记忆详情。\n"
        "- mark_important(id, is_important: bool)：将 importance 切换为 5 或 3。\n"
        "调用以上工具时，请使用对应的工具参数命名的函数式（function calling 约定格式进行。"
        "新写入的记忆应简短、高价值，优先；避免重复写入相近内容；"
        "重要结论、架构权衡、API 约定、反复踩坑的问题都值得写入长期记忆。"
    )
