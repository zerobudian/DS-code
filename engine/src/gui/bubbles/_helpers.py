# -*- coding: utf-8 -*-
"""Bubbles 共用工具函数与常量：图片加载、颜色变暗、动态问候语。

从原 src/gui/bubbles.py 顶部拆出。注意：ASSETS_DIR 路径已根据本模块
所在层级（src/gui/bubbles/_helpers.py）调整为向上三级，仍指向项目根 assets。
"""
from __future__ import annotations

import os
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

from src.gui.icon_factory import get_icon

# 统一复用 _gui_helpers 中的 _darken，避免多处分发重复定义
from src.gui._gui_helpers import _darken  # noqa: F401  re-exported for backward compatibility

# 本模块位于 src/gui/bubbles/_helpers.py，需向上三级到达项目根目录的 assets
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "assets")

def _img(name: str) -> str:
    return os.path.join(ASSETS_DIR, name)


def _pixel_pm(path: str, size: int) -> QPixmap:
    pm = QPixmap(path)
    if pm.isNull():
        return QPixmap()
    return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _try_pm(names, size: int) -> QPixmap:
    """按文件名列表依次尝试加载，返回第一个成功的 QPixmap。

    若文件名形如 <icon_name>_<size>.png 且 icon_factory 支持该图标，
    则优先回退到统一图标工厂生成，保证风格一致。
    """
    for n in names:
        pm = _pixel_pm(_img(n), size)
        if not pm.isNull():
            return pm
        # 尝试用 icon_factory 生成统一风格图标
        if n.endswith(".png"):
            base = n[:-4]
            parts = base.rsplit("_", 1)
            if len(parts) == 2 and parts[1].isdigit():
                icon_name, icon_size = parts[0], int(parts[1])
                pm = get_icon(icon_name, icon_size)
                if not pm.isNull():
                    return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return QPixmap()


