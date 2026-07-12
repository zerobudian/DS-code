# -*- coding: utf-8 -*-
"""sklii —— 轻量的自定义工具包 (.sklii)

一个 .sklii 文件就是一个 JSON，格式如下:

{
  "version": 1,
  "name": "tool-package-name",
  "display_name": "...",
  "description": "...",
  "tools": [
    {
      "name": "hello",
      "args": {...},
      "run": {
        "type": "python|shell|http|mcp",
        ...
      }
    }
  ]
}

=== 安全策略（默认只允许 http / mcp）===
  - 创建 / 导入 .sklii 文件时：严格拒绝 python / shell 类型工具
  - 历史遗留 / 手工编辑的 python 工具文件：若被调用，在受限
    exec 沙箱中运行 —— __builtins__ 显式覆盖为白名单；
    禁止 __import__ / open / subprocess 等，并做关键字二次扫描。
  - shell 类型工具走 subprocess.run shell=False + 白名单命令 + 输入长度限制。
  - http 类型工具复用 NetworkTools 的 SSRF 防御。
  - _interpolate_dict 深度上限：防止 JSON 递归展开栈溢出。
"""

from __future__ import annotations

import json
import logging
import ast
import os
import re
import shlex
import subprocess
import threading
from src.core.dns_pinning import set_dns_pin, clear_dns_pin, ensure_dns_patch, get_dns_pin_ip
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.core.dns_timeout import getaddrinfo_with_timeout

logger = logging.getLogger(__name__)

SKLII_VERSION = 1

# ======== 安全硬限制 ========
_MAX_INTERPOLATE_DEPTH = 32            # _interpolate_dict 最大递归深度
_MAX_SKLII_FILE_BYTES = 4 * 1024 * 1024  # 单个 .sklii 文件最大 4MB
_MAX_TOOL_COUNT = 256                   # 单个 .sklii 文件最多工具数
_MAX_PYTHON_CODE_BYTES = 64 * 1024      # python 代码最大字节
_MAX_SHELL_CMD_BYTES = 2048            # shell 命令最大字节
_MAX_HTTP_RESPONSE_BYTES = 4 * 1024 * 1024  # HTTP 响应最大 4MB
_MAX_PYTHON_EXEC_SECONDS = 10           # Python 代码执行超时

# shell 白名单（python/shell 类型默认禁止创建，仅允许导入时残留）
ALLOWED_SHELL_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "grep", "find", "wc", "sort", "uniq", "echo",
    "pwd", "which", "python3", "python", "node", "npm", "npx", "pip", "pip3",
    "git", "curl", "wget", "unzip", "tar", "mkdir", "cp", "mv", "touch", "chmod",
    "diff", "patch", "env", "printenv", "id", "df", "du", "ps", "top",
})

# Python run 关键字黑名单（exec 已受限，这里做二次扫描）
_PYTHON_BLACKLIST_TOKENS = (
    "__import__", "__builtins__", "__loader__", "__spec__", "__file__",
    "subprocess", "os.system", "os.exec", "os.spawn", "os.fork",
    "eval(", "exec(", "execfile", "compile(", "getattr(", "setattr(",
    "delattr(", "globals(", "locals(", "vars(",
    "open(", "input(", "raw_input",
    "importlib", "builtins", "ctypes", "marshal", "pickle",
    "executemany", "execute",
    "__class__", "__bases__", "__subclasses__", "__mro__",
    "exit(", "quit(", "os._exit",
)


@dataclass
class SklIIITool:
    name: str
    description: str
    run_type: str
    run_config: Dict[str, Any]
    args_schema: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SklIIITool":
        rc = raw.get("run") or {}
        return cls(
            name=str(raw.get("name", "")),
            description=str(raw.get("description", "")),
            run_type=str(rc.get("type", "python")).lower(),
            run_config=rc,
            args_schema=raw.get("args") or {},
        )


@dataclass
class SklIIIPackage:
    path: str
    name: str
    display_name: str
    description: str
    author: str
    tools: List[SklIIITool] = field(default_factory=list)
    enabled: bool = True

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], path: str) -> "SklIIIPackage":
        tools_raw = raw.get("tools") or []
        tools = []
        for t in tools_raw:
            try:
                tools.append(SklIIITool.from_dict(t))
            except Exception as e:
                logger.warning("加载工具失败 %s: %s", t.get("name"), e)
        return cls(
            path=path,
            name=str(raw.get("name", os.path.splitext(os.path.basename(path))[0])),
            display_name=str(raw.get("display_name") or raw.get("name", "")),
            description=str(raw.get("description", "")),
            author=str(raw.get("author", "")),
            tools=tools,
            enabled=bool(raw.get("enabled", True)),
        )


_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,63}$")


