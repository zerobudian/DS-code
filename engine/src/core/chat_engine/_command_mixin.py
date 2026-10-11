# -*- coding: utf-8 -*-
"""SlashCommandMixin — 斜杠命令解析与分发（拆分自 chat_engine.py）。"""
from __future__ import annotations

from src.core.chat_engine._imports import (
    Optional,
    Tuple,
    time,
)

import logging

logger = logging.getLogger(__name__)




class SlashCommandMixin(object):
    """斜杠命令处理相关方法。"""

    def _handle_skill_command(self, skill, args: list) -> bool:
        """处理技能斜杠命令。返回False让消息继续进入正常流程（技能指令注入到用户消息中）。"""
        # 解析参数
        args_str = args[0] if args else ""
        parsed_args = {}
        if skill.arguments:
            # 简单参数解析：位置参数按顺序分配
            pos_args = args_str.split() if args_str else []
            for i, arg_def in enumerate(skill.arguments):
                if i < len(pos_args):
                    parsed_args[arg_def.name] = pos_args[i]
                elif arg_def.default is not None:
                    parsed_args[arg_def.name] = arg_def.default
                elif arg_def.required:
                    if self.on_content_delta:
                        self.on_content_delta(
                            f"技能 {skill.command} 缺少必填参数: {arg_def.name}\n"
                            f"用法: {skill.command} <{arg_def.name}>"
                        )
                    if self.on_finished:
                        self.on_finished(0.0, 0.0, 0, 0)
                    return True  # 消费消息，不继续执行

        # 构建技能执行提示
        skill_prompt = skill.build_execution_prompt(parsed_args)
        # 将技能提示保存到实例变量，在构建用户消息时注入
        self._pending_skill_prompt = skill_prompt
        if self.on_status_update:
            self.on_status_update(f"执行技能: {skill.name}")
        return False  # 不消费消息，让正常流程继续（会在构建消息时注入技能提示）

    def _detect_slash_command(self, text: str) -> Optional[Tuple[str, list]]:
        """识别 "/undo 3" 或 "/deploy production" 等前缀命令。
        返回 (command_name, [args...]) 或 None（非命令）。
        支持内置命令和Skills技能命令。"""
        if not text:
            return None
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        parts = stripped.split()
        cmd = parts[0].lower()
        # 内置命令
        if cmd in self._COMMANDS:
            return cmd, parts[1:]
        # Skills技能命令
        if self.skill_manager is not None:
            parsed = self.skill_manager.parse_slash_command(stripped)
            if parsed is not None:
                scmd, sargs, skill = parsed
                return f"/{scmd}", [sargs] if sargs else []
        return None

    def _handle_slash_command(self, cmd: str, args: list) -> bool:
        """在 send_message 主循环前处理斜杠命令。返回 True 表示此请求已被消费（不进入规划/执行）。"""

        # Skills技能命令处理（前置检查，不在命令分发字典中）
        if self.skill_manager is not None:
            cmd_name = cmd.lstrip("/")
            # 先检查是否是skill类命令
            skill = self.skill_manager.get(cmd_name)
            if skill is not None:
                return self._handle_skill_command(skill, args)
            # 检查是否是slash_handler类命令（如/skill list, /help）
            args_str = args[0] if args else ""
            handler_result = self.skill_manager.execute_slash_handler(cmd_name, args_str)
            if handler_result is not None:
                if self.on_content_delta:
                    self.on_content_delta(handler_result)
                if self.on_finished:
                    self.on_finished(0.0, 0.0, 0, 0)
                return True

        # 命令分发字典：将斜杠命令映射到对应的 _cmd_* 处理方法
        dispatch = {
            "/clear": self._cmd_clear,
            "/reset": self._cmd_clear,
            "/undo": self._cmd_undo,
            "/snapshot": self._cmd_snapshot,
            "/restore": self._cmd_restore,
            "/snapshots": self._cmd_snapshots,
            "/plan": self._cmd_plan,
            "/ultra": self._cmd_ultra,
            "/normal": self._cmd_normal,
            "/help": self._cmd_help,
            "/hook": self._cmd_hook,
            "/lsp": self._cmd_lsp,
        }
        handler = dispatch.get(cmd)
        if handler is None:
            return False
        return handler(args)

    # ── 各斜杠命令的独立处理方法 ──────────────────────────────

    def _cmd_clear(self, args: list) -> bool:
        """处理 /clear 与 /reset：清空聊天（保留项目状态）。"""
        if self.on_chat_clear_requested:
            self.on_chat_clear_requested()
        if self.history is not None:
            self.history.clear()
        if self.on_finished:
            self.on_finished(0.0, 0.0, 0, 0)
        return True

    def _cmd_undo(self, args: list) -> bool:
        """处理 /undo [N]：撤销最近 N 次写入。"""
        n = 1
        if args:
            try:
                n = int(args[0])
            except (TypeError, ValueError):
                n = 1
        msg = "沙箱未初始化"
        if self.sandbox is not None:
            try:
                result = self.sandbox.undo(n)
                if isinstance(result, tuple) and len(result) == 2:
                    _, msg = result
                else:
                    msg = "undo 返回格式异常"
            except Exception as e:
                msg = str(e)
        if self.on_content_delta:
            self.on_content_delta(f"已撤销最近 {n} 步: {msg}")
        if self.on_finished:
            self.on_finished(0.0, 0.0, 0, 0)
        return True

    def _cmd_snapshot(self, args: list) -> bool:
        """处理 /snapshot [名称]：创建命名快照。"""
        name = " ".join(args) or ("manual_" + time.strftime("%Y%m%d_%H%M%S"))
        if self.sandbox is not None:
            try:
                snap = self.sandbox.snapshot(name)
                if isinstance(snap, dict):
                    if self.on_content_delta:
                        self.on_content_delta(
                            f"已创建快照: {snap.get('name')} "
                            f"(id={str(snap.get('id','?'))[:8]}..., 文件数={snap.get('file_count',0)})"
                        )
                else:
                    if self.on_content_delta:
                        self.on_content_delta("创建快照失败（返回格式异常）")
            except Exception as e:
                if self.on_content_delta:
                    self.on_content_delta(f"创建快照失败: {e}")
        if self.on_finished:
            self.on_finished(0.0, 0.0, 0, 0)
        return True

    def _cmd_restore(self, args: list) -> bool:
        """处理 /restore <id|name>：恢复到指定快照。"""
        sid = " ".join(args).strip()
        if not sid:
            if self.on_content_delta:
                self.on_content_delta("用法: /restore <snapshot_id_or_name>")
        elif self.sandbox is not None:
            msg = "restore 返回格式异常"
            try:
                result = self.sandbox.restore(sid)
                if isinstance(result, tuple) and len(result) == 2:
                    _, msg = result
                else:
                    msg = "restore 返回格式异常"
            except Exception as e:
                msg = str(e)
            if self.on_content_delta:
                self.on_content_delta(f"恢复快照: {msg}")
        if self.on_finished:
            self.on_finished(0.0, 0.0, 0, 0)
        return True

    def _cmd_snapshots(self, args: list) -> bool:
        """处理 /snapshots：列出所有可用快照。"""
        if self.sandbox is not None:
            snaps = self.sandbox.list_snapshots()
            if not snaps:
                if self.on_content_delta:
                    self.on_content_delta("暂无快照")
            else:
                lines = [f"可用快照（共 {len(snaps)} 个，最新在前）:"]
                for i, s in enumerate(snaps, 1):
                    if not isinstance(s, dict):
                        continue
                    sid = str(s.get('id', '?'))
                    # list_snapshots 返回 {id, title, ts}，兼容旧格式 {name, file_count, description}
                    title = s.get('title') or s.get('name') or '<noname>'
                    lines.append(f"  {i}. {title}  "
                                 f"id={sid[:8]}...  "
                                 f"ts={s.get('ts', '')}")
                if self.on_content_delta:
                    self.on_content_delta("\n".join(lines))
        if self.on_finished:
            self.on_finished(0.0, 0.0, 0, 0)
        return True

    def _cmd_plan(self, args: list) -> bool:
        """处理 /plan：强制进入 planning 模式并产生计划。"""
        # 强制进入 planning 模式并产生计划
        self.set_mode("plan")
        self.cfg.set("chat_mode", "plan")
        return False  # 让 send_message 走正常流程（会在 planning 模式下产计划）

    def _cmd_ultra(self, args: list) -> bool:
        """处理 /ultra：切换到 Ultra 模式（规划验证交付闭环）。"""
        self.set_mode("ultra")
        self.cfg.set("chat_mode", "ultra")
        return False  # 让消息走正常流程（会在 ultra 模式下走 _run_ultra_loop）

    def _cmd_normal(self, args: list) -> bool:
        self.set_mode("normal")
        self.cfg.set("chat_mode", "normal")
        if self.on_finished:
            self.on_finished(0.0, 0.0, 0, 0)
        return True

    def _cmd_help(self, args: list) -> bool:
        """处理 /help：显示可用命令列表。"""
        help_text = (
            "可用命令:\n"
            "  /clear             清空聊天（保留项目状态）\n"
            "  /undo [N]          撤销最近 N 次写入 (默认 N=1)\n"
            "  /snapshot [名称]   创建命名快照\n"
            "  /restore <id|name> 恢复到指定快照\n"
            "  /snapshots         列出所有快照\n"
            "  /plan              为当前项目生成计划（不执行）\n"
            "  /hook              管理钩子（/hook list/add/remove/test）\n"
            "  /lsp               查看LSP状态（/lsp status/start/stop）\n"
            "  /review            代码审查\n"
            "  /test              运行测试\n"
            "  /fix               自动修复错误\n"
            "  /release           发布流程\n"
            "  /explain           代码解释\n"
            "  /help              显示此帮助\n"
        )
        if self.on_content_delta:
            self.on_content_delta(help_text)
        if self.on_finished:
            self.on_finished(0.0, 0.0, 0, 0)
        return True

    def _cmd_hook(self, args: list) -> bool:
        """处理 /hook：管理钩子（委托给 _handle_hook_command）。"""
        return self._handle_hook_command(args)

    def _cmd_lsp(self, args: list) -> bool:
        """处理 /lsp：查看 LSP 状态（委托给 _handle_lsp_command）。"""
        return self._handle_lsp_command(args)

