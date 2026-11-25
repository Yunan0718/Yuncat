"""tests/unit/test_ws_origin_validation.py — WS Origin 校验测试

Phase 2.2 修复: routes/ws.py 仅用 peer = ws.headers.get("origin", "?") 用于日志，
未做实际校验。恶意网页（如 evil.com）可建立 WS 连接尝试 token 暴力破解。

修复: 在 ws.accept() 之前校验 origin——
- 空 Origin：Electron 生产环境（file://），允许
- file:// 前缀：Electron 加载本地资源，允许
- http(s)://127.0.0.1 或 localhost 任意端口：开发服务器，允许
- 其他：拒绝（close code 1008）
- token 校验改用 hmac.compare_digest 防时序攻击

本测试验证:
1. _is_origin_allowed() 对各类 Origin 的判定
2. 静态检查: ws.py 使用 hmac.compare_digest 而非 == 比较 token
3. 静态检查: ws.py 在 accept() 前调用 _is_origin_allowed
4. 静态检查: 拒绝时 close code 为 1008
"""
import os
import sys
import re

_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from routes.ws import _is_origin_allowed, _ALLOWED_WS_ORIGIN_RE


# ============================================================
# 1. 允许的 Origin
# ============================================================

def test_empty_origin_allowed():
    """空 Origin = Electron 生产环境，允许。"""
    assert _is_origin_allowed("") is True


def test_file_protocol_allowed():
    """file:// 前缀 = Electron 加载本地资源，允许。"""
    assert _is_origin_allowed("file://") is True
    assert _is_origin_allowed("file:///C:/Users/test/index.html") is True


def test_localhost_http_allowed():
    """http://localhost 任意端口允许（开发服务器）。"""
    assert _is_origin_allowed("http://localhost:5173") is True
    assert _is_origin_allowed("http://localhost:3000") is True
    assert _is_origin_allowed("http://localhost") is True


def test_localhost_https_allowed():
    """https://localhost 允许。"""
    assert _is_origin_allowed("https://localhost:5173") is True
    assert _is_origin_allowed("https://localhost") is True


def test_loopback_ip_allowed():
    """http://127.0.0.1 任意端口允许。"""
    assert _is_origin_allowed("http://127.0.0.1:5173") is True
    assert _is_origin_allowed("http://127.0.0.1:8000") is True
    assert _is_origin_allowed("http://127.0.0.1") is True
    assert _is_origin_allowed("https://127.0.0.1:5173") is True


# ============================================================
# 2. 拒绝的 Origin
# ============================================================

def test_evil_com_rejected():
    """http://evil.com 被拒绝。"""
    assert _is_origin_allowed("http://evil.com") is False
    assert _is_origin_allowed("https://evil.com:443") is False


def test_non_localhost_subdomain_rejected():
    """localhost.evil.com 被拒绝（防子域名绕过）。"""
    assert _is_origin_allowed("http://localhost.evil.com") is False


def test_other_protocols_rejected():
    """ws:// / wss:// 协议被拒绝（只允许 http/https + file://）。"""
    assert _is_origin_allowed("ws://localhost:5173") is False
    assert _is_origin_allowed("wss://localhost:5173") is False


def test_null_origin_rejected():
    """'null' Origin（部分浏览器跨域）被拒绝。"""
    assert _is_origin_allowed("null") is False


# ============================================================
# 3. 正则模式校验
# ============================================================

def test_origin_regex_matches_localhost_with_port():
    """正则匹配 localhost + 端口。"""
    assert _ALLOWED_WS_ORIGIN_RE.match("http://localhost:5173")
    assert _ALLOWED_WS_ORIGIN_RE.match("https://127.0.0.1:8000")


def test_origin_regex_rejects_evil():
    """正则不匹配 evil.com。"""
    assert not _ALLOWED_WS_ORIGIN_RE.match("http://evil.com")
    assert not _ALLOWED_WS_ORIGIN_RE.match("https://attacker.example:443")


# ============================================================
# 4. 静态检查: ws.py 源码安全模式
# ============================================================

def _read_ws_source() -> str:
    """读取 ws.py 源码。"""
    path = os.path.join(_SRC, "routes", "ws.py")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_ws_uses_hmac_compare_digest():
    """静态检查: token 比较使用 hmac.compare_digest 而非 ==。

    == 比较字符串会在首个不匹配字符短路返回，攻击者可据此逐字符爆破 token。
    hmac.compare_digest 恒定时间比较，防时序攻击。
    """
    source = _read_ws_source()
    assert "hmac.compare_digest" in source, (
        "WS token comparison must use hmac.compare_digest (constant-time) "
        "to prevent timing attacks"
    )
    # 不应直接用 == 比较 token
    assert "msg_token == token" not in source, (
        "WS token must not use == comparison (timing attack risk)"
    )


def test_ws_checks_origin_before_accept():
    """静态检查: ws.accept() 前调用 _is_origin_allowed。

    顺序: 1) 读取 origin  2) 校验  3) reject 则 close  4) accept
    """
    source = _read_ws_source()
    # _is_origin_allowed 必须在 ws.accept() 之前调用
    origin_check_pos = source.find("_is_origin_allowed")
    accept_pos = source.find("ws.accept()")
    assert origin_check_pos != -1, "_is_origin_allowed must be called"
    assert accept_pos != -1, "ws.accept() must be called"
    assert origin_check_pos < accept_pos, (
        "Origin check must happen BEFORE ws.accept() "
        "(otherwise malicious connection is already established)"
    )


def test_ws_reject_uses_close_code_1008():
    """静态检查: Origin 拒绝时 close code 为 1008（Policy Violation）。"""
    source = _read_ws_source()
    # 1008 = Policy Violation（RFC 6455）
    assert "1008" in source, (
        "WS rejection must use close code 1008 (Policy Violation)"
    )


def test_ws_imports_hmac():
    """静态检查: ws.py 顶部 import hmac。"""
    source = _read_ws_source()
    assert "import hmac" in source, "ws.py must import hmac at module level"
