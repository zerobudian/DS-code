# -*- coding: utf-8 -*-
"""网络搜索 & WebFetch
- 必应搜索 (Bing Search) 前五条
- webfetch 抓取网页内容
"""
import ipaddress
import logging
import re
from typing import List, Dict, Optional, Tuple
from urllib.parse import quote, urlparse, urljoin

from src.core import dns_pinning

# 可选依赖延迟导入：requests / bs4 未安装时优雅降级
try:
    import requests as _requests
except ImportError:
    _requests = None  # type: ignore

# 公开别名：供测试与外部代码以 `web_search.requests` 形式访问已加载的 requests 库。
requests = _requests

try:
    from bs4 import BeautifulSoup as _BeautifulSoup
except ImportError:
    _BeautifulSoup = None  # type: ignore

# 公开别名：供测试与外部代码以 `web_search.BeautifulSoup` 形式访问。
BeautifulSoup = _BeautifulSoup


logger = logging.getLogger(__name__)


def _ensure_requests():
    """延迟检查 requests 是否可用，返回模块或 None"""
    if _requests is None:
        logger.warning("requests 未安装，网络搜索功能不可用")
    return _requests


def _ensure_bs4():
    """延迟检查 bs4 是否可用，返回 BeautifulSoup 类或 None"""
    if _BeautifulSoup is None:
        logger.warning("beautifulsoup4 未安装，网页解析功能不可用")
    return _BeautifulSoup


def _is_safe_url(url: str) -> bool:
    """检查 URL 是否安全（防 SSRF：禁止内网/私有地址、非 HTTP scheme）。"""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False
    hostname = (parsed.hostname or "").lower().strip("[]")
    if not hostname:
        return False
    # 禁止明显的内网域名模式
    blocked_suffixes = (".local", ".internal", "localhost", "loopback")
    for suffix in blocked_suffixes:
        if hostname == suffix or hostname.endswith(suffix):
            return False
    # IP 检查
    try:
        ip = ipaddress.ip_address(hostname)
        if not dns_pinning.check_ip_safe(str(ip)):
            return False
    except ValueError:
        # 域名 — 做 DNS 解析检查（C-006: 带超时，避免恶意 DNS 永久阻塞）
        safe_ip = dns_pinning.get_dns_pin_ip(hostname)
        if not safe_ip:
            return False
    return True


def _resolve_public_ips(hostname: str) -> List[str]:
    """解析 hostname，返回所有公网 IP；任一地址为内网/非法则返回空列表。"""
    return dns_pinning.resolve_all_public_ips(hostname)


def _request_with_pin(method: str, url: str,
                      timeout: Tuple[float, float] = (10, 30),
                      **kwargs):
    """对 URL 做一次 DNS-pinning 请求，防止 DNS rebinding SSRF。

    实现：先解析 hostname 到公网 IP，然后在临时上下文内激活 DNS pinning，
    使 requests 在建立 TCP 连接时强制使用已校验的 IP。HTTPS 的 SNI 与证书验证
    因此得以保留。

    MEDIUM-004: 默认 timeout=(10, 30)，避免无默认超时时网络请求永久阻塞。
    """
    req = _ensure_requests()
    if req is None:
        raise RuntimeError("requests 未安装，无法发起网络请求")
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().strip("[]")
    if not hostname:
        raise ValueError("URL 缺少 hostname")
    safe_ips = _resolve_public_ips(hostname)
    if not safe_ips:
        raise ValueError("SSRF: hostname 无可用公网 IP")
    dns_pinning.ensure_dns_patch()
    dns_pinning.set_dns_pin(hostname, safe_ips[0])
    try:
        # 明确启用 SSL 证书验证，防止中间人攻击。
        kwargs.setdefault("verify", True)
        return req.request(method, url, timeout=timeout, **kwargs)
    finally:
        dns_pinning.clear_dns_pin()


