# -*- coding: utf-8 -*-
"""颜色 / 圆角 / 间距 / 字体 / 样式工厂 / 状态色 — 设计系统底层。

从原 src/gui/style.py 拆分而来（仅做代码组织，不改变任何公开 API）。
设计规格遵循用户提供的 UI 重构文档 · 极简像素风。
"""
# ======================================================================
# 浅色主题（默认）
#
# 实际颜色值（已同步验证 2026-07-10）：
#   BGD        = #F8F9FB   —— 整个应用的最外层背景
#   CARD       = #FFFFFF   —— 气泡 / 内嵌卡片 / 白色胶囊
#   CARD_HOVER = #F0F4FA   —— 卡片悬停态微高亮
#   SURFACE    = #F0F2F5   —— 次级面板（侧边栏、卡片外层）
#   PRIM       = #EEF4FF   —— 浅天蓝高亮（选中态/气泡/大面板）
#   PRIM_LIGHT = #DDE9FF   —— 天蓝色次亮（聚焦态）
#   PRIM_DARK  = #0B1F3A   —— 深色强调
#   BLACK      = #000000   —— 纯黑
#   MUTED      = #6B7280   —— 辅助/说明/占位文本
#   BORDER     = #E5E7EB   —— 分隔线（极细）
# ======================================================================
COLORS_LIGHT = {
    "BGD": "#F8F9FB",         # 微灰背景，与CARD拉开层次
    "CARD": "#FFFFFF",        # 白色卡片
    "CARD_HOVER": "#F4F6F8",  # 卡片悬停
    "SURFACE": "#F0F2F5",     # 次级面板（更中性灰，减少蓝调）
    "PRIM": "#EEF4FF",        # 极浅蓝（输入框背景/选中态）
    "PRIM_LIGHT": "#DDE9FF",  # 浅蓝（hover态）
    "PRIM_DEEP": "#3B82F6",   # 中蓝
    "ACCENT": "#3B82F6",      # 主蓝（Tailwind blue-500）
    "ACCENT_HOVER": "#2563EB",
    "ACCENT_PRESSED": "#1D4ED8",
    "PRIM_DARK": "#000000",
    "BLACK": "#000000",
    "MUTED": "#6B7280",
    "TEXT": "#111827",        # 主文字（近黑但柔和）
    "TEXT_2": "#374151",
    "TEXT_3": "#6B7280",
    "TEXT_4": "#9CA3AF",
    "TEXT_5": "#D1D5DB",
    "TEXT_DARK": "#111827",
    "TEXT_ON_ACCENT": "#FFFFFF",  # 主色上的文字
    "BORDER": "#E5E7EB",
    "DIVIDER": "#E5E7EB",
    "THINK_BGD": "#F3F4F6",
    "CODE_BGD": "#F3F4F6",
    "CODE_BGD_DARK": "#1E293B",
    "CODE_TEXT": "#1F2937",
    "SUCCESS": "#10B981",
    "SUCCESS_LIGHT": "#DCFCE7",
    "SUCCESS_DARK": "#059669",
    "WARNING": "#F59E0B",
    "WARNING_LIGHT": "#FEF3C7",
    "WARNING_DARK": "#D97706",
    "ERROR": "#EF4444",
    "ERROR_DARK": "#DC2626",
    "ERROR_LIGHT": "#FEE2E2",
    "INFO": "#3B82F6",
    "INFO_LIGHT": "#DBEAFE",
    "INFO_DARK": "#2563EB",
    "DISABLED": "#E5E7EB",
    "DIFF_ADD": "#DCFCE7",
    "DIFF_ADD_TEXT": "#166534",
    "DIFF_REMOVE": "#FEE2E2",
    "DIFF_REMOVE_TEXT": "#991B1B",
    "DIFF_HEAD": "#FEF3C7",

    # 语义化别名（向后兼容，推荐新业务代码使用）
    "TEXT_PRIMARY": "#111827",
    "TEXT_SECONDARY": "#374151",
    "TEXT_MUTED": "#9CA3AF",
    "BG_CANVAS": "#F8F9FB",
    "FG_ACCENT": "#3B82F6",
}

