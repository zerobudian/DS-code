# -*- coding: utf-8 -*-
"""用户偏好记忆（Honcho 用户画像）。

持久化文件：``~/.config/dscode/user_profile.json``（通过 platformdirs 获取）。
字段：favorite_languages、frameworks、code_style、directory_structure、
     command_preferences、other_notes。

提供 ``update_from_message(message)`` 用简单正则/关键词抽取偏好，
以及 ``get_profile_text()`` 供 system prompt 注入。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Set

try:
    import platformdirs

    _USER_CONFIG_DIR = Path(platformdirs.user_config_dir("dscode", "dscode"))
except Exception:
    _USER_CONFIG_DIR = Path.home() / ".config" / "dscode"

PROFILE_FILE = _USER_CONFIG_DIR / "user_profile.json"

DEFAULT_PROFILE: Dict[str, object] = {
    "favorite_languages": [],
    "frameworks": [],
    "code_style": [],
    "directory_structure": [],
    "command_preferences": [],
    "other_notes": [],
}

_LANGUAGE_KEYWORDS = {
    "python": "Python",
    "py": "Python",
    "javascript": "JavaScript",
    "js": "JavaScript",
    "typescript": "TypeScript",
    "ts": "TypeScript",
    "java": "Java",
    "go": "Go",
    "golang": "Go",
    "rust": "Rust",
    "rs": "Rust",
    "c++": "C++",
    "cpp": "C++",
    "c#": "C#",
    "csharp": "C#",
    "ruby": "Ruby",
    "php": "PHP",
    "swift": "Swift",
    "kotlin": "Kotlin",
    "vue": "Vue",
    "react": "React",
    "html": "HTML",
    "css": "CSS",
    "sql": "SQL",
    "shell": "Shell",
    "bash": "Bash",
}

_FRAMEWORK_KEYWORDS = {
    "django": "Django",
    "flask": "Flask",
    "fastapi": "FastAPI",
    "spring": "Spring",
    "springboot": "Spring Boot",
    "express": "Express",
    "next": "Next.js",
    "nextjs": "Next.js",
    "nuxt": "Nuxt.js",
    "nuxtjs": "Nuxt.js",
    "react": "React",
    "vue": "Vue",
    "angular": "Angular",
    "flutter": "Flutter",
    "pytorch": "PyTorch",
    "tensorflow": "TensorFlow",
    "pytest": "pytest",
    "jest": "Jest",
}

_CODE_STYLE_HINTS = [
    # 中文
    (r"(?:代码风格|风格|规范|缩进|命名) ?[是为：:] ?([^。！\n]{2,80})", "code_style"),
    (r"(?:用|使用) ?(\d) ?空格缩进", "code_style"),
    (r"(?:我喜欢|我要|希望|需要) ?([^。！\n]{2,40}(?:缩进|命名|风格|规范))", "code_style"),
    # 英文
    (r"(?:code style|coding style|style guide)\s*[:=：]\s*([^\.\!\n]{2,80})", "code_style"),
    (r"(?:use|prefer)\s+(\d\s*space|\d\s*spaces)\s+indent", "code_style"),
    (r"I prefer ([^\.\!\n]{2,40}(?:style|indent|naming))", "code_style"),
]

_DIR_STRUCTURE_HINTS = [
    (r"(?:目录结构|项目结构|文件组织) ?[是为：:] ?([^。！\n]{2,100})", "directory_structure"),
    (r"(?:directory structure|folder structure|project structure)\s*[:=：]\s*([^\.\!\n]{2,100})",
     "directory_structure"),
]

_COMMAND_PREFERENCE_HINTS = [
    (r"(?:命令偏好|命令|终端|运行) ?[是为：:] ?([^。！\n]{2,80})", "command_preferences"),
    (r"(?:command preference|run command|terminal)\s*[:=：]\s*([^\.\!\n]{2,80})", "command_preferences"),
    (r"(?:用|使用) ?([^。！\n]{2,30}) ?(?:运行|启动|测试)", "command_preferences"),
]

_OTHER_HINTS = [
    (r"(?:其他|备注|注意|偏好) ?[是为：:] ?([^。！\n]{2,120})", "other_notes"),
    (r"(?:other notes|notes|preference|preferences)\s*[:=：]\s*([^\.\!\n]{2,120})", "other_notes"),
]

_ALL_HINT_PATTERNS = (
    _CODE_STYLE_HINTS + _DIR_STRUCTURE_HINTS + _COMMAND_PREFERENCE_HINTS + _OTHER_HINTS
)


def _clean_text(text: str) -> str:
    return re.sub(r"[。！?\.\!]+$", "", text.strip())


class UserProfileMemory:
    """用户画像记忆：跨项目持久化的用户偏好。"""

    def __init__(self, profile_path: Optional[Path] = None) -> None:
        self.profile_path: Path = Path(profile_path) if profile_path else PROFILE_FILE
        self._lock: threading.RLock = threading.RLock()
        self._profile: Dict[str, List[str]] = {
            k: list(v) for k, v in DEFAULT_PROFILE.items()
        }
        self.load()

    def load(self) -> None:
        """从 JSON 文件加载用户画像。"""
        with self._lock:
            if not self.profile_path.exists():
                return
            try:
                text = self.profile_path.read_text(encoding="utf-8")
                data = json.loads(text)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("读取用户画像失败: %s", exc)
                return
            if not isinstance(data, dict):
                return
            for key in DEFAULT_PROFILE:
                value = data.get(key)
                if isinstance(value, list):
                    self._profile[key] = [str(x).strip() for x in value if str(x).strip()]
                elif isinstance(value, str) and value.strip():
                    self._profile[key] = [value.strip()]

    def save(self) -> bool:
        """保存用户画像到 JSON 文件（原子写入：临时文件 + rename）。"""
        with self._lock:
            try:
                self.profile_path.parent.mkdir(parents=True, exist_ok=True)
                # 原子写入：先写临时文件，再 rename，避免进程崩溃导致文件损坏
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(self.profile_path.parent),
                    prefix=".user_profile_",
                    suffix=".tmp",
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(self._profile, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, str(self.profile_path))
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
                return True
            except OSError as exc:
                logger.warning("写入用户画像失败: %s", exc)
                return False

    def get_profile_text(self) -> str:
        """返回可直接注入 system prompt 的用户画像文本。"""
        with self._lock:
            lines = ["## 用户画像", ""]
            labels = {
                "favorite_languages": "偏好语言",
                "frameworks": "常用框架",
                "code_style": "代码风格",
                "directory_structure": "目录结构偏好",
                "command_preferences": "命令偏好",
                "other_notes": "其他偏好",
            }
            has_any = False
            for key, label in labels.items():
                values = self._profile.get(key, [])
                if values:
                    has_any = True
                    joined = "; ".join(values)
                    lines.append(f"- {label}: {joined}")
            if not has_any:
                return ""
            return "\n".join(lines)

    def update(self, key: str, value: str) -> bool:
        """向指定字段追加一条偏好（去重）。"""
        key = str(key).strip()
        value = _clean_text(str(value).strip())
        if key not in DEFAULT_PROFILE or not value or len(value) < 2:
            return False
        with self._lock:
            existing: Set[str] = {x.lower() for x in self._profile[key]}
            if value.lower() in existing:
                return False
            self._profile[key].append(value)
        return self.save()

    def update_from_message(self, message: str) -> List[str]:
        """从用户消息中抽取偏好并更新，返回被更新的字段名列表。"""
        if not message or not isinstance(message, str):
            return []
        updated: List[str] = []

        # 1. 关键词匹配语言 / 框架
        lowered = message.lower()
        for kw, name in _LANGUAGE_KEYWORDS.items():
            if re.search(rf"\b{re.escape(kw)}\b", lowered):
                if self.update("favorite_languages", name):
                    updated.append("favorite_languages")
        for kw, name in _FRAMEWORK_KEYWORDS.items():
            if re.search(rf"\b{re.escape(kw)}\b", lowered):
                if self.update("frameworks", name):
                    updated.append("frameworks")

        # 2. 正则模式匹配更具体的偏好
        for pattern, field in _ALL_HINT_PATTERNS:
            for m in re.finditer(pattern, message, re.IGNORECASE):
                captured = m.group(1).strip()
                captured = _clean_text(captured)
                if len(captured) < 3:
                    continue
                if self.update(field, captured):
                    updated.append(field)

        return list(dict.fromkeys(updated))  # 去重并保持顺序

    def to_dict(self) -> Dict[str, List[str]]:
        """导出当前画像副本。"""
        with self._lock:
            return {k: list(v) for k, v in self._profile.items()}
