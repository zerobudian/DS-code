# -*- coding: utf-8 -*-
"""Skills技能包系统 — Markdown定义的可复用工作流

Skill 文件格式（Markdown）:
```markdown
---
name: deploy
description: 部署应用到生产环境
command: /deploy
when: 准备部署应用时使用
arguments:
  - name: environment
    description: 目标环境 (staging/production)
    required: false
    default: production
tools_allowed:
  - run_command
  - run_command_stream
  - git_status
  - git_diff
  - web_fetch
tools_forbidden:
  - delete_file
fork_context: true  # 是否fork隔离上下文执行
---

# 部署流程

执行以下步骤来部署应用：

1. 先检查当前git状态：使用 git_status
2. 检查是否有未提交的更改：使用 git_diff
3. 运行测试：使用 run_command 执行 pytest
4. 如果测试通过，构建并部署
5. 部署完成后验证健康检查

## 注意事项
- 部署前确保所有测试通过
- 生产环境部署需要确认
```

Skill加载来源:
  1. 内置技能: src/skills/*.md
  2. 用户级: ~/.config/dscode/skills/*.md
  3. 项目级: {project}/.dscode/skills/*.md

斜杠命令触发:
  - /deploy staging  -> 触发deploy skill，参数environment=staging
  - /review          -> 触发review skill
  - /skill list      -> 列出所有可用技能
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# 技能描述最大长度（注入到系统提示时）
MAX_SKILL_DESC_IN_PROMPT = 500


@dataclass
class SkillArgument:
    """技能参数定义"""
    name: str
    description: str = ""
    required: bool = False
    default: Optional[str] = None


@dataclass
class Skill:
    """单个技能包"""
    name: str
    description: str = ""
    command: str = ""           # 斜杠命令，如 "/deploy"
    when: str = ""              # 何时使用的描述
    prompt: str = ""            # Markdown提示体（执行时注入的指令）
    arguments: List[SkillArgument] = field(default_factory=list)
    tools_allowed: Optional[Set[str]] = None    # None=不限制
    tools_forbidden: Set[str] = field(default_factory=set)
    fork_context: bool = False   # 是否fork隔离上下文
    source_path: Optional[Path] = None
    builtin: bool = False

    def to_prompt_description(self) -> str:
        """生成系统提示中使用的技能描述"""
        parts = [f"- **{self.command}**: {self.description}"]
        if self.when:
            parts.append(f"  当{self.when}时使用")
        if self.arguments:
            arg_parts = []
            for arg in self.arguments:
                req = "必填" if arg.required else f"可选(默认:{arg.default})"
                arg_parts.append(f"{arg.name}({req})")
            parts.append(f"  参数: {', '.join(arg_parts)}")
        return "\n".join(parts)

    def build_execution_prompt(self, args: Optional[Dict[str, str]] = None) -> str:
        """构建执行该技能时注入的完整提示"""
        args = args or {}
        # 合并默认参数
        context: Dict[str, Any] = {}
        for arg in self.arguments:
            if arg.default is not None:
                context[arg.name] = arg.default
        context.update(args)

        lines = [f"# 技能: {self.name}"]
        lines.append(f"说明: {self.description}")
        if self.when:
            lines.append(f"触发条件: {self.when}")
        if args:
            lines.append("\n## 用户提供的参数:")
            for k, v in args.items():
                lines.append(f"- {k}: {v}")
        # 填入默认参数（仅显示未在args中提供的）
        for arg in self.arguments:
            if arg.name not in args and arg.default is not None:
                lines.append(f"- {arg.name}: {arg.default} (默认值)")
        lines.append("\n## 执行指令:")
        # 渲染模板（{{var}} 和 {% if var %}...{% endif %}）
        rendered_prompt = self._render_template(self.prompt, context)
        lines.append(rendered_prompt)
        if self.tools_forbidden:
            lines.append(f"\n## 禁止使用的工具: {', '.join(self.tools_forbidden)}")
        return "\n".join(lines)

    @staticmethod
    def _render_template(template: str, context: Dict[str, Any]) -> str:
        """极简模板渲染：支持 {{var}} 替换和 {% if var %}...{% endif %} / {% if var %}...{% else %}...{% endif %} 块。
        不引入jinja2依赖，仅处理fix.md等技能所需的基本语法。
        """
        import re as _re

        # 标记所有控制流 token：{% if xxx %}、{% else %}、{% endif %}
        token_re = _re.compile(r"\{%\s*(if\s+[^%}]+?|else|endif)\s*%\}", _re.DOTALL)

        # 使用栈匹配嵌套的 if/endif：每次找到最内层的 if/endif 对进行处理
        result = template
        for _ in range(20):  # 最多20层嵌套
            tokens = list(token_re.finditer(result))
            if not tokens:
                break

            # 扫描 token 序列，找到第一个匹配的 if/endif 对（最内层）
            found = False
            i = 0
            while i < len(tokens):
                if tokens[i].group(1).startswith("if "):
                    # 向后查找匹配的 endif，计数嵌套深度
                    depth = 0
                    j = i
                    while j < len(tokens):
                        tj = tokens[j]
                        if tj.group(1).startswith("if "):
                            depth += 1
                        elif tj.group(1) == "endif":
                            depth -= 1
                            if depth == 0:
                                # 找到匹配的 if/endif 对（最内层，无中间未配对的 if）
                                if_tok = tokens[i]
                                endif_tok = tj
                                # 提取条件
                                var_name = if_tok.group(1)[3:].strip()
                                negate = False
                                if var_name.startswith("not "):
                                    negate = True
                                    var_name = var_name[4:].strip()
                                # 提取块内容
                                block_body = result[if_tok.end():endif_tok.start()]
                                # 检查 {% else %}
                                else_parts = _re.split(r"\{%\s*else\s*%\}", block_body, maxsplit=1)
                                if_body = else_parts[0]
                                else_body = else_parts[1] if len(else_parts) > 1 else ""
                                # 判断真假
                                val = context.get(var_name, "")
                                is_truthy = bool(val) and val not in ("", "0", "false", "False", "None", 0, None, False)
                                if negate:
                                    is_truthy = not is_truthy
                                replacement = if_body if is_truthy else else_body
                                result = result[:if_tok.start()] + replacement + result[endif_tok.end():]
                                found = True
                                break
                        j += 1
                    if found:
                        break
                i += 1
            if not found:
                break

        # 处理 {{var}} 替换
        def _replace_var(m):
            var_name = m.group(1).strip()
            val = context.get(var_name, "")
            return str(val) if val is not None else ""

        result = _re.sub(r"\{\{\s*(\w+)\s*\}\}", _replace_var, result)
        return result


class SkillManager:
    """技能管理器 — 加载、注册、触发技能"""

    # 内置技能目录
    BUILTIN_SKILLS_DIRNAME = "skills"

    def __init__(self, work_dir: Optional[Path] = None, config_dir: Optional[Path] = None):
        self.work_dir = Path(work_dir) if work_dir else None
        self.config_dir = Path(config_dir) if config_dir else None
        self._skills: Dict[str, Skill] = {}  # command -> Skill
        self._lock = threading.RLock()
        self._slash_handlers: Dict[str, Callable] = {}  # 额外斜杠命令处理器
        self._loaded = False

    def register(self, skill: Skill) -> bool:
        """注册一个技能"""
        with self._lock:
            if not skill.command:
                skill.command = f"/{skill.name}"
            cmd = skill.command.lstrip("/")
            if cmd in self._skills:
                logger.debug("技能已存在，覆盖: %s", cmd)
            self._skills[cmd] = skill
            return True

    def unregister(self, command: str) -> bool:
        """移除一个技能"""
        with self._lock:
            cmd = command.lstrip("/")
            if cmd in self._skills:
                del self._skills[cmd]
                return True
            return False

    def get(self, command: str) -> Optional[Skill]:
        """获取指定技能"""
        with self._lock:
            cmd = command.lstrip("/")
            return self._skills.get(cmd)

    def list_skills(self) -> List[Skill]:
        """列出所有已注册技能"""
        with self._lock:
            return list(self._skills.values())

    def list_commands(self) -> List[str]:
        """列出所有斜杠命令"""
        with self._lock:
            cmds = [f"/{cmd}" for cmd in self._skills.keys()]
            cmds.extend([f"/{c}" for c in self._slash_handlers.keys()])
            return sorted(set(cmds))

    def register_slash_handler(self, command: str, handler: Callable) -> None:
        """注册一个额外的斜杠命令处理器（用于非skill类命令）
        handler 签名: handler(args: str) -> Optional[str]
        返回None表示继续正常流程，返回字符串表示直接回复
        """
        with self._lock:
            self._slash_handlers[command.lstrip("/")] = handler

    # ── 解析 ──────────────────────────────────────────────────
    @staticmethod
    def parse_skill_file(path: Path, builtin: bool = False) -> Optional[Skill]:
        """解析Markdown格式的技能文件"""
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.debug("读取技能文件失败(%s): %s", path, e)
            return None

        # 解析 YAML front matter
        name = path.stem
        description = ""
        command = f"/{name}"
        when = ""
        arguments: List[SkillArgument] = []
        tools_allowed: Optional[Set[str]] = None
        tools_forbidden: Set[str] = set()
        fork_context = False
        prompt = content

        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
        if fm_match:
            fm_text = fm_match.group(1)
            prompt = fm_match.group(2).strip()
            # 简单YAML解析（不依赖PyYAML）
            try:
                fm = _parse_simple_yaml(fm_text)
                name = str(fm.get("name", name))
                description = str(fm.get("description", description))
                command = str(fm.get("command", f"/{name}"))
                when = str(fm.get("when", when))
                fork_context = bool(fm.get("fork_context", False))

                if "tools_allowed" in fm and fm["tools_allowed"]:
                    ta = fm["tools_allowed"]
                    if isinstance(ta, list):
                        tools_allowed = set(str(x) for x in ta)
                if "tools_forbidden" in fm and fm["tools_forbidden"]:
                    tf = fm["tools_forbidden"]
                    if isinstance(tf, list):
                        tools_forbidden = set(str(x) for x in tf)

                if "arguments" in fm and isinstance(fm["arguments"], list):
                    for arg_def in fm["arguments"]:
                        if isinstance(arg_def, dict):
                            arguments.append(SkillArgument(
                                name=str(arg_def.get("name", "")),
                                description=str(arg_def.get("description", "")),
                                required=bool(arg_def.get("required", False)),
                                default=str(arg_def["default"]) if "default" in arg_def else None,
                            ))
            except Exception as e:
                logger.debug("解析技能front matter失败(%s): %s", path, e)

        if not description and prompt:
            # 从prompt第一行提取描述
            first_line = prompt.split("\n")[0].strip().lstrip("#").strip()
            description = first_line[:200]

        return Skill(
            name=name,
            description=description,
            command=command if command.startswith("/") else f"/{command}",
            when=when,
            prompt=prompt,
            arguments=arguments,
            tools_allowed=tools_allowed,
            tools_forbidden=tools_forbidden,
            fork_context=fork_context,
            source_path=path,
            builtin=builtin,
        )

    # ── 加载 ──────────────────────────────────────────────────
    def load_all(self) -> int:
        """从所有来源加载技能，返回加载数量"""
        count = 0
        # 1. 内置技能
        builtin_dir = Path(__file__).parent.parent / self.BUILTIN_SKILLS_DIRNAME
        count += self.load_from_dir(builtin_dir, builtin=True)

        # 2. 用户级技能
        if self.config_dir:
            user_dir = self.config_dir / "skills"
            count += self.load_from_dir(user_dir, builtin=False)

        # 3. 项目级技能
        if self.work_dir:
            project_dir = self.work_dir / ".dscode" / "skills"
            count += self.load_from_dir(project_dir, builtin=False)

        # 注册内置默认斜杠命令
        self._register_builtin_slash_commands()
        self._loaded = True
        return count

    def load_from_dir(self, directory: Path, builtin: bool = False) -> int:
        """从目录加载所有 .md 技能文件"""
        count = 0
        try:
            if not directory.exists() or not directory.is_dir():
                return 0
            for md_file in directory.glob("*.md"):
                try:
                    skill = self.parse_skill_file(md_file, builtin=builtin)
                    if skill and skill.name:
                        if self.register(skill):
                            count += 1
                except Exception as e:
                    logger.debug("加载技能文件失败(%s): %s", md_file, e)
        except Exception as e:
            logger.debug("加载技能目录失败(%s): %s", directory, e)
        return count

    def load_from_ds_md(self, ds_md_content: str) -> int:
        """从DS.md中加载内联技能块（```skill ... ```）"""
        count = 0
        pattern = re.compile(r"```skill\s+(\w+)\s*\n(.*?)```", re.DOTALL)
        for match in pattern.finditer(ds_md_content):
            try:
                name = match.group(1)
                body = match.group(2)
                # 将body包装成完整skill markdown格式
                full_md = f"---\nname: {name}\ncommand: /{name}\n---\n{body}"
                import tempfile
                with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
                    f.write(full_md)
                    tmp_path = Path(f.name)
                try:
                    skill = self.parse_skill_file(tmp_path, builtin=False)
                    if skill:
                        self.register(skill)
                        count += 1
                finally:
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("解析DS.md skill块失败: %s", e)
        return count

    def _register_builtin_slash_commands(self):
        """注册内置斜杠命令处理器"""
        # /skill list
        def _skill_list(args: str) -> str:
            skills = self.list_skills()
            if not skills:
                return "当前没有可用的技能。"
            lines = ["## 可用技能列表\n"]
            for s in skills:
                src = "内置" if s.builtin else ("项目" if s.source_path and ".dscode" in str(s.source_path) else "用户")
                lines.append(f"- **{s.command}** [{src}]: {s.description}")
            return "\n".join(lines)

        self.register_slash_handler("skill", _skill_list)
        self.register_slash_handler("skills", _skill_list)

        # /help
        def _help(args: str) -> str:
            cmds = self.list_commands()
            basic_cmds = [
                ("/help", "显示帮助信息"),
                ("/clear", "清空对话"),
                ("/model", "切换模型"),
                ("/plan", "切换到计划模式"),
                ("/deep", "切换到深度思考模式"),
                ("/compact", "压缩上下文"),
                ("/skills", "列出可用技能"),
            ]
            lines = ["## DSCode 帮助\n", "### 基础命令\n"]
            for cmd, desc in basic_cmds:
                lines.append(f"- **{cmd}**: {desc}")
            lines.append("\n### 技能命令\n")
            for s in self.list_skills():
                lines.append(f"- **{s.command}**: {s.description}")
            return "\n".join(lines)

        self.register_slash_handler("help", _help)

    # ── 触发 ──────────────────────────────────────────────────
    def parse_slash_command(self, text: str) -> Optional[Tuple[str, str, Optional[Skill]]]:
        """解析斜杠命令，返回 (command, args, skill) 或 None
        例如: "/deploy production" -> ("deploy", "production", skill_obj)
        """
        text = text.strip()
        if not text.startswith("/"):
            return None
        parts = text.split(None, 1)
        cmd = parts[0].lstrip("/")
        args = parts[1] if len(parts) > 1 else ""

        with self._lock:
            if cmd in self._skills:
                return (cmd, args, self._skills[cmd])
            if cmd in self._slash_handlers:
                return (cmd, args, None)
        return None

    def execute_slash_handler(self, command: str, args: str) -> Optional[str]:
        """执行非skill类斜杠处理器，返回回复文本或None"""
        with self._lock:
            handler = self._slash_handlers.get(command.lstrip("/"))
        if handler:
            try:
                return handler(args)
            except Exception as e:
                return f"命令执行失败: {e}"
        return None

    def get_system_prompt_section(self) -> str:
        """生成系统提示中技能部分的文本"""
        with self._lock:
            if not self._skills:
                return ""
            lines = ["## 可用技能（Skills）",
                     "你可以通过斜杠命令或在适当时候主动使用以下技能。当用户输入斜杠命令时，执行对应技能的指令：\n"]
            for skill in self._skills.values():
                desc = skill.to_prompt_description()
                if len(desc) > MAX_SKILL_DESC_IN_PROMPT:
                    desc = desc[:MAX_SKILL_DESC_IN_PROMPT] + "..."
                lines.append(desc)
            lines.append("\n使用 `/skill list` 查看所有技能详情。")
            return "\n".join(lines)


def _parse_simple_yaml(text: str) -> Dict[str, Any]:
    """简单YAML解析器（支持基本的key: value、列表、对象列表，基于缩进，不依赖PyYAML）"""
    result: Dict[str, Any] = {}
    lines = text.split("\n")

    def parse_scalar(val: str) -> Any:
        """解析标量值（字符串/数字/布尔）"""
        val = val.strip()
        if not val:
            return ""
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            return val[1:-1]
        low = val.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if low in ("null", "~", ""):
            return None
        try:
            return int(val)
        except ValueError:
            pass
        try:
            return float(val)
        except ValueError:
            pass
        return val

    def get_indent(line: str) -> int:
        return len(line) - len(line.lstrip())

    def parse_block(start_idx: int, base_indent: int):
        """从start_idx开始解析一个块（字典或列表），返回(parsed_value, next_line_idx)"""
        # 判断第一个非空行是列表(- )还是字典
        i = start_idx
        while i < len(lines):
            s = lines[i]
            if not s.strip() or s.strip().startswith("#"):
                i += 1
                continue
            ind = get_indent(s)
            if ind < base_indent:
                return {}, i
            stripped = s.strip()
            if stripped.startswith("- "):
                return parse_list(start_idx, base_indent)
            else:
                return parse_dict(start_idx, base_indent)
        return {}, i

    def parse_dict(start_idx: int, base_indent: int):
        """解析字典"""
        d: Dict[str, Any] = {}
        i = start_idx
        while i < len(lines):
            s = lines[i]
            if not s.strip() or s.strip().startswith("#"):
                i += 1
                continue
            ind = get_indent(s)
            if ind < base_indent:
                break
            if ind > base_indent:
                # 不属于当前层
                i += 1
                continue
            stripped = s.strip()
            if stripped.startswith("- "):
                # 不该出现在dict里的列表项，跳过
                i += 1
                continue
            if ":" in stripped:
                key, _, value = stripped.partition(":")
                key = key.strip()
                value = value.strip()
                if value:
                    # 简单键值对
                    d[key] = parse_scalar(value)
                    i += 1
                else:
                    # 有子块，看下一行的缩进
                    j = i + 1
                    while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
                        j += 1
                    if j < len(lines):
                        child_indent = get_indent(lines[j])
                        if child_indent > base_indent:
                            child_val, i = parse_block(j, child_indent)
                            d[key] = child_val
                        else:
                            d[key] = ""
                            i += 1
                    else:
                        d[key] = ""
                        i += 1
            else:
                i += 1
        return d, i

    def parse_list(start_idx: int, base_indent: int):
        """解析列表"""
        lst: List[Any] = []
        i = start_idx
        while i < len(lines):
            s = lines[i]
            if not s.strip() or s.strip().startswith("#"):
                i += 1
                continue
            ind = get_indent(s)
            if ind < base_indent:
                break
            if ind > base_indent:
                # 属于上一个列表项的子内容
                i += 1
                continue
            stripped = s.strip()
            if not stripped.startswith("- "):
                break
            # 列表项
            item_text = stripped[2:].strip()
            if ":" in item_text and not (item_text.startswith('"') or item_text.startswith("'")):
                # 对象项：- key: value
                # 将当前行转为"key: value"形式，然后从当前缩进+2处解析为dict
                # 先解析"- "后面的键值对
                first_key, _, first_val = item_text.partition(":")
                first_key = first_key.strip()
                first_val = first_val.strip()
                obj: Dict[str, Any] = {}
                if first_val:
                    obj[first_key] = parse_scalar(first_val)
                # 收集后续缩进行（ind > base_indent）
                j = i + 1
                # 收集属于这个对象的所有行
                obj_lines = []
                while j < len(lines):
                    ns = lines[j]
                    if not ns.strip() or ns.strip().startswith("#"):
                        j += 1
                        continue
                    nind = get_indent(ns)
                    if nind <= base_indent:
                        break
                    obj_lines.append(ns)
                    j += 1
                # 递归解析这些行为dict（用对象内容的缩进作为base）
                if obj_lines:
                    child_indent = get_indent(obj_lines[0])
                    # 把第一行也加入obj_lines的解析，重新构造
                    combined = "  " + first_key + ":" + ((" " + first_val) if first_val else "") + "\n" + "\n".join(obj_lines)
                    sub_obj, _ = parse_dict(combined.split("\n"), 0, 0) if False else _parse_inline_dict(obj_lines, child_indent, obj, first_key, first_val)
                    lst.append(sub_obj)
                else:
                    lst.append(obj)
                i = j
            else:
                # 简单标量项
                lst.append(parse_scalar(item_text))
                i += 1
        return lst, i

    def _parse_inline_dict(obj_lines, child_indent, initial_obj, first_key, first_val):
        """解析对象列表项后续缩进行为dict，合并initial_obj"""
        d = dict(initial_obj)
        # 按行解析，支持多层嵌套
        k = 0
        while k < len(obj_lines):
            ns = obj_lines[k]
            if not ns.strip() or ns.strip().startswith("#"):
                k += 1
                continue
            nind = get_indent(ns)
            if nind < child_indent:
                k += 1
                continue
            s = ns.strip()
            if ":" in s:
                key, _, value = s.partition(":")
                key = key.strip()
                value = value.strip()
                if value:
                    d[key] = parse_scalar(value)
                    k += 1
                else:
                    # 可能有子块（如嵌套的parameters里又有列表）
                    sub_lines = []
                    m = k + 1
                    while m < len(obj_lines):
                        ms = obj_lines[m]
                        if not ms.strip() or ms.strip().startswith("#"):
                            m += 1
                            continue
                        mind = get_indent(ms)
                        if mind <= nind:
                            break
                        sub_lines.append(ms)
                        m += 1
                    if sub_lines:
                        sub_indent = get_indent(sub_lines[0])
                        if sub_lines[0].strip().startswith("- "):
                            sub_val, _ = parse_list_sub(sub_lines, sub_indent)
                        else:
                            sub_val, _ = parse_dict_sub(sub_lines, sub_indent)
                        d[key] = sub_val
                        k = m
                    else:
                        d[key] = ""
                        k += 1
            else:
                k += 1
        return d, k

    def parse_dict_sub(lines_list, base_indent):
        d = {}
        k = 0
        while k < len(lines_list):
            ns = lines_list[k]
            if not ns.strip() or ns.strip().startswith("#"):
                k += 1
                continue
            nind = get_indent(ns)
            if nind < base_indent:
                k += 1
                continue
            s = ns.strip()
            if ":" in s:
                key, _, value = s.partition(":")
                key = key.strip()
                value = value.strip()
                if value:
                    d[key] = parse_scalar(value)
                    k += 1
                else:
                    sub_lines = []
                    m = k + 1
                    while m < len(lines_list):
                        ms = lines_list[m]
                        if not ms.strip() or ms.strip().startswith("#"):
                            m += 1
                            continue
                        mind = get_indent(ms)
                        if mind <= nind:
                            break
                        sub_lines.append(ms)
                        m += 1
                    if sub_lines:
                        sub_indent = get_indent(sub_lines[0])
                        if sub_lines[0].strip().startswith("- "):
                            sub_val, _ = parse_list_sub(sub_lines, sub_indent)
                        else:
                            sub_val, _ = parse_dict_sub(sub_lines, sub_indent)
                        d[key] = sub_val
                        k = m
                    else:
                        d[key] = ""
                        k += 1
            else:
                k += 1
        return d, k

    def parse_list_sub(lines_list, base_indent):
        lst = []
        k = 0
        while k < len(lines_list):
            ns = lines_list[k]
            if not ns.strip() or ns.strip().startswith("#"):
                k += 1
                continue
            nind = get_indent(ns)
            if nind < base_indent:
                break
            if nind > base_indent:
                k += 1
                continue
            s = ns.strip()
            if not s.startswith("- "):
                break
            item_text = s[2:].strip()
            if ":" in item_text and not (item_text.startswith('"') or item_text.startswith("'")):
                fk, _, fv = item_text.partition(":")
                fk, fv = fk.strip(), fv.strip()
                obj = {fk: parse_scalar(fv)} if fv else {}
                sub_lines = []
                m = k + 1
                while m < len(lines_list):
                    ms = lines_list[m]
                    if not ms.strip() or ms.strip().startswith("#"):
                        m += 1
                        continue
                    mind = get_indent(ms)
                    if mind <= nind:
                        break
                    sub_lines.append(ms)
                    m += 1
                if sub_lines:
                    sub_indent = get_indent(sub_lines[0])
                    sub_obj, _ = _parse_inline_dict(sub_lines, sub_indent, obj, fk, fv)
                    lst.append(sub_obj)
                else:
                    lst.append(obj)
                k = m
            else:
                lst.append(parse_scalar(item_text))
                k += 1
        return lst, k

    # 主入口：跳过空行和注释，找到第一个有效行
    parsed, _ = parse_block(0, 0)
    return parsed if isinstance(parsed, dict) else {}
