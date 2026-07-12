# -*- coding: utf-8 -*-
"""SystemPromptMixin — 系统提示构建与 @文件/@符号 上下文注入（拆分自 chat_engine.py）。"""
from __future__ import annotations

from src.core.chat_engine._imports import *  # noqa: F401, F403




class SystemPromptMixin(object):
    """系统提示构建相关方法。"""

    @staticmethod
    def _is_code_location_query(query: str) -> bool:
        """判断用户问题是否包含‘找函数/代码在哪里’类的语义。"""
        if not isinstance(query, str):
            return False
        q = query.lower()
        keywords = [
            "这个函数在哪里", "这个函数在哪", "函数在哪里", "函数在哪",
            "找一下", "在哪里", "在哪",
            "where is", "where are", "find the", "locate", "search for",
        ]
        return any(k in q for k in keywords)

    def _get_latest_user_query(self) -> str:
        """从历史记录中提取最近一条用户消息文本。"""
        if self.history is None:
            return ""
        try:
            for m in reversed(self.history.messages):
                if m.get("role") == "user":
                    content = m.get("content")
                    if isinstance(content, str):
                        return content
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return ""

    def _search_code_context(self, query: str) -> str:
        """调用代码向量索引获取相关代码片段，格式化为 system prompt 片段。"""
        store = getattr(self, "_code_vector_store", None)
        if store is None:
            return ""
        try:
            results = store.search_similar(query, top_k=5)
            if not results:
                return ""
            lines = ["【相关代码片段】"]
            for r in results:
                path = r.get("path", "")
                start = r.get("start_line", 1)
                end = r.get("end_line", 1)
                score = r.get("score", 0.0)
                snippet = r.get("snippet", "")
                lines.append(f"文件: {path} (行 {start}-{end}, 相关度 {score})")
                if snippet:
                    lines.append("```")
                    lines.append(snippet)
                    lines.append("```")
            return "\n".join(lines)
        except Exception as _exc:
            logger.debug("代码检索失败: %s", _exc, exc_info=True)
            return ""

    # ── @文件/@符号 上下文注入 ──────────────────────────────
    _AT_FILE_RE = None        # 纯 @file.py
    _AT_FILE_LINE_RE = None   # @file.py:10-20
    _AT_FILE_SYMBOL_RE = None # @file.py#symbol
    _AT_SYMBOL_RE = None      # @symbol_name

    def _inject_at_mentions(self, text: str) -> str:
        """解析用户消息中的 @ 引用并注入对应上下文。

        支持语法:
          @symbol_name              - 按符号名查找并注入源码
          @path/to/file.py          - 注入文件内容（若GUI层未处理则兜底）
          @path/to/file.py:10-20    - 注入指定行范围
          @path/to/file.py#symbol   - 注入文件中指定符号
        """
        import re
        if self._AT_FILE_LINE_RE is None:
            # 文件路径：允许字母数字/._-/ 开头（相对路径），扩展名在末尾
            # 加lookbehind防止匹配email等（@前面不能是字母数字/._/）
            self._AT_FILE_RE = re.compile(
                r'(?<![\w/.\\-])@([\w./\\-]+\.\w+)(?![:#\w])'
            )
            self._AT_FILE_LINE_RE = re.compile(
                r'(?<![\w/.\\-])@([\w./\\-]+\.\w+):(\d+)(?:-(\d+))?'
            )
            self._AT_FILE_SYMBOL_RE = re.compile(
                r'(?<![\w/.\\-])@([\w./\\-]+\.\w+)#([A-Za-z_][A-Za-z0-9_]*)'
            )
            self._AT_SYMBOL_RE = re.compile(
                r'(?<![\w/.\\])@([A-Za-z_][A-Za-z0-9_.]*)(?!\.\w)'
            )

        if not text or self.sandbox is None:
            return ""

        injected_parts: List[str] = []
        work_dir = self.sandbox.work_dir
        seen = set()  # 防重复注入

        # 1. 处理 @file:line-range （最高优先级，精确匹配）
        for m in self._AT_FILE_LINE_RE.finditer(text):
            file_part = m.group(1)
            start_line = int(m.group(2))
            end_line = int(m.group(3)) if m.group(3) else start_line
            key = f"line:{file_part}:{start_line}-{end_line}"
            if key in seen:
                continue
            seen.add(key)
            snippet = self._read_file_range(file_part, start_line, end_line, work_dir)
            if snippet:
                injected_parts.append(snippet)

        # 2. 处理 @file#symbol
        for m in self._AT_FILE_SYMBOL_RE.finditer(text):
            file_part = m.group(1)
            sym_name = m.group(2)
            key = f"fsym:{file_part}#{sym_name}"
            if key in seen:
                continue
            seen.add(key)
            snippet = self._read_file_symbol(file_part, sym_name, work_dir)
            if snippet:
                injected_parts.append(snippet)

        # 2.5 处理纯 @file（不带行号/符号后缀，兜底注入文件内容）
        for m in self._AT_FILE_RE.finditer(text):
            file_part = m.group(1)
            key = f"file:{file_part}"
            if key in seen:
                continue
            # 检查是否已被file:line或file#symbol覆盖（file_part相同）
            already = any(k.startswith(f"line:{file_part}:") or k.startswith(f"fsym:{file_part}#")
                          for k in seen)
            if already:
                continue
            seen.add(key)
            snippet = self._read_whole_file(file_part, work_dir)
            if snippet:
                injected_parts.append(snippet)

        # 3. 处理 @symbol（符号名，不包含路径斜杠和扩展名的才是符号）
        symbol_names = set()
        for m in self._AT_SYMBOL_RE.finditer(text):
            name = m.group(1)
            # 排除掉已经被上面处理的情况（file.py后面跟:或#的情况已在_FILE_LINE/FILE_SYMBOL处理）
            # 排除含路径分隔符的（那是文件路径）
            if '/' in name or '\\' in name:
                continue
            # 排除数字开头的
            if name[0].isdigit():
                continue
            # 排除纯英文常用词（避免误判 "hello @home" 这类）
            if '.' in name and len(name.split('.')[-1]) == 0:
                continue
            symbol_names.add(name)
            if len(symbol_names) >= 5:
                break

        for sym_name in symbol_names:
            key = f"sym:{sym_name}"
            if key in seen:
                continue
            seen.add(key)
            snippet = self._find_symbol_source(sym_name)
            if snippet:
                injected_parts.append(snippet)

        if not injected_parts:
            return ""

        header = (
            "— 以下上下文通过 @ 引用自动注入（供分析代码使用）—\n"
            + "\n\n".join(injected_parts)
            + "\n\n— 上下文结束 —"
        )
        return header

    @staticmethod
    def _is_within_dir(p: Path, base: Path) -> bool:
        """安全检查p是否在base目录内（防止路径穿越）"""
        try:
            p.resolve().relative_to(base.resolve())
            return True
        except ValueError:
            return False

    def _read_file_range(self, file_part: str, start: int, end: int, work_dir: Path) -> str:
        """读取文件指定行范围"""
        try:
            p = Path(file_part)
            if not p.is_absolute():
                p = work_dir / p
            p = p.resolve()
            work_dir_resolved = work_dir.resolve()
            if not self._is_within_dir(p, work_dir_resolved):
                return ""
            if not p.is_file():
                return ""
            # 大小限制
            try:
                import os
                if os.path.getsize(str(p)) > 512 * 1024:
                    return f"<!-- @{file_part}:{start}-{end} 文件过大跳过 -->"
            except Exception:
                logger.debug("_read_file_range 异常", exc_info=True)
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            if start < 1:
                start = 1
            if end > len(lines):
                end = len(lines)
            if start > len(lines):
                return ""
            excerpt = lines[start - 1:end]
            # 限制注入大小
            excerpt_text = "\n".join(excerpt)
            if len(excerpt_text) > 8000:
                excerpt_text = excerpt_text[:8000] + "\n... (截断)"
            ext = p.suffix.lstrip(".")
            lang = {"py": "python", "js": "javascript", "ts": "typescript",
                    "jsx": "jsx", "tsx": "tsx", "go": "go", "rs": "rust",
                    "java": "java", "c": "c", "cpp": "cpp", "h": "c",
                    "md": "markdown", "json": "json", "yaml": "yaml",
                    "yml": "yaml", "sh": "bash", "rb": "ruby"}.get(ext, "")
            rel = self._safe_rel(p, work_dir)
            return (
                f"<!-- [@{rel}:{start}-{end}] -->\n"
                f"```{lang}\n{excerpt_text}\n```"
            )
        except Exception:
            return ""

    def _read_whole_file(self, file_part: str, work_dir: Path) -> str:
        """读取整个文件（纯@file兜底，限制大小防止OOM）"""
        try:
            p = Path(file_part)
            if not p.is_absolute():
                p = work_dir / p
            p = p.resolve()
            work_dir_resolved = work_dir.resolve()
            if not self._is_within_dir(p, work_dir_resolved):
                return ""
            if not p.is_file():
                return ""
            import os
            fsize = os.path.getsize(str(p))
            if fsize > 100 * 1024:  # 100KB上限
                return f"<!-- @{file_part} 文件过大({fsize} bytes)跳过，使用@file:line或@file#symbol精确引用 -->"
            content = p.read_text(encoding="utf-8", errors="replace")
            if len(content) > 8000:
                content = content[:8000] + "\n... (截断)"
            ext = p.suffix.lstrip(".")
            lang = {"py": "python", "js": "javascript", "ts": "typescript",
                    "jsx": "jsx", "tsx": "tsx", "go": "go", "rs": "rust",
                    "java": "java", "c": "c", "cpp": "cpp", "h": "c",
                    "md": "markdown", "json": "json", "yaml": "yaml",
                    "yml": "yaml", "sh": "bash", "rb": "ruby"}.get(ext, "")
            rel = self._safe_rel(p, work_dir)
            return f"<!-- [@{rel}] 完整文件 -->\n```{lang}\n{content}\n```"
        except Exception:
            return ""

    def _read_file_symbol(self, file_part: str, sym_name: str, work_dir: Path) -> str:
        """读取文件中指定符号"""
        try:
            p = Path(file_part)
            if not p.is_absolute():
                p = work_dir / p
            p = p.resolve()
            work_dir_resolved = work_dir.resolve()
            if not self._is_within_dir(p, work_dir_resolved):
                return ""
            if not p.is_file():
                return ""
            # 直接用AST查找符号
            return self._ast_find_symbol_in_file(p, sym_name, work_dir)
        except Exception:
            return ""

    def _ast_find_symbol_in_file(self, p: Path, sym_name: str, work_dir: Path) -> str:
        """用AST在文件中查找符号"""
        try:
            import ast
            text = p.read_text(encoding="utf-8", errors="replace")
            if p.suffix != ".py":
                return ""
            tree = ast.parse(text)
            lines = text.splitlines()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == sym_name:
                        end_line = node.end_lineno if hasattr(node, 'end_lineno') else node.lineno + 10
                        start_line = max(1, node.lineno - 1)
                        end_line = min(len(lines), end_line + 1)
                        excerpt = "\n".join(lines[start_line - 1:end_line])
                        if len(excerpt) > 5000:
                            excerpt = excerpt[:5000] + "\n... (截断)"
                        rel = self._safe_rel(p, work_dir)
                        return (
                            f"<!-- [@{rel}#{sym_name}] -->\n"
                            f"```python\n{excerpt}\n```"
                        )
        except Exception:
            logger.debug("_ast_find_symbol_in_file 异常", exc_info=True)
        return ""

    def _find_symbol_source(self, sym_name: str) -> str:
        """按符号名查找源码"""
        try:
            from src.analysis.symbol_indexer import get_global_index
            index = get_global_index(str(self.sandbox.work_dir))
            sym = index.find_symbol(sym_name)
            if sym is None:
                return ""
            src = index.get_symbol_source(sym, context_lines=2)
            if not src:
                return ""
            if len(src) > 5000:
                src = src[:5000] + "\n... (截断)"
            ext = Path(sym.file_path).suffix.lstrip(".")
            lang = {"py": "python", "js": "javascript", "ts": "typescript"}.get(ext, ext)
            return (
                f"<!-- [@{sym.short_path}:{sym.line}] {sym.kind} `{sym.name}`"
                + (f"\n{sym.signature}" if sym.signature else "")
                + f" -->\n```{lang}\n{src}\n```"
            )
        except Exception:
            return ""

    @staticmethod
    def _safe_rel(p: Path, base: Path) -> str:
        """安全获取相对路径"""
        try:
            return str(p.resolve().relative_to(base.resolve()))
        except Exception:
            return str(p.name)

    def _build_project_context(self) -> dict:
        """自动构建项目上下文（读取 package.json / pyproject.toml / 依赖等）"""
        fallback: Dict[str, Any] = {"type": "unknown",
                    "language": self.cfg.get("language", "zh") or "zh",
                    "deps_count": 0, "test_framework": "", "build_cmd": "",
                    "entry_points": [], "summary": ""}
        try:
            from src.core.project_context import ProjectContextBuilder as _B
            work_root = str(getattr(self.sandbox, "work_dir", ".")) if self.sandbox else "."
            if not work_root:
                work_root = "."
            result = _B.analyze(work_root)
            if not isinstance(result, dict):
                return fallback
            return result
        except Exception:
            # 轻量级 fallback: 不依赖新模块
            return fallback

    def _collect_mcp_context(self) -> Tuple[List[Dict], List[Dict]]:
        """收集 MCP 服务器配置与工具描述，返回 (mcp_servers, mcp_tools)。"""
        # 收集 MCP 服务器配置（已安装列表）
        mcp_servers: List[Dict] = []
        try:
            if self.mcp_manager is not None:
                for s in self.mcp_manager.list_servers() or []:
                    mcp_servers.append({
                        "name": s.get("name", ""),
                        "display_name": s.get("display_name") or s.get("name", ""),
                        "description": str(s.get("description", "")),
                        "enabled": bool(s.get("enabled", False)),
                        "running": bool(s.get("running", False)),
                    })
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        # 收集 MCP 工具
        mcp_tools: List[Dict] = []
        try:
            if self.mcp_manager is not None:
                raw = self.mcp_manager.get_all_tools() or []
                for t in raw:
                    mcp_tools.append({
                        "name": t.get("name", ""),
                        "full_name": t.get("name", ""),
                        "display_name": t.get("display_name") or t.get("name", ""),
                        "description": str(t.get("description", "")),
                    })
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return mcp_servers, mcp_tools

    def _collect_sklii_context(self) -> Tuple[List[Dict], List[Dict]]:
        """收集 SklII 包与工具描述，返回 (sklii_packages, sklii_tools)。"""
        # 收集 SklII 包（已安装列表）
        sklii_packages: List[Dict] = []
        try:
            if self.sklii_manager is not None:
                for p in self.sklii_manager.list_packages() or []:
                    sklii_packages.append({
                        "name": p.get("name", ""),
                        "display_name": p.get("display_name") or p.get("name", ""),
                        "description": str(p.get("description", "")),
                        "enabled": bool(p.get("enabled", False)),
                        "tool_names": list(p.get("tool_names") or []),
                    })
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        # 收集 SklII 工具
        sklii_tools: List[Dict] = []
        try:
            if self.sklii_manager is not None:
                sklii_tools = list(self.sklii_manager.get_tool_definitions() or [])
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return sklii_packages, sklii_tools

    def _inject_project_context(self, parts: List[str]) -> None:
        """追加项目上下文摘要（优先使用缓存，避免每次重复 I/O）。"""
        try:
            with self._state_lock:
                ctx = self._state.get("project_context_cache")
            if ctx is None:
                ctx = self._build_project_context()
                with self._state_lock:
                    self._state["project_context_cache"] = ctx
            if ctx and isinstance(ctx, dict) and ctx.get("summary"):
                summary = ctx["summary"]
                if isinstance(summary, str) and summary.strip():
                    parts.append("\n\n[项目上下文]\n" + summary.strip())
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def _inject_memory_context(self, parts: List[str]) -> None:
        """追加各类记忆上下文：用户画像 / 长期记忆 / 记忆片段 / 自动记忆。"""
        # 注入用户画像（Honcho 用户画像）
        try:
            if self.user_profile_memory is not None:
                profile_text = self.user_profile_memory.get_profile_text()
                if profile_text:
                    parts.append("\n\n" + profile_text)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        # 注入长期全局记忆（MEMORY.md）
        try:
            if self.long_term_memory is not None:
                recall_query = self.sandbox.read_ds_md() or self.sandbox.name or ""
                ltm_snippet = self.long_term_memory.recall_as_text(recall_query, top_k=3)
                if ltm_snippet:
                    parts.append("\n\n" + ltm_snippet)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        # 注入长期记忆
        if self.memory_snippet_provider is not None:
            try:
                snippet = self.memory_snippet_provider(2000)
                if snippet and isinstance(snippet, str) and snippet.strip():
                    parts.append("\n\n" + snippet)
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        # 注入自动记忆（从用户历史消息中抽取的偏好与项目事实）
        try:
            if self.auto_memory is not None:
                # 用 DS.md 内容或项目名作为查询召回相关记忆
                recall_query = self.sandbox.read_ds_md() or self.sandbox.name or ""
                auto_snippet = self.auto_memory.recall_as_text(recall_query, top_k=5)
                if auto_snippet and auto_snippet.strip():
                    parts.append("\n\n" + auto_snippet.strip())
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def _inject_code_context(self, parts: List[str]) -> None:
        """根据用户问题的“找代码/函数在哪里”语义，注入相关代码片段。"""
        try:
            query = self._get_latest_user_query()
            if self._is_code_location_query(query):
                code_ctx = self._search_code_context(query)
                if code_ctx:
                    parts.append("\n\n" + code_ctx)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def _inject_custom_rules(self, parts: List[str]) -> None:
        """追加用户自定义规则（来自 DS.md / 配置）。"""
        try:
            cr = self.cfg.get("custom_rules", "")
            if cr and isinstance(cr, str) and cr.strip():
                header = "用户定义的助手行为规则"
                parts.append(f"\n\n---\n{header}:\n{cr.strip()}")
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def _inject_skills(self, parts: List[str]) -> None:
        """追加 Skills 技能列表到系统提示。"""
        try:
            if self.skill_manager is not None:
                skills_section = self.skill_manager.get_system_prompt_section()
                if skills_section:
                    parts.append("\n\n---\n" + skills_section)
        except Exception as _exc:
            logger.debug("注入技能列表失败: %s", _exc, exc_info=True)

    def _inject_mode_prompt(self, parts: List[str]) -> None:
        """根据当前模式追加不同的系统提示。"""
        mode = self.get_mode()
        if mode == "ultra":
            parts.append(
                "\n\n---\n【DS Code Ultra 模式】\n"
                "请按以下闭环严格执行：\n"
                "1. 规划：拆解任务，生成详细执行计划（不调用工具）\n"
                "2. 执行：按计划调用工具执行，每轮将工具调用写入 reasoning_content\n"
                "3. 验证：检查执行结果是否正确，若发现问题则回到执行步骤修正\n"
                "4. 交付：确认无误后在 content 中给出最终交付结果\n"
                "注意：当工具涉及删除文件、执行 shell/python 命令、修改关键配置时，"
                "必须先等待用户确认，不得擅自执行高危操作。"
            )
        # normal / plan 保持现有行为，不追加额外提示

    def _build_system_prompt(self) -> str:
        """构造全量系统提示 — 包含所有工具描述与动态上下文。

        本方法作为协调器：先收集 MCP/SklII 上下文构建核心提示，再依次注入
        项目上下文、记忆、代码、自定义规则、技能与模式提示。
        """
        if self.sandbox is None:
            return ""

        mcp_servers, mcp_tools = self._collect_mcp_context()
        sklii_packages, sklii_tools = self._collect_sklii_context()

        # 构建系统提示核心
        base = SystemPromptBuilder.build(
            model_name=self.cfg.model,
            username=self.cfg.get("username", "User"),
            project_name=self.sandbox.name,
            ds_md_content=self.sandbox.read_ds_md(),
            tasks=self.sandbox.get_tasks(),
            work_files=self.sandbox.list_work_files(),
            preview_files=self.sandbox.list_preview_files(),
            language=self.cfg.get("language", "zh"),
            mcp_servers=mcp_servers,
            mcp_tools=mcp_tools,
            sklii_packages=sklii_packages,
            sklii_tools=sklii_tools,
        )

        # 依次注入各段动态上下文（每段已自带分隔符，拼接后等价于原 base += 逻辑）
        parts: List[str] = []
        self._inject_project_context(parts)
        self._inject_memory_context(parts)
        self._inject_code_context(parts)
        self._inject_custom_rules(parts)
        self._inject_skills(parts)
        self._inject_mode_prompt(parts)
        return base + "".join(parts)

