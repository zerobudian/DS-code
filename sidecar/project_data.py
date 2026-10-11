"""Persistent project metadata, tasks and bounded file attachments."""

import base64
import hashlib
import json
import mimetypes
import re
from urllib.parse import unquote

from src.core.atomic_files import atomic_write_bytes

from .http_protocol import HTTPError

MAX_UPLOAD = 512 * 1024
MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024


def task_snapshot(sandbox):
    tasks = sandbox.get_tasks() if sandbox is not None else []
    revision = hashlib.sha256(json.dumps(tasks, ensure_ascii=False).encode()).hexdigest()
    return {"tasks": tasks, "revision": revision}


def prepare_attachments(sandbox, names):
    images, texts, total = [], [], 0
    for name in names:
        path = sandbox._safe_path(name, False)
        if path is None or not path.is_file():
            raise HTTPError(404, "附件不存在")
        if path.stat().st_size > MAX_UPLOAD:
            raise HTTPError(413, "附件内容过大")
        raw = path.read_bytes()
        total += len(raw)
        if len(raw) > MAX_UPLOAD or total > MAX_ATTACHMENT_BYTES:
            raise HTTPError(413, "附件内容过大")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if mime in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            images.append(f"data:{mime};base64," + base64.b64encode(raw).decode())
            texts.append(f"[图片附件 {name}]")
        else:
            try:
                content = raw.decode("utf-8")
                if "\x00" in content:
                    raise UnicodeError()
            except UnicodeError as exc:
                raise HTTPError(422, "聊天附件支持 UTF-8 文本、PNG/JPEG/GIF/WebP 图片") from exc
            texts.append(f"[附件 {name}]\n{content}\n[附件结束]")
    return "\n\n".join(texts), images


class ProjectDataService:
    def __init__(self, runtime):
        self.runtime = runtime

    def route(self, request):
        parts = request.path[len("/api/projects/"):].split("/")
        if len(parts) > 2:
            raise HTTPError(404, "端点不存在")
        name = self.runtime.canonical(unquote(parts[0]))
        action = parts[1] if len(parts) == 2 else ""
        allowed = {"": {"PUT"}, "tasks": {"GET", "PUT"},
                   "files": {"GET"}, "file": {"GET"}, "uploads": {"POST"}}
        if action not in allowed:
            raise HTTPError(404, "端点不存在")
        if request.method not in allowed[action]:
            raise HTTPError(405, "此端点不支持该方法")
        with self.runtime.lock:
            writing = request.method != "GET"
            if writing:
                self.runtime.require_idle()
            sandbox = self.runtime.projects.get(name)
            if sandbox is None and action in {"tasks", "uploads"} and writing:
                sandbox = self.runtime.projects.create(name)
            if sandbox is None and request.method == "GET":
                if action == "tasks": return task_snapshot(None)
                if action == "files": return {"files": []}
            if sandbox is None:
                raise HTTPError(404, "项目不存在")
            with sandbox._lock:
                return self._route(sandbox, action, request)

    def _route(self, sandbox, action, request):
        if action == "":
            body = request.json()
            if not body or set(body) - {"pinned", "display_name"}:
                raise HTTPError(400, "无效项目字段")
            updates = {}
            if "pinned" in body:
                if type(body["pinned"]) is not bool:
                    raise HTTPError(400, "pinned 必须是布尔值")
                import time
                updates.update(pinned=body["pinned"], pinned_at=time.time() if body["pinned"] else 0)
            if "display_name" in body:
                value = body["display_name"]
                if not isinstance(value, str) or not value.strip() or len(value) > 128:
                    raise HTTPError(400, "无效项目显示名")
                updates.update(display_name=value.strip(), renamed_by_user=True)
            if not sandbox.update_meta(updates):
                raise HTTPError(500, "保存项目失败")
            return {"name": sandbox.name, **sandbox.get_meta()}
        if action == "tasks":
            current = task_snapshot(sandbox)
            if request.method == "PUT":
                body = request.json()
                if body.get("revision") != current["revision"]:
                    raise HTTPError(409, "任务已更新，请刷新后重试")
                if not sandbox.replace_tasks(body.get("tasks")):
                    raise HTTPError(400, "任务格式无效或保存失败")
                current = task_snapshot(sandbox)
                if self.runtime.engine.sandbox is not None and self.runtime.engine.sandbox.name == sandbox.name:
                    self.runtime.engine._files_changed()
            return current
        if action == "files":
            def public(items, area):
                return [{"path": item.get("path", item.get("name")), "size": item.get("size", 0),
                         "area": area} for item in items]
            return {"files": public(sandbox.list_work_files(), "work")
                    + public(sandbox.list_preview_files(), "preview")}
        if action == "file":
            path = request.query.get("path", [""])[0]
            area = request.query.get("area", ["work"])[0]
            if area not in {"work", "preview"}:
                raise HTTPError(400, "无效文件区域")
            target = sandbox._safe_path(path, area == "work")
            if target is None:
                raise HTTPError(400, "文件路径不合法")
            if not target.is_file():
                raise HTTPError(404, "文件不存在")
            if target.stat().st_size > MAX_UPLOAD:
                raise HTTPError(413, "文件过大，无法预览")
            raw = target.read_bytes()
            if len(raw) > MAX_UPLOAD:
                raise HTTPError(413, "文件过大，无法预览")
            mime = mimetypes.guess_type(target.name)[0]
            if mime in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                return {"path": path, "area": area,
                        "data_url": f"data:{mime};base64," + base64.b64encode(raw).decode()}
            try:
                text = raw.decode("utf-8")
                if "\x00" in text:
                    raise UnicodeError()
            except UnicodeError as exc:
                raise HTTPError(422, "此文件不支持文本预览") from exc
            return {"path": path, "area": area, "content": text}
        body = request.json()
        name, encoded = body.get("name"), body.get("data")
        if (not isinstance(name, str) or not re.fullmatch(r"[^/\\\x00-\x1f]{1,128}", name)
                or name.startswith(".") or len(name.encode("utf-8")) > 200 or not isinstance(encoded, str)):

            raise HTTPError(400, "无效附件名称或内容")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise HTTPError(400, "附件编码无效") from exc
        if len(raw) > MAX_UPLOAD:
            raise HTTPError(413, "每个附件最大 512 KiB")
        saved_name = hashlib.sha256(raw).hexdigest()[:16] + "_" + name
        target = sandbox._safe_path(saved_name, False)
        if target is None or not atomic_write_bytes(target, raw):
            raise HTTPError(500, "保存附件失败")
        return {"name": saved_name, "display_name": name, "size": len(raw)}
