"""tests/contract/test_phase31_bridge.py — Phase 31 Agent 桥接合约测试"""
import os, sys, pytest


# ---- OpenClaw execute_tool ----

def test_openclaw_has_execute_tool():
    """OpenClawAdapter 有 execute_tool 方法"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from adapters.openclaw import OpenClawAdapter
    assert hasattr(OpenClawAdapter, "execute_tool"), "OpenClawAdapter missing execute_tool"
    assert callable(OpenClawAdapter.execute_tool)


def test_openclaw_has_list_tools():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from adapters.openclaw import OpenClawAdapter
    assert hasattr(OpenClawAdapter, "list_available_tools")


def test_openclaw_execute_tool_not_connected():
    """未启动 gateway 时 execute_tool 返回错误（不抛异常）"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    import asyncio
    from adapters.openclaw import OpenClawAdapter
    oc = OpenClawAdapter(port_hint=19999)
    result = asyncio.run(oc.execute_tool("test_tool", {"arg": "value"}))
    assert result["ok"] is False  # 未连接
    assert "error" in result


def test_openclaw_list_tools_from_manifest():
    """list_available_tools 返回 manifest 中的工具"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    import asyncio
    from adapters.openclaw import OpenClawAdapter
    oc = OpenClawAdapter(port_hint=19999)
    if not asyncio.run(oc._gateway_reachable()):
        pytest.skip("OpenClaw gateway not running (integration)")
    tools = asyncio.run(oc.list_available_tools())
    assert isinstance(tools, list)
    # 至少有 openclaw_vision
    names = [t["name"] for t in tools]
    assert "openclaw_vision" in names, f"Expected openclaw_vision in manifest, got {names}"


# ---- Hermes execute_tool + list_available_tools ----

def test_hermes_has_execute_tool():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from adapters.hermes import HermesAdapter
    assert hasattr(HermesAdapter, "execute_tool")
    assert hasattr(HermesAdapter, "list_available_tools")


def test_hermes_fallback_manifest():
    """list_available_tools 返回 Hermes 可用工具"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    import asyncio
    from adapters.hermes import HermesAdapter
    h = HermesAdapter()
    if not asyncio.run(h._gateway_reachable()):
        pytest.skip("Hermes gateway not running (integration)")
    tools = asyncio.run(h.list_available_tools())
    assert isinstance(tools, list)
    names = [t["name"] for t in tools]
    assert "hermes_web_search" in names or "hermes_shell_exec" in names or "delegate_to_agent" in names


# ---- notify 端点 ----

def test_notify_uses_execute_tool():
    """routes/notify.py 的 notify 通过 MessagingHub 调用 execute_tool，而非直接 HTTP POST"""
    base_dir = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
    notify_code = open(os.path.join(base_dir, "routes", "notify.py"), encoding="utf-8").read()
    hub_code = open(os.path.join(base_dir, "services", "messaging_hub.py"), encoding="utf-8").read()

    fn_section = notify_code.split("async def send_notification")[1].split("\nasync def ")[0]
    assert "send_message(" in fn_section, "notify should delegate to MessagingHub.send_message"
    assert "from services.messaging_hub import" in notify_code
    assert "f\"http://127.0.0.1:" not in notify_code, "notify should NOT make direct HTTP call to OpenClaw"

    # Hub 内部必须走 execute_tool / ToolService.execute（副作用唯一入口）
    assert "adapter.execute_tool" in hub_code or "tool_service.execute" in hub_code


def test_notify_platform_tool_map():
    """MessagingHub 使用正确的平台→工具映射"""
    hub_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "services", "messaging_hub.py"),
        encoding="utf-8",
    ).read()
    assert "telegram_send" in hub_code
    assert "discord_send" in hub_code
    assert "wechat_work_send" in hub_code


# ---- 桥接 API 端点 ----

def test_main_has_bridge_endpoints():
    routes_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "routes", "agents.py"),
        encoding="utf-8",
    ).read()
    assert "/agents/{agent_id}/tools" in routes_code
    assert "/agents/{agent_id}/tools/{tool_name}/invoke" in routes_code


# ---- Hermes manifest 文件 ----

def test_hermes_manifest_exists():
    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "src-python", "config", "tools", "hermes.yaml",
    )
    assert os.path.exists(path)
    import yaml
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    tools = data.get("tools", [])
    assert len(tools) >= 5, f"Expected >=5 tools in hermes.yaml, got {len(tools)}"


def test_openclaw_manifest_exists():
    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "src-python", "config", "tools", "openclaw.yaml",
    )
    assert os.path.exists(path)


# ---- Platform bridge execute_tool delegate ----

def test_platform_bridge_delegation_hint():
    pb = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "adapters", "platform_bridge.py"),
        encoding="utf-8",
    ).read()
    assert "Phase 31" in pb, "platform_bridge should mention Phase 31 delegation hint"
    assert "adapter.execute_tool" in pb, "platform_bridge should hint at adapter.execute_tool"


# ---- OpenClaw capabilities still correct ----

def test_openclaw_capabilities():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from adapters.openclaw import OpenClawAdapter
    oc = OpenClawAdapter(port_hint=19999)
    caps = oc.capabilities()
    assert caps.supports_image is True
    assert caps.supports_tools is True
