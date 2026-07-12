# -*- coding: utf-8 -*-
"""i18n — 极简翻译系统 (I18N / tr / 内置 zh·en 翻译字典)。

从原 src/gui/style.py 拆分而来（仅做代码组织，不改变任何公开 API）。
"""
import json
import re
from pathlib import Path

# ======================================================================
# i18n — 中文 / English / 文言
# ======================================================================
class I18N:
    """极简翻译系统: tr(key) 返回当前语言的翻译文本。

    使用方式:
        from src.gui.style import tr
        tr("settings")  # → "设置" 或 "Settings"
    """

    TRANSLATIONS = {
        "zh": {
            # ---- 通用 ----
            "app_name": "DS Code",
            "ok": "好的",
            "cancel": "取消",
            "confirm": "确认",
            "close": "关闭",
            "save": "保存",
            "delete": "删除",
            "rename": "重命名",
            "create": "新建",
            "copy": "复制",
            "refresh": "刷新",
            "search": "搜索",
            "loading": "加载中…",
            "ready": "就绪",
            "done": "完成",
            "error": "错误",
            "warning": "警告",
            "running": "运行中",
            "yes": "是",
            "no": "否",

            # ---- 主界面 ----
            "new_chat": "+ 新建对话",
            "new_project": "新项目",
            "create_project": "创建项目",
            "open_project": "打开项目",
            "create_project_failed_title": "创建项目失败",
            "create_project_failed_msg": "无法创建项目 \"{name}\"，请检查目录权限或项目名称是否合法。",
            "welcome_subtitle": "有什么可以帮你的？输入消息开始对话，或使用 /plan 和 /ultra 切换模式",
            "settings": "设置",
            "stop": "停止",
            "send": "发送",
            "thinking": "思考中…",
            "thinking_done": "已思考",
            "typing_indicator": "正在响应…",
            "project_list": "对话列表",
            "mention_hint": "输入你的想法以开始构建...",
            "status_ready": "就绪",
            "status_stopped": "已停止",
            "model_current": "当前模型",
            "cost_label": "成本",
            "cache_hit": "缓存命中",
            "vendor_label": "供应商",
            "balance_label": "余额",
            "upload_image": "图片",
            "preview": "预览",
            "light_theme": "浅色主题",
            "dark_theme": "深色主题",
            "light_mode": "浅色",
            "dark_mode": "深色",
            "pin_chat": "置顶该对话",
            "unpin_chat": "取消置顶",
            "rename_chat": "重命名对话",
            "delete_chat": "删除对话",

            # ---- 状态栏 ----
            "status_api_balance": "API 余额",
            "status_cache_rate": "缓存命中率",
            "status_vendor": "供应商",
            "status_model": "当前模型",

            # ---- 设置对话框 ----
            "settings_title": "设置",
            "basic_settings_tab": "基础设置",
            "api_settings_tab": "API 配置",
            "about_tab": "关于",
            "theme_label": "主题",
            "theme_light": "浅色",
            "theme_dark": "深色",
            "thinking_depth_label": "思考深度",
            "thinking_high": "标准",
            "thinking_max": "深度",
            "permission_label": "权限",
            "perm_reviewer": "审查者",
            "perm_coder": "码农",
            "perm_full": "完全权限",
            "perm_reviewer_desc": "只读，所有写入操作需确认",
            "perm_coder_desc": "标准，可以创建/修改文件",
            "perm_full_desc": "完全权限，可执行命令",
            "popup_title": "模型 · 权限设置",
            "popup_tab_model": "模型",
            "popup_tab_thinking": "思考深度",
            "popup_tab_permission": "权限模式",
            "popup_footer": "修改将自动保存到配置",
            "popup_mode_normal": "普通",
            "popup_mode_plan": "规划",
            "popup_mode_label": "模式",
            "popup_perm_reviewer_title": "审查者模式",
            "popup_perm_reviewer_desc": "只读 + 只能写自己的文档，不能改原有文件。",
            "popup_perm_coder_title": "码农模式",
            "popup_perm_coder_desc": "重要操作需确认；删除文件一律移入 RecycleBin 文件夹。",
            "popup_perm_full_title": "完全体模式",
            "popup_perm_full_desc": "无确认，删除仍进 RecycleBin。",
            "popup_show_chain": "展开思考链",
            "popup_chain_length": "思考链长度",
            "popup_api_key": "API Key",
            "popup_base_url": "Base URL",
            "popup_model_select": "模型选择",
            "popup_temperature": "Temperature",
            "popup_temperature_tooltip": "取值 0.0-2.0，越低回答越确定；越高创意性越强。",
            "popup_max_tokens": "Max Tokens",
            "popup_max_tokens_tooltip": "模型单次回复的最大 token 数，范围 1-128000。",
            "popup_reasoning_label": "思考深度",
            "plan_bubble_title": "执行计划",
            "plan_bubble_empty": "(模型未提供计划文本)",
            "plan_bubble_cancel": "取消",
            "plan_bubble_confirm": "确认并执行",
            "render_fallback_hint": "[渲染降级：部分格式可能丢失]",
            "welcome_skip_title": "跳过设置",
            "welcome_skip_msg": "确定要跳过设置向导吗？你可以稍后在设置中配置。",
            "action_copy": "复制",
            "action_copied": "已复制",
            "action_regenerate": "重新生成",
            "popup_mode_ultra": "Ultra",
            "mode_ultra": "Ultra",
            "mode_ultra_short": "Ultra",
            "slash_ultra_desc": "DS Code Ultra：规划验证交付",
            "slash_plan_desc": "规划模式：先规划再执行",
            "reasoning_high": "标准",
            "reasoning_max": "深度",
            "reasoning_high_short": "",
            "reasoning_max_short": "Max",
            "model_label": "模型",
            "model_flash": "DeepSeek-v4-flash",
            "model_pro": "DeepSeek-v4-pro",
            "mode_label": "工作模式",
            "mode_normal": "普通模式",
            "mode_plan": "Plan模式",
            "mode_ultra_desc": "规划-执行-验证-交付",
            "mode_normal_desc": "直接回答",
            "mode_plan_desc": "先规划再执行",
            "api_config_title": "API 配置",
            "api_key_label": "API Key",
            "api_key": "API Key",
            "api_key_placeholder": "输入你的 API Key",
            "base_url_label": "Base URL",
            "base_url": "Base URL",
            "provider_official": "官方 API",
            "provider_custom": "自定义",
            "custom_base_url": "自定义 Base URL",
            "show_password": "显示",
            "hide_password": "隐藏",
            "custom_model": "模型名称",
            "add_provider": "添加供应商",
            "remove_provider": "移除",
            "nickname_label": "昵称",
            "nickname_placeholder": "你的昵称（选填）",
            "custom_rules_label": "自定义规则",
            "custom_rules_placeholder": "添加你希望AI遵守的自定义规则...",
            "user_agreement": "查看用户协议",
            "feedback": "反馈问题",
            "export_project": "导出项目",
            "import_project": "导入项目",
            "version_label": "版本",
            "save_settings": "保存设置",
            "settings_saved": "设置已保存",
            "api_key_required": "请先配置 API Key",
            "about_desc": "DScode 是一款基于 DeepSeek API 的AI编程助手",
            "settings_subtitle": "个性化你的 DS Code 体验",
            "theme_system": "跟随系统",
            "basic_settings_subtitle": "主题 / 数据 / 扩展工具",
            "permission_level_label": "权限级别",
            "data_management": "数据管理",
            "extension_tools": "扩展工具",
            "custom_mcp": "自定义 MCP",
            "coming_soon": "即将支持",
            "mcp_coming_soon": "自定义 MCP 功能即将上线，敬请期待。",
            "add_skill": "添加 skill",
            "skill_coming_soon": "skill 扩展功能即将上线，敬请期待。",
            "about_version_fmt": "关于 DScode\nv{ver}",
            "about_card_title": "关于你",
            "about_card_subtitle": "个性化模型行为",
            "custom_rules_hint": "这些规则会写入每轮对话的系统提示中，帮助模型保持一致的行为和语气。",
            "provider_card_title": "供应商",
            "provider_card_subtitle": "API 与模型配置",
            "provider_url_label": "供应商 URL",
            "openai_compatible_hint": "支持 OpenAI 兼容 API 格式",
            "tip": "提示",
            "max_providers_msg": "最多配置 2 家供应商。",
            "min_providers_msg": "至少需要保留一个供应商配置。",
            "custom_provider_default": "自定义 {n}",
            "provider_default_name": "供应商 {n}",
            "model_name_placeholder": "模型名称，例如 deepseek-v4-pro",
            "long_context_label": "支持 1M 上下文",
            "add_provider_btn": "+ 添加供应商",
            "remove_provider_btn": "- 移除",
            "api_key_sk_placeholder": "sk-...",

            # ---- 欢迎/使用前确认 ----
            "welcome_title": "开始使用 DScode",
            "disclaimer_title": "AI 免责声明",
            "disclaimer_text": "DScode 是一款AI编程助手，生成内容可能有误，请注意辨别。",
            "legal_title": "法律合规",
            "legal_text": "使用本工具时请遵守当地法律法规，不得用于违法活动。",
            "agreement_checkbox": "我已阅读并同意用户协议",
            "agreement_link": "用户协议",
            "agreement_required": "请先同意用户协议",
            "start_using": "开始使用",
            "quick_start": "快速开始",
            "welcome_hint": "点击开始，进入主界面后可在设置中配置API Key和模型",
            "welcome_greeting": "你好，我是 DS code",
            "welcome_greeting_sub": "你的AI编程助手，准备好帮你写代码了",
            "wizard_next": "下一步",
            "wizard_back": "上一步",
            "wizard_skip": "稍后设置",
            "wizard_finish": "开始使用",
            "step_nickname_title": "你叫什么名字？",
            "step_nickname_desc": "告诉我你的昵称，我会这样称呼你",
            "step_apikey_title": "配置 API Key",
            "step_apikey_desc": "输入你的 DeepSeek API Key 即可开始对话",
            "step_apikey_hint": "没有 Key？可在 platform.deepseek.com 注册获取，也可以稍后在设置中配置",
            "step_model_title": "选择默认模型",
            "step_model_desc": "选择你偏好的默认 AI 模型",
            "step_model_flash": "DeepSeek-V4-Flash — 快速响应，适合日常对话",
            "step_model_pro": "DeepSeek-V4-Pro — 深度思考，适合复杂编程任务",
            "step_reasoning_title": "思考深度",
            "step_reasoning_desc": "AI 回答前会花多少时间思考",
            "step_perm_title": "权限模式",
            "step_perm_desc": "控制 AI 对你的文件和系统的访问权限",
            "step_mode_title": "工作模式",
            "step_mode_desc": "选择默认的工作方式",
            "step_done_title": "一切就绪！",
            "step_done_desc": "配置完成，准备好开始编程之旅了吗？",
            "nickname_placeholder": "输入你的昵称（选填）",
            "apikey_placeholder": "sk-...",

            # ---- 右侧面板 ----
            "tab_todos": "待办事项",
            "tab_files": "项目文件",
            "no_todos": "暂无待办事项",
            "no_todos_hint": "模型会在此处列出它计划做的事情",
            "no_files": "选择项目后这里会展示文件树",
            "select_project_first": "选择项目后这里会展示文件树",

            # ---- 文件树 ----
            "file_tree_title": "项目文件",
            "search_files_placeholder": "搜索文件...",
            "new_file": "新建文件",
            "new_folder": "新建文件夹",
            "open_in_terminal": "在终端中打开",
            "new_file_name": "文件名",
            "new_folder_name": "文件夹名",
            "confirm_delete": "确认删除",
            "delete_confirm_msg": "确定要删除 \"{name}\" 吗？",
            "file_created": "文件已创建",
            "folder_created": "文件夹已创建",
            "refresh_tree": "刷新",

            # ---- 终端/命令 ----
            "terminal_title": "终端",
            "confirm_run_command": "执行命令",
            "command_preview": "将要执行以下命令：",
            "exit_code": "退出码",
            "apply_diff": "应用变更",
            "reject_diff": "取消变更",

            # ---- 工具调用 ----
            "tool_call": "工具调用",
            "preview_file": "文件预览",

            # ---- 对话模式描述 ----
            "mode_normal_hint": "普通模式：直接回答问题",
            "mode_plan_hint": "Plan模式：先输出计划，确认后执行",
            "mode_ultra_hint": "Ultra模式：统筹规划、反复验证、子代理调度",

            # ---- 杂项 ----
            "project_no_name": "未命名项目",
            "no_project_selected": "未选择项目",
            "select_project": "选择项目",
            "export_success": "导出成功",
            "import_success": "导入成功",
            "network_error": "网络错误，请检查网络连接",
            "api_error": "API 调用失败",
            "empty_message": "请输入消息",
            "max_reasoning": "max（深度思考）",
            "high_reasoning": "high（标准）",
            "token_stats_fmt": "{rate} token/s · {total} tokens",
            "token_suffix": "tokens",
            "token_per_sec_suffix": "token/s",

            # ---- 语言选择 ----
            "language_label": "语言",
            "add_language_pack": "添加语言包",
            "lang_pack_dialog_title": "添加语言包",
            "lang_pack_step1_title": "第 1 步：导出英文模板",
            "lang_pack_step1_desc": "点击下方按钮导出英文语言包 JSON 文件，文件中 key 和 value 均为英文。",
            "lang_pack_export_btn": "导出英文模板",
            "lang_pack_export_success": "模板已导出到：{path}",
            "lang_pack_step2_title": "第 2 步：交给 AI 翻译",
            "lang_pack_step2_desc": "将导出的 JSON 文件发给 Claude / ChatGPT / DeepSeek 等 AI，使用提示词让它翻译为目标语言（如日语、韩语、法语等），保持 key 不变、仅翻译 value，并保留 {占位符}。",
            "lang_pack_prompt_hint": "推荐提示词已复制到剪贴板，也可点击下方按钮复制。",
            "lang_pack_copy_prompt": "复制提示词",
            "lang_pack_step3_title": "第 3 步：拖入翻译好的文件",
            "lang_pack_step3_desc": "将 AI 返回的翻译 JSON 文件拖拽到下方区域，或点击选择文件。",
            "lang_pack_drop_hint": "拖拽 JSON 文件到此处",
            "lang_pack_drop_hover": "松开以导入",
            "lang_pack_or_click": "或点击选择文件",
            "lang_pack_invalid_json": "无效的 JSON 文件：{err}",
            "lang_pack_missing_meta": "缺少 _meta 字段，请确认文件是由模板翻译而来",
            "lang_pack_missing_keys": "缺少 {n} 个翻译项，将自动使用英文补充",
            "lang_pack_placeholder_mismatch": "占位符不匹配：key \"{key}\" 中的 {ph}",
            "lang_pack_load_success": "已添加语言：{name}（{code}）",
            "lang_pack_file_exists": "语言包 {code}.json 已存在，是否覆盖？",
            "lang_pack_builtin_protected": "内置语言 {name} 不可覆盖",
            "lang_pack_select_file_title": "选择翻译好的语言包 JSON",
            "lang_pack_json_filter": "JSON 文件 (*.json)",
            "lang_pack_prompt_text": "你是一位专业翻译。请将附件 JSON 文件中的英文 value 翻译为【请在此指定目标语言，例如：日本語 / 한국어 / Français / Deutsch / Español】，要求：\n1. 保持所有 key 不变，只翻译 value；\n2. 保留所有 {xxx} 形式的占位符，不要翻译花括号内的内容；\n3. 保留 \\n 等转义字符；\n4. 修改 _meta 字段：将 _meta.code 改为目标语言的 BCP-47 代码（如 ja / ko / fr / de / es），将 _meta.name 改为该语言的本地名称（如 日本語 / 한국어 / Français）；\n5. 输出完整的 JSON，不要省略任何条目，不要添加解释说明。",

            # ---- 删除/重命名对话框 ----
            "delete_chat_title": "删除对话",
            "delete_chat_msg": "确定删除该对话，该操作不可恢复。",
            "delete_countdown_fmt": "删除 ({n})",
            "enter_new_name": "输入新名称",
            "conversation_list": "对话列表",
            "dark_mode_btn": "深色模式",
            "light_mode_btn": "浅色模式",

            # ---- 索引状态 ----
            "index_ready": "索引就绪",
            "index_degraded": "索引降级",
            "index_unavailable": "索引不可用",
            "index_building": "索引构建中",
            "index_status_tooltip": "代码索引状态",

            # ---- 输入区 tooltip ----
            "clear_chat_tooltip": "清空当前对话",
            "send_tooltip": "Ctrl+Enter 发送",
            "git_tooltip": "打开 Git 状态弹窗",
            "terminal_tooltip": "打开/关闭内置终端",
            "slash_tooltip": "输入斜杠打开快捷命令",

            # ---- 模式简称 ----
            "mode_normal_short": "普通",
            "mode_plan_short": "规划",

            # ---- 图片/文件上传 ----
            "max_images_msg": "最多上传 {n} 张图片",
            "image_n_fmt": "图片 {n}",
            "file_label": "文件",

            # ---- 计划/命令确认 ----
            "no_plan_text": "(模型未提供计划文本)",
            "command_confirm_title": "命令执行确认",
            "command_confirm_msg": "DS Code 想要执行以下命令：\n\n{cmd}\n\n是否允许？",
            "allow_exec": "允许执行",
            "mode_status_fmt": "模式：{mode}",

            # ---- 文件对话框 ----
            "select_file_title": "选择文件",
            "all_files_filter": "所有文件 (*)",
        },
        "en": {
            # ---- Common ----
            "app_name": "DS Code",
            "ok": "OK",
            "cancel": "Cancel",
            "confirm": "Confirm",
            "close": "Close",
            "save": "Save",
            "delete": "Delete",
            "rename": "Rename",
            "create": "New",
            "copy": "Copy",
            "refresh": "Refresh",
            "search": "Search",
            "loading": "Loading…",
            "ready": "Ready",
            "done": "Done",
            "error": "Error",
            "warning": "Warning",
            "running": "Running",
            "yes": "Yes",
            "no": "No",

            # ---- Main UI ----
            "new_chat": "+ New Chat",
            "new_project": "New Project",
            "create_project": "Create Project",
            "open_project": "Open Project",
            "create_project_failed_title": "Create Project Failed",
            "create_project_failed_msg": "Unable to create project \"{name}\". Please check directory permissions or whether the project name is valid.",
            "welcome_subtitle": "How can I help? Type a message to start, or use /plan and /ultra to switch modes",
            "settings": "Settings",
            "stop": "Stop",
            "send": "Send",
            "thinking": "Thinking…",
            "thinking_done": "Thought",
            "typing_indicator": "Responding…",
            "project_list": "Chats",
            "mention_hint": "Type your thoughts to start building...",
            "status_ready": "Ready",
            "status_stopped": "Stopped",
            "model_current": "Model",
            "cost_label": "Cost",
            "cache_hit": "Cache Hit",
            "vendor_label": "Vendor",
            "balance_label": "Balance",
            "upload_image": "Image",
            "preview": "Preview",
            "light_theme": "Light Theme",
            "dark_theme": "Dark Theme",
            "light_mode": "Light",
            "dark_mode": "Dark",
            "pin_chat": "Pin Chat",
            "unpin_chat": "Unpin Chat",
            "rename_chat": "Rename Chat",
            "delete_chat": "Delete Chat",

            # ---- Status Bar ----
            "status_api_balance": "API Balance",
            "status_cache_rate": "Cache Hit Rate",
            "status_vendor": "Vendor",
            "status_model": "Model",

            # ---- Settings Dialog ----
            "settings_title": "Settings",
            "basic_settings_tab": "Basic",
            "api_settings_tab": "API Config",
            "about_tab": "About",
            "theme_label": "Theme",
            "theme_light": "Light",
            "theme_dark": "Dark",
            "thinking_depth_label": "Thinking Depth",
            "thinking_high": "Standard",
            "thinking_max": "Deep",
            "permission_label": "Permission",
            "perm_reviewer": "Reviewer",
            "perm_coder": "Coder",
            "perm_full": "Full Access",
            "perm_reviewer_desc": "Read-only, all writes require confirmation",
            "perm_coder_desc": "Standard, can create/modify files",
            "perm_full_desc": "Full access, can execute commands",
            "popup_title": "Model & Permission Settings",
            "popup_tab_model": "Model",
            "popup_tab_thinking": "Thinking",
            "popup_tab_permission": "Permission",
            "popup_footer": "Changes auto-save to config",
            "popup_mode_normal": "Normal",
            "popup_mode_plan": "Plan",
            "popup_mode_label": "Mode",
            "popup_perm_reviewer_title": "Reviewer Mode",
            "popup_perm_reviewer_desc": "Read-only + can only write your own docs, cannot modify existing files.",
            "popup_perm_coder_title": "Coder Mode",
            "popup_perm_coder_desc": "Important operations need confirmation; deleted files go to RecycleBin folder.",
            "popup_perm_full_title": "Full Mode",
            "popup_perm_full_desc": "No confirmation, deletions still go to RecycleBin.",
            "popup_show_chain": "Show thinking chain",
            "popup_chain_length": "Chain length",
            "popup_api_key": "API Key",
            "popup_base_url": "Base URL",
            "popup_model_select": "Model Selection",
            "popup_temperature": "Temperature",
            "popup_temperature_tooltip": "Range 0.0-2.0. Lower values produce more deterministic answers; higher values are more creative.",
            "popup_max_tokens": "Max Tokens",
            "popup_max_tokens_tooltip": "Maximum number of tokens in a single model response, range 1-128000.",
            "popup_reasoning_label": "Thinking Depth",
            "plan_bubble_title": "Execution Plan",
            "plan_bubble_empty": "(No plan text provided by model)",
            "plan_bubble_cancel": "Cancel",
            "plan_bubble_confirm": "Confirm & Execute",
            "render_fallback_hint": "[Render fallback: some formatting may be lost]",
            "welcome_skip_title": "Skip Setup",
            "welcome_skip_msg": "Are you sure you want to skip the setup wizard? You can configure later in Settings.",
            "action_copy": "Copy",
            "action_copied": "Copied",
            "action_regenerate": "Regenerate",
            "popup_mode_ultra": "Ultra",
            "mode_ultra": "Ultra",
            "mode_ultra_short": "Ultra",
            "slash_ultra_desc": "DS Code Ultra: Plan, verify, deliver",
            "slash_plan_desc": "Plan mode: Plan first, then execute",
            "reasoning_high": "Standard",
            "reasoning_max": "Deep",
            "reasoning_high_short": "",
            "reasoning_max_short": "Max",
            "model_label": "Model",
            "model_flash": "DeepSeek-v4-flash",
            "model_pro": "DeepSeek-v4-pro",
            "mode_label": "Mode",
            "mode_normal": "Normal",
            "mode_plan": "Plan",
            "mode_ultra_desc": "Plan, execute, verify, deliver",
            "mode_normal_desc": "Direct answers",
            "mode_plan_desc": "Plan first, then execute",
            "api_config_title": "API Configuration",
            "api_key_label": "API Key",
            "api_key": "API Key",
            "api_key_placeholder": "Enter your API Key",
            "base_url_label": "Base URL",
            "base_url": "Base URL",
            "provider_official": "Official API",
            "provider_custom": "Custom",
            "custom_base_url": "Custom Base URL",
            "show_password": "Show",
            "hide_password": "Hide",
            "custom_model": "Model Name",
            "add_provider": "Add Provider",
            "remove_provider": "Remove",
            "nickname_label": "Nickname",
            "nickname_placeholder": "Your nickname (optional)",
            "custom_rules_label": "Custom Rules",
            "custom_rules_placeholder": "Add custom rules for AI to follow...",
            "user_agreement": "User Agreement",
            "feedback": "Feedback",
            "export_project": "Export Project",
            "import_project": "Import Project",
            "version_label": "Version",
            "save_settings": "Save",
            "settings_saved": "Settings saved",
            "api_key_required": "Please configure your API Key first",
            "about_desc": "DScode is an AI coding assistant powered by DeepSeek API",
            "settings_subtitle": "Personalize your DS Code experience",
            "theme_system": "System",
            "basic_settings_subtitle": "Theme / Data / Tools",
            "permission_level_label": "Permission Level",
            "data_management": "Data Management",
            "extension_tools": "Extensions",
            "custom_mcp": "Custom MCP",
            "coming_soon": "Coming Soon",
            "mcp_coming_soon": "Custom MCP feature is coming soon, stay tuned.",
            "add_skill": "Add Skill",
            "skill_coming_soon": "Skill extension feature is coming soon, stay tuned.",
            "about_version_fmt": "About DScode\nv{ver}",
            "about_card_title": "About You",
            "about_card_subtitle": "Personalize model behavior",
            "custom_rules_hint": "These rules are added to the system prompt of every conversation to help the model maintain consistent behavior and tone.",
            "provider_card_title": "Providers",
            "provider_card_subtitle": "API & Model Configuration",
            "provider_url_label": "Provider URL",
            "openai_compatible_hint": "Supports OpenAI-compatible API format",
            "tip": "Tip",
            "max_providers_msg": "Maximum 2 providers allowed.",
            "min_providers_msg": "At least one provider must remain.",
            "custom_provider_default": "Custom {n}",
            "provider_default_name": "Provider {n}",
            "model_name_placeholder": "Model name, e.g. deepseek-v4-pro",
            "long_context_label": "Supports 1M context",
            "add_provider_btn": "+ Add Provider",
            "remove_provider_btn": "- Remove",
            "api_key_sk_placeholder": "sk-...",

            # ---- Welcome / First-run ----
            "welcome_title": "Welcome to DScode",
            "disclaimer_title": "AI Disclaimer",
            "disclaimer_text": "DScode is an AI coding assistant. Generated content may contain errors — please verify.",
            "legal_title": "Legal Compliance",
            "legal_text": "Please comply with local laws and regulations. Do not use for illegal activities.",
            "agreement_checkbox": "I have read and agree to the User Agreement",
            "agreement_link": "User Agreement",
            "agreement_required": "Please agree to the User Agreement first",
            "start_using": "Get Started",
            "quick_start": "Quick Start",
            "welcome_hint": "Click Start, you can configure API Key and model in Settings after entering the main interface",
            "welcome_greeting": "Hi, I'm DS code",
            "welcome_greeting_sub": "Your AI coding assistant, ready to help you build",
            "wizard_next": "Next",
            "wizard_back": "Back",
            "wizard_skip": "Set up later",
            "wizard_finish": "Get Started",
            "step_nickname_title": "What's your name?",
            "step_nickname_desc": "Tell me your nickname so I know how to call you",
            "step_apikey_title": "Configure API Key",
            "step_apikey_desc": "Enter your DeepSeek API Key to start chatting",
            "step_apikey_hint": "No key? Get one at platform.deepseek.com, or configure later in Settings",
            "step_model_title": "Choose Default Model",
            "step_model_desc": "Select your preferred AI model",
            "step_model_flash": "DeepSeek-V4-Flash — Fast responses, great for daily chat",
            "step_model_pro": "DeepSeek-V4-Pro — Deep reasoning, best for complex coding",
            "step_reasoning_title": "Reasoning Depth",
            "step_reasoning_desc": "How much time the AI spends thinking before answering",
            "step_perm_title": "Permission Mode",
            "step_perm_desc": "Control AI access to your files and system",
            "step_mode_title": "Working Mode",
            "step_mode_desc": "Choose your default working style",
            "step_done_title": "All Set!",
            "step_done_desc": "Configuration complete. Ready to start coding?",
            "nickname_placeholder": "Enter your nickname (optional)",
            "apikey_placeholder": "sk-...",

            # ---- Right Panel ----
            "tab_todos": "Todos",
            "tab_files": "Files",
            "no_todos": "No todos yet",
            "no_todos_hint": "The AI will list planned tasks here",
            "no_files": "Select a project to view files",
            "select_project_first": "Select a project to view file tree",

            # ---- File Tree ----
            "file_tree_title": "Project Files",
            "search_files_placeholder": "Search files...",
            "new_file": "New File",
            "new_folder": "New Folder",
            "open_in_terminal": "Open in Terminal",
            "new_file_name": "File name",
            "new_folder_name": "Folder name",
            "confirm_delete": "Confirm Delete",
            "delete_confirm_msg": "Are you sure you want to delete \"{name}\"?",
            "file_created": "File created",
            "folder_created": "Folder created",
            "refresh_tree": "Refresh",

            # ---- Terminal / Commands ----
            "terminal_title": "Terminal",
            "confirm_run_command": "Run Command",
            "command_preview": "The following command will be executed:",
            "exit_code": "Exit Code",
            "apply_diff": "Apply Changes",
            "reject_diff": "Reject Changes",

            # ---- Tool calls ----
            "tool_call": "Tool Call",
            "preview_file": "File Preview",

            # ---- Mode hints ----
            "mode_normal_hint": "Normal mode: direct Q&A",
            "mode_plan_hint": "Plan mode: plan first, execute after confirmation",
            "mode_ultra_hint": "Ultra mode: plan, verify, iterate, dispatch sub-agents",

            # ---- Misc ----
            "project_no_name": "Untitled Project",
            "no_project_selected": "No project selected",
            "select_project": "Select Project",
            "export_success": "Export successful",
            "import_success": "Import successful",
            "network_error": "Network error, please check your connection",
            "api_error": "API call failed",
            "empty_message": "Please enter a message",
            "max_reasoning": "max (deep thinking)",
            "high_reasoning": "high (standard)",
            "token_stats_fmt": "{rate} token/s · {total} tokens",
            "token_suffix": "tokens",
            "token_per_sec_suffix": "token/s",

            # ---- Delete/Rename dialogs ----
            "delete_chat_title": "Delete Chat",
            "delete_chat_msg": "Are you sure you want to delete this chat? This action cannot be undone.",
            "delete_countdown_fmt": "Delete ({n})",
            "enter_new_name": "Enter new name",
            "conversation_list": "Conversations",
            "dark_mode_btn": "Dark Mode",
            "light_mode_btn": "Light Mode",

            # ---- Index status ----
            "index_ready": "Index Ready",
            "index_degraded": "Index Degraded",
            "index_unavailable": "Index Unavailable",
            "index_building": "Indexing...",
            "index_status_tooltip": "Code Index Status",

            # ---- Input area tooltips ----
            "clear_chat_tooltip": "Clear current chat",
            "send_tooltip": "Ctrl+Enter to Send",
            "git_tooltip": "Open Git Status",
            "terminal_tooltip": "Toggle Built-in Terminal",
            "slash_tooltip": "Type / for commands",

            # ---- Short mode names ----
            "mode_normal_short": "Normal",
            "mode_plan_short": "Plan",

            # ---- Image/file upload ----
            "max_images_msg": "Maximum {n} images",
            "image_n_fmt": "Image {n}",
            "file_label": "File",

            # ---- Plan / Command confirmation ----
            "no_plan_text": "(No plan provided)",
            "command_confirm_title": "Command Confirmation",
            "command_confirm_msg": "DS Code wants to execute the following command:\n\n{cmd}\n\nAllow?",
            "allow_exec": "Allow",
            "mode_status_fmt": "Mode: {mode}",

            # ---- File dialog ----
            "select_file_title": "Select File",
            "all_files_filter": "All Files (*)",

            # ---- Language selection ----
            "language_label": "Language",
            "add_language_pack": "Add Language Pack",
            "lang_pack_dialog_title": "Add Language Pack",
            "lang_pack_step1_title": "Step 1: Export English Template",
            "lang_pack_step1_desc": "Click the button below to export the English language pack JSON file. Keys and values are both in English.",
            "lang_pack_export_btn": "Export English Template",
            "lang_pack_export_success": "Template exported to: {path}",
            "lang_pack_step2_title": "Step 2: Let AI Translate",
            "lang_pack_step2_desc": "Send the exported JSON file to an AI (Claude / ChatGPT / DeepSeek, etc.) using the prompt below. It will translate values to the target language (e.g. Japanese, Korean, French) while keeping keys unchanged and preserving {placeholders}.",
            "lang_pack_prompt_hint": "The recommended prompt is ready to copy. Click the button below to copy it to clipboard.",
            "lang_pack_copy_prompt": "Copy Prompt",
            "lang_pack_step3_title": "Step 3: Drop the Translated File",
            "lang_pack_step3_desc": "Drag and drop the translated JSON file into the area below, or click to select a file.",
            "lang_pack_drop_hint": "Drop JSON file here",
            "lang_pack_drop_hover": "Release to import",
            "lang_pack_or_click": "or click to select file",
            "lang_pack_invalid_json": "Invalid JSON file: {err}",
            "lang_pack_missing_meta": "Missing _meta field. Please confirm the file was translated from the template.",
            "lang_pack_missing_keys": "{n} translation entries missing; English will be used as fallback.",
            "lang_pack_placeholder_mismatch": "Placeholder mismatch: key \"{key}\" contains {ph}",
            "lang_pack_load_success": "Language added: {name} ({code})",
            "lang_pack_file_exists": "Language pack {code}.json already exists. Overwrite?",
            "lang_pack_builtin_protected": "Built-in language {name} cannot be overwritten.",
            "lang_pack_select_file_title": "Select Translated Language Pack JSON",
            "lang_pack_json_filter": "JSON Files (*.json)",
            "lang_pack_prompt_text": "You are a professional translator. Translate the English values in the attached JSON file into [SPECIFY TARGET LANGUAGE HERE, e.g. 日本語 / 한국어 / Français / Deutsch / Español]. Requirements:\n1. Keep all keys unchanged; only translate values;\n2. Preserve all {xxx} placeholders exactly as-is, do not translate content inside curly braces;\n3. Preserve escape characters such as \\n;\n4. Modify the _meta field: set _meta.code to the BCP-47 code of the target language (e.g. ja / ko / fr / de / es), set _meta.name to the native name of the language (e.g. 日本語 / 한국어 / Français);\n5. Output the complete JSON without omitting any entries or adding explanatory text.",
        },
    }

    # ---- 自定义语言包运行时存储 ----
    _custom_packs: dict = {}
    BUILTIN_LANGS = frozenset({"zh", "en"})
    _language_changed_callbacks: list = []
    _PLACEHOLDER_RE = re.compile(r'\{([a-zA-Z_][a-zA-Z0-9_]*)\}')

    # ---- 目录 ----
    @classmethod
    def get_locale_dir(cls) -> Path:
        """返回自定义语言包存储目录 <user_data_dir>/dscode/locale/，不存在则创建。"""
        try:
            from src.core.config import Config
            base = Config.BASE_DIR
        except Exception:
            base = Path.home() / ".local" / "share" / "dscode"
        locale_dir = Path(base) / "locale"
        locale_dir.mkdir(parents=True, exist_ok=True)
        return locale_dir

    # ---- 模板导出 ----
    @classmethod
    def export_template(cls, path) -> str:
        """导出英文模板 JSON 到指定路径，返回实际写入路径。"""
        en_table = cls.TRANSLATIONS["en"]
        template = {"_meta": {
            "code": "",
            "name": "",
            "version": 1,
            "app_version": "1.0.2.18",
        }}
        for k, v in en_table.items():
            template[k] = v
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(template, f, ensure_ascii=False, indent=2)
        return str(p)

    # ---- 校验 ----
    @classmethod
    def _validate_pack(cls, data: dict) -> tuple:
        """校验语言包数据。
        返回: (is_valid, code, name, errors, warnings)
        errors/warnings 为字符串列表。
        """
        errors = []
        warnings = []

        meta = data.get("_meta")
        if not isinstance(meta, dict):
            errors.append("missing_meta")
            return False, "", "", errors, warnings

        code = str(meta.get("code", "")).strip().lower()
        name = str(meta.get("name", "")).strip()

        if not code:
            errors.append("meta_code_empty")
            return False, code, name, errors, warnings

        if not re.match(r'^[a-z]{2,3}(-[a-z0-9]+)*$', code):
            errors.append("meta_code_invalid")
            return False, code, name, errors, warnings

        if code in cls.BUILTIN_LANGS or code.split("-")[0] in cls.BUILTIN_LANGS:
            errors.append("builtin_protected")
            return False, code, name, errors, warnings

        if not name:
            warnings.append("meta_name_empty")
            name = code

        en_table = cls.TRANSLATIONS["en"]
        en_keys = set(en_table.keys())
        pack_keys = {k for k in data.keys() if k != "_meta"}

        missing = en_keys - pack_keys
        if missing:
            warnings.append(f"missing_keys:{len(missing)}")

        extra = pack_keys - en_keys
        if extra:
            warnings.append(f"extra_keys:{len(extra)}")

        for k in pack_keys & en_keys:
            val = data.get(k)
            if not isinstance(val, str):
                continue
            en_val = en_table[k]
            en_ph = set(cls._PLACEHOLDER_RE.findall(en_val))
            val_ph = set(cls._PLACEHOLDER_RE.findall(val))
            if en_ph != val_ph:
                mismatched = en_ph.symmetric_difference(val_ph)
                errors.append(f"placeholder_mismatch:{k}:{','.join(sorted(mismatched))}")

        is_valid = len(errors) == 0
        return is_valid, code, name, errors, warnings

    # ---- 单包加载 ----
    @classmethod
    def _load_single_pack(cls, file_path) -> tuple:
        """从 JSON 文件加载单个语言包，执行校验。
        返回: (success, code, name, pack_dict, errors, warnings)
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return False, "", "", None, [f"invalid_json:{e}"], []

        if not isinstance(data, dict):
            return False, "", "", None, ["invalid_json:not_object"], []

        is_valid, code, name, errors, warnings = cls._validate_pack(data)
        if not is_valid:
            return False, code, name, None, errors, warnings

        en_table = cls.TRANSLATIONS["en"]
        pack = {"_meta": data.get("_meta", {"code": code, "name": name})}
        for k, v in en_table.items():
            pack[k] = data.get(k, v)

        return True, code, name, pack, errors, warnings

    # ---- 注册 ----
    @classmethod
    def register_language(cls, code: str, pack: dict) -> None:
        """将语言包注册到运行时（同时注册基础前缀）。"""
        code = str(code).strip().lower()
        cls._custom_packs[code] = pack
        base = code.split("-")[0]
        if base != code and base not in cls._custom_packs:
            cls._custom_packs[base] = pack
        cls._invalidate_table_cache()  # P0-3: 注册新语言包后清空 table 缓存

    # ---- 批量扫描加载 ----
    @classmethod
    def load_custom_language_packs(cls) -> list:
        """扫描 locale 目录加载所有 *.json 自定义语言包。返回成功加载的 code 列表。"""
        loaded = []
        locale_dir = cls.get_locale_dir()
        for fp in sorted(locale_dir.glob("*.json")):
            try:
                # 跳过内置语言文件（防御）
                stem = fp.stem.lower()
                if stem in cls.BUILTIN_LANGS:
                    print(f"[I18N] Skip built-in language file: {fp.name}")
                    continue
                success, code, name, pack, errors, warnings = cls._load_single_pack(fp)
                if success and pack is not None:
                    cls.register_language(code, pack)
                    loaded.append(code)
            except Exception as e:
                print(f"[I18N] Failed to load language pack {fp}: {e}")
        return loaded

    # ---- 语言列表 ----
    @classmethod
    def available_languages(cls) -> list:
        """返回 [(code, display_name, is_builtin)]，内置 zh/en 排前面。"""
        result = [
            ("zh", "中文", True),
            ("en", "English", True),
        ]
        existing = {"zh", "en"}
        for code in sorted(cls._custom_packs.keys()):
            if code in existing:
                continue
            pack = cls._custom_packs[code]
            name = pack.get("_meta", {}).get("name", code)
            result.append((code, name, False))
            existing.add(code)
        return result

    @classmethod
    def get_language_name(cls, code: str) -> str:
        """返回语言本地显示名。"""
        if code == "zh":
            return "中文"
        if code == "en":
            return "English"
        pack = cls._custom_packs.get(code) or cls._custom_packs.get(code.split("-")[0])
        if pack:
            return pack.get("_meta", {}).get("name", code)
        return code

    # ---- 保存到磁盘 ----
    @classmethod
    def save_pack_to_disk(cls, pack: dict) -> Path:
        """将校验通过的语言包保存到 locale 目录。文件名用基础前缀（如 ja.json）。"""
        code = pack.get("_meta", {}).get("code", "").strip().lower()
        base_code = code.split("-")[0]
        if base_code in cls.BUILTIN_LANGS:
            raise ValueError(f"Built-in language {base_code} cannot be overwritten")
        locale_dir = cls.get_locale_dir()
        target = locale_dir / f"{base_code}.json"
        with open(target, "w", encoding="utf-8") as f:
            json.dump(pack, f, ensure_ascii=False, indent=2)
        return target

    # ---- 回调 ----
    @classmethod
    def on_language_changed(cls, callback) -> None:
        """注册语言变更回调 callback(lang_code)。"""
        if callback not in cls._language_changed_callbacks:
            cls._language_changed_callbacks.append(callback)

    @classmethod
    def _emit_language_changed(cls) -> None:
        for cb in list(cls._language_changed_callbacks):
            try:
                cb(cls._current_lang)
            except Exception:
                pass

    # ---- 语言代码解析 ----
    @classmethod
    def _resolve_language_code(cls, lang: str) -> str:
        """将任意 BCP-47 代码解析为系统实际可用的语言代码。"""
        if not lang:
            return "zh"
        lang = str(lang).strip().lower()
        if lang in cls._custom_packs:
            return lang
        prefix = lang.split("-")[0]
        if prefix in cls._custom_packs:
            return prefix
        if prefix == "zh" or lang.startswith("zh"):
            return "zh"
        if prefix == "en" or lang.startswith("en"):
            return "en"
        return "en"

    # ---- 核心 get / set ----
    # P0-3: 缓存当前语言的 table 引用，避免每次 get() 都走 if/elif 链。
    # 语言切换时通过 _invalidate_table_cache() 清空。
    _table_cache: dict = {}  # {lang_code: table_dict}

    @classmethod
    def _invalidate_table_cache(cls) -> None:
        """语言切换或自定义包加载后清空 table 缓存。"""
        cls._table_cache.clear()

    @classmethod
    def _resolve_table(cls, lang: str):
        """解析语言代码对应的翻译表，带缓存。"""
        if lang in cls._table_cache:
            return cls._table_cache[lang]
        table = None
        if lang in cls._custom_packs:
            table = cls._custom_packs[lang]
        elif "-" in lang:
            prefix = lang.split("-")[0]
            if prefix in cls._custom_packs:
                table = cls._custom_packs[prefix]
            elif prefix == "zh":
                table = cls.TRANSLATIONS["zh"]
            elif prefix == "en":
                table = cls.TRANSLATIONS["en"]
        elif lang in cls.TRANSLATIONS:
            table = cls.TRANSLATIONS[lang]
        elif lang.startswith("zh"):
            table = cls.TRANSLATIONS["zh"]
        else:
            table = cls.TRANSLATIONS["en"]
        cls._table_cache[lang] = table
        return table

    @classmethod
    def get(cls, key: str, lang: str = None, **kwargs) -> str:
        """按语言取翻译；支持内置 zh/en + 自定义包 + BCP-47 前缀回退。"""
        if lang is None:
            lang = cls._current_lang

        # P0-3: 使用缓存的 table 引用，避免每次走 if/elif 链
        table = cls._resolve_table(lang)

        text = table.get(key, key) if table else key
        if text == key and table is not cls.TRANSLATIONS["en"]:
            text = cls.TRANSLATIONS["en"].get(key, key)

        if kwargs:
            try:
                text = text.format(**kwargs)
            except Exception:
                pass
        return text

    @classmethod
    def set_language(cls, lang: str) -> None:
        """设置全局语言，支持任意 BCP-47 代码。变化时触发回调。"""
        resolved = cls._resolve_language_code(lang)
        if resolved != cls._current_lang:
            cls._current_lang = resolved
            cls._invalidate_table_cache()  # P0-3: 语言变更时清空 table 缓存
            cls._emit_language_changed()
        else:
            cls._current_lang = resolved

    @classmethod
    def current_language(cls) -> str:
        return getattr(cls, "_current_lang", "zh")

    _current_lang = "zh"


# 全局翻译函数：tr("key") 或 tr("key", name="value")
def tr(key: str, **kwargs) -> str:
    """获取当前语言的翻译文本。用法: tr("settings"), tr("delete_confirm_msg", name="file.py")"""
    return I18N.get(key, **kwargs)

