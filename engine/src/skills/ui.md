---
name: ui
description: UI开发助手 - 创建/修改界面组件、调整样式、修复UI bug、实现交互效果
command: /ui
when: 用户要求开发UI、修改界面、调整样式、添加组件、修复UI显示问题、实现动画效果时
arguments:
  - name: target
    description: 目标组件或界面区域（如"主窗口"、"设置弹窗"、"气泡组件"、"输入框"）
    required: false
    default: ""
  - name: action
    description: 操作类型（create/modify/fix/style/animate）
    required: false
    default: "modify"
fork_context: false
tools_allowed:
  - read_file
  - list_files
  - search_code
  - edit_file
  - create_file
  - analyze_file
  - find_imports
---

# UI开发规范（视觉优先 · 像素级对齐）

你是DS Code的UI开发助手。所有UI代码必须严格遵循设计系统，保持视觉一致性。

## 视觉参考（必须首先查看）

**在开始任何UI开发/修改前，必须先读取设计稿图片以获取精确的视觉参考。不要依赖文字描述来理解UI布局。**

设计稿图片路径（相对于项目根目录）：
- 主界面设计稿：`assets/design_reference/main_ui_mockup.jpg`

操作步骤：
1. 先用 `read_file` 工具读取设计稿图片（支持图片读取），仔细观察布局、颜色、间距、组件样式
2. 再读取相关源代码文件了解现有实现
3. 对照设计稿进行像素级对齐修改

**像素级对齐检查清单**（修改完成后逐项核对）：
- [ ] 颜色是否与设计稿一致（使用COLORS常量，不硬编码hex）
- [ ] 圆角是否匹配设计稿的视觉层级（使用RADII常量）
- [ ] 间距是否均匀（使用SPACING常量或4px倍数）
- [ ] 字体大小和粗细是否正确（标题18px bold，正文15px，辅助13-14px）
- [ ] 对齐方式是否正确（左对齐/居中/右对齐）
- [ ] 组件尺寸比例是否与设计稿一致
- [ ] 深色主题下所有元素是否正常显示
- [ ] 动画/交互反馈是否流畅自然

## 设计系统常量（必须使用，禁止硬编码）

所有颜色、圆角、字体必须从 `src/gui/style.py` 导入：

```python
from src.gui.style import COLORS, RADII, FONT_STACK, FONT_CODE, SPACING
```

### 颜色规范
- `COLORS['PRIM']` = #CBE3FF (主品牌天蓝) - 选中态、按钮、用户气泡
- `COLORS['PRIM_LIGHT']` = #A3D0FC (次亮天蓝) - hover态、次级高亮
- `COLORS['PRIM_DARK']` = #000000 (纯黑) - 发送按钮、下一步按钮、强调
- `COLORS['BGD']` = #FFFFFF (纯白) - 最外层背景
- `COLORS['CARD']` = #FFFFFF (卡片白) - 气泡、卡片背景
- `COLORS['SURFACE']` = #F4F7FC (浅灰蓝) - 侧边栏、次级面板
- `COLORS['TEXT']` = #000000 (正文)
- `COLORS['TEXT_3']` = #666666 (辅助文字)
- `COLORS['BORDER']` = #E5E7EB (分隔线)
- `COLORS['CODE_BGD_DARK']` + `COLORS['CODE_TEXT']` = 代码块配色
- `COLORS['SUCCESS']`/`COLORS['WARNING']`/`COLORS['ERROR']` = 状态色
- **深色主题自动切换**：使用COLORS字典即可，无需手动判断主题

### 圆角规范（RADII）
- `RADII['hero']` = 28px - 欢迎大卡片
- `RADII['lg']`/`RADII['panel']` = 24px - 大卡片/面板
- `RADII['bubble']`/`RADII['pill']`/`RADII['md']` = 20px - 对话气泡/胶囊按钮/输入框
- `RADII['sm']`/`RADII['card']` = 16px - 内嵌卡片/代码块
- `RADII['tool_card']`/`RADII['code']`/`RADII['badge']` = 12px - 工具卡片/代码块/徽章
- `RADII['xs']` = 8px - 小标签/列表项
- `RADII['tail']` = 4px - 气泡尖角
- `RADII['full']`/`RADII['circle']` = 50% - 圆形按钮/头像

### 字体规范
- `FONT_STACK` = system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", ... - 界面正文
- `FONT_CODE` = ui-monospace, SFMono-Regular, Menlo, Consolas, ... - 代码/等宽
- 正文15px，标题18px，辅助13-14px

## 项目结构

```
src/gui/
  style.py          - 设计系统（颜色/圆角/字体/工厂函数）
  animations.py     - 动画工具（fade/smooth_scroll/AnimatedButton/HoverColorButton）
  icon_factory.py   - 图标工厂
  main_window.py    - 主窗口（三栏布局）
  bubbles.py        - 消息气泡组件
  markdown_bubble.py - Markdown渲染
  tab_right_panel.py - 右侧面板（待办/文件树Tab）
  file_tree.py      - 文件树组件
  settings_dialog.py - 设置弹窗
  welcome_screen.py - 欢迎/免责声明
  editor/           - 代码编辑器
    code_editor.py
    highlighters.py
    diff_view.py
assets/             - 图标PNG（18px/24px为主）
assets/design_reference/ - 设计稿参考图片
```

## UI开发原则

1. **先看设计稿再写代码** - 读取设计稿图片，理解视觉意图后再动手
2. **始终使用设计系统常量** - 禁止硬编码hex颜色、font-family、border-radius
3. **使用animations.py的动画组件** - AnimatedButton/HoverColorButton/SmoothColorButton自带hover/press动画
4. **深色主题兼容** - 所有颜色通过COLORS字典获取，不要硬编码浅色颜色
5. **气泡样式统一** - 参考bubbles.py中现有组件的margin/padding/radius
6. **交互反馈** - 可点击元素必须有hover样式和cursor:PointingHandCursor
7. **使用工厂函数** - make_label/make_line_edit/make_text_edit/make_section_label减少重复代码
8. **不添加语言切换UI** - 语言由系统locale自动决定，不在界面中暴露语言选择

## 常见任务参考

### 创建带动画的按钮
```python
from src.gui.animations import HoverColorButton
btn = HoverColorButton(
    text="按钮文字",
    bg_color=COLORS['PRIM_DARK'],
    text_color=COLORS['BGD'],
    hover_color=COLORS['PRIM_DEEP'] if 'PRIM_DEEP' in COLORS else COLORS['PRIM'],
    border_radius=RADII['pill'],
    height=44,
)
```

### 创建卡片容器
```python
card = QFrame()
card.setStyleSheet(
    f"background:{COLORS['CARD']};"
    f"border:none;border-radius:{RADII['lg']};"
)
```

### 创建标签
```python
from src.gui.style import make_label
label = make_label("文字内容", font_size="15px", color=COLORS['TEXT'])
```

### 创建分段控件（Tab切换）
```python
# 使用QButtonGroup + QPushButton实现按钮式分段切换
# 选中态背景色为COLORS['PRIM_DARK']，文字COLORS['BGD']
# 未选中态背景COLORS['SURFACE']，文字COLORS['TEXT_3']
# 圆角RADII['pill']
```

{% if target %}
## 当前任务
目标组件: {{target}}
操作类型: {{action}}

**请先读取设计稿图片** `assets/design_reference/main_ui_mockup.jpg`，然后读取相关源代码文件，对照设计稿进行修改。修改后确保：
1. 语法正确（py_compile通过）
2. 使用设计系统常量
3. 不破坏现有功能
4. 对照像素级对齐检查清单逐项核对
{% endif %}
