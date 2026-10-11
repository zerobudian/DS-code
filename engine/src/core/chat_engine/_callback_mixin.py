# -*- coding: utf-8 -*-
"""Callbacks and instance-scoped status refresh for ChatEngine."""
from __future__ import annotations

from src.core.chat_engine._imports import (
    Any,
    ConfirmationError,
    Dict,
    List,
    Optional,
    threading,
    time,
)

import logging

logger = logging.getLogger(__name__)




class CallbackMixin(object):
    """回调、状态栏与健康检查相关方法。"""

    @staticmethod
    def _tool_status_type(name: str) -> str:
        """根据工具名判断执行状态类型（reading/writing/coding）。

        把分散在两处的工具分类表集中到这里，避免 _run_single_turn 与
        _run_deep_loop 出现不一致（原 deep 循环中写的是 mark_delete，
        而单次循环写的是 mark_delete_file，属于潜在维护风险）。
        """
        if not isinstance(name, str):
            return "writing"
        reading_tools = {
            "read_file", "list_files", "list_preview_files", "web_fetch",
            "web_search", "http_request", "docs_lookup",
        }
        writing_tools = {
            "create_file", "write_file", "modify_file", "edit_file",
            "insert_lines", "delete_lines", "replace_in_files",
            "rename_symbol", "write_section", "mark_delete_file",
            "mark_delete", "update_ds_section",
        }
        coding_tools = {
            "run_command", "run_command_stream", "git_commit", "git_checkout",
            "git_status", "git_diff", "git_branch", "git_log", "mcp_call",
            "execute_custom", "sklii_call",
        }
        if name in reading_tools:
            return "reading"
        if name in writing_tools:
            return "writing"
        if name in coding_tools:
            return "coding"
        return "writing"

    def _on_tool_output_line(self, line: str):
        """工具逐行输出回调 —— 转发到 GUI（on_content_delta + on_output_line 双通道）。"""
        if self.on_content_delta:
            try:
                self.on_content_delta(line)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        if self.on_output_line:
            try:
                self.on_output_line(line)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def _on_artifact_updated(self, artifact_list: List[Dict[str, Any]]) -> None:
        """ArtifactManager 检测到 artifact 变化时，转发给 GUI 回调。"""
        if self.on_artifact_updated:
            try:
                self.on_artifact_updated([dict(a) for a in artifact_list])
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def _emit_status(self, status_type: str, text: str):
        """轻量级状态推送: coding/reading/writing/done/error.
        由 GUI 的 StreamingStatusBubble 展示彩色前缀行。"""
        status_type = str(status_type or "").lower()
        if status_type not in ("coding", "reading", "writing", "done", "error"):
            status_type = "writing"
        try:
            if self.on_tool_status:
                self.on_tool_status(str(text or ""), status_type)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    @staticmethod
    def _tool_args_summary(call: Dict) -> str:
        """从工具调用字典中提取参数摘要，避免打印完整大对象。"""
        args = call.get("arguments") or call.get("args") or {}
        if not isinstance(args, dict):
            return str(args)[:120]
        if not args:
            return "(无参数)"
        items = []
        for k, v in args.items():
            s = str(v)
            if len(s) > 80:
                s = s[:77] + "..."
            items.append(f"{k}={s}")
        return ", ".join(items)[:200]

    @staticmethod
    def _tool_result_summary(result, ok: bool) -> str:
        """生成工具执行结果摘要，限制长度，失败时保留错误信息。"""
        text = str(result) if result is not None else ""
        if not text:
            return "执行成功" if ok else "执行失败"
        if len(text) > 300:
            text = text[:297] + "..."
        return text

    def _emit_tool_start(self, call: Dict):
        """工具开始执行时回调 GUI。"""
        if self.on_tool_start:
            try:
                name = call.get("tool", "")
                self.on_tool_start(str(name), dict(call))
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def _emit_tool_end(self, name: str, ok: bool, result):
        """工具执行结束时回调 GUI。"""
        if self.on_tool_end:
            try:
                self.on_tool_end(str(name), bool(ok), self._tool_result_summary(result, ok))
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def _current_permission_mode(self) -> str:
        """读取当前权限模式：full / coder / reviewer。"""
        try:
            return self.cfg.permission_mode
        except Exception:
            return "coder"

    def _on_edit_apply_request(self, filepath: str, diff_text: str) -> bool:
        """内部钩子: diff 确认请求。full 权限直接放行，其它走 ConfirmationGate。"""
        if self._current_permission_mode() == "full":
            return True
        return self._gate.wait_for_edit_confirmation(filepath, diff_text, self.on_confirm_edit)

    def _on_confirm_command_engine(self, call: dict) -> bool:
        """内部钩子: 命令确认请求。full 权限直接放行，其它走 ConfirmationGate。

        若确认系统自身异常，转换为 False，确保上层工具执行器按"拒绝"处理。
        """
        if self._current_permission_mode() == "full":
            return True
        try:
            return self._gate.wait_for_command_confirmation(call, self.on_confirm_command)
        except ConfirmationError as e:
            logger.info("命令确认门异常，按拒绝处理: %s", e)
            logger.debug("命令确认门异常详情", exc_info=True)
            return False

    def _on_conversation_title_changed(self, title: str):
        """模型调用 set_conversation_title 后触发上层回调刷新 UI。"""
        if self.on_conversation_title_changed:
            try:
                self.on_conversation_title_changed(str(title))
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def _files_changed(self):
        """文件系统变化 —— 触发上层回调，并失效项目上下文缓存"""
        # 失效项目上下文缓存，下次构建 system prompt 时重建
        with self._state_lock:
            self._state["project_context_cache"] = None
        if self.on_files_changed:
            try:
                self.on_files_changed()
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def _archive_session_memory(self, status: str, error_message: Optional[str] = None) -> None:
        """把当前会话短时记忆归档，并在结束时抽取长期记忆。"""
        try:
            if self.session_memory is None:
                return
            if status == "finished":
                self.session_memory.mark_finished()
            elif status == "stopped":
                self.session_memory.mark_stopped()
            elif status == "error":
                self.session_memory.mark_error(error_message)
            else:
                return
            # 归档前立即落盘一次，确保最后时刻不丢失
            try:
                self.session_memory.save_checkpoint()
            except Exception as _exc:
                logger.debug("归档前最终 checkpoint 失败: %s", _exc, exc_info=True)
            archived_path = self.session_memory.archive()
            if archived_path:
                logger.debug("会话已归档: %s", archived_path)
            # 归档完成后停止后台 checkpoint 线程（archive 内部也会停止，保险起见再调一次）
            self._stop_session_memory_checkpoint()
            # 抽取长期记忆：把会话摘要 + 错误信息写入 MEMORY.md
            if self.long_term_memory is not None:
                summary = self.session_memory.summarize()
                if summary:
                    self.long_term_memory.remember_from_message(summary)
                if error_message:
                    self.long_term_memory.remember_from_message(error_message)
        except Exception as _exc:
            logger.debug("归档会话记忆失败: %s", _exc, exc_info=True)

    def _maybe_extract_long_term(self, text: str) -> None:
        """对文本做长期记忆语义抽取。"""
        try:
            if self.long_term_memory is not None:
                self.long_term_memory.remember_from_message(text)
        except Exception as _exc:
            logger.debug("长期记忆抽取失败: %s", _exc, exc_info=True)

    def _emit_finished(self, archive: bool = True):
        """发出完成事件。archive=False 用于用户已手动停止的场景，避免重复归档。"""
        if archive:
            self._archive_session_memory("finished")
        cached = self._state_int("total_cached")
        total_tokens = self._state_int("total_tokens")
        cost = self._state_cost("total_cost_cny")
        hit_rate = (cached / total_tokens * 100) if total_tokens > 0 else 0.0
        if self.on_finished:
            try:
                self.on_finished(cost, hit_rate, cached, total_tokens)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def _refresh_status_bar(self, usage: Dict):
        """状态栏刷新 + 异步余额查询"""
        if not usage:
            return
        cached = usage.get("prompt_cache_hit_tokens", 0) or 0
        total_p = cached + (usage.get("prompt_cache_miss_tokens", 0) or 0)
        hit_rate = (cached / total_p * 100) if total_p > 0 else 0
        cost = usage.get("cost_cny", 0)
        if self.on_status_update:
            self.on_status_update(
                f"模型: {self.cfg.model} | 缓存命中率: {hit_rate:.0f}% | 本次成本: \u00a5{cost:.4f} | 查询余额中..."
            )

        generation, callback = self._project_generation, self.on_status_update
        def _async_check_balance():
            try:
                info = self.api.check_balance()
                avail = info.get("available_balance", 0)
                currency = info.get("currency", "CNY")
                if callback and not self._closed and generation == self._project_generation:
                    callback(
                        f"模型: {self.cfg.model} | 缓存命中率: {hit_rate:.0f}% | 本次成本: \u00a5{cost:.4f} | 余额: {currency} {avail:.2f}"
                    )
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            finally:
                with self._balance_lock:
                    self._balance_running = False

        # 防抖：避免高频调用创建过多线程（至少间隔 5 秒）
        # 每个引擎分别记录刷新状态，避免跨实例互相阻塞。
        with self._balance_lock:
            now = time.monotonic()
            if now - self._balance_last_call < 5.0:
                return
            # 检查是否已有余额查询线程在运行中
            if self._balance_running:
                return
            self._balance_running = True
            self._balance_last_call = now
        try:
            threading.Thread(target=_async_check_balance, daemon=True).start()
        except Exception:
            # 线程启动失败时重置标志，避免余额查询功能永久失效
            with self._balance_lock:
                self._balance_running = False

    # ========== 控制方法 ==========
    def check_health(self) -> Dict:
        """健康检查: API key / 余额 / 服务器"""
        info = {"api_key_ok": bool(self.cfg.api_key and self.cfg.api_key != "your_api_key_here")}
        try:
            bal = self.api.check_balance()
            info["balance"] = bal.get("available_balance", 0)
            info["balance_ok"] = bool(bal.get("ok", False))
        except Exception:
            info["balance_ok"] = False
        try:
            info["server_ok"] = self.api.check_server_status().get("ok", False)
        except Exception:
            info["server_ok"] = False
        return info

