"""MCP 协议错误必须显式冒泡——不能被折成「空工具列表」。

背景（AGENTS.md 规则 9）：
旧实现把 JSON-RPC 错误 / 超时 / 连接断开折进返回值（`{"error": ...}`），
调用方极易把它当成「正常响应但内容为空」。表现就是——MCP 服务器其实挂了，
工具列表却是空的，UI 显示「该服务器没有提供工具」，真因被静默吞掉。
"""
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from services.mcp_client import McpProtocolError, McpTool, StdioTransport


# ---------------------------------------------------------------- 传输层


@pytest.mark.asyncio
async def test_list_tools_raises_on_protocol_error(monkeypatch):
    transport = StdioTransport(command=["true"])

    async def _fake_request(method, params):
        return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no method"}}

    monkeypatch.setattr(transport, "_request", _fake_request)
    with pytest.raises(McpProtocolError):
        await transport.list_tools()


@pytest.mark.asyncio
async def test_call_tool_raises_on_protocol_error(monkeypatch):
    transport = StdioTransport(command=["true"])

    async def _fake_request(method, params):
        return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}

    monkeypatch.setattr(transport, "_request", _fake_request)
    with pytest.raises(McpProtocolError):
        await transport.call_tool("write_file", {"path": "x"})


@pytest.mark.asyncio
async def test_call_tool_raises_on_malformed_response(monkeypatch):
    """既没有 result 也没有 error = 协议违例，不能当成空结果返回。"""
    transport = StdioTransport(command=["true"])

    async def _fake_request(method, params):
        return {"jsonrpc": "2.0", "id": 1}

    monkeypatch.setattr(transport, "_request", _fake_request)
    with pytest.raises(McpProtocolError):
        await transport.call_tool("echo", {})


@pytest.mark.asyncio
async def test_call_tool_returns_result_payload(monkeypatch):
    transport = StdioTransport(command=["true"])

    async def _fake_request(method, params):
        return {"jsonrpc": "2.0", "id": 1, "result": {"content": "ok"}}

    monkeypatch.setattr(transport, "_request", _fake_request)
    assert await transport.call_tool("echo", {}) == {"content": "ok"}


@pytest.mark.asyncio
async def test_list_tools_returns_tools(monkeypatch):
    transport = StdioTransport(command=["true"])

    async def _fake_request(method, params):
        return {
            "jsonrpc": "2.0", "id": 1,
            "result": {"tools": [{"name": "read_file", "description": "读文件",
                                  "inputSchema": {"type": "object"}}]},
        }

    monkeypatch.setattr(transport, "_request", _fake_request)
    tools = await transport.list_tools()
    assert [t.name for t in tools] == ["read_file"]
    assert tools[0].input_schema == {"type": "object"}


@pytest.mark.asyncio
async def test_request_timeout_raises_protocol_error(monkeypatch):
    """超时必须抛异常——旧实现返回 {"error": timeout}，上层当成空工具。"""
    monkeypatch.setattr("services.mcp_client.MCP_REQUEST_TIMEOUT_SEC", 0.01)
    transport = StdioTransport(command=["true"])
    with pytest.raises(McpProtocolError):
        await transport._request("tools/list", {})


def test_child_env_inherits_parent_and_overrides():
    """子进程环境 = 继承 + 覆盖（Windows 下丢了 PATH/SystemRoot 就起不来）。"""
    transport = StdioTransport(command=["true"], env={"MY_TOKEN": "abc"})
    assert transport._env.get("MY_TOKEN") == "abc"
    assert "PATH" in transport._env or "Path" in transport._env


# ---------------------------------------------------------------- 路由层


class _FakeTransport:
    """替身 StdioTransport——不真的起子进程。"""

    def __init__(self, command=None, env=None, tools=None, error=None, start_error=None):
        self.command = command
        self.env = env
        self._tools = tools or []
        self._error = error
        self._start_error = start_error
        self.stopped = False

    async def start(self):
        if self._start_error:
            raise self._start_error

    async def list_tools(self):
        if self._error:
            raise self._error
        return self._tools

    async def stop(self):
        self.stopped = True


@pytest.fixture
async def mcp_api(tmp_path):
    import aiosqlite
    from repositories import SCHEMA_SQL
    from repositories.mcp_repo import create_mcp_server
    from routes.mcp import router

    conn = await aiosqlite.connect(str(tmp_path / "mcp.db"))
    await conn.executescript(SCHEMA_SQL)
    enabled_id = await create_mcp_server(conn, "fs", "npx", ["-y", "srv"], enabled=True)
    disabled_id = await create_mcp_server(conn, "off", "npx", enabled=False)

    app = FastAPI()
    app.state.db = conn
    app.include_router(router)
    yield TestClient(app), conn, enabled_id, disabled_id
    await conn.close()


def test_tools_endpoint_lists_server_tools(mcp_api, monkeypatch):
    client, _conn, sid, _ = mcp_api
    fake = _FakeTransport(tools=[McpTool(name="read_file", description="读文件",
                                         input_schema={"type": "object"})])
    monkeypatch.setattr("routes.mcp.StdioTransport", lambda **kw: fake)

    resp = client.get(f"/api/v1/mcp/servers/{sid}/tools")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["tools"][0]["name"] == "read_file"
    assert body["tools"][0]["description"] == "读文件"
    assert fake.stopped is True


def test_tools_endpoint_404_unknown_server(mcp_api):
    client, *_ = mcp_api
    resp = client.get("/api/v1/mcp/servers/does-not-exist/tools")
    assert resp.status_code == 404


def test_tools_endpoint_400_disabled_server(mcp_api, monkeypatch):
    client, _conn, _sid, disabled_id = mcp_api
    monkeypatch.setattr("routes.mcp.StdioTransport", lambda **kw: _FakeTransport())
    resp = client.get(f"/api/v1/mcp/servers/{disabled_id}/tools")
    assert resp.status_code == 400


def test_tools_endpoint_502_on_protocol_error(mcp_api, monkeypatch):
    """服务器通信失败必须报错，不能伪装成「没有工具」。"""
    client, _conn, sid, _ = mcp_api
    fake = _FakeTransport(error=McpProtocolError("tools/list failed: timeout"))
    monkeypatch.setattr("routes.mcp.StdioTransport", lambda **kw: fake)

    resp = client.get(f"/api/v1/mcp/servers/{sid}/tools")
    assert resp.status_code == 502, resp.text
    assert "timeout" in resp.json()["detail"]
    assert fake.stopped is True


def test_tools_endpoint_400_when_command_missing(mcp_api, monkeypatch):
    client, _conn, sid, _ = mcp_api
    fake = _FakeTransport(start_error=FileNotFoundError("npx"))
    monkeypatch.setattr("routes.mcp.StdioTransport", lambda **kw: fake)

    resp = client.get(f"/api/v1/mcp/servers/{sid}/tools")
    assert resp.status_code == 400, resp.text
    assert fake.stopped is True
