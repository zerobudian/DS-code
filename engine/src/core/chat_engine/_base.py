# -*- coding: utf-8 -*-
"""ChatEngineBase — 对话引擎核心状态管理（拆分自 chat_engine.py）。

本类持有所有运行期状态（历史、配置、内存层、看门狗等）并通过 ``__init__``
完成初始化；其余 mixin 通过 ``self.`` 访问这些状态。``ChatEngine`` 在
``__init__.py`` 中以本类为最后基类，确保 ``__init__`` 来自这里。
"""
from __future__ import annotations

from src.core.chat_engine._imports import *  # noqa: F401, F403




class ChatEngineBase(object):
    """对话引擎基类 - 整合 DeepSeek API + 工具调用 + 历史上下文 (核心状态管理)"""

    # C-001: API 调用重试上限与指数退避上限
    # 注意：底层 chat_stream 内部已有 3 次网络层重试，此处 2 次用于处理流式中断等高层失败
    _MAX_MODEL_RETRIES = 2
    _MAX_RETRY_DELAY = 8.0
    # C-001: 单个工具调用重试上限（仅对瞬时/网络类错误重试，非幂等工具失败不重试）
    _MAX_TOOL_RETRIES = 1

    def __init__(self):
        self.cfg = Config()
        self.api = DeepSeekAPI()
        # 核心依赖使用真实类型，便于 mypy 与 IDE 静态检查，避免 object 上的未知属性调用
        self.sandbox: Optional[ProjectSandbox] = None
        self.history: Optional[ConversationHistory] = None
        self.tool_exec: Optional[ToolExecutor] = None
        # 以下模块由外部注入或动态加载，使用 Protocol 标注，既保留注入灵活性，
        # 又能让静态分析识别方法签名，提前发现 AttributeError / None 风险。
        self.mcp_manager: Optional[_MCPManagerLike] = None
        self.sklii_manager: Optional[_SklIIIManagerLike] = None
        self.image_doc_manager: Optional[_ImageDocumentManagerLike] = None
        self.hook_manager = None        # Hooks钩子系统（set_project中初始化）
        self.skill_manager = None       # Skills技能系统（set_project中初始化）
        self.lsp_manager = None         # LSP管理器（set_project中初始化）
        self._code_vector_store = None  # 代码向量存储（懒初始化）
        self._pending_skill_prompt: Optional[str] = None  # 待注入的技能执行提示
        self._subagent_depth: int = 0  # 当前引擎在子代理嵌套中的深度
        self._state_lock = threading.RLock()  # 保护 stop / images / phase 等共享状态的写
        # L-014: 防止发送按钮重复提交，确保同一时刻只有一个 send_message 在执行
        self._send_lock = threading.Lock()
        self._send_in_progress: bool = False
        # 受 _state_lock 保护的标志统一放在 _state dict，避免分散的 torn read。
        # 使用 Dict[str, Any] 因为内部存储多种类型（bool/str/float/list/None），
        # 所有读取都通过 _state_cost / _state_int 等带类型转换的辅助方法完成。
        self._state: Dict[str, Any] = {
            "stop_requested": False,   # 新的可取消标志（替代 _stop）
            "stop": False,             # 保持旧名字兼容
            "phase": "planning",       # "planning" | "executing"
            "pending_plan": None,      # 计划文本
            "plan_confirmed": False,   # 用户是否已确认当前计划
            "auto_pre_snapshot": True, # 进入 execution 前自动创建快照
            "images_for_message": [],  # 当前消息附带的图片 (base64 data URL)
            "total_cost_cny": 0.0,
            "total_cached": 0,
            "total_tokens": 0,
            "project_context_cache": None,
            "mode": self.cfg.get("chat_mode", "normal"),
            "subagents_launched": False,  # 本轮是否启动了子代理
        }
        # 兼容旧模式名: plan_iterate / deep → ultra
        if self._state["mode"] in ("plan_iterate", "deep"):
            self._state["mode"] = "ultra"
        if self._state["mode"] not in ("normal", "plan", "ultra"):
            self._state["mode"] = "normal"
        self._gate = ConfirmationGate()    # 确认门（组合方式）
        # 注意: gate 内部回调不设为 _on_confirm_command_engine / _on_edit_apply_request
        # 否则会形成自引用递归（这两个函数内部又调用 gate.request_*）
        # 改为通过 wait_for_* 方法 → 走 engine 的公共回调 on_confirm_command / on_confirm_edit
        self._subagent: Optional[_SubAgentEngineLike] = None  # 子代理引擎（懒加载，set_project 中初始化）
        # 用户可能显式发送 "/plan" "/undo" 等命令 — 由本类拦截处理
        self._COMMANDS = {"/clear", "/reset", "/undo", "/snapshot", "/restore",
                          "/snapshots", "/plan", "/ultra", "/help", "/hook", "/lsp"}

        # ========== 回调接口 (GUI 或 CLI 自行设置) ==========
        self.on_status_update: Optional[Callable[[str], None]] = None
        self.on_deep_mode_status: Optional[Callable[[str], None]] = None
        self.on_new_turn: Optional[Callable[[int], None]] = None
        self.on_content_delta: Optional[Callable[[str], None]] = None
        self.on_reasoning_delta: Optional[Callable[[str], None]] = None
        self.on_reasoning: Optional[Callable[[str], None]] = None
        self.on_tool_call_detected: Optional[Callable[[Dict], None]] = None
        self.on_tool_result: Optional[Callable[[str, str], None]] = None
        self.on_tool_start: Optional[Callable[[str, Dict], None]] = None
        self.on_tool_end: Optional[Callable[[str, bool, str], None]] = None
        self.on_usage: Optional[Callable[[Dict], None]] = None
        self.on_assistant_message: Optional[Callable[[str, str, List[Dict]], None]] = None
        self.on_finished: Optional[Callable[[float, float, int, int], None]] = None
        self.on_error: Optional[Callable[[int, str], None]] = None
        # 长期记忆回调：返回 str 注入 system prompt（GUI 面板可管理记忆）
        self.memory_snippet_provider: Optional[Callable[[int], str]] = None
        self.on_exit_conversation: Optional[Callable[[str], None]] = None
        self.on_need_new_agent: Optional[Callable[[], None]] = None
        self.on_ask_user_questions: Optional[Callable[[Dict], Optional[str]]] = None  # 返回用户回答文本
        self.on_confirm_command: Optional[Callable[[Dict], None]] = None  # 发信号弹窗, GUI 调用 submit_command_confirmation 回复
        self.on_confirm_edit: Optional[Callable[[str, str], None]] = None  # 发信号弹窗显示 diff 确认, GUI 调用 confirm_edit 回复
        self.on_plan_ready: Optional[Callable[[str], None]] = None  # 计划就绪（供 GUI 弹确认）
        self.on_chat_clear_requested: Optional[Callable[[], None]] = None  # 请求清空聊天
        # 文件系统监听 —— work 目录 / DS.md 变化时自动通知上层刷新右侧面板
        self._fs_monitor = FileSystemMonitor()
        self.on_files_changed: Optional[Callable[[], None]] = None
        self.on_tool_status: Optional[Callable[[str, str], None]] = None
        self.on_conversation_title_changed: Optional[Callable[[str], None]] = None
        self.on_code_index_status: Optional[Callable[[str], None]] = None  # 代码索引状态：ready/degraded/unavailable
        # 终端面板回调：命令工具的逐行流式输出
        self.on_output_line: Optional[Callable[[str], None]] = None

        # ========== Artifact 自动识别 ==========
        self.artifact_manager: Optional[ArtifactManager] = ArtifactManager()
        self.on_artifact_updated: Optional[Callable[[List[Dict[str, Any]]], None]] = None
        try:
            self.artifact_manager.on_artifact_updated = lambda arts: self._on_artifact_updated(arts)
        except Exception as _exc:
            logger.debug("ArtifactManager 回调绑定失败: %s", _exc, exc_info=True)

        # ========== Kun Runtime 集成 ==========
        self._kun = KunRuntime()
        self._session_id: Optional[str] = None

        # ========== 自动记忆（懒加载）==========
        self.auto_memory: Optional["AutoMemory"] = None

        # ========== 三层持久记忆（懒加载）==========
        self.session_memory: Optional["SessionMemory"] = None
        self.long_term_memory: Optional["LongTermMemory"] = None
        self._user_profile_memory: Optional["UserProfileMemory"] = None

        # ========== 会话 checkpoint 定时保存 ==========
        # ChatEngine 负责管理 session_memory 的后台定时 checkpoint：
        # 每 5 秒检查一次 _dirty，若脏则调用 save_checkpoint() 原子化落盘。
        self._session_memory_checkpoint_stop = threading.Event()
        self._session_memory_checkpoint_thread: Optional[threading.Thread] = None
        self._session_memory_checkpoint_interval = 5.0

    @property
    def user_profile_memory(self) -> Optional["UserProfileMemory"]:
        """用户画像记忆：首次访问时懒加载，避免启动时触发文件 IO。"""
        if self._user_profile_memory is None:
            try:
                from src.core.user_profile_memory import UserProfileMemory
                self._user_profile_memory = UserProfileMemory()
            except Exception as e:
                print(f"[ChatEngine] 用户画像记忆初始化失败: {e}")
                self._user_profile_memory = None
        return self._user_profile_memory

    def submit_user_answer(self, answer_text: str):
        """由 GUI 调用：用户提交回答后唤醒等待的 chat 线程"""
        self._gate.submit_user_answer(answer_text)

    def submit_command_confirmation(self, confirmed: bool):
        """由 GUI 调用：用户同意/拒绝执行命令后唤醒 chat 线程"""
        self._gate.submit_command_confirmation(confirmed)

    def confirm_command(self, confirmed: bool):
        """submit_command_confirmation 的别名（方便外部调用）"""
        self._gate.confirm_command(confirmed)

    def confirm_edit(self, confirmed: bool):
        """由 GUI 调用：用户同意/拒绝文件 diff 后唤醒 chat 线程"""
        self._gate.confirm_edit(confirmed)

    def stop(self):
        """停止当前对话（新接口） — 线程安全写入"""
        # 防止用户/看门狗重复调用导致重复归档。
        # 仅当停止标志已置位且会话已经归档为 stopped 时才直接返回，
        # 避免外部测试或异常路径中标志被提前设置而跳过必要归档。
        if self._is_stopped():
            if self.session_memory is None or self.session_memory.status == "stopped":
                return
        # 先置位 stop 标志，再归档 — 防止归档窗口内 _emit_finished 覆盖为 finished
        with self._state_lock:
            self._state["stop_requested"] = True
            self._state["stop"] = True
        # C-2 修复: 取消所有正在等待的用户确认（命令确认/编辑确认/问答），
        # 唤醒阻塞的 chat 线程，避免最长 600 秒超时等待
        try:
            self._gate.cancel_all()
        except Exception as _exc:
            logger.debug("取消确认门异常: %s", _exc, exc_info=True)
        # C-3 修复: 停止所有正在运行的子代理
        if self._subagent is not None:
            try:
                self._subagent.stop_all(timeout=2.0)
            except Exception as _exc:
                logger.debug("停止子代理异常: %s", _exc, exc_info=True)
        self._archive_session_memory("stopped")

    def _is_stopped(self) -> bool:
        """读取停止标志 — 线程安全（一次加锁读取避免 torn read）"""
        with self._state_lock:
            return bool(self._state.get("stop_requested")) or bool(self._state.get("stop"))

    def _get_model(self) -> str:
        """获取当前生效的模型名称。
        子 agent 调用时可通过 _subagent_model_override 覆盖，否则使用全局配置。
        （线程安全：通过 _state_lock 保护 _subagent_model_override 的读写）
        """
        with self._state_lock:
            override = self._state.get("_subagent_model_override", None)
        if override and isinstance(override, str) and override.strip():
            return override.strip()
        return self.cfg.model

    def _state_cost(self, key: str, default: float = 0.0) -> float:
        """线程安全读取 _state 中的浮点数值，异常时回退到 default。"""
        with self._state_lock:
            val = self._state.get(key, default)
        try:
            return float(val) if val is not None else default
        except (TypeError, ValueError):
            return default

    def _state_int(self, key: str, default: int = 0) -> int:
        """线程安全读取 _state 中的整数值，异常时回退到 default。"""
        with self._state_lock:
            val = self._state.get(key, default)
        try:
            return int(val) if val is not None else default
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------
    # SessionMemory checkpoint 定时保存 / 崩溃恢复
    # ------------------------------------------------------------------

    def _try_recover_session_memory(self, sandbox_root: Path) -> Optional["SessionMemory"]:
        """检查并恢复未归档的会话 checkpoint。

        若存在有效 checkpoint，打印日志并恢复状态；返回 None 表示无需恢复。
        """
        try:
            from src.core.session_memory import SessionMemory
            if not SessionMemory.has_checkpoint(sandbox_root):
                return None
            print(f"[ChatEngine] 检测到未归档的会话 checkpoint，正在恢复: {sandbox_root}")
            recovered = SessionMemory.load_checkpoint(sandbox_root, enable_auto_checkpoint=False)
            if recovered is not None:
                print(f"[ChatEngine] 会话 checkpoint 已恢复: {recovered.to_dict()}")
                self._start_session_memory_checkpoint()
            return recovered
        except Exception as e:
            print(f"[ChatEngine] 恢复会话 checkpoint 失败: {e}")
            return None

    def _start_session_memory_checkpoint(self) -> None:
        """启动 session_memory 后台定时 checkpoint 线程。"""
        self._stop_session_memory_checkpoint()
        self._session_memory_checkpoint_stop.clear()
        # 确保旧线程完全退出后再启动新线程，避免快速切换项目时出现多个 checkpoint 线程
        deadline = time.monotonic() + 5.0
        while (
            self._session_memory_checkpoint_thread is not None
            and self._session_memory_checkpoint_thread.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        self._session_memory_checkpoint_thread = threading.Thread(
            target=self._session_memory_checkpoint_loop,
            daemon=True,
            name="ChatEngineSessionCheckpoint",
        )
        self._session_memory_checkpoint_thread.start()

    def _stop_session_memory_checkpoint(self) -> None:
        """停止 session_memory 后台定时 checkpoint 线程。"""
        self._session_memory_checkpoint_stop.set()
        thread = self._session_memory_checkpoint_thread
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=3.0)
            except Exception as _exc:
                logger.debug("停止 checkpoint 线程失败: %s", _exc, exc_info=True)
        # 只有确认线程已死亡才清空引用，否则上层仍可判断旧线程是否存活
        if thread is None or not thread.is_alive():
            self._session_memory_checkpoint_thread = None

    def _session_memory_checkpoint_loop(self) -> None:
        """每 5 秒检查 session_memory._dirty，脏时触发 save_checkpoint。"""
        while not self._session_memory_checkpoint_stop.wait(
            timeout=self._session_memory_checkpoint_interval
        ):
            try:
                mem = self.session_memory
                if mem is not None and mem.dirty:
                    mem.save_checkpoint()
            except Exception as _exc:
                logger.debug("定时 checkpoint 失败: %s", _exc, exc_info=True)

    def _accumulate_usage(self, usage: Dict) -> None:
        """把单轮 usage 累加到 _state 的 total_cost_cny / total_cached / total_tokens。

        集中处理类型转换，避免在多处重复写 float(object) / int(object)。
        """
        try:
            cost = float(usage.get("cost_cny", 0) or 0)
        except (TypeError, ValueError):
            cost = 0.0
        try:
            hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        except (TypeError, ValueError):
            hit = 0
        try:
            miss = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
        except (TypeError, ValueError):
            miss = 0
        with self._state_lock:
            self._state["total_cost_cny"] = self._state_cost("total_cost_cny") + cost
            self._state["total_cached"] = self._state_int("total_cached") + hit
            self._state["total_tokens"] = self._state_int("total_tokens") + (hit + miss)
        try:
            if self.on_usage:
                self.on_usage(dict(usage))
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    # ========== 项目切换 ==========
    def set_project(self, sandbox: ProjectSandbox):
        """设置当前项目"""
        # P1-9 修复: 切换项目前完整清理旧资源，防止MCP/子代理/文件监视器泄漏
        # stop() 已负责归档旧会话（mark_stopped + save_checkpoint + archive + 停止checkpoint线程）
        self.stop()

        # 置空旧会话引用（stop()已完成归档，此处不再重复操作）
        self.session_memory = None

        # P1-9: 停止旧项目的 MCP 管理器（防止旧 servers 继续运行）
        try:
            old_mcp = getattr(self, 'mcp_manager', None)
            if old_mcp is not None and hasattr(old_mcp, 'stop_all'):
                try:
                    old_mcp.stop_all()
                except Exception as _exc:
                    logger.debug("停止旧 MCP manager 失败: %s", _exc, exc_info=True)
        except Exception:
            logger.debug("set_project 清理旧 MCP 异常", exc_info=True)

        # 停止旧项目的 LSP 管理器（防止旧 language servers 继续运行）
        try:
            old_lsp = getattr(self, 'lsp_manager', None)
            if old_lsp is not None and hasattr(old_lsp, 'shutdown_all'):
                try:
                    old_lsp.shutdown_all()
                except Exception as _exc:
                    logger.debug("停止旧 LSP manager 失败: %s", _exc, exc_info=True)
        except Exception:
            logger.debug("set_project 清理旧 LSP 异常", exc_info=True)

        # P1-9: 停止旧项目的子代理引擎
        try:
            old_subagent = getattr(self, '_subagent', None)
            if old_subagent is not None:
                if hasattr(old_subagent, 'stop_all'):
                    try:
                        old_subagent.stop_all()
                    except Exception as _exc:
                        logger.debug("停止旧 subagent engine 失败: %s", _exc, exc_info=True)
                if hasattr(old_subagent, 'stop_watchdog'):
                    try:
                        old_subagent.stop_watchdog()
                    except Exception as _exc:
                        logger.debug("停止旧 subagent watchdog 失败: %s", _exc, exc_info=True)
        except Exception:
            logger.debug("set_project 清理旧 subagent 异常", exc_info=True)

        self.sandbox = sandbox
        self.history = ConversationHistory(sandbox.history_dir)

        # MCP 管理器 (懒加载)
        from src.mcp.manager import MCPManager as _MC
        try:
            mcp_config_path = sandbox.root / "mcp_servers.json"
            self.mcp_manager = _MC(str(mcp_config_path), str(sandbox.work_dir))
            # 尝试启动所有 enabled 的 servers
            try:
                self.mcp_manager.start_all()
            except Exception as e:
                print(f"[ChatEngine] MCP 自动启动失败: {e}")
        except Exception as e:
            print(f"[ChatEngine] MCP 管理器初始化失败: {e}")
            self.mcp_manager = None

        # sklii 自定义工具包管理器
        from src.core.sklii_manager import SklIIIManager as _SklII  # 模块路径应与文件名一致: sklii_manager

        def _notify_sklii():
            if self.on_status_update:
                try:
                    self.on_status_update("sklii 包已更新")
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        try:
            project_root = str(sandbox.root) if hasattr(sandbox, 'root') else str(sandbox.work_dir)
            self.sklii_manager = _SklII(work_dir=str(sandbox.work_dir), project_dir=project_root,
                                         mcp_manager=self.mcp_manager,
                                         on_packages_changed=_notify_sklii)
        except Exception as e:
            print(f"[ChatEngine] Sklii 管理器初始化失败: {e}")
            self.sklii_manager = None

        # 图片 & 文档管理器
        try:
            project_root = str(sandbox.root) if hasattr(sandbox, 'root') else str(sandbox.work_dir)
            from src.core.image_document_manager import ImageDocumentManager as _IDM
            self.image_doc_manager = _IDM(project_root)
        except Exception as e:
            print(f"[ChatEngine] 图片文档管理器初始化失败: {e}")
            self.image_doc_manager = None

        # 自动记忆 / 手动记忆：统一使用 .dscode/memories 目录
        self.memory_manager: Optional[Any] = None
        try:
            from src.core.memory_manager import MemoryManager
            from src.core.auto_memory import AutoMemory
            memory_dir = Path(str(sandbox.work_dir)) / ".dscode" / "memories"
            self.memory_manager = MemoryManager(memory_dir)
            self.auto_memory = AutoMemory(
                memory_manager=self.memory_manager, work_dir=sandbox.work_dir
            )
        except Exception as e:
            print(f"[ChatEngine] 自动记忆初始化失败: {e}")
            self.memory_manager = None
            self.auto_memory = None

        # 三层持久记忆：会话短时记忆 + 长期全局记忆
        # 优先尝试从崩溃后遗留的 checkpoint 恢复会话短时记忆
        try:
            recovered = self._try_recover_session_memory(sandbox.work_dir)
            if recovered is not None:
                self.session_memory = recovered
            else:
                from src.core.session_memory import SessionMemory
                self.session_memory = SessionMemory(
                    sandbox_root=sandbox.work_dir,
                    enable_auto_checkpoint=False,  # 由 ChatEngine 统一调度定时线程
                )
                self._start_session_memory_checkpoint()
        except Exception as e:
            print(f"[ChatEngine] 会话短时记忆初始化失败: {e}")
            self.session_memory = None
        try:
            from src.core.long_term_memory import LongTermMemory
            self.long_term_memory = LongTermMemory(project_root=sandbox.work_dir)
        except Exception as e:
            print(f"[ChatEngine] 长期全局记忆初始化失败: {e}")
            self.long_term_memory = None

        # Hooks钩子系统 — 在ToolExecutor之前创建，注入到ToolExecutor
        from src.core.hooks import HookManager
        try:
            from platformdirs import user_config_dir
            config_dir = Path(user_config_dir("dscode", "dscode"))
        except Exception:
            config_dir = Path.home() / ".config" / "dscode"
        try:
            self.hook_manager = HookManager(
                work_dir=Path(str(sandbox.work_dir)),
                config_dir=config_dir,
            )
            self.hook_manager.load_all()
            # 从DS.md加载hooks块（使用sandbox.read_ds_md()确保路径正确且含@import解析）
            try:
                ds_content = sandbox.read_ds_md()
                if ds_content:
                    self.hook_manager.load_from_ds_md(ds_content)
            except Exception:
                logger.debug("set_project 加载 DS.md hooks 异常", exc_info=True)
        except Exception as e:
            print(f"[ChatEngine] Hooks系统初始化失败: {e}")
            self.hook_manager = None

        # --- LSP客户端（真实Language Server，LSP Lite作为fallback）---
        # 在ToolExecutor之前创建，以便注入
        try:
            from src.core.lsp_client import LSPManager
            self.lsp_manager = LSPManager(work_dir=str(sandbox.work_dir))
            # 懒启动：在第一次需要时自动启动，避免无LSP环境下启动报错
        except Exception as e:
            print(f"[ChatEngine] LSP管理器初始化失败: {e}")
            self.lsp_manager = None

        # 把管理器注入 ToolExecutor
        self.tool_exec = ToolExecutor(sandbox, mcp_manager=self.mcp_manager,
                                    sklii_manager=self.sklii_manager,
                                    image_doc_manager=self.image_doc_manager,
                                    hook_manager=self.hook_manager,
                                    lsp_manager=self.lsp_manager)

        # 把lsp_manager注入到lsp_tools中
        if self.lsp_manager is not None:
            try:
                self.tool_exec.lsp_tools.set_lsp_manager(self.lsp_manager)
            except Exception as e:
                print(f"[ChatEngine] LSP工具注入失败: {e}")

        # --- Skills技能包系统 ---
        from src.core.skills import SkillManager
        try:
            self.skill_manager = SkillManager(
                work_dir=Path(str(sandbox.work_dir)),
                config_dir=config_dir,
            )
            self.skill_manager.load_all()
            # 从DS.md加载内联skill块（使用sandbox.read_ds_md()确保路径正确）
            try:
                ds_content = sandbox.read_ds_md()
                if ds_content:
                    self.skill_manager.load_from_ds_md(ds_content)
            except Exception:
                logger.debug("set_project 加载 DS.md skills 异常", exc_info=True)
        except Exception as e:
            print(f"[ChatEngine] Skills系统初始化失败: {e}")
            self.skill_manager = None

        # --- 子代理引擎（最多 5 个并行子代理，由主模型决定分配）---
        from src.core.subagent_engine import SubAgentEngine
        try:
            self._subagent = SubAgentEngine(self)
        except Exception as e:
            print(f"[ChatEngine] 子代理引擎初始化失败: {e}")
            self._subagent = None

        # 注入可选回调
        try:
            def _output_line_with_memory(line: str):
                try:
                    if self.session_memory is not None:
                        self.session_memory.add_terminal_log(line)
                except Exception:
                    logger.debug("_output_line_with_memory 异常", exc_info=True)
                return self._on_tool_output_line(line)

            def _edit_apply_with_memory(filepath: str, diff: str):
                try:
                    if self.session_memory is not None and filepath and diff:
                        self.session_memory.add_code_change(filepath, diff[:800])
                except Exception:
                    logger.debug("_edit_apply_with_memory 异常", exc_info=True)
                return self._on_edit_apply_request(filepath, diff)

            self.tool_exec.on_output_line = _output_line_with_memory
            self.tool_exec.on_cancel_check = lambda: self._is_stopped()
            self.tool_exec.on_edit_apply_request = _edit_apply_with_memory
            self.tool_exec.on_confirm_command = lambda call: self._on_confirm_command_engine(call)
            self.tool_exec.language = self.cfg.get("language", "zh") or "zh"
            self.tool_exec.on_conversation_title_changed = lambda title: self._on_conversation_title_changed(str(title))
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        # 自动构建项目上下文
        try:
            if self.cfg.get("auto_build_context", True):
                with self._state_lock:
                    self._state["project_context_cache"] = self._build_project_context()
        except Exception:
            with self._state_lock:
                self._state["project_context_cache"] = None

        # 代码向量索引延迟初始化：仅在首次代码分析工具调用时才加载
        # （避免 torch/sentence-transformers 导入时因 NumPy 不兼容 segfault）

        self.cfg.set("last_project", sandbox.name)
        with self._state_lock:
            self._state["total_cost_cny"] = 0.0
            self._state["total_cached"] = 0
            self._state["total_tokens"] = 0
            self._state["images_for_message"] = []
            # project_context_cache 已在上方构建，此处不再重置

        # 切换项目时重置 artifact 识别器，避免旧项目 artifact 累积到当前项目
        if self.artifact_manager is not None:
            try:
                self.artifact_manager.reset()
            except Exception as _exc:
                logger.debug("ArtifactManager reset 异常: %s", _exc, exc_info=True)
        # 启动文件系统监听（work 目录 + DS.md），变化时通知上层刷新右侧面板
        try:
            self._fs_monitor.stop()
            root = sandbox.work_dir.parent if hasattr(sandbox, 'work_dir') else None
            if root:
                self._fs_monitor.start(
                    root,
                    lambda: self._files_changed()
                )
        except Exception as e:
            print(f"[ChatEngine] 文件监听启动失败: {e}")

        # ── 初始化 Kun Runtime 会话 ──
        try:
            self._session_id = sandbox.name
            self._kun.create_session(
                session_id=self._session_id,
                work_dir=sandbox.work_dir,
            )
        except ValueError:
            # 会话已存在（活跃状态），激活它
            try:
                self._kun.activate_session(self._session_id)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    # ========== 外部可注入属性 / 设置 ==========
    def set_project_context(self, context_dict: dict):
        """设置项目上下文摘要（替代自动构建）"""
        try:
            if isinstance(context_dict, dict):
                with self._state_lock:
                    self._state["project_context_cache"] = dict(context_dict)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def set_language(self, lang: str):
        """设置语言: 'zh' / 'en' / 'wenyan'"""
        try:
            lang = str(lang or "zh").lower()
            self.cfg.set("language", lang)
            if self.tool_exec is not None and hasattr(self.tool_exec, "language"):
                self.tool_exec.language = lang
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def set_dark_mode(self, enabled: bool):
        """保存 dark mode 偏好 (仅保存配置，渲染由 GUI 处理)"""
        try:
            self.cfg.set("theme", "dark" if enabled else "light")
            self.cfg.set("dark_mode", bool(enabled))
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def set_mode(self, mode: str):
        """切换模式: normal / plan / ultra。非法值静默忽略，保留当前模式。"""
        try:
            mode = str(mode or "").strip()
        except Exception:
            return
        # 兼容旧 plan_iterate / deep，统一映射为 ultra
        if mode in ("plan_iterate", "deep"):
            mode = "ultra"
        if mode not in ("normal", "plan", "ultra"):
            return
        with self._state_lock:
            self._state["mode"] = mode
            # 根据模式设置 phase
            if mode == "normal":
                self._state["phase"] = "executing"
                self._state["plan_confirmed"] = True
            elif mode == "ultra":
                # Ultra 模式：规划 → 执行 → 验证 → 迭代 → 交付
                self._state["phase"] = "ultra_executing"
                self._state["plan_confirmed"] = True
            else:
                # 规划模式：先规划，再执行
                self._state["phase"] = "planning"
                self._state["plan_confirmed"] = False

    def get_mode(self) -> str:
        with self._state_lock:
            return str(self._state.get("mode", "normal"))

    # ---------- 代码向量检索联动 ----------
    def _maybe_init_code_vector_store(self):
        """为当前 sandbox 初始化代码向量索引，并在后台异步构建。"""
        if self.sandbox is None:
            return
        work_dir = getattr(self.sandbox, "work_dir", None)
        if not work_dir:
            return
        # 代码向量索引初始化移至后台线程，避免 sentence-transformers/torch
        # 在 NumPy 不兼容环境下导入时 segfault 导致主进程崩溃。
        def _init_cv():
            try:
                from src.analysis.code_vector_store import CodeVectorStore
                store = CodeVectorStore(work_dir)
                self._code_vector_store = store
                self._update_code_index_status(store.status)
                store.index(force=False)
                self._update_code_index_status(store.status)
            except Exception as _exc:
                logger.info("代码向量索引初始化失败: %s", _exc)
                self._code_vector_store = None
                self._update_code_index_status("unavailable")
        self._update_code_index_status("initializing")
        threading.Thread(target=_init_cv, daemon=True, name="CodeVectorInit").start()

    def _update_code_index_status(self, status: str):
        """通知 UI 更新代码索引状态。"""
        if self.on_code_index_status:
            try:
                self.on_code_index_status(str(status))
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    @property
    def current_sandbox(self) -> Optional[ProjectSandbox]:
        """对外暴露当前项目沙箱"""
        return self.sandbox

    @property
    def current_history(self) -> Optional[ConversationHistory]:
        """对外暴露当前 history"""
        return self.history

    def start_new_chat(self):
        """开启新一轮对话 (重置 history 和 session_memory，防止状态泄漏)
        P0-3 修复: 完整重置会话状态，包括归档旧 session_memory、停止 checkpoint 线程、
        创建新的 SessionMemory，与 set_project() 的清理逻辑保持一致。
        """
        if self.sandbox is None:
            return
        # 先停止任何正在运行的操作
        self.stop()
        # 归档旧的 session_memory（不抽取长期记忆，因为不是会话结束）
        try:
            if self.session_memory is not None:
                self._stop_session_memory_checkpoint()
                try:
                    self.session_memory.save_checkpoint()
                except Exception as _exc:
                    logger.debug("start_new_chat 保存 checkpoint 失败: %s", _exc, exc_info=True)
                self.session_memory = None
        except Exception as _exc:
            logger.debug("start_new_chat 归档旧记忆失败: %s", _exc, exc_info=True)
        # 创建新的 history（会自动加载最新文件，所以使用 clear 清空）
        self.history = ConversationHistory(self.sandbox.history_dir, skip_load=True)
        self.history.clear()
        # 创建新的 session_memory
        try:
            from src.core.session_memory import SessionMemory
            self.session_memory = SessionMemory(
                sandbox_root=self.sandbox.work_dir,
                enable_auto_checkpoint=False,
            )
            self._start_session_memory_checkpoint()
        except Exception as _exc:
            logger.debug("start_new_chat 创建新 session_memory 失败: %s", _exc, exc_info=True)
            self.session_memory = None
        # 重置累计器，避免跨对话累加
        with self._state_lock:
            self._state["total_cost_cny"] = 0.0
            self._state["total_cached"] = 0
            self._state["total_tokens"] = 0
            self._state["phase"] = "idle"
            self._state["plan_confirmed"] = False
            self._state["pending_plan"] = None

    def load_chat(self, chat_id: str):
        """根据 chat_id 加载历史对话"""
        if self.sandbox is None:
            return
        # 安全校验 chat_id：仅允许字母、数字、下划线、连字符，禁止路径分隔符与 .. 穿越
        if not chat_id or not re.match(r"^[A-Za-z0-9_\-]{1,128}$", chat_id):
            return
        # 使用 skip_load=True 避免自动 _load_latest() 导致重复/污染
        self.history = ConversationHistory(self.sandbox.history_dir, skip_load=True)
        # 读取指定 JSON 文件并直接替换消息列表
        try:
            fpath = self.sandbox.history_dir / f"{chat_id}.json"
            # 确保解析后的路径仍在 history_dir 下，防止路径穿越
            try:
                fpath.resolve().relative_to(self.sandbox.history_dir.resolve())
            except ValueError:
                return
            if fpath.exists():
                data = json.loads(fpath.read_text(encoding="utf-8"))
                msgs = data.get("messages", []) if isinstance(data, dict) else []
                self.history.replace_messages(msgs)
                self.history.set_current_session_file(str(fpath))
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    # ========== 主入口 ==========
    @property
    def is_sending(self) -> bool:
        """是否有消息正在处理中（用于 UI 禁用/启用发送按钮）。"""
        return self._send_in_progress

    def send_message(self, user_text: str, uploaded_files: Optional[List[str]] = None,
                     images: Optional[List[str]] = None):
        """发送用户消息入口。防止按钮重复提交，若已有请求在处理中则直接拒绝。"""
        if not self._send_lock.acquire(blocking=False):
            logger.warning("拒绝重复发送：已有请求正在处理中")
            if self.on_error:
                self.on_error(0, "已有请求正在处理，请勿重复点击发送")
            return
        self._send_in_progress = True
        try:
            return self._send_message_impl(user_text, uploaded_files, images)
        finally:
            self._send_in_progress = False
            self._send_lock.release()

    def _send_message_impl(self, user_text: str, uploaded_files: Optional[List[str]] = None,
                     images: Optional[List[str]] = None):
        """发送用户消息 -> 斜杠命令或两阶段 (Planning / Execution)

        对 history/sandbox/API 调用异常均有防御，确保中途异常不会让 history 内存状态与磁盘不一致。
        """
        turn_watchdog_stop = threading.Event()
        turn_watchdog_thread: Optional[threading.Thread] = None

        # 启动单轮看门狗，防止长任务挂死 UI
        max_turn_time = int(self.cfg.get("max_turn_time", DEFAULT_MAX_TURN_TIME) or DEFAULT_MAX_TURN_TIME)
        if max_turn_time < 60:
            max_turn_time = 60
        turn_deadline = time.monotonic() + max_turn_time

        def _watch_turn():
            while not turn_watchdog_stop.wait(timeout=5.0):
                if time.monotonic() >= turn_deadline:
                    self.stop()
                    try:
                        if self.on_status_update:
                            self.on_status_update(f"本轮运行超过 {max_turn_time}s，已自动停止")
                    except Exception as _exc:
                        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                    break

        turn_watchdog_thread = threading.Thread(target=_watch_turn, daemon=True, name="TurnWatchdog")
        turn_watchdog_thread.start()

        # Bug 1修复: 用统一的 try/finally 确保看门狗线程在所有路径
        # （提前return、前置流程异常、执行阶段异常、正常完成）都被清理，
        # 防止看门狗泄漏后误杀后续会话
        try:

            # 第一步：立即重置停止标志（线程安全），防止斜杠命令或 sandbox
            # 为 None 的提前返回路径留下 stop=True，导致后续请求永久阻断（B-10）。
            with self._state_lock:
                self._state["stop"] = False
                self._state["stop_requested"] = False
            # C-4 修复: 如果 checkpoint 线程已被 stop() 停止（通过 _archive_session_memory），重启它
            if self.session_memory is not None:
                self._start_session_memory_checkpoint()
            # 重置工具调用链，防止长会话累计达到上限后瘫痪所有工具调用
            if self.tool_exec:
                self.tool_exec.reset_call_chain()

            # UserSubmit 钩子 — 用户消息提交前触发（可注入上下文）
            hook_context = ""
            if self.hook_manager is not None:
                try:
                    results = self.hook_manager.trigger(
                        "UserSubmit",
                        user_message=user_text,
                    )
                    hook_context = "\n".join(r.context_injection for r in results if r.context_injection)
                except Exception as _e:
                    logger.debug("UserSubmit钩子执行错误: %s", _e, exc_info=True)

            # --- 斜杠命令优先处理 ---
            slash = self._detect_slash_command(user_text)
            if slash:
                cmd, args = slash
                consumed = self._handle_slash_command(cmd, args)
                if consumed:
                    return

            if self.sandbox is None:
                if self.on_error:
                    self.on_error(0, "请先选择或创建项目")
                return

            # 让 tool_executor 可以访问子代理引擎（用于分发 start_subagent / list_subagents 等工具）
            if not hasattr(self, "tool_exec") or self.tool_exec is None:
                # 懒初始化：确保至少有一个可调用的 tool_exec
                try:
                    from src.tools.tool_executor import ToolExecutor
                    self.tool_exec = ToolExecutor(
                        self.sandbox,
                        mcp_manager=getattr(self, "mcp_manager", None),
                        sklii_manager=getattr(self, "sklii_manager", None),
                        image_doc_manager=getattr(self, "image_doc_manager", None),
                        hook_manager=getattr(self, "hook_manager", None),
                        lsp_manager=getattr(self, "lsp_manager", None),
                    )
                    # 注入lsp_tools
                    lsp_mgr = getattr(self, "lsp_manager", None)
                    if lsp_mgr is not None:
                        try:
                            self.tool_exec.lsp_tools.set_lsp_manager(lsp_mgr)
                        except Exception:
                            logger.debug("_send_message_impl 注入 lsp_manager 异常", exc_info=True)
                except Exception:
                    self.tool_exec = None
            if self.tool_exec is None:
                if self.on_error:
                    self.on_error(0, "工具执行器初始化失败，无法继续对话")
                return

            try:
                # _chat_engine 是 ToolExecutor 运行时动态挂载的反向引用，非类声明属性
                self.tool_exec._chat_engine = self  # type: ignore[attr-defined]
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

            # --- 记录本轮图片 (用于 vision) + 存储到项目预览目录 ---
            # 保留调用方通过 _state 预设置的图片，除非显式传了 images 参数
            try:
                raw_images = list(images) if images is not None else None
            except Exception:
                raw_images = None
            with self._state_lock:
                if raw_images is not None:
                    self._state["images_for_message"] = list(raw_images)
                # else: 保留 _state 中已有的 images_for_message（由调用方预设置）
            # 存储到 project/.dscode/images/ 让模型后续可通过 view_image 工具重新查看
            image_doc_manager = getattr(self, "image_doc_manager", None)
            if raw_images and image_doc_manager is not None:
                for idx, data_url in enumerate(raw_images):
                    try:
                        image_doc_manager.store_from_data_url(
                            data_url, source_doc=f"chat_upload_{idx+1}"
                        )
                    except Exception as _exc:
                        logger.debug("跳过异常项: %s", _exc, exc_info=True)
                        continue

            # --- 用户上传文件 ---
            uploaded_info = ""
            if uploaded_files:
                names = []
                for f in uploaded_files:
                    try:
                        n = self.sandbox.add_preview_file(f)
                        if n:
                            names.append(n)
                    except Exception as _exc:
                        logger.debug("跳过异常项: %s", _exc, exc_info=True)
                        continue
                if names:
                    uploaded_info = f"[已上传文件: {', '.join(names)}]\n"

            # --- 保存用户消息到 history ---
            full_user_text = (uploaded_info + user_text).strip()
            # 纯图片消息时，提供默认文本
            if not full_user_text and raw_images:
                full_user_text = "[用户发送了图片]"

            # --- @文件/@符号 上下文自动注入 ---
            mention_context = self._inject_at_mentions(full_user_text)
            if mention_context:
                full_user_text = mention_context + "\n\n" + full_user_text

            # 注入UserSubmit钩子产生的上下文
            if hook_context:
                if full_user_text:
                    full_user_text = full_user_text + "\n\n[钩子上下文]\n" + hook_context
                else:
                    full_user_text = "[钩子上下文]\n" + hook_context
            # 注入Skills技能执行提示
            pending_skill = getattr(self, "_pending_skill_prompt", None)
            if pending_skill:
                if full_user_text:
                    full_user_text = full_user_text + "\n\n" + pending_skill
                else:
                    full_user_text = pending_skill
                self._pending_skill_prompt = None  # 一次性使用
            if not full_user_text:
                if self.on_error:
                    self.on_error(0, "消息内容为空")
                return
            if self.history is None:
                if self.on_error:
                    self.on_error(0, "历史记录未初始化")
                return
            try:
                self.history.add_user_message(full_user_text)
            except Exception as e:
                print(f"[ChatEngine] history.add_user_message 失败: {e}")
                if self.on_error:
                    self.on_error(0, f"保存用户消息失败: {e}")
                return

            # 会话短时记忆：记录用户消息并立即 checkpoint
            try:
                if self.session_memory is not None:
                    self.session_memory.add_chat("user", full_user_text)
                    self.session_memory.save_checkpoint()
            except Exception as _exc:
                logger.debug("记录会话用户消息失败: %s", _exc, exc_info=True)

            # 用户画像记忆：从用户消息中抽取偏好
            try:
                if self.user_profile_memory is not None:
                    updated_fields = self.user_profile_memory.update_from_message(full_user_text)
                    if updated_fields and self.on_status_update:
                        self.on_status_update(f"用户画像已更新: {', '.join(updated_fields)}")
            except Exception as _exc:
                logger.debug("更新用户画像失败: %s", _exc, exc_info=True)

            # 长期全局记忆：从用户消息中抽取报错/踩坑/解决经验
            try:
                self._maybe_extract_long_term(full_user_text)
            except Exception as _e:
                logger.debug("长期记忆提取失败: %s", _e, exc_info=True)

            # 自动记忆：从用户消息中提取长期偏好/项目事实
            try:
                if self.auto_memory is not None:
                    added = self.auto_memory.remember(full_user_text)
                    if added and self.on_status_update:
                        kinds = ", ".join({a.get("kind", "memory") for a in added})
                        self.on_status_update(f"自动记忆已更新 ({kinds})")
            except Exception as e:
                print(f"[ChatEngine] 自动记忆更新失败: {e}")

            # --- 路由: Ultra 模式（规划→执行→验证→迭代→交付）---
            if self.get_mode() == "ultra":
                with self._state_lock:
                    self._state["phase"] = "ultra_executing"
                    self._state["plan_confirmed"] = True
                self._run_ultra_loop(full_user_text)
                return

            # --- 路由: Planning 阶段 vs Execution 阶段 ---
            # 关键: planning 阶段只跑 1 轮且禁止工具调用（只读）
            with self._state_lock:
                is_planning = (self._state["phase"] == "planning" and not self._state["plan_confirmed"])

            if is_planning:
                # --- Phase 1: Planning（一轮，只读，不执行工具）---
                if self.on_status_update:
                    self.on_status_update("正在生成执行计划...")
                self._run_single_turn(
                    turn_counter=1,
                    allow_tools=False,        # 禁止工具调用（除 ask_user_questions）
                    phase_label="PLANNING",
                    is_final_if_no_tools=True,
                )
                return

            # --- Phase 2: Execution（用户已确认计划）---
            # 进入执行前自动创建 pre-execution 快照（一次）
            with self._state_lock:
                _auto_pre_snapshot = bool(self._state.get("auto_pre_snapshot"))
            if _auto_pre_snapshot and self.sandbox is not None:
                try:
                    snap = self.sandbox.snapshot("pre-execution-auto")
                    if self.on_status_update:
                        self.on_status_update(
                            f"已创建前置快照: {snap.get('name')} (id={snap.get('id','?')[:8]}...)"
                        )
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                with self._state_lock:
                    self._state["auto_pre_snapshot"] = False  # 仅在首个消息创建一次

            if self.on_status_update:
                self.on_status_update("执行中...")

            # --- 原有工具调用循环 (最多 100 轮，由模型自己决定何时停止) ---
            execution_done = False
            for turn in range(1, 100):
                if self._is_stopped():
                    break

                # 重置子代理启动标记
                with self._state_lock:
                    self._state["subagents_launched"] = False

                finished = self._run_single_turn(
                    turn_counter=turn,
                    allow_tools=True,
                    phase_label=f"EXEC-ROUND-{turn}",
                    is_final_if_no_tools=True,
                )
                # 本轮无工具调用时: _run_single_turn 返回 True -> 对话结束
                if finished:
                    execution_done = True
                    break

                # 检测是否启动了子代理 — 暂停主代理，等待子代理完成
                with self._state_lock:
                    _subagents_launched = bool(self._state.get("subagents_launched"))
                if _subagents_launched and self._subagent is not None:
                    if self.on_status_update:
                        self.on_status_update("等待子代理完成...")
                    # 等待所有子代理完成（C-3: 传入 stop_check 以响应 stop()）
                    self._subagent.wait_for_all(timeout=600, stop_check=self._is_stopped)
                    # 将子代理结果作为工具调用结果注入历史
                    results = self._subagent.get_results()
                    if self.history is not None and results:
                        self.history.add_tool_result("subagent_results", results)
                    if self.on_tool_result:
                        self.on_tool_result("subagent_results", results)
                    # 继续循环，让主代理审查子代理结果
                    continue

            # --- 执行结束后的统一处理 ---
            # H-1/H-2 修复: 确保所有终止路径都调用 _emit_finished
            _finished_emitted = False
            if execution_done:
                # 工具循环自然结束（最终回答已由 _run_single_turn 发出），
                # _run_single_turn 内部已调用 _emit_finished
                _finished_emitted = True
            elif self._is_stopped():
                # 被用户中断：stop() 已经归档为 stopped，这里只触发 UI 完成回调
                self._emit_finished(archive=False)
                _finished_emitted = True
            else:
                # 达到最大轮数或 history 为 None：生成最终总结
                if self.history is None:
                    if self.on_error:
                        self.on_error(0, "历史记录未初始化")
                    self._emit_finished(archive=False)
                    _finished_emitted = True
                else:
                    if self.on_new_turn:
                        self.on_new_turn(99)  # 总结轮
                    system_prompt = self._build_system_prompt()
                    api_messages = self.history.to_api_messages(
                        system_prompt, language=self.cfg.get("language", "zh") or "zh"
                    )
                    try:
                        usage = self._stream_chat(api_messages)
                        content = usage.get("content", "")
                        reasoning = usage.get("reasoning_content", "")
                        # 被取消或已停止时不追加总结轮助手消息
                        if not usage.get("cancelled") and not self._is_stopped():
                            if self.on_assistant_message:
                                self.on_assistant_message(content, reasoning, [])
                            if self.history is not None:
                                self.history.add_assistant_message(content, reasoning)
                            # 会话短时记忆：记录总结轮助手消息并 checkpoint
                            try:
                                if self.session_memory is not None:
                                    self.session_memory.add_chat("assistant", content)
                                    if reasoning:
                                        self.session_memory.add_chat("reasoning", reasoning[:2000])
                                    self.session_memory.save_checkpoint()
                            except Exception as _exc:
                                logger.debug("记录总结轮会话消息失败: %s", _exc, exc_info=True)
                        self._accumulate_usage(usage)
                        # 停止时stop()已经归档过，避免重复归档
                        if self._is_stopped():
                            self._emit_finished(archive=False)
                        else:
                            self._emit_finished()
                        _finished_emitted = True
                        # Bug 2修复: _emit_finished 之后的代码（如 _refresh_status_bar）
                        # 如果抛异常，不应导致外层 catch-all 再次调用 _emit_finished。
                        # 将后续操作包在独立 try/except 中，只记录日志。
                        try:
                            self._refresh_status_bar(usage)
                        except Exception as _exc:
                            logger.debug("刷新状态栏失败（已完成收尾，不影响结果）: %s", _exc, exc_info=True)
                    except APIError as e:
                        # M-6 修复: 总结轮 APIError 也要调用 _emit_finished
                        self._archive_session_memory("error", error_message=e.message)
                        if self.on_error:
                            self.on_error(e.code, e.message)
                        self._emit_finished(archive=False)
                        _finished_emitted = True

            # C-1 修复: plan 模式执行完成后重置 phase 为 planning，
            # 让下一轮用户消息重新生成计划（替代死代码 plan_iterate 重置逻辑）
            with self._state_lock:
                _mode = self._state.get("mode")
            if _mode == "plan":
                try:
                    with self._state_lock:
                        self._state["phase"] = "planning"
                        self._state["plan_confirmed"] = False
                        self._state["pending_plan"] = None
                    if self.on_status_update:
                        self.on_status_update("执行完成，可以开始新任务")
                except Exception as _exc:
                    logger.debug("重置 plan 模式 phase 失败: %s", _exc, exc_info=True)
        except Exception as _exc:
            # H-3 修复: 捕获执行过程中未处理的异常，确保 UI 收到完成通知
            logger.exception("聊天执行过程中未处理的异常: %s", _exc)
            self._archive_session_memory("error", error_message=str(_exc))
            if self.on_error:
                try:
                    self.on_error(0, f"执行异常: {_exc}")
                except Exception:
                    logger.debug("_send_message_impl on_error 回调异常", exc_info=True)
            # Bug 2修复: 仅在未正常 emit finished 时才调用，避免重复归档/回调
            if not locals().get("_finished_emitted"):
                self._emit_finished(archive=False)
        finally:
            # Bug 1修复: 统一清理看门狗线程，覆盖所有路径（提前return、异常、正常完成）
            turn_watchdog_stop.set()
            if turn_watchdog_thread is not None and turn_watchdog_thread.is_alive():
                try:
                    turn_watchdog_thread.join(timeout=1.0)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    # ========== 内部方法 ==========
    def _wait_for_user_answer(self, payload: Dict) -> str:
        """调用 GUI 弹窗，等待用户回答 ask_user_questions。委托给 ConfirmationGate"""
        return self._gate.wait_for_user_answer(payload, self.on_ask_user_questions)

    # 保留: event-based 命令确认路径（当前未使用，由 _on_confirm_command_engine → ConfirmationGate.request_command_confirm 驱动）
    def _wait_for_command_confirmation(self, call: Dict) -> bool:
        """阻塞等待 GUI 弹窗让用户确认执行命令。委托给 ConfirmationGate"""
        return self._gate.wait_for_command_confirmation(call, self.on_confirm_command)

    def _clear_images_for_message(self) -> None:
        """清空本轮消息携带的图片，避免在多轮工具调用 / 深度循环中重复发送。"""
        try:
            with self._state_lock:
                self._state["images_for_message"] = []
        except Exception as _exc:
            logger.debug("清空图片状态失败: %s", _exc, exc_info=True)

    def _gen_id(self) -> str:
        """生成简单唯一 ID"""
        return f"sa_{int(time.time() * 1000) & 0xFFFFFFF:x}"

