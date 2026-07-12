# -*- coding: utf-8 -*-
"""HookCommandMixin — 自定义 Hook 命令处理（拆分自 chat_engine.py）。"""
from __future__ import annotations

from src.core.chat_engine._imports import *  # noqa: F401, F403




class HookCommandMixin(object):
    """自定义 Hook 命令处理方法。"""

    # ========== /hook 和 /lsp 命令 ==========
    def _handle_hook_command(self, args: list) -> bool:
        """/hook 命令: list/add/remove/test/reload"""
        sub = args[0].lower() if args else "list"
        out = ""

        if self.hook_manager is None:
            out = "[hook] Hooks系统未初始化"
        elif sub == "list" or sub == "ls":
            hooks = self.hook_manager.list_hooks()
            if not hooks:
                out = "[hook] 当前没有配置任何钩子\n\n用法:\n  /hook list              列出所有钩子\n  /hook test <event>      测试触发某钩子\n  /hook reload            重新加载钩子配置\n\n可用钩子事件: PreToolUse, PostToolUse, EditApplied, FileRead, CommandStart, CommandEnd"
            else:
                lines = [f"[hook] 共 {len(hooks)} 个钩子:\n"]
                for i, h in enumerate(hooks, 1):
                    event = h.get("event", "?")
                    action_type = h.get("action", {}).get("type", "?")
                    name = h.get("name", f"hook_{i}")
                    enabled = "✓" if h.get("enabled", True) else "✗"
                    target = ""
                    if action_type == "command":
                        target = h.get("action", {}).get("command", "")[:60]
                    elif action_type == "http":
                        target = h.get("action", {}).get("url", "")[:60]
                    elif action_type == "prompt":
                        target = "注入提示"
                    elif action_type == "python":
                        target = "Python脚本"
                    lines.append(f"  {enabled} {i}. [{event}] {name} ({action_type})")
                    if target:
                        lines.append(f"       → {target}")
                    filter_tool = h.get("filter", {}).get("tool_name", "")
                    filter_path = h.get("filter", {}).get("path_glob", "")
                    if filter_tool:
                        lines.append(f"       工具: {filter_tool}")
                    if filter_path:
                        lines.append(f"       路径: {filter_path}")
                out = "\n".join(lines)
        elif sub == "test":
            event = args[1] if len(args) > 1 else "PostToolUse"
            try:
                results = self.hook_manager.trigger(event, tool="manual", args={})
                out = f"[hook test] 触发 {event}，{len(results)}个钩子响应"
                for r in results[:5]:
                    out += f"\n  → {str(r)[:200]}"
            except Exception as e:
                out = f"[hook test] 失败: {e}"
        elif sub == "reload":
            try:
                self.hook_manager.load_all()
                out = "[hook reload] 钩子配置已重新加载"
            except Exception as e:
                out = f"[hook reload] 失败: {e}"
        elif sub == "add":
            # 交互式添加钩子 — 提示用户用自然语言描述需求，由AI在对话中生成配置
            out = (
                "[hook add] 请在对话中描述你想要的钩子，例如:\n"
                "  \"每次保存Python文件后自动运行black格式化\"\n"
                "  \"危险命令执行前二次确认\"\n"
                "  \"编辑完成后Slack通知到 #dev 频道\"\n"
                "  \"代码提交前自动lint\"\n\n"
                "AI将帮你生成钩子配置并保存。"
            )
        elif sub == "remove" or sub == "rm" or sub == "delete":
            if not args or len(args) < 2:
                out = "[hook remove] 请指定钩子编号或名称:\n" \
                      "  /hook remove <编号>   按编号删除（用/hook list查看）\n" \
                      "  /hook remove <名称>   按名称删除"
            else:
                target = args[1]
                try:
                    idx = int(target)
                    removed = self.hook_manager.remove_by_index(idx)
                    if removed:
                        out = f"[hook remove] 已删除 #{idx}: [{removed['event']}] {removed['name']}"
                    else:
                        out = f"[hook remove] 未找到编号 {idx}（使用 /hook list 查看）"
                except ValueError:
                    # 按名称删除
                    n = self.hook_manager.unregister(target)
                    if n > 0:
                        out = f"[hook remove] 已删除名为 '{target}' 的 {n} 个钩子"
                    else:
                        out = f"[hook remove] 未找到名为 '{target}' 的钩子"
        else:
            out = f"[hook] 未知子命令: {sub}\n可用: list, test, reload, add, remove"

        if self.on_content_delta:
            self.on_content_delta(out)
        if self.on_finished:
            self.on_finished(0.0, 0.0, 0, 0)
        return True