# ======================================================================
# 深色主题（保持一致的语义 key，对应更深色值）
# ======================================================================
COLORS_DARK = {
    "BGD": "#0F172A",
    "CARD": "#1E293B",
    "CARD_HOVER": "#273449",
    "SURFACE": "#1A2332",
    "PRIM": "#1E3A5F",
    "PRIM_LIGHT": "#2D4F7A",
    "PRIM_DEEP": "#60A5FA",
    "ACCENT": "#60A5FA",
    "ACCENT_HOVER": "#3B82F6",
    "ACCENT_PRESSED": "#2563EB",
    "PRIM_DARK": "#F1F5F9",
    "BLACK": "#F1F5F9",
    "MUTED": "#94A3B8",
    "TEXT": "#F1F5F9",
    "TEXT_2": "#CBD5E1",
    "TEXT_3": "#94A3B8",
    "TEXT_4": "#64748B",
    "TEXT_5": "#334155",
    "TEXT_DARK": "#F1F5F9",
    "TEXT_ON_ACCENT": "#FFFFFF",
    "BORDER": "#273449",
    "DIVIDER": "#273449",
    "THINK_BGD": "#111C33",
    "CODE_BGD": "#0D1829",
    "CODE_BGD_DARK": "#0F172A",
    "CODE_TEXT": "#E2E8F0",
    "SUCCESS": "#34D399",
    "SUCCESS_LIGHT": "#1F3A2E",
    "SUCCESS_DARK": "#6EE7B7",
    "WARNING": "#FBBF24",
    "WARNING_LIGHT": "#3A3A1F",
    "WARNING_DARK": "#FCD34D",
    "ERROR": "#F87171",
    "ERROR_DARK": "#FCA5A5",
    "ERROR_LIGHT": "#3A1F1E",
    "INFO": "#60A5FA",
    "INFO_LIGHT": "#1F2E4A",
    "INFO_DARK": "#93C5FD",
    "DISABLED": "#273449",
    "DIFF_ADD": "#1F3A2E",
    "DIFF_ADD_TEXT": "#86EFAC",
    "DIFF_REMOVE": "#3A1F1E",
    "DIFF_REMOVE_TEXT": "#FCA5A5",
    "DIFF_HEAD": "#3A3A1F",

    # 语义化别名（向后兼容，推荐新业务代码使用）
    "TEXT_PRIMARY": "#F1F5F9",
    "TEXT_SECONDARY": "#CBD5E1",
    "TEXT_MUTED": "#64748B",
    "BG_CANVAS": "#0F172A",
    "FG_ACCENT": "#60A5FA",
}

# 全局默认颜色表（运行时动态更新内容）
# 使用 dict 副本而非直接引用，使 apply_theme 的 clear()+update()
# 不会影响 COLORS_LIGHT/COLORS_DARK 原始模板
COLORS = dict(COLORS_LIGHT)


def get_colors(theme: str) -> dict:
    """按主题名返回颜色表"""
    if theme and theme.lower() == "dark":
        return COLORS_DARK
    return COLORS_LIGHT


# ======================================================================
# 圆角规范（4px 基础网格，语义化命名）
#   xs=4    小标签、checkbox
#   sm=8    按钮、小徽章、代码行内、行项
#   md=12   代码块、内嵌卡片、菜单
#   lg=16   聊天气泡、Tooltip、搜索框
#   xl=20   大卡片（TodoCard/FileTreeCard/StatsCard）
#   '2xl'=24 输入框胶囊、新对话按钮
#   pill=9999 圆形按钮、头像、胶囊标签
#   tail=4  气泡尖角
# ======================================================================
RADII = {
    "xs": "4px",
    "sm": "8px",
    "md": "12px",
    "lg": "16px",
    "xl": "20px",
    "2xl": "24px",
    "pill": "9999px",
    "circle": "50%",
    "full": "50%",
    "bubble": "16px",
    "card": "20px",
    "panel": "20px",
    "tool_card": "12px",
    "badge": "9999px",
    "capsule_outer": "28px",   # 双层胶囊外层（高度 ~80px）
    "capsule_inner": "22px",   # 双层胶囊内层（高度 44px）
    "code": "8px",
    "icon": "8px",
    "tail": "4px",
    "hero": "24px",
}

