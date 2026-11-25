"""tests/unit/test_url_guard_ssrf.py — SSRF 防护测试

Phase 1.3 修复: install_workflow_from_url / install_skill_from_url 直接
aiohttp.session.get(url) 未校验 host 白名单。攻击者可构造
http://169.254.169.254/ 让 Agent 访问云元数据服务。

修复: 创建 security/url_guard.py 提供 validate_external_url()——
scheme 白名单 + hostname 白名单 + DNS 解析 + 私有 IP 拒绝。

本测试验证:
1. 白名单 host（github.com 等）直接放行
2. 非 HTTPS 被 reject（默认强制 https）
3. 未知 scheme（ftp/file）被 reject
4. 空 URL / 无 hostname 被 reject
5. 云元数据 IP 169.254.169.254 被 reject（mock DNS）
6. localhost / 127.0.0.1 被 reject（mock DNS）
7. DNS 解析失败被 reject
8. is_skill_url_allowed() 布尔包装器正确
"""
import os
import sys
import socket
from unittest.mock import patch, MagicMock

_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from security.url_guard import (
    validate_external_url,
    is_skill_url_allowed,
    ALLOWED_HOSTS,
)


# ============================================================
# 1. 白名单 host 放行
# ============================================================

def test_whitelisted_hosts_pass():
    """白名单 host + HTTPS 直接放行（不做 DNS 解析）。"""
    for host in ("github.com", "raw.githubusercontent.com", "gitlab.com", "gitee.com"):
        url = f"https://{host}/some/path/file.yaml"
        result = validate_external_url(url)
        assert result == url


def test_allow_insecure_flag_allows_http_for_whitelisted():
    """allow_insecure=True 允许 HTTP（dev 场景）。"""
    url = "http://github.com/file.yaml"
    result = validate_external_url(url, allow_insecure=True)
    assert result == url


# ============================================================
# 2. scheme 校验
# ============================================================

def test_non_https_rejected_by_default():
    """默认强制 HTTPS——HTTP 被 reject。"""
    import pytest
    with pytest.raises(ValueError, match="[Ii]nsecure HTTP"):
        validate_external_url("http://github.com/file.yaml")


def test_unknown_scheme_rejected():
    """未知 scheme（ftp/file/ws）被 reject。"""
    import pytest
    for scheme in ("ftp://", "file:///", "ws://", "javascript:alert(1)"):
        with pytest.raises(ValueError, match="[Uu]nsupported scheme"):
            validate_external_url(scheme + "example.com/file")


def test_empty_url_rejected():
    """空 URL / None 被 reject。"""
    import pytest
    with pytest.raises(ValueError, match="non-empty"):
        validate_external_url("")
    with pytest.raises(ValueError, match="non-empty"):
        validate_external_url(None)


def test_no_hostname_rejected():
    """无 hostname 的 URL 被 reject。"""
    import pytest
    with pytest.raises(ValueError, match="no hostname"):
        validate_external_url("https:///just-a-path")


# ============================================================
# 3. SSRF 防护——内网 IP / 云元数据 / DNS rebinding
# ============================================================

def _mock_dns_resolve(ip: str):
    """构造一个 mock，让 socket.getaddrinfo 返回指定 IP。"""
    # getaddrinfo 返回 list of (family, type, proto, canonname, sockaddr)
    # sockaddr for IPv4 = (ip, port)
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


def test_cloud_metadata_ip_rejected():
    """169.254.169.254（AWS/云元数据端点）被 reject。

    即使 host 是公网域名，DNS 解析到 169.254.x.x 也必须拒绝（防 DNS rebinding）。
    """
    import pytest
    import socket
    with patch("security.url_guard.socket.getaddrinfo",
               return_value=_mock_dns_resolve("169.254.169.254")):
        with pytest.raises(ValueError, match="non-public IP|link-local"):
            validate_external_url("https://evil-metadata.example.com/")


def test_loopback_ip_rejected():
    """127.0.0.1 被 reject（防内网探测）。"""
    import pytest
    import socket
    with patch("security.url_guard.socket.getaddrinfo",
               return_value=_mock_dns_resolve("127.0.0.1")):
        with pytest.raises(ValueError, match="non-public IP|loopback"):
            validate_external_url("https://internal.example.com/")


def test_private_ip_rfc1918_rejected():
    """RFC 1918 私有 IP（10.x / 172.16.x / 192.168.x）被 reject。"""
    import pytest
    import socket
    for private_ip in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
        with patch("security.url_guard.socket.getaddrinfo",
                   return_value=_mock_dns_resolve(private_ip)):
            with pytest.raises(ValueError, match="non-public IP|private"):
                validate_external_url(f"https://internal-{private_ip}.example.com/")


def test_dns_resolution_failure_rejected():
    """DNS 解析失败被 reject。"""
    import pytest
    import socket
    with patch("security.url_guard.socket.getaddrinfo",
               side_effect=socket.gaierror("DNS lookup failed")):
        with pytest.raises(ValueError, match="DNS resolution failed"):
            validate_external_url("https://nonexistent-domain-xyz.example.com/")


def test_public_non_whitelisted_host_allowed():
    """公网非白名单 host（DNS 解析到公网 IP）被放行。"""
    import socket
    with patch("security.url_guard.socket.getaddrinfo",
               return_value=_mock_dns_resolve("93.184.216.34")):  # example.com 公网 IP
        result = validate_external_url("https://example.com/some-file.yaml")
        assert result == "https://example.com/some-file.yaml"


# ============================================================
# 4. is_skill_url_allowed 布尔包装器
# ============================================================

def test_is_skill_url_allowed_returns_true_for_whitelisted():
    """白名单 host 返回 True。"""
    assert is_skill_url_allowed("https://github.com/file.yaml") is True


def test_is_skill_url_allowed_returns_false_for_rejected():
    """被拒绝的 URL 返回 False（不抛异常）。"""
    assert is_skill_url_allowed("ftp://example.com/") is False
    assert is_skill_url_allowed("") is False
    assert is_skill_url_allowed("javascript:alert(1)") is False


# ============================================================
# 5. ALLOWED_HOSTS 白名单内容
# ============================================================

def test_allowed_hosts_contains_github():
    """ALLOWED_HOSTS 包含关键公网源。"""
    assert "github.com" in ALLOWED_HOSTS
    assert "raw.githubusercontent.com" in ALLOWED_HOSTS
    assert "gitlab.com" in ALLOWED_HOSTS


def test_allowed_hosts_is_frozenset():
    """ALLOWED_HOSTS 是 frozenset（防运行时篡改）。"""
    assert isinstance(ALLOWED_HOSTS, frozenset)
