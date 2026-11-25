"""tests/unit/test_hermes_ha_native_tools.py — HermesAdapter 与 hermes-agent 真实 API 对齐

2026-10-18 重写。旧测试断言的是 **虚构的 API**：
  · POST /tools/execute（hermes-agent 0.16 无此端点 → 稳定 404）
  · GET  /tools/schema （同上）
  · 默认端口 8015（真实 api_server 默认 8642）

本文件改用 hermes-agent 的真实契约（gateway/platforms/api_server.py）：
  · GET /v1/toolsets   — 工具集（含展开后的工具名）
  · GET /v1/skills     — 已安装技能
  · GET /v1/capabilities — 能力清单
  · GET /v1/models     — 模型名
  · Authorization: Bearer <API_SERVER_KEY>（必填）
  · X-Hermes-Session-Id / X-Hermes-Session-Key（会话连续性）

还验证「工具由 Hermes 服务端执行」这一事实：execute_tool 必须返回**明确的
unsupported**，而不是去请求一个不存在的端点。
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class _MockResponse:
    def __init__(self, status: int = 200, json_data=None, text: str = "", headers=None):
        self.status = status
        self._json_data = json_data
        self._text = text
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text


class _MockSession:
    def __init__(self, status: int = 200, json_data=None, exc: Exception | None = None,
                 headers=None, text: str = ""):
        self._status = status
        self._json_data = json_data
        self._exc = exc
        self._headers = headers or {}
        self._text = text
        self.closed = False
        self.requests: list[tuple[str, str, dict]] = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.requests.append((method, url, headers or {}))
        if self._exc:
            raise self._exc
        return _MockResponse(self._status, self._json_data, text=self._text, headers=self._headers)

    def get(self, url, timeout=None, headers=None):
        return self.request("GET", url, headers=headers)

    def post(self, url, json=None, headers=None, timeout=None):
        return self.request("POST", url, json=json, headers=headers)

    async def close(self):
        self.closed = True


def _make_adapter(reachable: bool = True, session=None):
    from adapters.hermes import HermesAdapter
    a = HermesAdapter()
    a._gateway_reachable = AsyncMock(return_value=reachable)
    if session is not None:
        a._session = session
    return a


# ── 端口契约 ──────────────────────────────────────────────────

def test_default_port_matches_hermes_api_server():
    """hermes-agent 的 api_server DEFAULT_PORT = 8642（不是 8015）。"""
    from adapters.hermes import HERMES_DEFAULT_PORT, HermesAdapter
    assert HERMES_DEFAULT_PORT == 8642
    assert HermesAdapter.default_port == 8642


# ── 鉴权 / 会话头 ─────────────────────────────────────────────

def test_auth_header_from_env(monkeypatch):
    monkeypatch.setenv("HERMES_API_KEY", "secret-123")
    a = _make_adapter()
    headers = asyncio.run(a._extra_headers({}))
    assert headers["Authorization"] == "Bearer secret-123"


def test_api_server_key_env_is_honored(monkeypatch):
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    monkeypatch.setenv("API_SERVER_KEY", "server-key")
    a = _make_adapter()
    headers = asyncio.run(a._extra_headers({}))
    assert headers["Authorization"] == "Bearer server-key"


def test_session_continuity_headers():
    a = _make_adapter()
    headers = asyncio.run(a._extra_headers({"session_id": "sess-abc"}))
    assert headers["X-Hermes-Session-Id"] == "sess-abc"
    assert headers["X-Hermes-Session-Key"] == "yuncat:sess-abc"


# ── 工具发现：/v1/toolsets ────────────────────────────────────

def test_list_available_tools_flattens_enabled_toolsets():
    payload = {
        "object": "list",
        "platform": "api_server",
        "data": [
            {"name": "web", "label": "Web", "description": "web tools",
             "enabled": True, "configured": True, "tools": ["web_search", "web_fetch"]},
            {"name": "off", "label": "Off", "description": "",
             "enabled": False, "configured": True, "tools": ["should_not_appear"]},
        ],
    }
    a = _make_adapter(session=_MockSession(json_data=payload))
    tools = asyncio.run(a.list_available_tools())
    names = [t["name"] for t in tools]
    assert names == ["web_search", "web_fetch"]
    assert tools[0]["category"] == "Web"
    assert "should_not_appear" not in names


def test_list_available_tools_unreachable_returns_empty():
    a = _make_adapter(reachable=False)
    assert asyncio.run(a.list_available_tools()) == []


def test_list_available_tools_error_returns_empty():
    a = _make_adapter(session=_MockSession(status=500, text="boom"))
    assert asyncio.run(a.list_available_tools()) == []


def test_list_skills():
    payload = {"object": "list", "data": [{"name": "pdf", "description": "d"}]}
    a = _make_adapter(session=_MockSession(json_data=payload))
    assert asyncio.run(a.list_skills()) == [{"name": "pdf", "description": "d"}]


# ── 能力 / 模型 ──────────────────────────────────────────────

def test_capabilities_info_cached():
    payload = {"object": "hermes.api_server.capabilities", "features": {"run_stop": True}}
    sess = _MockSession(json_data=payload)
    a = _make_adapter(session=sess)
    first = asyncio.run(a.capabilities_info())
    second = asyncio.run(a.capabilities_info())
    assert first["features"]["run_stop"] is True
    assert second is first
    assert len(sess.requests) == 1  # 第二次走缓存


def test_fallback_model_uses_v1_models():
    payload = {"object": "list", "data": [{"id": "hermes-agent"}]}
    a = _make_adapter(session=_MockSession(json_data=payload))
    assert asyncio.run(a._resolve_model({})) == "hermes-agent"


def test_explicit_model_wins_over_fallback():
    a = _make_adapter(session=_MockSession(json_data={"data": [{"id": "hermes-agent"}]}))
    model = asyncio.run(a._resolve_model({"model_config": {"model": "deepseek-v4-pro"}}))
    assert model == "deepseek-v4-pro"


# ── 工具执行：Hermes 在服务端执行，不提供外部单工具端点 ──────────

def test_execute_tool_is_explicitly_unsupported():
    a = _make_adapter(session=_MockSession())
    result = asyncio.run(a.execute_tool("web_search", {"q": "x"}))
    assert result["ok"] is False
    assert result["unsupported"] is True
    assert result["hint"] == "delegate_to_agent"
    assert "delegate_to_agent" in result["error"]


# ── health ──────────────────────────────────────────────────

def test_health_unreachable_false():
    a = _make_adapter(reachable=False)
    assert asyncio.run(a.health()) is False


def test_health_reachable_true():
    a = _make_adapter(reachable=True)
    assert asyncio.run(a.health()) is True


# ── capabilities() 如实反映服务端执行模型 ─────────────────────

def test_capabilities_flags():
    a = _make_adapter()
    caps = a.capabilities()
    assert caps.supports_stream is True
    assert caps.supports_tools is False   # 工具在 Hermes 服务端执行
    assert caps.supports_interrupt is True
