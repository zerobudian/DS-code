# -*- coding: utf-8 -*-
"""工具执行器 (ToolExecutor) — 统一工具分发入口

将工具按功能拆分为以下子模块:
  - file_tools.py       — 文件操作工具
  - command_tools.py    — 命令执行工具
  - network_tools.py    — 网络工具
  - code_analysis_tools.py — 代码分析工具
  - quality_tools.py    — 测试和质量工具
  - agent_tools.py      — 代理和计划工具
  - git_tools.py        — Git 工具
  - sklii_tools.py      — sklii 工具
  - image_tools.py      — 图片文档工具

本模块保留:
  - TOOL_NAMES / EXTERNAL_TOOLS 集合
  - execute() 分发方法
  - extract_tool_calls() 解析方法
  - __init__ 初始化
"""

import logging

logger = logging.getLogger(__name__)

import json
import re

from src.core.config import Config
from src.tools.file_tools import FileTools
from src.tools.command_tools import CommandTools
from src.tools.network_tools import NetworkTools
from src.tools.code_analysis_tools import CodeAnalysisTools
from src.tools.quality_tools import QualityTools
from src.tools.agent_tools import AgentTools
from src.tools.git_tools import GitTools
from src.tools.sklii_tools import SklIITools
from src.tools.image_tools import ImageTools
from src.tools.lesson_tools import LessonTools
from src.tools.lsp_tools import LSPTools
# 已移除渐进式工具索引：工具描述全部直接注入系统提示，不再按需检索


