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
        self._closed = False
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
    def extract_tool_calls(self, text):
        from src.tools.text_tool_parser import extract_calls
        return extract_calls(text, self.TOOL_NAMES)

    def shutdown(self):
        """Release resources owned by this executor; safe to call more than once."""
        self._closed = True
        terminal = getattr(self, "_persistent_terminal", None)
        if terminal is not None:
            terminal.terminate()
            self._persistent_terminal = None

    # 会修改项目状态的工具（子代理应禁止）
    _WRITE_TOOLS = frozenset({
        # 文件写入/编辑
        "create_file", "modify_file", "delete_file", "mark_delete",
        "edit_file", "insert_lines", "delete_lines",
        "replace_in_files", "replace_symbol_in_files", "format_code",
        "backup_workspace", "update_ds_section", "add_task", "complete_task",
        # Git 状态变更
        "git_reset", "git_checkout", "git_stash", "git_branch_delete", "git_commit", "git_add", "git_branch_create",
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
        if self._closed:
            return False, "工具执行器已关闭"
        # 防御: call 必须是非空 dict
        if not isinstance(call, dict):
            return False, f"[execute] 参数格式错误: 期望 dict，实际 {type(call).__name__}"
        tool = call.get("tool", "")
        if not isinstance(tool, str) or not tool:
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
            if Config().permission_mode == "reviewer" and tool in (self._REVIEWER_BLOCKED_TOOLS | self._WRITE_TOOLS):
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
                    tool_input={k: v for k, v in call.items() if k != "tool"},
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
                    tool_input={k: v for k, v in call.items() if k != "tool"},
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

    _TOOL_ROUTES = {
        'add_task': ('file_tools', 'add_task', True, ()),
        'agent_progress': ('agent_tools', 'agent_progress', False, ()),
        'all_symbols': ('code_analysis_tools', 'all_symbols', False, ()),
        'analyze_file': ('code_analysis_tools', 'analyze_file', True, ()),
        'analyze_symbol': ('code_analysis_tools', 'analyze_symbol', True, ()),
        'ask_user_questions': ('command_tools', 'ask_user_questions', True, ()),
        'backup_workspace': ('file_tools', 'backup_workspace', True, ()),
        'calculator': ('command_tools', 'calculator', True, ()),
        'check_types': ('quality_tools', 'check_types', True, ()),
        'code_metrics': ('code_analysis_tools', 'code_metrics', False, ()),
        'codebase_search': ('code_analysis_tools', 'codebase_search', True, ()),
        'complete_task': ('file_tools', 'complete_task', True, ()),
        'complexity_report': ('code_analysis_tools', 'complexity_report', False, ()),
        'create_file': ('file_tools', 'create_file', True, ()),
        'create_plan': ('agent_tools', 'create_plan', True, ()),
        'create_snapshot': ('file_tools', 'create_snapshot', True, ()),
        'delegate_subagent': ('agent_tools', 'delegate_subagent', True, ()),
        'delete_file': ('file_tools', 'mark_delete', True, ()),
        'delete_lines': ('file_tools', 'delete_lines', True, ()),
        'docs_lookup': ('network_tools', 'docs_lookup', True, ()),
        'document_symbols': ('lsp_tools', 'document_symbols', True, ()),
        'edit_file': ('file_tools', 'edit_file', True, ()),
        'execute_custom': ('command_tools', 'execute_custom', True, ()),
        'file_intel': ('code_analysis_tools', 'file_intel', True, ()),
        'find_files': ('file_tools', 'find_files', True, ()),
        'find_fixme': ('file_tools', 'find_markers', True, ('FIXME',)),
        'find_imports': ('code_analysis_tools', 'find_imports', True, ()),
        'find_references': ('lsp_tools', 'find_references', True, ()),
        'find_related': ('code_analysis_tools', 'find_related', True, ()),
        'find_symbol': ('file_tools', 'find_symbol', True, ()),
        'find_todo': ('file_tools', 'find_markers', True, ('TODO',)),
        'format_code': ('quality_tools', 'format_code', True, ()),
        'format_document': ('lsp_tools', 'format_document', True, ()),
        'get_diagnostics': ('lsp_tools', 'get_diagnostics', True, ()),
        'git_add': ('git_tools', 'git_add', True, ()),
        'git_branch': ('git_tools', 'git_branch', True, ()),
        'git_branch_create': ('git_tools', 'git_branch_create', True, ()),
        'git_branch_delete': ('git_tools', 'git_branch_delete', True, ()),
        'git_checkout': ('git_tools', 'git_checkout', True, ()),
        'git_commit': ('git_tools', 'git_commit', True, ()),
        'git_diff': ('git_tools', 'git_diff', True, ()),
        'git_log': ('git_tools', 'git_log', True, ()),
        'git_reset': ('git_tools', 'git_reset', True, ()),
        'git_stash': ('git_tools', 'git_stash', True, ()),
        'git_status': ('git_tools', 'git_status', True, ()),
        'goto_definition': ('lsp_tools', 'goto_definition', True, ()),
        'hover_info': ('lsp_tools', 'hover_info', True, ()),
        'impact_analysis': ('code_analysis_tools', 'impact_analysis', True, ()),
        'insert_lines': ('file_tools', 'insert_lines', True, ()),
        'lint_code': ('quality_tools', 'lint_code', True, ()),
        'list_files': ('file_tools', 'list_files', False, ()),
        'list_snapshots': ('file_tools', 'list_snapshots', False, ()),
        'list_uploaded_images': ('image_tools', 'list_images', True, ()),
        'mark_delete': ('file_tools', 'mark_delete', True, ()),
        'mark_step_done': ('agent_tools', 'mark_step_done', True, ()),
        'mcp_add': ('network_tools', 'mcp_add', True, ()),
        'mcp_enable': ('network_tools', 'mcp_enable', True, ()),
        'mcp_list': ('network_tools', 'mcp_list', True, ()),
        'mcp_remove': ('network_tools', 'mcp_remove', True, ()),
        'mcp_stop': ('network_tools', 'mcp_stop', True, ()),
        'modify_file': ('file_tools', 'create_file', True, ()),
        'parse_document': ('image_tools', 'parse_doc', True, ()),
        'parse_document_images': ('image_tools', 'parse_doc_images', True, ()),
        'parse_document_text': ('image_tools', 'parse_doc_text', True, ()),
        'project_brief': ('code_analysis_tools', 'project_brief', False, ()),
        'quick_syntax_check': ('quality_tools', 'quick_syntax_check', True, ()),
        'read_file': ('file_tools', 'read_file', True, ()),
        'rename_symbol': ('lsp_tools', 'rename_symbol', True, ()),
        'replace_in_files': ('file_tools', 'replace_in_files', True, ()),
        'replace_symbol_in_files': ('file_tools', 'rename_symbol', True, ()),
        'restore_snapshot': ('file_tools', 'restore_snapshot', True, ()),
        'revise_plan': ('agent_tools', 'revise_plan', True, ()),
        'risk_files': ('code_analysis_tools', 'risk_files', False, ()),
        'run_agents': ('agent_tools', 'run_agents', True, ()),
        'run_command': ('command_tools', 'run_command', True, ()),
        'run_command_stream': ('command_tools', 'run_command_stream', True, ()),
        'run_tests': ('quality_tools', 'run_tests', True, ()),
        'search_code': ('file_tools', 'search_code', True, ()),
        'shell_cd': ('command_tools', 'shell_cd', True, ()),
        'shell_exec': ('command_tools', 'shell_exec', True, ()),
        'shell_read': ('command_tools', 'shell_read', True, ()),
        'shell_send': ('command_tools', 'shell_send', True, ()),
        'shell_start': ('command_tools', 'shell_start', True, ()),
        'shell_stop': ('command_tools', 'shell_stop', True, ()),
        'show_call_chain': ('code_analysis_tools', 'show_call_chain', True, ()),
        'show_dependencies': ('code_analysis_tools', 'show_dependencies', False, ()),
        'show_plan': ('agent_tools', 'show_plan', False, ()),
        'sklii_call': ('sklii_tools', 'sklii_call', True, ()),
        'sklii_create': ('sklii_tools', 'sklii_create', True, ()),
        'sklii_list': ('sklii_tools', 'sklii_list', False, ()),
        'undo_last': ('file_tools', 'undo_last', True, ()),
        'update_ds_section': ('file_tools', 'update_ds_section', True, ()),
        'validate_all': ('quality_tools', 'validate_all', True, ()),
        'validate_file': ('quality_tools', 'validate_file', True, ()),
        'view_image': ('image_tools', 'view_image', True, ()),
        'web_fetch': ('network_tools', 'web_fetch', True, ()),
        'web_search': ('network_tools', 'web_search', True, ()),
    }

    def _dispatch(self, call, tool):
        """Dispatch registered tools after centralized permission and hook checks."""
        try:
            route = self._TOOL_ROUTES.get(tool)
            if route:
                owner, method, takes_call, extras = route
                args = ((call,) if takes_call else ()) + extras
                return getattr(getattr(self, owner), method)(*args)
            if tool == 'http_request':
                result = self.network_tools.http_request(call)
                if isinstance(result, dict):
                    ok = bool(result.get('ok', False))
                    status_code = result.get('status_code', 'n/a')
                    headers = result.get('headers') or {}
                    body = str(result.get('body', ''))
                    error = result.get('error')
                    lines = ['[http_request] ' + str(call.get('method', 'GET')) + ' ' + str(call.get('url', ''))]
                    lines.append('status: ' + str(status_code))
                    if headers:
                        hs = headers.items() if hasattr(headers, 'items') else []
                        lines.append('headers:')
                        for k, v in list(hs)[:10]:
                            lines.append('  ' + str(k) + ': ' + str(v)[:160])
                    lines.append('body:')
                    lines.append(body[:4000])
                    if error:
                        lines.append('error: ' + str(error))
                    return (ok, '\n'.join(lines))
                return result
            if tool in ('mcp_call', 'mcp_start'):
                if self._subagent_restrictions.get('forbid_commands', False):
                    logger.info('[AUDIT] denied tool=%s reason=forbid_commands', tool)
                    return (False, f'[{tool}] 已禁用 (子代理不可执行 MCP 命令)')
                if tool == 'mcp_call':
                    return self.network_tools.mcp_call(call)
                return self.network_tools.mcp_start(call)
            if tool == 'exit_conversation':
                return (True, '[exit_conversation] ' + str(call.get('reason', '')))
            if tool == 'start_subagent':
                return self._handle_subagent_tool('start_subagent', call)
            if tool == 'list_subagents':
                return self._handle_subagent_tool('list_subagents', call)
            if tool == 'get_subagent_results':
                return self._handle_subagent_tool('get_subagent_results', call)
            if tool == 'stop_subagent':
                return self._handle_subagent_tool('stop_subagent', call)
            if tool == 'record_lesson':
                return self.lesson_tools.record_lesson(call.get('title', ''), call.get('content', ''), call.get('tags', ''))
            if tool == 'list_lessons':
                return self.lesson_tools.list_lessons(call.get('tag', ''), call.get('search', ''))
            if tool == 'delete_lesson':
                return self.lesson_tools.delete_lesson(call.get('title', ''))
            if tool == 'set_conversation_title':
                return self._set_conversation_title(call)
            return False, "未知工具: " + str(tool)
        except Exception as exc:
            return False, "执行异常: " + str(exc)


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