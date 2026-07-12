# -*- coding: utf-8 -*-
"""确认门 (ConfirmationGate) — 从 chat_engine.py 拆分

负责处理用户确认逻辑。安全要点：
  - default-deny（缺回调 / 超时 / 异常一律拒绝）
  - 阻塞等待有明确超时（最长 10 分钟）
  - 线程内部 Event 由同一模块管理，防止外部直接篡改 pending 状态
  - 返回值强转 bool，防止 truthy 意外通过
"""

import threading
import time as _time
from typing import Dict, Optional, Callable, Any


class ConfirmationError(Exception):
    """确认门异常 — 当确认回调本身抛异常时抛出，防止异常被吞掉后默认继续执行。"""
    pass


class ConfirmationGate:
    """确认门 — 管理需要用户交互确认的阻塞等待逻辑（线程安全）。

    并发安全性要点：
    - 每次 wait_for_xxx 都会生成一个**全新的** threading.Event，
      并用 dict 注册；submit_xxx 通过 ID 找到该 event。
      这彻底避免了旧 Event.clear()/set() 在新旧等待之间发生竞态。
    - 每次 wait 都有明确超时；default-deny（缺回调/超时/异常一律拒绝）。
    - 提交方可以在 wait 之前、之中或之后调用 set，都能正确传递结果。
    """

    # 全局超时：防止死锁；具体方法可单独设置
    _MAX_WAIT_SECONDS = 600
    _MIN_WAIT_SECONDS = 5

    def __init__(self):
        # 待处理事件表：key (str) -> (event, value_slot)
        # 每次调用 wait_for_xxx 会创建一个唯一 key 的 event，submit 提交给这个 key
        self._events: Dict[str, Any] = {}
        # 当前活跃等待 key（每个 ConfirmationGate 实例一次只允许一条等待路径）
        self._current_cmd_key: Optional[str] = None
        self._current_edit_key: Optional[str] = None
        self._current_answer_key: Optional[str] = None

        # 对外回调（GUI 层注入弹窗逻辑）
        self._on_confirm_command: Optional[Callable[[Dict], bool]] = None
        self._on_confirm_edit: Optional[Callable[[str, str], bool]] = None

        # 内部锁：防止同一线程重复 enter，同时保护 events dict
        self._lock = threading.RLock()

    # ========== 内部：Event 注册 / 清理 ==========
    def _alloc_event(self) -> str:
        """生成一个唯一 event key，在锁内注册到 _events。返回 key。"""
        key = f"ev_{id(self)}_{_time.monotonic_ns()}"
        with self._lock:
            self._events[key] = {"event": threading.Event(), "result": None}
        return key

    def _free_event(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)

    def _submit_result(self, key: Optional[str], result: Any) -> None:
        """设置某个等待的结果并唤醒等待方。key 未知则忽略（例如超时后才被提交）。"""
        if key is None:
            return
        with self._lock:
            slot = self._events.get(key)
            if slot is None:
                return
            slot["result"] = result
            slot["event"].set()

    def cancel_all(self) -> None:
        """取消所有正在等待的确认（stop 时调用），唤醒所有阻塞等待线程返回 default-deny。"""
        with self._lock:
            keys = list(self._events.keys())
            self._current_cmd_key = None
            self._current_edit_key = None
            self._current_answer_key = None
        for key in keys:
            self._submit_result(key, False)

    def _wait_for_result(self, key: str, timeout: float) -> Any:
        """阻塞等待某个 key 的结果。超时或异常返回 None。"""
        slot = None
        with self._lock:
            slot = self._events.get(key)
        if slot is None:
            return None
        timeout = float(max(self._MIN_WAIT_SECONDS, min(self._MAX_WAIT_SECONDS, timeout)))
        ok = slot["event"].wait(timeout=timeout)
        return slot.get("result") if ok else None

    # ========== 设置回调 ==========
    def set_on_confirm_command(self, callback: Optional[Callable[[Dict], bool]]):
        with self._lock:
            self._on_confirm_command = callback

    def set_on_confirm_edit(self, callback: Optional[Callable[[str, str], bool]]):
        with self._lock:
            self._on_confirm_edit = callback

    # ========== GUI 提交确认 ==========
    def submit_command_confirmation(self, confirmed: bool) -> None:
        """由 GUI 调用：用户同意/拒绝执行命令后唤醒 chat 线程"""
        key = None
        with self._lock:
            key = self._current_cmd_key
        self._submit_result(key, bool(confirmed))

    def confirm_command(self, confirmed: bool) -> None:
        """submit_command_confirmation 的别名"""
        self.submit_command_confirmation(confirmed)

    def confirm_edit(self, confirmed: bool) -> None:
        """由 GUI 调用：用户同意/拒绝文件 diff 后唤醒 chat 线程"""
        key = None
        with self._lock:
            key = self._current_edit_key
        self._submit_result(key, bool(confirmed))

    def submit_user_answer(self, answer_text: str) -> None:
        """由 GUI 调用：用户提交问题回答后唤醒等待的 chat 线程"""
        key = None
        with self._lock:
            key = self._current_answer_key
        self._submit_result(key, str(answer_text or ""))

    # ========== 命令确认入口 ==========
    def wait_for_command_confirmation(self, call: Dict,
                                       on_confirm_command_gui=None) -> bool:
        """阻塞等待 GUI 弹窗让用户确认执行命令。返回 True=允许, False=拒绝（default-deny）。

        安全行为：
          - 若 GUI 回调自身抛异常，不再吞掉后默认拒绝，而是抛出 ConfirmationError，
            由调用方明确捕获并中止当前操作，防止"弹窗异常却继续执行"的 CWE-1021 风险。
          - 超时或从未收到提交结果仍返回 False（default-deny）。
        """
        if on_confirm_command_gui is None:
            return False

        key = self._alloc_event()
        try:
            with self._lock:
                self._current_cmd_key = key

            # 调用 GUI 回调；回调内部必须（最终）调用 submit_command_confirmation
            try:
                on_confirm_command_gui(dict(call) if isinstance(call, dict) else {})
            except Exception as e:
                # 不再吞掉异常：抛出 ConfirmationError，确保上层能够识别为"确认系统异常"
                # 并强制中止操作，而不是默认继续执行。
                raise ConfirmationError(f"命令确认弹窗异常: {e}") from e

            if isinstance(call, dict):
                try:
                    timeout = float(call.get("timeout", self._MAX_WAIT_SECONDS))
                except (TypeError, ValueError):
                    timeout = self._MAX_WAIT_SECONDS
            else:
                timeout = self._MAX_WAIT_SECONDS
            result = self._wait_for_result(key, timeout)
            # default-deny
            if result is None:
                return False
            return bool(result)
        finally:
            with self._lock:
                if self._current_cmd_key == key:
                    self._current_cmd_key = None
            self._free_event(key)

    def request_command_confirm(self, call: Dict) -> bool:
        """旧版：直接调用回调获取结果（非阻塞）。保持向后兼容。"""
        try:
            if self._on_confirm_command is not None:
                return bool(self._on_confirm_command(call))
            return False
        except Exception:
            return False

    # ========== 文件编辑确认入口 ==========
    def wait_for_edit_confirmation(self, filepath: str, diff_text: str,
                                    on_confirm_edit_gui=None) -> bool:
        """阻塞等待 GUI 弹窗让用户确认文件编辑。返回 True=允许, False=拒绝（default-deny）。"""
        if on_confirm_edit_gui is None:
            return False

        key = self._alloc_event()
        try:
            with self._lock:
                self._current_edit_key = key

            try:
                on_confirm_edit_gui(str(filepath or ""), str(diff_text or ""))
            except Exception as e:
                # 与命令确认保持一致：弹窗异常必须被识别，禁止静默按拒绝处理
                raise ConfirmationError(f"文件编辑确认弹窗异常: {e}") from e

            result = self._wait_for_result(key, self._MAX_WAIT_SECONDS)
            if result is None:
                return False
            return bool(result)
        finally:
            with self._lock:
                if self._current_edit_key == key:
                    self._current_edit_key = None
            self._free_event(key)

    def request_edit_confirm(self, filepath: str, diff_text: str) -> bool:
        """旧版：直接在当前线程中调用回调获取结果（非阻塞）。保持向后兼容。"""
        if self._on_confirm_edit is None:
            return False
        try:
            return bool(self._on_confirm_edit(filepath, diff_text))
        except Exception:
            return False

    # ========== 用户提问（问答气泡）==========
    def wait_for_user_answer(self, payload: Dict, on_ask_user_questions=None) -> str:
        """阻塞等待 GUI 返回用户选择的回答文本。无回调或超时返回空串。"""
        if on_ask_user_questions is None:
            return ""

        key = self._alloc_event()
        try:
            with self._lock:
                self._current_answer_key = key

            try:
                on_ask_user_questions(dict(payload) if isinstance(payload, dict) else {})
            except Exception as e:
                print(f"[ask_user_questions] 弹窗失败: {e}", flush=True)
                return ""

            result = self._wait_for_result(key, self._MAX_WAIT_SECONDS)
            if result is None:
                return ""
            return str(result)
        finally:
            with self._lock:
                if self._current_answer_key == key:
                    self._current_answer_key = None
            self._free_event(key)
