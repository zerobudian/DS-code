# -*- coding: utf-8 -*-
"""项目上下文分析器。

读取项目根目录中的常见配置文件（package.json、pyproject.toml、requirements.txt、
tsconfig.json、Cargo.toml、CMakeLists.txt、go.mod、Makefile、.git/config），
汇总项目类型、语言、依赖数量、测试框架、构建命令与入口文件。
"""
import logging

logger = logging.getLogger(__name__)

import json
import os
import re
import threading
import time
from typing import Dict, List, Optional


class ProjectContextBuilder:
    """项目上下文构建器。静态方法驱动，外部可直接调用。"""

    # 项目上下文缓存：防御 CWE-444 缓存中毒
    # key: resolved_root, value: {"result": dict, "timestamp": float, "mtimes": dict}
    _cache: Dict[str, Dict] = {}
    _cache_lock = threading.Lock()
    _CACHE_TTL_SECONDS = 60  # 缓存有效期 60 秒

    @staticmethod
    def _cache_key(resolved_root: str) -> str:
        return str(resolved_root)

    @classmethod
    def _load_cached(cls, resolved_root: str, current_files: List[str]) -> Optional[Dict]:
        """加载有效缓存：要求未过期、文件列表一致且所有被扫描文件的 mtime 未改变。"""
        key = cls._cache_key(resolved_root)
        with cls._cache_lock:
            entry = cls._cache.get(key)
        if entry is None:
            return None
        now = time.time()
        if now - entry.get("timestamp", 0) > cls._CACHE_TTL_SECONDS:
            return None
        # 文件列表变更（新增/删除）时失效，防止新配置文件被忽略
        cached_files = entry.get("files") or []
        if set(cached_files) != set(current_files):
            return None
        mtimes = entry.get("mtimes") or {}
        for path, cached_mtime in mtimes.items():
            try:
                if not os.path.isfile(path):
                    return None
                current = os.path.getmtime(path)
            except OSError:
                return None
            # 允许 1ms 浮点误差
            if abs(current - cached_mtime) > 0.001:
                return None
        return entry.get("result")

    @classmethod
    def _save_cached(cls, resolved_root: str, result: Dict, files: List[str],
                     mtimes: Dict[str, float]) -> None:
        key = cls._cache_key(resolved_root)
        with cls._cache_lock:
            cls._cache[key] = {
                "result": result,
                "timestamp": time.time(),
                "files": list(files),
                "mtimes": mtimes,
            }

    @classmethod
    def _collect_mtimes(cls, resolved_root: str, files: List[str]) -> Dict[str, float]:
        """收集被解析配置文件当前的 mtime，用于缓存校验。"""
        mtimes: Dict[str, float] = {}
        for name in files:
            path = os.path.join(resolved_root, name)
            try:
                if os.path.isfile(path):
                    mtimes[path] = os.path.getmtime(path)
            except OSError:
                continue
        return mtimes

    @staticmethod
    def analyze(sandbox_root: str) -> Dict:
        """分析 sandbox_root 目录，返回项目上下文 dict。

        对不存在目录、权限异常、符号链接循环、超大文件均有防御。
        """
        root = str(sandbox_root or ".")
        # normalize：跟随一次 resolve（strict=False 允许路径不存在）
        # 并通过 exists 之前检查，避免循环导致 OSError
        try:
            resolved_root = os.path.realpath(root)
        except Exception:
            resolved_root = root

        if not os.path.isdir(resolved_root):
            return {
                "type": "Unknown", "language": "",
                "deps_count": 0, "test_framework": "",
                "build_cmd": "", "entry_points": [],
                "summary": f"工作目录不存在或不可访问：{root}",
            }

        files_at_root: List[str] = []
        try:
            # 对 PermissionError 等也有防御
            raw = os.listdir(resolved_root)
            files_at_root = sorted([f for f in raw if isinstance(f, str)])
        except PermissionError:
            return {
                "type": "Unknown", "language": "",
                "deps_count": 0, "test_framework": "",
                "build_cmd": "", "entry_points": [],
                "summary": "缺少读取工作目录的权限。",
            }
        except OSError as e:
            return {
                "type": "Unknown", "language": "",
                "deps_count": 0, "test_framework": "",
                "build_cmd": "", "entry_points": [],
                "summary": f"读取工作目录失败：{e}",
            }
        except Exception:
            files_at_root = []

        # 尝试使用 TTL + mtime 校验的缓存，防御 CWE-444 缓存中毒
        cached = ProjectContextBuilder._load_cached(resolved_root, files_at_root)
        if cached is not None:
            return cached

        # --- 逐个文件解析 ---
        parsed = {}
        for name in files_at_root:
            # 限制扫描的最大文件数，避免超大项目拖慢启动
            if len(parsed) >= 500:
                break
            try:
                full = os.path.join(resolved_root, name)
                # 跳过符号链接指向外部的目录（防循环）
                if os.path.islink(full) and os.path.isdir(full):
                    continue
                parsed[name] = ProjectContextBuilder._parse_file(full)
            except Exception:
                parsed[name] = {"ok": False}

        # --- 统计 ---
        signals = {
            "node": 0, "python": 0, "rust": 0, "go": 0,
            "cpp": 0, "shell": 0,
        }
        deps_count = 0
        test_framework = ""
        build_cmd = ""
        entry_points: List[str] = []

        # 解析 package.json
        pj = parsed.get("package.json") or {}
        if pj.get("ok"):
            signals["node"] += 10
            deps_count += int(pj.get("deps_count", 0))
            if pj.get("test_framework"):
                test_framework = pj["test_framework"]
            if pj.get("main"):
                entry_points.append(pj["main"])
            if pj.get("scripts"):
                scripts = pj["scripts"]
                if isinstance(scripts, dict):
                    if "build" in scripts:
                        build_cmd = "npm run build"
                    elif "test" in scripts:
                        build_cmd = "npm test"

        # 解析 pyproject.toml / requirements.txt
        py_prj = parsed.get("pyproject.toml") or {}
        if py_prj.get("ok"):
            signals["python"] += 10
            deps_count += int(py_prj.get("deps_count", 0))
            if py_prj.get("test_framework"):
                test_framework = test_framework or py_prj["test_framework"]
            if not build_cmd and py_prj.get("build_cmd"):
                build_cmd = py_prj["build_cmd"]

        req_txt = parsed.get("requirements.txt") or {}
        if req_txt.get("ok"):
            signals["python"] += 5
            deps_count += int(req_txt.get("deps_count", 0))
            if not test_framework:
                test_framework = req_txt.get("test_framework") or "pytest"
            if not build_cmd:
                build_cmd = "python -m pip install -r requirements.txt"

        # tsconfig.json
        if parsed.get("tsconfig.json", {}).get("ok"):
            signals["node"] += 3
            entry_points.append("src/index.ts")

        # Cargo.toml (Rust)
        cargo = parsed.get("Cargo.toml") or {}
        if cargo.get("ok"):
            signals["rust"] += 10
            deps_count += int(cargo.get("deps_count", 0))
            if cargo.get("entry_points"):
                entry_points.extend(cargo["entry_points"])
            if not build_cmd:
                build_cmd = "cargo build"
            if not test_framework:
                test_framework = "cargo test"

        # CMakeLists.txt
        if parsed.get("CMakeLists.txt", {}).get("ok"):
            signals["cpp"] += 8
            if not build_cmd:
                build_cmd = "cmake -S . -B build && cmake --build build"

        # go.mod
        go_mod = parsed.get("go.mod") or {}
        if go_mod.get("ok"):
            signals["go"] += 10
            deps_count += int(go_mod.get("deps_count", 0))
            if not build_cmd:
                build_cmd = "go build ./..."
            if not test_framework:
                test_framework = "go test ./..."

        # Makefile
        if parsed.get("Makefile", {}).get("ok"):
            signals["shell"] += 3
            if not build_cmd:
                build_cmd = "make"

        # --- 根据信号推断项目类型 ---
        strong = [k for k, v in signals.items() if v >= 10]
        if len(strong) == 1:
            type_map = {
                "node": "Node.js", "python": "Python", "rust": "Rust",
                "go": "Go", "cpp": "C++", "shell": "Shell",
            }
            project_type = type_map.get(strong[0], "Unknown")
        elif len(strong) > 1:
            project_type = "Mixed"
        else:
            # 弱信号
            weak = [k for k, v in signals.items() if v >= 3]
            if len(weak) == 1:
                project_type = {
                    "node": "Node.js", "python": "Python", "rust": "Rust",
                    "go": "Go", "cpp": "C++", "shell": "Shell",
                }.get(weak[0], "Unknown")
            elif len(weak) > 1:
                project_type = "Mixed"
            else:
                project_type = "Unknown"

        # language
        lang_map = {
            "Node.js": "JavaScript/TypeScript",
            "Python": "Python",
            "Rust": "Rust",
            "Go": "Go",
            "C++": "C++",
            "Shell": "Shell",
            "Mixed": "Mixed",
            "Unknown": "",
        }
        language = lang_map.get(project_type, "")

        # entry_points 去重 & 补常见入口
        common_entries = {
            "Node.js": ["src/index.js", "src/index.ts", "index.js", "main.js"],
            "Python": ["main.py", "__init__.py", "src/main.py"],
            "Rust": ["src/main.rs", "src/lib.rs"],
            "Go": ["main.go", "cmd/main.go"],
            "C++": ["src/main.cpp", "main.cpp"],
        }
        seen = set()
        cleaned_entries: List[str] = []
        for entry in entry_points:
            if not entry:
                continue
            norm = entry.strip().strip("'\"")
            if norm and norm not in seen:
                seen.add(norm)
                cleaned_entries.append(norm)
        if not cleaned_entries and project_type in common_entries:
            for cand in common_entries[project_type]:
                if os.path.isfile(os.path.join(resolved_root, cand)):
                    cleaned_entries.append(cand)
                    break

        # 摘要
        summary_parts = [f"项目类型: {project_type or '未知'}"]
        if language:
            summary_parts.append(f"主要语言: {language}")
        if deps_count:
            summary_parts.append(f"依赖约 {deps_count} 项")
        if test_framework:
            summary_parts.append(f"测试框架: {test_framework}")
        if build_cmd:
            summary_parts.append(f"构建命令: {build_cmd}")
        if cleaned_entries:
            summary_parts.append(f"入口: {', '.join(cleaned_entries[:3])}")
        summary = "；".join(summary_parts) + "。"

        result = {
            "type": project_type,
            "language": language,
            "deps_count": int(deps_count),
            "test_framework": test_framework,
            "build_cmd": build_cmd,
            "entry_points": cleaned_entries,
            "summary": summary,
        }
        # 保存结果到带 TTL + mtime 校验的缓存
        mtimes = ProjectContextBuilder._collect_mtimes(resolved_root, files_at_root)
        ProjectContextBuilder._save_cached(resolved_root, result, files_at_root, mtimes)
        return result

    @staticmethod
    def to_system_prompt(context: Dict, language: str = "zh") -> str:
        """将 context 格式化为可注入 system prompt 的字符串。

        language 支持: "zh"（中文）/"en"（英文）/"wenyan"（文言文）。
        """
        if not context:
            return ""

        lang = str(language or "zh").lower()
        try:
            if lang == "en":
                return ProjectContextBuilder._to_en(context)
            if lang == "wenyan":
                return ProjectContextBuilder._to_wenyan(context)
            return ProjectContextBuilder._to_zh(context)
        except Exception:
            return ""

    # ---------- 内部工具 ----------
    @staticmethod
    def _parse_file(path: str) -> Dict:
        """按照文件名后缀/内容粗略解析，返回带信号字段的 dict。

        安全：显式拒绝符号链接指向外部；对文件大小硬上限，避免 DoS。
        """
        name = os.path.basename(path)
        try:
            st = os.lstat(path)  # 不跟随 symlink
            import stat as _stat
            if not _stat.S_ISREG(st.st_mode):
                return {"ok": False}
        except Exception:
            return {"ok": False}
        try:
            size = st.st_size
        except Exception:
            try:
                size = os.path.getsize(path)
            except Exception:
                size = 0
        if size > 500_000:  # 不解析超过 500KB 的大文件
            return {"ok": False}

        if name == "package.json":
            return ProjectContextBuilder._parse_package_json(path)
        if name == "pyproject.toml":
            return ProjectContextBuilder._parse_pyproject_toml(path)
        if name == "requirements.txt":
            return ProjectContextBuilder._parse_requirements_txt(path)
        if name == "tsconfig.json":
            return ProjectContextBuilder._parse_tsconfig(path)
        if name == "Cargo.toml":
            return ProjectContextBuilder._parse_cargo_toml(path)
        if name == "CMakeLists.txt":
            return ProjectContextBuilder._parse_cmakelists(path)
        if name == "go.mod":
            return ProjectContextBuilder._parse_gomod(path)
        if name == "Makefile":
            return {"ok": True, "file": "Makefile"}
        if name == ".git" or (name == "config" and "/.git/" in path.replace("\\", "/")):
            return {"ok": True, "file": "git"}
        return {"ok": False}

    @staticmethod
    def _read_text(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception:
            return ""

    @staticmethod
    def _parse_package_json(path: str) -> Dict:
        try:
            data = json.loads(ProjectContextBuilder._read_text(path) or "{}")
            if not isinstance(data, dict):
                return {"ok": False}
            deps = 0
            for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                val = data.get(key)
                if isinstance(val, dict):
                    deps += len(val)
            scripts = data.get("scripts") or {}
            test_framework = ""
            if isinstance(scripts, dict):
                for skey in ("test", "lint"):
                    val = scripts.get(skey) or ""
                    if "jest" in str(val):
                        test_framework = "jest"; break
                    if "vitest" in str(val):
                        test_framework = "vitest"; break
                    if "mocha" in str(val):
                        test_framework = "mocha"; break
                    if "pytest" in str(val):
                        test_framework = "pytest"; break
            # 安全获取 bin 字典，避免对 None 调用 .values()
            bin_value = data.get("bin")
            if isinstance(bin_value, dict):
                main = next(iter(bin_value.values()), None)
            else:
                main = data.get("main") or data.get("module") or bin_value
            return {
                "ok": True, "deps_count": deps,
                "main": str(main) if main else "",
                "test_framework": test_framework,
                "scripts": scripts,
            }
        except Exception:
            return {"ok": False}

    @staticmethod
    def _parse_pyproject_toml(path: str) -> Dict:
        text = ProjectContextBuilder._read_text(path)
        if not text:
            return {"ok": False}
        # 尝试使用 tomllib（Python 3.11+），否则使用正则粗略解析
        deps_count = 0
        test_framework = ""
        build_cmd = ""
        try:
            import tomllib  # type: ignore
            try:
                data = tomllib.loads(text)
                # project.dependencies
                proj = data.get("project") if isinstance(data, dict) else None
                if isinstance(proj, dict):
                    deps = proj.get("dependencies")
                    if isinstance(deps, list):
                        deps_count += len(deps)
                    opt = proj.get("optional-dependencies") or {}
                    if isinstance(opt, dict):
                        for v in opt.values():
                            if isinstance(v, list):
                                deps_count += len(v)
                # tool.poetry.dependencies
                tool = data.get("tool") if isinstance(data, dict) else None
                if isinstance(tool, dict):
                    poetry = tool.get("poetry")
                    if isinstance(poetry, dict):
                        p_deps = poetry.get("dependencies")
                        if isinstance(p_deps, dict):
                            deps_count += len(p_deps)
                # 猜测试框架
                lower = text.lower()
                if "pytest" in lower:
                    test_framework = "pytest"
                elif "unittest" in lower:
                    test_framework = "unittest"
                elif "tox" in lower:
                    test_framework = "tox"
                if "poetry" in lower:
                    build_cmd = "poetry install"
                return {"ok": True, "deps_count": deps_count,
                        "test_framework": test_framework, "build_cmd": build_cmd}
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

        # Fallback: 正则粗略统计
        return ProjectContextBuilder._parse_pyproject_toml_fallback(text)

    @staticmethod
    def _parse_pyproject_toml_fallback(text: str) -> Dict:
        deps_count = 0
        in_deps = False
        bracket_depth = 0
        test_framework = ""
        for line in text.splitlines():
            low = line.lower().strip()
            if "pytest" in low and not test_framework:
                test_framework = "pytest"
            if low.startswith("dependencies") or "dependencies" in low and low.startswith("[") and "dependencies" in low:
                if low.count("[") > 0:
                    in_deps = True
                    bracket_depth = low.count("[") - low.count("]")
                    continue
            if in_deps:
                if "=" in line and any(ch.isalpha() for ch in line.split("=", 1)[0]):
                    if low.startswith("[") and "dependencies" not in low:
                        in_deps = False
                        continue
                stripped = line.strip().strip(",").strip("\"'")
                if stripped and stripped not in ("[", "]", "") and not stripped.startswith("#"):
                    deps_count += 1
                bracket_depth += low.count("[") - low.count("]")
                if bracket_depth <= 0 and "[" not in low:
                    in_deps = False
        return {"ok": True, "deps_count": deps_count, "test_framework": test_framework, "build_cmd": ""}

    @staticmethod
    def _parse_requirements_txt(path: str) -> Dict:
        text = ProjectContextBuilder._read_text(path)
        if not text:
            return {"ok": False}
        count = 0
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("-"):
                continue
            if re.match(r"^[A-Za-z0-9_]", s):
                count += 1
        return {"ok": True, "deps_count": count, "test_framework": ""}

    @staticmethod
    def _parse_tsconfig(path: str) -> Dict:
        try:
            text = ProjectContextBuilder._read_text(path)
            if not text:
                return {"ok": False}
            # tsconfig 允许注释 —— 简单除注释后再 load
            cleaned = re.sub(r"//[^\n]*", "", text)
            cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.S)
            data = json.loads(cleaned)
            return {"ok": True, "data": data}
        except Exception:
            return {"ok": False}

    @staticmethod
    def _parse_cargo_toml(path: str) -> Dict:
        text = ProjectContextBuilder._read_text(path)
        if not text:
            return {"ok": False}
        entry_points: List[str] = []
        deps_count = 0
        try:
            in_section = ""
            for raw in text.splitlines():
                line = raw.strip()
                if line.startswith("[") and line.endswith("]"):
                    in_section = line.strip("[]").lower()
                    continue
                if in_section == "package":
                    if "name" in line and "=" in line:
                        nm = line.split("=", 1)[1].strip().strip("\"'")
                        entry_points.append(f"src/{nm}.rs")
                if in_section == "dependencies":
                    if "=" in line and line[0].isalpha():
                        deps_count += 1
                if in_section.startswith("dependencies."):
                    if "=" in line and line[0].isalpha():
                        deps_count += 1
        except Exception as _exc:
            logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        return {"ok": True, "deps_count": deps_count, "entry_points": entry_points}

    @staticmethod
    def _parse_cmakelists(path: str) -> Dict:
        text = ProjectContextBuilder._read_text(path)
        if not text:
            return {"ok": False}
        return {"ok": True, "lines": text.count("\n")}

    @staticmethod
    def _parse_gomod(path: str) -> Dict:
        text = ProjectContextBuilder._read_text(path)
        if not text:
            return {"ok": False}
        require_count = 0
        in_require = False
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("//"):
                continue
            low = s.lower()
            if low.startswith("require"):
                if "(" in low:
                    in_require = True
                    # 单行 require xxx v1.0.0
                    parts = [p for p in low[len("require"):].strip("()").split() if p]
                    if parts and len(parts) >= 2 and "(" not in low:
                        require_count += 1
                    continue
                # 单行: require xxx v1.0.0
                parts = [p for p in low[len("require"):].strip().split() if p]
                if parts:
                    require_count += 1
                continue
            if in_require:
                if low.startswith(")") or low.startswith(")"):
                    in_require = False
                    continue
                parts = low.split()
                if len(parts) >= 1 and parts[0] not in ("//", ""):
                    require_count += 1
        return {"ok": True, "deps_count": require_count}

    # ---------- to_system_prompt 的语言变体 ----------
    @staticmethod
    def _to_zh(ctx: Dict) -> str:
        ptype = ctx.get("type") or ""
        lang = ctx.get("language") or ""
        deps = ctx.get("deps_count") or 0
        tf = ctx.get("test_framework") or ""
        bc = ctx.get("build_cmd") or ""
        ep = ctx.get("entry_points") or []
        parts = []
        if ptype:
            parts.append(f"当前项目类型为 {ptype}")
        if lang:
            parts.append(f"主要语言 {lang}")
        if deps:
            parts.append(f"约有 {deps} 个依赖")
        if tf:
            parts.append(f"测试框架 {tf}")
        if bc:
            parts.append(f"常用构建/运行命令: {bc}")
        if ep:
            parts.append("入口文件: " + ", ".join(ep[:3]))
        return "【项目上下文】" + ("；".join(parts) + "。" if parts else "未识别到明确项目信号。")

    @staticmethod
    def _to_en(ctx: Dict) -> str:
        ptype = ctx.get("type") or ""
        lang = ctx.get("language") or ""
        deps = ctx.get("deps_count") or 0
        tf = ctx.get("test_framework") or ""
        bc = ctx.get("build_cmd") or ""
        ep = ctx.get("entry_points") or []
        parts = []
        if ptype:
            parts.append(f"Project type: {ptype}")
        if lang:
            parts.append(f"Primary language: {lang}")
        if deps:
            parts.append(f"~{deps} dependencies")
        if tf:
            parts.append(f"Test framework: {tf}")
        if bc:
            parts.append(f"Build cmd: {bc}")
        if ep:
            parts.append("Entry: " + ", ".join(ep[:3]))
        return "[Project Context] " + ("; ".join(parts) + "." if parts else "No clear project signals detected.")

    @staticmethod
    def _to_wenyan(ctx: Dict) -> str:
        ptype = ctx.get("type") or ""
        lang = ctx.get("language") or ""
        deps = ctx.get("deps_count") or 0
        tf = ctx.get("test_framework") or ""
        bc = ctx.get("build_cmd") or ""
        ep = ctx.get("entry_points") or []
        parts = []
        if ptype:
            parts.append(f"此乃 {ptype} 之项目")
        if lang:
            parts.append(f"所用之语为 {lang}")
        if deps:
            parts.append(f"依赖约 {deps} 项")
        if tf:
            parts.append(f"所用测者: {tf}")
        if bc:
            parts.append(f"建造之法: {bc}")
        if ep:
            parts.append("入口: " + "、".join(ep[:3]))
        return "【项目之概】" + ("；".join(parts) + "。" if parts else "此项目之信号未明也。")

    @staticmethod
    def build_context_with_code_search(root: str, query: str,
                                       language: str = "zh") -> Dict:
        """构建项目上下文，并按 query 召回相关代码片段。

        返回的 dict 在原有项目上下文基础上增加：
          - code_search_results: list[dict]
          - code_search_summary: str（可直接注入 prompt）
        """
        from src.analysis.code_vector_store import CodeVectorStore

        context = ProjectContextBuilder.analyze(root)
        results: List[Dict] = []
        try:
            store = CodeVectorStore(root)
            store.index(force=False)
            results = store.search_similar(query, top_k=5)
        except Exception as _exc:
            logger.debug("代码搜索失败: %s", _exc, exc_info=True)

        lines = []
        if results:
            lines.append("【相关代码片段】")
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
        else:
            lines.append("【相关代码片段】未找到相关代码。")

        summary = "\n".join(lines)
        context["code_search_results"] = results
        context["code_search_summary"] = summary
        # 把代码摘要追加到项目 summary，方便直接注入 system prompt
        base_summary = context.get("summary", "")
        if base_summary:
            context["summary"] = base_summary + "\n\n" + summary
        else:
            context["summary"] = summary
        return context