# ======================================================================
# 动态问候语 (用于 WelcomeCard)
# ======================================================================
GREETINGS = {
    "zh": {
        "morning": [  # 06:00 - 12:00
            ("早安！", "我是 DS Code，今天写点什么？"),
            ("你好，早起的鸟儿。", "我是 DS Code，随时准备写代码。"),
            ("早上好！", "我是 DS Code。新的一天，新的代码。"),
            ("又是一个早晨。", "我是 DS Code，今天想做点什么？"),
            ("嘿，早安！", "我是 DS Code，你想先处理什么任务？"),
        ],
        "afternoon": [  # 12:00 - 18:00
            ("你好，", "我是 DS Code。下午好，写点什么好呢？"),
            ("下午好！", "我是 DS Code。有文件要分析？有新功能要写？"),
            ("嗨！", "我是 DS Code。这个时间点写代码最爽。"),
            ("欢迎回来。", "我是 DS Code，随时待命。"),
            ("嘿，你好！", "我是 DS Code。先看一下你之前做了什么？"),
        ],
        "evening": [  # 18:00 - 23:00
            ("晚上好，", "我是 DS Code。加班写代码的时间到了。"),
            ("你好，", "我是 DS Code。晚上效率最高，一起来？"),
            ("嘿，夜猫子。", "我是 DS Code。有什么要处理的？"),
            ("晚餐愉快！", "我是 DS Code。吃完记得回来写代码哦。"),
            ("晚上好。", "我是 DS Code。今晚想做点什么？"),
        ],
        "night": [  # 23:00 - 06:00
            ("你好，夜猫子 🦉", "我是 DS Code。这么晚还在写代码？"),
            ("嘿，深夜党！", "我是 DS Code。要不我们快点写完去睡觉？"),
            ("夜深了。", "我是 DS Code，陪你把这行代码写完。"),
            ("你好，", "我是 DS Code。这个点还在奋斗，辛苦了。"),
            ("还没睡？", "我是 DS Code，随时可以开始。"),
        ],
    },
    "en": {
        "morning": [
            ("Good morning!", "I'm DS Code. What should we build today?"),
            ("Hey early bird,", "I'm DS Code. Got something fun to write?"),
            ("Morning!", "I'm DS Code. Fresh code, fresh ideas."),
            ("Rise and shine.", "I'm DS Code. Let's make something work."),
            ("Hi there!", "I'm DS Code. What's on your mind this morning?"),
        ],
        "afternoon": [
            ("Hello,", "I'm DS Code. Got files to review or features to add?"),
            ("Afternoon!", "I'm DS Code. Let's keep the momentum going."),
            ("Hi!", "I'm DS Code. Got a bug to squash? Or a new file to write?"),
            ("Welcome back.", "I'm DS Code. Standing by."),
            ("Hey there!", "I'm DS Code. Need me to look at something?"),
        ],
        "evening": [
            ("Good evening,", "I'm DS Code. Working late? I've got you."),
            ("Hello,", "I'm DS Code. Evening sessions hit different."),
            ("Hey night owl.", "I'm DS Code. What needs doing?"),
            ("Enjoying dinner?", "I'm DS Code. Don't leave the code waiting!"),
            ("Evening.", "I'm DS Code. What's the plan for tonight?"),
        ],
        "night": [
            ("Hello night owl 🦉", "I'm DS Code. Burning the midnight oil?"),
            ("Hey late night coder!", "I'm DS Code. Let's ship this so you can sleep."),
            ("It's getting late.", "I'm DS Code. One more function, I promise."),
            ("Hi,", "I'm DS Code. Still going strong — respect the grind."),
            ("Still up?", "I'm DS Code. Let's get it done."),
        ],
    },
    "wenyan": {
        "morning": [
            ("晨", "吾乃 DS Code，今日欲写何物？"),
            ("早起佳", "吾乃 DS Code，新日新意，新代码。"),
            ("晨好！", "吾乃 DS Code，今日欲谋何事？"),
            ("朝露未晞", "吾乃 DS Code，卿欲何作？"),
        ],
        "afternoon": [
            ("午后", "吾乃 DS Code，此时编程，最是惬意。"),
            ("日中", "吾乃 DS Code，有何可效之劳？"),
            ("嗨", "吾乃 DS Code，有文件欲察，或有新功能欲书？"),
            ("归否？", "吾乃 DS Code，随时候命。"),
        ],
        "evening": [
            ("暮", "吾乃 DS Code，夜作代码，其乐何如？"),
            ("夜将至", "吾乃 DS Code，今夕欲作何事？"),
            ("晚来好。", "吾乃 DS Code，有何可助？"),
            ("黄昏之际", "吾乃 DS Code，共写数行代码，何如？"),
        ],
        "night": [
            ("夜猫子 🦉", "吾乃 DS Code，夜深矣，尚在写代码？"),
            ("夜将半", "吾乃 DS Code，趁早完工，去睡何如？"),
            ("夜何其深", "吾乃 DS Code，与此代码同行。"),
            ("仍未眠？", "吾乃 DS Code，随时候命。"),
        ],
    },
}

GREETING_HINTS = {
    "zh": "在下方输入你想构建的内容，我将为你编写代码、分析文件、执行命令。",
    "en": "Type what you want to build below. I'll write code, analyze files, and run commands for you.",
    "wenyan": "于下方书所欲作，吾将为君写代码、察文件、发命令。",
}


def _time_slot(hour: int) -> str:
    """返回 morning / afternoon / evening / night"""
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    return "night"


def pick_greeting(language: str = "zh"):
    """随机挑一条问候语，返回 (line1, line2, hint)"""
    import datetime as _dt
    import random as _rand
    lang = language if language in GREETINGS else "zh"
    slot = _time_slot(_dt.datetime.now().hour)
    pool = GREETINGS[lang].get(slot, GREETINGS["zh"]["afternoon"])
    line1, line2 = pool[_rand.randrange(len(pool))]
    return line1, line2, GREETING_HINTS.get(lang, GREETING_HINTS["zh"])
