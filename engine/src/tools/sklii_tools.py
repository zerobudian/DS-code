# -*- coding: utf-8 -*-
"""sklii 工具 — 从 tool_executor.py 拆分"""

import logging

logger = logging.getLogger(__name__)

import re


class SklIITools:
    """sklii 自定义工具包工具集：sklii_call, sklii_create, sklii_list"""

    def __init__(self, executor):
        self._exec = executor

    @staticmethod
    def _safe_fmt(template: str, **kwargs) -> str:
        """安全格式化：避免用户可控字符串中包含 { } 导致 .format() 抛出 KeyError。

        使用 string.Template 的 safe_substitute 作为后备，它不会对缺失的键
        或意外的花括号抛出异常。
        """
        from string import Template
        try:
            return template.format(**kwargs)
        except (KeyError, ValueError):
            # 回退到安全的模板替换
            t = Template(template)
            return t.safe_substitute(kwargs)

    def sklii_call(self, call):
        manager = getattr(self._exec, "_sklii_manager", None)
        if manager is None:
            return False, "[sklii_call] sklii 管理器未初始化"
        name = str(call.get("name", "") or "").strip()
        if not name:
            return False, "[sklii_call] 缺少 name (格式 'package.tool')"
        # 安全校验：name 长度与字符白名单
        if len(name) > 256 or "\x00" in name:
            return False, "[sklii_call] name 参数非法"
        if not re.match(r"^[A-Za-z0-9_\-\.]{1,256}$", name):
            return False, "[sklii_call] name 只允许字母/数字/下划线/点/连字符"
        args = call.get("args") or {}
        result = manager.call_tool(name, args if isinstance(args, dict) else {})
        ok = bool(result.get("ok"))
        content = str(result.get("content") or "")
        if not ok:
            error = str(result.get("error") or "调用失败")
            return False, (
                "[sklii_call] " + name + " 失败: " + error + "\n" + content[:2000]
            )
        return True, "[sklii_call] " + name + "\n" + content[:6000]

    def sklii_create(self, call):
        manager = getattr(self._exec, "_sklii_manager", None)
        if manager is None:
            return False, "[sklii_create] sklii 管理器未初始化"
        try:
            name_raw = call.get("name", "") or ""
            display_raw = call.get("display_name") or call.get("name") or ""
            desc_raw = call.get("description", "") or ""
            author_raw = call.get("author", "AI") or "AI"
            tools = call.get("tools") or []
            # —— 统一的基础类型清洗：对每个字符串字段做空字符与长度检查 ——
            def _safe_str(v, max_len):
                if isinstance(v, bool):
                    return ""
                if not isinstance(v, str):
                    try:
                        v = str(v) if v is not None else ""
                    except Exception:
                        return ""
                if len(v) > max_len:
                    return None  # 标记为非法
                if "\x00" in v or "\n" in v:
                    return None
                return v

            name = _safe_str(name_raw, 128)
            display_name = _safe_str(display_raw, 256)
            description = _safe_str(desc_raw, 2048)
            author = _safe_str(author_raw, 128)

            # 检查清洗结果
            _fields = [
                ("name", name),
                ("display_name", display_name),
                ("description", description),
                ("author", author),
            ]
            for field_name, value in _fields:
                if value is None:
                    return False, f"[sklii_create] {field_name} 参数非法（过长或含控制字符）"
            if not name:
                return False, "[sklii_create] 缺少 name"
            # tools 字段必须是 list（允许为空）；拒绝非列表类型
            if not isinstance(tools, list):
                return False, "[sklii_create] tools 必须是列表"
            # 对列表中的元素进行基础类型归一
            try:
                cleaned_tools = []
                for item in tools[:64]:
                    if isinstance(item, str):
                        if len(item) > 256:
                            return False, "[sklii_create] tools 中的工具名过长"
                        cleaned_tools.append({"name": item, "run": {"type": "http"}})
                    elif isinstance(item, dict):
                        # 字典形式：支持扁平字段，自动归一为 create_package 期望的 run 结构
                        tool_name = str(item.get("name", "")).strip()
                        if not tool_name:
                            continue
                        # 安全：禁止本地执行类型
                        run_type = (item.get("type") or "")
                        if run_type in ("python", "shell", "exec"):
                            return False, f"[sklii_create] 不允许 {run_type} 类型工具"
                        if "run" in item:
                            run = dict(item["run"])
                        else:
                            run = {}
                            for k in ("type", "method", "url", "base_url", "path", "headers", "args", "timeout"):
                                if k in item:
                                    run[k] = item[k]
                        if not run.get("type"):
                            run["type"] = "http"
                        cleaned_tools.append({
                            "name": tool_name,
                            "description": str(item.get("description", ""))[:512],
                            "run": run,
                        })
                    else:
                        try:
                            cleaned_tools.append({"name": str(item)[:256], "run": {"type": "http"}})
                        except Exception as _exc:
                            logger.debug("跳过异常项: %s", _exc, exc_info=True); continue
                tools = cleaned_tools
            except Exception as e:
                return False, "[sklii_create] tools 字段解析失败: " + str(e)
            data = {
                "name": name,
                "display_name": display_name,
                "description": description,
                "author": author,
                "tools": tools,
            }
            result = manager.create_package(data)
            if result.get("ok"):
                return True, (
                    "[sklii_create] 已创建 " + str(result.get("name", name))
                    + " (" + str(result.get("tools_count", 0)) + " 个工具)\n"
                    "路径: " + str(result.get("path", "未知"))
                )
            return False, "[sklii_create] 失败: " + str(result.get("error", ""))
        except Exception as e:
            return False, "[sklii_create] 异常: " + str(e)[:512]

    def sklii_list(self):
        manager = getattr(self._exec, "_sklii_manager", None)
        if manager is None:
            return False, "[sklii_list] sklii 管理器未初始化"
        pkgs = manager.list_packages()
        if not pkgs:
            return True, "[sklii_list] 当前项目还没有加载任何 sklii 工具包"
        lines = ["[sklii_list] 共 " + str(len(pkgs)) + " 个包:"]
        for p in pkgs:
            status = "启用" if p.get("enabled") else "禁用"
            tools = ", ".join(p.get("tool_names", [])) or "(无)"
            lines.append(
                "  - " + str(p.get("name", "?"))
                + " [" + status + "] "
                + str(p.get("display_name", ""))
            )
            lines.append("    描述: " + str(p.get("description", "(无)")))
            lines.append("    作者: " + str(p.get("author", "(未知)")))
            lines.append("    工具: " + tools)
        return True, "\n".join(lines)