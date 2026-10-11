"""Serialize access to ChatEngine and keep events owned by one chat run."""

import queue
import secrets
import threading
import time
from dataclasses import dataclass, field

from .http_protocol import HTTPError


@dataclass
class ChatRun:
    project: str
    id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    events: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=4096))
    finished: threading.Event = field(default_factory=threading.Event)
    attached: bool = False
    cancelled: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    pending: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=lambda: {
        "cost": 0, "cache_hit_rate": 0, "cached_tokens": 0, "total_tokens": 0,
    })
    error: dict = field(default_factory=dict)
    created: float = field(default_factory=time.monotonic)

    def emit(self, kind, data):
        try:
            self.events.put_nowait((kind, data))
        except queue.Full:
            self.cancel_event.set()
            self.error = {"code": 503, "message": "事件接收过慢，请重试"}
            if kind in {"done", "srv_error"}:
                self.events.get_nowait()
                self.events.put_nowait(("srv_error", self.error))


class ChatRuntime:
    def __init__(self, cfg, projects, engine):
        self.cfg, self.projects, self.engine = cfg, projects, engine
        self.lock = threading.RLock()
        self.active = None
        self.runs = {}
        self.worker = None
        self.closing = False

    def require_idle(self):
        if self.active is not None or self.closing:
            raise HTTPError(409, "已有请求正在处理，请等待完成")

    def canonical(self, name):
        safe = self.projects._safe_project_name(name)
        if safe is None:
            raise HTTPError(400, "无效项目名")
        # Preserve already existing names from older versions.
        return name if name in self.projects.list_projects() else safe

    def start(self, project, text, continue_plan=False):
        with self.lock:
            self.require_idle()
            project = self.canonical(project)
            run = ChatRun(project)
            self.active = run
            # Keep completed, unattached runs briefly for POST -> SSE handoff.
            self.runs = {k: v for k, v in self.runs.items()
                         if time.monotonic() - v.created < 300 and not v.attached}
            if len(self.runs) >= 32:
                self.runs.pop(next(iter(self.runs)))
            self.runs[run.id] = run
            self.worker = threading.Thread(target=self._work, args=(run, text, continue_plan),
                                           daemon=True, name="sidecar-chat")
            self.worker.start()
            return run

    def _bind(self, run):
        engine = self.engine
        engine.on_content_delta = lambda d: run.emit("content", str(d))
        engine.on_reasoning_delta = lambda d: run.emit("reasoning", str(d))
        engine.on_new_turn = lambda t: run.emit("new_turn", {"turn": t})
        engine.on_status_update = lambda d: run.emit("status", str(d))
        engine.on_tool_call_detected = lambda d: run.emit("tool_call", d)
        engine.on_tool_result = lambda n, r: run.emit("tool_result", {"name": n, "result": str(r)})
        engine.on_plan_ready = lambda d: run.emit("plan_ready", str(d))
        engine.on_chat_clear_requested = lambda: run.emit("chat_clear", {})
        engine.on_confirm_command = lambda d: self._question(run, "command", d)
        engine.on_confirm_edit = lambda p, d: self._question(run, "edit", {"filepath": p, "diff": d})
        engine.on_ask_user_questions = lambda d: self._question(run, "answer", d)
        engine.on_error = lambda code, msg: run.error.update(code=code, message=str(msg))
        engine.on_finished = lambda cost, hit, cached, total: run.metrics.update(
            cost=cost or 0, cache_hit_rate=hit or 0, cached_tokens=cached, total_tokens=total)

    def _question(self, run, kind, data):
        with self.lock:
            if run.cancelled:
                # Covers stop arriving immediately before the gate allocates its event.
                self.engine.stop()
                self.engine.cancel_pending_confirmations()
                return
            request_id = secrets.token_urlsafe(16)
            run.pending[kind] = request_id
            run.emit({"command": "confirm_command", "edit": "confirm_edit",
                      "answer": "ask_user_questions"}[kind],
                     {"request_id": request_id, "payload": data})
            if run.cancel_event.is_set():
                self.engine.cancel_pending_confirmations()

    def submit(self, run_id, kind, request_id, value):
        with self.lock:
            run = self.active
            if (run is None or run.id != run_id or run.cancelled
                    or run.pending.get(kind) != request_id):
                raise HTTPError(409, "确认请求已失效")
            run.pending.pop(kind)
            handlers = {"command": self.engine.submit_command_confirmation,
                        "edit": self.engine.confirm_edit, "answer": self.engine.submit_user_answer}
            handlers[kind](value)

    def _work(self, run, text, continue_plan):
        try:
            self._bind(run)
            sandbox = self.projects.get(run.project) or self.projects.create(run.project)
            if sandbox is None:
                raise RuntimeError("创建或加载项目失败")
            if self.engine.sandbox is None or self.engine.sandbox.name != run.project:
                self.engine.set_project(sandbox)
            if not continue_plan:
                self.engine.set_mode(self.cfg.get("chat_mode", "normal"))
            with self.lock:
                cancelled = run.cancelled
            if not cancelled:
                if continue_plan:
                    self.engine.confirm_plan()
                    self.engine.continue_from_plan(cancel_event=run.cancel_event)
                else:
                    self.engine.send_message(text, cancel_event=run.cancel_event)
        except Exception as exc:
            run.error = {"code": 500, "message": str(exc)}
        finally:
            with self.lock:
                run.pending.clear()
                if run.error and not run.cancelled:
                    run.emit("srv_error", run.error)
                else:
                    run.emit("done", {**run.metrics, "stopped": run.cancelled,
                                      "mode": self.engine.get_mode()})
                run.finished.set()
                if self.active is run:
                    self.active = None

    def attach(self, run_id):
        with self.lock:
            run = self.runs.get(run_id)
            if run is None:
                raise HTTPError(404, "聊天请求不存在或已过期")
            if run.attached:
                raise HTTPError(409, "此聊天流已有接收端")
            run.attached = True
            return run

    def stop(self, run_id=None):
        with self.lock:
            run = self.active
            if run_id is not None and (run is None or run.id != run_id):
                return False
            if run is None:
                return False
            run.cancelled = True
            run.cancel_event.set()
            run.pending.clear()
        self.engine.stop()
        return True

    def close(self):
        with self.lock:
            self.closing = True
        self.stop()
        if self.worker:
            self.worker.join(timeout=3)
        self.engine.shutdown()