BORDER = {
    "thin": "1px",
    "thick": "2px",
}

# ======================================================================
# 阴影系统（elevation）
# 格式: (y_offset, blur_radius, alpha)  alpha 为 0-1 浮点数
# sm: 输入框/小按钮  md: 卡片/气泡  lg: 弹出面板/菜单  xl: 模态对话框
# ======================================================================
SHADOWS = {
    "sm": (1, 3, 0.05),
    "md": (2, 10, 0.08),
    "lg": (4, 20, 0.10),
    "xl": (8, 40, 0.12),
}

SPACING = {
    "1": "4px",
    "2": "8px",
    "3": "12px",
    "4": "16px",
    "5": "20px",
    "6": "24px",
    "8": "32px",
    "10": "40px",
}

FONT_STACK = '-apple-system, "SF Pro Display", "SF Pro Text", "PingFang SC", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif'
FONT_CODE = 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Source Code Pro", "Courier New", monospace'


# ======================================================================
# 通用 UI 工厂函数（减少弹窗/对话框之间的重复样式代码）
# ======================================================================

try:
    from PySide6.QtWidgets import QLabel, QLineEdit, QTextEdit, QWidget, QHBoxLayout, QFrame
    _HAS_QT = True
except Exception:
    _HAS_QT = False


# ======================================================================
# P0-2: 样式表缓存系统
# 缓存工厂函数生成的样式表字符串，避免重复拼接 f-string。
# 主题切换时通过 invalidate_style_cache() 清空缓存。
# ======================================================================
_STYLE_CACHE: dict = {}
_STYLE_CACHE_VERSION = 0  # 主题切换时递增，使旧缓存条目失效


def invalidate_style_cache() -> None:
    """主题切换时调用，清空所有缓存的样式表字符串。"""
    global _STYLE_CACHE_VERSION
    _STYLE_CACHE_VERSION += 1
    _STYLE_CACHE.clear()


def _cached_style(key: str, builder):
    """缓存样式表字符串。key 必须包含所有影响样式的参数。"""
    cache_key = (_STYLE_CACHE_VERSION, key)
    cached = _STYLE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    value = builder()
    _STYLE_CACHE[cache_key] = value
    return value


def make_label(text, font_size="14px", weight="400", color=None, align=None, word_wrap=False):
    """创建统一风格的 QLabel。"""
    if not _HAS_QT:
        return None
    lbl = QLabel(text)
    if align is not None:
        lbl.setAlignment(align)
    if word_wrap:
        lbl.setWordWrap(True)
    c = color or COLORS['TEXT']
    ss = _cached_style(
        f"label|{c}|{font_size}|{weight}",
        lambda: f"color:{c};font-size:{font_size};font-weight:{weight};"
                f"font-family:{FONT_STACK};background:transparent;"
    )
    lbl.setStyleSheet(ss)
    return lbl


