"""tests/unit/test_phase63_gateway.py —— OpenClaw 外部网关契约

Phase 融合重构后：OpenClaw 不再 vendored 子进程 / node-embed。OpenClawAdapter 现在
继承 HttpGatewayAgentAdapter，作为「外部 agent」连接用户本地启动的 OpenClaw Gateway
（默认 http://127.0.0.1:18789，可用 OPENCLAW_PORT 覆盖）。

原 Phase 63 的 circuit-breaker 探测逻辑（_should_try_gateway / _gateway_health_probe /
_gateway_failures / _gateway_cooldown_until）已被 HttpGatewayAgentAdapter._gateway_reachable()
取代。本测试验证新的外部网关契约：
- 类属性正确
- capabilities 正确
- _gateway_reachable() 行为（port=0 / gateway 不可达 / 可达）
- health() 行为（gateway 不可达 + CLI 不在 PATH → False）
- execute_tool / list_available_tools / refresh_skills 在 gateway 不可达时优雅降级
- gateway 可达时 execute_tool 真正发起 HTTP 请求
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from config import OC_PORT_HINT


async def _always_true(*a, **k):
    return True


class _MockResponse:
    def __init__(self, status: int = 200, json_data: dict | None = None):
        self.status = status
        self._json = json_data or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self._json

    async def text(self):
        return f"status={self.status}"


class _MockSession:
    """可控制 GET/POST 行为的 mock aiohttp session。"""
    closed = False  # _ensure_session 据此跳过重建

    def __init__(self, get_status: int = 200, post_status: int = 200,
                 post_json: dict | None = None, get_json: dict | None = None,
                 exc: Exception | None = None):
        self.get_status = get_status
        self.post_status = post_status
        self.post_json = post_json or {}
        self.get_json = get_json or {}
        self.exc = exc
        self.get_calls = 0
        self.post_calls = 0

    def get(self, url, timeout=None):
        self.get_calls += 1
        if self.exc:
            raise self.exc
        return _MockResponse(self.get_status, self.get_json)

    def post(self, url, json=None, timeout=None):
        self.post_calls += 1
        if self.exc:
            raise self.exc
        return _MockResponse(self.post_status, self.post_json)


def _make_oc(port: int = OC_PORT_HINT):
    """创建 OpenClawAdapter 实例并 mock _ensure_session（用可控 _MockSession）。"""
    from adapters.openclaw import OpenClawAdapter
    a = OpenClawAdapter()
    a._port = port
    return a


# ---- 类属性 ----

def test_cli_name():
    from adapters.openclaw import OpenClawAdapter
    assert OpenClawAdapter.cli_name == "openclaw"


def test_default_port():
    from adapters.openclaw import OpenClawAdapter
    assert OpenClawAdapter.default_port == OC_PORT_HINT


def test_port_env():
    from adapters.openclaw import OpenClawAdapter
    assert OpenClawAdapter.port_env == "OPENCLAW_PORT"


def test_health_path():
    from adapters.openclaw import OpenClawAdapter
    assert OpenClawAdapter.health_path == "/v1/status"


def test_capabilities():
    a = _make_oc()
    cap = a.capabilities()
    assert cap.supports_stream is True
    assert cap.supports_tools is True
    assert cap.supports_browser is True
    assert cap.supports_image is True


# ---- _gateway_reachable() 行为 ----

async def test_gateway_reachable_false_when_port_zero():
    a = _make_oc(port=0)
    assert await a._gateway_reachable() is False


async def test_gateway_reachable_false_when_down():
    a = _make_oc()
    a._session = _MockSession(get_status=503)
    assert await a._gateway_reachable() is False


async def test_gateway_reachable_true_when_200():
    a = _make_oc()
    a._session = _MockSession(get_status=200)
    assert await a._gateway_reachable() is True


async def test_gateway_reachable_false_on_network_error():
    import aiohttp
    a = _make_oc()
    a._session = _MockSession(exc=aiohttp.ClientError("conn refused"))
    assert await a._gateway_reachable() is False


# ---- health() 行为 ----

async def test_health_false_when_gateway_down_and_no_cli(monkeypatch):
    a = _make_oc()
    a._session = _MockSession(get_status=503)
    # 模拟 openclaw CLI 不在 PATH
    monkeypatch.setattr("adapters.ext_http.shutil.which", lambda name: None)
    assert await a.health() is False


# ---- 优雅降级：gateway 不可达 ----

async def test_execute_tool_graceful_when_down():
    a = _make_oc(port=0)  # 短路 reachable=False
    res = await a.execute_tool("openclaw_vision", {"x": 1})
    assert res.get("ok") is False
    assert "不可达" in res.get("error", "")


async def test_list_available_tools_empty_when_down():
    a = _make_oc(port=0)
    tools = await a.list_available_tools()
    assert tools == []


async def test_refresh_skills_empty_when_down():
    a = _make_oc(port=0)
    skills = await a.refresh_skills()
    assert skills == []


# ---- gateway 可达时真正发起请求 ----

async def test_execute_tool_posts_when_reachable(monkeypatch):
    a = _make_oc()
    a._session = _MockSession(post_status=200, post_json={"result": {"ok": 1}})
    monkeypatch.setattr(a, "_gateway_reachable", _always_true)
    res = await a.execute_tool("openclaw_vision", {"img": "data"})
    assert res.get("ok") is True
    assert a._session.post_calls == 1


async def test_list_available_tools_returns_when_reachable(monkeypatch):
    a = _make_oc()
    a._session = _MockSession(get_status=200, post_status=200,
                              get_json={"tools": [{"name": "t1"}]})
    # _gateway_reachable 用 .get(health_path)；list 用 .get(/tools)
    monkeypatch.setattr(a, "_gateway_reachable", _always_true)
    tools = await a.list_available_tools()
    assert tools == [{"name": "t1"}]
