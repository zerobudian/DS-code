# -*- coding: utf-8 -*-
"""尺寸 / 间距 / 图标大小 token。

与 _colors.py 对应：所有像素级硬编码尺寸应收敛到此处，业务代码通过
`from src.gui.style import SIZES` 读取，禁止裸写像素数字。
"""

SIZES = {
    # 窗口
    "window": {
        "default_width": 1200,
        "default_height": 800,
        "minimum_width": 960,
        "minimum_height": 600,
    },

    # 侧边栏
    "sidebar": {
        "expanded": 260,
        "collapsed": 56,
        "toggle_btn": 40,
        "compact_new_btn": 36,
        "new_btn_height": 40,
        "new_btn_radius": "pill",
        "item_height": 56,
        "bottom_btn": 36,
        "bottom_radius": "sm",
        "content_margin": 16,
        "icon_md": 18,
        "icon_sm": 16,
    },

    # 输入区
    "input": {
        "card_min_height": 56,
        "area_min_height": 70,
        "area_max_height": 300,
        "min_height": 80,
        "max_height": 350,
        "send_btn": 36,
        "toolbar_btn": 28,
        "attach_btn": 32,
        "model_tag_height": 22,
        "disclaimer_height": 20,
        "token_stats_height": 20,
    },

    # 按钮
    "button": {
        "primary_height": 44,
        "option_height": 36,
        "dialog_height": 40,
        "icon_sm": 16,
        "icon_md": 18,
        "icon_lg": 20,
    },

    # 弹窗 / 对话框
    "popup": {
        "status_width": 520,
        "status_height": 620,
        "dialog_min_width": 900,
        "dialog_min_height": 680,
        "header_height": 72,
        "tab_height": 48,
        "close_btn": 28,
    },

    # 气泡
    "bubble": {
        "max_width": 720,
        "icon": 40,
        "status_icon": 16,
        "arrow": 16,
    },

    # 间距（与 SPACING 字符串字典并行，供代码中需要 int 的场景）
    "spacing": {
        "xs": 4,
        "sm": 8,
        "md": 12,
        "lg": 16,
        "xl": 24,
        "2xl": 32,
    },

    # 图标尺寸（像素）
    "icon_size": {
        "sm": (12, 12),
        "md": (16, 16),
        "lg": (18, 18),
        "xl": (20, 20),
        "2xl": (24, 24),
    },

    # 统计卡片
    "stats_card": {
        "min_width": 320,
        "max_width": 420,
        "cell_height": 56,
    },
}
