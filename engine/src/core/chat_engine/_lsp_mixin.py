# -*- coding: utf-8 -*-
"""LSPCommandMixin — LSP 命令处理（拆分自 chat_engine.py）。"""
from __future__ import annotations

from src.core.chat_engine._imports import *  # noqa: F401, F403




class LSPCommandMixin(object):
    """LSP 命令处理方法。"""

    def _handle_lsp_command(self, args: list) -> bool:
        """/lsp 命令: status/start/stop/restart"""
        sub = args[0].lower() if args else "status"
        out = ""

        if self.lsp_manager is None:
            out = "[lsp] LSP管理器未初始化"
        elif sub == "status":
            servers = self.lsp_manager.list_running_servers()
            if servers:
                out = f"[lsp] 运行中的Language Server ({len(servers)}个):\n"
                for s in servers:
                    out += f"  • {s}\n"
            else:
                out = (
                    "[lsp] 当前没有运行的Language Server\n\n"
                    "支持的语言服务器:\n"
                    "  Python:   pyright, pylsp, jedi-language-server\n"
                    "  JS/TS:    typescript-language-server\n"
                    "  Go:       gopls\n"
                    "  Rust:     rust-analyzer\n\n"
                    "首次使用 goto_definition/find_references/get_diagnostics 时会自动启动对应语言的服务器（需已安装在PATH中）\n"
                    "手动启动: /lsp start"
                )
        elif sub == "start":
            try:
                n = self.lsp_manager.start()
                if n > 0:
                    servers = self.lsp_manager.list_running_servers()
                    out = f"[lsp start] 已启动 {n} 个Language Server:\n" + "\n".join(f"  • {s}" for s in servers)
                else:
                    out = (
                        "[lsp start] 未检测到可启动的Language Server\n"
                        "请先安装对应的语言服务器:\n"
                        "  pip install pyright (Python)\n"
                        "  npm install -g typescript typescript-language-server (JS/TS)\n"
                        "  安装后LSP功能在首次使用时会自动启动"
                    )
            except Exception as e:
                out = f"[lsp start] 失败: {e}"
        elif sub == "stop":
            try:
                self.lsp_manager.shutdown_all()
                out = "[lsp stop] 所有Language Server已关闭"
            except Exception as e:
                out = f"[lsp stop] 失败: {e}"
        elif sub == "restart":
            try:
                self.lsp_manager.shutdown_all()
                n = self.lsp_manager.start()
                out = f"[lsp restart] 已重启，启动了 {n} 个服务器"
            except Exception as e:
                out = f"[lsp restart] 失败: {e}"
        else:
            out = f"[lsp] 未知子命令: {sub}\n可用: status, start, stop, restart"

        if self.on_content_delta:
            self.on_content_delta(out)
        if self.on_finished:
            self.on_finished(0.0, 0.0, 0, 0)
        return True

