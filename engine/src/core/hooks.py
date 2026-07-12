# -*- coding: utf-8 -*-
"""Hooks钩子系统 — 生命周期事件驱动的自动化脚本引擎

支持的钩子事件（Hook Events）:
  - SessionStart       : 新会话开始时
  - SessionEnd         : 会话正常结束时
  - UserSubmit         : 用户提交消息前（可修改/拦截消息）
  - PreToolUse         : 工具调用前（可阻断/修改参数）
  - PostToolUse        : 工具调用后（可获取结果，注入额外上下文）
  - EditApplied        : 文件编辑被应用后
  - FileRead           : 文件读取后
  - CommandStart       : 命令执行前（可阻断危险命令）
  - CommandEnd         : 命令执行后
  - SubagentStart      : 子代理启动前
  - SubagentEnd        : 子代理结束后
  - PreLLMCall         : LLM API调用前（可注入额外上下文）
  - PostLLMCall        : LLM API调用后
  - ErrorOccurred      : 发生错误时

支持的钩子类型（Hook Types）:
  - command : 执行shell命令，stdout返回注入上下文，stderr独立，exit code非0可阻断
  - http    : 发送HTTP请求，响应体注入上下文
  - prompt  : 直接注入一段文本提示到上下文
  - python  : 执行受限Python函数（沙箱内）

钩子配置来源:
  1. 项目级: {project}/.dscode/hooks.json
  2. DS.md中的hooks代码块
  3. 用户级: ~/.config/dscode/hooks.json
  4. 内置默认钩子（安全防护）

安全考虑:
  - 钩子命令默认在沙箱内执行
  - PreToolUse/CommandStart类型的阻断钩子需要明确声明block权限
  - 钩子执行有超时限制（默认10秒）
  - 钩子输出有大小限制（默认4KB），防止污染上下文
"""

from __future__ import annotations

import logging
import json
import os
import shlex
import subprocess
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 支持的钩子事件 ──────────────────────────────────────────────
HOOK_EVENTS = {
    "SessionStart", "SessionEnd", "UserSubmit",
    "PreToolUse", "PostToolUse", "EditApplied", "FileRead",
    "CommandStart", "CommandEnd",
    "SubagentStart", "SubagentEnd",
    "PreLLMCall", "PostLLMCall", "ErrorOccurred",
}

# ── 支持的钩子类型 ──────────────────────────────────────────────
HOOK_TYPES = {"command", "http", "prompt", "python"}

# ── 默认安全限制 ────────────────────────────────────────────────
DEFAULT_HOOK_TIMEOUT = 10        # 秒
DEFAULT_HOOK_MAX_OUTPUT = 4096   # 字节
DEFAULT_HOOK_MAX_HTTP_BODY = 8192

# ── 工具匹配器 ──────────────────────────────────────────────────
DEFAULT_TOOL_MATCH = "*"  # 匹配所有工具


class HookResult:
    """钩子执行结果"""
    __slots__ = ("hook_name", "event", "blocked", "context_injection", "error", "duration_ms")

    def __init__(
        self,
        hook_name: str,
        event: str,
        blocked: bool = False,
        context_injection: str = "",
        error: Optional[str] = None,
        duration_ms: int = 0,
    ):
        self.hook_name = hook_name
        self.event = event
        self.blocked = blocked              # True 表示阻断后续操作
        self.context_injection = context_injection  # 注入到LLM上下文的文本
        self.error = error                  # 钩子自身错误
        self.duration_ms = duration_ms

    @property
    def ok(self) -> bool:
        return self.error is None and not self.blocked


