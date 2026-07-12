# -*- coding: utf-8 -*-
"""并行子代理引擎 — SubAgentEngine

负责:
1. 管理最多 MAX_PARALLEL 个并行子代理线程
2. 主模型通过 JSON 工具调用 start_subagent 启动子代理
3. 每个子代理独立运行在自己的 ChatEngine 实例中
4. 主模型决定每个子代理使用哪个模型 (pro/flash)
5. 收集子代理的输出结果并返回给主模型

使用方式 (从 ChatEngine 调用):
    self._subagent = SubAgentEngine(self)
    self._subagent.start(task_id, task_description, model_name)
    results = self._subagent.get_results()
    status = self._subagent.list_agents()
"""

import logging

logger = logging.getLogger(__name__)

import threading
import time
from typing import Dict, List, Optional, Any, Tuple


MAX_PARALLEL_DEFAULT = 5   # 默认最多 5 个并行子代理
MAX_AGENT_RUNTIME_DEFAULT = 600.0   # 单个子代理最大运行秒数
WATCHDOG_INTERVAL = 5.0             # 看门狗轮询间隔


class SubAgentEngine:
    """并行子代理管理器 — 线程安全

    线程安全策略：
    - 所有对 self._agents / self._stop_flags 的读写都必须持有 self._lock
    - 子线程内每次更新 agent 状态都通过 self._lock 保护
    - stop_agent 在锁内更新 status 后释放锁再 set event / join
    - clear_done 使用 "collect → join → delete" 三步，避免持有锁时阻塞
    """

    def __init__(self, parent_engine=None):
        """
        Args:
            parent_engine: 父 ChatEngine 实例（用于共享配置）
        """
        self._parent = parent_engine
        self._lock = threading.RLock()
        self._agents: Dict[str, Dict[str, Any]] = {}  # agent_id -> {线程, 输出, 状态, ...}
        self._stop_flags: Dict[str, "threading.Event"] = {}
        self._max_parallel = MAX_PARALLEL_DEFAULT
        self._max_total = 50      # 生命周期内子代理总数硬上限
        self._max_depth = 3       # 子代理嵌套深度硬上限
        self._max_agent_runtime = MAX_AGENT_RUNTIME_DEFAULT  # 单个子代理最大运行秒数
        self._next_id = 0  # 自动生成 agent_id 的计数器（锁保护）

        # 看门狗线程：检测运行超时的子代理并强制停止
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None

        # 从父引擎读取配置与嵌套深度
        self._cfg = None
        self._depth = 0
        if parent_engine is not None:
            self._cfg = getattr(parent_engine, "cfg", None)
            self._depth = getattr(parent_engine, "_subagent_depth", 0)

        if self._cfg is not None and hasattr(self._cfg, "get_subagent_config"):
            sub_cfg = self._cfg.get_subagent_config()
            self._max_parallel = int(sub_cfg.get("max_parallel", MAX_PARALLEL_DEFAULT))
            self._max_total = int(sub_cfg.get("max_total", self._max_total))
            self._max_depth = int(sub_cfg.get("max_depth", self._max_depth))
            self._max_agent_runtime = float(sub_cfg.get("max_agent_runtime", self._max_agent_runtime))

        self._start_watchdog()

    def _start_watchdog(self):
        """启动守护线程，定时清理运行超时的子代理。"""
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="SubAgentWatchdog",
        )
        self._watchdog_thread.start()

    def stop_watchdog(self):
        """停止看门狗线程（通常由父引擎清理时调用）。"""
        self._watchdog_stop.set()
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            try:
                self._watchdog_thread.join(timeout=2.0)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def __del__(self):
        try:
            self.stop_watchdog()
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def _watchdog_loop(self):
        """看门狗主循环：每 WATCHDOG_INTERVAL 秒扫描一次，停止超时子代理。"""
        while not self._watchdog_stop.is_set():
            try:
                self._watchdog_tick()
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            self._watchdog_stop.wait(timeout=WATCHDOG_INTERVAL)

    def _watchdog_tick(self):
        """扫描所有 running 状态的子代理，对超时的调用 stop_agent。"""
        now = time.time()
        expired: List[str] = []
        with self._lock:
            if not isinstance(self._agents, dict):
                return
            for agent_id, a in list(self._agents.items()):
                if not isinstance(a, dict):
                    continue
                if a.get("status") != "running":
                    continue
                start = a.get("start_time")
                if start is None:
                    continue
                elapsed = now - float(start)
                if elapsed > self._max_agent_runtime:
                    expired.append(agent_id)
        for agent_id in expired:
            try:
                self.stop_agent(agent_id)
                # 标记为超时（stop_agent 只设为 stopped）
                with self._lock:
                    a = self._agents.get(agent_id)
                    if isinstance(a, dict) and a.get("status") == "stopped":
                        a["status"] = "error"
                        a["error"] = f"运行超过最大时限 {self._max_agent_runtime:.0f}s，已被看门狗终止"
                        a["end_time"] = time.time()
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def _gen_agent_id(self, prefix: str = "agent") -> str:
        """在锁内生成唯一 agent_id，避免外部重复 ID 竞态"""
        with self._lock:
            self._next_id += 1
            return f"{prefix}_{self._next_id}_{id(self)}"

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def set_max_parallel(self, n: int) -> None:
        """动态设置最大并行数"""
        with self._lock:
            self._max_parallel = max(1, int(n))

    def _count_active_agents(self) -> int:
        """返回当前处于 running 或 pending 状态的子代理数量（调用方需已持锁）。"""
        count = 0
        if not isinstance(self._agents, dict):
            return 0
        for a in self._agents.values():
            if isinstance(a, dict) and a.get("status") in ("running", "pending"):
                count += 1
        return count

    def start_subagent(self, agent_id: str, task_description: str,
                       model: Optional[str] = None) -> Tuple[bool, str]:
        """启动一个子代理（线程安全）。

        Args:
            agent_id: 子代理唯一标识；空则自动生成
            task_description: 子任务描述
            model: 模型名；None 则用配置中的 default_model

        Returns:
            (success: bool, message: str)
        """
        try:
            # 生成默认 ID（需在锁内，避免并发传入空 ID 冲突）
            need_gen_id = not (agent_id and isinstance(agent_id, str) and agent_id.strip())
            with self._lock:
                if not isinstance(self._agents, dict):
                    self._agents = {}
                if need_gen_id:
                    agent_id = self._gen_agent_id()
                else:
                    agent_id = str(agent_id).strip()

                # 检查嵌套深度与总数限制
                if self._depth >= self._max_depth:
                    return False, f"子代理嵌套深度已达上限 {self._max_depth}"
                # 生命周期总数硬上限（含已完成）
                total_count = len(self._agents)
                if total_count >= self._max_total:
                    return False, f"子代理总数已达上限 {self._max_total}，请先清理已完成记录"

                # 检查并行数限制：严格保证 running + pending 不超过 max_parallel
                self._max_parallel = max(1, int(self._max_parallel))
                active_count = self._count_active_agents()
                if active_count >= self._max_parallel:
                    return False, f"当前运行子代理数 {active_count} 已达最大并行数 {self._max_parallel}，请等现有子代理完成后再试"

                # 检查 ID 冲突
                if agent_id in self._agents:
                    return False, f"子代理 ID '{agent_id}' 已存在"

                # 决定模型，并做白名单校验（防御绕过 tool_start_subagent 直接调用）
                allowed_models = {"deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-ultra",
                                  "deepseek-chat", "deepseek-reasoner"}
                if model is None or model == "":
                    model = self._default_model()
                else:
                    model = str(model).strip()
                    if model not in allowed_models:
                        return False, f"[start_subagent] 不支持的模型: {model}"

                stop_event = threading.Event()
                if not isinstance(self._stop_flags, dict):
                    self._stop_flags = {}
                self._stop_flags[agent_id] = stop_event

                agent_data: Dict[str, Any] = {
                    "id": agent_id,
                    "task": task_description,
                    "model": model,
                    "status": "pending",   # pending -> running -> done / error
                    "output": "",
                    "error": None,
                    "start_time": time.time(),
                    "end_time": None,
                    "thread": None,
                }
                self._agents[agent_id] = agent_data

                # 启动线程（线程创建在锁内，确保 thread 字段立即可见）
                t = threading.Thread(
                    target=self._run_agent_thread,
                    args=(agent_id, task_description, model, stop_event),
                    daemon=True,
                    name=f"SubAgent-{agent_id}",
                )
                agent_data["thread"] = t
                t.start()

                return True, f"子代理 '{agent_id}' 已启动 (model={model})"
        except Exception as e:
            return False, f"[start_subagent] 异常: {e}"

    def list_agents(self) -> List[Dict]:
        """列出所有子代理及其状态（线程安全：一次加锁快照所有字段）"""
        now = time.time()
        with self._lock:
            if not isinstance(self._agents, dict):
                return []
            snapshot = list(self._agents.values())
        result = []
        for a in snapshot:
            if not isinstance(a, dict):
                continue
            try:
                status = a.get("status", "unknown")
                if status == "running":
                    elapsed = now - float(a.get("start_time") or now)
                elif a.get("end_time"):
                    elapsed = float(a.get("end_time") or now) - float(a.get("start_time") or now)
                else:
                    elapsed = 0
                task = str(a.get("task", ""))
                result.append({
                    "id": a.get("id", ""),
                    "status": status,
                    "model": a.get("model", ""),
                    "running_seconds": round(elapsed, 1),
                    "task": task[:80] + ("..." if len(task) > 80 else ""),
                })
            except Exception as _exc:
                logger.debug("跳过异常项: %s", _exc, exc_info=True)
                continue
        return result

    def get_results(self) -> str:
        """获取所有已完成子代理的结果汇总（线程安全：先快照再格式化）"""
        try:
            now = time.time()
            with self._lock:
                if not isinstance(self._agents, dict) or not self._agents:
                    snapshot = []
                else:
                    snapshot = [
                        (agent_id, dict(data)) for agent_id, data in self._agents.items()
                        if isinstance(data, dict)
                    ]
            lines = ["=== 子代理结果汇总 ==="]
            if not snapshot:
                lines.append("(尚无子代理)")
                return "\n".join(lines)
            for agent_id, data in sorted(snapshot):
                if not isinstance(data, dict):
                    continue
                status = data.get("status", "unknown")
                if status in ("done", "error"):
                    lines.append(f"[{agent_id}] model={data.get('model', '?')}, status={status}")
                    if status == "error":
                        lines.append(f"  Error: {data.get('error')}")
                    else:
                        out = str(data.get("output", "")).strip()
                        if len(out) > 2000:
                            out = out[:2000] + "\n...(truncated)"
                        lines.append(f"  Output:\n{out}")
                    try:
                        elapsed = (float(data.get("end_time") or now) -
                                   float(data.get("start_time") or now))
                        lines.append(f"  Time: {elapsed:.1f}s")
                    except (TypeError, ValueError) as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                    lines.append("")
                elif status == "running":
                    lines.append(f"[{agent_id}] still running (model={data.get('model', '?')})")
            return "\n".join(lines)
        except Exception as e:
            return f"[get_results] 异常: {e}"

    def wait_for_all(self, timeout: float = 600, stop_check=None) -> None:
        """等待所有子代理完成（阻塞直到全部 done/error/stopped 或超时）。

        每 0.5 秒轮询一次，超时后不再等待。
        线程安全：每次轮询在锁内快照状态。

        Args:
            timeout: 最大等待秒数（默认 600 秒 = 10 分钟）
            stop_check: 可选回调，返回 True 时提前退出等待（用于响应 stop()）
        """
        import time as _time
        if timeout <= 0:
            return
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if stop_check is not None:
                try:
                    if stop_check():
                        return
                except Exception:
                    pass
            all_done = True
            with self._lock:
                if not isinstance(self._agents, dict):
                    break
                for a in self._agents.values():
                    if isinstance(a, dict) and a.get("status") in ("running", "pending"):
                        all_done = False
                        break
            if all_done:
                return
            _time.sleep(0.5)

    def stop_all(self, timeout: float = 3.0) -> None:
        """停止所有正在运行的子代理（stop() 时调用）。"""
        import time as _time
        with self._lock:
            if not isinstance(self._agents, dict):
                return
            agent_ids = [aid for aid, a in self._agents.items()
                         if isinstance(a, dict) and a.get("status") in ("running", "pending")]
        for aid in agent_ids:
            try:
                self.stop_agent(aid)
            except Exception as _exc:
                logger.debug("停止子代理 %s 异常: %s", aid, _exc, exc_info=True)
        # 等待线程退出
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            any_running = False
            with self._lock:
                for a in (self._agents or {}).values():
                    if isinstance(a, dict) and a.get("status") in ("running", "pending"):
                        any_running = True
                        break
            if not any_running:
                break
            _time.sleep(0.1)

    def stop_agent(self, agent_id: str) -> Tuple[bool, str]:
        """停止一个运行中的子代理（线程安全）。

        流程:
        1. 在锁内更新 status / end_time，并获取 stop_event / thread 引用
        2. 释放锁后再触发 event 与 join，避免长时间持锁
        """
        try:
            stop_event = None
            thread = None
            msg_suffix = ""
            with self._lock:
                if not isinstance(self._agents, dict) or agent_id not in self._agents:
                    return False, f"子代理 '{agent_id}' 不存在"
                a = self._agents[agent_id]
                if not isinstance(a, dict):
                    return False, f"子代理 '{agent_id}' 数据损坏"
                status = a.get("status")
                if status not in ("running", "pending"):
                    return False, f"子代理 '{agent_id}' 已处于 {status} 状态"
                a["status"] = "stopped"
                a["end_time"] = a.get("end_time") or time.time()
                stop_event = self._stop_flags.get(agent_id)
                thread = a.get("thread")
                msg_suffix = "已取消" if status == "pending" else "已请求停止"
            # 锁外触发停止信号，避免阻塞其他并发操作
            if stop_event is not None:
                try:
                    stop_event.set()
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            if thread is not None and thread.is_alive():
                try:
                    thread.join(timeout=1.0)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            return True, f"子代理 '{agent_id}' {msg_suffix}"
        except Exception as e:
            return False, f"[stop_agent] 异常: {e}"

    def clear_done(self, timeout: float = 0.5) -> None:
        """清理已完成/停止/失败的子代理记录（先发送停止信号并等待线程释放）。

        - 锁内收集 ID → 锁外设置 stop_event + join → 线程仍在运行则保留记录。
        - 二次确认：删除前再次检查 agent 状态，确保未在 join 期间被重新激活。
        """
        to_remove: List[str] = []
        with self._lock:
            if isinstance(self._agents, dict):
                for key, agent in list(self._agents.items()):
                    if isinstance(agent, dict) and agent.get("status") in ("done", "error", "stopped"):
                        to_remove.append(key)
        # 锁外：触发停止事件并 join；线程未结束则保留记录
        still_alive: List[str] = []
        for key in to_remove:
            thread = None
            stop_event = None
            with self._lock:
                agent_state: Optional[Dict[str, Any]] = self._agents.get(key)
                if isinstance(agent_state, dict):
                    thread = agent_state.get("thread")
                    stop_event = self._stop_flags.get(key)
            if stop_event is not None:
                try:
                    stop_event.set()
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            if thread is not None and thread.is_alive():
                try:
                    thread.join(timeout=timeout)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                if thread.is_alive():
                    still_alive.append(key)
        # 锁内二次确认后删除，仍在运行的保留
        with self._lock:
            for key in to_remove:
                if key in still_alive:
                    continue
                agent_info: Optional[Dict[str, Any]] = self._agents.get(key)
                if isinstance(agent_info, dict) and agent_info.get("status") in ("done", "error", "stopped"):
                    self._agents.pop(key, None)
                    self._stop_flags.pop(key, None)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _default_model(self) -> str:
        """获取默认子代理模型"""
        if self._cfg is not None and hasattr(self._cfg, "get_subagent_config"):
            sub_cfg = self._cfg.get_subagent_config()
            return sub_cfg.get("default_model", "deepseek-v4-flash")
        return "deepseek-v4-flash"

    def _run_agent_thread(self, agent_id: str, task_description: str, model: str,
                          stop_event: threading.Event) -> None:
        """在独立线程中运行子代理（线程安全）。

        关键:
        - stop_event 由 start_subagent 预先创建并注册到 _stop_flags，
          确保 stop_agent() 在子线程启动前后都能立即找到它；
        - 对 _agents 的修改统一在锁内完成；
        - 子代理完成后清理 ChatEngine 资源（文件监听器等）。
        """
        output_lines: List[str] = []
        sub = None
        try:
            # 检查是否已被停止：若 stop_agent 在创建 ChatEngine 前调用，直接退出
            if stop_event.is_set():
                with self._lock:
                    if agent_id in self._agents and isinstance(self._agents[agent_id], dict):
                        self._agents[agent_id]["status"] = "stopped"
                        self._agents[agent_id]["end_time"] = time.time()
                return

            # 更新状态为 running
            with self._lock:
                if agent_id in self._agents and isinstance(self._agents[agent_id], dict):
                    # 仅在状态仍为 pending 时才推进到 running，避免覆盖 stopped
                    if self._agents[agent_id].get("status") == "pending":
                        self._agents[agent_id]["status"] = "running"

            from src.core.chat_engine import ChatEngine

            # 创建独立 ChatEngine 实例，并记录子代理深度
            sub = ChatEngine()
            sub._subagent_depth = self._depth + 1

            # 挂载 cfg 引用（只读使用，子代理通过 _subagent_model_override 覆盖模型）
            if self._cfg is not None:
                sub.cfg = self._cfg
                if model:
                    try:
                        with sub._state_lock:
                            sub._state["_subagent_model_override"] = model
                    except Exception as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

            # 继承 sandbox / 项目上下文
            if hasattr(self._parent, "sandbox") and self._parent.sandbox is not None:
                if hasattr(sub, "set_project"):
                    sub.set_project(self._parent.sandbox)
                else:
                    sub.sandbox = self._parent.sandbox

            if hasattr(self._parent, "history") and self._parent.history is not None:
                # 子代理有独立的历史记录，不继承（隔离执行）
                # 但需要 set_project 来初始化
                pass

            # 工具执行器 — 禁用命令执行/写文件等危险操作
            if hasattr(sub, "tool_exec") and sub.tool_exec is not None:
                # C-005：通过统一接口锁定子代理权限，避免被模型参数或内部状态覆盖。
                if hasattr(sub.tool_exec, "set_subagent_restrictions"):
                    sub.tool_exec.set_subagent_restrictions(
                        is_subagent=True, forbid_commands=True, forbid_write=True
                    )
                else:
                    # 兼容旧版本 ToolExecutor
                    if hasattr(sub.tool_exec, "_is_subagent"):
                        setattr(sub.tool_exec, "_is_subagent", True)
                    if hasattr(sub.tool_exec, "_forbid_commands"):
                        setattr(sub.tool_exec, "_forbid_commands", True)
                    if hasattr(sub.tool_exec, "_forbid_write"):
                        setattr(sub.tool_exec, "_forbid_write", True)

            # 流式回调
            def on_delta(d: str) -> None:
                if d:
                    output_lines.append(d)

            def on_content(d: str) -> None:
                if d:
                    output_lines.append(d)

            try:
                sub.on_content_delta = on_delta
                sub.on_reasoning_delta = on_content
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

            # 构造子代理专用 prompt — 简洁且可执行
            prompt = (
                "你是一个子代理 (Sub-Agent)。\n"
                "你被主模型启动来执行一个具体的子任务。\n"
                f"你的任务:\n\n{task_description}\n\n"
                "要求:\n"
                "1. 专注完成此任务，不要发散\n"
                "2. 如果需要读写文件，直接使用工具\n"
                "3. 给出最终结果摘要（不要空泛的描述）\n"
                "4. 尽量控制在 1-2 轮工具调用内完成\n"
            )

            # 启动子代理（在本线程内同步执行）
            # done_event 必须在 if 外初始化，确保无 send_message 时 finally 也能安全引用
            done_event = threading.Event()
            if hasattr(sub, "send_message"):
                try:
                    # 将外部 stop_event 透传给 ChatEngine 的停止机制
                    watcher: Optional[threading.Thread] = None
                    if stop_event is not None and hasattr(sub, "stop"):
                        def _watch_stop(evt=stop_event, engine=sub, done=done_event):
                            try:
                                # 任一事件触发即退出，避免 send_message 完成后 watcher 泄漏
                                while not done.is_set() and not evt.is_set():
                                    done.wait(timeout=0.2)
                                if evt.is_set():
                                    engine.stop()
                            except Exception as _exc:
                                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                        watcher = threading.Thread(target=_watch_stop, daemon=True)
                        watcher.start()
                    try:
                        if stop_event is None or not stop_event.is_set():
                            sub.send_message(prompt)
                    finally:
                        done_event.set()
                        if watcher is not None and watcher.is_alive():
                            try:
                                watcher.join(timeout=0.5)
                            except Exception as _exc:
                                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                except Exception:
                    output_lines.append("[子代理执行中出现异常]")
            else:
                # 兜底：直接把 prompt 当作结果
                output_lines.append(f"[子代理无法执行，prompt: {prompt[:200]}]")

            result_text = "".join(str(x) for x in output_lines)
            if not result_text.strip():
                result_text = "(子代理完成但无文本输出)"

            with self._lock:
                if isinstance(self._agents.get(agent_id), dict):
                    self._agents[agent_id]["output"] = result_text
                    if self._agents[agent_id].get("status") != "stopped":
                        self._agents[agent_id]["status"] = "done"
                    self._agents[agent_id]["end_time"] = time.time()

        except Exception as e:
            try:
                with self._lock:
                    if isinstance(self._agents.get(agent_id), dict):
                        if self._agents[agent_id].get("status") != "stopped":
                            self._agents[agent_id]["status"] = "error"
                        self._agents[agent_id]["error"] = str(e)
                        self._agents[agent_id]["output"] = "".join(output_lines)
                        self._agents[agent_id]["end_time"] = time.time()
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        finally:
            # 清理子代理 ChatEngine 资源（文件监听器、MCP、session memory、子代理等）
            if sub is not None:
                try:
                    if hasattr(sub, "_fs_monitor") and sub._fs_monitor is not None:
                        sub._fs_monitor.stop()
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                # 停止子代理启动的 MCP 服务器，避免孤儿进程
                try:
                    mcp = getattr(sub, "mcp_manager", None)
                    if mcp is not None and hasattr(mcp, "stop_all"):
                        mcp.stop_all()
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                # 停止子代理的 session memory checkpoint 线程
                try:
                    if hasattr(sub, "_stop_session_memory_checkpoint"):
                        sub._stop_session_memory_checkpoint()
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                # 清理 stop_flags 中的 stop_event
                try:
                    with self._lock:
                        self._stop_flags.pop(agent_id, None)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                # 清理子 ChatEngine 自身的子代理（sub-sub-agent）：先 stop_all 再 stop_watchdog
                try:
                    if hasattr(sub, "_subagent") and sub._subagent is not None:
                        subagent = getattr(sub, "_subagent")
                        if hasattr(subagent, "stop_all"):
                            subagent.stop_all()
                        if hasattr(subagent, "stop_watchdog"):
                            subagent.stop_watchdog()
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)


def create_subagent_engine(parent_engine=None) -> SubAgentEngine:
    """便捷函数 — 创建一个新的子代理引擎实例"""
    return SubAgentEngine(parent_engine)