def _fetch_with_redirects(method: str, url: str, max_redirects: int = 5,
                          max_bytes: int = 5 * 1024 * 1024,
                          headers: Optional[Dict[str, str]] = None,
                          timeout: Tuple[float, float] = (10, 30)) -> Tuple[int, str]:
    """DNS-pinning 抓取，手动处理重定向并逐跳校验 SSRF。返回 (status, text)。"""
    current_url = url
    redirect_count = 0
    req_headers = dict(headers) if headers else {}
    while redirect_count <= max_redirects:
        if not _is_safe_url(current_url):
            return 0, f"[SSRF] 拒绝访问内网或非 HTTP 地址: {current_url[:200]}"
        resp = None
        try:
            resp = _request_with_pin(
                method, current_url,
                headers=req_headers,
                timeout=timeout,
                stream=True,
                allow_redirects=False,
            )
            if 300 <= resp.status_code < 400:
                loc = resp.headers.get("Location") or resp.headers.get("location")
                resp.close()
                if not loc:
                    return 0, "[HTTP] 重定向缺少 Location"
                current_url = urljoin(current_url, loc)
                redirect_count += 1
                continue
            # 按 Content-Length 或分块读取，上限 max_bytes
            try:
                cl = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                cl = 0
            if cl > max_bytes:
                resp.close()
                return 0, f"[HTTP] 响应体过大 ({cl} bytes > {max_bytes})"
            chunks: List[str] = []
            total = 0
            try:
                for chunk in resp.iter_content(chunk_size=8192, decode_unicode=True):
                    if not chunk:
                        continue
                    total += len(chunk) if isinstance(chunk, str) else len(chunk)
                    if total > max_bytes:
                        return 0, f"[HTTP] 响应体超过 {max_bytes} bytes 上限"
                    chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace"))
            finally:
                resp.close()
            return resp.status_code, "".join(chunks)
        except Exception as e:
            if resp is not None:
                try:
                    resp.close()
                except Exception as _exc:
                    logger.debug("忽略可恢复异常: %s", _exc, exc_info=True)
            return 0, f"[HTTP] 请求失败: {e}"
    return 0, f"[HTTP] 重定向次数超过上限 {max_redirects}"


class WebSearch:
    """必应搜索"""

    USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

    @classmethod
    def search(cls, query: str, max_results: int = 5,
               *, timeout: Tuple[float, float] = (10, 30),
               max_redirects: int = 5,
               max_bytes: int = 1 * 1024 * 1024) -> List[Dict]:
        """必应搜索, 返回 [{"title":..., "snippet":..., "url":...}]"""
        results = []
        try:
            url = f"https://www.bing.com/search?q={quote(query)}&count={max_results + 5}"
            status, html_text = _fetch_with_redirects(
                "GET", url,
                headers={
                    "User-Agent": cls.USER_AGENT,
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
                timeout=timeout,
                max_redirects=max_redirects,
                max_bytes=max_bytes,
            )
            if status != 200:
                raise RuntimeError(f"Bing 返回 {status}")
            bs = _ensure_bs4()
            if bs is None:
                raise RuntimeError("beautifulsoup4 未安装，无法解析搜索结果")
            soup = bs(html_text, "html.parser")
            # bing 搜索结果: li.b_algo / h2 > a
            items = soup.select("li.b_algo")
            for item in items:
                if len(results) >= max_results:
                    break
                a = item.select_one("h2 a")
                title = a.get_text(strip=True) if a else ""
                link = a.get("href", "") if a else ""
                snippet_elem = item.select_one("p, .b_caption p, .snippet")
                snippet = snippet_elem.get_text(" ", strip=True) if snippet_elem else item.get_text(" ", strip=True)
                snippet = snippet[:300]
                if title and link:
                    results.append({"title": title, "url": link, "snippet": snippet})
        except Exception as e:
            results = [{"title": "搜索失败", "url": "", "snippet": f"错误: {str(e)}"}]
        return results


class WebFetch:
    """抓取页面纯文本"""

    @classmethod
    def fetch(cls, url: str, max_chars: int = 5000,
              *, timeout: Tuple[float, float] = (10, 30),
              max_redirects: int = 5,
              max_bytes: int = 5 * 1024 * 1024) -> str:
        # SSRF 防护：检查 URL 是否指向安全的公网地址
        if not _is_safe_url(url):
            return f"[WebFetch] 拒绝访问内网或非 HTTP 地址: {url[:200]}"
        status, html_text = _fetch_with_redirects(
            "GET", url,
            headers={
                "User-Agent": WebSearch.USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            timeout=timeout,
            max_redirects=max_redirects,
            max_bytes=max_bytes,
        )
        if status == 0:
            return html_text
        if status != 200:
            return f"[WebFetch] HTTP {status}"
        try:
            bs = _ensure_bs4()
            if bs is None:
                return "[WebFetch] beautifulsoup4 未安装，无法解析网页"
            soup = bs(html_text, "html.parser")
            # 去除脚本/样式
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            text = soup.get_text("\n", strip=True)
            # 压缩多余空行
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text[:max_chars]
        except Exception as e:
            return f"[WebFetch] 解析失败: {e}"