def _is_valid_name(name: str) -> bool:
    if not isinstance(name, str):
        return False
    return bool(_NAME_RE.match(name))


_INTERP_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _interpolate(template: str, args: Dict[str, Any]) -> str:
    """把 {{key}} 替换成 args[key] 的字符串形式。长度上限由调用方控制。"""
    if not isinstance(template, str):
        return template
    if len(template) > 65536:
        return template[:65536]

    def repl(m):
        key = m.group(1)
        if key in args:
            val = args[key]
            if isinstance(val, (dict, list)):
                try:
                    return str(json.dumps(val, ensure_ascii=False))[:1024]
                except Exception:
                    return ""
            return str(val)[:1024]
        return m.group(0)

    try:
        out = template
        for _ in range(3):  # 最多迭代 3 次，防止嵌套污染
            new = _INTERP_RE.sub(repl, out)
            if new == out:
                break
            out = new
        return out
    except Exception:
        return template


def _interpolate_dict(d: Dict[str, Any], args: Dict[str, Any], depth: int = 0) -> Dict[str, Any]:
    """递归插值字典值。保护：最大深度、最大 dict 元素数。"""
    if depth > _MAX_INTERPOLATE_DEPTH:
        return {}
    if not isinstance(d, dict):
        return d
    if len(d) > 1024:
        return {}
    out: Dict[str, Any] = {}
    for k, v in d.items():
        try:
            key = str(k)[:256]
        except Exception as _exc:
            logger.debug("跳过异常项: %s", _exc, exc_info=True)
            continue
        if isinstance(v, str):
            out[key] = _interpolate(v, args)
        elif isinstance(v, dict):
            out[key] = _interpolate_dict(v, args, depth + 1)
        elif isinstance(v, list):
            if len(v) > 1024:
                v = v[:1024]
            out[key] = _interpolate_list(v, args, depth + 1)
        else:
            out[key] = v
    return out


def _interpolate_list(lst: List[Any], args: Dict[str, Any], depth: int = 0) -> List[Any]:
    """递归插值列表值，处理嵌套列表和字典。保护：最大深度、最大 list 元素数。"""
    if depth > _MAX_INTERPOLATE_DEPTH:
        return []
    result: List[Any] = []
    for x in lst:
        if isinstance(x, str):
            result.append(_interpolate(x, args))
        elif isinstance(x, dict):
            result.append(_interpolate_dict(x, args, depth + 1))
        elif isinstance(x, list):
            if len(x) > 1024:
                x = x[:1024]
            result.append(_interpolate_list(x, args, depth + 1))
        else:
            result.append(x)
    return result


def _read_json_like(path: str) -> Optional[Dict[str, Any]]:
    """读取 .sklii：先尝试 JSON 解析；失败则尝试去除前后可能的 ```code fence。

    P0-002 修复: 添加 JSON 嵌套深度检查, 防止恶意深度嵌套耗尽内存。
    """
    try:
        size = os.path.getsize(path)
        if size > _MAX_SKLII_FILE_BYTES:
            logger.warning("sklii 文件过大 %s: %d bytes", path, size)
            return None
    except Exception:
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return None

    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3]
        first_nl = text.find("\n")
        if 0 <= first_nl < 80:
            head = text[:first_nl].strip()
            if head and all(c.isalnum() or c in "-_+" for c in head):
                text = text[first_nl + 1:]

    text = text.lstrip()
    if not text or text[0] != "{":
        return None

    # P0-002 修复: 使用带深度限制的自定义 JSONDecoder,
    # 防止恶意深度嵌套耗尽栈空间和内存
    _MAX_JSON_DEPTH = 64
    try:
        # 先用标准 loads 解析 (已有 RecursionError 保护),
        # 同时用 raw_decode 检查最大嵌套深度
        decoder = json.JSONDecoder(parse_int=int, parse_float=float)
        raw_result, _ = decoder.raw_decode(text)

        def _check_depth(obj, depth=0):
            if depth > _MAX_JSON_DEPTH:
                raise ValueError(f"JSON 嵌套深度超过 {_MAX_JSON_DEPTH}")
            if isinstance(obj, dict):
                for v in obj.values():
                    _check_depth(v, depth + 1)
            elif isinstance(obj, list):
                for v in obj:
                    _check_depth(v, depth + 1)

        _check_depth(raw_result)
        return raw_result
    except RecursionError:
        logger.warning("sklii JSON 过深: %s", path)
        return None
    except ValueError as e:
        logger.warning("sklii JSON 深度超限 %s: %s", path, e)
        return None
    except Exception as e:
        logger.warning("sklii JSON 解析失败 %s: %s", path, e)
        return None


