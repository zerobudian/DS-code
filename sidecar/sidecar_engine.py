# -*- coding: utf-8 -*-
"""DScode V2 Sidecar — 引擎封装层。

将 ChatEngine 的同步回调接口（on_content_delta / on_new_turn / on_finished 等）
包装为异步事件队列，供 FastAPI SSE endpoint 消费。

本模块只做适配，不改动 src/ 下的任何现有代码。
"""

import asyncio
import json
import queue
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

# 确保 src/ 在 Python path 里（旧版 dscode/src/，新版结构会沿用此路径）
SIDECAR_DIR = Path(__file__).resolve().parent
DSCODE_V2_ROOT = SIDECAR_DIR.parent
_OLD_DSC_ROOT = DSCODE_V2_ROOT.parent / "dscode"
for _p in (DSCODE_V2_ROOT, _OLD_DSC_ROOT):
    _ps = str(_p)
    if _ps not in sys.path and _p.exists():
        sys.path.insert(0, _ps)

# 复用现有模块（一行不改）
from src.core.config import Config
from src.core.chat_engine import ChatEngine
from src.sandbox.project_manager import ProjectManager


class EngineManager:
    """全局单例 — 持有 ChatEngine + ProjectManager 实例。"""

    def __init__(self):
        self.cfg = Config()
        self.projects = ProjectManager()
        self.engine = ChatEngine()
        self._loaded_project: Optional[str] = None
        self._init_engine()

    def _init_engine(self):
        """初始化引擎回调（在 set_project 之前只需占位）。"""
        self.engine.on_content_delta = lambda d: self._push("content", d)
        self.engine.on_reasoning_delta = lambda d: self._push("reasoning", d)
        self.engine.on_new_turn = lambda t: self._push("new_turn", {"turn": t})
        self.engine.on_finished = lambda c, h, cached, total: self._push("done", {
            "cost": round(c, 6) if c else 0,
            "cache_hit_rate": round(h, 2) if h else 0,
            "cached_tokens": cached,
            "total_tokens": total,
        })
        self.engine.on_error = lambda code, msg: self._push("error", {"code": code, "message": msg})
        self.engine.on_status_update = lambda m: self._push("status", m)
        self.engine.on_tool_call_detected = lambda c: self._push("tool_call", self._serialize(c))
        self.engine.on_tool_result = lambda n, r: self._push("tool_result", {"name": n, "result": str(r)[:500]})
        self.engine.on_plan_ready = lambda text: self._push("plan_ready", text)
        self.engine.on_confirm_command = lambda call: self._push("confirm_command", call)

        # 事件队列
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()

    def _push(self, event_type: str, data: Any):
        """回调线程安全地将事件推入队列。"""
        try:
            self._queue.put({"type": event_type, "data": data})
        except Exception:
            pass

    @staticmethod
    def _serialize(obj) -> Any:
        """安全序列化回调参数。"""
        if isinstance(obj, (str, int, float, bool, type(None))):
            return obj
        if isinstance(obj, (list, tuple)):
            return [EngineManager._serialize(i) for i in obj]
        if isinstance(obj, dict):
            return {k: EngineManager._serialize(v) for k, v in obj.items()}
        try:
            return str(obj)[:1000]
        except Exception:
            return repr(obj)[:1000]

    def ensure_project(self, project_name: str = "demo"):
        """确保项目已加载到引擎。"""
        if self._loaded_project == project_name and self.engine.sandbox is not None:
            return

        existing = self.projects.list_projects()
        if project_name not in existing:
            display_name = project_name.capitalize()
            sb = self.projects.create(project_name, display_name=display_name)
        else:
            sb = self.projects.get(project_name)

        if sb is None:
            raise RuntimeError(f"无法创建或加载项目: {project_name}")

        self.engine.set_project(sb)
        self._loaded_project = project_name

    def send_message(self, text: str, project: str = "demo", images: Optional[List[str]] = None):
        """在后台线程调用 engine.send_message，通过回调将事件推入 _queue。

        调用方应持续 drain _queue 直到收到 {"type":"done"} 或 {"type":"error"}。
        """
        # 清空上一轮的残留事件
        with self._lock:
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

        def _worker():
            try:
                self.ensure_project(project)
                self.engine.send_message(text, images=images)
            except Exception as e:
                self._push("error", {"code": 500, "message": str(e)})
                self._push("done", {"cost": 0, "cache_hit_rate": 0, "cached_tokens": 0, "total_tokens": 0})

        t = threading.Thread(target=_worker, daemon=True, name="sidecar-chat")
        t.start()

    def stop(self):
        """中断当前请求。"""
        try:
            self.engine.stop()
        except Exception:
            pass

    def cleanup(self):
        """应用退出时的清理。"""
        try:
            self.engine.stop()
        except Exception:
            pass
        try:
            if hasattr(self.engine, "cleanup"):
                self.engine.cleanup()
        except Exception:
            pass


# 全局单例
_engine: Optional[EngineManager] = None


def get_engine() -> EngineManager:
    global _engine
    if _engine is None:
        _engine = EngineManager()
    return _engine


def get_sse_events() -> Any:
    """从引擎事件队列中非阻塞地取一条事件。无事件时返回 None。"""
    eng = get_engine()
    try:
        return eng._queue.get_nowait()
    except queue.Empty:
        return None
