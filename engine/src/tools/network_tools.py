# -*- coding: utf-8 -*-
"""网络工具 — 从 tool_executor.py 拆分

安全策略:
  1. http_request 默认禁止访问内网 (127/10/172.16-31/192.168/fe80/::1/0.0.0.0)
     以及 file:/// ftp:/// gopher:// 等非 HTTP 协议（SSRF 防御）。
  2. URL 必须满足长度上限、非空、合法 scheme 白名单；包含空字符直接拒绝。
  3. 外部可通过 allow_private=True 显式开启内网访问（默认关闭）。
  4. docs_lookup 严格限制目标为 docs.python.org，由上层常量控制。
"""

import logging

logger = logging.getLogger(__name__)

import re
import html
import ipaddress
import urllib.parse as _up
import urllib.request as _ur
import urllib.error as _ue
import json as _json
from typing import Optional, List

from src.core.confirmation import ConfirmationError
from src.core import dns_pinning


# ========== SSRF / URL 合法性白名单 ==========
_ALLOWED_SCHEMES = {"http", "https"}
# 禁止的 hostname 模式（不区分大小写）
_BLOCKED_HOSTNAME_SUFFIXES = (".internal", ".local", ".localhost", "localhost")
# URL 最大长度
_MAX_URL_LEN = 8192
# 单次请求最大读取字节（避免服务器返回超大响应耗尽可能）
_MAX_RESP_BODY_BYTES = 8 * 1024 * 1024

# CGNAT (RFC 6598) 共享地址空间 — Python ≤3.10 的 ipaddress.is_private 未覆盖此段
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
# IPv6 唯一本地地址 (ULA, RFC 4193) — 与 is_private 在各版本表现一致，显式列出以防遗漏
_IPV6_ULA_NETWORK = ipaddress.ip_network("fc00::/7")


def _is_blocked_ip(ip) -> bool:
    """判断 IP 是否属于应被 SSRF 防御拦截的地址范围。

    覆盖: private / loopback / link_local / multicast / reserved / unspecified
         + CGNAT (100.64.0.0/10, RFC 6598)
         + IPv6 ULA (fc00::/7, RFC 4193)
    """
    if (ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified):
        return True
    # 显式检查 CGNAT（Python ≤3.10 的 is_private 不包含此段）
    if ip.version == 4 and ip in _CGNAT_NETWORK:
        return True
    # 显式检查 IPv6 ULA（部分 Python 版本 is_private 未包含）
    if ip.version == 6 and ip in _IPV6_ULA_NETWORK:
        return True
    return False


# ---------------------------------------------------------------------------
# DNS pinning（防御 DNS rebinding SSRF）—— 使用统一模块
# ---------------------------------------------------------------------------
# 所有 DNS pinning 逻辑统一在 src.core.dns_pinning 模块中实现，避免与
# web_search.py 各自打补丁导致互相覆盖（P0-002 修复）。


def _resolve_public_ips(hostname: str) -> List[str]:
    """解析 hostname，返回公网 IP 列表；任一地址为内网/非法则返回空列表。"""
    return dns_pinning.resolve_all_public_ips(hostname)


def _get_dns_pin_ip(hostname: str, allow_private: bool) -> Optional[str]:
    """获取可用于 DNS pinning 的安全 IP；allow_private=True 时跳过内网检查。"""
    if allow_private:
        # 允许内网时，返回任意解析到的 IP（首个）
        return dns_pinning.get_dns_pin_ip(hostname, allow_private=True)
    return dns_pinning.get_dns_pin_ip(hostname, allow_private=False)


def _parse_hostname(url: str) -> Optional[str]:
    """解析 URL 的 hostname；解析失败返回 None。"""
    try:
        parsed = _up.urlparse(url)
        host = (parsed.hostname or "").strip()
        return host or None
    except Exception:
        return None


