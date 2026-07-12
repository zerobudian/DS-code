# -*- coding: utf-8 -*-
"""统一的 DNS Pinning 模块，防止 DNS rebinding SSRF 攻击。

设计原则：
1. 全局只安装一次 socket.getaddrinfo 补丁，避免多个模块各自打补丁导致互相覆盖
2. 使用线程局部存储 (threading.local) 保存 pin，多线程互不干扰
3. 提供 set_pin / clear_pin / get_safe_ip 等公共 API
4. 无 pin 时完全透传原始 getaddrinfo，不影响正常 DNS 解析
"""
import ipaddress
import socket
import threading
from typing import Optional, List, Tuple

from src.core.dns_timeout import getaddrinfo_with_timeout

# 线程局部存储：保存当前线程的 DNS pin
_ssrf_local = threading.local()

# 原始 getaddrinfo（在模块加载时捕获，此时尚未被任何补丁修改）
_orig_getaddrinfo = socket.getaddrinfo

_patch_lock = threading.Lock()
_patched = False


def _patched_getaddrinfo(host, port, *args, **kwargs):
    """线程局部 DNS pinning：仅当本线程设置了 pin 且 host 匹配时才替换为 pinned IP。"""
    pin = getattr(_ssrf_local, "pin", None)
    if pin and isinstance(host, str) and host.lower().strip("[]") == pin["host"].lower():
        try:
            return _orig_getaddrinfo(pin["ip"], port, *args, **kwargs)
        except Exception:
            # 回退时根据 IP 类型选择正确的地址族，并确保 port 为整型
            ip = pin["ip"]
            try:
                _port = int(port)
            except (TypeError, ValueError):
                _port = 0
            try:
                # 检测 IPv6 还是 IPv4
                import ipaddress as _ipa
                addr = _ipa.ip_address(ip)
                if addr.version == 6:
                    return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, _port, 0, 0))]
                else:
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, _port))]
            except Exception:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, _port))]
    return _orig_getaddrinfo(host, port, *args, **kwargs)


def ensure_dns_patch() -> None:
    """懒加载安装 DNS pinning 补丁：只在首次调用时替换一次 socket.getaddrinfo。

    线程安全：双重检查锁定确保只打一次补丁。
    """
    global _patched
    if _patched:
        return
    with _patch_lock:
        if _patched:
            return
        socket.getaddrinfo = _patched_getaddrinfo
        _patched = True


def set_dns_pin(hostname: str, ip: str) -> None:
    """在当前线程设置 DNS pin，后续对 hostname 的解析将强制返回 ip。"""
    _ssrf_local.pin = {"host": hostname, "ip": ip}


def clear_dns_pin() -> None:
    """清除当前线程的 DNS pin，恢复正常 DNS 解析。"""
    _ssrf_local.pin = None


def get_dns_pin_ip(
    hostname: str,
    allow_private: bool = False,
    blocked_networks: Optional[List[ipaddress._BaseNetwork]] = None,
    timeout: float = 3.0,
) -> Optional[str]:
    """解析 hostname 并返回第一个安全的（非私有/非阻断）IP 地址。

    Args:
        hostname: 待解析主机名。
        allow_private: 是否允许私有地址（默认为 False）。
        blocked_networks: 额外阻断的 IP 网络列表。
        timeout: DNS 解析超时秒数。

    Returns:
        第一个安全 IP 的字符串表示，或 None（无安全 IP）。
    """
    try:
        infos = getaddrinfo_with_timeout(
            hostname, None, timeout=timeout,
            getaddrinfo_fn=_orig_getaddrinfo,
        )
    except Exception:
        return None

    for fam, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        # 检查是否为阻断的私有/保留地址
        if not allow_private:
            if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
                    or ip_obj.is_multicast or ip_obj.is_reserved or ip_obj.is_unspecified):
                continue
            # CGNAT (RFC 6598)
            if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in ipaddress.ip_network("100.64.0.0/10"):
                continue
            # IPv6 ULA (fc00::/7)
            if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj in ipaddress.ip_network("fc00::/7"):
                continue

        # 检查自定义阻断网络
        if blocked_networks:
            if any(ip_obj in net for net in blocked_networks):
                continue

        return ip_str

    return None


def resolve_all_public_ips(hostname: str, timeout: float = 3.0) -> List[str]:
    """解析 hostname，返回所有公网 IP；任一地址为内网/非法则返回空列表。

    用于需要验证所有解析结果都是公网 IP 的场景。
    """
    try:
        infos = getaddrinfo_with_timeout(
            hostname, None, timeout=timeout,
            family=0, type=socket.SOCK_STREAM,
            getaddrinfo_fn=_orig_getaddrinfo,
        )
    except (socket.gaierror, socket.herror, OSError, socket.timeout):
        return []

    _CGNAT = ipaddress.ip_network("100.64.0.0/10")
    _ULA = ipaddress.ip_network("fc00::/7")
    ips: List[str] = []
    for fam, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
                or ip_obj.is_multicast or ip_obj.is_reserved or ip_obj.is_unspecified):
            return []
        if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _CGNAT:
            return []
        if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj in _ULA:
            return []
        ips.append(ip_str)
    return ips


def check_ip_safe(ip_str: str, allow_private: bool = False) -> bool:
    """检查 IP 地址是否安全（非私有/非保留/非阻断）。"""
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if allow_private:
        return True
    if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
            or ip_obj.is_multicast or ip_obj.is_reserved or ip_obj.is_unspecified):
        return False
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in ipaddress.ip_network("100.64.0.0/10"):
        return False
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj in ipaddress.ip_network("fc00::/7"):
        return False
    return True
