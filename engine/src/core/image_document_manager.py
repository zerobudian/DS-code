# -*- coding: utf-8 -*-
"""图片与文档管理器

设计目标:
 1. 用户上传文件: 图片直接转 base64 data URL, 文档(如 PDF/DOCX/TXT)可分别
    - text_only: 提取文字内容
    - image_only: 提取所有插图 (逐页)
    - combined: 同时返回文字 + 图片
 2. 上传的图片会在项目预览目录 (project/.dscode/images/) 存一份副本，
    且用 sha256 做 id，避免重复存储。
 3. 模型在对话中可随时调用「查看图片」工具 —
    ImageDocumentManager 会提供已存储图片的 data URL 让 LLM 重新查看。

调用方式（ChatEngine 内部使用）:
    mgr = ImageDocumentManager(sandbox.root)
    mgr.store_from_data_url("data:image/png;base64,...")  →  image_id
    mgr.get_image_data_url("image_id_or_name")  →  "data:image/png;base64,..."
    mgr.list_images()  →  [{"id", "source", "size_kb", "mime"}]
    mgr.parse_document("/path/to/file.pdf", mode="combined")  →  {text, images:[...]}

给模型暴露的工具 (由 chat_engine 动态注入):
    - view_image(image_id_or_name)  →  把图片 data URL 重新传给模型
    - list_uploaded_images()         →  已上传图片清单
    - parse_document_text(filepath)  →  只提取文档文字
    - parse_document_images(filepath) → 只提取文档图片
    - parse_document(filepath)       → 文字 + 图片
"""

import logging

logger = logging.getLogger(__name__)

import os
import io
import json
import hashlib
import base64
import tempfile
import threading
import datetime
from typing import Optional, List, Dict, Any, Tuple


_TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".py", ".js", ".ts",
    ".json", ".yaml", ".yml", ".csv", ".tsv", ".log", ".ini",
    ".conf", ".cfg", ".xml", ".html", ".htm", ".css", ".scss",
    ".toml", ".sh", ".bat", ".cmd", ".sql", ".php", ".java",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb",
    ".swift", ".kt", ".dart", ".lua", ".vim", ".vimrc",
}

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".tif"}

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}

_MAX_FILE_BYTES = 10 * 1024 * 1024   # 10 MB 单文件上限
_MAX_TEXT_BYTES = 200_000             # 文本截断 200KB
_IMG_PREVIEW_MAX = 10                  # 单个文档最多提取 10 张插图
# C-007: 单个文档提取的所有图片总大小上限，防止批量插图 OOM
_MAX_DOC_EXTRACT_IMAGES_BYTES = 100 * 1024 * 1024  # 100 MB


def _atomic_write_json(path: str, obj: Any) -> None:
    """原子写 JSON — 先写 tmp 文件再 rename"""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json.tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        raise
    finally:
        # 确保 fd 在 fdopen 失败时也被关闭
        # (closefd=False 时由 finally 兜底关闭)
        try:
            os.close(fd)
        except OSError as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)