def _is_private_ip(hostname: str) -> bool:
    """判断 hostname 是否为内网 / 环回 / 链路本地 / 组播 / 未指定地址。

    规则:
      - 直接是 IP 文字: 交给 ipaddress 模块判断
      - 显式字符串 'localhost' / 'loopback' 视为内网
      - 如果是域名, 尝试 A / AAAA 解析；解析出的任一地址为内网即拒绝
        （防御 DNS rebinding 指向内网）
    """
    if not hostname:
        return False
    low = hostname.lower().strip("[]")  # 去掉 IPv6 方括号
    if low in ("localhost", "loopback", "0.0.0.0", "0", "::", "::1"):  # nosec B104: 仅做主机名检查，非 socket.bind
        return True
    for suf in _BLOCKED_HOSTNAME_SUFFIXES:
        if low.endswith(suf):
            return True
    # 尝试解析为 IP 字符串
    try:
        ip = ipaddress.ip_address(low)
        if _is_blocked_ip(ip):
            return True
        # 成功解析为 IP 且不在黑名单中：是公网 IP，直接放行，无需 DNS 查询
        return False
    except (ValueError, TypeError) as _exc:
        logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
    # 若是普通域名：尝试 A/AAAA 解析，看看是否解析到内网（DNS rebinding 防御）
    # C-006: 使用统一的超时包装，避免恶意/缓慢 DNS 永久阻塞
    public_ips = dns_pinning.resolve_all_public_ips(hostname)
    if not public_ips:
        # 解析超时/失败/解析到内网：保守拒绝（fail-closed）
        return True
    return False


def _validate_ssrf(url: str, allow_private: bool) -> Optional[str]:
    """返回 None 表示合法；返回非空字符串表示被拒绝的原因。"""
    if not isinstance(url, str):
        return "URL 必须是字符串"
    if not url or len(url) > _MAX_URL_LEN:
        return "URL 为空或过长"
    # 空字符 / 控制字符防御
    if "\x00" in url:
        return "URL 包含非法空字符"
    if any(ord(c) < 32 or ord(c) == 127 for c in url):
        return "URL 包含非法控制字符"
    try:
        parsed = _up.urlparse(url)
    except Exception as e:
        return f"URL 解析失败: {e}"
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return f"URL scheme 非法，仅允许 http/https: {scheme or '(空)'}"
    hostname = (parsed.hostname or "").strip()
    if not hostname:
        return "URL 缺少 hostname"
    if not allow_private and _is_private_ip(hostname):
        return f"禁止访问内网 / 环回地址: {hostname}"
    return None