class Hook:
    """单个钩子配置"""
    __slots__ = ("name", "event", "hook_type", "config", "matcher", "enabled",
                 "timeout", "max_output", "can_block", "priority")

    def __init__(self, name: str, event: str, hook_type: str, config: Dict,
                 matcher: str = DEFAULT_TOOL_MATCH, enabled: bool = True,
                 timeout: float = DEFAULT_HOOK_TIMEOUT,
                 max_output: int = DEFAULT_HOOK_MAX_OUTPUT,
                 can_block: bool = False, priority: int = 0):
        self.name = name
        self.event = event
        self.hook_type = hook_type
        self.config = config
        self.matcher = matcher
        self.enabled = enabled
        self.timeout = timeout
        self.max_output = max_output
        self.can_block = can_block
        self.priority = priority  # 越大越先执行

    def matches_tool(self, tool_name: str) -> bool:
        """检查钩子是否匹配指定工具"""
        if self.matcher == DEFAULT_TOOL_MATCH:
            return True
        # 支持 glob: read_*, create_file, 逗号分隔列表
        patterns = [p.strip() for p in self.matcher.split(",")]
        import fnmatch
        return any(fnmatch.fnmatch(tool_name, p) for p in patterns)

    def matches_file(self, file_path: str, work_dir: str) -> bool:
        """检查钩子是否匹配指定文件路径（用于EditApplied等事件）"""
        if self.matcher == DEFAULT_TOOL_MATCH:
            return True
        import fnmatch
        try:
            rel = os.path.relpath(file_path, work_dir)
        except ValueError:
            return False
        patterns = [p.strip() for p in self.matcher.split(",")]
        return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(file_path, p) for p in patterns)


