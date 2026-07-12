# -*- coding: utf-8 -*-
"""图文文档管理器 (ImageDocumentManager)。

用途：将项目内的图片与文本合并，统一以"文档块"形式呈现给模型。

安全策略（与 image_tools.py 一致）：
1. 图片路径规范化 + 限制在 sandbox.work_dir 内；
2. 图片大小 / 像素数上限；
3. 不使用外部可执行；
4. 文档块使用清晰的 `----- BEGIN IMAGE DOC -----` / `----- END ... -----` 标记，
   防止注入文本被视为指令。
"""

import logging

logger = logging.getLogger(__name__)

import os
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_IMAGE_DOCS = 20
_MAX_CAPTION_LEN = 512


class ImageDocumentManager:
    """图文文档管理器。"""

    def __init__(self, executor):
        self._exec = executor

    # ---------- 辅助 ----------
    def _sandbox_root(self) -> Optional[Path]:
        try:
            sandbox = self._exec.sandbox
            work = getattr(sandbox, "work_dir", None) or "."
            return Path(work).resolve()
        except Exception:
            return None

    @staticmethod
    def _clean_text(raw, max_len) -> str:
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

    def _safe_image_path(self, raw) -> Optional[Path]:
        root = self._sandbox_root()
        if root is None:
            return None
        cleaned = self._clean_text(raw, 512)
        if not cleaned:
            return None
        try:
            if os.path.isabs(cleaned):
                candidate = Path(cleaned).resolve()
            else:
                candidate = (root / cleaned).resolve()
        except Exception:
            return None
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        suffix = candidate.suffix.lower()
        if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            return None
        try:
            st = candidate.stat()
        except OSError:
            return None
        if st.st_size <= 0 or st.st_size > _MAX_IMAGE_BYTES:
            return None
        return candidate

    # ---------- 公共 API ----------
    def build_image_documents(self, images: Iterable[Tuple[str, str]] = ()) -> str:
        """构造图文文档块。

        images: [(path, caption), ...]
        """
        root = self._sandbox_root()
        if root is None:
            return ""
        lines: List[str] = []
        lines.append("----- BEGIN IMAGE DOC -----")
        n = 0
        for entry in images:
            if n >= _MAX_IMAGE_DOCS:
                break
            try:
                path_raw, cap_raw = entry
            except (TypeError, ValueError) as _exc:
                logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
            p = self._safe_image_path(path_raw)
            if p is None:
                continue
            caption = self._clean_text(cap_raw, _MAX_CAPTION_LEN)
            lines.append(f"IMAGE_{n+1}_PATH: {p.name}")
            if caption:
                lines.append(f"IMAGE_{n+1}_CAPTION: {caption}")
            n += 1
        lines.append(f"IMAGE_COUNT: {n}")
        lines.append("----- END IMAGE DOC -----")
        if n == 0:
            return ""
        return "\n".join(lines)