class NetworkTools:
    """网络工具集：web_search, web_fetch, http_request, docs_lookup, mcp_call"""

    # H-003：统一超时 / 重定向 / 响应大小配置
    CONNECT_TIMEOUT = 10
    READ_TIMEOUT = 30
    TOTAL_TIMEOUT = 60
    MAX_REDIRECTS = 5
    MAX_RESPONSE_SIZE = 10 * 1024 * 1024

    @classmethod
    def _request_timeout(cls) -> tuple:
        """requests 使用的 (connect_timeout, read_timeout) 元组。"""
        return (cls.CONNECT_TIMEOUT, cls.READ_TIMEOUT)

    def __init__(self, executor):
        self._exec = executor

    def web_search(self, call):
        # P0-2 修复: 使用 (call.get("query") or "") 防御显式 null 参数
        query = (call.get("query") or "").strip()
        if not query:
            return False, "[web_search] 缺少 query"
        # query 本身做基础清洗（避免注入攻击）
        if not isinstance(query, str) or len(query) > 2000 or "\x00" in query:
            return False, "[web_search] query 参数非法或过长"
        try:
            from src.core.web_search import WebSearch
            results = WebSearch.search(
                query,
                timeout=self._request_timeout(),
                max_redirects=self.MAX_REDIRECTS,
                max_bytes=self.MAX_RESPONSE_SIZE,
            )
            lines = ["[web_search] query: " + query + "\n"]
            for i, r in enumerate(results, 1):
                url = str(r.get("url", ""))
                # 对返回的 URL 结果做协议 / SSRF 校验
                if url:
                    if _validate_ssrf(url, allow_private=False):
                        continue  # 跳过不安全的搜索结果
                title = str(r.get("title", "(无标题)"))
                snippet = str(r.get("snippet", ""))
                lines.append(str(i) + ". " + title + "\n   " + snippet + "\n   URL: " + url + "\n")
            return True, "\n".join(lines)
        except Exception as e:
            return False, "[web_search] " + str(e)

    def web_fetch(self, call):
        # P0-2 修复: 使用 (call.get("url") or "") 防御显式 null 参数
        url = (call.get("url") or "").strip()
        if not url:
            return False, "[web_fetch] 缺少 url"
        # web_fetch 复用 SSRF 防御
        reason = _validate_ssrf(url, bool(call.get("allow_private", False)))
        if reason:
            return False, f"[web_fetch] {reason}"
        try:
            from src.core.web_search import WebFetch
            text = WebFetch.fetch(
                url,
                timeout=self._request_timeout(),
                max_redirects=self.MAX_REDIRECTS,
                max_bytes=self.MAX_RESPONSE_SIZE,
            )
            # H-3 修复: 截断时添加提示
            if len(text) > 3000:
                return True, f"[web_fetch] {url} (内容已截断，仅显示前 3000/{len(text)} 字符):\n" + text[:3000]
            return True, "[web_fetch] " + url + "\n" + text
        except Exception as e:
            return False, "[web_fetch] " + str(e)

    def http_request(self, call):
        url = str(call.get("url", "")).strip()
        method = str(call.get("method", "GET") or "GET").upper()
        headers = call.get("headers") or {}
        body = call.get("body", "") or ""
        try:
            timeout = float(call.get("timeout", 30) or 30)
        except (TypeError, ValueError):
            timeout = 30
        timeout = max(1, min(300, timeout))
        follow_redirects = bool(call.get("follow_redirects", True))
        # 安全：urllib.request 默认强制 TLS 校验，不接受 verify_ssl=False 绕过
        allow_private = bool(call.get("allow_private", False))
        # P0-4 修复: max_bytes 转换添加 try-except，防御非数字字符串
        try:
            max_bytes = int(call.get("max_bytes", _MAX_RESP_BODY_BYTES) or _MAX_RESP_BODY_BYTES)
        except (TypeError, ValueError):
            max_bytes = _MAX_RESP_BODY_BYTES
        max_bytes = max(1024, min(_MAX_RESP_BODY_BYTES * 4, max_bytes))

        # method 白名单
        if method not in {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}:
            return {"ok": False, "status_code": 0, "body": "",
                    "headers": {}, "error": f"非法 HTTP method: {method}"}

        # SSRF 防御：目标 URL
        reason = _validate_ssrf(url, allow_private)
        if reason:
            return {"ok": False, "status_code": 0, "body": "",
                    "headers": {}, "error": f"[SSRF 防御] {reason}"}

        # headers 浅过滤（只保留字符串 / 数字值）
        # —— 额外防御：禁止设置敏感头部（Host/授权类/内部代理标记），
        #    避免被用于绕过反向代理、SSRF 变种和请求 smuggling
        _BLOCKED_HEADER_PREFIXES = ("proxy-", "sec-")
        _BLOCKED_HEADERS = {
            "host", "authorization", "proxy-authorization",
            "www-authenticate", "proxy-authenticate", "cookie", "set-cookie",
            "transfer-encoding", "content-length", "connection", "upgrade",
            "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
            "via",
        }
        cleaned_headers = {}
        try:
            if isinstance(headers, dict):
                for k, v in headers.items():
                    if not isinstance(k, (str, bytes)):
                        continue
                    if v is None:
                        continue
                    ks = str(k)
                    kl = ks.lower().strip()
                    if not kl:
                        continue
                    if kl in _BLOCKED_HEADERS:
                        continue
                    if any(kl.startswith(p) for p in _BLOCKED_HEADER_PREFIXES):
                        continue
                    if len(ks) > 256 or "\x00" in ks or "\n" in ks or "\r" in ks:
                        continue
                    vs = str(v)
                    if len(vs) > 8192 or "\x00" in vs or "\n" in vs or "\r" in vs:
                        continue
                    cleaned_headers[ks] = vs
        except Exception as e:
            return {"ok": False, "status_code": 0, "body": "",
                    "headers": {}, "error": "headers 解析失败: " + str(e)}

        try:
            req = _ur.Request(url, method=method)
        except TypeError:
            req = _ur.Request(url)
            req.get_method = lambda: method  # type: ignore[assignment]

        for k, v in cleaned_headers.items():
            try:
                req.add_header(k, v)
            except Exception as _exc:
                logger.debug("跳过异常项: %s", _exc, exc_info=True); continue

        data = None
        if body:
            if isinstance(body, (dict, list)):
                try:
                    body_bytes = _json.dumps(body, ensure_ascii=False).encode("utf-8")
                    if not any(h.lower() == "content-type" for h in cleaned_headers.keys()):
                        req.add_header("Content-Type", "application/json; charset=utf-8")
                except Exception as e:
                    return {"ok": False, "status_code": 0, "body": "",
                            "headers": {}, "error": "JSON body 序列化失败: " + str(e)}
            elif isinstance(body, (bytes, bytearray)):
                body_bytes = bytes(body)
            else:
                body_bytes = str(body).encode("utf-8", errors="replace")
            if len(body_bytes) > 8 * 1024 * 1024:
                return {"ok": False, "status_code": 0, "body": "",
                        "headers": {}, "error": "body 超过 8MB 上限"}
            data = body_bytes

        handlers = []
        # verify_ssl 已被强制为 True，不再接受禁用 TLS 校验的请求

        # 重定向处理器：跟随重定向时，再次做 SSRF 校验 + DNS pinning
        class _RedirectHandler(_ur.HTTPRedirectHandler):
            def redirect_request(self, req_obj, fp, code, msg, hdrs, newurl):  # noqa: N802
                if not follow_redirects:
                    return None
                # 目标 URL 也做 SSRF 校验
                rsn = _validate_ssrf(str(newurl), allow_private)
                if rsn:
                    return None  # 拒绝跳转
                # 为跳转目标设置 DNS pin，防止 rebinding
                try:
                    parsed_new = _up.urlparse(str(newurl))
                    new_host = (parsed_new.hostname or "").lower().strip("[]")
                    new_pin = _get_dns_pin_ip(new_host, allow_private)
                    if new_pin:
                        dns_pinning.set_dns_pin(new_host, new_pin)
                    else:
                        # fail-closed: 无法获取安全 IP 则拒绝重定向
                        return None
                except Exception:
                    # fail-closed: 异常时也拒绝重定向
                    return None
                return super().redirect_request(req_obj, fp, code, msg, hdrs, newurl)

        handlers.append(_RedirectHandler())
        opener = None
        try:
            opener = _ur.build_opener(*handlers)
        except Exception as e:
            return {"ok": False, "status_code": 0, "body": "",
                    "headers": {}, "error": "构建 opener 失败: " + str(e)}
        # 设置初始请求的 DNS pin，并在上下文内临时激活 DNS pinning
        pin_set = False
        try:
            parsed_url = _up.urlparse(url)
            hostname = (parsed_url.hostname or "").lower().strip("[]")
            pin_ip = _get_dns_pin_ip(hostname, allow_private)
            if pin_ip:
                dns_pinning.ensure_dns_patch()
                dns_pinning.set_dns_pin(hostname, pin_ip)
                pin_set = True
            else:
                dns_pinning.clear_dns_pin()
                # P1-006 修复: DNS pin 设置失败时 fail-closed（包括异常情况）
                if hostname:
                    return {"ok": False, "status_code": 0, "body": "",
                            "headers": {}, "error": "SSRF 防护：无法解析到安全的 IP 地址"}
        except Exception as _exc:
            dns_pinning.clear_dns_pin()
            logger.debug("DNS pin 设置失败: %s", _exc, exc_info=True)
            return {"ok": False, "status_code": 0, "body": "",
                    "headers": {}, "error": "SSRF 防护：DNS 解析失败"}
        try:
            resp = opener.open(req, data=data, timeout=timeout)
            try:
                status_code = resp.getcode()
                resp_headers = {}
                try:
                    for k, v in resp.headers.items():
                        if isinstance(k, str) and isinstance(v, str):
                            resp_headers[k] = v[:2048]
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
                try:
                    raw = resp.read(max_bytes)
                except Exception:
                    raw = b""
                try:
                    text = raw.decode("utf-8", errors="replace")
                except Exception:
                    text = str(raw)
                snippet = text[:4000]

                return {
                    "ok": True,
                    "status_code": int(status_code),
                    "body": snippet,
                    "headers": resp_headers,
                    "error": None,
                }
            finally:
                try:
                    resp.close()
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
        except _ue.HTTPError as he:
            status_code = getattr(he, "code", 0)
            err_body = ""
            try:
                raw = he.read(max_bytes)
                err_body = raw.decode("utf-8", errors="replace")[:4000]
            except Exception:
                err_body = ""
            return {
                "ok": False,
                "status_code": int(status_code),
                "body": err_body,
                "headers": {},
                "error": "HTTPError " + str(status_code),
            }
        except _ue.URLError as ue:
            return {"ok": False, "status_code": 0, "body": "",
                    "headers": {}, "error": "URLError " + str(getattr(ue, "reason", ue))}
        except Exception as e:
            return {"ok": False, "status_code": 0, "body": "",
                    "headers": {}, "error": str(e)}
        finally:
            dns_pinning.clear_dns_pin()
            try:
                if opener is not None:
                    opener.close()
            except Exception as _exc:
                logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)

    def docs_lookup(self, call):
        query = str(call.get("query", "")).strip()
        language = str(call.get("language", "en") or "en").lower()[:2] or "en"
        try:
            max_lines = int(call.get("max_lines", 300) or 300)
        except (TypeError, ValueError):
            max_lines = 300

        if not query:
            return False, "[docs_lookup] 缺少 query 参数"

        lang_path = ""
        if language != "en":
            if language == "zh":
                lang_path = "/zh-cn"
            else:
                lang_path = ""

        encoded_q = _up.quote(query, safe="/")
        search_url = "https://docs.python.org/3" + lang_path + "/search.html?q=" + encoded_q + "&check_keywords=yes&area=default"
        prefix = "[docs_lookup] " + query + " -> " + search_url
        result = self.http_request({
            "url": search_url,
            "method": "GET",
            "timeout": 30,
            "verify_ssl": True,
            # docs.python.org 是外部域名，正常不需 allow_private
            "allow_private": False,
        })
        if isinstance(result, dict):
            ok = result.get("ok", False)
            payload = result.get("body", "") or result.get("error", "")
        else:
            ok = False
            payload = str(result)
        if not ok:
            return False, prefix + "\n请求失败:\n" + str(payload)

        text = str(payload)
        body_start = text.find("body:")
        if body_start >= 0:
            html_text = text[body_start + len("body:"):]
        else:
            html_text = text

        def _strip_html(s):
            s = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", s)
            s = re.sub(r"(?is)<a\b[^>]*>(.*?)</a>", r" \1 ", s)
            s = re.sub(r"<[^>]+>", " ", s)
            s = html.unescape(s)
            s = re.sub(r"[ \t\r\f\v]+", " ", s)
            s = re.sub(r"\n{3,}", "\n\n", s)
            return s.strip()

        plain = _strip_html(html_text)
        lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
        summary_lines = []
        for ln in lines:
            if len(summary_lines) >= max_lines:
                break
            summary_lines.append(ln[:300])

        if not summary_lines:
            return True, prefix + "\n（未在 docs.python.org 上找到文字结果；请直接访问: " + search_url + "）"

        out = [prefix, "---"]
        out.extend(summary_lines[:max_lines])
        out.append("---")
        out.append("原始链接: " + search_url)
        return True, "\n".join(out)

    def mcp_call(self, call):
        # 子代理/受限上下文禁止通过 MCP 调用外部工具
        subagent_restrictions = getattr(self._exec, "_subagent_restrictions", {}) or {}
        if subagent_restrictions.get("forbid_commands", False) or getattr(self._exec, "_forbid_commands", False):
            return False, "[mcp_call] 已禁用 (子代理不可调用 MCP 工具)"
        server = call.get("server_name", "")
        tool_name = call.get("tool_name", "")
        args = call.get("args", {}) or {}
        # args 必须是 dict（上层允许任意类型，这里二次防御）
        if not isinstance(args, dict):
            return False, "[mcp_call] args 必须是字典对象"

        # 兼容 system_prompt 中的 command 格式: "server_name:tool_name --arg value"
        if not server and not tool_name:
            cmd = call.get("command", "")
            if cmd and isinstance(cmd, str) and cmd.strip():
                cmd = cmd.strip()
                # —— 长度防御：整串不超过 1024；拒绝空字符 / 换行 ——
                if len(cmd) > 1024 or "\x00" in cmd or "\n" in cmd or "\r" in cmd:
                    return False, "[mcp_call] command 参数过长或含非法字符"
                if ":" in cmd:
                    parts = cmd.split(":", 1)
                    server = parts[0].strip()
                    rest = parts[1].strip() if len(parts) > 1 else ""
                    if " --" in rest:
                        tool_parts = rest.split(" --", 1)
                        tool_name = tool_parts[0].strip()
                        arg_str = tool_parts[1].strip() if len(tool_parts) > 1 else ""
                        if arg_str:
                            # —— arg_str 长度上限 + token 数上限；限制 key/value 字符集 ——
                            if len(arg_str) > 512:
                                return False, "[mcp_call] 参数串过长"
                            _KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]{0,63}$")
                            tokens = arg_str.split()
                            if len(tokens) > 32:
                                return False, "[mcp_call] 参数个数过多"
                            i = 0
                            while i < len(tokens):
                                raw_key = tokens[i].lstrip("-")
                                if not raw_key or not _KEY_RE.match(raw_key):
                                    return False, "[mcp_call] 参数名非法: " + raw_key
                                if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                                    val = tokens[i + 1]
                                    # 对 value 做字符与长度限制：禁止空字符；限制 256 字符
                                    if len(val) > 256 or "\x00" in val or "\n" in val:
                                        return False, "[mcp_call] 参数值非法"
                                    args[raw_key] = val
                                    i += 2
                                else:
                                    args[raw_key] = True
                                    i += 1
                    else:
                        tool_name = rest.strip()

        if not (server and tool_name):
            return False, "[mcp_call] 需要 server_name 和 tool_name"
        # —— 对 server/tool_name 做基础字符集与长度限制，避免注入 ——
        if not isinstance(server, str) or not isinstance(tool_name, str):
            return False, "[mcp_call] server_name/tool_name 必须是字符串"
        if len(server) > 128 or len(tool_name) > 128:
            return False, "[mcp_call] server_name/tool_name 过长"
        if not re.match(r"^[A-Za-z0-9_\-\.]{1,128}$", server) or \
           not re.match(r"^[A-Za-z0-9_\-\.]{1,128}$", tool_name):
            return False, "[mcp_call] server_name/tool_name 只允许字母/数字/下划线/点/连字符"
        if self._exec is None:
            return False, "[mcp_call] 执行器未初始化"
        mgr = getattr(self._exec, "_mcp_manager", None)
        if mgr is not None:
            try:
                full_name = server + "_" + tool_name
                result = mgr.call_tool(full_name, args)
                if isinstance(result, dict):
                    ok = result.get("ok", False)
                    content = result.get("content", "")
                    if ok:
                        content_str = str(content) if content is not None else ""
                        # 截断输出，防止子工具返回过大内容
                        return True, "[mcp_call] " + server + "/" + tool_name + "\n" + content_str[:4000]
                    return False, "[mcp_call] 失败: " + str(result.get("error", "") or "")
                return False, "[mcp_call] 返回格式异常"
            except Exception as e:
                return False, "[mcp_call] " + str(e)[:512]
        return True, "[mcp_call] " + server + "/" + tool_name + " (MCP 未配置)"

    # --------------------- MCP 管理工具（模型自管理 MCP） ---------------------
    def _mcp_manager(self):
        if self._exec is None:
            return None
        return getattr(self._exec, "_mcp_manager", None)

    def mcp_list(self, call=None):
        """列出所有 MCP 服务器配置与运行状态。"""
        mgr = self._mcp_manager()
        if mgr is None:
            return False, "[mcp_list] MCP 管理器未初始化"
        try:
            servers = mgr.list_servers()
            if not servers:
                return True, "[mcp_list] 当前没有配置任何 MCP 服务器"
            lines = ["[mcp_list] 已配置服务器:"]
            for s in servers:
                lines.append(
                    f"  - {s.get('name')} ({s.get('display_name')})\n"
                    f"    enabled={s.get('enabled')} running={s.get('running')} "
                    f"tools={s.get('tool_count', 0)}\n"
                    f"    description: {s.get('description', '')}\n"
                    f"    install_hint: {s.get('install_hint', '')}"
                )
            return True, "\n".join(lines)
        except Exception as e:
            return False, "[mcp_list] 失败: " + str(e)[:512]

    # 允许启动 MCP server 的可信命令白名单
    _MCP_CMD_ALLOWLIST = frozenset({"npx", "uvx", "python3", "python", "node"})
    _MCP_CMD_SHELL_METACHAR = frozenset({";", "&", "|", "$", "`", "\\", "\n", "\r", "<", ">", "*", "?", "{", "}"})
    _MCP_PKG_RE = re.compile(r"^[a-zA-Z0-9_.~/@-]+$")

    def _mcp_validate_command(self, command: str, args: List[str]) -> Optional[str]:
        """校验 MCP 启动命令与参数，防止任意命令执行。"""
        cmd = command.strip().lower()
        # 仅允许白名单中的命令
        if cmd not in self._MCP_CMD_ALLOWLIST:
            return f"[mcp_add] command '{command}' 不在允许列表 {sorted(self._MCP_CMD_ALLOWLIST)} 中"
        # args 中禁止 shell 元字符
        for a in args:
            s = str(a)
            if any(ch in s for ch in self._MCP_CMD_SHELL_METACHAR):
                return f"[mcp_add] args 包含非法字符: {s[:80]}"
        return None

    def _mcp_validate_package(self, command: str, args: List[str]) -> Optional[str]:
        """校验包名/模块参数格式，防止任意脚本/路径执行。"""
        cmd = command.strip().lower()
        # 对 npx/uvx，包名参数需符合 npm 包名基本字符集
        if cmd in ("npx", "uvx"):
            for a in args:
                s = str(a)
                # 跳过 flag 参数，只校验看起来像包名的位置参数
                if s.startswith("-"):
                    continue
                if not self._MCP_PKG_RE.match(s):
                    return f"[mcp_add] 包名/参数含非法字符: {s[:80]}"
        # 对 python，禁止直接执行文件路径，只允许模块名
        if cmd in ("python", "python3"):
            for a in args:
                s = str(a)
                if s.startswith("-"):
                    continue
                if "/" in s or "\\" in s or s.endswith(".py"):
                    return f"[mcp_add] python 启动参数只允许模块名，禁止文件路径: {s[:80]}"
        return None

    def mcp_add(self, call):
        """添加或更新一个 MCP 服务器配置。

        变更 MCP 配置属于敏感操作，需要外部审批回调显式批准。
        """
        mgr = self._mcp_manager()
        if mgr is None:
            return False, "[mcp_add] MCP 管理器未初始化"
        # 审批门：缺失/异常默认拒绝
        confirm_fn = getattr(self._exec, "on_confirm_command", None)
        if not callable(confirm_fn):
            return False, "[mcp_add] 缺少审批回调，已拒绝保存 MCP 配置"
        try:
            confirm_call = dict(call)
            confirm_call["command"] = f"mcp_add: {call.get('name')} -> {call.get('command')} {' '.join(str(a) for a in (call.get('args') or []))}"[:200]
            if not bool(confirm_fn(confirm_call)):
                return False, "[mcp_add] 用户拒绝保存 MCP 配置"
        except ConfirmationError:
            logger.info("[mcp_add] MCP 配置确认回调异常，已拒绝执行")
            logger.debug("[mcp_add] MCP 配置确认回调异常详情", exc_info=True)
            return False, "[mcp_add] 确认失败，操作已取消"
        except Exception:
            logger.info("[mcp_add] MCP 配置确认回调出现未知异常，已拒绝执行")
            logger.debug("[mcp_add] MCP 配置确认回调异常详情", exc_info=True)
            return False, "[mcp_add] 确认失败，操作已取消"
        try:
            from src.mcp.manager import MCPServerConfig
            name = str(call.get("name", "")).strip()
            if not name:
                return False, "[mcp_add] 缺少 name"
            if not re.match(r"^[A-Za-z0-9_\-]{1,64}$", name):
                return False, "[mcp_add] name 只允许字母/数字/下划线/连字符"
            command = str(call.get("command", "npx"))
            args = [str(a) for a in (call.get("args") or [])]
            err = self._mcp_validate_command(command, args)
            if err:
                return False, err
            err = self._mcp_validate_package(command, args)
            if err:
                return False, err
            cfg = MCPServerConfig(
                name=name,
                display_name=str(call.get("display_name", name)),
                description=str(call.get("description", ""))[:512],
                command=command,
                args=args,
                env={str(k): str(v) for k, v in (call.get("env") or {}).items()},
                working_dir=call.get("working_dir") or None,
                enabled=bool(call.get("enabled", False)),
                install_hint=str(call.get("install_hint", ""))[:512],
            )
            ok = mgr.add_server(cfg)
            return (True, f"[mcp_add] 已保存配置: {name}") if ok else (False, "[mcp_add] 保存配置失败")
        except Exception as e:
            return False, "[mcp_add] 失败: " + str(e)[:512]

    def mcp_remove(self, call):
        """移除一个 MCP 服务器配置。"""
        mgr = self._mcp_manager()
        if mgr is None:
            return False, "[mcp_remove] MCP 管理器未初始化"
        name = str(call.get("name", "")).strip()
        if not name:
            return False, "[mcp_remove] 缺少 name"
        try:
            ok = mgr.remove_server(name)
            return (True, f"[mcp_remove] 已移除: {name}") if ok else (False, f"[mcp_remove] 未找到: {name}")
        except Exception as e:
            return False, "[mcp_remove] 失败: " + str(e)[:512]

    def mcp_enable(self, call):
        """启用或禁用 MCP 服务器（保存配置但不自动启动）。"""
        mgr = self._mcp_manager()
        if mgr is None:
            return False, "[mcp_enable] MCP 管理器未初始化"
        name = str(call.get("name", "")).strip()
        if not name:
            return False, "[mcp_enable] 缺少 name"
        enabled = bool(call.get("enabled", True))
        try:
            ok = mgr.update_server(name, enabled=enabled)
            action = "启用" if enabled else "禁用"
            return (True, f"[mcp_enable] 已{action}: {name}") if ok else (False, f"[mcp_enable] 未找到: {name}")
        except Exception as e:
            return False, "[mcp_enable] 失败: " + str(e)[:512]

    def mcp_start(self, call):
        """启动一个 MCP 服务器。"""
        # 子代理/受限上下文禁止启动外部 MCP 服务器
        subagent_restrictions = getattr(self._exec, "_subagent_restrictions", {}) or {}
        if subagent_restrictions.get("forbid_commands", False) or getattr(self._exec, "_forbid_commands", False):
            return False, "[mcp_start] 已禁用 (子代理不可启动 MCP 服务器)"
        mgr = self._mcp_manager()
        if mgr is None:
            return False, "[mcp_start] MCP 管理器未初始化"
        name = str(call.get("name", "")).strip()
        if not name:
            return False, "[mcp_start] 缺少 name"
        try:
            result = mgr.start_server(name)
            if result.get("ok"):
                return True, f"[mcp_start] {name} 已启动，工具数: {result.get('tools', 0)}"
            return False, f"[mcp_start] {name} 启动失败: {result.get('message', '')}"
        except Exception as e:
            return False, "[mcp_start] 失败: " + str(e)[:512]

    def mcp_stop(self, call):
        """停止一个 MCP 服务器。"""
        mgr = self._mcp_manager()
        if mgr is None:
            return False, "[mcp_stop] MCP 管理器未初始化"
        name = str(call.get("name", "")).strip()
        if not name:
            return False, "[mcp_stop] 缺少 name"
        try:
            result = mgr.stop_server(name)
            if result.get("ok"):
                return True, f"[mcp_stop] {name} 已停止"
            return False, f"[mcp_stop] {name} 停止失败: {result.get('message', '')}"
        except Exception as e:
            return False, "[mcp_stop] 失败: " + str(e)[:512]

    # --------------------- docs / package 查询 ---------------------