class ImageDocumentManager:
    """项目级的图片/文档管理器。

    Args:
        sandbox_root: 项目根目录 (Sandbox.root) — <project> 目录，
                       在其下建立 .dscode/images/ 与 .dscode/documents/
    """

    def __init__(self, sandbox_root: str):
        self.root = os.path.abspath(str(sandbox_root))
        self.images_dir = os.path.join(self.root, ".dscode", "images")
        self.docs_dir = os.path.join(self.root, ".dscode", "documents")
        self.index_path = os.path.join(self.images_dir, "index.json")
        self._lock = threading.RLock()

        os.makedirs(self.images_dir, exist_ok=True)
        os.makedirs(self.docs_dir, exist_ok=True)

        # 加载/初始化 index.json
        self._index: Dict[str, Dict[str, Any]] = {}
        self._aliases: Dict[str, str] = {}   # 短名 → image_id
        try:
            if os.path.exists(self.index_path):
                with open(self.index_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self._index = raw.get("images", {}) or {}
                self._aliases = raw.get("aliases", {}) or {}
        except Exception:
            self._index = {}
            self._aliases = {}

        # 顺序数字名 (image_1, image_2 ...) 与 中文别名 (图1, 第一张...)
        self._rebuild_aliases()

    # ---------- 内部工具 ----------
    def _save_index(self) -> None:
        with self._lock:
            try:
                obj = {
                    "images": self._index,
                    "aliases": self._aliases,
                    "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
                }
                _atomic_write_json(self.index_path, obj)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def _rebuild_aliases(self) -> None:
        """根据已存顺序生成短名。index 中本身有 order 字段。"""
        items = sorted(
            self._index.items(),
            key=lambda kv: kv[1].get("order", 0) or 0,
        )
        self._aliases = {}
        for idx, (image_id, meta) in enumerate(items, start=1):
            for alias in (f"image_{idx}", f"img_{idx}", f"图{idx}", f"图片{idx}",
                           f"第{idx}张", f"第{idx}张图"):
                self._aliases[alias] = image_id
            # 第一张 / 最后一张 这种别名
            if idx == 1:
                for alias in ("第一张", "第一张图", "first"):
                    self._aliases[alias] = image_id
            if idx == len(items):
                for alias in ("最后一张", "最近", "latest"):
                    self._aliases[alias] = image_id
            # 文件名做别名
            if meta.get("source_file"):
                base = os.path.basename(meta["source_file"])
                self._aliases[base] = image_id
                self._aliases[os.path.splitext(base)[0]] = image_id

    def _next_order(self) -> int:
        return max([v.get("order", 0) or 0 for v in self._index.values()] or [0]) + 1

    def _to_data_url(self, mime: str, raw_bytes: bytes) -> str:
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        return f"data:{mime};base64,{b64}"

    def _image_size(self, raw_bytes: bytes) -> Tuple[int, int]:
        """返回 (width, height)；失败返回 (0, 0)"""
        try:
            from PIL import Image as _PILImage
            with _PILImage.open(io.BytesIO(raw_bytes)) as im:
                return im.size
        except Exception:
            return (0, 0)

    # ---------- 安全路径校验 ----------
    def _safe_path(self, filepath: str) -> Optional[str]:
        """校验 filepath 必须位于 sandbox_root 下，且路径链上无符号链接。

        返回安全绝对路径；若存在路径穿越、绝对路径、隐藏段或符号链接则返回 None。
        """
        try:
            root = os.path.abspath(self.root)
            raw = str(filepath or "")
            if not raw or "\x00" in raw:
                return None
            # 拒绝绝对路径（POSIX / Windows 盘符）
            if os.path.isabs(raw) or (len(raw) >= 2 and raw[1] == ":"):
                p = os.path.abspath(raw)
            else:
                p = os.path.abspath(os.path.join(root, raw))
            if not p.startswith(root + os.sep) and p != root:
                return None
            rel = os.path.relpath(p, root)
            if rel.startswith(".."):
                return None
            current = root
            for part in rel.split(os.sep):
                if not part or part.startswith(".") or part == "..":
                    return None
                current = os.path.join(current, part)
                try:
                    if os.path.islink(current):
                        return None
                except OSError:
                    return None
            return current
        except Exception:
            return None

    # ---------- 图片存储 ----------
    def store_from_data_url(self, data_url: str, source_doc: str = "direct_upload") -> Optional[str]:
        """存储一张 data URL 图片 → 返回 image_id"""
        try:
            if not data_url or "," not in data_url:
                return None
            header, b64_body = data_url.split(",", 1)
            # data:image/png;base64,xxxx
            mime = "image/png"
            for m in ("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"):
                if m in header.lower():
                    mime = m
                    break
            raw = base64.b64decode(b64_body.encode("ascii"))
            return self._store_raw(raw, mime, source_doc)
        except Exception:
            return None

    def store_from_file(self, filepath: str, source_doc: str = "file_upload") -> Optional[str]:
        """从本地图片文件存储 → image_id"""
        # 安全路径校验（含符号链接沙箱逃逸防护）
        abs_path = self._safe_path(filepath)
        if abs_path is None:
            return None
        try:
            if not os.path.isfile(abs_path):
                return None
            # 文件大小预检：避免 f.read() 把超大文件整体读入内存导致 OOM。
            # _store_raw 内的 _MAX_FILE_BYTES 检查在 read 之后才执行，为时已晚。
            if os.path.getsize(abs_path) > _MAX_FILE_BYTES:
                logger.debug("图片文件过大(>%d字节)，跳过存储: %s", _MAX_FILE_BYTES, abs_path)
                return None
            with open(abs_path, "rb") as f:
                raw = f.read()
        except Exception:
            return None
        ext = os.path.splitext(abs_path)[1].lower()
        mime = _MIME_BY_EXT.get(ext, "image/png")
        return self._store_raw(raw, mime, source_doc)

    def _store_raw(self, raw_bytes: bytes, mime: str, source_doc: str) -> Optional[str]:
        if not raw_bytes or len(raw_bytes) == 0:
            return None
        if len(raw_bytes) > _MAX_FILE_BYTES:
            return None
        image_id = hashlib.sha256(raw_bytes).hexdigest()
        with self._lock:
            if image_id in self._index:
                # 已存在，仅更新 alias
                self._rebuild_aliases()
                return image_id

            ext = ".png"
            for e, m in _MIME_BY_EXT.items():
                if m == mime:
                    ext = e
                    break
            out_path = os.path.join(self.images_dir, f"{image_id}{ext}")
            try:
                with open(out_path, "wb") as f:
                    f.write(raw_bytes)
            except Exception:
                return None
            w, h = self._image_size(raw_bytes)
            self._index[image_id] = {
                "id": image_id,
                "path": out_path,
                "ext": ext,
                "mime": mime,
                "source_doc": source_doc,
                "uploaded_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "size_bytes": len(raw_bytes),
                "width": w,
                "height": h,
                "order": self._next_order(),
            }
            self._rebuild_aliases()
            self._save_index()
            return image_id

    # ---------- 查询 ----------
    def resolve_id(self, image_id_or_name: str) -> Optional[str]:
        """把短名 / id 解析成真实 image_id"""
        if not image_id_or_name:
            return None
        key = str(image_id_or_name).strip()
        with self._lock:
            if key in self._index:
                return key
            if key in self._aliases:
                return self._aliases[key]
            # 模糊匹配 — 用户输入 "image1" (无下划线) 之类
            low = key.lower()
            for k, v in self._aliases.items():
                if low == k.lower():
                    return v
            # 按 id 前缀匹配
            for k in self._index.keys():
                if k.startswith(key):
                    return k
            # 按文件名模糊匹配
            for image_id, meta in self._index.items():
                src = meta.get("source_file") or ""
                if key in src:
                    return image_id
            return None

    def get_image_data_url(self, image_id_or_name: str) -> Optional[str]:
        """取回图片 data URL。供 chat_engine 传给模型。"""
        real_id = self.resolve_id(image_id_or_name)
        if not real_id:
            return None
        meta = self._index.get(real_id)
        if not meta:
            return None
        path = meta.get("path")
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                raw = f.read()
            return self._to_data_url(meta.get("mime", "image/png"), raw)
        except Exception:
            return None

    def get_image_info(self, image_id_or_name: str) -> Optional[Dict[str, Any]]:
        real_id = self.resolve_id(image_id_or_name)
        if not real_id:
            return None
        return dict(self._index.get(real_id, {}))

    def list_images(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = sorted(self._index.values(), key=lambda m: m.get("order", 0) or 0)
            result = []
            for m in items:
                result.append({
                    "id": m.get("id"),
                    "source": m.get("source_doc", ""),
                    "mime": m.get("mime", ""),
                    "size_kb": round((m.get("size_bytes") or 0) / 1024, 1),
                    "width": m.get("width", 0),
                    "height": m.get("height", 0),
                    "uploaded_at": m.get("uploaded_at", ""),
                })
            return result

    # ---------- 文档解析 ----------
    def parse_text(self, filepath: str) -> Dict[str, Any]:
        """text_only: 只提取文字"""
        safe_path = self._safe_path(filepath)
        if safe_path is None:
            return {"ok": False, "text": "", "error": f"路径非法或不在沙箱内: {filepath}"}
        if not os.path.exists(safe_path):
            return {"ok": False, "text": "", "error": f"文件不存在: {filepath}"}
        if os.path.isdir(safe_path):
            return {"ok": False, "text": "", "error": "不支持目录作为文档"}
        try:
            if os.path.getsize(safe_path) > _MAX_FILE_BYTES:
                return {"ok": False, "text": "", "error": f"文件大于 {_MAX_FILE_BYTES // (1024*1024)}MB，无法直接读取"}
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        filepath = safe_path
        ext = os.path.splitext(filepath)[1].lower()

        text = ""
        meta = {"file": filepath, "ext": ext, "mode": "text_only"}

        # 1) 纯文本 — 直接读
        if ext in _TEXT_EXT:
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read(_MAX_TEXT_BYTES)
            except Exception as e:
                return {"ok": False, "text": "", "error": str(e)}

        # 2) PDF — 尝试 pymupdf / pdfplumber / pypdf
        elif ext == ".pdf":
            try:
                try:
                    import fitz  # pymupdf
                    doc = fitz.open(filepath)
                    pages = []
                    for pno, page in enumerate(doc, start=1):
                        pages.append(f"[page {pno}]")
                        pages.append(page.get_text("text") or "")
                        if sum(len(x) for x in pages) > _MAX_TEXT_BYTES:
                            break
                    text = "\n".join(pages)
                    meta["pages"] = len(doc)
                except Exception:
                    try:
                        import pdfplumber
                        pages = []
                        with pdfplumber.open(filepath) as pdf:
                            meta["pages"] = len(pdf.pages)
                            for pno, page in enumerate(pdf.pages, start=1):
                                txt = page.extract_text() or ""
                                pages.append(f"[page {pno}]\n{txt}")
                                if sum(len(x) for x in pages) > _MAX_TEXT_BYTES:
                                    break
                        text = "\n".join(pages)
                    except Exception:
                        try:
                            from pypdf import PdfReader
                            reader = PdfReader(filepath)
                            meta["pages"] = len(reader.pages)
                            pages = []
                            for pno, page in enumerate(reader.pages, start=1):
                                pages.append(f"[page {pno}]\n{page.extract_text() or ''}")
                                if sum(len(x) for x in pages) > _MAX_TEXT_BYTES:
                                    break
                            text = "\n".join(pages)
                        except Exception as e:
                            return {"ok": False, "text": "",
                                    "error": f"未安装 PDF 库 (pymupdf/pdfplumber/pypdf): {e}"}
            except Exception as e:
                return {"ok": False, "text": "", "error": str(e)}

        # 3) DOCX — 尝试 python-docx
        elif ext == ".docx":
            try:
                try:
                    from docx import Document
                    doc = Document(filepath)
                    paras = []
                    for idx, p in enumerate(doc.paragraphs, start=1):
                        paras.append(p.text or "")
                        if sum(len(x) for x in paras) > _MAX_TEXT_BYTES:
                            break
                    text = "\n".join(paras)
                except ImportError as e:
                    return {"ok": False, "text": "", "error": f"未安装 python-docx: {e}"}
            except Exception as e:
                return {"ok": False, "text": "", "error": str(e)}

        # 4) 图片 — 不返回文字，但提示用户可当作图片解析
        elif ext in _IMAGE_EXT:
            return {"ok": True, "text": f"(图片文件: {os.path.basename(filepath)})",
                    "hint": "这是一个图片文件，已自动作为图片上传。",
                    **meta}

        else:
            # 尝试当文本读 — 如果失败则返回错误
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read(_MAX_TEXT_BYTES)
            except Exception as e:
                return {"ok": False, "text": "", "error": f"无法解析此文件类型 {ext}: {e}"}

        if len(text) > _MAX_TEXT_BYTES:
            # UTF-8 安全截断：按字节边界截断，避免破坏多字节字符
            encoded = text.encode("utf-8")
            if len(encoded) > _MAX_TEXT_BYTES:
                truncated = encoded[:_MAX_TEXT_BYTES]
                text = truncated.decode("utf-8", errors="ignore") + "\n...(已截断，超出 200KB)"
        return {"ok": True, "text": text, **meta}

    def parse_images(self, filepath: str) -> Dict[str, Any]:
        """image_only: 只提取文档中的图片 → [{image_id, page, data_url}]"""
        safe_path = self._safe_path(filepath)
        if safe_path is None:
            return {"ok": False, "images": [], "error": f"路径非法或不在沙箱内: {filepath}"}
        if not os.path.exists(safe_path):
            return {"ok": False, "images": [], "error": f"文件不存在: {filepath}"}
        # 文件大小预检：防止超大 PDF/DOCX 导致 OOM
        try:
            if os.path.getsize(safe_path) > _MAX_FILE_BYTES:
                logger.debug("文档文件过大(>%d字节)，跳过图片提取: %s", _MAX_FILE_BYTES, safe_path)
                return {"ok": False, "images": [], "error": f"文件过大(超过{_MAX_FILE_BYTES}字节)"}
        except OSError:
            return {"ok": False, "images": [], "error": "无法读取文件大小"}
        filepath = safe_path
        ext = os.path.splitext(filepath)[1].lower()

        if ext in _IMAGE_EXT:
            # 单图片文件 — 直接存
            image_id = self.store_from_file(filepath, source_doc=os.path.basename(filepath))
            if image_id:
                info = self.get_image_info(image_id) or {}
                data_url = self.get_image_data_url(image_id)
                return {"ok": True,
                        "images": [{"image_id": image_id, "page": 1,
                                    "mime": info.get("mime", ""),
                                    "width": info.get("width"), "height": info.get("height"),
                                    "data_url": data_url}]}
            return {"ok": False, "images": []}

        collected = []
        total_img_bytes = 0
        if ext == ".pdf":
            try:
                import fitz  # pymupdf
                doc = fitz.open(filepath)
                for pno, page in enumerate(doc, start=1):
                    for img_idx, img in enumerate(page.get_images(full=True), start=1):
                        if len(collected) >= _IMG_PREVIEW_MAX:
                            break
                        try:
                            xref = img[0]
                            base = doc.extract_image(xref)
                            image_bytes = base["image"]
                            if total_img_bytes + len(image_bytes) > _MAX_DOC_EXTRACT_IMAGES_BYTES:
                                break
                            total_img_bytes += len(image_bytes)
                            ext_img = base.get("ext", "png")
                            mime = f"image/{ext_img}"
                            image_id = self._store_raw(image_bytes, mime, f"{os.path.basename(filepath)} p{pno}")
                            if image_id:
                                info = self.get_image_info(image_id) or {}
                                data_url = self.get_image_data_url(image_id)
                                collected.append({
                                    "image_id": image_id, "page": pno,
                                    "mime": info.get("mime", mime),
                                    "width": info.get("width"), "height": info.get("height"),
                                    "data_url": data_url,
                                })
                        except Exception as _exc:
                            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                    if len(collected) >= _IMG_PREVIEW_MAX:
                        break
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        elif ext == ".docx":
            try:
                from docx import Document
                doc = Document(filepath)
                for pno, rel in enumerate(doc.part.rels.values(), start=1):
                    if "image" not in (rel.reltype or ""):
                        continue
                    if len(collected) >= _IMG_PREVIEW_MAX:
                        break
                    try:
                        image_bytes = rel.target_part.blob
                        if total_img_bytes + len(image_bytes) > _MAX_DOC_EXTRACT_IMAGES_BYTES:
                            break
                        total_img_bytes += len(image_bytes)
                        image_id = self._store_raw(image_bytes, "image/png",
                                                   f"{os.path.basename(filepath)} img{pno}")
                        if image_id:
                            info = self.get_image_info(image_id) or {}
                            data_url = self.get_image_data_url(image_id)
                            collected.append({
                                "image_id": image_id, "page": pno,
                                "mime": info.get("mime", "image/png"),
                                "width": info.get("width"), "height": info.get("height"),
                                "data_url": data_url,
                            })
                    except Exception as _exc:
                        logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        return {"ok": True, "images": collected}

    def parse_document(self, filepath: str, mode: str = "combined") -> Dict[str, Any]:
        """combined: text + images — 默认组合模式"""
        safe_path = self._safe_path(filepath)
        if safe_path is None:
            return {"ok": False, "file": filepath, "mode": mode,
                    "text": "", "images": [], "meta": {},
                    "error": f"路径非法或不在沙箱内: {filepath}"}
        filepath = safe_path
        result = {
            "ok": True,
            "file": filepath,
            "mode": mode,
            "text": "",
            "images": [],
            "meta": {},
        }
        if mode in ("text_only", "combined"):
            tr = self.parse_text(filepath)
            result["text"] = tr.get("text", "")
            result["meta"]["text"] = tr
            if not tr.get("ok"):
                result["ok"] = False
                result["error"] = tr.get("error")
        if mode in ("image_only", "combined"):
            ir = self.parse_images(filepath)
            result["images"] = ir.get("images", [])
            result["meta"]["images_count"] = len(result["images"])
        return result

    # ---------- 给模型暴露的工具 ----------
    def tool_view_image(self, image_id_or_name: str) -> Dict[str, Any]:
        """模型调用：查看指定图片。返回图片 data URL + 元信息。"""
        real_id = self.resolve_id(image_id_or_name)
        if not real_id:
            return {"ok": False,
                    "error": f"找不到图片: {image_id_or_name}。可用的图片 id/别名见 list_uploaded_images()"}
        data_url = self.get_image_data_url(real_id)
        if not data_url:
            return {"ok": False, "error": f"图片文件已丢失: {image_id_or_name}"}
        info = self.get_image_info(real_id) or {}
        return {
            "ok": True,
            "image_id": real_id,
            "data_url": data_url,
            "mime": info.get("mime", "image/png"),
            "width": info.get("width"),
            "height": info.get("height"),
            "source_doc": info.get("source_doc", ""),
        }

    def tool_list_images(self) -> Dict[str, Any]:
        """模型调用：列出本项目中已上传的所有图片"""
        items = self.list_images()
        if not items:
            return {"ok": True, "images": [],
                    "hint": "目前没有上传的图片。如果用户在消息里粘贴了图片，会自动加入本列表。"}
        return {"ok": True, "images": items, "count": len(items)}


# ---------- 单元测试 ----------
if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ImageDocumentManager(tmp)
        # 构造一张假的 PNG
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        iid = mgr._store_raw(png_bytes, "image/png", "test")
        print(f"stored id: {iid}")
        print(f"list: {mgr.list_images()}")
        print(f"view_image('图1'): {mgr.tool_view_image('图1')}")
        # 纯文本
        p = os.path.join(tmp, "hello.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("你好世界\nfrom DS Code\n")
        print(mgr.parse_document(p, "combined"))
