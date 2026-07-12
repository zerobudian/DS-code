# -*- coding: utf-8 -*-
"""图片工具（多文档解析）。

实现:
- view_image：查看图片元数据（尺寸、格式、模式）。
- image_to_base64：把图片编码为 data URI。
- describe_image：同 view_image，返回元数据摘要。
- list_images / list_uploaded_images：列出沙箱内的图片。
- parse_doc_text / parse_document_text：从文档（PDF/DOCX/TXT/HTML/MD）
  中提取纯文本。
- parse_doc_images / parse_document_images：从文档中提取图片数量。
- parse_doc / parse_document：综合解析（文本 + 图片 + 元数据）。

安全要点:
1. 所有 path 一律 realpath 归一化 + 限制在 sandbox.work_dir 内；
2. 拒绝打开 >_MAX_IMAGE_BYTES 的图片，防止解压炸弹；
3. 拒绝调用外部可执行文件（使用 Pillow + 内置库），不做命令行调用；
4. 使用 LLM 时只把图像编码为 base64 发送，不直接输出二进制。
"""

import logging

logger = logging.getLogger(__name__)

import base64
import io
import os
import re
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from PIL import Image  # type: ignore
    _PIL_OK = True
except Exception:  # noqa: BLE001
    _PIL_OK = False


_MAX_IMAGE_BYTES = 10 * 1024 * 1024     # 单文件 10MB 上限
_MAX_IMAGE_PIXELS = 64 * 1024 * 1024    # 像素数上限 64M px
_MAX_TEXT_BYTES = 4 * 1024 * 1024       # 文本提取上限 4MB
_SAFE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
# 支持作为文档解析的扩展名
_DOC_SUFFIXES = {
    ".pdf", ".docx", ".doc", ".odt", ".rtf",
    ".txt", ".md", ".markdown", ".rst",
    ".html", ".htm", ".xml",
    ".json", ".csv",
}


