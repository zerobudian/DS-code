# -*- coding: utf-8 -*-
"""真正的LSP (Language Server Protocol) 客户端

通过 stdio 连接真实的 Language Server（pylsp/pyright/ts-server/rust-analyzer等），
提供精准的符号定义、引用查找、诊断（错误/警告）等功能。

自研 LSP Lite（基于AST静态索引）作为fallback。

支持的LSP功能:
  - textDocument/definition  — 跳转到定义
  - textDocument/references  — 查找引用
  - textDocument/hover       — 悬停信息（类型/文档）
  - textDocument/diagnostic  — 诊断错误/警告
  - textDocument/completion  — 补全（暂不启用）
  - textDocument/documentSymbol — 文档符号树

用法:
  lsp = LSPClient(work_dir)
  lsp.start_server("pylsp", ["pylsp"])  # or pyright, typescript-language-server
  lsp.initialize()
  lsp.did_open("file:///path/to/file.py")
  defs = lsp.goto_definition("file:///path/to/file.py", line, col)
  diags = lsp.get_diagnostics("file:///path/to/file.py")
  lsp.shutdown()
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# LSP 常量
LSP_TIMEOUT = 10  # 秒
LSP_INIT_TIMEOUT = 15  # 初始化超时


class LSPError(Exception):
    pass


class LSPClient:
    """Language Server Protocol 客户端 (stdio传输)"""

    def __init__(self, root_uri: str, workspace_folders: Optional[List[str]] = None):
        """
        root_uri: 项目根URI，如 "file:///home/user/project"
        """
        self.root_uri = root_uri.rstrip("/") if root_uri else ""
        self.root_path = root_uri.replace("file://", "", 1) if root_uri.startswith("file://") else root_uri
        self.workspace_folders = workspace_folders or [{"uri": self.root_uri, "name": Path(self.root_path).name}]
        self._proc: Optional[subprocess.Popen] = None
        self._msg_id = 0
        self._lock = threading.Lock()
        self._response_queue: Dict[int, queue.Queue] = {}
        self._diagnostics: Dict[str, List[Dict]] = {}  # uri -> diagnostics
        self._reader_thread: Optional[threading.Thread] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._write_queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._initialized = False
        self._initializing = False
        self._server_capabilities: Dict[str, Any] = {}
        self._shutdown_requested = False
        self._server_name = ""
        self._stderr_drain_thread: Optional[threading.Thread] = None

    @classmethod
    def from_path(cls, path: str) -> "LSPClient":
        """从文件路径创建LSPClient"""
        abs_path = str(Path(path).resolve())
        return cls(f"file://{abs_path}")

    @staticmethod
    def path_to_uri(path: str) -> str:
        return "file://" + str(Path(path).resolve())

    @staticmethod
    def uri_to_path(uri: str) -> str:
        if uri.startswith("file://"):
            return uri[7:]
        return uri

    # ── 生命周期 ──────────────────────────────────────────────
    def start_server(self, name: str, cmd: List[str], env: Optional[Dict[str, str]] = None,
                     cwd: Optional[str] = None) -> bool:
        """启动LSP服务器进程
        name: 服务器名称（如 "pylsp", "pyright", "typescript-language-server"）
        cmd: 启动命令（如 ["pylsp"] 或 ["pyright-langserver", "--stdio"]）
        """
        # 如果已有进程，先彻底关闭并等待线程退出
        if self._proc is not None:
            self.shutdown()
            # 等待旧线程完全退出
            for t in (self._reader_thread, self._writer_thread, self._stderr_drain_thread):
                try:
                    if t is not None and t.is_alive():
                        t.join(timeout=3)
                except Exception:
                    pass

        # 重置所有状态
        self._server_name = name
        self._server_capabilities = {}
        self._initialized = False
        self._shutdown_requested = False
        self._msg_id = 0
        self._response_queue.clear()
        self._diagnostics.clear()
        # 清空写队列
        while not self._write_queue.empty():
            try:
                self._write_queue.get_nowait()
            except Exception:
                break

        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,  # 需要drain线程消费，否则管道满后死锁
                env=merged_env,
                cwd=cwd or self.root_path,
                text=False,
            )
        except FileNotFoundError:
            logger.debug("LSP服务器未找到: %s", " ".join(cmd))
            self._proc = None
            return False
        except Exception as e:
            logger.debug("启动LSP服务器失败(%s): %s", name, e)
            self._proc = None
            return False

        # 启动stderr drain线程，防止stderr管道满导致服务器死锁
        self._stderr_drain_thread = threading.Thread(
            target=self._stderr_drain_loop, daemon=True, name=f"LSP-Stderr-{name}"
        )
        self._stderr_drain_thread.start()

        # 启动读写线程
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True, name=f"LSP-Reader-{name}")
        self._reader_thread.start()
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True, name=f"LSP-Writer-{name}")
        self._writer_thread.start()

        return True

    def initialize(self) -> bool:
        """发送initialize请求，等待服务器响应（带并发保护）"""
        if self._proc is None:
            return False
        # 双重检查锁定防止并发initialize
        with self._lock:
            if self._initialized:
                return True
            if self._initializing:
                return False
            self._initializing = True
        try:
            init_params = {
                "processId": os.getpid(),
                "rootUri": self.root_uri,
                "rootPath": self.root_path,
                "workspaceFolders": self.workspace_folders,
                "capabilities": {
                    "textDocument": {
                        "definition": {"dynamicRegistration": True},
                        "references": {"dynamicRegistration": True},
                        "hover": {"dynamicRegistration": True},
                        "diagnostic": {"dynamicRegistration": True},
                        "documentSymbol": {"dynamicRegistration": True},
                        "rename": {"dynamicRegistration": True},
                        "formatting": {"dynamicRegistration": True},
                        "codeAction": {"dynamicRegistration": True},
                        "completion": {"dynamicRegistration": True},
                    },
                    "workspace": {
                        "workspaceFolders": True,
                        "applyEdit": True,
                        "configuration": True,
                    },
                    "window": {
                        "showMessage": {"messageActionItem": {"additionalPropertiesSupport": True}},
                        "showDocument": {"support": True},
                    },
                },
            }
            resp = self._request("initialize", init_params, timeout=LSP_INIT_TIMEOUT)
            if resp is None:
                logger.debug("LSP initialize 超时或失败")
                return False
            with self._lock:
                self._server_capabilities = resp.get("capabilities", {})
            # 发送 initialized 通知
            self._notify("initialized", {})
            self._initialized = True
            return True
        except Exception as e:
            logger.debug("LSP initialize 失败: %s", e)
            return False
        finally:
            self._initializing = False

    def shutdown(self):
        """关闭LSP服务器"""
        self._initialized = False
        # 唤醒所有等待响应的线程
        with self._lock:
            for q in self._response_queue.values():
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            self._response_queue.clear()
        # 判断是否从 reader 线程调用（避免死锁：reader 线程无法同时处理响应）
        _in_reader = (self._reader_thread is not None and
                      threading.current_thread() is self._reader_thread)
        # 发送 shutdown 请求（仅在非 reader 线程时，否则 _request 会死锁等待自身处理的响应）
        if not _in_reader:
            try:
                if self._proc is not None and self._proc.poll() is None:
                    self._request("shutdown", {}, timeout=3)
            except Exception:
                pass
        # 设置关闭标志（必须在 shutdown 请求之后，否则 _request 入口检查会短路）
        self._shutdown_requested = True
        # 发送 exit 通知（_notify 不等待响应，任何线程调用都安全）
        try:
            if self._proc is not None and self._proc.poll() is None:
                self._notify("exit", {})
        except Exception:
            pass
        # 停止写线程（关闭stdin使其退出）
        try:
            if self._proc is not None and self._proc.stdin is not None:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._write_queue.put(None)
        except Exception:
            pass
        # 等待线程退出（给合理超时）
        for t in (self._writer_thread, self._reader_thread, self._stderr_drain_thread):
            try:
                if t is not None and t.is_alive():
                    t.join(timeout=2)
            except Exception:
                pass
        # 等待进程退出
        if self._proc is not None:
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=2)  # 回收僵尸
                except Exception:
                    pass
            # 关闭所有管道
            for fd_name in ("stdin", "stdout", "stderr"):
                try:
                    fd = getattr(self._proc, fd_name, None)
                    if fd is not None:
                        fd.close()
                except Exception:
                    pass
            self._proc = None
        with self._lock:
            self._diagnostics.clear()

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self._initialized

    # ── 文本同步 ──────────────────────────────────────────────
    def did_open(self, uri: str, language_id: str = "", version: int = 1, text: str = ""):
        """通知服务器文件已打开"""
        if not self._initialized:
            return
        if not language_id:
            lang_map = {
                ".py": "python", ".js": "javascript", ".ts": "typescript",
                ".jsx": "javascriptreact", ".tsx": "typescriptreact",
                ".go": "go", ".rs": "rust", ".java": "java",
                ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
                ".rb": "ruby", ".php": "php", ".cs": "csharp",
            }
            ext = Path(self.uri_to_path(uri)).suffix.lower()
            language_id = lang_map.get(ext, "plaintext")
        if not text:
            try:
                text = Path(self.uri_to_path(uri)).read_text(encoding="utf-8", errors="replace")
            except Exception:
                return
        self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": language_id,
                "version": version,
                "text": text,
            }
        })

    def did_change(self, uri: str, text: str, version: int = None):
        """通知服务器文件内容变更"""
        if not self._initialized:
            return
        params = {
            "textDocument": {"uri": uri, "version": version or int(time.time())},
            "contentChanges": [{"text": text}],
        }
        self._notify("textDocument/didChange", params)

    def did_close(self, uri: str):
        """通知服务器文件已关闭"""
        if not self._initialized:
            return
        self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        self._diagnostics.pop(uri, None)

    def did_save(self, uri: str, text: str = ""):
        """通知服务器文件已保存"""
        if not self._initialized:
            return
        params = {"textDocument": {"uri": uri}}
        if text:
            params["text"] = text
        self._notify("textDocument/didSave", params)

    # ── LSP功能 ───────────────────────────────────────────────
    def goto_definition(self, uri: str, line: int, character: int) -> List[Dict[str, Any]]:
        """跳转到定义
        line/character: 0-based
        返回: [{"uri": str, "range": {"start": {"line": int, "character": int}, "end": {...}}, "name": str?}]
        """
        if not self._initialized:
            return []
        if not self._server_capabilities.get("definitionProvider"):
            return []
        resp = self._request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }, timeout=LSP_TIMEOUT)
        return self._parse_locations(resp)

    def find_references(self, uri: str, line: int, character: int,
                         include_declaration: bool = True) -> List[Dict[str, Any]]:
        """查找引用"""
        if not self._initialized:
            return []
        if not self._server_capabilities.get("referencesProvider"):
            return []
        resp = self._request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": include_declaration},
        }, timeout=LSP_TIMEOUT)
        return self._parse_locations(resp)

    def hover(self, uri: str, line: int, character: int) -> Optional[Dict[str, Any]]:
        """获取悬停信息（类型/文档）
        返回完整的Hover响应: {"contents": ..., "range": ...} 或 None
        """
        if not self._initialized:
            return None
        if not self._server_capabilities.get("hoverProvider"):
            return None
        resp = self._request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }, timeout=LSP_TIMEOUT)
        return resp  # 返回完整响应供上层处理

    def get_diagnostics(self, uri: str) -> List[Dict[str, Any]]:
        """获取文件诊断（错误/警告）"""
        return list(self._diagnostics.get(uri, []))

    def get_all_diagnostics(self) -> Dict[str, List[Dict[str, Any]]]:
        """获取所有文件诊断"""
        return dict(self._diagnostics)

    def document_symbols(self, uri: str) -> List[Dict[str, Any]]:
        """获取文档符号树"""
        if not self._initialized:
            return []
        if not self._server_capabilities.get("documentSymbolProvider"):
            return []
        resp = self._request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        }, timeout=LSP_TIMEOUT)
        if not isinstance(resp, list):
            return []
        return resp

    def rename_symbol(self, uri: str, line: int, character: int, new_name: str) -> Optional[Dict[str, Any]]:
        """重命名符号
        返回 WorkspaceEdit: {"changes": {uri: [TextEdit, ...]}}
        """
        if not self._initialized:
            return None
        if not self._server_capabilities.get("renameProvider"):
            return None
        resp = self._request("textDocument/rename", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "newName": new_name,
        }, timeout=LSP_TIMEOUT)
        return resp

    def format_document(self, uri: str) -> List[Dict[str, Any]]:
        """格式化整个文档
        返回 TextEdit 列表: [{"range": {...}, "newText": "..."}]
        """
        if not self._initialized:
            return []
        if not self._server_capabilities.get("documentFormattingProvider"):
            return []
        resp = self._request("textDocument/formatting", {
            "textDocument": {"uri": uri},
            "options": {"tabSize": 4, "insertSpaces": True},
        }, timeout=LSP_TIMEOUT)
        if not isinstance(resp, list):
            return []
        return resp

    def format_range(self, uri: str, start_line: int, start_char: int,
                     end_line: int, end_char: int) -> List[Dict[str, Any]]:
        """格式化选中范围"""
        if not self._initialized:
            return []
        if not self._server_capabilities.get("documentRangeFormattingProvider"):
            return []
        resp = self._request("textDocument/rangeFormatting", {
            "textDocument": {"uri": uri},
            "range": {
                "start": {"line": start_line, "character": start_char},
                "end": {"line": end_line, "character": end_char},
            },
            "options": {"tabSize": 4, "insertSpaces": True},
        }, timeout=LSP_TIMEOUT)
        if not isinstance(resp, list):
            return []
        return resp

    def code_actions(self, uri: str, start_line: int, start_char: int,
                     end_line: int, end_char: int,
                     diagnostics: Optional[List[Dict]] = None) -> List[Dict[str, Any]]:
        """获取代码操作（快速修复、自动导入等）"""
        if not self._initialized:
            return []
        if not self._server_capabilities.get("codeActionProvider"):
            return []
        range_diags = diagnostics or []
        resp = self._request("textDocument/codeAction", {
            "textDocument": {"uri": uri},
            "range": {
                "start": {"line": start_line, "character": start_char},
                "end": {"line": end_line, "character": end_char},
            },
            "context": {"diagnostics": range_diags},
        }, timeout=LSP_TIMEOUT)
        if not isinstance(resp, list):
            return []
        return resp

    def execute_command(self, command: str, arguments: Optional[List] = None) -> Optional[Any]:
        """执行 workspace/executeCommand（用于应用 code action）"""
        if not self._initialized:
            return None
        if not self._server_capabilities.get("executeCommandProvider"):
            return None
        resp = self._request("workspace/executeCommand", {
            "command": command,
            "arguments": arguments or [],
        }, timeout=LSP_TIMEOUT)
        return resp

    def completion(self, uri: str, line: int, character: int) -> List[Dict[str, Any]]:
        """获取补全项"""
        if not self._initialized:
            return []
        if not self._server_capabilities.get("completionProvider"):
            return []
        resp = self._request("textDocument/completion", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "context": {"triggerKind": 1},  # Invoked
        }, timeout=5)  # 补全超时短一些
        if resp is None:
            return []
        # CompletionList 或 CompletionItem[]
        if isinstance(resp, dict):
            return resp.get("items", [])
        if isinstance(resp, list):
            return resp
        return []

    def will_save(self, uri: str, reason: int = 1):
        """通知文档即将保存"""
        if not self._initialized:
            return
        self._notify("textDocument/willSave", {
            "textDocument": {"uri": uri},
            "reason": reason,
        })

    # ── 协议层 ────────────────────────────────────────────────
    def _next_id(self) -> int:
        with self._lock:
            self._msg_id += 1
            return self._msg_id

    def _send_message(self, msg: dict):
        """将消息放入写队列"""
        try:
            body = json.dumps(msg, ensure_ascii=False)
            header = f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n"
            self._write_queue.put(header + body)
        except Exception as e:
            logger.debug("LSP发送消息失败: %s", e)

    def _send_response(self, msg_id: Any, result: Any):
        """发送响应消息"""
        self._send_message({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _send_error(self, msg_id: Any, code: int, message: str):
        """发送错误响应"""
        self._send_message({
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}
        })

    def _request(self, method: str, params: dict, timeout: float = LSP_TIMEOUT) -> Optional[Any]:
        """发送请求并等待响应"""
        if self._proc is None or self._shutdown_requested:
            return None
        msg_id = self._next_id()
        resp_queue: "queue.Queue[Any]" = queue.Queue(maxsize=1)
        with self._lock:
            self._response_queue[msg_id] = resp_queue
        msg = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params,
        }
        self._send_message(msg)
        try:
            resp = resp_queue.get(timeout=timeout)
            # 处理错误哨兵
            if isinstance(resp, dict) and resp.get("__lsp_error__"):
                err = resp["__lsp_error__"]
                logger.debug("LSP %s error on %s: %s", self._server_name, method,
                             err.get("message", "")[:200])
                return None
            return resp
        except queue.Empty:
            logger.debug("LSP请求超时: %s", method)
            return None
        finally:
            with self._lock:
                self._response_queue.pop(msg_id, None)

    def _notify(self, method: str, params: dict):
        """发送通知（不等待响应）"""
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._send_message(msg)

    def _writer_loop(self):
        """写线程：从队列读取消息写入stdin"""
        while not self._shutdown_requested:
            try:
                data = self._write_queue.get(timeout=0.5)
                if data is None:
                    break
                if self._proc and self._proc.stdin:
                    self._proc.stdin.write(data.encode("utf-8"))
                    self._proc.stdin.flush()
            except queue.Empty:
                continue
            except Exception as e:
                if not self._shutdown_requested:
                    logger.debug("LSP写线程错误: %s", e)
                break

    def _stderr_drain_loop(self):
        """持续消费stderr，防止管道满导致服务器死锁"""
        try:
            while not self._shutdown_requested:
                if self._proc is None or self._proc.stderr is None:
                    break
                chunk = self._proc.stderr.read(4096)
                if not chunk:
                    break
                # 只记录warning以上的stderr，避免日志泛滥
                try:
                    text = chunk.decode("utf-8", errors="replace").strip()
                    if text:
                        logger.debug("[LSP %s stderr] %s", self._server_name, text[:500])
                except Exception:
                    pass
        except Exception:
            pass

    def _reader_loop(self):
        """读线程：从stdout读取LSP消息（使用大块读取+缓冲解析）"""
        buf = b""
        content_length = None
        consecutive_errors = 0
        while not self._shutdown_requested:
            try:
                if self._proc is None or self._proc.stdout is None:
                    break
                # 大块读取，避免逐字节read的性能问题
                chunk = self._proc.stdout.read(4096)
                if not chunk:
                    # stdout关闭，服务器退出
                    break
                buf += chunk

                # 循环解析缓冲区中所有完整消息
                while True:
                    # 解析 Content-Length 头
                    if content_length is None:
                        header_end = buf.find(b"\r\n\r\n")
                        if header_end == -1:
                            # 头部不完整，继续读
                            # 防止缓冲区无限增长（异常情况）
                            if len(buf) > 65536:
                                logger.warning("LSP header too large (%d bytes), resetting buffer", len(buf))
                                buf = b""
                            break
                        header = buf[:header_end].decode("ascii", errors="replace")
                        content_length = None
                        for line in header.split("\r\n"):
                            if line.lower().startswith("content-length:"):
                                try:
                                    content_length = int(line.split(":", 1)[1].strip())
                                except ValueError:
                                    content_length = -1
                        if content_length is None or content_length < 0:
                            # 没有Content-Length或解析失败，流可能错位，断开连接
                            logger.warning("LSP %s: invalid/missing Content-Length header, disconnecting",
                                           self._server_name)
                            self._shutdown_requested = True
                            # 清理子进程与管道，避免 is_running 仍为 True 但实际已断连。
                            # 从读线程调用 shutdown() 是安全的：此时 _shutdown_requested 已为 True，
                            # _request 会立即短路返回；对当前线程的 join 会抛 RuntimeError 被吞掉，不死锁。
                            self.shutdown()
                            return
                        buf = buf[header_end + 4:]

                    # 检查消息体是否完整
                    if len(buf) < content_length:
                        break  # 需要更多数据

                    # 提取完整消息
                    body = buf[:content_length]
                    buf = buf[content_length:]
                    content_length = None
                    try:
                        msg = json.loads(body.decode("utf-8", errors="replace"))
                        self._handle_message(msg)
                        consecutive_errors = 0
                    except json.JSONDecodeError:
                        consecutive_errors += 1
                        if consecutive_errors > 5:
                            logger.warning("LSP %s: too many JSON parse errors, disconnecting",
                                           self._server_name)
                            self._shutdown_requested = True
                            # 清理子进程与管道（同上 invalid Content-Length 分支）。
                            self.shutdown()
                            return
            except Exception as e:
                if not self._shutdown_requested:
                    logger.debug("LSP读线程错误: %s", e)
                break

    def _handle_message(self, msg: dict):
        """处理收到的LSP消息"""
        # 响应消息（有id且有result或error）
        if "id" in msg and ("result" in msg or "error" in msg):
            msg_id = msg["id"]
            with self._lock:
                resp_queue = self._response_queue.get(msg_id)
            if resp_queue:
                try:
                    if "error" in msg:
                        err = msg["error"]
                        # 返回一个特殊的错误哨兵，而非None（None表示无结果）
                        resp_queue.put_nowait({"__lsp_error__": err})
                    else:
                        resp_queue.put_nowait(msg.get("result"))
                except queue.Full:
                    pass
            return

        # 服务器→客户端请求（有id和method，但无result/error），必须响应否则服务器挂起
        if "id" in msg and "method" in msg:
            method = msg.get("method", "")
            msg_id = msg["id"]
            logger.debug("LSP %s: server request %s (id=%s)", self._server_name, method, msg_id)
            # 处理动态能力注册
            if method == "client/registerCapability":
                params = msg.get("params", {})
                registrations = params.get("registrations", [])
                with self._lock:
                    for reg in registrations:
                        method_name = reg.get("method", "")
                        if method_name:
                            # 将动态注册的能力加入server_capabilities
                            cap_key = method_name.split("/")[-1]
                            # 常见映射：textDocument/definition -> definitionProvider
                            provider_map = {
                                "definition": "definitionProvider",
                                "references": "referencesProvider",
                                "hover": "hoverProvider",
                                "rename": "renameProvider",
                                "formatting": "documentFormattingProvider",
                                "completion": "completionProvider",
                                "codeAction": "codeActionProvider",
                            }
                            for k, v in provider_map.items():
                                if k in method_name:
                                    self._server_capabilities[v] = True
                self._send_response(msg_id, None)
            elif method == "client/unregisterCapability":
                self._send_response(msg_id, None)
            elif method == "workspace/configuration":
                # 返回空配置，避免服务器等待
                params = msg.get("params", {})
                items = params.get("items", [])
                self._send_response(msg_id, [None] * len(items))
            elif method == "window/showMessageRequest":
                # 简单地选第一个选项或返回null
                self._send_response(msg_id, None)
            elif method == "workspace/applyEdit":
                # 不支持applyEdit，返回拒绝
                self._send_response(msg_id, {"applied": False, "failureReason": "not supported"})
            else:
                # 其他未知请求，返回MethodNotFound错误
                self._send_error(msg_id, -32601, f"Method not found: {method}")
            return

        # 通知消息（无id）
        method = msg.get("method", "")
        params = msg.get("params", {})
        if method == "textDocument/publishDiagnostics":
            uri = params.get("uri", "")
            diagnostics = params.get("diagnostics", [])
            with self._lock:
                self._diagnostics[uri] = diagnostics
        elif method in ("window/logMessage", "window/showMessage"):
            # 服务器日志消息，debug级别记录
            msg_type = params.get("type", 4)
            msg_text = params.get("message", "")
            if msg_type <= 2:  # Error/Warning
                logger.debug("[LSP %s] %s", self._server_name, msg_text[:200])
        elif method in ("$/progress", "$/logTrace"):
            # 进度/追踪通知，忽略
            pass

    def _parse_locations(self, resp) -> List[Dict[str, Any]]:
        """解析LSP响应中的Location或LocationLink列表"""
        if resp is None:
            return []
        results = []
        if isinstance(resp, dict):
            # 单个Location
            results.append(self._normalize_location(resp))
        elif isinstance(resp, list):
            for item in resp:
                if isinstance(item, dict):
                    results.append(self._normalize_location(item))
        return [r for r in results if r]

    @staticmethod
    def _normalize_location(loc: dict) -> Optional[Dict[str, Any]]:
        """标准化Location/LocationLink格式"""
        if "uri" in loc and "range" in loc:
            return {"uri": loc["uri"], "range": loc["range"]}
        # LocationLink格式: targetUri + targetRange + targetSelectionRange
        if "targetUri" in loc:
            return {
                "uri": loc["targetUri"],
                "range": loc.get("targetSelectionRange", loc.get("targetRange", {})),
            }
        return None


# ── LSP管理器（管理多个LSP服务器实例）──────────────────────────

# 支持的LSP服务器配置
LSP_SERVER_CONFIGS = {
    "python": {
        "servers": [
            {"name": "pyright", "cmd": ["pyright-langserver", "--stdio"], "check": "pyright-langserver"},
            {"name": "pylsp", "cmd": ["pylsp"], "check": "pylsp"},
            {"name": "jedi-language-server", "cmd": ["jedi-language-server"], "check": "jedi-language-server"},
        ],
        "extensions": [".py"],
    },
    "typescript": {
        "servers": [
            {"name": "typescript-language-server", "cmd": ["typescript-language-server", "--stdio"],
             "check": "typescript-language-server"},
        ],
        "extensions": [".ts", ".tsx", ".js", ".jsx"],
    },
    "go": {
        "servers": [
            {"name": "gopls", "cmd": ["gopls", "serve"], "check": "gopls"},
        ],
        "extensions": [".go"],
    },
    "rust": {
        "servers": [
            {"name": "rust-analyzer", "cmd": ["rust-analyzer"], "check": "rust-analyzer"},
        ],
        "extensions": [".rs"],
    },
}


class LSPManager:
    """LSP管理器 — 自动检测并启动合适的LSP服务器，自研LSP Lite作为fallback"""

    def __init__(self, work_dir: str):
        self.work_dir = str(Path(work_dir).resolve())
        self.root_uri = f"file://{self.work_dir}"
        self._clients: Dict[str, LSPClient] = {}  # language -> LSPClient
        self._lock = threading.RLock()
        self._open_documents: Dict[str, str] = {}  # uri -> language

    def _detect_languages(self) -> List[str]:
        """检测项目中使用的编程语言"""
        exts = set()
        try:
            root = Path(self.work_dir)
            for item in root.iterdir():
                if item.name.startswith(".") or item.name in {"node_modules", "venv", "__pycache__", ".git"}:
                    continue
                if item.is_file():
                    exts.add(item.suffix.lower())
            # 简单扫描一级子目录
            for subdir in list(root.iterdir())[:20]:
                if subdir.is_dir() and not subdir.name.startswith("."):
                    for item in list(subdir.iterdir())[:30]:
                        if item.is_file():
                            exts.add(item.suffix.lower())
        except Exception:
            pass
        langs = []
        for lang, config in LSP_SERVER_CONFIGS.items():
            if any(ext in exts for ext in config["extensions"]):
                langs.append(lang)
        return langs

    def _which(self, name: str) -> bool:
        """检查命令是否在PATH中"""
        from shutil import which
        return which(name) is not None

    def _start_language_server(self, language: str) -> Optional[LSPClient]:
        """尝试启动某语言的LSP服务器"""
        config = LSP_SERVER_CONFIGS.get(language)
        if not config:
            return None
        for server_config in config["servers"]:
            if not self._which(server_config["check"]):
                continue
            client = LSPClient(self.root_uri)
            if client.start_server(server_config["name"], server_config["cmd"], cwd=self.work_dir):
                if client.initialize():
                    logger.info("[LSP] 已启动 %s for %s", server_config["name"], language)
                    return client
                else:
                    client.shutdown()
        return None

    def start(self, languages: Optional[List[str]] = None) -> int:
        """启动LSP服务器（自动检测或指定语言）"""
        started = 0
        with self._lock:
            langs = languages or self._detect_languages()
            for lang in langs:
                if lang in self._clients:
                    continue
                client = self._start_language_server(lang)
                if client:
                    self._clients[lang] = client
                    started += 1
        return started

    def shutdown_all(self):
        """关闭所有LSP服务器"""
        with self._lock:
            for client in self._clients.values():
                try:
                    client.shutdown()
                except Exception:
                    pass
            self._clients.clear()
            self._open_documents.clear()

    def _get_language_for_file(self, file_path: str) -> Optional[str]:
        """根据文件扩展名确定语言"""
        ext = Path(file_path).suffix.lower()
        for lang, config in LSP_SERVER_CONFIGS.items():
            if ext in config["extensions"]:
                return lang
        return None

    def _get_client(self, file_path: str) -> Optional[LSPClient]:
        """获取文件对应的LSP客户端"""
        uri = LSPClient.path_to_uri(file_path)
        lang = self._get_language_for_file(file_path)
        if not lang:
            return None
        with self._lock:
            client = self._clients.get(lang)
            if client is None or not client.is_running:
                # 尝试启动
                client = self._start_language_server(lang)
                if client:
                    self._clients[lang] = client
                else:
                    return None
            # 确保文件已打开
            if uri not in self._open_documents:
                # 读取文件内容
                try:
                    text = Path(file_path).read_text(encoding="utf-8", errors="replace")
                except Exception:
                    text = ""
                lang_id = {"python": "python", "javascript": "javascript", "typescript": "typescript",
                           "go": "go", "rust": "rust"}.get(lang, "plaintext")
                client.did_open(uri, language_id=lang_id, text=text)
                self._open_documents[uri] = lang
            return client

    def goto_definition(self, file_path: str, line: int, col: int) -> List[Dict[str, Any]]:
        """跳转到定义"""
        client = self._get_client(file_path)
        if client is None:
            return []
        uri = LSPClient.path_to_uri(file_path)
        return client.goto_definition(uri, line, col)

    def find_references(self, file_path: str, line: int, col: int,
                        include_declaration: bool = True) -> List[Dict[str, Any]]:
        """查找引用"""
        client = self._get_client(file_path)
        if client is None:
            return []
        uri = LSPClient.path_to_uri(file_path)
        return client.find_references(uri, line, col, include_declaration)

    def hover(self, file_path: str, line: int, col: int) -> Optional[Dict[str, Any]]:
        """悬停信息"""
        client = self._get_client(file_path)
        if client is None:
            return None
        uri = LSPClient.path_to_uri(file_path)
        return client.hover(uri, line, col)

    def get_diagnostics(self, file_path: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
        """获取诊断信息，file_path为None时返回所有诊断"""
        if file_path is None:
            return self.get_all_diagnostics()
        client = self._get_client(file_path)
        if client is None:
            return {}
        uri = LSPClient.path_to_uri(file_path)
        # 等待一下让诊断到达
        time.sleep(0.3)
        diags = client.get_diagnostics(uri)
        return {uri: diags} if diags else {}

    def document_symbols(self, file_path: str) -> List[Dict[str, Any]]:
        """文档符号"""
        client = self._get_client(file_path)
        if client is None:
            return []
        uri = LSPClient.path_to_uri(file_path)
        return client.document_symbols(uri)

    def rename_symbol(self, file_path: str, line: int, col: int, new_name: str) -> Dict[str, Any]:
        """重命名符号，返回 {uri: [TextEdit,...]} 的changes dict，失败返回空dict"""
        client = self._get_client(file_path)
        if client is None:
            return {}
        uri = LSPClient.path_to_uri(file_path)
        result = client.rename_symbol(uri, line, col, new_name)
        if not result:
            return {}
        # WorkspaceEdit 可能是 changes 或 documentChanges
        changes = result.get("changes", {})
        if not changes and "documentChanges" in result:
            for dc in result.get("documentChanges", []):
                edits = dc.get("edits", [])
                doc_uri = dc.get("textDocument", {}).get("uri", "")
                if doc_uri and edits:
                    changes[doc_uri] = edits
        return changes

    def format_document(self, file_path: str) -> List[Dict[str, Any]]:
        """格式化文档，返回TextEdit列表"""
        client = self._get_client(file_path)
        if client is None:
            return []
        uri = LSPClient.path_to_uri(file_path)
        return client.format_document(uri)

    def format_range(self, file_path: str, start_line: int, start_char: int,
                     end_line: int, end_char: int) -> List[Dict[str, Any]]:
        """格式化范围"""
        client = self._get_client(file_path)
        if client is None:
            return []
        uri = LSPClient.path_to_uri(file_path)
        return client.format_range(uri, start_line, start_char, end_line, end_char)

    def code_actions(self, file_path: str, start_line: int, start_char: int,
                     end_line: int, end_char: int,
                     diagnostics: Optional[List[Dict]] = None) -> List[Dict[str, Any]]:
        """获取代码操作（快速修复）"""
        client = self._get_client(file_path)
        if client is None:
            return []
        uri = LSPClient.path_to_uri(file_path)
        return client.code_actions(uri, start_line, start_char, end_line, end_char, diagnostics)

    def apply_workspace_edit(self, changes: Dict[str, List[Dict]]) -> Dict[str, int]:
        """应用WorkspaceEdit（由rename返回的changes）到实际文件，返回每个文件的修改行数"""
        results: Dict[str, int] = {}
        for uri, edits in changes.items():
            try:
                fpath = LSPClient.uri_to_path(uri)
                p = Path(fpath)
                if not p.is_file():
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")

                # 将所有edit转为(offset, end_offset, new_text)，基于原始文本计算
                # 注意：必须一次性基于原始text计算所有offset，然后按从大到小应用
                def line_char_to_offset(text_str: str, line: int, char: int) -> int:
                    """0-based line/char 转字符偏移"""
                    off = 0
                    lines_list = text_str.split('\n')
                    for i in range(min(line, len(lines_list))):
                        off += len(lines_list[i]) + 1  # +1 for '\n'
                    # char位置不能超过该行长度
                    if line < len(lines_list):
                        off += min(char, len(lines_list[line]))
                    return off

                offset_edits = []
                for edit in edits:
                    rng = edit.get("range", {})
                    start = rng.get("start", {})
                    end = rng.get("end", {})
                    new_text = edit.get("newText", "")
                    sl, sc = start.get("line", 0), start.get("character", 0)
                    el, ec = end.get("line", 0), end.get("character", 0)
                    try:
                        so = line_char_to_offset(text, sl, sc)
                        eo = line_char_to_offset(text, el, ec)
                        if 0 <= so <= eo <= len(text):
                            offset_edits.append((so, eo, new_text))
                    except Exception:
                        continue

                # 按offset从大到小排序，从后往前替换，避免偏移错位
                offset_edits.sort(key=lambda x: x[0], reverse=True)
                applied = 0
                for so, eo, new_text in offset_edits:
                    try:
                        text = text[:so] + new_text + text[eo:]
                        applied += 1
                    except Exception:
                        continue

                p.write_text(text, encoding="utf-8")
                results[fpath] = applied
            except Exception:
                continue
        return results

    def did_open(self, file_path: str, text: str = ""):
        """通知文档打开"""
        uri = LSPClient.path_to_uri(file_path)
        lang = self._get_language_for_file(file_path)
        if not lang:
            return
        with self._lock:
            client = self._clients.get(lang)
            if client is None or not client.is_running:
                client = self._start_language_server(lang)
                if client:
                    self._clients[lang] = client
                else:
                    return
            # 检查是否已经打开过
            if uri in self._open_documents:
                # 已打开，发送did_change而不是重复did_open
                if text:
                    client.did_change(uri, text)
                return
            # 新打开
            if not text:
                try:
                    text = Path(file_path).read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
            lang_id = {"python": "python", "javascript": "javascript", "typescript": "typescript",
                       "go": "go", "rust": "rust"}.get(lang or "", "plaintext")
            client.did_open(uri, language_id=lang_id, text=text)
            self._open_documents[uri] = lang

    def did_change(self, file_path: str, text: str):
        """通知文档变更"""
        client = self._get_client(file_path)
        if client is None:
            return
        uri = LSPClient.path_to_uri(file_path)
        client.did_change(uri, text)

    def did_save(self, file_path: str, text: str = ""):
        """通知文档保存"""
        client = self._get_client(file_path)
        if client is None:
            return
        uri = LSPClient.path_to_uri(file_path)
        client.did_save(uri, text)

    def did_close(self, file_path: str):
        """通知文档关闭"""
        lang = self._get_language_for_file(file_path)
        if not lang:
            return
        with self._lock:
            client = self._clients.get(lang)
            if client:
                uri = LSPClient.path_to_uri(file_path)
                client.did_close(uri)
                self._open_documents.pop(uri, None)

    def get_all_diagnostics(self) -> Dict[str, List[Dict[str, Any]]]:
        """获取所有文件诊断"""
        all_diags: Dict[str, List[Dict]] = {}
        with self._lock:
            for client in self._clients.values():
                try:
                    diags = client.get_all_diagnostics()
                    all_diags.update(diags)
                except Exception:
                    pass
        return all_diags

    def list_running_servers(self) -> List[str]:
        """列出运行中的LSP服务器"""
        with self._lock:
            return [f"{lang}:{client._server_name}" for lang, client in self._clients.items() if client.is_running]
