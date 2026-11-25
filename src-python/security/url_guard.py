"""security/url_guard.py —— URL 白名单守卫，防止 SSRF 攻击。

设计参考：
- OWASP SSRF Prevention Cheat Sheet
- Node.js `request` / Python `requests` 社区最佳实践

策略：
1. scheme 仅允许 http/https（默认强制 https）
2. hostname 白名单优先（已知安全的公网源）
3. 非白名单 host 解析 DNS，拒绝任何 private / loopback / link-local IP
   （防 169.254.169.254 云元数据 / 127.0.0.1 / RFC 1918 内网探测）
4. 返回规范化 URL；不通过抛 ValueError（fail-closed）

不引入新依赖——仅用标准库 ipaddress / socket / urllib。
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

_log = logging.getLogger("yuncat.url_guard")

# 公网白名单——与 system_tool_install._ALLOWED_SKILL_HOSTS 对齐 + 扩展
# 新增源需在此声明，避免任意 host 被动放行
ALLOWED_HOSTS: frozenset[str] = frozenset({
    # Yuncat 官方源
    "clawhub.ai",
    "agentskills.io",
    # GitHub（raw / archive / api / release asset）
    "github.com",
    "raw.githubusercontent.com",
    "codeload.github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    # GitLab / Gitee（开源镜像）
    "gitlab.com",
    "gitee.com",
})


def validate_external_url(url: str, *, allow_insecure: bool = False) -> str:
    """校验外部 URL 安全性。返回原 URL；不通过抛 ValueError。

    参数：
        url: 待校验的 URL 字符串
        allow_insecure: True 时允许 http://（仅 dev 场景）；默认 False 强制 https

    用法：
        try:
            safe_url = validate_external_url(user_url)
        except ValueError as e:
            return ToolResult(success=False, error=f"URL rejected: {e}").to_dict()
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL must be a non-empty string")

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("https", "http"):
        raise ValueError(f"Unsupported scheme: {scheme!r} (only http/https allowed)")

    if scheme == "http" and not allow_insecure:
        raise ValueError(
            "Insecure HTTP rejected (set allow_insecure=True for localhost/dev)"
        )

    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("URL has no hostname")

    # 1) 白名单优先——已知安全公网源直接放行
    if host in ALLOWED_HOSTS:
        return url

    # 2) 非白名单 host：解析 DNS 并检查 IP 是否落入内网段
    #    这一步防御 host 直接是 IP（如 http://169.254.169.254/）或
    #    DNS rebinding 攻击（公网域名解析到内网 IP）
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"DNS resolution failed for {host!r}: {e}")

    if not infos:
        raise ValueError(f"No DNS records for {host!r}")

    for info in infos:
        ip_str = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            # 异常 IP 字符串——fail-closed
            raise ValueError(f"Unparseable IP {ip_str!r} for host {host!r}")

        # 拒绝 private / loopback / link-local / reserved
        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_reserved
            or ip_obj.is_multicast
        ):
            raise ValueError(
                f"Host {host!r} resolves to non-public IP {ip_obj} "
                f"(private/loopback/link-local/reserved/multicast rejected)"
            )

    _log.debug("url_guard accepted non-whitelisted host=%s (DNS public)", host)
    return url


def is_skill_url_allowed(url: str) -> bool:
    """Backward-compatible boolean checker——替代原 _validate_skill_url。

    保留布尔返回以兼容现有调用方；新代码应直接用 validate_external_url。
    """
    try:
        validate_external_url(url)
        return True
    except ValueError:
        return False
