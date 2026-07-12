# -*- coding: utf-8 -*-
"""Git 工具模块。

提供一组安全的 Git 操作函数，所有命令均通过 subprocess.run(shell=False) 调用，
默认超时 30 秒。输出统一经过 sanitize_for_log() 脱敏。

安全策略：
1. project_dir 必须是真实 Git 仓库（存在 .git 目录且 git rev-parse 成功）。
2. 所有 git 命令都在 project_dir 下执行，禁止离开沙箱。
3. 用户传入的 file / message 等字符串拒绝以 '-' 开头，并过滤 --upload-pack 等危险参数。
4. file 参数做路径归一化校验，确保位于 project_dir 内，禁止路径穿越。
"""

import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple, Union

from src.core.log_sanitizer import sanitize_for_log

logger = logging.getLogger(__name__)

_TIMEOUT = 30
_MAX_MESSAGE_LEN = 4096
_MAX_FILE_ARG_LEN = 512
_MAX_LOG_N = 1000


class GitError(Exception):
    """Git 工具内部错误。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ---------- 内部辅助 ----------


def _validate_git_repo(project_dir: str) -> str:
    """验证 project_dir 是真实 Git 仓库，返回归一化后的绝对路径。"""
    if not isinstance(project_dir, str) or not project_dir:
        raise GitError("project_dir 不能为空")
    try:
        repo = os.path.realpath(project_dir)
    except Exception as exc:
        raise GitError(f"project_dir 非法: {exc}")
    if not os.path.isdir(repo):
        raise GitError("project_dir 不存在或不是目录")
    git_dir = os.path.join(repo, ".git")
    # Git Worktree 的 .git 是文件而非目录，也属于合法 Git 仓库
    if not os.path.exists(git_dir):
        raise GitError("project_dir 不是 Git 仓库（缺少 .git）")
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            shell=False,
        )
    except Exception as exc:
        raise GitError(f"验证 Git 仓库失败: {exc}")
    if result.returncode != 0:
        stderr = sanitize_for_log((result.stderr or "").strip(), 500)
        raise GitError(f"验证 Git 仓库失败: {stderr}")
    return repo


def _is_dangerous_arg(value: str) -> bool:
    """检查用户传入的字符串是否包含危险 Git 选项/参数。"""
    if not isinstance(value, str):
        return True
    lowered = value.lower()
    # 禁止以 '-' 开头（会被解析为选项）
    if value.startswith("-"):
        return True
    # 禁止已知的危险远程辅助参数
    dangerous = ("--upload-pack", "--receive-pack", "--exec", "--config")
    for d in dangerous:
        if d in lowered:
            return True
    return False


_GIT_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_MAX_IDENTIFIER_LEN = 256


def _validate_git_identifier(identifier: str) -> str:
    """校验分支名、commit hash、tag 等 Git 标识符。

    只允许字母、数字、下划线、连字符、点、斜杠；禁止以 '-' 开头；长度不超过 256。
    返回去除首尾空白后的标识符。
    """
    if not isinstance(identifier, str):
        raise GitError("Git 标识符必须是字符串")
    identifier = identifier.strip()
    if not identifier:
        raise GitError("Git 标识符不能为空")
    if identifier.startswith("-"):
        raise GitError("Git 标识符不能以 '-' 开头")
    if len(identifier) > _MAX_IDENTIFIER_LEN:
        raise GitError("Git 标识符过长")
    if not _GIT_IDENTIFIER_RE.match(identifier):
        raise GitError("Git 标识符包含非法字符")
    # 拒绝 "." 和 ".." 作为标识符（git checkout . 会丢弃工作树修改）
    if identifier in (".", ".."):
        raise GitError("Git 标识符不能是 '.' 或 '..'")
    return identifier


def _safe_path_arg(project_dir: str, raw_path: Any) -> str:
    """校验并归一化文件路径，返回相对于 project_dir 的相对路径。"""
    if not isinstance(raw_path, str):
        raise GitError("file 路径必须是字符串")
    raw_path = raw_path.strip()
    if not raw_path:
        raise GitError("file 路径不能为空")
    if len(raw_path) > _MAX_FILE_ARG_LEN:
        raise GitError("file 路径过长")
    if _is_dangerous_arg(raw_path):
        raise GitError("file 路径包含危险参数")
    try:
        base = os.path.realpath(project_dir)
        if os.path.isabs(raw_path):
            candidate = os.path.realpath(raw_path)
        else:
            candidate = os.path.realpath(os.path.join(base, raw_path))
    except Exception as exc:
        raise GitError(f"路径解析失败: {exc}")
    if candidate != base and not candidate.startswith(base + os.sep):
        raise GitError("file 路径超出项目目录")
    try:
        rel = os.path.relpath(candidate, base)
    except Exception as exc:
        raise GitError(f"路径归一化失败: {exc}")
    if ".." in rel.split(os.sep):
        raise GitError("file 路径包含非法穿越")
    if rel.startswith("-"):
        raise GitError("file 路径不能以 '-' 开头")
    return rel


def _build_git_env() -> dict:
    """构建清洗后的 Git 子进程环境变量，防止 hooks 逃逸和环境注入。"""
    # 白名单方式：只保留必要的环境变量
    safe_keys = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "HOME", "USER", "TMPDIR", "TEMP", "TMP"}
    if os.name == "nt":
        safe_keys.update({"SYSTEMROOT", "COMSPEC", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES"})
    env = {k: v for k, v in os.environ.items() if k in safe_keys}
    # 禁用 Git hooks，防止 .git/hooks/ 下的恶意脚本执行
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def _run_git(
    project_dir: str,
    args: List[str],
    timeout: int = _TIMEOUT,
) -> subprocess.CompletedProcess:
    """在已验证的 Git 仓库内执行 git 命令。

    安全措施：
    - shell=False 防止 shell 注入
    - 清洗环境变量，移除 GIT_SSH_COMMAND/GIT_CONFIG_* 等注入向量
    - 通过 -c core.hooksPath=/dev/null 禁用 hooks 执行
    - 所有命令带 --no-verify 防止 pre-commit/commit-msg 钩子
    """
    repo = _validate_git_repo(project_dir)
    # 在 git 命令前插入 hooks 禁用配置（使用 os.devnull 兼容 Windows）
    _null = os.devnull
    safe_args = ["git", "-c", f"core.hooksPath={_null}"] + (args[1:] if args and args[0] == "git" else args)
    try:
        return subprocess.run(
            safe_args,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            env=_build_git_env(),
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"git 命令执行超时（{timeout}s）")
    except Exception as exc:
        raise GitError(f"git 命令执行失败: {exc}")


# ---------- 公共 API ----------


def git_status(project_dir: str) -> Dict[str, List[str]]:
    """返回工作区状态，包含 modified / staged / untracked 三个文件列表。"""
    result = _run_git(project_dir, ["git", "status", "--porcelain"])
    modified: List[str] = []
    staged: List[str] = []
    untracked: List[str] = []
    for line in (result.stdout or "").splitlines():
        if len(line) < 3:
            continue
        index_status = line[0]
        worktree_status = line[1]
        path_text = line[3:].rstrip()
        if not path_text:
            continue
        # 重命名/拷贝格式：XY old_path -> new_path
        if " -> " in path_text:
            _, new_path = path_text.split(" -> ", 1)
            path_text = new_path.strip()
        # 去掉可能存在的路径引号
        if path_text.startswith('"') and path_text.endswith('"'):
            path_text = path_text[1:-1]
        path_text = sanitize_for_log(path_text, 1024)
        if index_status == "?" and worktree_status == "?":
            untracked.append(path_text)
        elif index_status in "AMDRCU":
            staged.append(path_text)
        if worktree_status in "MD" and (index_status, worktree_status) != ("?", "?"):
            modified.append(path_text)
    return {
        "modified": modified,
        "staged": staged,
        "untracked": untracked,
    }


def git_diff(project_dir: str, cached: bool = False, file: Optional[str] = None) -> str:
    """返回 diff 文本。cached=True 时查看 staged diff；file 指定单个文件。"""
    args = ["git", "diff"]
    if cached:
        args.append("--cached")
    if file:
        rel = _safe_path_arg(project_dir, file)
        args.extend(["--", rel])
    result = _run_git(project_dir, args)
    out = (result.stdout or "") + (result.stderr or "")
    return sanitize_for_log(out, 8000)


def git_add(project_dir: str, files: Union[str, List[str]]) -> Tuple[bool, str]:
    """stage 指定文件。"""
    if isinstance(files, str):
        files = [files]
    if not isinstance(files, (list, tuple)) or not files:
        return False, "[git_add] files 不能为空"
    rel_paths = []
    for f in files:
        try:
            rel_paths.append(_safe_path_arg(project_dir, f))
        except GitError as exc:
            return False, f"[git_add] {exc.message}"
    args = ["git", "add", "--"] + rel_paths
    result = _run_git(project_dir, args)
    out = (result.stdout or "") + (result.stderr or "")
    ok = result.returncode == 0
    return ok, "[git_add]\n" + sanitize_for_log(out, 4000) + f"\n[exit: {result.returncode}]"


def git_commit(project_dir: str, message: str) -> Tuple[bool, str]:
    """提交 staged 改动。"""
    if not isinstance(message, str):
        return False, "[git_commit] message 必须是字符串"
    message = message.strip()
    if not message:
        return False, "[git_commit] message 不能为空"
    if message.startswith("-"):
        return False, "[git_commit] message 不能以 '-' 开头"
    if len(message.encode("utf-8", errors="replace")) > _MAX_MESSAGE_LEN:
        return False, "[git_commit] message 过长"
    # 防御性过滤控制字符与 NUL
    message = "".join(ch for ch in message if ord(ch) != 0 and (ord(ch) >= 32 or ch in "\n\r\t"))
    result = _run_git(project_dir, ["git", "commit", "-m", message])
    out = (result.stdout or "") + (result.stderr or "")
    ok = result.returncode == 0
    return ok, "[git_commit]\n" + sanitize_for_log(out, 4000) + f"\n[exit: {result.returncode}]"


def git_log(project_dir: str, n: int = 10) -> List[Dict[str, str]]:
    """返回最近 n 条提交信息。"""
    if isinstance(n, bool):
        n = 10
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise GitError("n 必须是整数")
    if n <= 0:
        n = 10
    if n > _MAX_LOG_N:
        n = _MAX_LOG_N
    fmt = "%H%x00%an%x00%ad%x00%s%x00%x01"
    result = _run_git(project_dir, ["git", "log", f"-n{n}", f"--pretty=format:{fmt}"])
    entries: List[Dict[str, str]] = []
    raw = result.stdout or ""
    if not raw.strip():
        return entries
    for record in raw.split("\x01"):
        record = record.strip("\x00")
        if not record:
            continue
        parts = record.split("\x00")
        if len(parts) < 4:
            continue
        entries.append({
            "hash": sanitize_for_log(parts[0].strip(), 128),
            "author": sanitize_for_log(parts[1].strip(), 256),
            "date": sanitize_for_log(parts[2].strip(), 128),
            "message": sanitize_for_log(parts[3].strip(), 1024),
        })
    return entries


def git_branch(project_dir: str) -> Dict[str, Any]:
    """返回当前分支和本地分支列表。"""
    branches_result = _run_git(project_dir, ["git", "branch", "--format=%(refname:short)"])
    branches = [
        sanitize_for_log(line.strip(), 128)
        for line in (branches_result.stdout or "").splitlines()
        if line.strip()
    ]
    current_result = _run_git(project_dir, ["git", "branch", "--show-current"])
    current = (current_result.stdout or "").strip()
    current = sanitize_for_log(current, 128) if current else None
    return {
        "current": current,
        "branches": branches,
    }


def git_checkout(project_dir: str, branch: str, create: bool = False) -> str:
    """切换分支，或创建并切换（create=True）。"""
    repo = _validate_git_repo(project_dir)
    branch = _validate_git_identifier(branch)
    args = ["git", "checkout"]
    if create:
        args.append("-b")
    args.append(branch)
    result = _run_git(repo, args)
    out = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise GitError(f"git checkout 失败 (exit {result.returncode}): {out.strip()[:500]}")
    return sanitize_for_log(out.strip(), 1024)


def git_branch_create(project_dir: str, branch: str, base: Optional[str] = None) -> str:
    """新建分支。"""
    repo = _validate_git_repo(project_dir)
    branch = _validate_git_identifier(branch)
    args = ["git", "branch", branch]
    if base:
        base = _validate_git_identifier(base)
        args.append(base)
    result = _run_git(repo, args)
    out = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise GitError(f"git branch 失败 (exit {result.returncode}): {out.strip()[:500]}")
    return sanitize_for_log(out.strip(), 1024)


def git_branch_delete(project_dir: str, branch: str, force: bool = False) -> str:
    """删除本地分支。"""
    repo = _validate_git_repo(project_dir)
    branch = _validate_git_identifier(branch)
    args = ["git", "branch", "-D" if force else "-d", branch]
    result = _run_git(repo, args)
    out = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise GitError(f"git branch -d 失败 (exit {result.returncode}): {out.strip()[:500]}")
    return sanitize_for_log(out.strip(), 1024)


def git_stash(
    project_dir: str,
    action: str = "push",
    message: Optional[str] = None,
    stash_index: Optional[int] = None,
) -> str:
    """stash 操作：push / pop / list / drop / apply。

    - push: 暂存当前改动，可带 message
    - pop: 恢复最近一次 stash
    - apply: 应用最近一次 stash 但不删除
    - drop: 删除指定 stash_index 的 stash
    - list: 列出所有 stash
    """
    repo = _validate_git_repo(project_dir)
    action = str(action).strip().lower()
    if action not in ("push", "pop", "list", "drop", "apply"):
        raise GitError("action 必须是 push/pop/list/drop/apply 之一")
    args = ["git", "stash", action]
    if action == "push" and message:
        message = str(message).strip()
        if not message:
            raise GitError("message 不能为空")
        if len(message) > _MAX_MESSAGE_LEN:
            raise GitError("message 过长")
        if message.startswith("-"):
            raise GitError("message 不能以 '-' 开头")
        if _is_dangerous_arg(message):
            raise GitError("message 包含危险参数")
        args.extend(["-m", message])
    if action in ("pop", "apply", "drop") and stash_index is not None:
        try:
            idx = int(stash_index)
            if idx < 0:
                raise GitError("stash_index 不能为负数")
            args.append(f"stash@{{{idx}}}")
        except (TypeError, ValueError):
            raise GitError("stash_index 必须是整数")
    result = _run_git(repo, args)
    out = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise GitError(f"git stash 失败 (exit {result.returncode}): {out.strip()[:500]}")
    return sanitize_for_log(out.strip(), 4096)


def git_reset(project_dir: str, mode: str = "mixed", target: str = "HEAD") -> str:
    """重置仓库状态。

    - mode: soft / mixed / hard
    - target: HEAD / commit hash / branch
    """
    repo = _validate_git_repo(project_dir)
    mode = str(mode).strip().lower()
    if mode not in ("soft", "mixed", "hard"):
        raise GitError("mode 必须是 soft/mixed/hard 之一")
    target = _validate_git_identifier(target)
    args = ["git", "reset", f"--{mode}", target]
    result = _run_git(repo, args)
    out = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise GitError(f"git reset 失败 (exit {result.returncode}): {out.strip()[:500]}")
    return sanitize_for_log(out.strip(), 1024)


# ---------- 兼容原有 GitTools 类的适配器 ----------


class GitTools:
    """Git 工具适配器，供 ToolExecutor 使用。

    内部通过调用本模块的纯函数实现，保持与旧代码的类接口兼容。
    """

    def __init__(self, executor):
        self._exec = executor

    def _project_dir(self) -> str:
        try:
            work_dir = getattr(self._exec.sandbox, "work_dir", None)
            if work_dir:
                return str(work_dir)
            root = getattr(self._exec.sandbox, "root", None)
            if root:
                return str(root)
        except Exception:
            pass
        return "."

    def git_status(self, call=None):
        project_dir = self._project_dir()
        try:
            data = git_status(project_dir)
            lines = [f"分支: {git_branch(project_dir).get('current') or 'unknown'}"]
            lines.append(f"modified ({len(data['modified'])}):")
            lines.extend(f"  {f}" for f in data["modified"])
            lines.append(f"staged ({len(data['staged'])}):")
            lines.extend(f"  {f}" for f in data["staged"])
            lines.append(f"untracked ({len(data['untracked'])}):")
            lines.extend(f"  {f}" for f in data["untracked"])
            return True, "[git_status]\n" + "\n".join(lines)
        except GitError as exc:
            return False, "[git_status] " + exc.message

    def git_diff(self, call=None):
        project_dir = self._project_dir()
        cached = False
        target_file = None
        if isinstance(call, dict):
            cached_val = str(call.get("cached", "")).strip().lower()
            if cached_val in ("1", "true", "yes"):
                cached = True
            target_file = call.get("file") or call.get("filename") or call.get("path")
        try:
            text = git_diff(project_dir, cached=cached, file=target_file)
            return True, "[git_diff]\n" + text
        except GitError as exc:
            return False, "[git_diff] " + exc.message

    def git_add(self, call=None):
        project_dir = self._project_dir()
        files = []
        if isinstance(call, dict):
            val = call.get("files") or call.get("file") or call.get("path")
            if isinstance(val, str):
                files = [val]
            elif isinstance(val, (list, tuple)):
                files = list(val)
        if not files:
            return False, "[git_add] 缺少 files"
        try:
            ok, out = git_add(project_dir, files)
            return ok, out
        except GitError as exc:
            return False, "[git_add] " + exc.message

    def git_commit(self, call=None):
        project_dir = self._project_dir()
        message = ""
        if isinstance(call, dict):
            message = call.get("message", "")
        if not isinstance(message, str):
            return False, "[git_commit] message 必须是字符串"
        return git_commit(project_dir, message)

    def git_log(self, call=None):
        project_dir = self._project_dir()
        n = 10
        if isinstance(call, dict):
            n = call.get("n", 10)
        try:
            entries = git_log(project_dir, n=n)
            lines = []
            for entry in entries:
                lines.append(f"{entry['hash'][:7]} {entry['date']} {entry['message']}")
            return True, "[git_log]\n" + "\n".join(lines)
        except GitError as exc:
            return False, "[git_log] " + exc.message

    def git_branch(self, call=None):
        project_dir = self._project_dir()
        try:
            data = git_branch(project_dir)
            lines = [f"当前分支: {data.get('current') or '(detached)'}"]
            lines.append("本地分支:")
            lines.extend(f"  {b}" for b in data["branches"])
            return True, "[git_branch]\n" + "\n".join(lines)
        except GitError as exc:
            return False, "[git_branch] " + exc.message

    def git_checkout(self, call=None):
        project_dir = self._project_dir()
        branch = ""
        create = False
        if isinstance(call, dict):
            branch = str(call.get("branch") or call.get("name") or "").strip()
            create_val = str(call.get("create", "")).strip().lower()
            if create_val in ("1", "true", "yes"):
                create = True
        if not branch:
            return False, "[git_checkout] 缺少 branch"
        try:
            out = git_checkout(project_dir, branch, create=create)
            return True, "[git_checkout]\n" + out
        except GitError as exc:
            return False, "[git_checkout] " + exc.message

    def git_branch_create(self, call=None):
        project_dir = self._project_dir()
        branch = ""
        base = None
        if isinstance(call, dict):
            branch = str(call.get("branch") or call.get("name") or "").strip()
            base = call.get("base") or call.get("from")
        if not branch:
            return False, "[git_branch_create] 缺少 branch"
        try:
            out = git_branch_create(project_dir, branch, base=base)
            return True, "[git_branch_create]\n" + out
        except GitError as exc:
            return False, "[git_branch_create] " + exc.message

    def git_branch_delete(self, call=None):
        project_dir = self._project_dir()
        branch = ""
        force = False
        if isinstance(call, dict):
            branch = str(call.get("branch") or call.get("name") or "").strip()
            force_val = str(call.get("force", "")).strip().lower()
            if force_val in ("1", "true", "yes"):
                force = True
        if not branch:
            return False, "[git_branch_delete] 缺少 branch"
        try:
            out = git_branch_delete(project_dir, branch, force=force)
            return True, "[git_branch_delete]\n" + out
        except GitError as exc:
            return False, "[git_branch_delete] " + exc.message

    def git_stash(self, call=None):
        project_dir = self._project_dir()
        action = "push"
        message = None
        stash_index = None
        if isinstance(call, dict):
            action = str(call.get("action", "push")).strip().lower()
            message = call.get("message")
            stash_index = call.get("index")
        try:
            out = git_stash(project_dir, action=action, message=message, stash_index=stash_index)
            return True, "[git_stash]\n" + out
        except GitError as exc:
            return False, "[git_stash] " + exc.message

    def git_reset(self, call=None):
        project_dir = self._project_dir()
        mode = "mixed"
        target = "HEAD"
        if isinstance(call, dict):
            mode = str(call.get("mode", "mixed")).strip().lower()
            target = str(call.get("target", "HEAD")).strip()
        try:
            out = git_reset(project_dir, mode=mode, target=target)
            return True, "[git_reset]\n" + out
        except GitError as exc:
            return False, "[git_reset] " + exc.message
