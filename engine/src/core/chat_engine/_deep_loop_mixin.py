# -*- coding: utf-8 -*-
"""DeepLoopMixin — 深度循环（单轮/步骤/工具执行/计划阶段）核心循环（拆分自 chat_engine.py）。"""
from __future__ import annotations

from src.core.chat_engine._imports import *  # noqa: F401, F403




class DeepLoopMixin(object):
    """深度对话循环相关方法。"""

    def _run_single_turn(self, turn_counter: int, allow_tools: bool,
                         phase_label: str = "EXEC",
                         is_final_if_no_tools: bool = False) -> bool:
        """单轮调用 — 从构建 system prompt 到流式输出 + 解析工具调用 + 执行。
        allow_tools=False 时: 即使模型在 reasoning 中写了工具调用 JSON，也不执行。
        返回 True 表示"本轮无工具调用 -> 整个对话可以结束";
        返回 False 表示"本轮执行了工具调用 -> 可以继续下一轮"。"""
        if self.on_new_turn:
            self.on_new_turn(turn_counter)

        if self.history is None:
            if self.on_error:
                self.on_error(0, "历史记录未初始化")
            self._emit_finished(archive=False)
            return True

        system_prompt = self._build_system_prompt()
        api_messages = self.history.to_api_messages(
            system_prompt, language=self.cfg.get("language", "zh") or "zh"
        )

        if self.on_status_update:
            self.on_status_update(f"调用模型 ({self.cfg.model}) [{phase_label}] ...")

        try:
            usage = self._stream_chat(api_messages)
        except APIError as e:
            return self._handle_turn_api_error(e)

        # 图片只在当前用户消息的第一轮模型调用中携带，后续工具/深度循环不再重复发送
        self._clear_images_for_message()

        content = usage.get("content", "")
        reasoning = usage.get("reasoning_content", "")
        # Think mode fallback: model puts everything in reasoning, nothing in content
        if not content and reasoning:
            content = reasoning

        # token / cost 累计
        self._accumulate_usage(usage)

        # ── Kun Runtime: 记录用量 ──
        try:
            if self._session_id:
                self._kun.record_usage(self._session_id, usage, model=self._get_model())
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        # 如果流式请求已被取消或引擎已停止，不再追加助手消息 / 不执行工具
        if usage.get("cancelled") or self._is_stopped():
            self._emit_finished(archive=False)
            return True

        # 获取工具调用：优先使用 DeepSeek V4 原生 tool_calls，
        # 其次从 reasoning 文本中解析，再 fallback 到 content（think 模式下可能分离）
        tool_calls = usage.get("tool_calls") or []
        if not tool_calls:
            tool_calls = self._extract_tool_calls(reasoning)
        if not tool_calls:
            tool_calls = self._extract_tool_calls(content)

        if not allow_tools and tool_calls:
            # Planning 阶段: 忽略工具调用（不执行），但在 status 上告知
            if self.on_status_update:
                self.on_status_update("计划已生成（计划阶段不执行工具调用）")
            tool_calls = []

        # 写入 history: assistant 消息
        self._record_assistant_message(content, reasoning, tool_calls)

        if not tool_calls:
            return self._handle_no_tool_calls(content, usage, is_final_if_no_tools)

        # 有工具调用 — 逐一轮执行（仅 allow_tools=True 时到这里）
        return self._execute_tool_calls_in_turn(tool_calls, usage)

    def _handle_turn_api_error(self, exc: APIError) -> bool:
        """处理单轮调用中的 APIError：499 视为用户取消不归档，其余归档为 error。"""
        # 499 为用户主动取消，不视为错误，也不重复归档
        if exc.code == 499:
            self._emit_finished(archive=False)
            return True
        self._archive_session_memory("error", error_message=f"APIError {exc.code}: {exc.message}")
        if self.on_error:
            self.on_error(exc.code, exc.message)
        self._emit_finished(archive=False)
        return True  # 停止本轮，API 调用失败

    def _extract_tool_calls(self, reasoning: str) -> List[Dict]:
        """从模型 reasoning_content 中解析工具调用列表。"""
        tool_calls: List[Dict] = []
        if self.tool_exec is not None:
            try:
                tool_calls = self.tool_exec.extract_tool_calls(reasoning)
            except Exception as e:
                logger.warning("解析工具调用失败: %s", e, exc_info=True)
                tool_calls = []
        return tool_calls

    def _record_assistant_message(self, content: str, reasoning: str,
                                  tool_calls: List[Dict]) -> None:
        """记录助手消息：触发回调、写入 history、记录到会话短时记忆并 checkpoint。"""
        if self.on_assistant_message:
            try:
                self.on_assistant_message(content, reasoning, tool_calls)
            except Exception as _exc:
                logger.debug("on_assistant_message 回调异常: %s", _exc, exc_info=True)
        if self.history is not None:
            self.history.add_assistant_message(content, reasoning, tool_calls=tool_calls)

        # 会话短时记忆：记录助手消息并立即 checkpoint
        try:
            if self.session_memory is not None:
                self.session_memory.add_chat("assistant", content)
                if reasoning:
                    self.session_memory.add_chat("reasoning", reasoning[:2000])
                self.session_memory.save_checkpoint()
        except Exception as _exc:
            logger.debug("记录会话助手消息失败: %s", _exc, exc_info=True)

    def _handle_no_tool_calls(self, content: str, usage: Dict,
                              is_final_if_no_tools: bool) -> bool:
        """处理本轮无工具调用的情况：设置 pending_plan、触发完成回调与状态栏刷新。"""
        with self._state_lock:
            _phase = self._state.get("phase")
            _plan_confirmed = bool(self._state.get("plan_confirmed"))
            # M-1 修复: 仅在 planning 阶段设置 pending_plan，execution 阶段最终回答不污染该字段
            if _phase == "planning" and not _plan_confirmed:
                self._state["pending_plan"] = content
        # 计划等待用户确认时，不归档会话（continue_from_plan 后续仍需 checkpoint 保护）
        _is_awaiting_plan_confirm = (_phase == "planning" and not _plan_confirmed)
        if is_final_if_no_tools:
            # 保存 plan 内容并触发 GUI 确认弹窗
            if self.on_plan_ready and _is_awaiting_plan_confirm:
                self.on_plan_ready(content)
            self._emit_finished(archive=not _is_awaiting_plan_confirm)
        else:
            self._emit_finished(archive=not _is_awaiting_plan_confirm)
        try:
            self._refresh_status_bar(usage)
        except Exception as _exc:
            logger.debug("刷新状态栏失败（已完成收尾，不影响结果）: %s", _exc, exc_info=True)
        return True

    def _execute_tool_calls_in_turn(self, tool_calls: List[Dict], usage: Dict) -> bool:
        """在单轮中逐一执行工具调用，处理 ask_user_questions / exit_conversation 等特殊情况。

        返回 True 表示本轮应结束（如模型主动 exit_conversation），返回 False 表示可继续下一轮。
        """
        if self.on_status_update:
            self.on_status_update(f"执行 {len(tool_calls)} 个工具调用...")

        any_ok = False
        for call in tool_calls:
            if self._is_stopped():
                break
            # 规范化工具调用格式：OpenAI 原生 tool_calls vs 旧 JSON 格式
            name = call.get("tool", "")
            if not name:
                fn = call.get("function", {})
                if isinstance(fn, dict):
                    name = fn.get("name", "")
                    try:
                        raw_args = fn.get("arguments", "{}")
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        if isinstance(args, dict):
                            for k, v in args.items():
                                if k not in call:
                                    call[k] = v
                        call["tool"] = name
                    except (json.JSONDecodeError, TypeError):
                        pass
            if not name:
                continue
            # 状态前缀（由 GUI 上色）
            self._emit_status(self._tool_status_type(name), "开始执行工具: " + str(name))

            # ── Kun Runtime: 记录工具调用遥测 ──
            try:
                self._kun.record_tool_call(name)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

            if self.on_tool_call_detected:
                try:
                    self.on_tool_call_detected(call)
                except Exception as _exc:
                    logger.debug("on_tool_call_detected 回调异常: %s", _exc, exc_info=True)
            self._emit_tool_start(call)
            # run_command / run_command_stream 的确认在 tool_exec 内部通过 on_confirm_command 处理
            if self.tool_exec is None:
                if self.on_error:
                    self.on_error(0, "工具执行器未初始化")
                self._emit_tool_end(name, False, "工具执行器未初始化")
                break
            # H-2 修复: 使用 _execute_tool_call 统一入口，获得幂等工具的瞬时错误重试能力
            ok, result = self._execute_tool_call(call)
            self._emit_tool_end(name, ok, result)
            # 执行后状态: 成功 -> done，失败 -> error
            if ok:
                any_ok = True
                self._emit_status("done", "完成: " + str(name))
            else:
                self._emit_status("error", "失败: " + str(name))

            if name == "ask_user_questions" and ok and isinstance(result, str) \
                    and result.startswith("__ASK_USER_QUESTIONS__"):
                try:
                    payload = json.loads(result[len("__ASK_USER_QUESTIONS__"):])
                except Exception:
                    payload = {"questions": []}
                user_answer = self._wait_for_user_answer(payload)
                if self.on_tool_result:
                    try:
                        self.on_tool_result(name, user_answer)
                    except Exception as _exc:
                        logger.debug("on_tool_result 回调异常: %s", _exc, exc_info=True)
                if self.history is not None:
                    self.history.add_tool_result(name, user_answer, call.get("id", ""))
                # 会话短时记忆：用户回答也应记录，保证链路一致
                try:
                    if self.session_memory is not None:
                        self.session_memory.add_chat("user", str(user_answer))
                        self.session_memory.save_checkpoint()
                except Exception as _exc:
                    logger.debug("记录用户回答到 session_memory 失败: %s", _exc, exc_info=True)
                continue
            if self.on_tool_result:
                try:
                    self.on_tool_result(name, result)
                except Exception as _exc:
                    logger.debug("on_tool_result 回调异常: %s", _exc, exc_info=True)
            if self.history is not None:
                self.history.add_tool_result(name, result, call.get("id", ""))
            # 会话短时记忆：记录工具调用摘要并立即 checkpoint
            try:
                if self.session_memory is not None:
                    summary = str(result)
                    if len(summary) > 1000:
                        summary = summary[:1000] + "..."
                    self.session_memory.add_chat("tool", f"[{name}] {summary}")
                    self.session_memory.save_checkpoint()
            except Exception as _exc:
                logger.debug("记录会话工具结果失败: %s", _exc, exc_info=True)
            if name == "exit_conversation":
                if self.on_exit_conversation:
                    self.on_exit_conversation(call.get("reason", "模型主动退出对话"))
                self._emit_finished()
                return True

        if self.history is not None and self.history.tool_call_counter >= TOOL_CALL_THRESHOLD:
            if self.on_need_new_agent:
                self.on_need_new_agent()
            self.history.reset_tool_counter()

        # 检测本轮是否启动了子代理 — 如果是，标记并让外层循环暂停等待
        subagent_tools = {"start_subagent", "delegate_subagent"}
        for call in tool_calls:
            if call.get("tool", "") in subagent_tools:
                with self._state_lock:
                    self._state["subagents_launched"] = True
                break

        self._refresh_status_bar(usage)
        return False

    def _execute_tool_call(self, call: Dict) -> Tuple[bool, str]:
        """执行单个工具调用；对只读/幂等工具的瞬时错误重试1次，非幂等操作不重试。

        M-6 修复: 非幂等操作（run_command/delete_file/git_commit 等）即使遇到超时/网络错误
        也不重试。超时不代表命令未执行（命令可能已在运行但响应丢失），重试会导致重复执行。
        """
        if self.tool_exec is None:
            return False, "[_execute_tool_call] 工具执行器未初始化"
        ok, result = self.tool_exec.execute(call)
        if ok:
            return True, result
        name = call.get("tool", "")
        # 非幂等操作一律不重试（无论错误类型）
        _non_idempotent = {"delete_file", "git_commit", "run_command", "run_command_stream",
                           "execute_custom", "create_file", "modify_file", "edit_file",
                           "insert_lines", "delete_lines", "replace_in_files",
                           "git_reset", "git_checkout", "git_stash", "mark_delete",
                           "create_snapshot", "restore_snapshot", "undo_last",
                           "mcp_add", "mcp_remove", "mcp_start", "mcp_stop",
                           "format_code", "rename_symbol", "delegate_subagent"}
        if name in _non_idempotent:
            self._emit_status("error", f"工具 {name} 执行失败（非幂等操作不自动重试）")
            return False, result
        # 对只读/幂等工具（read_file/search_code/web_fetch 等）的瞬时/网络类错误重试1次
        _transient_markers = (
            # 英文瞬时错误标志
            "timeout", "connection", "network", "timed out", "refused", "reset",
            "temporarily", "unreachable", "fail", "failed", "failure",
            # 中文瞬时错误标志
            "超时", "连接", "网络", "临时失败", "失败", "重试", "不可达", "中断",
        )
        result_lower = str(result).lower()
        is_transient = any(m in result_lower for m in _transient_markers)
        if not is_transient:
            return False, result
        for retry in range(1, self._MAX_TOOL_RETRIES + 1):
            self._emit_status("error", f"工具 {name} 第 {retry} 次执行失败，立即重试")
            ok, result = self.tool_exec.execute(call)
            if ok:
                self._emit_status("done", f"工具 {name} 重试成功")
                return True, result
        self._emit_status("error", f"工具 {name} 重试后仍失败")
        return False, result

    def _execute_step(self, iteration: int, max_iterations: int) -> Tuple[str, Optional[int]]:
        """执行一轮深度循环（一个 step）。

        Returns:
            ("success", None)  step 正常，至少有一个工具成功
            ("failure", None)  step 失败但可继续（非 4xx）
            ("fatal", code)    4xx / 认证错误，调用方应终止循环
            ("finished", None) 模型给出最终回答或主动 exit_conversation
        """
        if self.on_deep_mode_status:
            try:
                self.on_deep_mode_status(
                    f"深度执行中… 第 {iteration} 轮 / step {iteration}"
                )
            except Exception as _exc:
                logger.debug("深度状态回调异常: %s", _exc, exc_info=True)

        if self.on_new_turn:
            self.on_new_turn(iteration)
        if self.on_status_update:
            self.on_status_update(f"深度执行第 {iteration}/{max_iterations} 轮...")

        system_prompt = self._build_system_prompt()
        api_messages = self.history.to_api_messages(
            system_prompt, language=self.cfg.get("language", "zh") or "zh"
        )

        try:
            usage = self._call_model(api_messages)
        except APIError as e:
            return self._handle_step_api_error(e)

        # 图片只在深度循环的第一次模型调用中携带，后续迭代不再重复发送
        self._clear_images_for_message()

        content = usage.get("content", "")
        reasoning = usage.get("reasoning_content", "")
        # Think mode fallback: model puts everything in reasoning, nothing in content
        if not content and reasoning:
            content = reasoning

        # token / cost 累计
        self._accumulate_usage(usage)

        # Kun Runtime 用量记录
        try:
            if self._session_id:
                self._kun.record_usage(self._session_id, usage, model=self._get_model())
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        # 深度模式：请求被取消或已停止时，不再追加助手消息
        if usage.get("cancelled") or self._is_stopped():
            return "stopped", None

        # 解析工具调用：优先 native tool_calls，其次文本提取
        tool_calls = usage.get("tool_calls") or []
        if not tool_calls:
            try:
                tool_calls = self.tool_exec.extract_tool_calls(reasoning)
                if not tool_calls:
                    tool_calls = self.tool_exec.extract_tool_calls(content)
            except Exception as e:
                logger.warning("解析工具调用失败: %s", e, exc_info=True)
                tool_calls = []

        self._record_step_assistant_message(content, reasoning, tool_calls)

        # 没有工具调用 -> 视为最终回答
        if not tool_calls:
            self._emit_finished()
            try:
                self._refresh_status_bar(usage)
            except Exception as _exc:
                logger.debug("刷新状态栏失败（已完成收尾，不影响结果）: %s", _exc, exc_info=True)
            return "finished", None

        # 执行工具调用
        return self._execute_step_tools(tool_calls, usage)

    def _handle_step_api_error(self, exc: APIError) -> Tuple[str, Optional[int]]:
        """处理 step 执行中的 APIError：499 为 stopped，4xx 为 fatal，其余为 failure。"""
        # 499 为用户主动取消，按 stopped 处理，避免被归档为 fatal/finished
        if exc.code == 499:
            return "stopped", None
        if self.on_error:
            try:
                self.on_error(exc.code, exc.message)
            except Exception as _exc:
                logger.debug("错误回调异常: %s", _exc, exc_info=True)
        if 400 <= exc.code < 500 and exc.code != 429:
            return "fatal", exc.code
        return "failure", None

    def _record_step_assistant_message(self, content: str, reasoning: str,
                                       tool_calls: List[Dict]) -> None:
        """记录深度模式 step 的助手消息：触发回调、写入 history、记录到会话短时记忆并 checkpoint。"""
        if self.on_assistant_message:
            try:
                self.on_assistant_message(content, reasoning, tool_calls)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        if self.history is not None:
            self.history.add_assistant_message(content, reasoning, tool_calls=tool_calls)

        # 会话短时记忆：记录深度模式助手消息并 checkpoint
        try:
            if self.session_memory is not None:
                self.session_memory.add_chat("assistant", content)
                if reasoning:
                    self.session_memory.add_chat("reasoning", reasoning[:2000])
                self.session_memory.save_checkpoint()
        except Exception as _exc:
            logger.debug("记录深度模式会话消息失败: %s", _exc, exc_info=True)

    def _execute_step_tools(self, tool_calls: List[Dict],
                            usage: Dict) -> Tuple[str, Optional[int]]:
        """在 step 中执行工具调用，处理子代理等待与阈值重置。

        返回 ("success", None) 至少一个工具成功；("failure", None) 无工具成功；
        ("finished", None) 模型主动 exit_conversation。
        """
        # 执行工具调用
        if self.on_status_update:
            self.on_status_update(f"执行 {len(tool_calls)} 个工具调用...")

        any_ok = False
        for call in tool_calls:
            if self._is_stopped():
                break
            name = call.get("tool", "")
            self._emit_status(self._tool_status_type(name), "开始执行工具: " + str(name))

            # Kun Runtime 工具调用遥测
            try:
                self._kun.record_tool_call(name)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

            if self.on_tool_call_detected:
                try:
                    self.on_tool_call_detected(call)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

            self._emit_tool_start(call)
            ok, result = self._execute_tool_call(call)
            self._emit_tool_end(name, ok, result)
            if ok:
                any_ok = True
                self._emit_status("done", "完成: " + str(name))
            else:
                self._emit_status("error", "失败: " + str(name))

            if name == "ask_user_questions" and ok and isinstance(result, str) \
                    and result.startswith("__ASK_USER_QUESTIONS__"):
                try:
                    payload = json.loads(result[len("__ASK_USER_QUESTIONS__"):])
                except Exception:
                    payload = {"questions": []}
                user_answer = self._wait_for_user_answer(payload)
                if self.on_tool_result:
                    try:
                        self.on_tool_result(name, user_answer)
                    except Exception as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                if self.history is not None:
                    self.history.add_tool_result(name, user_answer, call.get("id", ""))
                continue

            if name == "exit_conversation":
                if self.on_exit_conversation:
                    try:
                        self.on_exit_conversation(call.get("reason", "模型主动退出对话"))
                    except Exception as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                self._emit_finished()
                return "finished", None

            if self.on_tool_result:
                try:
                    self.on_tool_result(name, result)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            if self.history is not None:
                self.history.add_tool_result(name, result, call.get("id", ""))

            # 会话短时记忆：记录深度模式工具结果并 checkpoint
            try:
                if self.session_memory is not None:
                    summary = str(result)
                    if len(summary) > 1000:
                        summary = summary[:1000] + "..."
                    self.session_memory.add_chat("tool", f"[{name}] {summary}")
                    self.session_memory.save_checkpoint()
            except Exception as _exc:
                logger.debug("记录深度模式工具结果失败: %s", _exc, exc_info=True)

        # 检测是否启动了子代理并等待其完成
        subagent_tools = {"start_subagent", "delegate_subagent"}
        launched = any(call.get("tool", "") in subagent_tools for call in tool_calls)
        if launched and self._subagent is not None:
            if self.on_status_update:
                self.on_status_update("等待子代理完成...")
            self._subagent.wait_for_all(timeout=600, stop_check=self._is_stopped)
            results = self._subagent.get_results()
            if self.history is not None and results:
                self.history.add_tool_result("subagent_results", results)
            if self.on_tool_result:
                self.on_tool_result("subagent_results", results)

        if self.history is not None and self.history.tool_call_counter >= TOOL_CALL_THRESHOLD:
            if self.on_need_new_agent:
                try:
                    self.on_need_new_agent()
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            self.history.reset_tool_counter()

        self._refresh_status_bar(usage)

        if not any_ok:
            return "failure", None
        return "success", None

    # _run_deep_loop 已移除 —— deep 模式现已合并到 ultra 模式中，统一使用 _run_ultra_loop。

    # ========== Ultra 模式 ==========
    def _run_ultra_loop(self, user_message: str):
        """Ultra 模式闭环：规划 → 执行 → 验证 → 迭代（最多3轮） → 交付。

        复用 deep loop 的 _execute_step 基础设施，但在执行后增加验证环节：
        1. 规划：生成详细执行计划（只读，不调用工具）
        2. 执行：按计划调用工具执行（复用 deep loop 迭代）
        3. 验证：检查执行结果，若发现问题则回到步骤2（最多3轮迭代）
        4. 交付：确认无误后给出最终结果
        """
        if self.sandbox is None or self.history is None or self.tool_exec is None:
            if self.on_error:
                self.on_error(0, "Ultra 模式需要已初始化的项目沙箱与工具执行器")
            return

        with self._state_lock:
            self._state["phase"] = "ultra_executing"
            self._state["plan_confirmed"] = True

        # 前置快照
        with self._state_lock:
            _auto_pre_snapshot = bool(self._state.get("auto_pre_snapshot"))
        if _auto_pre_snapshot and self.sandbox is not None:
            try:
                snap = self.sandbox.snapshot("pre-ultra-auto")
                if self.on_status_update:
                    self.on_status_update(
                        f"已创建前置快照: {snap.get('name')} (id={snap.get('id','?')[:8]}...)"
                    )
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            with self._state_lock:
                self._state["auto_pre_snapshot"] = False

        # --- Phase 1: 规划（只读一轮）---
        if self.on_status_update:
            self.on_status_update("【Ultra · 规划】正在生成执行计划...")
        try:
            self._run_single_turn(
                turn_counter=1,
                allow_tools=False,
                phase_label="ULTRA_PLANNING",
                is_final_if_no_tools=True,
            )
        except Exception as _exc:
            logger.warning("Ultra 规划阶段异常: %s", _exc, exc_info=True)

        if self._is_stopped():
            self._emit_finished(archive=False)
            return

        # --- Phase 2-3: 执行 + 验证迭代（最多3轮）---
        max_verify_rounds = 3
        for verify_round in range(1, max_verify_rounds + 1):
            if self._is_stopped():
                break
            if self.on_status_update:
                self.on_status_update(f"【Ultra · 执行】第 {verify_round} 轮执行...")

            # 执行：复用 deep loop 的迭代机制（最多20轮/次）
            max_iterations = 20
            consecutive_failures = 0
            iteration = 1
            while iteration <= max_iterations and not self._is_stopped():
                outcome, code = self._execute_step(iteration, max_iterations)
                if outcome == "finished":
                    break
                if outcome == "stopped":
                    self._emit_finished(archive=False)
                    return
                if outcome == "fatal":
                    self._archive_session_memory("error", error_message=f"API fatal error: {code}")
                    self._emit_finished(archive=False)
                    return
                if outcome == "failure":
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
                if consecutive_failures >= 3:
                    if self.on_status_update:
                        self.on_status_update("连续 3 轮失败，跳到验证环节...")
                    break
                iteration += 1

            if self._is_stopped():
                break

            # 验证：要求模型自检执行结果
            if verify_round < max_verify_rounds:
                if self.on_status_update:
                    self.on_status_update(f"【Ultra · 验证】第 {verify_round} 轮验证...")
                # 注入验证提示，让模型检查是否需要修正
                verify_msg = (
                    "请验证上述执行结果是否正确完成。"
                    "如果发现问题，请指出并重新执行修正；"
                    "如果一切正确，请给出最终交付结果。"
                )
                if self.history is not None:
                    self.history.add_user_message(verify_msg)
                try:
                    usage = self._stream_chat(
                        self.history.to_api_messages(
                            self._build_system_prompt(),
                            language=self.cfg.get("language", "zh") or "zh",
                        )
                    )
                    content = usage.get("content", "")
                    reasoning = usage.get("reasoning_content", "")
                    if not usage.get("cancelled") and not self._is_stopped():
                        if self.on_assistant_message:
                            self.on_assistant_message(content, reasoning, [])
                        if self.history is not None:
                            self.history.add_assistant_message(content, reasoning)
                        self._accumulate_usage(usage)
                    # 如果模型没有调用工具且给出了最终答案，说明验证通过
                    if not usage.get("tool_calls"):
                        break
                except Exception as _exc:
                    logger.warning("Ultra 验证阶段异常: %s", _exc, exc_info=True)
                    break
            else:
                # 最后一轮不再验证，直接交付
                break

        # --- Phase 4: 交付（生成最终总结）---
        if not self._is_stopped() and self.history is not None:
            if self.on_status_update:
                self.on_status_update("【Ultra · 交付】生成最终交付...")
            if self.on_new_turn:
                self.on_new_turn(99)
            try:
                usage = self._stream_chat(
                    self.history.to_api_messages(
                        self._build_system_prompt(),
                        language=self.cfg.get("language", "zh") or "zh",
                    )
                )
                content = usage.get("content", "")
                reasoning = usage.get("reasoning_content", "")
                if not usage.get("cancelled") and not self._is_stopped():
                    if self.on_assistant_message:
                        self.on_assistant_message(content, reasoning, [])
                    if self.history is not None:
                        self.history.add_assistant_message(content, reasoning)
                    try:
                        if self.session_memory is not None:
                            self.session_memory.add_chat("assistant", content)
                            if reasoning:
                                self.session_memory.add_chat("reasoning", reasoning[:2000])
                            self.session_memory.save_checkpoint()
                    except Exception as _exc:
                        logger.debug("记录 Ultra 交付轮失败: %s", _exc, exc_info=True)
                self._accumulate_usage(usage)
                if self._is_stopped():
                    self._emit_finished(archive=False)
                else:
                    self._emit_finished()
                try:
                    self._refresh_status_bar(usage)
                except Exception as _exc:
                    logger.debug("刷新状态栏失败: %s", _exc, exc_info=True)
            except APIError as e:
                self._archive_session_memory("error", error_message=e.message)
                if self.on_error:
                    self.on_error(e.code, e.message)
                self._emit_finished(archive=False)
        else:
            self._emit_finished(archive=False)

    # ========== 计划控制 / 斜杠命令 ==========
    def confirm_plan(self):
        """GUI 调用: 用户点击"确认计划"，进入执行阶段"""
        with self._state_lock:
            self._state["plan_confirmed"] = True
            self._state["phase"] = "executing"
            # 注意：不在这里清空 pending_plan，continue_from_plan() 需要读取它来构造带摘要的 prompt
            self._state["auto_pre_snapshot"] = True

    def reject_plan(self):
        """GUI 调用: 用户点击"放弃计划"，重置为 planning 状态"""
        with self._state_lock:
            self._state["phase"] = "planning"
            self._state["plan_confirmed"] = False
            self._state["pending_plan"] = None

    def continue_from_plan(self, cancel_event=None):
        """计划确认后继续执行；如果未实现或未确认，回落到标准 send_message。"""
        try:
            with self._state_lock:
                _phase = self._state.get("phase")
                _plan_confirmed = bool(self._state.get("plan_confirmed"))
                _pending_plan = self._state.get("pending_plan")
            if _phase == "planning" and not _plan_confirmed:
                self.confirm_plan()
            prompt = None
            try:
                if isinstance(_pending_plan, str) and _pending_plan.strip():
                    snippet = _pending_plan.strip()
                    if len(snippet) > 400:
                        snippet = snippet[:400] + "..."
                    prompt = (
                        "以下是此前生成的执行计划，请按照计划执行对应操作：\n"
                        + snippet
                        + "\n\n请以实际工具调用/文件修改完成。"
                    )
            except Exception:
                prompt = None
            if not prompt:
                prompt = "请按此前已确认的计划执行（调用必要工具）。"
            # 使用完 pending_plan 后清空，避免后续执行阶段被污染
            with self._state_lock:
                self._state["pending_plan"] = None
            self.send_message(prompt, cancel_event=cancel_event)
        except Exception as e:
            try:
                if self.on_error:
                    self.on_error(0, f"continue_from_plan 异常: {e}")
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def reset_phase(self):
        """重置为初始 planning 状态（新项目 / 新对话开始时）"""
        with self._state_lock:
            self._state["phase"] = "planning"
            self._state["plan_confirmed"] = False
            self._state["pending_plan"] = None
            self._state["auto_pre_snapshot"] = True

    def get_phase(self) -> str:
        """返回当前阶段: 'planning' 或 'executing'"""
        with self._state_lock:
            return str(self._state.get("phase", "planning"))

