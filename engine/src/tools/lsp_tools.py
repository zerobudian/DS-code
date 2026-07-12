# -*- coding: utf-8 -*-
"""LSP工具模块 — 真实Language Server集成工具集

提供基于LSP的精确代码智能功能：
- goto_definition: 跳转到符号定义
- find_references: 查找符号引用
- hover_info: 获取悬停信息（类型/文档）
- get_diagnostics: 获取诊断错误/警告
- document_symbols: 获取文档符号树

当LSP不可用时，自动fallback到现有的AST/正则实现。
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class LSPTools:
    """LSP工具集，供ToolExecutor调用"""

    def __init__(self, tool_executor):
        self._exec = tool_executor
        self._lsp_manager = None  # 延迟绑定，由ChatEngine注入

    def set_lsp_manager(self, lsp_manager):
        """设置LSP管理器引用"""
        self._lsp_manager = lsp_manager

    def _resolve_path(self, file_path: str) -> Optional[Path]:
        """解析文件路径，确保在沙箱内（使用relative_to严格检查路径边界）"""
        if not file_path:
            return None
        sandbox = self._exec.sandbox
        if sandbox is None:
            return None
        try:
            p = Path(file_path)
            if not p.is_absolute():
                p = sandbox.work_dir / p
            p = p.resolve()
            work_dir = sandbox.work_dir.resolve()
            # 严格路径边界检查（修复startswith无分隔符的穿越漏洞）
            try:
                p.relative_to(work_dir)
            except ValueError:
                return None
            return p if p.exists() else None
        except Exception:
            return None

    def _path_to_uri(self, path: Path) -> str:
        """路径转URI"""
        return f"file://{path}"

    def _format_location(self, loc: Dict[str, Any], base_path: Path) -> str:
        """格式化单个位置信息为可读字符串"""
        try:
            uri = loc.get("uri", "")
            rng = loc.get("range", {})
            start = rng.get("start", {})
            end = rng.get("end", {})
            line = start.get("line", 0) + 1  # LSP是0-based，转1-based
            col = start.get("character", 0) + 1
            end_line = end.get("line", line - 1) + 1
            end_col = end.get("character", col - 1) + 1

            # 提取相对路径
            if uri.startswith("file://"):
                fpath = Path(uri[7:])
                try:
                    rel = fpath.relative_to(base_path)
                    display_path = str(rel)
                except Exception:
                    display_path = str(fpath)
            else:
                display_path = uri

            name = loc.get("name", "")
            loc_str = f"{display_path}:{line}:{col}"
            if end_line != line or end_col != col:
                loc_str += f"-{end_line}:{end_col}"
            if name:
                loc_str += f" [{name}]"
            return loc_str
        except Exception:
            return str(loc)

    def _get_source_snippet(self, path: Path, line: int, context: int = 2) -> str:
        """获取源码片段"""
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(0, line - 1 - context)
            end = min(len(lines), line + context)
            snippet_lines = []
            for i in range(start, end):
                marker = ">" if i == line - 1 else " "
                snippet_lines.append(f"{marker}{i+1:5d} | {lines[i]}")
            return "\n".join(snippet_lines)
        except Exception:
            return ""

    # ========== 工具方法 ==========

    def goto_definition(self, call: Dict[str, Any]) -> Tuple[bool, str]:
        """跳转到符号定义
        参数:
            file: 文件路径（相对或绝对）
            line: 行号（1-based）
            character: 列号（1-based）
        """
        file_path = call.get("file", "")
        line = call.get("line", 1)
        character = call.get("character", 1)

        path = self._resolve_path(file_path)
        if path is None:
            return False, f"[goto_definition] 文件不存在或不在项目内: {file_path}"

        sandbox = self._exec.sandbox
        base_path = sandbox.work_dir.resolve() if sandbox else path.parent

        # 尝试使用LSP
        definitions = []
        lsp_used = False
        if self._lsp_manager is not None:
            try:
                uri = self._path_to_uri(path)
                # 确保文档已打开
                self._ensure_document_open(path)
                # LSP使用0-based位置
                defs = self._lsp_manager.goto_definition(
                    str(path), line - 1, character - 1
                )
                if defs:
                    definitions = defs
                    lsp_used = True
            except Exception as e:
                logger.debug("LSP goto_definition失败: %s", e, exc_info=True)

        # Fallback: 使用现有符号索引
        if not definitions:
            definitions = self._fallback_find_definition(path, line, character)

        if not definitions:
            msg = "[goto_definition] 未找到定义位置"
            if lsp_used:
                msg += " (LSP返回空结果)"
            return True, msg

        # 格式化输出
        output_lines = [f"[goto_definition] 找到 {len(definitions)} 个定义位置:"]
        output_lines.append("")
        for i, loc in enumerate(definitions, 1):
            loc_str = self._format_location(loc, base_path)
            output_lines.append(f"  {i}. {loc_str}")
            # 显示源码片段
            try:
                uri = loc.get("uri", "")
                if uri.startswith("file://"):
                    loc_path = Path(uri[7:])
                    # 校验 LSP 返回的路径在沙箱边界内，防止任意文件读取
                    sandbox = self._exec.sandbox if self._exec else None
                    if sandbox and sandbox.work_dir:
                        try:
                            loc_path.resolve().relative_to(sandbox.work_dir.resolve())
                        except ValueError:
                            continue  # 跳过沙箱外的路径
                    loc_line = loc.get("range", {}).get("start", {}).get("line", 0) + 1
                    snippet = self._get_source_snippet(loc_path, loc_line, context=2)
                    if snippet:
                        output_lines.append("     " + snippet.replace("\n", "\n     "))
                        output_lines.append("")
            except Exception:
                pass

        if not lsp_used and definitions:
            output_lines.append("")
            output_lines.append("[提示] 未连接真实Language Server，使用AST/正则索引（精度有限）")
            output_lines.append("       安装pyright/ts-server/gopls等可获得更精准结果")

        return True, "\n".join(output_lines)

    def find_references(self, call: Dict[str, Any]) -> Tuple[bool, str]:
        """查找符号引用
        参数:
            file: 文件路径
            line: 行号（1-based）
            character: 列号（1-based）
            include_declaration: 是否包含声明位置（默认true）
        """
        file_path = call.get("file", "")
        line = call.get("line", 1)
        character = call.get("character", 1)
        include_decl = call.get("include_declaration", True)

        path = self._resolve_path(file_path)
        if path is None:
            return False, f"[find_references] 文件不存在或不在项目内: {file_path}"

        sandbox = self._exec.sandbox
        base_path = sandbox.work_dir.resolve() if sandbox else path.parent

        # 尝试使用LSP
        references = []
        lsp_used = False
        if self._lsp_manager is not None:
            try:
                self._ensure_document_open(path)
                refs = self._lsp_manager.find_references(
                    str(path), line - 1, character - 1,
                    include_declaration=include_decl
                )
                if refs:
                    references = refs
                    lsp_used = True
            except Exception as e:
                logger.debug("LSP find_references失败: %s", e, exc_info=True)

        # Fallback
        if not references:
            references = self._fallback_find_references(path, line, character)

        if not references:
            msg = "[find_references] 未找到引用位置"
            if lsp_used:
                msg += " (LSP返回空结果)"
            return True, msg

        # 按文件分组
        by_file: Dict[str, List[Dict]] = {}
        for ref in references:
            uri = ref.get("uri", "")
            if uri not in by_file:
                by_file[uri] = []
            by_file[uri].append(ref)

        output_lines = [f"[find_references] 找到 {len(references)} 个引用位置（{len(by_file)}个文件）:"]
        output_lines.append("")

        for uri, refs in sorted(by_file.items()):
            try:
                if uri.startswith("file://"):
                    fpath = Path(uri[7:])
                    try:
                        rel = fpath.relative_to(base_path)
                        display = str(rel)
                    except Exception:
                        display = str(fpath)
                else:
                    display = uri
                    fpath = None

                output_lines.append(f"  {display} ({len(refs)}处):")
                # 按行号排序
                refs_sorted = sorted(refs, key=lambda r: r.get("range", {}).get("start", {}).get("line", 0))
                for ref in refs_sorted[:20]:  # 每个文件最多显示20处
                    rng = ref.get("range", {})
                    start = rng.get("start", {})
                    ref_line = start.get("line", 0) + 1
                    ref_col = start.get("character", 0) + 1
                    output_lines.append(f"    L{ref_line}:{ref_col}")
                if len(refs) > 20:
                    output_lines.append(f"    ... 还有 {len(refs) - 20} 处")
                output_lines.append("")
            except Exception:
                pass

        if not lsp_used and references:
            output_lines.append("[提示] 未连接真实Language Server，使用文本搜索（可能有遗漏或误报）")

        return True, "\n".join(output_lines)

    def hover_info(self, call: Dict[str, Any]) -> Tuple[bool, str]:
        """获取悬停信息（类型/文档）
        参数:
            file: 文件路径
            line: 行号（1-based）
            character: 列号（1-based）
        """
        file_path = call.get("file", "")
        line = call.get("line", 1)
        character = call.get("character", 1)

        path = self._resolve_path(file_path)
        if path is None:
            return False, f"[hover_info] 文件不存在或不在项目内: {file_path}"

        # 尝试使用LSP
        hover_result = None
        lsp_used = False
        if self._lsp_manager is not None:
            try:
                self._ensure_document_open(path)
                hover = self._lsp_manager.hover(str(path), line - 1, character - 1)
                if hover:
                    hover_result = hover
                    lsp_used = True
            except Exception as e:
                logger.debug("LSP hover失败: %s", e, exc_info=True)

        if not hover_result:
            # Fallback: 简单显示当前行和上下文
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                idx = line - 1
                if 0 <= idx < len(lines):
                    current_line = lines[idx]
                    snippet = self._get_source_snippet(path, line, context=3)
                    return True, (
                        f"[hover_info] 无Language Server，显示源码上下文:\n\n"
                        f"{snippet}\n\n"
                        f"[提示] 安装pyright等语言服务器可获得类型信息和文档"
                    )
            except Exception:
                pass
            return True, "[hover_info] 无可用信息（LSP未连接且无法读取源码）"

        # 格式化hover结果
        contents = hover_result.get("contents", {})
        output_lines = ["[hover_info] LSP悬停信息:"]
        output_lines.append("")

        if isinstance(contents, dict):
            kind = contents.get("kind", "")
            value = contents.get("value", "")
            if kind == "markdown":
                output_lines.append(value)
            else:
                output_lines.append(f"```\n{value}\n```")
        elif isinstance(contents, list):
            for item in contents:
                if isinstance(item, dict):
                    val = item.get("value", "")
                    if val:
                        output_lines.append(val)
                elif isinstance(item, str):
                    output_lines.append(item)
        elif isinstance(contents, str):
            output_lines.append(contents)

        # 显示源码位置
        rng = hover_result.get("range")
        if rng:
            start = rng.get("start", {})
            hline = start.get("line", 0) + 1
            snippet = self._get_source_snippet(path, hline, context=2)
            if snippet:
                output_lines.append("")
                output_lines.append("源码位置:")
                output_lines.append(snippet)

        return True, "\n".join(output_lines)

    def get_diagnostics(self, call: Dict[str, Any]) -> Tuple[bool, str]:
        """获取诊断信息（错误/警告）
        参数:
            file: (可选) 文件路径，不提供则返回所有文件诊断
            severity: (可选) 过滤严重程度: "error"|"warning"|"info"|"hint"
        """
        file_path = call.get("file", "")
        severity_filter = call.get("severity", "").lower()

        all_diags: Dict[str, List[Dict]] = {}

        # 从LSP获取诊断
        lsp_used = False
        if self._lsp_manager is not None:
            try:
                # _resolve_path 可能返回 None（路径解析失败），避免把 None 转成 "None"
                resolved = self._resolve_path(file_path) if file_path else None
                target_path = str(resolved) if resolved else None
                diags = self._lsp_manager.get_diagnostics(target_path)
                if diags:
                    all_diags = diags
                    lsp_used = True
            except Exception as e:
                logger.debug("LSP diagnostics失败: %s", e, exc_info=True)

        # 如果指定了文件，先确保它被打开（触发诊断）
        if file_path and self._lsp_manager is not None and not all_diags:
            path = self._resolve_path(file_path)
            if path:
                try:
                    self._lsp_manager.did_open(str(path))
                    # 等待诊断到达
                    import time
                    time.sleep(0.8)
                    diags = self._lsp_manager.get_diagnostics(str(path))
                    if diags:
                        all_diags = diags
                        lsp_used = True
                except Exception:
                    pass

        # Fallback: 使用quick_syntax_check
        if not all_diags and file_path:
            path = self._resolve_path(file_path)
            if path:
                fallback_diags = self._fallback_get_diagnostics(path)
                if fallback_diags:
                    all_diags = {str(path): fallback_diags}

        if not all_diags:
            msg = "[get_diagnostics] 未发现诊断问题"
            if file_path:
                msg += f" ({file_path})"
            if not lsp_used:
                msg += "\n[提示] 未连接真实Language Server，仅进行基础语法检查"
            return True, msg

        # 严重程度映射
        sev_map = {1: "error", 2: "warning", 3: "info", 4: "hint"}
        sev_order = {"error": 0, "warning": 1, "info": 2, "hint": 3}
        sev_icon = {"error": "✗", "warning": "⚠", "info": "ℹ", "hint": "?"}

        sandbox = self._exec.sandbox
        base_path = sandbox.work_dir.resolve() if sandbox else Path.cwd()

        total_errors = 0
        total_warnings = 0
        output_lines = []

        # 过滤和统计
        for uri, diags in all_diags.items():
            filtered = []
            for d in diags:
                sev_num = d.get("severity", 1)
                sev_name = sev_map.get(sev_num, "error")
                if severity_filter and sev_name != severity_filter:
                    continue
                filtered.append((sev_name, d))
                if sev_name == "error":
                    total_errors += 1
                elif sev_name == "warning":
                    total_warnings += 1

            if not filtered:
                continue

            # 显示文件路径
            try:
                if uri.startswith("file://"):
                    fpath = Path(uri[7:])
                    try:
                        rel = fpath.relative_to(base_path)
                        display = str(rel)
                    except Exception:
                        display = str(fpath)
                else:
                    display = uri
                    fpath = None
            except Exception:
                display = uri
                fpath = None

            output_lines.append(f"  {display}:")
            # 按行号和严重程度排序
            filtered.sort(key=lambda x: (
                x[1].get("range", {}).get("start", {}).get("line", 0),
                sev_order.get(x[0], 0)
            ))

            for sev_name, d in filtered:
                rng = d.get("range", {})
                start = rng.get("start", {})
                line = start.get("line", 0) + 1
                col = start.get("character", 0) + 1
                message = d.get("message", "").strip()
                code = d.get("code", "")
                source = d.get("source", "")

                icon = sev_icon.get(sev_name, "•")
                code_str = f"[{code}]" if code else ""
                source_str = f"({source})" if source else ""
                output_lines.append(f"    {icon} L{line}:{col} {sev_name.upper()} {code_str}{source_str} {message}")
            output_lines.append("")

        if not output_lines:
            return True, f"[get_diagnostics] 没有{severity_filter}级别的诊断"

        # 汇总
        summary = f"[get_diagnostics] 共发现 {total_errors} 个错误, {total_warnings} 个警告"
        if severity_filter:
            summary += f" (仅显示{severity_filter})"
        summary += "\n"
        if not lsp_used:
            summary += "[提示] 未连接真实Language Server，诊断可能不完整\n"

        return True, summary + "\n" + "\n".join(output_lines)

    def document_symbols(self, call: Dict[str, Any]) -> Tuple[bool, str]:
        """获取文档符号树（类、函数、方法等）
        参数:
            file: 文件路径
        """
        file_path = call.get("file", "")
        path = self._resolve_path(file_path)
        if path is None:
            return False, f"[document_symbols] 文件不存在或不在项目内: {file_path}"

        sandbox = self._exec.sandbox
        base_path = sandbox.work_dir.resolve() if sandbox else path.parent

        # 尝试LSP
        symbols = []
        lsp_used = False
        if self._lsp_manager is not None:
            try:
                self._ensure_document_open(path)
                syms = self._lsp_manager.document_symbols(str(path))
                if syms:
                    symbols = syms
                    lsp_used = True
            except Exception as e:
                logger.debug("LSP document_symbols失败: %s", e, exc_info=True)

        # Fallback: 使用AST解析
        if not symbols:
            symbols = self._fallback_document_symbols(path)

        if not symbols:
            return True, "[document_symbols] 未找到符号"

        # 格式化符号树
        try:
            rel_path = path.relative_to(base_path)
            display_path = str(rel_path)
        except Exception:
            display_path = str(path)

        output_lines = [f"[document_symbols] {display_path}:"]
        output_lines.append("")

        def format_symbol(sym: Dict[str, Any], indent: int = 0):
            """递归格式化符号"""
            name = sym.get("name", "")
            kind = sym.get("kind", 0)
            kind_name = self._symbol_kind_name(kind)
            rng = sym.get("range") or sym.get("location", {}).get("range", {})
            start = rng.get("start", {})
            line = start.get("line", 0) + 1
            detail = sym.get("detail", "")

            prefix = "  " * indent
            icon = self._symbol_kind_icon(kind)
            line_str = f"{prefix}{icon} {kind_name} {name}"
            if detail:
                if len(detail) > 60:
                    detail = detail[:57] + "..."
                line_str += f" — {detail}"
            line_str += f" (L{line})"
            output_lines.append(line_str)

            # 递归处理子符号
            children = sym.get("children", [])
            for child in children:
                format_symbol(child, indent + 1)

        for sym in symbols:
            format_symbol(sym)

        if not lsp_used:
            output_lines.append("")
            output_lines.append("[提示] 使用AST解析（未连接LSP），符号信息可能不完整")

        return True, "\n".join(output_lines)

    def rename_symbol(self, call: Dict[str, Any]) -> Tuple[bool, str]:
        """重命名符号（LSP支持时精准跨文件重命名，否则fallback到文本替换）
        参数:
            file: 文件路径
            line: 行号（1-based）
            character: 列号（1-based）
            new_name: 新名称
            apply: 是否直接应用修改（默认false，仅预览）
        """
        file_path = call.get("file", "")
        line = call.get("line", 1)
        character = call.get("character", 1)
        new_name = call.get("new_name", "")
        apply_changes = call.get("apply", False)

        if not new_name or not isinstance(new_name, str):
            return False, "[rename_symbol] 缺少 new_name 参数"

        path = self._resolve_path(file_path)
        if path is None:
            return False, f"[rename_symbol] 文件不存在或不在项目内: {file_path}"

        sandbox = self._exec.sandbox
        base_path = sandbox.work_dir.resolve() if sandbox else path.parent

        # 尝试使用LSP rename
        changes: Dict[str, List[Dict]] = {}
        lsp_used = False
        if self._lsp_manager is not None:
            try:
                self._ensure_document_open(path)
                chg = self._lsp_manager.rename_symbol(
                    str(path), line - 1, character - 1, new_name
                )
                if chg:
                    changes = chg
                    lsp_used = True
            except Exception as e:
                logger.debug("LSP rename失败: %s", e, exc_info=True)

        # Fallback: 仅在当前文件文本替换（保守模式）
        if not changes:
            changes = self._fallback_rename(path, line, character, new_name)

        if not changes:
            return True, "[rename_symbol] 未找到可重命名的符号"

        # 格式化预览
        total_edits = sum(len(edits) for edits in changes.values())
        output_lines = [f"[rename_symbol] 将重命名为 '{new_name}', 共 {total_edits} 处修改（{len(changes)}个文件）:"]
        output_lines.append("")

        for uri, edits in changes.items():
            try:
                if uri.startswith("file://"):
                    fpath = Path(uri[7:])
                    try:
                        rel = fpath.relative_to(base_path)
                        display = str(rel)
                    except Exception:
                        display = str(fpath)
                else:
                    display = uri
                    fpath = Path(uri) if not uri.startswith("file://") else Path(uri[7:])

                output_lines.append(f"  {display} ({len(edits)}处):")
                edits_sorted = sorted(edits, key=lambda e: e.get("range", {}).get("start", {}).get("line", 0))
                for edit in edits_sorted[:10]:
                    rng = edit.get("range", {})
                    start = rng.get("start", {})
                    eline = start.get("line", 0) + 1
                    ecol = start.get("character", 0) + 1
                    old_text = edit.get("newText", "")
                    output_lines.append(f"    L{eline}:{ecol}")
                if len(edits) > 10:
                    output_lines.append(f"    ... 还有 {len(edits) - 10} 处")
                output_lines.append("")
            except Exception:
                pass

        if apply_changes and lsp_used:
            # 应用修改
            try:
                results = self._lsp_manager.apply_workspace_edit(changes)
                applied_total = sum(results.values())
                output_lines.append(f"✓ 已应用 {applied_total} 处修改")
                # 通知LSP文件变更
                for fpath_str in results.keys():
                    try:
                        self._lsp_manager.did_save(fpath_str)
                    except Exception:
                        pass
                # 通知文件变更（更新AST索引等）
                try:
                    lsp_tools = self
                    for fpath_str in results.keys():
                        try:
                            lsp_tools.notify_file_changed(fpath_str, is_new_file=False)
                        except Exception:
                            pass
                except Exception:
                    pass
            except Exception as e:
                output_lines.append(f"✗ 应用失败: {e}")
                return False, "\n".join(output_lines)
        elif apply_changes and not lsp_used:
            # Fallback应用
            applied = self._apply_fallback_rename(path, changes, new_name)
            output_lines.append(f"✓ (无LSP，已在当前文件替换 {applied} 处)")
        else:
            output_lines.append("[提示] 传入 apply=true 以实际应用修改")
            if not lsp_used:
                output_lines.append("[提示] 未连接Language Server，仅在当前文件做保守替换预览")

        return True, "\n".join(output_lines)

    def format_document(self, call: Dict[str, Any]) -> Tuple[bool, str]:
        """使用LSP格式化文档（或fallback到black/prettier等命令行工具）
        参数:
            file: 文件路径
            apply: 是否直接应用（默认true）
        """
        file_path = call.get("file", "")
        apply_changes = call.get("apply", True)

        path = self._resolve_path(file_path)
        if path is None:
            return False, f"[format_document] 文件不存在或不在项目内: {file_path}"

        # 尝试LSP格式化
        edits = []
        lsp_used = False
        if self._lsp_manager is not None:
            try:
                self._ensure_document_open(path)
                edits = self._lsp_manager.format_document(str(path))
                if edits:
                    lsp_used = True
            except Exception as e:
                logger.debug("LSP format失败: %s", e, exc_info=True)

        if edits:
            if apply_changes:
                try:
                    # 构造正确的file:// URI
                    uri = f"file://{path.resolve()}"
                    results = self._lsp_manager.apply_workspace_edit({uri: edits})
                    output = f"[format_document] ✓ 使用LSP格式化完成，应用 {sum(results.values())} 处修改"
                    try:
                        self._lsp_manager.did_save(str(path))
                        self.notify_file_changed(str(path), is_new_file=False)
                    except Exception:
                        pass
                    return True, output
                except Exception as e:
                    return False, f"[format_document] 应用格式化失败: {e}"
            else:
                return True, f"[format_document] LSP返回 {len(edits)} 处格式化修改（预览模式，apply=true以应用）"

        # Fallback: 使用命令行工具
        return self._fallback_format(path, apply_changes)

    def _fallback_rename(self, path: Path, line: int, col: int, new_name: str) -> Dict[str, List[Dict]]:
        """Fallback: AST/文本重命名（仅当前文件）"""
        changes: Dict[str, List[Dict]] = {}
        try:
            import re
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines(keepends=True)
            if 0 <= line - 1 < len(lines):
                old_name = self._extract_word_at(lines[line - 1], col - 1)
                if not old_name or old_name == new_name:
                    return {}
                # 用正则找到所有出现位置（仅单词边界）
                edits = []
                full = text
                offset = 0
                for m in re.finditer(rf'\b{re.escape(old_name)}\b', full):
                    start_off = m.start()
                    end_off = m.end()
                    # 转回line:col
                    start_pos = self._offset_to_position(full, start_off)
                    end_pos = self._offset_to_position(full, end_off)
                    edits.append({
                        "range": {"start": start_pos, "end": end_pos},
                        "newText": new_name,
                    })
                if edits:
                    changes[f"file://{path}"] = edits
        except Exception:
            pass
        return changes

    @staticmethod
    def _offset_to_position(text: str, offset: int) -> Dict[str, int]:
        """偏移量转 {line, character} (0-based)"""
        line = 0
        char = 0
        for i, ch in enumerate(text):
            if i >= offset:
                break
            if ch == '\n':
                line += 1
                char = 0
            else:
                char += 1
        return {"line": line, "character": char}

    def _apply_fallback_rename(self, path: Path, changes: Dict[str, List[Dict]], new_name: str) -> int:
        """Fallback应用重命名（只改当前文件）"""
        applied = 0
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            for uri, edits in changes.items():
                try:
                    if uri.startswith("file://"):
                        fpath = Path(uri[7:])
                    else:
                        fpath = Path(uri)
                    if fpath.resolve() != path.resolve():
                        continue  # fallback只改当前文件

                    # 一次性基于原始text计算所有offset，从后往前替换
                    def pos_to_off(text_str: str, line: int, char: int) -> int:
                        off = 0
                        ls = text_str.split('\n')
                        for i in range(min(line, len(ls))):
                            off += len(ls[i]) + 1
                        if line < len(ls):
                            off += min(char, len(ls[line]))
                        return off

                    offset_edits = []
                    for edit in edits:
                        rng = edit.get("range", {})
                        start = rng.get("start", {})
                        end = rng.get("end", {})
                        sl, sc = start.get("line", 0), start.get("character", 0)
                        el, ec = end.get("line", 0), end.get("character", 0)
                        try:
                            so = pos_to_off(text, sl, sc)
                            eo = pos_to_off(text, el, ec)
                            nt = edit.get("newText", new_name)
                            if 0 <= so <= eo <= len(text):
                                offset_edits.append((so, eo, nt))
                        except Exception:
                            continue

                    offset_edits.sort(key=lambda x: x[0], reverse=True)
                    for so, eo, nt in offset_edits:
                        try:
                            text = text[:so] + nt + text[eo:]
                            applied += 1
                        except Exception:
                            continue
                except Exception:
                    continue
            path.write_text(text, encoding="utf-8")
        except Exception:
            pass
        return applied

    def _fallback_format(self, path: Path, apply: bool) -> Tuple[bool, str]:
        """Fallback格式化：使用命令行工具"""
        suffix = path.suffix.lower()
        import shutil
        import subprocess

        formatters = {
            ".py": [("black", ["black", "--quiet", "-"]), ("autopep8", ["autopep8", "-"]), ("yapf", ["yapf", "-"])],
            ".js": [("prettier", ["prettier", "--stdin-filepath", str(path)])],
            ".ts": [("prettier", ["prettier", "--stdin-filepath", str(path)])],
            ".jsx": [("prettier", ["prettier", "--stdin-filepath", str(path)])],
            ".tsx": [("prettier", ["prettier", "--stdin-filepath", str(path)])],
            ".json": [("prettier", ["prettier", "--stdin-filepath", str(path)])],
            ".md": [("prettier", ["prettier", "--stdin-filepath", str(path)])],
            ".css": [("prettier", ["prettier", "--stdin-filepath", str(path)])],
            ".html": [("prettier", ["prettier", "--stdin-filepath", str(path)])],
            ".go": [("gofmt", ["gofmt"])],
            ".rs": [("rustfmt", ["rustfmt", "--edition", "2021", "--emit", "stdout"])],
            ".c": [("clang-format", ["clang-format", "-"])],
            ".cpp": [("clang-format", ["clang-format", "-"])],
            ".h": [("clang-format", ["clang-format", "-"])],
            ".java": [("clang-format", ["clang-format", "-"])],
        }

        if suffix not in formatters:
            return True, f"[format_document] 无可用格式化工具（LSP未连接且未找到{suffix}的formatter）"

        try:
            original = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return False, f"[format_document] 无法读取文件: {path}"

        for name, cmd in formatters[suffix]:
            # 安全解析二进制路径（防止PATH劫持：不从项目目录查找）
            try:
                from src.tools import command_tools as _ct
                safe_bin = _ct._resolve_safe_binary(cmd[0])
                if safe_bin is None:
                    continue
                safe_cmd = [safe_bin] + list(cmd[1:])
                env = _ct._sanitize_command_env()
            except Exception:
                continue
            try:
                proc = subprocess.run(
                    safe_cmd,
                    input=original,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=str(path.parent),
                    env=env,
                )
                if proc.returncode == 0 and proc.stdout and proc.stdout != original:
                    if apply:
                        path.write_text(proc.stdout, encoding="utf-8")
                        # 通知LSP文件已修改
                        try:
                            self.notify_file_changed(str(path), is_new_file=False)
                        except Exception:
                            pass
                        return True, f"[format_document] ✓ 使用 {name} 格式化完成"
                    else:
                        return True, f"[format_document] {name} 可格式化（{len(proc.stdout)}字节，apply=true以应用）"
                elif proc.returncode == 0 and proc.stdout == original:
                    return True, f"[format_document] 文件已经是标准格式（使用 {name} 检查）"
            except Exception as e:
                logger.debug("formatter %s 失败: %s", name, e)
                continue

        return True, f"[format_document] 未找到可用的格式化工具（LSP未连接，尝试过: {[c[0] for c in formatters[suffix]]}）"

    def _symbol_kind_name(self, kind: int) -> str:
        """LSP SymbolKind数值转名称"""
        names = {
            1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class",
            6: "Method", 7: "Property", 8: "Field", 9: "Constructor", 10: "Enum",
            11: "Interface", 12: "Function", 13: "Variable", 14: "Constant",
            15: "String", 16: "Number", 17: "Boolean", 18: "Array", 19: "Object",
            20: "Key", 21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event",
            25: "Operator", 26: "TypeParameter",
        }
        return names.get(kind, f"Kind{kind}")

    def _symbol_kind_icon(self, kind: int) -> str:
        """符号类型图标"""
        icons = {
            5: "📦",   # Class
            6: "🔧",   # Method
            12: "ƒ",   # Function
            10: "📋",  # Enum
            11: "📐",  # Interface
            23: "🏗",  # Struct
            9: "➕",   # Constructor
            7: "📌",   # Property
            8: "📎",   # Field
            13: "📊",  # Variable
            14: "🔒",  # Constant
            2: "📁",   # Module
            4: "📦",   # Package
        }
        return icons.get(kind, "•")

    def _ensure_document_open(self, path: Path):
        """确保文档在LSP中已打开"""
        if self._lsp_manager is None:
            return
        try:
            self._lsp_manager.did_open(str(path))
        except Exception:
            pass

    def notify_file_changed(self, file_path: str, is_new_file: bool = False):
        """通知LSP文件已变更（在编辑/创建后调用）
        :param file_path: 相对路径或绝对路径
        :param is_new_file: 是否是新创建的文件
        """
        if self._lsp_manager is None:
            return
        path = self._resolve_path(file_path)
        if path is None:
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            if is_new_file:
                self._lsp_manager.did_open(str(path), text)
            else:
                self._lsp_manager.did_change(str(path), text)
                self._lsp_manager.did_save(str(path), text)
        except Exception as e:
            logger.debug("LSP文件变更通知失败: %s", e, exc_info=True)

    def notify_file_deleted(self, file_path: str):
        """通知LSP文件已删除/关闭"""
        if self._lsp_manager is None:
            return
        path = self._resolve_path(file_path)
        if path is None:
            return
        try:
            self._lsp_manager.did_close(str(path))
        except Exception:
            pass

    # ========== Fallback 实现（不依赖LSP） ==========

    def _fallback_find_definition(self, path: Path, line: int, col: int) -> List[Dict]:
        """Fallback: 使用AST/正则查找定义"""
        results = []
        try:
            import ast
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            if 0 <= line - 1 < len(lines):
                current_line = lines[line - 1]
                # 提取光标下的标识符
                word = self._extract_word_at(current_line, col - 1)
                if not word:
                    return []

                # 尝试AST解析
                try:
                    tree = ast.parse(text)
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            if node.name == word:
                                results.append({
                                    "uri": f"file://{path}",
                                    "range": {
                                        "start": {"line": node.lineno - 1, "character": node.col_offset},
                                        "end": {"line": node.end_lineno - 1 if hasattr(node, 'end_lineno') else node.lineno - 1,
                                                "character": node.end_col_offset if hasattr(node, 'end_col_offset') else node.col_offset}
                                    },
                                    "name": word,
                                })
                except Exception:
                    pass

                # 如果AST没找到，用正则搜索当前文件
                if not results:
                    import re
                    pattern = rf'(?:def|class)\s+{re.escape(word)}\b'
                    for i, l in enumerate(lines):
                        if re.search(pattern, l):
                            match = re.search(rf'\b{re.escape(word)}\b', l)
                            char_pos = match.start() if match else 0
                            results.append({
                                "uri": f"file://{path}",
                                "range": {
                                    "start": {"line": i, "character": char_pos},
                                    "end": {"line": i, "character": char_pos + len(word)}
                                },
                                "name": word,
                            })
        except Exception:
            pass
        return results

    def _fallback_find_references(self, path: Path, line: int, col: int) -> List[Dict]:
        """Fallback: 文本搜索引用"""
        results = []
        try:
            import re
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            if 0 <= line - 1 < len(lines):
                word = self._extract_word_at(lines[line - 1], col - 1)
                if not word:
                    return []

                sandbox = self._exec.sandbox
                search_dir = sandbox.work_dir if sandbox else path.parent
                pattern = re.compile(rf'\b{re.escape(word)}\b')

                # 搜索所有相关文件（使用 os.walk 避免跟随符号链接逃逸沙箱）
                exts = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".c", ".cpp", ".h"}
                import os as _os
                for root, dirs, files in _os.walk(str(search_dir), followlinks=False):
                    # 跳过隐藏目录和常见忽略目录
                    dirs[:] = [d for d in dirs if not d.startswith(".")
                               and d not in ("__pycache__", "node_modules", ".git")]
                    for fname in files:
                        fpath = Path(root) / fname
                        if fpath.suffix not in exts:
                            continue
                        if _os.path.islink(str(fpath)):
                            continue  # 跳过符号链接文件
                        try:
                            file_text = fpath.read_text(encoding="utf-8", errors="replace")
                            for i, l in enumerate(file_text.splitlines()):
                                for match in pattern.finditer(l):
                                    results.append({
                                        "uri": f"file://{fpath}",
                                        "range": {
                                            "start": {"line": i, "character": match.start()},
                                            "end": {"line": i, "character": match.end()}
                                        },
                                        "name": word,
                                    })
                        except Exception:
                            continue
        except Exception:
            pass
        return results

    def _fallback_get_diagnostics(self, path: Path) -> List[Dict]:
        """Fallback: 基础语法检查"""
        diags = []
        suffix = path.suffix
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            if suffix == ".py":
                try:
                    import ast
                    ast.parse(text)
                except SyntaxError as e:
                    diags.append({
                        "severity": 1,  # error
                        "range": {
                            "start": {"line": (e.lineno or 1) - 1, "character": (e.offset or 1) - 1},
                            "end": {"line": (e.lineno or 1) - 1, "character": (e.offset or 1)},
                        },
                        "message": str(e.msg),
                        "source": "python-ast",
                    })
            # 检查文件编码
            try:
                path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                diags.append({
                    "severity": 2,  # warning
                    "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
                    "message": "文件编码不是有效的UTF-8",
                    "source": "encoding-check",
                })
        except Exception:
            pass
        return diags

    def _fallback_document_symbols(self, path: Path) -> List[Dict]:
        """Fallback: AST解析文档符号"""
        symbols = []
        suffix = path.suffix
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            if suffix == ".py":
                try:
                    import ast
                    tree = ast.parse(text)
                    for node in tree.body:
                        sym = self._ast_node_to_symbol(node)
                        if sym:
                            # 类的话，添加方法
                            if isinstance(node, ast.ClassDef):
                                children = []
                                for item in node.body:
                                    child = self._ast_node_to_symbol(item)
                                    if child:
                                        children.append(child)
                                sym["children"] = children
                            symbols.append(sym)
                except Exception:
                    pass
            elif suffix in (".js", ".ts", ".jsx", ".tsx"):
                # 简单正则提取
                import re
                lines = text.splitlines()
                for i, line in enumerate(lines):
                    # 函数/类声明
                    m = re.match(r'\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)', line)
                    if m:
                        symbols.append({
                            "name": m.group(1), "kind": 12,
                            "range": {"start": {"line": i, "character": m.start(1)}, "end": {"line": i, "character": m.end(1)}}
                        })
                        continue
                    m = re.match(r'\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)', line)
                    if m:
                        symbols.append({
                            "name": m.group(1), "kind": 5,
                            "range": {"start": {"line": i, "character": m.start(1)}, "end": {"line": i, "character": m.end(1)}}
                        })
                        continue
                    m = re.match(r'\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=', line)
                    if m:
                        symbols.append({
                            "name": m.group(1), "kind": 13,
                            "range": {"start": {"line": i, "character": m.start(1)}, "end": {"line": i, "character": m.end(1)}}
                        })
        except Exception:
            pass
        return symbols

    def _ast_node_to_symbol(self, node) -> Optional[Dict]:
        """AST节点转符号字典"""
        import ast
        if isinstance(node, ast.FunctionDef):
            kind = 12  # Function
            args = ", ".join(a.arg for a in node.args.args)
            detail = f"({args})"
        elif isinstance(node, ast.AsyncFunctionDef):
            kind = 12
            args = ", ".join(a.arg for a in node.args.args)
            detail = f"async ({args})"
        elif isinstance(node, ast.ClassDef):
            kind = 5  # Class
            bases = ", ".join(
                b.id if isinstance(b, ast.Name) else ast.unparse(b) if hasattr(ast, 'unparse') else "..."
                for b in node.bases
            )
            detail = f"({bases})" if bases else ""
        elif isinstance(node, ast.Assign):
            # 顶层赋值作为变量
            for target in node.targets:
                if isinstance(target, ast.Name):
                    return {
                        "name": target.id, "kind": 13,
                        "range": {
                            "start": {"line": target.lineno - 1, "character": target.col_offset},
                            "end": {"line": target.end_lineno - 1 if hasattr(target, 'end_lineno') else target.lineno - 1,
                                    "character": target.end_col_offset if hasattr(target, 'end_col_offset') else target.col_offset}
                        },
                        "detail": "",
                    }
            return None
        else:
            return None

        return {
            "name": node.name,
            "kind": kind,
            "detail": detail,
            "range": {
                "start": {"line": node.lineno - 1, "character": node.col_offset},
                "end": {
                    "line": node.end_lineno - 1 if hasattr(node, 'end_lineno') else node.lineno - 1,
                    "character": node.end_col_offset if hasattr(node, 'end_col_offset') else node.col_offset
                }
            },
            "children": [],
        }

    def _extract_word_at(self, line: str, col: int) -> str:
        """提取指定列位置的单词/标识符"""
        if not line or col < 0 or col >= len(line):
            return ""
        # 向左找单词开头
        start = col
        while start > 0 and (line[start - 1].isalnum() or line[start - 1] == "_"):
            start -= 1
        # 向右找单词结尾
        end = col
        while end < len(line) and (line[end].isalnum() or line[end] == "_"):
            end += 1
        return line[start:end]