class ToolExecutor:

    TOOL_NAMES = {
        # 文件基础
        "create_file", "read_file", "modify_file", "mark_delete", "delete_file",
        "list_files", "backup_workspace",
        # DS.md
        "update_ds_section", "add_task", "complete_task",
        # 网络
        "web_search", "web_fetch",
        "http_request", "docs_lookup",
        # 命令 / MCP / 退出
        "run_command", "run_command_stream", "calculator",
        "mcp_call", "mcp_list", "mcp_add", "mcp_remove",
        "mcp_enable", "mcp_start", "mcp_stop",
        "exit_conversation",
        # 持久终端会话
        "shell_start", "shell_exec", "shell_send",
        "shell_read", "shell_cd", "shell_stop",
        # 快照 / 撤销
        "create_snapshot", "restore_snapshot", "list_snapshots", "undo_last",
        # 子代理 / 自定义
        "delegate_subagent", "execute_custom",
        # 并行子代理
        "start_subagent", "list_subagents",
        "get_subagent_results", "stop_subagent",
        # 向用户提问
        "ask_user_questions",
        # 代码搜索
        "search_code", "find_symbol", "find_files",
        "find_todo", "find_fixme",
        # 代码编辑
        "edit_file", "insert_lines", "delete_lines",
        "replace_in_files", "replace_symbol_in_files",
        # LSP 工具（重命名、格式化等）
        "rename_symbol", "format_document",
        # 代码分析
        "analyze_file", "find_imports", "show_call_chain",
        "code_metrics", "show_dependencies",
        # 测试 / 质量 / 格式化
        "run_tests", "lint_code", "check_types", "format_code",
        # 代码分析层 (新增)
        "project_brief", "file_intel", "find_related",
        "analyze_symbol", "impact_analysis", "all_symbols",
        "complexity_report", "risk_files", "codebase_search",
        # 自动验证 (新增)
        # validate_all 会运行 pytest，可能执行任意测试代码，因此不放入 SAFE_TOOLS
        "validate_file", "quick_syntax_check", "validate_all",
        # 多代理 (新增)
        "create_plan", "revise_plan", "show_plan",
        "run_agents", "agent_progress", "mark_step_done",
        # 自定义工具包 (sklii)
        "sklii_call", "sklii_create", "sklii_list",
        # Git 工具集 (新增)
        "git_status", "git_diff", "git_commit", "git_add",
        "git_branch", "git_branch_create", "git_branch_delete",
        "git_log", "git_checkout", "git_stash", "git_reset",
        # 图片 / 文档解析工具 (新增)
        "view_image", "list_uploaded_images",
        "parse_document_text", "parse_document_images",
        "parse_document",
        # Lessons / Notes (Constitutional)
        "record_lesson", "list_lessons", "delete_lesson",
        # 对话元信息
        "set_conversation_title",
        # LSP真实Language Server工具
        "goto_definition", "find_references", "hover_info",
        "get_diagnostics", "document_symbols",
    }

    EXTERNAL_TOOLS = {"ask_user_questions"}
    VALID_SECTIONS = {"description", "pending", "completed", "custom"}

    def __init__(self, sandbox, mcp_manager=None, sklii_manager=None, image_doc_manager=None, hook_manager=None, lsp_manager=None):
        self.sandbox = sandbox
        self._mcp_manager = mcp_manager
        self._sklii_manager = sklii_manager
        self._image_doc_manager = image_doc_manager
        self._hook_manager = hook_manager  # Hooks钩子系统
        self._lsp_manager = lsp_manager    # LSP客户端管理器
        # 可选回调 (由外部粘合层注入)
        self.on_output_line = None       # run_command_stream 逐行回调
        self.on_cancel_check = None      # 返回 True 即停止
        self.on_edit_apply_request = None  # (filepath, unified_diff_str) -> bool
        self.on_confirm_command = None     # 命令确认回调
        self.on_conversation_title_changed = None  # (title) -> None
        self.on_edit_applied = None        # (filepath) -> None 编辑应用后回调（触发EditApplied钩子）
        self.language = "zh"              # 工具默认错误信息语言 ("zh"/"en"/"wenyan")

        # 子代理调用链记录（M-005）：用于限制连续调用次数并检测危险模式
        self._call_chain: list = []

        # C-005：子代理权限由内部字典统一管理，避免被外部/模型参数直接覆盖。
        # execute() 每次均从此字典读取，确保强制检查。
        self._subagent_restrictions = {
            "forbid_write": False,
            "forbid_commands": False,
            "is_subagent": False,
        }

        # 子模块实例化
        self.file_tools = FileTools(self)
        self.command_tools = CommandTools(self)
        self.network_tools = NetworkTools(self)
        self.code_analysis_tools = CodeAnalysisTools(self)
        self.quality_tools = QualityTools(self)
        self.agent_tools = AgentTools(self)
        self.git_tools = GitTools(self)
        self.sklii_tools = SklIITools(self)
        self.image_tools = ImageTools(self)
        self.lesson_tools = LessonTools(self)
        self.lsp_tools = LSPTools(self)
        # 如果构造时传入了lsp_manager，立即设置
        if lsp_manager is not None:
            self.lsp_tools.set_lsp_manager(lsp_manager)

    def reset_call_chain(self):
        """每轮对话开始时重置调用链，防止长会话累计达到上限后瘫痪所有工具调用。"""
        self._call_chain.clear()

    # C-005：子代理权限管理 — 统一入口，禁止通过普通属性赋值绕过。
    def set_subagent_restrictions(self, forbid_write=None, forbid_commands=None, is_subagent=None):
        """设置子代理权限限制。传入 None 表示保持当前值。"""
        if forbid_write is not None:
            self._subagent_restrictions["forbid_write"] = bool(forbid_write)
        if forbid_commands is not None:
            self._subagent_restrictions["forbid_commands"] = bool(forbid_commands)
        if is_subagent is not None:
            self._subagent_restrictions["is_subagent"] = bool(is_subagent)

    @property
    def _forbid_write(self):
        return self._subagent_restrictions.get("forbid_write", False)

    @_forbid_write.setter
    def _forbid_write(self, value):
        self._subagent_restrictions["forbid_write"] = bool(value)

    @property
    def _forbid_commands(self):
        return self._subagent_restrictions.get("forbid_commands", False)

    @_forbid_commands.setter
    def _forbid_commands(self, value):
        self._subagent_restrictions["forbid_commands"] = bool(value)

    @property
    def _is_subagent(self):
        return self._subagent_restrictions.get("is_subagent", False)

    @_is_subagent.setter
    def _is_subagent(self, value):
        self._subagent_restrictions["is_subagent"] = bool(value)

    # ========== 解析 & 执行 ==========
    # 单次提取最多处理多少字符，避免超长 reasoning 卡死循环
    _MAX_TOOL_SEARCH_LEN = 120_000
    _MAX_TOOL_CALLS = 50

    def extract_tool_calls(self, text):
        """从模型输出（reasoning）中提取工具调用 JSON 数组。

        对 None/空值/超长文本/无效 JSON 均有防御，避免下游收到异常数据。
        """
        calls = []
        if not isinstance(text, str):
            try:
                text = str(text or "")
            except Exception:
                return calls
        if not text:
            return calls
        # 超长文本：只扫描前段，避免推理输出数万字内容时陷入大循环
        if len(text) > self._MAX_TOOL_SEARCH_LEN:
            text = text[: self._MAX_TOOL_SEARCH_LEN]

        idx = 0
        safety_iter = 0
        max_iter = max(1000, text.count('"tool"') * 3 + 16)
        while safety_iter < max_iter:
            safety_iter += 1
            if len(calls) >= self._MAX_TOOL_CALLS:
                break
            pos = text.find('"tool"', idx)
            if pos == -1:
                break
            # C-2 修复: 使用字符串感知的方式向后查找开括号，避免定位到字符串内的 {
            open_brace = -1
            scan_i = pos - 1
            depth_back = 0
            in_str_back = False
            while scan_i >= 0:
                ch = text[scan_i]
                if ch == "\\" and in_str_back and scan_i > 0:
                    # 转义字符：跳过下一个字符
                    scan_i -= 2
                    continue
                if ch == '"':
                    in_str_back = not in_str_back
                elif not in_str_back:
                    if ch == "}":
                        depth_back += 1
                    elif ch == "{":
                        if depth_back == 0:
                            open_brace = scan_i
                            break
                        depth_back -= 1
                scan_i -= 1
            if open_brace == -1:
                idx = pos + 6
                continue
            depth = 0
            close_brace = -1
            in_string = False
            i = open_brace
            # C-1 修复: 扩大扫描窗口到 64KB，支持大型工具调用（如 create_file 携带大 content）
            search_end = min(len(text), open_brace + 65536)
            while i < search_end:
                ch = text[i]
                if ch == "\\" and in_string and i + 1 < search_end:
                    i += 2
                    continue
                # JSON 标准仅使用双引号作为字符串界定符；单引号只是普通字符。
                if ch == '"':
                    in_string = not in_string
                elif not in_string:
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            close_brace = i + 1
                            break
                i += 1
            if close_brace == -1:
                # 工具调用 JSON 未闭合（可能超过 64KB 扫描窗口）
                logger.warning(
                    "工具调用 JSON 未闭合，open_brace=%d, scan_window=%d, text_len=%d — 工具调用被跳过",
                    open_brace, 65536, len(text)
                )
                idx = pos + 6
                continue
            try:
                raw = text[open_brace:close_brace]
            except Exception:
                idx = pos + 6
                continue
            data = self._try_parse_json(raw)
            if data and isinstance(data, dict) and data.get("tool") in self.TOOL_NAMES:
                calls.append(data)
                idx = close_brace
                continue
            try:
                fixed = self._fix_json_newlines(raw)
            except Exception:
                fixed = raw
            data = self._try_parse_json(fixed)
            if data and isinstance(data, dict) and data.get("tool") in self.TOOL_NAMES:
                calls.append(data)
                idx = close_brace
                continue
            # C-3 修复: 移除危险的 replace("'", '"') fallback。
            # 单引号替换会破坏含英文撇号的字符串值（如 "it's a bug" → "it"s a bug"），
            # 导致无效 JSON 或参数错误。直接走 manual_parse fallback。
            data = self._manual_parse(raw)
            if data and isinstance(data, dict) and data.get("tool") in self.TOOL_NAMES:
                calls.append(data)
            idx = close_brace
        return calls

    @staticmethod
    def _try_parse_json(s):
        try:
            return json.loads(s)
        except Exception:
            return None

    @staticmethod
    def _fix_json_newlines(s):
        result = []
        in_string = False
        i = 0
        while i < len(s):
            ch = s[i]
            if ch == "\\" and in_string and i + 1 < len(s):
                result.append(ch)
                result.append(s[i + 1])
                i += 2
                continue
            if ch == '"':
                if not in_string:
                    in_string = True
                    result.append(ch)
                else:
                    in_string = False
                    result.append(ch)
            elif in_string and ch == "\n":
                result.append("\\n")
            elif in_string and ch == "\r":
                result.append("\\r")
            elif in_string and ch == "\t":
                result.append("\\t")
            else:
                result.append(ch)
            i += 1
        return "".join(result)

    @staticmethod
    def _manual_parse(s):
        result = {}
        m = re.search(r'"tool"\s*:\s*["\']([\w_]+)["\']', s)
        if m:
            result["tool"] = m.group(1)
        m = re.search(r'"filename"\s*:\s*["\']([^"\']+)["\']', s)
        if m:
            result["filename"] = m.group(1)
        m = re.search(r'"section"\s*:\s*["\']([\w_]+)["\']', s)
        if m:
            result["section"] = m.group(1)
        m = re.search(r'"query"\s*:\s*["\']([^"\']+)["\']', s)
        if m:
            result["query"] = m.group(1)
        m = re.search(r'"url"\s*:\s*["\']([^"\']+)["\']', s)
        if m:
            result["url"] = m.group(1)
        m = re.search(r'"command"\s*:\s*["\']([^"\']+)["\']', s)
        if m:
            result["command"] = m.group(1)
        m = re.search(r'"task"\s*:\s*["\']([^"\']+)["\']', s)
        if m:
            result["task"] = m.group(1)
        m = re.search(r'"reason"\s*:\s*["\']([^"\']+)["\']', s)
        if m:
            result["reason"] = m.group(1)
        m = re.search(r'"content"\s*:\s*["\']', s)
        if m:
            start = m.end()
            quote_char = s[m.end() - 1]  # 记录起始引号类型
            # 从起始位置查找匹配的闭合引号，限制搜索窗口防止跨对象匹配
            # 搜索窗口上限取剩余字符串长度和 128KB 的较小值
            search_end = min(len(s), start + 131072)
            # 手动查找未转义的闭合引号
            i = start
            while i < search_end:
                ch = s[i]
                if ch == "\\" and i + 1 < search_end:
                    i += 2
                    continue
                if ch == quote_char:
                    end = i
                    break
                i += 1
            else:
                end = -1
            if end > start:
                content = s[start:end]
                content = content.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
                result["content"] = content
        return result if "tool" in result else None

    # 会修改项目状态的工具（子代理应禁止）
    _WRITE_TOOLS = frozenset({
        # 文件写入/编辑
        "create_file", "modify_file", "delete_file", "mark_delete",
        "edit_file", "insert_lines", "delete_lines",
        "replace_in_files", "replace_symbol_in_files", "format_code",
        "backup_workspace", "update_ds_section", "add_task", "complete_task",
        # Git 状态变更
        "git_reset", "git_checkout", "git_stash", "git_branch_delete", "git_commit",
        # 快照 / 撤销
        "create_snapshot", "restore_snapshot", "undo_last",
        # 测试 / 质量检查（可能产生副作用：写入缓存、触发钩子等）
        "run_tests", "lint_code", "check_types", "validate_all", "validate_file",
        # MCP 管理
        "mcp_add", "mcp_remove", "mcp_enable", "mcp_start", "mcp_stop",
        # 子代理委派与多代理编排
        "delegate_subagent", "start_subagent", "run_agents",
        # Lessons / Notes
        "record_lesson", "delete_lesson",
        # 自定义工具包 (sklii)
        "sklii_call", "sklii_create",
        # 持久终端（可执行任意命令/写文件）
        "shell_start", "shell_exec", "shell_send", "shell_cd",
    })

    # 子代理调用链限制（M-005）
    _CALL_CHAIN_MAX = 50
    # 触发危险模式检测的前置工具：文件写入/修改类操作
    _CHAIN_DANGEROUS_PREV = frozenset({
        "create_file", "modify_file", "delete_file", "mark_delete",
        "edit_file", "insert_lines", "delete_lines",
        "replace_in_files", "replace_symbol_in_files",
        "backup_workspace", "update_ds_section",
    })
    # 危险后置工具：可执行外部命令/代码
    _CHAIN_DANGEROUS_NEXT = frozenset({
        "run_command", "run_command_stream", "execute_custom",
        "shell_exec", "shell_send",
    })

    # reviewer 模式下直接拦截的非只读工具（run_command/run_command_stream/execute_custom
    # 会进入 command_tools 做更细粒度的只读命令判断，因此不在此处拦截）
    _REVIEWER_BLOCKED_TOOLS = frozenset({
        "create_file", "modify_file", "delete_file", "mark_delete",
        "edit_file", "insert_lines", "delete_lines",
        "replace_in_files", "replace_symbol_in_files", "format_code",
        "backup_workspace", "update_ds_section", "add_task", "complete_task",
        "create_snapshot", "restore_snapshot", "undo_last",
        "run_tests", "lint_code", "check_types", "validate_all",
        "validate_file", "quick_syntax_check",
        "record_lesson", "delete_lesson", "set_conversation_title",
        "mcp_add", "mcp_remove", "mcp_enable", "mcp_start", "mcp_stop",
        "delegate_subagent", "start_subagent", "stop_subagent",
        "run_agents", "mark_step_done", "create_plan", "revise_plan",
        "sklii_call", "sklii_create",
        "git_commit", "git_checkout",
        # 持久终端在reviewer模式下也禁用
        "shell_start", "shell_exec", "shell_send", "shell_cd",
    })

    # ========== 统一分发 ==========
    def execute(self, call):
        # 防御: call 必须是非空 dict
        if not isinstance(call, dict):
            return False, f"[execute] 参数格式错误: 期望 dict，实际 {type(call).__name__}"
        tool = call.get("tool", "")
        if not tool:
            return False, "[execute] 缺少 tool 字段"

        # C-005：调用链审计日志（记录 tool、子代理状态、调用链长度）
        is_subagent = self._subagent_restrictions.get("is_subagent", False)
        logger.info(
            "[AUDIT] execute tool=%s subagent=%s forbid_write=%s forbid_commands=%s chain_len=%d",
            tool, is_subagent,
            self._subagent_restrictions.get("forbid_write", False),
            self._subagent_restrictions.get("forbid_commands", False),
            len(self._call_chain),
        )

        # 子代理调用链记录与限制（M-005）
        # 注意：仅在所有权限检查通过后才真正 append 到 _call_chain，
        # 避免被拒绝的调用污染链长、导致误判危险模式。
        if tool in self.TOOL_NAMES:
            projected_len = len(self._call_chain) + 1
            if projected_len > self._CALL_CHAIN_MAX:
                logger.info("[AUDIT] denied tool=%s reason=call_chain_overflow", tool)
                return False, f"[{tool}] 连续工具调用次数超过上限 {self._CALL_CHAIN_MAX}，请总结后再继续"
            if self._call_chain:
                prev_tool = self._call_chain[-1]
                if prev_tool in self._CHAIN_DANGEROUS_PREV and tool in self._CHAIN_DANGEROUS_NEXT:
                    logger.info("[AUDIT] denied tool=%s reason=dangerous_chain %s->%s", tool, prev_tool, tool)
                    return False, f"[{tool}] 检测到危险调用链 ({prev_tool} -> {tool})，已拒绝"

        # C-005：子代理写保护 — 每次 execute 强制读取内部字典，不依赖 getattr 默认值。
        if tool in self._WRITE_TOOLS and self._subagent_restrictions.get("forbid_write", False):
            logger.info("[AUDIT] denied tool=%s reason=forbid_write", tool)
            return False, f"[{tool}] 子代理不允许修改项目文件或任务状态"

        # C-005：子代理命令执行保护 — execute_custom 可运行 shell/python，需服从 forbid_commands。
        if tool == "execute_custom" and self._subagent_restrictions.get("forbid_commands", False):
            logger.info("[AUDIT] denied tool=%s reason=forbid_commands", tool)
            return False, "[execute_custom] 子代理不允许执行自定义命令"

        # C-005：子代理命令执行保护 — mcp_call/mcp_start 会启动/调用外部 MCP 服务器，需服从 forbid_commands。
        if tool in ("mcp_call", "mcp_start") and self._subagent_restrictions.get("forbid_commands", False):
            logger.info("[AUDIT] denied tool=%s reason=forbid_commands", tool)
            return False, f"[{tool}] 已禁用 (子代理不可执行 MCP 命令)"

        # Git 工具属于受控命令，允许在 _forbid_commands 场景下使用（由 git_tools.py 内部做参数校验）

        # 权限模式前置拦截（reviewer 模式禁止写/删/状态变更类工具）
        try:
            if Config().permission_mode == "reviewer" and tool in self._REVIEWER_BLOCKED_TOOLS:
                logger.info("[AUDIT] denied tool=%s reason=reviewer_mode", tool)
                return False, f"[{tool}] reviewer 模式禁止执行该工具"
        except Exception:
            pass

        # 所有权限检查通过后，才将本次调用记入调用链，再进入实际分发
        if tool in self.TOOL_NAMES:
            self._call_chain.append(tool)

        # PreToolUse 钩子 — 在工具执行前触发，可阻断执行
        hook_injection = ""
        if self._hook_manager is not None:
            try:
                blocked, reason, injection = self._hook_manager.trigger_blocking(
                    "PreToolUse",
                    tool_name=tool,
                    tool_input=call.get("params", {}),
                )
                if blocked:
                    logger.info("[AUDIT] hook blocked tool=%s reason=%s", tool, reason)
                    return False, f"[{tool}] 被钩子阻断: {reason}"
                hook_injection = injection
            except Exception as _e:
                logger.debug("PreToolUse钩子执行错误: %s", _e, exc_info=True)

        # 执行工具分发
        result_ok, result_text = self._dispatch(call, tool)

        # PostToolUse 钩子 — 在工具执行后触发，可注入额外上下文
        if self._hook_manager is not None:
            try:
                post_results = self._hook_manager.trigger(
                    "PostToolUse",
                    tool_name=tool,
                    tool_input=call.get("params", {}),
                    tool_result=result_text,
                    tool_ok=result_ok,
                )
                post_injections = [r.context_injection for r in post_results if r.context_injection]
                if post_injections:
                    append_text = "\n".join(post_injections)
                    if result_text:
                        result_text = result_text + "\n" + append_text
                    else:
                        result_text = append_text
            except Exception as _e:
                logger.debug("PostToolUse钩子执行错误: %s", _e, exc_info=True)

        # 合并PreToolUse注入（如果有）
        if hook_injection and result_ok:
            result_text = result_text + "\n" + hook_injection if result_text else hook_injection

        return result_ok, result_text

    def _dispatch(self, call, tool):
        """工具内部分发 — 返回 (ok, result)"""
        try:
            # --- 文件基础操作 ---
            if tool in ("create_file", "modify_file"):
                return self.file_tools.create_file(call)
            elif tool == "read_file":
                return self.file_tools.read_file(call)
            elif tool in ("delete_file", "mark_delete"):
                return self.file_tools.mark_delete(call)
            elif tool == "list_files":
                return self.file_tools.list_files()
            elif tool == "backup_workspace":
                return self.file_tools.backup_workspace(call)
            elif tool == "update_ds_section":
                return self.file_tools.update_ds_section(call)
            elif tool == "add_task":
                return self.file_tools.add_task(call)
            elif tool == "complete_task":
                return self.file_tools.complete_task(call)

            # --- 网络工具 ---
            elif tool == "web_search":
                return self.network_tools.web_search(call)
            elif tool == "web_fetch":
                return self.network_tools.web_fetch(call)
            elif tool == "http_request":
                result = self.network_tools.http_request(call)
                if isinstance(result, dict):
                    ok = bool(result.get("ok", False))
                    status_code = result.get("status_code", "n/a")
                    headers = result.get("headers") or {}
                    body = str(result.get("body", ""))
                    error = result.get("error")
                    lines = ["[http_request] " + str(call.get("method", "GET")) + " " + str(call.get("url", ""))]
                    lines.append("status: " + str(status_code))
                    if headers:
                        hs = headers.items() if hasattr(headers, "items") else []
                        lines.append("headers:")
                        for k, v in list(hs)[:10]:
                            lines.append("  " + str(k) + ": " + str(v)[:160])
                    lines.append("body:")
                    lines.append(body[:4000])
                    if error:
                        lines.append("error: " + str(error))
                    return ok, "\n".join(lines)
                return result
            elif tool == "docs_lookup":
                return self.network_tools.docs_lookup(call)

            # --- 命令执行 ---
            elif tool == "run_command":
                return self.command_tools.run_command(call)
            elif tool == "run_command_stream":
                return self.command_tools.run_command_stream(call)
            elif tool == "calculator":
                return self.command_tools.calculator(call)
            elif tool == "execute_custom":
                return self.command_tools.execute_custom(call)

            # --- 持久终端会话 ---
            elif tool == "shell_start":
                return self.command_tools.shell_start(call)
            elif tool == "shell_exec":
                return self.command_tools.shell_exec(call)
            elif tool == "shell_send":
                return self.command_tools.shell_send(call)
            elif tool == "shell_read":
                return self.command_tools.shell_read(call)
            elif tool == "shell_cd":
                return self.command_tools.shell_cd(call)
            elif tool == "shell_stop":
                return self.command_tools.shell_stop(call)

            # --- Git 工具集 ---
            elif tool == "git_status":
                return self.git_tools.git_status(call)
            elif tool == "git_diff":
                return self.git_tools.git_diff(call)
            elif tool == "git_commit":
                return self.git_tools.git_commit(call)
            elif tool == "git_add":
                return self.git_tools.git_add(call)
            elif tool == "git_branch":
                return self.git_tools.git_branch(call)
            elif tool == "git_branch_create":
                return self.git_tools.git_branch_create(call)
            elif tool == "git_branch_delete":
                return self.git_tools.git_branch_delete(call)
            elif tool == "git_log":
                return self.git_tools.git_log(call)
            elif tool == "git_checkout":
                return self.git_tools.git_checkout(call)
            elif tool == "git_stash":
                return self.git_tools.git_stash(call)
            elif tool == "git_reset":
                return self.git_tools.git_reset(call)

            # --- MCP / 用户提问 ---
            elif tool in ("mcp_call", "mcp_start"):
                # C-005：子代理/受限上下文禁止 MCP 命令类操作
                if self._subagent_restrictions.get("forbid_commands", False):
                    logger.info("[AUDIT] denied tool=%s reason=forbid_commands", tool)
                    return False, f"[{tool}] 已禁用 (子代理不可执行 MCP 命令)"
                if tool == "mcp_call":
                    return self.network_tools.mcp_call(call)
                return self.network_tools.mcp_start(call)
            elif tool == "mcp_list":
                return self.network_tools.mcp_list(call)
            elif tool == "mcp_add":
                return self.network_tools.mcp_add(call)
            elif tool == "mcp_remove":
                return self.network_tools.mcp_remove(call)
            elif tool == "mcp_enable":
                return self.network_tools.mcp_enable(call)
            elif tool == "mcp_stop":
                return self.network_tools.mcp_stop(call)
            elif tool == "ask_user_questions":
                return self.command_tools.ask_user_questions(call)
            elif tool == "exit_conversation":
                return True, "[exit_conversation] " + str(call.get("reason", ""))

            # --- 快照 / 撤销 ---
            elif tool == "create_snapshot":
                return self.file_tools.create_snapshot(call)
            elif tool == "restore_snapshot":
                return self.file_tools.restore_snapshot(call)
            elif tool == "list_snapshots":
                return self.file_tools.list_snapshots()
            elif tool == "undo_last":
                return self.file_tools.undo_last(call)

            # --- 子代理 ---
            elif tool == "delegate_subagent":
                return self.agent_tools.delegate_subagent(call)

            # --- 子代理工具（parallel sub-agents）---
            elif tool == "start_subagent":
                return self._handle_subagent_tool("start_subagent", call)
            elif tool == "list_subagents":
                return self._handle_subagent_tool("list_subagents", call)
            elif tool == "get_subagent_results":
                return self._handle_subagent_tool("get_subagent_results", call)
            elif tool == "stop_subagent":
                return self._handle_subagent_tool("stop_subagent", call)

            # --- 代码搜索 ---
            elif tool == "search_code":
                return self.file_tools.search_code(call)
            elif tool == "find_symbol":
                return self.file_tools.find_symbol(call)
            elif tool == "find_files":
                return self.file_tools.find_files(call)
            elif tool == "find_todo":
                return self.file_tools.find_markers(call, "TODO")
            elif tool == "find_fixme":
                return self.file_tools.find_markers(call, "FIXME")

            # --- 代码编辑 ---
            elif tool == "edit_file":
                return self.file_tools.edit_file(call)
            elif tool == "insert_lines":
                return self.file_tools.insert_lines(call)
            elif tool == "delete_lines":
                return self.file_tools.delete_lines(call)
            elif tool == "replace_in_files":
                return self.file_tools.replace_in_files(call)
            elif tool == "replace_symbol_in_files":
                return self.file_tools.rename_symbol(call)

            # --- 代码分析 ---
            elif tool == "analyze_file":
                return self.code_analysis_tools.analyze_file(call)
            elif tool == "find_imports":
                return self.code_analysis_tools.find_imports(call)
            elif tool == "show_call_chain":
                return self.code_analysis_tools.show_call_chain(call)
            elif tool == "code_metrics":
                return self.code_analysis_tools.code_metrics()
            elif tool == "show_dependencies":
                return self.code_analysis_tools.show_dependencies()

            # --- 测试 / 质量 / 格式化 ---
            elif tool == "run_tests":
                return self.quality_tools.run_tests(call)
            elif tool == "lint_code":
                return self.quality_tools.lint_code(call)
            elif tool == "check_types":
                return self.quality_tools.check_types(call)
            elif tool == "format_code":
                return self.quality_tools.format_code(call)

            # --- 代码分析层 (CodeIntel) ---
            elif tool == "project_brief":
                return self.code_analysis_tools.project_brief()
            elif tool == "file_intel":
                return self.code_analysis_tools.file_intel(call)
            elif tool == "find_related":
                return self.code_analysis_tools.find_related(call)
            elif tool == "analyze_symbol":
                return self.code_analysis_tools.analyze_symbol(call)
            elif tool == "impact_analysis":
                return self.code_analysis_tools.impact_analysis(call)
            elif tool == "all_symbols":
                return self.code_analysis_tools.all_symbols()
            elif tool == "complexity_report":
                return self.code_analysis_tools.complexity_report()
            elif tool == "risk_files":
                return self.code_analysis_tools.risk_files()
            elif tool == "codebase_search":
                return self.code_analysis_tools.codebase_search(call)

            # --- LSP真实Language Server工具 ---
            elif tool == "goto_definition":
                return self.lsp_tools.goto_definition(call)
            elif tool == "find_references":
                return self.lsp_tools.find_references(call)
            elif tool == "hover_info":
                return self.lsp_tools.hover_info(call)
            elif tool == "get_diagnostics":
                return self.lsp_tools.get_diagnostics(call)
            elif tool == "document_symbols":
                return self.lsp_tools.document_symbols(call)
            elif tool == "rename_symbol":
                return self.lsp_tools.rename_symbol(call)
            elif tool == "format_document":
                return self.lsp_tools.format_document(call)

            # --- 自动验证 ---
            elif tool == "validate_all":
                return self.quality_tools.validate_all(call)
            elif tool == "validate_file":
                return self.quality_tools.validate_file(call)
            elif tool == "quick_syntax_check":
                return self.quality_tools.quick_syntax_check(call)

            # --- 多代理 ---
            elif tool == "create_plan":
                return self.agent_tools.create_plan(call)
            elif tool == "revise_plan":
                return self.agent_tools.revise_plan(call)
            elif tool == "show_plan":
                return self.agent_tools.show_plan()
            elif tool == "run_agents":
                return self.agent_tools.run_agents(call)
            elif tool == "agent_progress":
                return self.agent_tools.agent_progress()
            elif tool == "mark_step_done":
                return self.agent_tools.mark_step_done(call)

            # --- 图片 / 文档解析工具 ---
            elif tool == "view_image":
                return self.image_tools.view_image(call)
            elif tool == "list_uploaded_images":
                return self.image_tools.list_images(call)
            elif tool == "parse_document_text":
                return self.image_tools.parse_doc_text(call)
            elif tool == "parse_document_images":
                return self.image_tools.parse_doc_images(call)
            elif tool == "parse_document":
                return self.image_tools.parse_doc(call)

            # --- sklii 自定义工具包 ---
            elif tool == "sklii_call":
                return self.sklii_tools.sklii_call(call)
            elif tool == "sklii_create":
                return self.sklii_tools.sklii_create(call)
            elif tool == "sklii_list":
                return self.sklii_tools.sklii_list()

            # --- Lessons / Notes (Constitutional) ---
            elif tool == "record_lesson":
                return self.lesson_tools.record_lesson(
                    call.get("title", ""),
                    call.get("content", ""),
                    call.get("tags", ""),
                )
            elif tool == "list_lessons":
                return self.lesson_tools.list_lessons(
                    call.get("tag", ""),
                    call.get("search", ""),
                )
            elif tool == "delete_lesson":
                return self.lesson_tools.delete_lesson(call.get("title", ""))

            elif tool == "set_conversation_title":
                return self._set_conversation_title(call)

            else:
                return False, "未知工具: " + str(tool)
        except Exception as e:
            return False, "执行异常: " + str(e)

    # ========== 子代理辅助方法 ==========
    def _handle_subagent_tool(self, tool_name: str, call: dict) -> tuple:
        """统一处理子代理工具调用 — 通过 _chat_engine 访问 SubAgentEngine"""
        chat_engine = getattr(self, "_chat_engine", None)
        if chat_engine is None:
            # 尝试从其他属性获取
            chat_engine = getattr(self, "_parent", None)
        if chat_engine is None:
            return False, f"[{tool_name}] 子代理引擎未初始化"

        # 分发到具体方法
        try:
            if tool_name == "start_subagent":
                return chat_engine.tool_start_subagent(call)
            elif tool_name == "list_subagents":
                return chat_engine.tool_list_subagents(call)
            elif tool_name == "get_subagent_results":
                return chat_engine.tool_get_subagent_results(call)
            elif tool_name == "stop_subagent":
                return chat_engine.tool_stop_subagent(call)
            else:
                return False, f"[{tool_name}] 未知子代理工具"
        except Exception as e:
            return False, f"[{tool_name}] 执行失败: {str(e)}"

    def _set_conversation_title(self, call: dict) -> tuple:
        title = call.get("title", "")
        if not title:
            return False, "[set_conversation_title] 缺少 title"
        if not isinstance(title, str) or not title.strip():
            return False, "[set_conversation_title] title 不能为空"
        title = title.strip()
        if len(title) > 128:
            title = title[:128]
        try:
            if self.sandbox is None:
                return False, "[set_conversation_title] 当前未加载项目"
            ok = self.sandbox.set_display_name(title, user_initiated=False)
            if not ok:
                return False, "[set_conversation_title] 设置失败（可能用户已手动重命名）"
            if self.on_conversation_title_changed:
                try:
                    self.on_conversation_title_changed(title)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            return True, f"[set_conversation_title] 已设置为: {title}"
        except Exception as e:
            return False, f"[set_conversation_title] 失败: {str(e)}"