# -*- coding: utf-8 -*-
"""Icons package: icon generation and registry."""

from src.gui.icons._helpers import (
    WHALE_BLUE, WHALE_BLUE_LIGHT, ASSETS_DIR, ICONS_18_24, REQUIRED_ICONS,
    _q, _alpha, _begin, _circle_btn_bg, _set_stroke_pen, _set_fill, _center_rect,
)
from src.gui.icons._registry import (
    _MAKERS, _MAKER_ALIASES, _resolve_maker, ensure_icons, get_icon,
)
from src.gui.icons._draw import (
    draw_bubble, draw_arrow_up_white, draw_arrow_up_black,
    draw_keyboard, draw_terminal, draw_paper,
)

__all__ = [
    "WHALE_BLUE", "WHALE_BLUE_LIGHT", "ASSETS_DIR", "ICONS_18_24", "REQUIRED_ICONS",
    "_MAKERS", "_MAKER_ALIASES", "_resolve_maker", "ensure_icons", "get_icon",
    "draw_bubble", "draw_arrow_up_white", "draw_arrow_up_black",
    "draw_keyboard", "draw_terminal", "draw_paper",
]