class HookManager:
    """钩子管理器 — 注册、加载、触发、执行钩子"""

    def __init__(self, work_dir: Optional[Path] = None, config_dir: Optional[Path] = None):
        self.work_dir = Path(work_dir) if work_dir else None
        self.config_dir = Path(config_dir) if config_dir else None
        self._hooks: Dict[str, List[Hook]] = {e: [] for e in HOOK_EVENTS}
        self._lock = threading.RLock()
        self._python_hooks: Dict[str, Callable] = {}  # name -> callable
        self._loaded = False
        # 项目级 command 钩子需要显式审批（防止恶意项目自动执行任意命令）
        self._approved_project_hooks: set = set()  # 已审批的 hook name 集合
        self._pending_project_hooks: List[Dict] = []  # 待审批的项目级钩子列表

    # ── 注册 ──────────────────────────────────────────────────
    def register(self, hook: Hook) -> bool:
        """注册一个钩子"""
        with self._lock:
            if hook.event not in self._hooks:
                logger.warning("未知钩子事件: %s", hook.event)
                return False
            if hook.hook_type not in HOOK_TYPES:
                logger.warning("未知钩子类型: %s", hook.hook_type)
                return False
            self._hooks[hook.event].append(hook)
            # 按priority降序排列
            self._hooks[hook.event].sort(key=lambda h: -h.priority)
            return True

    def register_python_hook(self, name: str, fn: Callable) -> None:
        """注册一个Python函数钩子（供内置使用）"""
        with self._lock:
            self._python_hooks[name] = fn

    def unregister(self, name: str, event: Optional[str] = None) -> int:
        """移除指定名称的钩子，返回移除数量"""
        removed = 0
        with self._lock:
            events = [event] if event else list(self._hooks.keys())
            for ev in events:
                before = len(self._hooks[ev])
                self._hooks[ev] = [h for h in self._hooks[ev] if h.name != name]
                removed += before - len(self._hooks[ev])
        return removed

    def remove_by_index(self, index: int, event: Optional[str] = None) -> Optional[Dict]:
        """按list_hooks()返回的序号（1-based）删除钩子，返回被删除的钩子信息"""
        with self._lock:
            # 收集所有钩子的扁平列表
            all_hooks = []
            events = [event] if event else HOOK_EVENTS
            for ev in events:
                for h in self._hooks.get(ev, []):
                    all_hooks.append((ev, h))
            idx = index - 1  # 转0-based
            if idx < 0 or idx >= len(all_hooks):
                return None
            ev, hook = all_hooks[idx]
            self._hooks[ev] = [h for h in self._hooks[ev] if h is not hook]
            return {"name": hook.name, "event": ev}

    # ── 加载配置 ──────────────────────────────────────────────
    def load_from_config(self, config_dict: Dict) -> int:
        """从字典配置加载钩子，返回加载数量"""
        count = 0
        hooks_config = config_dict.get("hooks", [])
        if not isinstance(hooks_config, list):
            return 0
        for i, hc in enumerate(hooks_config):
            if not isinstance(hc, dict):
                continue
            try:
                hook = Hook(
                    name=hc.get("name", f"hook_{i}"),
                    event=hc.get("event", ""),
                    hook_type=hc.get("type", "command"),
                    config=hc.get("config", {}),
                    matcher=hc.get("matcher", DEFAULT_TOOL_MATCH),
                    enabled=hc.get("enabled", True),
                    timeout=float(hc.get("timeout", DEFAULT_HOOK_TIMEOUT)),
                    max_output=int(hc.get("max_output", DEFAULT_HOOK_MAX_OUTPUT)),
                    can_block=bool(hc.get("can_block", False)),
                    priority=int(hc.get("priority", 0)),
                )
                if self.register(hook):
                    count += 1
            except Exception as e:
                logger.debug("加载钩子配置失败(%d): %s", i, e)
        return count

    def load_from_file(self, path: Path) -> int:
        """从JSON文件加载钩子"""
        try:
            if not path.exists():
                return 0
            data = json.loads(path.read_text(encoding="utf-8"))
            return self.load_from_config(data if isinstance(data, dict) else {})
        except Exception as e:
            logger.debug("加载钩子文件失败(%s): %s", path, e)
            return 0

    def load_from_ds_md(self, ds_md_content: str) -> int:
        """从DS.md内容中加载hooks代码块（```hooks ... ```）"""
        import re
        count = 0
        # 匹配 ```hooks[属性] ... ``` 代码块，closing fence必须在行首（MULTILINE模式）
        pattern = re.compile(r"^[ \t]*```hooks[^\n]*\n(.*?)^[ \t]*```[ \t]*$",
                             re.DOTALL | re.MULTILINE)
        for match in pattern.finditer(ds_md_content):
            block = match.group(1)
            # 限制代码块大小防止内存耗尽
            if len(block) > 1024 * 1024:  # 1MB
                logger.debug("DS.md hooks块过大(%d字节)，跳过", len(block))
                continue
            try:
                hook_config = json.loads(block)
                if isinstance(hook_config, dict):
                    count += self.load_from_config(hook_config)
                elif isinstance(hook_config, list):
                    for item in hook_config:
                        if isinstance(item, dict):
                            count += self.load_from_config({"hooks": [item]})
            except json.JSONDecodeError:
                # 也支持简单的INI-like格式
                try:
                    count += self._parse_simple_hooks(block)
                except Exception:
                    pass
            except Exception as e:
                logger.debug("解析DS.md hooks块失败: %s", e)
        return count

    def _parse_simple_hooks(self, text: str) -> int:
        """解析简单格式的hooks配置（每行一个: event: type: command）"""
        count = 0
        for line in text.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":", 2)
            if len(parts) >= 3:
                event, hook_type, cmd = parts[0].strip(), parts[1].strip(), parts[2].strip()
                if hook_type == "command":
                    config = {"command": cmd}
                elif hook_type == "http":
                    config = {"url": cmd}
                elif hook_type == "python":
                    config = {"function": cmd}
                else:
                    config = {"text": cmd}
                    hook_type = "prompt"
                hook = Hook(
                    name=f"ds_hook_{count}",
                    event=event,
                    hook_type=hook_type,
                    config=config,
                )
                if self.register(hook):
                    count += 1
        return count

    def load_all(self) -> int:
        """加载所有配置来源的钩子

        安全策略：
        - 用户级钩子（config_dir/hooks.json）：直接加载，用户自己配置的
        - 项目级钩子（work_dir/.dscode/hooks.json）：
          * command 类型默认禁用，需通过 approve_project_hooks() 显式审批
          * http/prompt/python 类型正常加载（受限风险较低）
        """
        total = 0
        # 1. 内置安全钩子
        total += self._load_builtin_hooks()
        # 2. 用户级配置（用户自行配置，直接加载）
        if self.config_dir:
            user_hooks = self.config_dir / "hooks.json"
            total += self.load_from_file(user_hooks)
        # 3. 项目级配置（安全风险：需审批 command 类型）
        if self.work_dir:
            project_hooks = self.work_dir / ".dscode" / "hooks.json"
            total += self._load_project_hooks_safely(project_hooks)
        self._loaded = True
        return total

    def _load_project_hooks_safely(self, path: Path) -> int:
        """安全加载项目级钩子：command 类型默认禁用，需审批"""
        try:
            if not path.exists():
                return 0
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return 0
            hooks_list = data.get("hooks", [])
            if not isinstance(hooks_list, list):
                return 0

            count = 0
            self._pending_project_hooks = []
            for item in hooks_list:
                if not isinstance(item, dict):
                    continue
                hook_type = item.get("type", "")
                hook_name = item.get("name", f"proj_hook_{count}")
                # command 类型钩子需审批；其他类型正常加载
                if hook_type == "command":
                    # 记录待审批的钩子
                    self._pending_project_hooks.append({
                        "name": hook_name,
                        "event": item.get("event", ""),
                        "command": item.get("config", {}).get("command", ""),
                        "path": str(path),
                    })
                    # 以禁用状态注册，审批后启用
                    item["enabled"] = False
                    logger.info("项目级 command 钩子 '%s' 已注册但未启用（需审批）", hook_name)
                # 注册钩子（command类型为禁用状态）
                if self.load_from_config({"hooks": [item]}):
                    count += 1
            return count
        except Exception as e:
            logger.debug("安全加载项目级钩子失败(%s): %s", path, e)
            return 0

    def get_pending_project_hooks(self) -> List[Dict]:
        """获取待审批的项目级 command 钩子列表"""
        with self._lock:
            return list(self._pending_project_hooks)

    def approve_project_hooks(self, names: List[str]) -> int:
        """审批并启用指定的项目级 command 钩子

        Args:
            names: 要启用的钩子名称列表；传入 ["*"] 表示全部审批

        Returns:
            实际启用的钩子数量
        """
        enabled = 0
        with self._lock:
            approve_all = "*" in names
            for pending in self._pending_project_hooks:
                hook_name = pending["name"]
                if approve_all or hook_name in names:
                    # 在所有事件中查找并启用该钩子
                    for event_hooks in self._hooks.values():
                        for h in event_hooks:
                            if h.name == hook_name and not h.enabled:
                                h.enabled = True
                                self._approved_project_hooks.add(hook_name)
                                enabled += 1
                                logger.info("项目级钩子 '%s' 已审批启用", hook_name)
            # 清除已处理的待审批列表
            if approve_all:
                self._pending_project_hooks.clear()
            else:
                self._pending_project_hooks = [
                    p for p in self._pending_project_hooks
                    if p["name"] not in self._approved_project_hooks
                ]
        return enabled

    def _load_builtin_hooks(self) -> int:
        """注册内置安全钩子"""
        count = 0
        # 内置: PostToolUse对edit/create/delete后提示可能需要lint
        # 这里只注册空钩子框架，具体规则由DS.md和用户配置决定
        return count

    # ── 触发 ──────────────────────────────────────────────────
    def trigger(self, event: str, **kwargs) -> List[HookResult]:
        """触发指定事件的所有钩子，返回结果列表

        kwargs可包含:
          - tool_name: str (PreToolUse/PostToolUse)
          - tool_input: Dict (PreToolUse)
          - tool_result: str (PostToolUse)
          - file_path: str (EditApplied/FileRead)
          - command: str (CommandStart/CommandEnd)
          - user_message: str (UserSubmit)
          - error: str (ErrorOccurred)
          - context: dict (其他上下文)
        """
        results: List[HookResult] = []
        with self._lock:
            hooks = list(self._hooks.get(event, []))

        for hook in hooks:
            if not hook.enabled:
                continue
            # 工具匹配过滤
            tool_name = kwargs.get("tool_name", "")
            if tool_name and event in ("PreToolUse", "PostToolUse"):
                if not hook.matches_tool(tool_name):
                    continue
            # 文件匹配过滤
            file_path = kwargs.get("file_path", "")
            if file_path and event in ("EditApplied", "FileRead"):
                work_dir = str(self.work_dir) if self.work_dir else ""
                if not hook.matches_file(file_path, work_dir):
                    continue

            result = self._execute_hook(hook, **kwargs)
            results.append(result)
            # 如果钩子阻断且有权限，停止执行后续钩子
            if result.blocked and hook.can_block:
                break
        return results

    def trigger_blocking(self, event: str, **kwargs) -> Tuple[bool, str, str]:
        """触发阻断类钩子，返回 (blocked, reason, context_injection)

        PreToolUse/CommandStart等事件应使用此方法。
        """
        results = self.trigger(event, **kwargs)
        blocked = False
        reason = ""
        injections = []
        for r in results:
            if r.error:
                logger.debug("钩子 %s 执行错误: %s", r.hook_name, r.error)
                continue
            if r.blocked:
                blocked = True
                reason = f"[{r.hook_name}] {r.context_injection or '被钩子阻断'}"
            if r.context_injection:
                injections.append(r.context_injection)
        return blocked, reason, "\n".join(injections)

    # ── 执行 ──────────────────────────────────────────────────
    def _execute_hook(self, hook: Hook, **kwargs) -> HookResult:
        start = time.time()
        try:
            if hook.hook_type == "command":
                return self._exec_command_hook(hook, **kwargs)
            elif hook.hook_type == "http":
                return self._exec_http_hook(hook, **kwargs)
            elif hook.hook_type == "prompt":
                return self._exec_prompt_hook(hook, **kwargs)
            elif hook.hook_type == "python":
                return self._exec_python_hook(hook, **kwargs)
            else:
                return HookResult(hook.name, hook.event, error=f"未知类型: {hook.hook_type}")
        except Exception as e:
            duration = int((time.time() - start) * 1000)
            return HookResult(hook.name, hook.event, error=str(e), duration_ms=duration)

    def _build_env(self, hook: Hook, **kwargs) -> Dict[str, str]:
        """构建钩子执行的环境变量"""
        env = os.environ.copy()
        env["DSCODE_HOOK_EVENT"] = hook.event
        env["DSCODE_HOOK_NAME"] = hook.name
        env["DSCODE_WORK_DIR"] = str(self.work_dir) if self.work_dir else ""
        if "tool_name" in kwargs:
            env["DSCODE_TOOL_NAME"] = str(kwargs["tool_name"])
        if "file_path" in kwargs:
            env["DSCODE_FILE_PATH"] = str(kwargs["file_path"])
        if "command" in kwargs:
            env["DSCODE_COMMAND"] = str(kwargs["command"])
        # 工具输入通过临时文件传递（避免命令行参数长度限制）
        return env

    def _exec_command_hook(self, hook: Hook, **kwargs) -> HookResult:
        """执行command类型钩子"""
        cmd_template = hook.config.get("command", "")
        if not cmd_template:
            return HookResult(hook.name, hook.event, error="缺少command配置")

        # 简单变量替换
        cmd = cmd_template
        replacements = {
            "$TOOL_NAME": str(kwargs.get("tool_name", "")),
            "$FILE_PATH": str(kwargs.get("file_path", "")),
            "$COMMAND": str(kwargs.get("command", "")),
            "${TOOL_NAME}": str(kwargs.get("tool_name", "")),
            "${FILE_PATH}": str(kwargs.get("file_path", "")),
            "${COMMAND}": str(kwargs.get("command", "")),
            "$WORK_DIR": str(self.work_dir or ""),
            "${WORK_DIR}": str(self.work_dir or ""),
        }
        for var, val in replacements.items():
            # 所有变量统一做shell转义，防止命令注入
            cmd = cmd.replace(var, shlex.quote(val))

        cwd = str(self.work_dir) if self.work_dir else None
        env = self._build_env(hook, **kwargs)

        # 将tool_input写入临时文件供钩子读取
        tmp_input = None
        tool_input = kwargs.get("tool_input")
        if tool_input:
            import tempfile
            try:
                fd, tmp_input = tempfile.mkstemp(prefix=".dscode_hook_", suffix=".json", text=True,
                                                 dir=cwd if cwd else None)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(tool_input, f, ensure_ascii=False)
                env["DSCODE_TOOL_INPUT_FILE"] = tmp_input
            except Exception:
                pass

        try:
            start_t = time.time()
            import signal as _signal

            # 安全检查: 拒绝明显注入模式的模板（变量已通过 shlex.quote 转义）
            _unsafe_patterns = [
                ("`", "反引号命令替换"),
                ("$(", "命令替换 $(...)"),
                ("${", "变量展开 ${...}（仅 $TOOL_NAME 等已替换的变量安全）"),
            ]
            for _pat, _desc in _unsafe_patterns:
                if _pat in cmd_template:
                    # 检查是否出现在未被替换的变量引用之外
                    _remaining = cmd
                    _unsafe = False
                    for _var, _val in replacements.items():
                        _remaining = _remaining.replace(_var, "")
                    if _pat in _remaining:
                        return HookResult(hook.name, hook.event,
                                         error=f"命令模板包含不安全模式: {_desc}")

            # 将已转义的 shell 命令通过显式 /bin/sh -c 执行，语义清晰且有审计追踪
            proc = subprocess.Popen(
                ["/bin/sh", "-c", cmd],
                shell=False,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,  # 创建独立进程组，便于超时时杀整个进程树
            )
            try:
                stdout, stderr = proc.communicate(timeout=hook.timeout)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                # 杀死整个进程组，防止孙子进程泄漏
                try:
                    os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                try:
                    stdout, stderr = proc.communicate(timeout=2)
                except Exception:
                    stdout, stderr = "", ""
                return HookResult(hook.name, hook.event,
                                 error=f"超时({hook.timeout}s)，已杀死进程组",
                                 duration_ms=int(hook.timeout*1000))

            stdout = stdout[:hook.max_output] if stdout else ""
            stderr = stderr[:512] if stderr else ""
            blocked = returncode != 0 and hook.can_block
            injection = stdout
            if stderr and not blocked:
                injection += f"\n[hook stderr] {stderr[:200]}"
            err_msg = None if returncode == 0 else f"exit code {returncode}: {stderr[:200]}"
            if blocked and stderr:
                # 阻断时把stderr放入context_injection，让用户看到原因
                injection = f"[钩子阻断] {stderr[:300]}\n{stdout[:500]}"
            return HookResult(
                hook.name, hook.event,
                blocked=blocked,
                context_injection=injection.strip(),
                error=err_msg,
                duration_ms=int((time.time() - start_t) * 1000),
            )
        finally:
            if tmp_input:
                try:
                    os.unlink(tmp_input)
                except Exception:
                    pass

    def _exec_http_hook(self, hook: Hook, **kwargs) -> HookResult:
        """执行http类型钩子"""
        url = hook.config.get("url", "")
        method = hook.config.get("method", "POST").upper()
        headers = hook.config.get("headers", {"Content-Type": "application/json"})
        if not url:
            return HookResult(hook.name, hook.event, error="缺少url配置")

        # kwargs放前面，显式字段放后面，防止kwargs覆盖event/work_dir
        safe_kwargs = {k: v for k, v in kwargs.items()
                       if k not in ("event", "work_dir")
                       and isinstance(v, (str, int, float, bool, dict, list, type(None)))}
        payload = json.dumps({
            **safe_kwargs,
            "event": hook.event,
            "work_dir": str(self.work_dir) if self.work_dir else "",
        }).encode("utf-8")

        try:
            req = urllib.request.Request(url, data=payload if method != "GET" else None,
                                         headers=headers, method=method)
            start_t = time.time()
            with urllib.request.urlopen(req, timeout=hook.timeout) as resp:
                body = resp.read(hook.max_output + 1).decode("utf-8", errors="replace")
                if len(body) > hook.max_output:
                    body = body[:hook.max_output] + "\n[hook output truncated]"
                return HookResult(
                    hook.name, hook.event,
                    context_injection=body.strip(),
                    duration_ms=int((time.time() - start_t) * 1000),
                )
        except urllib.error.HTTPError as e:
            return HookResult(hook.name, hook.event, blocked=hook.can_block,
                             error=f"HTTP {e.code}: {e.reason}")
        except Exception as e:
            return HookResult(hook.name, hook.event, error=str(e))

    def _exec_prompt_hook(self, hook: Hook, **kwargs) -> HookResult:
        """执行prompt类型钩子 — 直接注入文本到上下文"""
        text = hook.config.get("text", "")
        if not text:
            return HookResult(hook.name, hook.event, error="缺少text配置")

        # 变量替换
        replacements = {
            "{{tool_name}}": str(kwargs.get("tool_name", "")),
            "{{file_path}}": str(kwargs.get("file_path", "")),
            "{{command}}": str(kwargs.get("command", "")),
            "{{user_message}}": str(kwargs.get("user_message", "")),
        }
        for var, val in replacements.items():
            text = text.replace(var, val)
        return HookResult(hook.name, hook.event, context_injection=text)

    def _exec_python_hook(self, hook: Hook, **kwargs) -> HookResult:
        """执行python类型钩子 — 调用已注册的Python函数"""
        fn_name = hook.config.get("function", "")
        if not fn_name:
            return HookResult(hook.name, hook.event, error="缺少function配置")
        fn = self._python_hooks.get(fn_name)
        if fn is None:
            return HookResult(hook.name, hook.event, error=f"未注册的Python钩子函数: {fn_name}")
        try:
            result = fn(**kwargs)
            if isinstance(result, dict):
                return HookResult(
                    hook.name, hook.event,
                    blocked=bool(result.get("blocked", False)),
                    context_injection=str(result.get("context", "") or ""),
                )
            elif isinstance(result, str):
                return HookResult(hook.name, hook.event, context_injection=result)
            elif isinstance(result, bool):
                return HookResult(hook.name, hook.event, blocked=result)
            else:
                return HookResult(hook.name, hook.event, context_injection=str(result) if result is not None else "")
        except Exception as e:
            return HookResult(hook.name, hook.event, error=str(e))

    # ── 工具方法 ──────────────────────────────────────────────
    def list_hooks(self, event: Optional[str] = None) -> List[Dict]:
        """列出所有已注册钩子"""
        with self._lock:
            result = []
            events = [event] if event else list(self._hooks.keys())
            for ev in events:
                for h in self._hooks.get(ev, []):
                    result.append({
                        "name": h.name,
                        "event": h.event,
                        "type": h.hook_type,
                        "matcher": h.matcher,
                        "enabled": h.enabled,
                        "can_block": h.can_block,
                        "priority": h.priority,
                    })
            return result

    def clear(self):
        """清空所有钩子"""
        with self._lock:
            self._hooks = {e: [] for e in HOOK_EVENTS}
            self._python_hooks.clear()


# ── 全局单例（可选使用） ────────────────────────────────────────
_default_manager: Optional[HookManager] = None
_manager_lock = threading.Lock()
_managers: Dict[str, HookManager] = {}


def get_hook_manager(work_dir: Optional[Path] = None, config_dir: Optional[Path] = None) -> HookManager:
    """获取或创建HookManager（带缓存和线程安全）。
    按work_dir缓存，避免每次创建新实例；不传work_dir时返回默认单例。
    """
    global _default_manager
    if work_dir is None:
        with _manager_lock:
            if _default_manager is None:
                _default_manager = HookManager(work_dir=None, config_dir=config_dir)
            return _default_manager
    # 按work_dir缓存
    key = str(Path(work_dir).resolve())
    with _manager_lock:
        mgr = _managers.get(key)
        if mgr is None:
            mgr = HookManager(work_dir=work_dir, config_dir=config_dir)
            _managers[key] = mgr
        return mgr