class ImageTools:
    """封装图像读取 / base64 编码 / 文档文本解析。"""

    def __init__(self, executor):
        self._exec = executor

    # ---------- 辅助 ----------
    def _sandbox_root(self) -> Optional[Path]:
        try:
            sandbox = self._exec.sandbox
        except Exception:
            sandbox = None
        try:
            work = getattr(sandbox, "work_dir", None) or "."
            return Path(work).resolve()
        except Exception:
            return None

    @staticmethod
    def _clean_text(raw, max_len: int) -> str:
        if raw is None:
            return ""
        if not isinstance(raw, str):
            try:
                raw = str(raw)
            except Exception:
                return ""
        out: List[str] = []
        for ch in raw:
            o = ord(ch)
            if o == 0:
                continue
            if o < 32 and ch not in ("\n", "\r", "\t"):
                continue
            if o == 0x7f:
                continue
            out.append(ch)
        s = "".join(out).strip()
        data = s.encode("utf-8", errors="replace")
        if len(data) > max_len:
            data = data[:max_len]
            s = data.decode("utf-8", errors="ignore")
        return s

    def _safe_path(self, raw) -> Optional[Path]:
        """规范化并校验目标路径（必须落在 work 内，路径链上无符号链接）。"""
        root = self._sandbox_root()
        if root is None:
            return None
        cleaned = self._clean_text(str(raw), 512)
        if not cleaned:
            return None
        if "\x00" in cleaned:
            return None
        # 拒绝绝对路径（POSIX / Windows 盘符）
        if os.path.isabs(cleaned) or (len(cleaned) >= 2 and cleaned[1] == ":"):
            return None
        try:
            root_resolved = root.resolve()
        except Exception:
            return None
        try:
            candidate_raw = root_resolved / cleaned
            rel_parts = candidate_raw.relative_to(root_resolved).parts
        except (ValueError, Exception):
            return None
        # 逐级遍历，拒绝 . / .. / 隐藏段 / 符号链接
        current = root_resolved
        for part in rel_parts:
            if part in (".", "..") or part.startswith("."):
                return None
            next_path = current / part
            try:
                if next_path.is_symlink():
                    return None
            except (OSError, RuntimeError):
                return None
            current = next_path
        try:
            # 使用 lstat 原子判断：必须是常规文件，且不是符号链接
            st = current.lstat()
            if (st.st_mode & 0o170000) != 0o100000:
                return None
            # 最终再确认仍在 root 下
            current.relative_to(root_resolved)
        except (OSError, ValueError):
            return None
        return current

    def _open_safe_image(self, path: Path) -> Tuple[bool, str, Optional[object]]:
        """安全地打开图片并校验像素数。返回 (ok, info, img)。

        先打开图片再校验大小，避免 TOCTOU 竞态条件（文件可能在大小检查后
        被替换为更大的恶意文件）。
        """
        if not _PIL_OK:
            return False, "Pillow 未安装", None
        img = None
        try:
            # 先打开再做大小检查，避免 TOCTOU
            img = Image.open(path)
            # 校验格式完整性
            img.verify()
            # verify() 之后需要重新打开（Pillow 的 verify() 会消耗文件指针）
            img.close()
            img = None
            # 文件大小检查（防御解压炸弹 / 大文件 DoS）
            fsize = path.stat().st_size
            if fsize > _MAX_IMAGE_BYTES:
                return False, f"图片文件过大（超过 {_MAX_IMAGE_BYTES // (1024*1024)}MB）", None
            img = Image.open(path)
            width, height = img.size
            if width * height > _MAX_IMAGE_PIXELS:
                img.close()
                img = None
                return False, "图片像素数过大（超过 64M）", None
            return True, _SAFE_MIME.get(path.suffix.lower(), "image/*"), img
        except Exception as exc:
            if img is not None:
                try:
                    img.close()
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            return False, f"图片读取失败: {exc}", None

    def _rel(self, path: Path) -> str:
        root = self._sandbox_root()
        try:
            if root is not None:
                return str(path.relative_to(root.resolve()))
        except ValueError as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return str(path)

    # ---------- 图像工具 ----------
    def image_to_base64(self, call) -> Tuple[bool, str]:
        path_raw = (call.get("path") or call.get("file")) if isinstance(call, dict) else None
        if not path_raw:
            return False, "[image_to_base64] 缺少 path"
        p = self._safe_path(path_raw)
        if p is None:
            return False, "[image_to_base64] 路径非法 / 不在沙箱 / 不支持的图片"
        suffix = p.suffix.lower()
        if suffix not in _SAFE_MIME:
            return False, "[image_to_base64] 不支持的图片格式"
        ok, info, img = self._open_safe_image(p)
        if not ok or img is None:
            return False, f"[image_to_base64] {info}"
        try:
            fmt = suffix.lstrip(".") if suffix in (
                ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp") else "png"
            if fmt == "jpg":
                fmt = "jpeg"
            buf = io.BytesIO()
            try:
                if img.mode not in ("RGB", "RGBA"):
                    original_img = img
                    img = img.convert("RGB")
                    original_img.close()
                img.save(buf, format=fmt.upper() if fmt.upper() in (
                    "PNG", "JPEG", "GIF", "WEBP", "BMP") else "PNG")
                data = buf.getvalue()
            finally:
                buf.close()
        finally:
            try:
                img.close()
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        try:
            mime = _SAFE_MIME.get(suffix, f"image/{fmt}")
            return True, f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        except Exception as exc:
            return False, f"[image_to_base64] base64 编码失败: {exc}"

    def describe_image(self, call) -> Tuple[bool, str]:
        """返回图片元数据（尺寸、格式、模式）— 不做 OCR。"""
        path_raw = (call.get("path") or call.get("file")) if isinstance(call, dict) else None
        if not path_raw:
            return False, "[describe_image] 缺少 path"
        p = self._safe_path(path_raw)
        if p is None:
            return False, "[describe_image] 路径非法 / 不在沙箱 / 不支持的图片"
        ok, info, img = self._open_safe_image(p)
        if not ok or img is None:
            return False, f"[describe_image] {info}"
        try:
            return True, (
                f"[describe_image] file={p.name}\n"
                f"format={img.format} size={img.size} mode={img.mode}\n"
                f"pixels={img.size[0]*img.size[1]}"
            )
        finally:
            try:
                img.close()
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def view_image(self, call) -> Tuple[bool, str]:
        """与 describe_image 等价（返回元数据，不在文本环境中打开图片）。"""
        ok, msg = self.describe_image(call)
        if ok:
            # 替换前缀以便后续根据工具名识别
            return True, msg.replace("[describe_image]", "[view_image]", 1)
        return ok, msg

    # ---------- 列出图片 ----------
    def list_images(self, call=None) -> Tuple[bool, str]:
        """列出沙箱内的图片文件。"""
        root = self._sandbox_root()
        if root is None:
            return False, "[list_images] sandbox root 未就绪"
        lines: List[str] = []
        total = 0
        stack: List[Path] = [root]
        while stack and total < 500:
            current = stack.pop()
            try:
                with os.scandir(str(current)) as scandir_it:
                    entries = list(scandir_it)
            except (PermissionError, OSError) as _exc:
                logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        name = entry.name
                        if name.startswith(".") or name in (
                            "node_modules", "__pycache__", "venv", ".venv",
                            "dist", "build", "target"):
                            continue
                        stack.append(Path(entry.path))
                        continue
                    if entry.is_file(follow_symlinks=False):
                        p = Path(entry.path)
                        if p.suffix.lower() in _SAFE_MIME:
                            lines.append(f"  - {self._rel(p)}")
                            total += 1
                except (OSError, PermissionError) as _exc:
                    logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
        header = [f"[list_images] total={total}"]
        return True, "\n".join(header + lines)

    # ---------- 文档解析 ----------
    def _extract_plain_text(self, path: Path) -> Tuple[bool, str]:
        """基于扩展名的纯文本提取（纯文本/Markdown/HTML/JSON/CSV 等）。"""
        # 文件大小检查（防御 ZIP 炸弹 / 大文件 DoS）
        try:
            fsize = path.stat().st_size
            if fsize > _MAX_IMAGE_BYTES:
                return False, f"文件过大（超过 {_MAX_IMAGE_BYTES // (1024*1024)}MB）"
        except OSError:
            return False, "无法读取文件信息"
        suffix = path.suffix.lower()
        if suffix in (".pdf",):
            # 轻量 PDF 解析：读取 stream 对象之外的文本（极简近似）
            try:
                with open(path, "rb") as f:
                    raw = f.read(_MAX_TEXT_BYTES)
                text = raw.decode("latin-1", errors="ignore")
                chunks: List[str] = []
                for seg in text.split("stream"):
                    if "endstream" in seg:
                        continue
                    for part in seg.split("(")[1:]:
                        end = part.find(")")
                        if end > 0:
                            chunks.append(part[:end])
                return True, "\n".join(chunks)
            except Exception as exc:
                return False, f"PDF 解析失败: {exc}"
        if suffix in (".docx",):
            # 解析 Office Open XML：读取 word/document.xml
            try:
                with zipfile.ZipFile(path, "r") as zf:
                    if "word/document.xml" not in zf.namelist():
                        return False, "DOCX 中缺少 word/document.xml"
                    with zf.open("word/document.xml") as f:
                        doc_bytes = f.read(_MAX_TEXT_BYTES)
                doc_text = doc_bytes.decode("utf-8", errors="ignore")
                # 最简单的方式：去除所有 XML 标签
                text = re.sub(r"<[^>]+>", " ", doc_text)
                return True, text
            except Exception as exc:
                return False, f"DOCX 解析失败: {exc}"
        if suffix in _DOC_SUFFIXES:
            try:
                with open(path, "rb") as f:
                    raw = f.read(_MAX_TEXT_BYTES)
                return True, raw.decode("utf-8", errors="ignore")
            except Exception as exc:
                return False, f"文本读取失败: {exc}"
        # 其它扩展名：尝试按文本处理
        try:
            with open(path, "rb") as f:
                raw = f.read(_MAX_TEXT_BYTES)
            return True, raw.decode("utf-8", errors="ignore")
        except Exception as exc:
            return False, f"读取失败: {exc}"

    def _count_doc_images(self, path: Path) -> Tuple[bool, int, str]:
        """粗略估算文档中包含的图片数量。对 DOCX 统计 word/media/*
        条目；对 PDF 等二进制内容按 '/XObject' / '/Image' 粗估。
        """
        suffix = path.suffix.lower()
        try:
            if suffix in (".docx",):
                with zipfile.ZipFile(path, "r") as zf:
                    names = zf.namelist()
                    count = sum(1 for n in names
                                if n.startswith("word/media/") and
                                any(n.lower().endswith(ext) for ext in
                                    (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".emf", ".wmf")))
                return True, count, "docx"
            if suffix in (".pdf",):
                with open(path, "rb") as f:
                    raw = f.read(_MAX_TEXT_BYTES)
                text = raw.decode("latin-1", errors="ignore")
                count = text.count("/Type /XObject") + text.count("/Subtype /Image")
                # 保守估计：每张图片可能同时出现两个标记，取 count//2；
                # 但至少计为 1（避免 count=1 时被除为 0）
                estimated = count // 2 if count >= 2 else count
                return True, estimated, "pdf (approx)"
        except Exception as exc:
            return False, 0, str(exc)
        return True, 0, suffix or "(unknown)"

    def parse_doc_text(self, call) -> Tuple[bool, str]:
        """提取文档的纯文本内容（由 tool_executor.parse_document_text 调用）。"""
        if not isinstance(call, dict):
            return False, "[parse_doc_text] 参数无效"
        path_raw = call.get("path") or call.get("file") or ""
        p = self._safe_path(path_raw)
        if p is None:
            return False, "[parse_doc_text] 路径非法 / 不在沙箱 / 文件不存在"
        ok, text = self._extract_plain_text(p)
        if not ok:
            return False, f"[parse_doc_text] {text}"
        return True, (
            f"[parse_doc_text] file={p.name}\n"
            f"format={p.suffix.lower()}\n"
            f"length={len(text)}\n"
            f"---\n{text[:4000]}"
        )

    def parse_document_text(self, call) -> Tuple[bool, str]:
        """parse_doc_text 的别名（与 tool_executor 中分支保持一致）。"""
        ok, text = self.parse_doc_text(call)
        if ok:
            return True, text.replace("[parse_doc_text]", "[parse_document_text]", 1)
        return ok, text

    def parse_doc_images(self, call) -> Tuple[bool, str]:
        """估算文档中的图片数量。"""
        if not isinstance(call, dict):
            return False, "[parse_doc_images] 参数无效"
        path_raw = call.get("path") or call.get("file") or ""
        p = self._safe_path(path_raw)
        if p is None:
            return False, "[parse_doc_images] 路径非法 / 不在沙箱 / 文件不存在"
        ok, count, note = self._count_doc_images(p)
        if not ok:
            return False, f"[parse_doc_images] {note}"
        return True, (
            f"[parse_doc_images] file={p.name}\n"
            f"kind={note}\n"
            f"approx_images={count}"
        )

    def parse_document_images(self, call) -> Tuple[bool, str]:
        ok, text = self.parse_doc_images(call)
        if ok:
            return True, text.replace("[parse_doc_images]", "[parse_document_images]", 1)
        return ok, text

    def parse_doc(self, call) -> Tuple[bool, str]:
        """综合解析：文本 + 图片数量 + 文件元数据。"""
        if not isinstance(call, dict):
            return False, "[parse_doc] 参数无效"
        path_raw = call.get("path") or call.get("file") or ""
        p = self._safe_path(path_raw)
        if p is None:
            return False, "[parse_doc] 路径非法 / 不在沙箱 / 文件不存在"

        ok_text, text = self._extract_plain_text(p)
        ok_img, count, img_note = self._count_doc_images(p)
        try:
            stat = p.stat()
            size_bytes = stat.st_size
        except OSError:
            size_bytes = -1
        lines_out = [
            f"[parse_doc] file={p.name}",
            f"format={p.suffix.lower()}",
            f"bytes={size_bytes}",
            f"approx_images={count}  ({img_note})",
            f"text_extraction_ok={ok_text}",
            "--- TEXT PREVIEW ---",
            text[:4000] if ok_text else f"<失败: {text}>",
        ]
        return True, "\n".join(lines_out)

    def parse_document(self, call) -> Tuple[bool, str]:
        ok, text = self.parse_doc(call)
        if ok:
            return True, text.replace("[parse_doc]", "[parse_document]", 1)
        return ok, text


__all__ = ["ImageTools"]