def make_line_edit(placeholder="", text="", echo=None, height=44,
                   padding="0 14px", font_size="14px"):
    """创建统一风格的单行输入框。"""
    if not _HAS_QT:
        return None
    le = QLineEdit(text)
    if echo is not None:
        le.setEchoMode(echo)
    if placeholder:
        le.setPlaceholderText(placeholder)
    le.setFixedHeight(height)
    ss = _cached_style(
        f"line_edit|{COLORS['CARD']}|{COLORS['TEXT']}|{COLORS['BORDER']}|{RADII['md']}|{padding}|{font_size}|{COLORS['PRIM_DARK']}|{COLORS['PRIM_LIGHT']}",
        lambda: f"QLineEdit{{background:{COLORS['CARD']};color:{COLORS['TEXT']};"
                f"border:1px solid {COLORS['BORDER']};"
                f"border-radius:{RADII['md']};padding:{padding};font-size:{font_size};"
                f"font-family:{FONT_STACK};}}"
                f"QLineEdit:focus{{border:1px solid {COLORS['PRIM_DARK']};}}"
                f"QLineEdit:hover{{border:1px solid {COLORS['PRIM_LIGHT']};}}"
    )
    le.setStyleSheet(ss)
    return le


def make_text_edit(text="", min_height=120):
    """创建统一风格的多行文本输入框。"""
    if not _HAS_QT:
        return None
    te = QTextEdit(text)
    te.setMinimumHeight(min_height)
    ss = _cached_style(
        f"text_edit|{COLORS['CARD']}|{COLORS['TEXT']}|{COLORS['BORDER']}|{RADII['md']}|{COLORS['PRIM_DARK']}",
        lambda: f"QTextEdit{{background:{COLORS['CARD']};color:{COLORS['TEXT']};"
                f"border:1px solid {COLORS['BORDER']};"
                f"border-radius:{RADII['md']};padding:10px 14px;font-size:14px;"
                f"font-family:{FONT_STACK};}}"
                f"QTextEdit:focus{{border:1px solid {COLORS['PRIM_DARK']};}}"
    )
    te.setStyleSheet(ss)
    return te


def make_section_label(text, bar_width=4, bar_height=20, font_size="13px", font_weight="600"):
    """创建带左侧色条的分组小标题。"""
    if not _HAS_QT:
        return None
    row = QHBoxLayout()
    row.setSpacing(8)
    bar = QFrame()
    bar.setFixedWidth(bar_width)
    bar.setFixedHeight(bar_height)
    bar.setStyleSheet(
        f"QFrame{{background:{COLORS['PRIM_DARK']};border-radius:{RADII['sm']};}}"
    )
    row.addWidget(bar)
    lbl = make_label(text, font_size=font_size, weight=font_weight)
    row.addWidget(lbl, 1)
    row.addStretch(0)
    container = QWidget()
    container.setLayout(row)
    return container


# ======================================================================
# 状态色定义 — ToolBubble / ReasoningBubble / 输入元数据 共用
# ======================================================================
def _build_status_colors(colors: dict) -> dict:
    """从当前颜色表派生状态色 map"""
    return {
        "coding": colors["PRIM_DARK"],
        "reading": colors["WARNING"],
        "writing": colors["PRIM_DARK"],
        "done": colors["SUCCESS"],
        "error": colors["ERROR"],
        "terminal": colors["TEXT"],
        "think": colors["TEXT_3"],
        "thinking": colors["TEXT_3"],
    }


STATUS_COLORS = _build_status_colors(COLORS)


def _refresh_status_colors() -> None:
    """主题切换后保持 STATUS_COLORS 与 COLORS 同步（原地更新，不重绑定）"""
    new = _build_status_colors(COLORS)
    STATUS_COLORS.clear()
    STATUS_COLORS.update(new)


_refresh_status_colors()


# ======================================================================
# 带 alpha 的颜色构造 — 供 icon_factory.py 中使用
# ======================================================================
def rgba(hex_color: str, alpha: float = 1.0) -> str:
    """#RRGGBB -> #RRGGBBAA 字符串"""
    try:
        h = hex_color.lstrip("#")
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
        a_v = max(0, min(255, int(alpha * 255)))
        return f"#{r:02X}{g:02X}{b:02X}{a_v:02X}"
    except Exception:
        return hex_color


def qcolor(hex_color: str, alpha: float = 1.0):
    """返回一个 QColor 实例（按需 import PySide6）"""
    try:
        from PySide6.QtGui import QColor
        return QColor(rgba(hex_color, alpha))
    except Exception:
        return hex_color

