# -*- coding: utf-8 -*-
"""Compatibility shim: re-exports from src.gui.icons package.

All icon generation has been split into the src.gui.icons package.
This file remains for backward-compatible imports:
    from src.gui.icon_factory import get_icon, ensure_icons, _MAKERS, ...
"""
from src.gui.icons._helpers import (  # noqa: F401
    WHALE_BLUE, WHALE_BLUE_LIGHT, ASSETS_DIR, ICONS_18_24, REQUIRED_ICONS,
    _q, _alpha, _begin, _circle_btn_bg, _set_stroke_pen, _set_fill, _center_rect,
)
from src.gui.icons._registry import (  # noqa: F401
    _MAKERS, _MAKER_ALIASES, _resolve_maker, ensure_icons, get_icon,
)
from src.gui.icons._draw import (  # noqa: F401
    draw_bubble, draw_arrow_up_white, draw_arrow_up_black,
    draw_keyboard, draw_terminal, draw_paper,
)
from src.gui.icons._navigation import (  # noqa: F401
    _make_arrow_up, _make_arrow_up_plain, _make_arrow_up_white,
    _make_arrow_up_disabled, _make_arrow_up_black,
    _make_stop, _make_stop_white, _make_stop_black,
    _make_chevron_left, _make_chevron_right, _make_chevron_up, _make_chevron_down,
)
from src.gui.icons._actions import (  # noqa: F401
    _make_paperclip, _make_bubble, _make_keyboard, _make_terminal, _make_paper,
    _make_settings, _make_refresh, _make_close, _make_copy, _make_check,
    _make_plus, _make_minus,
)
from src.gui.icons._status import (  # noqa: F401
    _make_warning, _make_info, _make_search, _make_filter, _make_sort, _make_question,
)
from src.gui.icons._content import (  # noqa: F401
    _make_file, _make_folder, _make_pencil, _make_checkbox_empty, _make_checkbox_checked,
)
from src.gui.icons._project import (  # noqa: F401
    _make_moon, _make_sun, _make_history, _make_memory, _make_model,
    _make_permission, _make_mode, _make_trash, _make_pin, _make_unpin,
)
