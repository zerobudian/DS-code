# -*- coding: utf-8 -*-
"""Lesson Tools — 持久化经验教训记录。

Constitutional requirement: "Store one lesson per file with a one-line summary
at the top. Record corrections and confirmed approaches alike, including why
they mattered. Don't save what the repo or chat history already records; update
an existing note rather than creating a duplicate; delete notes that turn out
to be wrong."

存储格式: .dscode/lessons/<sanitized-title>.md  — 每个文件一个 lesson
"""

import logging

logger = logging.getLogger(__name__)

import os
import re
import tempfile


class LessonTools:
    """记录 / 查询 / 删除 lessons 的工具集。

    sandbox 需要提供:
      - sandbox.work_dir  → 项目根目录
      - sandbox._safe_path(rel) → 路径校验
    """

    _LESSONS_DIR = ".dscode/lessons"
    _MAX_TITLE = 200
    _MAX_CONTENT = 32 * 1024
    _MAX_LESSONS = 200

    def __init__(self, executor):
        self._exec = executor

    # ------------------------------------------------------------------
    # 路径辅助
    # ------------------------------------------------------------------

    def _lessons_root(self) -> str:
        """返回 lessons 的绝对存储目录，不存在则创建."""
        wd = self._exec.sandbox.work_dir
        root = os.path.join(wd, self._LESSONS_DIR)
        try:
            os.makedirs(root, exist_ok=True)
        except OSError as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return root

    @staticmethod
    def _slugify(title: str) -> str:
        """将标题转为安全的文件名."""
        title = title.strip()[:200]
        # 保留中文 / 字母 / 数字 / 空格 / 连字符，去除其他
        slug = re.sub(r"[^\u4e00-\u9fff\u3400-\u4dbfA-Za-z0-9 _\-]", "", title)
        slug = re.sub(r"\s+", "_", slug)
        slug = re.sub(r"_+", "_", slug)
        slug = slug.strip("_")
        if not slug:
            slug = "untitled"
        return slug + ".md"

    # ------------------------------------------------------------------
    # record_lesson
    # ------------------------------------------------------------------

    def record_lesson(self, title: str, content: str, tags: str = "") -> tuple:
        """创建或更新一条 lesson."""
        # —— 防御式类型归一：仅接受 str，其他类型转为空串（或显式拒绝）——
        if isinstance(title, bool):
            title = ""
        elif not isinstance(title, str):
            try:
                title = str(title) if title is not None else ""
            except Exception:
                title = ""
        if isinstance(content, bool):
            content = ""
        elif not isinstance(content, str):
            try:
                content = str(content) if content is not None else ""
            except Exception:
                content = ""
        if isinstance(tags, bool):
            tags = ""
        elif not isinstance(tags, str):
            try:
                tags = str(tags) if tags is not None else ""
            except Exception:
                tags = ""
        title = title.strip()
        content = content.strip()
        if not title:
            return False, "[record_lesson] 缺少 title"
        if not content:
            return False, "[record_lesson] 缺少 content"
        # 拒绝空字符 / 控制字符（防止写入到文件里的内容被污染）
        if "\x00" in title or "\x00" in content:
            return False, "[record_lesson] 参数含非法空字符"

        title = title[:self._MAX_TITLE]
        content = content[:self._MAX_CONTENT]
        tags_str = tags.strip()[:512]

        root = self._lessons_root()
        filename = self._slugify(title)
        filepath = os.path.join(root, filename)

        # 构建带元数据的 markdown
        body_lines = ["# " + title, ""]
        if tags_str:
            body_lines.append("tags: " + tags_str)
            body_lines.append("")
        body_lines.append(content)

        try:
            fd, tmp_path = tempfile.mkstemp(dir=root, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as f:
                    f.write("\n".join(body_lines))
                os.replace(tmp_path, filepath)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                raise
        except OSError as e:
            return False, f"[record_lesson] 写入失败: {e}"

        return True, f"已记录 lesson: {title}"

    # ------------------------------------------------------------------
    # list_lessons
    # ------------------------------------------------------------------

    def list_lessons(self, tag: str = "", search: str = "") -> tuple:
        """列出所有 lessons，支持 tag 过滤和关键词搜索."""
        root = self._lessons_root()
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return True, "(暂无 lessons)"

        results = []
        tag_lower = (tag or "").strip().lower()
        search_lower = (search or "").strip().lower()

        for name in names:
            if not name.endswith(".md"):
                continue
            fp = os.path.join(root, name)
            try:
                # 安全：跳过符号链接，限制文件大小
                if os.path.islink(fp):
                    continue
                if os.path.getsize(fp) > 65536:  # 64KB 上限
                    continue
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as _exc:
                logger.debug("跳过异常项: %s", _exc, exc_info=True); continue

            # 解析 title
            lines = text.splitlines()
            title = ""
            file_tags = ""
            if lines and lines[0].startswith("# "):
                title = lines[0][2:].strip()
            if not title:
                title = name[:-3].replace("_", " ")

            # 解析 tags
            for line in lines[1:4]:
                if line.startswith("tags:"):
                    file_tags = line[5:].strip()
                    break

            # 过滤 tag
            if tag_lower:
                tag_list = [t.strip().lower() for t in file_tags.split(",") if t.strip()]
                if tag_lower not in tag_list:
                    continue

            # 搜索
            if search_lower:
                if search_lower not in title.lower() and search_lower not in text.lower():
                    continue

            results.append((title, file_tags))

        if not results:
            return True, "(无匹配的 lessons)"

        lines_out = []
        for idx, (t, tgs) in enumerate(results, 1):
            tag_str = f"  [{tgs}]" if tgs else ""
            lines_out.append(f"{idx}. {t}{tag_str}")

        return True, "\n".join(lines_out)

    # ------------------------------------------------------------------
    # delete_lesson
    # ------------------------------------------------------------------

    def delete_lesson(self, title: str) -> tuple:
        """按标题删除一条 lesson."""
        if not title or not isinstance(title, str):
            return False, "[delete_lesson] 缺少 title"

        title = title.strip()[:self._MAX_TITLE]
        root = self._lessons_root()
        filename = self._slugify(title)
        filepath = os.path.join(root, filename)

        if not os.path.isfile(filepath):
            # 尝试模糊匹配
            try:
                for name in os.listdir(root):
                    if not name.endswith(".md"):
                        continue
                    with open(os.path.join(root, name), "r", encoding="utf-8", errors="replace") as f:
                        first_line = f.readline()
                    if first_line.strip() == "# " + title:
                        filepath = os.path.join(root, name)
                        break
                else:
                    return False, f"[delete_lesson] 未找到 lesson: {title}"
            except OSError:
                return False, f"[delete_lesson] 未找到 lesson: {title}"

        try:
            os.remove(filepath)
        except OSError as e:
            return False, f"[delete_lesson] 删除失败: {e}"

        return True, f"已删除 lesson: {title}"