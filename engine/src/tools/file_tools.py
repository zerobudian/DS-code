# -*- coding: utf-8 -*-
"""文件操作工具 — 所有文件读写统一走 ProjectSandbox._safe_path

安全策略：
  1. create_file / read_file / mark_delete → 全部经过 sandbox._safe_path 路径校验
  2. edit_file / insert_lines / delete_lines / replace_in_files / rename_symbol / search_code / find_symbol / find_files / find_markers → 只操作已存在于 work 目录下的文件
  3. 任何路径参数超长或包含空字符都会被直接拒绝
  4. replace_in_files / rename_symbol 使用 sandbox.list_work_files()，其内部以 followlinks=False 遍历并过滤符号链接目录（H-012）
  5. 所有直接写入操作也会走 ProjectSandbox.write_file 再经过路径防御
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

import difflib
import os
import re
import ast
import fnmatch
import stat
import unicodedata

from src.core.config import Config


def _sanitize_audit_text(text: object) -> str:
    """清理审计日志中的控制字符，防止用户可控数据被当作格式化字符串或转义序列。"""
    if text is None:
        return ""
    s = str(text)
    return "".join(ch if not (0 <= ord(ch) <= 31) else "?" for ch in s)


def _safe_int(v, default=0, strict=False):
    """安全整数转换：拒绝布尔值、限制字符串长度、类型白名单，避免 DoS 和类型混淆。

    P0-1 修复: 将 delete_lines 内部的 _to_int 提升为模块级函数，支持 default 参数，
    供 delete_lines/insert_lines/search_code 等多处复用。
    strict=True 时，非法输入返回 None（由调用方判断并返回错误）。
    """
    if isinstance(v, bool):
        if strict:
            return None
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        stripped = v.strip()
        if len(stripped) > 32 or not stripped.lstrip("-").isdigit():
            if strict:
                return None
            return default
        try:
            return int(stripped)
        except (TypeError, ValueError):
            if strict:
                return None
            return default
    if strict:
        return None
    return default


def _sanitize_rel_path(sandbox, rel_path: str, is_work: bool = True):
    """上层统一入口：把 rel_path 规范化后再做安全校验。

    返回 (safe_abs_path, None) 或 (None, error_str)
    """
    if not rel_path or not isinstance(rel_path, (str, os.PathLike)):
        return None, "路径参数缺失或类型错误"
    rel = unicodedata.normalize("NFC", str(rel_path))
    if len(rel) > 4096:
        return None, "路径过长"
    if "\x00" in rel:
        return None, "路径含非法空字符"
    # 禁止 '.' 和 '..' 作为独立路径段（纵深防御：与 _safe_path 的校验保持一致）
    # 这里不依赖 Path 的归一化，因为 Path('.').parts 在不同平台可能为空。
    for seg in re.split(r"[\\/]", rel):
        if seg == "." or seg == "..":
            return None, "路径含非法的 . 或 .. 段"
    safe = sandbox._safe_path(rel, is_work)
    if safe is None:
        return None, "路径不合法（拒绝路径穿越或隐藏文件）"
    return safe, None


def _is_sensitive_filename(name: str) -> bool:
    """判断文件名是否为隐藏文件或敏感扩展名（M-002）。

    隐藏文件：路径中任意段以 '.' 或 '_' 开头（如 .env、.ssh、_keys）。
    敏感扩展名：.pem / .key / .p12 / .env。
    """
    if not name:
        return False
    # 标准化路径分隔符后逐段检查
    for seg in str(name).replace("\\", "/").split("/"):
        if not seg:
            continue
        if seg.startswith(".") or seg.startswith("_"):
            return True
        if os.path.splitext(seg)[1].lower() in {".pem", ".key", ".p12", ".env"}:
            return True
    return False


def _sanitize_list_entry(path: str, size: int) -> str:
    """对列表中的单条文件信息进行脱敏（M-002）。"""
    if _is_sensitive_filename(path):
        return "  [hidden]"
    return f"  {path} ({str(size)} bytes)"


class FileTools:
    """文件操作工具集：创建/读取/修改/删除/搜索文件"""

    _MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB，防止读取超大文件导致 OOM
    _MAX_CREATE_SIZE = 4 * 1024 * 1024  # 与 ProjectSandbox._MAX_WRITE_BYTES 对齐
    _READ_CHUNK_SIZE = 8192              # 分块读取块大小
    _READ_PREVIEW_MAX_BYTES = 4096       # 仅保留前 4KB 用于返回，避免内存膨胀
    # 列表输出上限，防止超大目录导致消息 OOM
    _MAX_LIST_OUTPUT_LINES = 1000
    _MAX_LIST_OUTPUT_BYTES = 100 * 1024

    def __init__(self, executor):
        self._exec = executor

    def _normalized_rel(self, safe) -> str:
        """把 _sanitize_rel_path 返回的绝对路径转成 work_dir 下的相对路径（已 NFC 规范化）。"""
        return os.path.relpath(str(safe), str(self._exec.sandbox.work_dir))

    # ---------- 权限模式辅助 ----------
    @staticmethod
    def _permission_mode() -> str:
        """读取当前权限模式（reviewer / coder / full）。"""
        try:
            return Config().permission_mode
        except Exception:
            return "coder"

    @staticmethod
    def _reviewer_deny(tool_name: str):
        """如果当前是 reviewer 模式，返回统一的拒绝消息；否则返回 None。"""
        if FileTools._permission_mode() == "reviewer":
            return False, f"[{tool_name}] 当前为 reviewer 模式，禁止修改项目文件"
        return None

    def _safe_work_root(self) -> str:
        """获取当前项目 work 目录的绝对路径（供批量操作的 root 目录）。"""
        base = None
        try:
            sandbox = getattr(self._exec, "sandbox", None)
            if sandbox is not None:
                base = getattr(sandbox, "work_dir", None)
        except Exception:
            base = None
        if base is None:
            base = "."
        try:
            return str(os.path.realpath(str(base)))
        except Exception:
            return str(base or ".")

    def _read_raw(self, rel_path: str) -> Optional[str]:
        """绕过 read_file 的格式化输出，直接读取原始文本内容。

        返回值：
        - str: 文件内容（包括空字符串表示空文件）
        - None: 读取失败（路径非法、文件过大、IO错误等）
        """
        safe, err = _sanitize_rel_path(self._exec.sandbox, rel_path, True)
        if safe is None:
            return None
        try:
            safe_resolved = os.path.realpath(str(safe))
            work_dir_resolved = os.path.realpath(str(self._exec.sandbox.work_dir))
            if not safe_resolved.startswith(work_dir_resolved + os.sep) and safe_resolved != work_dir_resolved:
                return None
        except OSError:
            return None
        try:
            # 文件大小检查，防止 OOM
            import os as _os
            try:
                _sz = _os.path.getsize(str(safe))
                if _sz > 10 * 1024 * 1024:  # 10MB 上限
                    return None
            except OSError:
                return None
            with open(str(safe), "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except (OSError, ValueError):
            return None

    # ========== 基础文件操作 (全部通过 ProjectSandbox 并强制安全路径) ==========
    def create_file(self, call):
        """创建或覆盖文件，若 on_edit_apply_request 存在则走 diff 确认。"""
        deny = self._reviewer_deny("create_file")
        if deny:
            return deny
        filename = call.get("filename") or call.get("path") or ""
        content = call.get("content", "")
        if not filename or not isinstance(filename, str):
            return False, "[create_file] 缺少 filename"
        # 基础内容防御：content 必须是 str 类型（防止把 dict 当文本写入）
        if not isinstance(content, str):
            if content is None:
                content = ""
            else:
                content = str(content)
        if len(content) > self._MAX_CREATE_SIZE:
            return False, f"[create_file] 内容超过 {self._MAX_CREATE_SIZE // (1024 * 1024)} MB"

        # 路径验证 — 防止把内容写入项目沙箱外
        safe, err = _sanitize_rel_path(self._exec.sandbox, filename, True)
        if safe is None:
            return False, f"[create_file] {err}: {filename}"
        rel = self._normalized_rel(safe)

        # 可选的 diff 确认钩子（GUI 场景中可见）
        if getattr(self._exec, "on_edit_apply_request", None):
            try:
                existing = ""
                try:
                    if os.path.isfile(safe):
                        with open(safe, "r", encoding="utf-8", errors="ignore") as f:
                            existing = f.read()
                except Exception:
                    existing = ""
                diff_text = self.make_unified_diff(existing, content, filename)
                try:
                    ok = bool(self._exec.on_edit_apply_request(filename, diff_text))
                    if not ok:
                        return False, f"[create_file] 用户拒绝修改 {filename}"
                except Exception as e:
                    # 回调异常时拒绝写入，防止未授权修改
                    return False, f"[create_file] 确认回调异常: {str(e)}"
            except Exception as e:
                return False, f"[create_file] diff 确认失败: {str(e)}"

        # 真正写入 — 通过 ProjectSandbox.write_file (安全路径)
        # 写入前再次验证路径，防止 TOCTOU 竞态（符号链接在检查后被修改）
        try:
            safe_resolved = os.path.realpath(str(safe))
            work_dir_resolved = os.path.realpath(str(self._exec.sandbox.work_dir))
            if not safe_resolved.startswith(work_dir_resolved + os.sep) and safe_resolved != work_dir_resolved:
                return False, f"[create_file] 路径验证失败（TOCTOU 防护）: {filename}"
        except OSError:
            return False, f"[create_file] 路径解析失败: {filename}"
        ok = self._exec.sandbox.write_file(rel, content)
        if ok:
            # M-018: 审计日志（文件创建）
            logger.info(
                "[AUDIT] create_file: %s (%d bytes)",
                _sanitize_audit_text(rel),
                len(content),
            )
            # 通知LSP文件已创建
            try:
                lsp_tools = getattr(self._exec, "lsp_tools", None)
                if lsp_tools:
                    lsp_tools.notify_file_changed(str(safe), is_new_file=True)
            except Exception:
                pass
            return True, f"[create_file] 已写入 {rel}"
        return False, f"[create_file] 写入失败: {rel}"

    def read_file(self, call):
        filename = call.get("filename") or call.get("path") or ""
        if not filename:
            return False, "[read_file] 缺少 filename"
        # 路径解析，防止路径穿越与隐藏文件
        safe, err = _sanitize_rel_path(self._exec.sandbox, filename, True)
        if safe is None:
            return False, f"[read_file] {err}: {filename}"

        # M-001: 读取前先用 os.path.getsize 检查文件大小，防止超大文件导致 OOM
        try:
            file_size = os.path.getsize(str(safe))
        except OSError as e:
            return False, f"[read_file] 无法获取文件大小: {filename} ({e})"
        if file_size > self._MAX_FILE_SIZE:
            return False, f"[read_file] 文件过大 ({file_size} bytes, 上限 {self._MAX_FILE_SIZE} bytes): {filename}"

        # 分块读取（8KB/块），累计超过上限立即报错，防止 OOM 与 TOCTOU（CWE-400）
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(str(safe), flags)
        except OSError as e:
            return False, f"[read_file] 无法打开文件: {filename} ({e})"

        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                os.close(fd)
                return False, f"[read_file] 不是普通文件: {filename}"
        except OSError as e:
            os.close(fd)
            return False, f"[read_file] 无法获取文件信息: {filename} ({e})"

        total = 0
        chunks = []
        max_keep = self._READ_PREVIEW_MAX_BYTES
        try:
            f = os.fdopen(fd, "rb")
        except OSError as e:
            os.close(fd)
            return False, f"[read_file] 无法打开文件描述符: {filename} ({e})"

        try:
            with f:
                while True:
                    data = f.read(self._READ_CHUNK_SIZE)
                    if not data:
                        break
                    total += len(data)
                    if total > self._MAX_FILE_SIZE:
                        return False, f"[read_file] 文件过大 (>{self._MAX_FILE_SIZE // (1024 * 1024)} MB): {filename}"
                    kept_so_far = sum(len(c) for c in chunks)
                    if kept_so_far < max_keep:
                        take = data[:max(0, max_keep - kept_so_far)]
                        if take:
                            chunks.append(take)
        except OSError as e:
            return False, f"[read_file] 读取失败: {filename} ({e})"

        content = b"".join(chunks).decode("utf-8", errors="replace")
        # H-3 修复: 截断时添加提示，让模型知道内容不完整
        if len(content) > 4000:
            short = content[:4000]
            return True, f"[read_file] {filename} (文件已截断，仅显示前 4000/{len(content)} 字符，剩余内容未显示，请使用 read_file_range 分段读取):\n{short}"
        return True, f"[read_file] {filename}:\n{content}"

    def mark_delete(self, call):
        deny = self._reviewer_deny("mark_delete")
        if deny:
            return deny
        filename = call.get("filename", "")
        if not filename:
            return False, "[mark_delete] 缺少 filename"
        safe, err = _sanitize_rel_path(self._exec.sandbox, filename, True)
        if safe is None:
            return False, f"[mark_delete] {err}: {filename}"
        rel = self._normalized_rel(safe)
        ok = self._exec.sandbox.safe_delete(rel)
        if ok:
            # M-018: 审计日志（文件删除）
            logger.info("[AUDIT] mark_delete: %s", _sanitize_audit_text(rel))
            # 通知LSP文件已关闭/删除
            try:
                lsp_tools = getattr(self._exec, "lsp_tools", None)
                if lsp_tools:
                    lsp_tools.notify_file_deleted(str(safe))
            except Exception:
                pass
            return True, f"[mark_delete] 已将 {rel} 移动到回收站"
        return False, f"[mark_delete] 失败: {rel}"

    def list_files(self):
        files = self._exec.sandbox.list_work_files()
        lines = []
        total = len(files)
        displayed = 0
        msg_size = 0
        for f in files:
            line = _sanitize_list_entry(f.get("path", ""), f.get("size", 0))
            if (
                displayed >= self._MAX_LIST_OUTPUT_LINES
                or msg_size + len(line) + 1 > self._MAX_LIST_OUTPUT_BYTES
            ):
                break
            lines.append(line)
            displayed += 1
            msg_size += len(line) + 1
        if total > displayed:
            lines.append(f"... ({total - displayed} more files omitted; showing {displayed}/{total})")
        text = "\n".join(lines) if lines else "  (空目录)"
        return True, "[list_files] Work 目录:\n" + text

    def backup_workspace(self, call):
        label = call.get("label", "backup") if isinstance(call, dict) else "backup"
        path = self._exec.sandbox.copy_work_as_backup(label)
        return True, "[backup_workspace] 已备份 -> " + str(path)

    # ========== DS.md 区块工具 ==========
    def update_ds_section(self, call):
        deny = self._reviewer_deny("update_ds_section")
        if deny:
            return deny
        section = str(call.get("section", "")).strip().lower()
        content = call.get("content", "")
        if section not in ("description", "pending", "completed", "custom"):
            return False, "[update_ds_section] section 必须是 description/pending/completed/custom"
        if not isinstance(content, str) or not content.strip():
            return False, "[update_ds_section] content 不能为空"
        ok = self._exec.sandbox.write_section(section, content)
        if ok:
            return True, f"[update_ds_section] 已更新 '{section}'"
        return False, "[update_ds_section] 更新失败"

    def add_task(self, call):
        deny = self._reviewer_deny("add_task")
        if deny:
            return deny
        task = str(call.get("task", "")).strip()
        if not task:
            return False, "[add_task] 缺少 task"
        ok = self._exec.sandbox.add_pending_task(task)
        if ok:
            return True, f"[add_task] 已添加: '{task}'"
        return False, "[add_task] 添加失败"

    def complete_task(self, call):
        deny = self._reviewer_deny("complete_task")
        if deny:
            return deny
        task = str(call.get("task", "")).strip()
        if not task:
            return False, "[complete_task] 缺少 task"
        ok = self._exec.sandbox.move_task_to_completed(task)
        if ok:
            return True, f"[complete_task] 已标记完成: '{task}'"
        return False, "[complete_task] 未找到任务"

    # ========== 细粒度代码编辑工具 ==========
    def edit_file(self, call):
        deny = self._reviewer_deny("edit_file")
        if deny:
            return deny
        fn = (str(call.get("filename", "") or "")).strip()
        old_text = call.get("old_text", "") or ""
        new_text = call.get("new_text", "") or ""
        if not fn:
            return False, "[edit_file] 缺少 filename"
        if not old_text:
            return False, "[edit_file] 缺少 old_text"
        # 安全校验
        safe, err = _sanitize_rel_path(self._exec.sandbox, fn, True)
        if safe is None:
            return False, f"[edit_file] {err}: {fn}"
        rel = self._normalized_rel(safe)
        # 写入前再次验证路径，防止 TOCTOU 竞态（符号链接在检查后被修改）
        try:
            safe_resolved = os.path.realpath(str(safe))
            work_dir_resolved = os.path.realpath(str(self._exec.sandbox.work_dir))
            if not safe_resolved.startswith(work_dir_resolved + os.sep) and safe_resolved != work_dir_resolved:
                return False, f"[edit_file] 路径验证失败（TOCTOU 防护）: {fn}"
        except OSError:
            return False, f"[edit_file] 路径解析失败: {fn}"
        content = self._read_raw(rel)
        if not isinstance(content, str):
            return False, f"[edit_file] 读取失败: {rel}"
        if old_text not in content:
            return False, "[edit_file] old_text 未在文件中找到"
        occurrences = content.count(old_text)
        if occurrences > 1:
            return False, "[edit_file] old_text 出现 " + str(occurrences) + " 次，请提供更多上下文"

        # 确认钩子 (on_edit_apply_request) — fail-closed：回调异常时拒绝修改
        if getattr(self._exec, "on_edit_apply_request", None):
            try:
                new_content = content.replace(old_text, new_text, 1)
                diff_text = self.make_unified_diff(content, new_content, rel)
                ok = bool(self._exec.on_edit_apply_request(rel, diff_text))
                if not ok:
                    return False, f"[edit_file] 用户拒绝修改 {rel}"
            except Exception as _exc:
                logger.warning("edit_file 确认回调异常，拒绝修改: %s", _exc, exc_info=True)
                return False, f"[edit_file] 确认回调异常: {_exc}"

        new_content = content.replace(old_text, new_text, 1)
        ok = self._exec.sandbox.write_file(rel, new_content)
        if ok:
            # M-018: 审计日志（文件修改）
            logger.info(
                "[AUDIT] edit_file: %s (old_len=%d, new_len=%d)",
                _sanitize_audit_text(rel),
                len(old_text),
                len(new_text),
            )
            # 通知LSP文件已变更
            try:
                lsp_tools = getattr(self._exec, "lsp_tools", None)
                if lsp_tools:
                    lsp_tools.notify_file_changed(str(safe), is_new_file=False)
            except Exception:
                pass
            return True, f"[edit_file] 已修改 {rel}"
        return False, f"[edit_file] 写入失败: {rel}"

    def insert_lines(self, call):
        deny = self._reviewer_deny("insert_lines")
        if deny:
            return deny
        fn = (str(call.get("filename", "") or "")).strip()
        after_line_raw = call.get("after_line", None)
        content = call.get("content", "") or ""
        if not fn:
            return False, "[insert_lines] 缺少 filename"
        # P1-1 修复: 使用 _safe_int(strict=True) 严格验证行号，拒绝布尔值
        after_line = _safe_int(after_line_raw, strict=True)
        if after_line is None:
            return False, "[insert_lines] after_line 必须是整数"
        after_line = after_line - 1
        safe, err = _sanitize_rel_path(self._exec.sandbox, fn, True)
        if safe is None:
            return False, f"[insert_lines] {err}: {fn}"
        rel = self._normalized_rel(safe)
        # 写入前再次验证路径，防止 TOCTOU 竞态（符号链接在检查后被修改）
        try:
            safe_resolved = os.path.realpath(str(safe))
            work_dir_resolved = os.path.realpath(str(self._exec.sandbox.work_dir))
            if not safe_resolved.startswith(work_dir_resolved + os.sep) and safe_resolved != work_dir_resolved:
                return False, f"[insert_lines] 路径验证失败（TOCTOU 防护）: {fn}"
        except OSError:
            return False, f"[insert_lines] 路径解析失败: {fn}"
        old = self._read_raw(rel)
        if not isinstance(old, str):
            return False, f"[insert_lines] 读取失败: {rel}"
        lines = old.splitlines(keepends=True)
        # 允许 after_line=-1（在最前面插入）以及 after_line==len(lines)（在末尾追加），
        # 修复空文件/首行插入时 "行号超出范围" 的回归。
        if after_line < -1 or after_line > len(lines):
            return False, "[insert_lines] 行号超出范围"
        insert = content if content.endswith("\n") else content + "\n"
        new_content = "".join(lines[:after_line + 1]) + insert + "".join(lines[after_line + 1:])
        ok = self._exec.sandbox.write_file(rel, new_content)
        if ok:
            # M-018: 审计日志（文件修改）
            logger.info(
                "[AUDIT] insert_lines: %s at line %d",
                _sanitize_audit_text(rel),
                after_line + 1,
            )
            # 通知LSP文件已变更
            try:
                lsp_tools = getattr(self._exec, "lsp_tools", None)
                if lsp_tools:
                    lsp_tools.notify_file_changed(str(safe), is_new_file=False)
            except Exception:
                pass
            return True, f"[insert_lines] 在 {rel}:{str(after_line + 1)} 处插入 {str(len(content.splitlines()))} 行"
        return False, "[insert_lines] 写入失败"

    def delete_lines(self, call):
        deny = self._reviewer_deny("delete_lines")
        if deny:
            return deny
        fn = (str(call.get("filename", "") or "")).strip()
        try:
            from_line_raw = call.get("from_line", 0)
            to_line_raw = call.get("to_line", None)
            if to_line_raw is None:
                return False, "[delete_lines] 缺少 to_line"
            from_line = _safe_int(from_line_raw, strict=True)
            to_line = _safe_int(to_line_raw, strict=True)
            if from_line is None or to_line is None:
                return False, "[delete_lines] from_line/to_line 必须是整数"
            from_line = from_line - 1
        except (TypeError, ValueError):
            return False, "[delete_lines] from_line/to_line 必须是整数"
        if not fn:
            return False, "[delete_lines] 缺少 filename"
        safe, err = _sanitize_rel_path(self._exec.sandbox, fn, True)
        if safe is None:
            return False, f"[delete_lines] {err}: {fn}"
        rel = self._normalized_rel(safe)
        # 写入前再次验证路径，防止 TOCTOU 竞态（符号链接在检查后被修改）
        try:
            safe_resolved = os.path.realpath(str(safe))
            work_dir_resolved = os.path.realpath(str(self._exec.sandbox.work_dir))
            if not safe_resolved.startswith(work_dir_resolved + os.sep) and safe_resolved != work_dir_resolved:
                return False, f"[delete_lines] 路径验证失败（TOCTOU 防护）: {fn}"
        except OSError:
            return False, f"[delete_lines] 路径解析失败: {fn}"
        old = self._read_raw(rel)
        if not isinstance(old, str) or old.startswith("[读取失败]"):
            return False, f"[delete_lines] {old}"
        lines = old.splitlines(keepends=True)
        if from_line < 0 or to_line > len(lines) or from_line >= to_line:
            return False, "[delete_lines] 行号超出范围或 from_line >= to_line"
        new_content = "".join(lines[:from_line] + lines[to_line:])
        ok = self._exec.sandbox.write_file(rel, new_content)
        if ok:
            # M-018: 审计日志（文件修改）
            logger.info(
                "[AUDIT] delete_lines: %s lines %d-%d",
                _sanitize_audit_text(rel),
                from_line + 1,
                to_line,
            )
            # 通知LSP文件已变更
            try:
                lsp_tools = getattr(self._exec, "lsp_tools", None)
                if lsp_tools:
                    lsp_tools.notify_file_changed(str(safe), is_new_file=False)
            except Exception:
                pass
            return True, f"[delete_lines] 已删除 {rel}:{str(from_line + 1)}-{str(to_line)}"
        return False, "[delete_lines] 写入失败"

    def replace_in_files(self, call):
        """批量替换：在 work 目录下按 file_mask 匹配文件后批量替换 find -> replace"""
        deny = self._reviewer_deny("replace_in_files")
        if deny:
            return deny
        find = call.get("find", "") or ""
        replace = call.get("replace", "") or ""
        mask = ((call.get("file_mask", "") or "*").strip() or "*")
        if not find:
            return False, "[replace_in_files] 缺少 find"
        if len(find) > 65536 or len(replace) > 65536:
            return False, "[replace_in_files] find/replace 文本过长"
        if "\x00" in find or "\x00" in replace or "\x00" in mask:
            return False, "[replace_in_files] 非法空字符"

        # 遍历 — 仅遍历 work 目录内容（受 sandbox 管控）
        base = str(self._safe_work_root())
        modified = []
        failed = []
        total = 0
        try:
            all_files = self._exec.sandbox.list_work_files()
        except Exception:
            all_files = []
        for f_info in all_files:
            rel = f_info.get("path") or ""
            full_path = f_info.get("full_path") or ""
            try:
                if not full_path or not os.path.isfile(full_path):
                    continue
                try:
                    base_real = os.path.realpath(base)
                    fp_real = os.path.realpath(full_path)
                    if not fp_real.startswith(base_real + os.sep):
                        continue
                except Exception as _exc:
                    logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
                # 文件名白名单限制：只处理 mask 匹配
                if not fnmatch.fnmatch(rel, mask) and not fnmatch.fnmatch(os.path.basename(rel), mask):
                    continue
                # 文件大小限制（避免超大文件替换 DoS）
                try:
                    sz = os.path.getsize(full_path)
                    if sz > 4 * 1024 * 1024:
                        failed.append(f"  ! {rel} (文件过大，跳过)")
                        continue
                except Exception as _exc:
                    logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
                try:
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                except Exception as e:
                    failed.append(f"  ! {rel} (读取失败: {str(e)})")
                    continue
                if find not in content:
                    continue
                new_content = content.replace(find, replace)
                # diff 确认（对每个待修改文件都确认）
                if getattr(self._exec, "on_edit_apply_request", None):
                    try:
                        diff_text = self.make_unified_diff(content, new_content, rel)
                        ok = bool(self._exec.on_edit_apply_request(rel, diff_text))
                        if not ok:
                            failed.append(f"  ! {rel} (用户拒绝)")
                            continue
                    except Exception as e:
                        failed.append(f"  ! {rel} (确认回调异常: {str(e)})")
                        continue
                try:
                    ok = self._exec.sandbox.write_file(rel, new_content)
                    if not ok:
                        failed.append(f"  ! {rel} (写入失败)")
                        continue
                except Exception as e:
                    failed.append(f"  ! {rel} (写入失败: {str(e)})")
                    continue
                # 通知LSP文件已修改
                try:
                    lsp_tools = getattr(self._exec, "lsp_tools", None)
                    if lsp_tools:
                        safe, _ = _sanitize_rel_path(self._exec.sandbox, rel, True)
                        if safe:
                            lsp_tools.notify_file_changed(str(safe), is_new_file=False)
                except Exception:
                    pass
                count = content.count(find)
                total += count
                modified.append(f"  > {rel} ({count} 处)")
                if len(modified) >= 256:
                    break
            except Exception as e:
                failed.append(f"  ! {rel} (异常: {str(e)})")
                continue
        if not modified and not failed:
            return True, f'[replace_in_files] 未找到 "{find}" (mask={mask})'
        # M-018: 审计日志（批量文件修改）
        logger.info(
            "[AUDIT] replace_in_files: find=%s mask=%s modified=%d total=%d",
            _sanitize_audit_text(find),
            _sanitize_audit_text(mask),
            len(modified),
            total,
        )
        msg = f"[replace_in_files] 修改 {len(modified)} 个文件，共替换 {total} 处"
        if failed:
            msg += f"，失败 {len(failed)} 个"
        msg += ":\n\n" + "\n".join(modified[:80])
        if failed:
            msg += "\n\n失败:\n" + "\n".join(failed[:20])
        return True, msg

    def rename_symbol(self, call):
        deny = self._reviewer_deny("rename_symbol")
        if deny:
            return deny
        old = (str(call.get("old_name", "") or "")).strip()
        new = (str(call.get("new_name", "") or "")).strip()
        if not old or not new:
            return False, "[rename_symbol] 缺少 old_name/new_name"
        if len(old) > 128 or len(new) > 128:
            return False, "[rename_symbol] old/new 名称过长"
        if not re.match(r"^[\w]+$", old) or not re.match(r"^[\w]+$", new):
            return False, "[rename_symbol] 名称只能是字母/数字/下划线"
        if "\x00" in old or "\x00" in new:
            return False, "[rename_symbol] 名称含非法空字符"

        base = self._safe_work_root()
        try:
            all_files = self._exec.sandbox.list_work_files()
        except Exception:
            all_files = []
        pattern = re.compile(r"\b" + re.escape(old) + r"\b")
        modified = []
        failed = []
        total = 0
        for f_info in all_files:
            rel = f_info.get("path") or ""
            full_path = f_info.get("full_path") or ""
            try:
                if not full_path or not os.path.isfile(full_path):
                    continue
                # 二次校验：文件必须真正落在 work 目录下
                try:
                    base_real = os.path.realpath(base)
                    fp_real = os.path.realpath(full_path)
                    if not fp_real.startswith(base_real + os.sep):
                        continue
                except Exception as _exc:
                    logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
                try:
                    sz = os.path.getsize(full_path)
                    if sz > 4 * 1024 * 1024:
                        failed.append(f"  ! {rel} (文件过大，跳过)")
                        continue
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                try:
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                except Exception as _exc:
                    logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
                if old not in content:
                    continue
                new_content = pattern.sub(new, content)
                if new_content == content:
                    continue
                try:
                    ok = self._exec.sandbox.write_file(rel, new_content)
                    if not ok:
                        failed.append(f"  ! {rel} (写入失败)")
                        continue
                except Exception as e:
                    failed.append(f"  ! {rel} (写入失败: {str(e)})")
                    continue
                # 通知LSP文件已修改
                try:
                    lsp_tools = getattr(self._exec, "lsp_tools", None)
                    if lsp_tools:
                        lsp_tools.notify_file_changed(str(full_path), is_new_file=False)
                except Exception:
                    pass
                count = len(pattern.findall(content))
                total += count
                modified.append(f"  > {rel} ({count} 处)")
                if len(modified) >= 256:
                    break
            except Exception as _exc:
                logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
        if not modified and not failed:
            return True, f"[rename_symbol] 未在任何文件中找到 '{old}'"
        # M-018: 审计日志（批量重命名符号）
        logger.info(
            "[AUDIT] rename_symbol: %s -> %s modified=%d total=%d",
            _sanitize_audit_text(old),
            _sanitize_audit_text(new),
            len(modified),
            total,
        )
        msg = f"[rename_symbol] 修改 {len(modified)} 个文件，共替换 {total} 处"
        if failed:
            msg += f"，失败 {len(failed)} 个"
        msg += ":\n\n" + "\n".join(modified[:80])
        if failed:
            msg += "\n\n失败:\n" + "\n".join(failed[:20])
        return True, msg

    # ========== 代码搜索工具 ==========
    def search_code(self, call):
        pattern = (call.get("pattern", "") or "").strip()
        if not pattern:
            return False, "[search_code] 缺少 pattern"
        ext_raw = (call.get("extension", "") or "").strip()
        extensions = None
        if ext_raw:
            extensions = []
            for e in ext_raw.split(","):
                e = e.strip().lstrip(".")
                if e:
                    extensions.append("." + e.lower())
        context = _safe_int(call.get("context", 2), default=2)
        hits = []
        total_files = 0
        try:
            all_files = self._exec.sandbox.list_work_files()
        except Exception:
            all_files = []
        for f_info in all_files:
            total_files += 1
            full_path = f_info.get("full_path")
            rel = f_info.get("path") or ""
            if not full_path:
                continue
            if extensions:
                _, ext = os.path.splitext(rel)
                if ext.lower() not in extensions:
                    continue
            try:
                if os.path.getsize(full_path) > 4 * 1024 * 1024:
                    continue
            except Exception as _exc:
                logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    f_lines = f.readlines()
            except Exception as _exc:
                logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
            for i, line in enumerate(f_lines):
                if pattern not in line:
                    continue
                start = max(0, i - context)
                end = min(len(f_lines), i + context + 1)
                snippet = []
                for j in range(start, end):
                    marker = " >> " if j == i else "    "
                    snippet.append(marker + str(j + 1) + ": " + f_lines[j].rstrip())
                hits.append("  > " + rel + ":" + str(i + 1) + "\n" + "\n".join(snippet))
        if not hits:
            return True, "[search_code] 扫描 " + str(total_files) + " 个文件，未找到 '" + pattern + "'"
        result = "\n\n".join(hits[:50])
        suffix = ""
        if len(hits) > 50:
            suffix = "\n... 共 " + str(len(hits)) + " 处，仅显示前 50 条"
        return True, "[search_code] pattern='" + pattern + "'\n扫描 " + str(total_files) + " 个文件，" + str(len(hits)) + " 处匹配\n\n" + result + suffix

    def find_symbol(self, call):
        symbol = (call.get("symbol", "") or "").strip()
        if not symbol:
            return False, "[find_symbol] 缺少 symbol"
        results = []
        try:
            all_files = self._exec.sandbox.list_work_files()
        except Exception:
            all_files = []
        for f_info in all_files:
            full_path = f_info.get("full_path") or ""
            rel = f_info.get("path") or ""
            try:
                if not full_path or os.path.getsize(full_path) > 4 * 1024 * 1024:
                    continue
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
            except Exception as _exc:
                logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
            if full_path.endswith(".py"):
                try:
                    tree = ast.parse(content)
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            if node.name == symbol:
                                results.append("  > " + rel + ":" + str(node.lineno) + " function " + node.name)
                        elif isinstance(node, ast.ClassDef):
                            if node.name == symbol:
                                results.append("  > " + rel + ":" + str(node.lineno) + " class " + node.name)
                except (SyntaxError, MemoryError, RecursionError) as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            else:
                for i, line in enumerate(content.splitlines(), 1):
                    if symbol not in line:
                        continue
                    if re.search(r"(def|function|fn|func)\s+" + re.escape(symbol), line):
                        results.append("  > " + rel + ":" + str(i) + " " + line.strip()[:120])
                    elif re.search(r"class\s+" + re.escape(symbol), line):
                        results.append("  > " + rel + ":" + str(i) + " " + line.strip()[:120])
                    elif re.search(r"\b" + re.escape(symbol) + r"\s*[=:]", line):
                        results.append("  > " + rel + ":" + str(i) + " " + line.strip()[:120])
        if not results:
            return True, "[find_symbol] 未找到 symbol='" + symbol + "'"
        body = "\n".join(results[:80])
        return True, "[find_symbol] symbol='" + symbol + "'\n找到 " + str(len(results)) + " 处:\n\n" + body

    def find_files(self, call):
        pat = ((call.get("glob", "") or "*").strip() or "*")
        results = []
        try:
            all_files = self._exec.sandbox.list_work_files()
        except Exception:
            all_files = []
        for f_info in all_files:
            rel = f_info.get("path") or ""
            size = f_info.get("size", 0) or 0
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(os.path.basename(rel), pat):
                results.append("  " + rel + " (" + str(size) + " B)")
        if not results:
            return True, "[find_files] 未找到匹配 '" + pat + "'"
        body = "\n".join(results[:200])
        more = "" if len(results) <= 200 else "\n... 共 " + str(len(results)) + " 个"
        return True, "[find_files] glob='" + pat + "'\n共 " + str(len(results)) + " 个文件:\n\n" + body + more

    def find_markers(self, call, marker):
        results = []
        try:
            all_files = self._exec.sandbox.list_work_files()
        except Exception:
            all_files = []
        for f_info in all_files:
            full_path = f_info.get("full_path")
            rel = f_info.get("path") or ""
            if not full_path:
                continue
            try:
                if os.path.getsize(full_path) > 4 * 1024 * 1024:
                    continue
            except Exception as _exc:
                logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if marker in line:
                            results.append("  > " + rel + ":" + str(i) + " " + line.strip()[:160])
            except Exception as _exc:
                logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
        if not results:
            return True, "[find_" + marker.lower() + "] 项目中无 '" + marker + "'"
        body = "\n".join(results[:120])
        return True, "[find_" + marker.lower() + "] 共 " + str(len(results)) + " 条:\n\n" + body

    # ========== 快照工具 ==========
    def create_snapshot(self, call):
        name = call.get("name", "") or call.get("description", "")
        if not name:
            return False, "[create_snapshot] 缺少 name 参数"
        snap = self._exec.sandbox.snapshot(name)
        return True, ("[create_snapshot] 已创建: '" + str(snap.get("name")) + "' " + "(id=" + str(snap.get("id")) + ")")

    def restore_snapshot(self, call):
        sid = call.get("snapshot_id", "") or call.get("id", "") or call.get("name", "")
        if not sid:
            return False, "[restore_snapshot] 缺少 snapshot_id"
        ok, msg = self._exec.sandbox.restore(sid)
        return ok, "[restore_snapshot] " + msg

    def list_snapshots(self):
        snaps = self._exec.sandbox.list_snapshots()
        if not snaps:
            return True, "[list_snapshots] 暂无快照"
        lines = ["[list_snapshots] 共 " + str(len(snaps)) + " 个:"]
        for s in snaps:
            title = str(s.get("title") or s.get("name") or s.get("id", "?"))
            sid = str(s.get("id", "?"))[:12]
            lines.append("  [" + title + "] id=" + sid)
        return True, "\n".join(lines)

    def undo_last(self, call):
        try:
            n = int(call.get("n", 1) or 1)
        except (TypeError, ValueError):
            n = 1
        # 防御: n=0 时不做任何操作但也不报错
        if n <= 0:
            return True, "[undo_last] n<=0, 无操作"
        ok, msg = self._exec.sandbox.undo(n)
        return ok, "[undo_last] " + msg

    @staticmethod
    def make_unified_diff(old_text: str, new_text: str, filename: str = "") -> str:
        """生成 unified diff 字符串，供 GUI 确认钩子使用。"""
        a_lines = old_text.splitlines(keepends=True)
        b_lines = new_text.splitlines(keepends=True)
        diff = difflib.unified_diff(
            a_lines, b_lines,
            fromfile="a/" + (filename or "file"),
            tofile="b/" + (filename or "file"),
        )
        return "".join(diff)
