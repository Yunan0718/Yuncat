"""tests/unit/test_mcp_bridge.py —— Phase U.2+U.3: MCP 桥接与注册测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services import mcp_bridge as _bridge


@pytest.fixture(autouse=True)
def _reset_bridge_state():
    """每个测试前清空 bridge 全局状态，避免交叉污染。"""
    _bridge._transports.clear()
    _bridge._mcp_tool_registry.clear()
    _bridge._server_configs.clear()
    _bridge._server_health.clear()
    from tools import registry as _registry
    original_tools = dict(_registry._tools)
    _registry._tools.clear()
    yield
    _bridge._transports.clear()
    _bridge._mcp_tool_registry.clear()
    _bridge._server_configs.clear()
    _bridge._server_health.clear()
    _registry._tools.clear()
    _registry._tools.update(original_tools)


@pytest.mark.asyncio
async def test_discover_tools_registers_and_returns_tools():
    """discover_tools 发现 MCP 工具并注册到 tools/registry。"""
    fake_tool = MagicMock()
    fake_tool.name = "list_directory"
    fake_tool.description = "List a directory"
    fake_tool.input_schema = {"type": "object"}

    transport = AsyncMock()
    transport.is_alive = True
    transport.list_tools = AsyncMock(return_value=[fake_tool])

    _bridge._server_configs = [{
        "id": "srv-1", "name": "fs", "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem"],
        "env": {}, "enabled": True,
    }]

    from tools import registry as _registry
    with patch("services.mcp_bridge._get_transport", new=AsyncMock(return_value=transport)):
        tools = await _bridge.discover_tools(conn=None)

    assert len(tools) == 1
    assert tools[0]["name"] == "list_directory"
    assert _registry.get_tool("list_directory") is not None
    # 重构后：MCP 工具注册到默认原生 agent(yuncat) + openclaw 网关，
    # 不再硬编码 hermes（见 services/mcp_bridge.py:143 使用 DEFAULT_AGENT_ID / OPENCLAW_AGENT_ID）。
    assert _registry.get_tool("list_directory").agents == ["yuncat", "openclaw"]


@pytest.mark.asyncio
async def test_call_mcp_tool_success():
    """call_mcp_tool 将调用转发到对应 transport。"""
    transport = AsyncMock()
    transport.is_alive = True
    transport.call_tool = AsyncMock(return_value={"content": "ok"})
    _bridge._transports["srv-1"] = transport

    result = await _bridge.call_mcp_tool("srv-1", "echo", {"text": "hi"})
    assert result["success"] is True
    transport.call_tool.assert_awaited_once_with("echo", {"text": "hi"})


@pytest.mark.asyncio
async def test_call_mcp_tool_dead_transport():
    """transport 不可用时返回结构化错误。"""
    _bridge._transports["srv-1"] = None
    result = await _bridge.call_mcp_tool("srv-1", "echo", {})
    assert result["success"] is False


@pytest.mark.asyncio
async def test_hermes_adapter_is_external_gateway():
    """Phase 融合重构后：Hermes 改为外部 gateway，Yuncat 不再为其构建 payload
    或注入 MCP 工具——工具由用户侧 Hermes gateway 自行管理。

    原 test_hermes_payload_includes_mcp_tools（断言 HermesAdapter._build_payload 注入
    MCP 工具）已不适用：外部 gateway 模式下不存在 _build_payload。本测试验证新契约。
    """
    from adapters.hermes import HermesAdapter
    from adapters.ext_http import HttpGatewayAgentAdapter

    adapter = HermesAdapter()
    # 外部 gateway 模式：不再构建自己的推理 payload（无 _build_payload）
    assert not hasattr(adapter, "_build_payload"), \
        "HermesAdapter 已改为外部 gateway，不应再构建 payload / 注入 MCP 工具"
    # 它连接用户侧 gateway，自身是 HttpGatewayAgentAdapter
    assert issubclass(HermesAdapter, HttpGatewayAgentAdapter)
    # Yuncat 原生 agent 仍可通过 mcp_bridge 直接调用 MCP 工具（见本文件其他用例）
    assert hasattr(_bridge, "call_mcp_tool")


@pytest.mark.asyncio
async def test_health_check_success():
    """transport 存活且 list_tools 成功 → healthy=True。"""
    transport = AsyncMock()
    transport.is_alive = True
    transport.list_tools = AsyncMock(return_value=[MagicMock()])
    _bridge._transports["srv-1"] = transport

    result = await _bridge.health_check("srv-1")
    assert result["healthy"] is True
    assert result["error"] is None
    transport.list_tools.assert_awaited_once()


@pytest.mark.asyncio
async def test_health_check_dead_transport():
    """transport 已死 → healthy=False，记录未连接错误。"""
    transport = AsyncMock()
    transport.is_alive = False
    _bridge._transports["srv-1"] = transport

    result = await _bridge.health_check("srv-1")
    assert result["healthy"] is False
    assert "not connected" in result["error"]


@pytest.mark.asyncio
async def test_health_check_failure_clears_transport():
    """list_tools 抛异常 → healthy=False 且 transport 被清理。"""
    transport = AsyncMock()
    transport.is_alive = True
    transport.list_tools = AsyncMock(side_effect=RuntimeError("boom"))
    transport.stop = AsyncMock()
    _bridge._transports["srv-1"] = transport

    result = await _bridge.health_check("srv-1")
    assert result["healthy"] is False
    assert "boom" in result["error"]
    assert "srv-1" not in _bridge._transports


@pytest.mark.asyncio
async def test_health_check_all_checks_configured_servers():
    """health_check_all 探测所有 enabled 配置中的服务器。"""
    _bridge._server_configs = [
        {"id": "srv-1", "enabled": True},
        {"id": "srv-2", "enabled": True},
        {"id": "srv-3", "enabled": False},
    ]
    transport = AsyncMock()
    transport.is_alive = True
    transport.list_tools = AsyncMock(return_value=[MagicMock()])
    _bridge._transports["srv-1"] = transport

    results = await _bridge.health_check_all()
    assert "srv-1" in results
    assert "srv-2" in results
    assert "srv-3" not in results
    assert results["srv-1"]["healthy"] is True


@pytest.mark.asyncio
async def test_get_health_status_returns_snapshot():
    """get_health_status 返回最近一次健康状态，不触发探测。"""
    _bridge._server_health["srv-1"] = {"healthy": True, "last_check": 123.0, "error": None}
    snapshot = _bridge.get_health_status()
    assert snapshot["srv-1"]["healthy"] is True