class SklIIIManager:
    """加载、注册、调用 .sklii 文件。"""

    def __init__(self, work_dir: str, project_dir: str,
                 mcp_manager: Any = None,
                 on_packages_changed: Optional[Callable[[], None]] = None) -> None:
        self._work_dir = os.path.abspath(work_dir or os.getcwd())
        self._project_dir = os.path.abspath(project_dir or self._work_dir)
        self._sklii_dir = os.path.join(self._project_dir, ".dscode", "sklii")
        self._mcp_manager = mcp_manager
        self._on_packages_changed = on_packages_changed
        self._packages: Dict[str, SklIIIPackage] = {}
        self._lock = threading.RLock()
        self._http_lock = threading.Lock()
        try:
            os.makedirs(self._sklii_dir, exist_ok=True)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        self.load_all()

    @property
    def sklii_dir(self) -> str:
        return self._sklii_dir

    def package_path(self, name: str) -> str:
        return os.path.join(self._sklii_dir, f"{name}.sklii")

    def load_all(self) -> int:
        """扫描并加载目录下所有 .sklii 文件。

        仅允许 http / mcp 类型；python / shell 类型会被忽略（导入阶段已拒绝，
        此处作为运行时纵深防御再次校验）。
        """
        count = 0
        if not os.path.isdir(self._sklii_dir):
            return 0
        for fn in sorted(os.listdir(self._sklii_dir)):
            if not fn.lower().endswith(".sklii"):
                continue
            try:
                pkg = self._load_file(os.path.join(self._sklii_dir, fn))
                if pkg is None:
                    continue
                # 运行时安全校验：仅允许 http / mcp
                if any(t.run_type not in ("http", "mcp") for t in pkg.tools):
                    logger.warning("忽略 %s: 包含非 http/mcp 的 run.type", fn)
                    continue
                self._packages[pkg.name] = pkg
                count += 1
            except Exception as e:
                logger.warning("加载 %s 失败: %s", fn, e)
        logger.info("已加载 %d 个 sklii 包", count)
        return count

    def _load_file(self, path: str) -> Optional[SklIIIPackage]:
        raw = _read_json_like(path)
        if raw is None:
            return None
        pkg = SklIIIPackage.from_dict(raw, path)
        if not pkg.name or not _is_valid_name(pkg.name):
            raise ValueError(f"sklii 文件缺少合法 name 字段: {path}")
        if not pkg.tools:
            raise ValueError(f"sklii 文件没有定义任何 tools: {path}")
        if len(pkg.tools) > _MAX_TOOL_COUNT:
            raise ValueError(f"sklii 工具数超过上限: {len(pkg.tools)}")
        for t in pkg.tools:
            if not _is_valid_name(t.name):
                raise ValueError(f"sklii 工具名非法: {t.name}")
            if t.run_type not in ("python", "shell", "http", "mcp"):
                raise ValueError(f"未知 run.type: {t.run_type}")
        return pkg

    def import_package(self, source_path: str) -> Dict[str, Any]:
        """从外部路径复制 .sklii 到项目目录并加载。

        安全策略：
        - 对 run.type 进行硬白名单校验；若工具包含 python / shell 类型则拒绝
          导入。http 与 mcp 是当前唯一允许的面向外部导入的工具类型。
        - 同时对所有工具名进行合法性校验；超过上限同样拒绝。
        """
        try:
            src = os.path.abspath(source_path)
            if not os.path.isfile(src):
                return {"ok": False, "error": f"文件不存在: {src}"}
            # 文件大小硬上限，避免极端大文件读入内存
            try:
                sz = os.path.getsize(src)
                if sz > 2 * 1024 * 1024:  # 2MB
                    return {"ok": False, "error": f"sklii 文件过大 ({sz} bytes)"}
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            pkg = self._load_file(src)
            if pkg is None:
                return {"ok": False, "error": "sklii 文件解析失败"}
            # —— 导入时二次校验：仅允许 http / mcp ——
            for t in pkg.tools:
                if t.run_type not in ("http", "mcp"):
                    return {
                        "ok": False,
                        "error": (f"导入失败: 工具 {t.name} 的 run.type={t.run_type} "
                                  f"不被允许，仅允许 http / mcp"),
                    }
            dest = self.package_path(pkg.name)
            with open(src, "r", encoding="utf-8") as f:
                content = f.read()
            with open(dest, "w", encoding="utf-8") as f:
                f.write(content)
            with self._lock:
                self._packages[pkg.name] = pkg
            self._notify_changed()
            return {
                "ok": True,
                "name": pkg.name,
                "display_name": pkg.display_name,
                "path": dest,
                "tools_count": len(pkg.tools),
                "message": f"已导入 {len(pkg.tools)} 个工具",
            }
        except Exception as e:
            logger.exception("导入 sklii 失败")
            return {"ok": False, "error": str(e)}

    def create_package(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """由模型或用户传入 data 创建新包。安全：禁止 python 和 shell 类型。"""
        try:
            name = str(data.get("name", "")).strip()
            if not _is_valid_name(name):
                return {"ok": False, "error": "name 只允许字母/数字/下划线 (长度 2-64)"}
            tools_raw = data.get("tools") or []
            if not isinstance(tools_raw, list) or not tools_raw:
                return {"ok": False, "error": "tools 必须是至少一个工具的数组"}
            if len(tools_raw) > _MAX_TOOL_COUNT:
                return {"ok": False, "error": f"单个 .sklii 最多 {_MAX_TOOL_COUNT} 个工具"}
            for t in tools_raw:
                if not _is_valid_name(t.get("name", "")):
                    return {"ok": False, "error": f"工具名非法: {t.get('name')}"}
                rc = t.get("run") or {}
                run_type = rc.get("type", "")
                if run_type not in ("http", "mcp"):
                    return {
                        "ok": False,
                        "error": f"不允许创建 run.type={run_type} 的 sklii 工具，仅允许 http 和 mcp",
                    }

            pkg_json = {
                "version": SKLII_VERSION,
                "name": name,
                "display_name": str(data.get("display_name") or name),
                "description": str(data.get("description", "")),
                "author": str(data.get("author", "AI")),
                "tools": list(tools_raw),
                "enabled": True,
            }

            path = self.package_path(name)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(pkg_json, f, ensure_ascii=False, indent=2)

            pkg = SklIIIPackage.from_dict(pkg_json, path)
            with self._lock:
                self._packages[pkg.name] = pkg
            self._notify_changed()
            return {
                "ok": True,
                "name": name,
                "path": path,
                "tools_count": len(pkg.tools),
            }
        except Exception as e:
            logger.exception("创建 sklii 失败")
            return {"ok": False, "error": str(e)}

    def remove_package(self, name: str) -> bool:
        with self._lock:
            if name not in self._packages:
                return False
            path = self._packages[name].path
            del self._packages[name]
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        self._notify_changed()
        return True

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            pkg = self._packages.get(name)
            if pkg is None:
                return False
            pkg.enabled = bool(enabled)
            try:
                with open(pkg.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["enabled"] = pkg.enabled
                with open(pkg.path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        self._notify_changed()
        return True

    def list_packages(self) -> List[Dict[str, Any]]:
        out = []
        with self._lock:
            for pkg in self._packages.values():
                out.append({
                    "name": pkg.name,
                    "display_name": pkg.display_name,
                    "description": pkg.description,
                    "author": pkg.author,
                    "enabled": pkg.enabled,
                    "path": pkg.path,
                    "tool_names": [t.name for t in pkg.tools],
                })
        return out

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        with self._lock:
            for pkg in self._packages.values():
                if not pkg.enabled:
                    continue
                for t in pkg.tools:
                    out.append({
                        "full_name": f"{pkg.name}.{t.name}",
                        "package": pkg.name,
                        "tool": t.name,
                        "description": t.description,
                        "args_schema": t.args_schema,
                        "run_type": t.run_type,
                    })
        return out

    def call_tool(self, full_tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        args = arguments or {}
        if "." not in full_tool_name:
            return {"ok": False, "error": f"工具名格式错误: {full_tool_name} (应为 package.tool)",
                    "content": ""}
        pkg_name, tool_name = full_tool_name.split(".", 1)
        with self._lock:
            pkg = self._packages.get(pkg_name)
        if pkg is None:
            return {"ok": False, "error": f"未找到 sklii 包: {pkg_name}", "content": ""}
        if not pkg.enabled:
            return {"ok": False, "error": f"sklii 包已禁用: {pkg_name}", "content": ""}
        tool = next((t for t in pkg.tools if t.name == tool_name), None)
        if tool is None:
            return {"ok": False, "error": f"包 {pkg_name} 未包含工具: {tool_name}", "content": ""}

        try:
            if tool.run_type == "python":
                return self._run_python(tool, args)
            elif tool.run_type == "shell":
                return self._run_shell(tool, args)
            elif tool.run_type == "http":
                return self._run_http(tool, args)
            elif tool.run_type == "mcp":
                return self._run_mcp(tool, args)
            else:
                return {"ok": False, "error": f"未知 run.type: {tool.run_type}", "content": ""}
        except Exception as e:
            logger.exception("调用 sklii 工具失败")
            return {"ok": False, "error": f"执行异常: {e}", "content": ""}

    # ======== run 类型实现 ========
    def _run_python(self, tool: SklIIITool, args: Dict[str, Any]) -> Dict[str, Any]:
        """【强受限】Python 代码执行：

        安全策略：
        1. AST 静态分析 + 关键字黑名单扫描 —— 命中则直接拒绝
        2. 代码长度上限；输出截断上限
        3. 在独立子进程中执行，设置硬超时并强制 kill，避免影响主进程
        4. 子进程内清空 builtins 并替换为白名单，阻断常见沙箱逃逸
        """
        import tempfile as _tmp
        import subprocess as _sp
        import ast as _ast

        code = str((tool.run_config.get("code")) or "")
        if not code.strip():
            return {"ok": False, "error": "python.run 缺少 code", "content": ""}
        if len(code) > _MAX_PYTHON_CODE_BYTES:
            return {"ok": False,
                    "error": f"python.run code 超过 {_MAX_PYTHON_CODE_BYTES} 字节",
                    "content": ""}
        if "\x00" in code:
            return {"ok": False, "error": "python.run 包含非法空字符", "content": ""}

        # 关键字二次扫描（防御简单字符串拼接攻击）
        lowered = code.lower()
        for tok in _PYTHON_BLACKLIST_TOKENS:
            if tok.lower() in lowered:
                return {"ok": False,
                        "error": f"python.run 含危险关键字: {tok}",
                        "content": ""}

        # AST 静态分析：拒绝 import / 危险调用 / dunder 属性访问等
        try:
            tree = _ast.parse(code, mode="exec")
        except SyntaxError as e:
            return {"ok": False, "error": f"python.run 语法错误: {e}", "content": ""}
        if not self._is_safe_python_ast(tree):
            return {"ok": False, "error": "python.run 代码包含危险操作（import / eval / exec / dunder 访问等）", "content": ""}

        # 在独立子进程中执行，带硬超时与 kill。
        # 使用受控 PATH 防止 PATH 劫持，并优先使用系统 python3。
        _safe_python = self._resolve_safe_python()
        if _safe_python is None:
            return {"ok": False, "error": "python.run 无法找到安全的 python3 路径", "content": ""}

        # 构造受限 builtins 包装代码：清空模块 builtins 并仅保留白名单
        _sce: Optional[Callable[[Optional[Dict[str, str]]], Dict[str, str]]]
        try:
            from src.tools.command_tools import _sanitize_command_env as _sce
        except Exception:
            def _sce(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
                return {}


        _BUILTIN_WRAPPER = '''import builtins
_R = {
    'abs': abs, 'all': all, 'any': any, 'ascii': ascii, 'bin': bin, 'bool': bool,
    'bytearray': bytearray, 'bytes': bytes, 'chr': chr, 'dict': dict, 'dir': dir,
    'divmod': divmod, 'enumerate': enumerate, 'filter': filter, 'float': float,
    'format': format, 'frozenset': frozenset, 'hash': hash, 'hex': hex, 'id': id,
    'int': int, 'isinstance': isinstance, 'issubclass': issubclass, 'iter': iter,
    'len': len, 'list': list, 'map': map, 'max': max, 'min': min, 'next': next,
    'oct': oct, 'ord': ord, 'pow': pow, 'print': print, 'range': range, 'repr': repr,
    'reversed': reversed, 'round': round, 'set': set, 'slice': slice, 'sorted': sorted,
    'str': str, 'sum': sum, 'tuple': tuple, 'type': type, 'zip': zip,
    'Exception': Exception, 'ValueError': ValueError, 'TypeError': TypeError,
    'RuntimeError': RuntimeError, 'AttributeError': AttributeError,
    'KeyError': KeyError, 'IndexError': IndexError, 'StopIteration': StopIteration,
}
builtins.__dict__.clear()
builtins.__dict__.update(_R)
__builtins__ = builtins
'''
        fd, script_path = _tmp.mkstemp(suffix=".py", dir=self._work_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(_BUILTIN_WRAPPER)
                f.write(code)
            try:
                env = _sce({"PWD": self._work_dir}) if _sce is not None else {}
                if env is None:
                    return {"ok": False, "error": "环境变量校验失败（存在非法注入变量）", "content": ""}
                env.update({
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1",
                    "PATH": "/usr/bin:/usr/local/bin:/bin:/usr/sbin:/sbin",
                })
                proc = _sp.run(
                    [_safe_python, "-S", script_path],
                    cwd=self._work_dir,
                    capture_output=True,
                    text=True,
                    timeout=_MAX_PYTHON_EXEC_SECONDS,
                    env=env,
                )
                text = proc.stdout or ""
                if proc.returncode != 0:
                    err = (proc.stderr or "执行失败")[:2000]
                    return {"ok": False, "error": f"Python 执行失败: {err}", "content": text[:2000]}
                return {"ok": True, "content": text[:8000]}
            except _sp.TimeoutExpired:
                return {"ok": False,
                        "error": f"python.run 执行超时 ({_MAX_PYTHON_EXEC_SECONDS}s)",
                        "content": ""}
            except Exception as e:
                return {"ok": False, "error": f"Python 执行异常: {e}", "content": ""}
        finally:
            try:
                os.unlink(script_path)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    @staticmethod
    def _is_safe_python_ast(tree: "ast.AST") -> bool:
        """AST 级安全检查：拒绝 import、危险调用、dunder 属性访问等。"""
        dangerous_builtins = {
            "eval", "exec", "compile", "getattr", "setattr", "delattr",
            "vars", "globals", "locals", "__import__", "open", "input",
            "exit", "quit", "breakpoint", "execfile",
        }
        dangerous_modules = {
            "os", "sys", "subprocess", "importlib", "builtins", "ctypes",
            "marshal", "pickle", "socket", "urllib", "http", "ftplib",
            "smtplib", "email", "shutil", "tempfile", "pathlib",
        }
        dangerous_attr_suffixes = ("__import__", "__builtins__", "__loader__",
                                   "__spec__", "__file__")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                return False
            if isinstance(node, ast.Attribute):
                attr = node.attr
                if isinstance(attr, str):
                    if attr.startswith("__") or attr in dangerous_attr_suffixes:
                        return False
                    # 拒绝 eval/exec/getattr/open 等危险方法调用
                    if attr in ("eval", "exec", "compile", "getattr", "setattr",
                                "delattr", "__import__", "open", "system", "popen",
                                "spawn", "fork", "kill", "remove", "unlink", "rmdir"):
                        return False
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in dangerous_builtins:
                    return False
                if isinstance(func, ast.Attribute) and isinstance(func.attr, str):
                    if func.attr in ("eval", "exec", "compile", "getattr", "setattr",
                                     "delattr", "__import__", "open", "system", "popen",
                                     "spawn", "fork"):
                        return False
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id in dangerous_modules or node.id == "__builtins__":
                    return False
        return True

    def _run_shell(self, tool: SklIIITool, args: Dict[str, Any]) -> Dict[str, Any]:
        command_tpl = str((tool.run_config.get("command")) or "")
        if not command_tpl.strip():
            return {"ok": False, "error": "shell.run 缺少 command", "content": ""}
        command = _interpolate(command_tpl, args)
        if len(command) > _MAX_SHELL_CMD_BYTES:
            return {"ok": False, "error": "shell.run command 超过长度限制", "content": ""}
        if "\x00" in command:
            return {"ok": False, "error": "shell.run command 包含非法字符", "content": ""}

        try:
            parts = shlex.split(command, posix=True)
        except ValueError:
            return {"ok": False, "error": "shell.run 命令解析失败", "content": ""}
        if not parts:
            return {"ok": False, "error": "shell.run 空命令", "content": ""}
        if parts[0] not in ALLOWED_SHELL_COMMANDS:
            return {"ok": False, "error": f"shell.run 命令不在白名单: {parts[0]}", "content": ""}
        # —— 关键防御：解析到可信系统目录下的绝对路径，防止 PATH 劫持 ——
        _sce: Optional[Callable[[Optional[Dict[str, str]]], Dict[str, str]]] = None
        try:
            from src.tools.command_tools import _resolve_safe_binary as _rsb
            from src.tools.command_tools import _sanitize_command_env as _sanitize_env
            _sce = _sanitize_env
            safe_bin = _rsb(parts[0])
        except Exception:
            safe_bin = None
        if not safe_bin:
            return {"ok": False,
                    "error": f"shell.run 无法安全解析命令路径 (疑似 PATH 劫持): {parts[0]}",
                    "content": ""}
        parts = [safe_bin] + list(parts[1:])

        if len(parts) > 64:
            return {"ok": False, "error": "shell.run 参数过多", "content": ""}

        timeout = int(tool.run_config.get("timeout", 60) or 60)
        timeout = max(1, min(300, timeout))
        try:
            env = _sce({"PWD": self._work_dir, "PYTHONDONTWRITEBYTECODE": "1"}) if _sce else None
            if env is None:
                return {"ok": False, "error": "环境变量校验失败（存在非法注入变量）", "content": ""}
            result = subprocess.run(
                parts, shell=False, cwd=self._work_dir,
                capture_output=True, text=True, timeout=timeout,
                env=env,
            )
            out = (result.stdout or "") + (result.stderr or "")
            return {
                "ok": result.returncode == 0,
                "content": out[:6000],
                "exit_code": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"命令执行超时 ({timeout}s)", "content": ""}
        except FileNotFoundError:
            return {"ok": False, "error": f"未找到命令: {parts[0]}", "content": ""}
        except PermissionError:
            return {"ok": False, "error": f"权限不足: {parts[0]}", "content": ""}
        except Exception as e:
            return {"ok": False, "error": f"shell 执行失败: {e}", "content": ""}

    @staticmethod
    def _resolve_safe_python() -> Optional[str]:
        """查找系统目录中的 python3，避免 PATH 劫持。"""
        trusted_dirs = [
            "/usr/bin", "/usr/local/bin", "/bin",
            "/usr/sbin", "/sbin",
        ]
        for d in trusted_dirs:
            for name in ("python3", "python"):
                p = os.path.join(d, name)
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    return p
        return None

    @staticmethod
    def _http_url_allowed(url: str) -> bool:
        """SSRF 防御：校验 URL 是否允许访问（禁止内网、禁止非 http/https）。"""
        if len(url) > 8192:
            return False
        try:
            from urllib.parse import urlparse as _urlparse
            parsed = _urlparse(url)
        except Exception:
            return False
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            return False
        hostname = (parsed.hostname or "").lower().strip("[]")
        if not hostname:
            return False
        # 黑名单关键字
        for bl in ("localhost", "loopback", ".local", ".internal"):
            if hostname == bl or hostname.endswith(bl):
                return False
        # 尝试作为 IP 判断内网范围
        try:
            import ipaddress as _ip
            ip = _ip.ip_address(hostname)
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                return False
        except (ValueError, TypeError):
            # 域名 —— 做一个轻量 DNS 检查（防御 DNS rebinding）
            # C-006: 带超时，避免恶意/缓慢 DNS 永久阻塞
            try:
                import socket as _socket
                infos = getaddrinfo_with_timeout(
                    hostname, None,
                    getaddrinfo_fn=_socket.getaddrinfo,
                    family=0, type=_socket.SOCK_STREAM,
                )
                for info in infos:
                    try:
                        ip = _ip.ip_address(info[4][0])
                        if (ip.is_private or ip.is_loopback or ip.is_link_local
                                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                            return False
                    except (ValueError, TypeError, IndexError) as _exc:
                        logger.debug("跳过异常项: %s", _exc, exc_info=True)
                        continue
            except (_socket.gaierror, _socket.herror, OSError, _socket.timeout):
                # 无法解析时让 request 自行失败
                pass
        return True

    def _run_http(self, tool: SklIIITool, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            import requests
        except ImportError:
            return {"ok": False, "error": "需要安装 requests 才能使用 http.run", "content": ""}
        rc = tool.run_config or {}
        method = str(rc.get("method", "GET")).upper()
        if method not in ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"):
            return {"ok": False, "error": f"http.run 非法 method: {method}", "content": ""}
        url_tpl = str(rc.get("url", "") or "")
        if not url_tpl:
            return {"ok": False, "error": "http.run 缺少 url", "content": ""}
        url = _interpolate(url_tpl, args)

        params = _interpolate_dict(rc.get("params") or {}, args)
        headers = _interpolate_dict(rc.get("headers") or {}, args)
        body = rc.get("body")
        if isinstance(body, str):
            body = _interpolate(body, args)
        elif isinstance(body, dict):
            body = _interpolate_dict(body, args)
        try:
            timeout = float(rc.get("timeout", 30) or 30)
            timeout = max(1, min(120, timeout))
        except (TypeError, ValueError):
            timeout = 30

        try:
            # 禁用自动重定向，防止 SSRF 绕过内网校验。
            # MEDIUM-002: 在锁内原子化完成 URL 校验与请求发送，防止 TOCTOU 竞态。
            # 使用 DNS Pinning 防止 DNS rebinding 攻击
            from urllib.parse import urlparse as _urlparse
            _parsed = _urlparse(url)
            _hostname = (_parsed.hostname or "").lower().strip("[]")
            if _hostname:
                _pin_ip = get_dns_pin_ip(_hostname)
                if _pin_ip:
                    ensure_dns_patch()
                    set_dns_pin(_hostname, _pin_ip)
                else:
                    return {"ok": False, "error": "SSRF 防护：无法解析到安全的 IP 地址", "content": ""}
            # 构造请求参数（在锁外，以便重定向复用）
            send_json = None
            send_data = None
            if method in ("GET", "HEAD", "OPTIONS"):
                pass  # no body
            else:
                json_body_cfg = rc.get("json")
                if json_body_cfg is not None:
                    send_json = (_interpolate_dict(json_body_cfg, args)
                                 if isinstance(json_body_cfg, dict) else json_body_cfg)
                else:
                    send_data = body

            def _do_request(req_method, req_url, req_json, req_data):
                return requests.request(req_method, req_url, params=params,
                                        headers=headers, timeout=timeout, stream=True,
                                        json=req_json, data=req_data,
                                        allow_redirects=False, verify=True)

            with self._http_lock:
                if not self._http_url_allowed(url):
                    return {"ok": False, "error": f"http.run URL 禁止访问: {url}", "content": ""}
                resp = _do_request(method, url, send_json, send_data)
            # 手动处理重定向：最多 3 跳，每跳都重新校验内网/本地地址
            redirect_count = 0
            while 300 <= resp.status_code < 400 and redirect_count < 3:
                loc = resp.headers.get("Location") or resp.headers.get("location")
                resp.close()
                if not loc:
                    return {"ok": False, "error": "HTTP 重定向缺少 Location", "content": ""}
                from urllib.parse import urljoin
                next_url = urljoin(url, loc)
                # 303 See Other 改为 GET 并丢弃 body；307/308 保留 method 和 body
                if resp.status_code == 303:
                    redirect_method = "GET"
                    redirect_json = None
                    redirect_data = None
                else:
                    redirect_method = method
                    redirect_json = send_json
                    redirect_data = send_data
                # 重定向时更新 DNS Pin：同域名保留原有 pin 防止 DNS rebinding，不同域名才重新解析
                _parsed_next = _urlparse(next_url)
                _hostname_next = (_parsed_next.hostname or "").lower().strip("[]")
                # Bug 5修复: 仅在 hostname 存在且与当前不同时才清除/更新 DNS pin。
                # 对于 hostname 为空的情况（非 HTTP 方案），_http_url_allowed 会拒绝请求并提前返回，
                # 不会发出实际请求，因此无需提前清除 pin（避免 pin 被清除后请求绕过验证发出）。
                if _hostname_next and _hostname_next != _hostname:
                    clear_dns_pin()
                    _pin_ip_next = get_dns_pin_ip(_hostname_next)
                    if _pin_ip_next:
                        set_dns_pin(_hostname_next, _pin_ip_next)
                    else:
                        clear_dns_pin()
                        return {"ok": False, "error": f"SSRF 防护：重定向目标无法解析到安全 IP: {_hostname_next}", "content": ""}
                with self._http_lock:
                    if not self._http_url_allowed(next_url):
                        return {"ok": False, "error": f"HTTP 重定向目标禁止访问: {next_url}", "content": ""}
                    url = next_url
                    method = redirect_method
                    send_json = redirect_json
                    send_data = redirect_data
                    _hostname = _hostname_next  # 更新当前主机名，保持 DNS pin 一致
                    resp = _do_request(redirect_method, next_url, redirect_json, redirect_data)
                redirect_count += 1

            # 重定向次数过多（仍为 3xx）：返回失败，避免误导调用方
            if 300 <= resp.status_code < 400:
                resp.close()
                clear_dns_pin()
                return {"ok": False, "error": "HTTP 重定向次数过多（超过 3 跳）", "content": ""}

            # 按 Content-Length 或分块读取进行上限保护
            try:
                cl = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                cl = 0
            if cl > _MAX_HTTP_RESPONSE_BYTES:
                resp.close()
                return {"ok": False,
                        "error": f"http.run 响应体过大 ({cl} bytes, 上限 {_MAX_HTTP_RESPONSE_BYTES})",
                        "content": ""}
            # 分块读取，累计不超过 _MAX_HTTP_RESPONSE_BYTES
            chunks: List[str] = []
            total = 0
            try:
                for chunk in resp.iter_content(chunk_size=8192, decode_unicode=True):
                    if not chunk:
                        continue
                    total += len(chunk) if isinstance(chunk, str) else len(chunk)
                    chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace"))
                    if total > _MAX_HTTP_RESPONSE_BYTES:
                        break
            finally:
                resp.close()
            text = "".join(chunks)
            # 截断到 8000 字符时附加截断标记，让模型感知内容不完整
            truncated = len(text) > 8000
            content = text[:8000]
            if truncated:
                content += f"\n\n...[内容已截断，原始长度 {len(text)} 字符]"
            return {
                "ok": True,
                "content": content,
                "status": resp.status_code,
                "truncated": truncated,
            }
        except Exception as e:
            return {"ok": False, "error": f"HTTP 请求失败: {e}", "content": ""}
        finally:
            # 清除 DNS Pin，防止影响后续请求
            clear_dns_pin()

    def _run_mcp(self, tool: SklIIITool, args: Dict[str, Any]) -> Dict[str, Any]:
        rc = tool.run_config or {}
        server_name = str(rc.get("server_name", "") or "")
        tool_name = str(rc.get("tool_name", "") or "")
        tpl_args = rc.get("args") or {}
        resolved = _interpolate_dict(tpl_args, args)
        if not server_name or not tool_name:
            return {"ok": False, "error": "mcp.run 需要 server_name + tool_name", "content": ""}
        if self._mcp_manager is None:
            return {"ok": False, "error": "MCP 管理器未初始化", "content": ""}
        try:
            full = f"{server_name}_{tool_name}"
            return self._mcp_manager.call_tool(full, resolved)
        except Exception as e:
            return {"ok": False, "error": f"MCP 转发失败: {e}", "content": ""}

    def _notify_changed(self) -> None:
        if self._on_packages_changed is not None:
            try:
                self._on_packages_changed()
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
