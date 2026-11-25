"""tests/unit/test_register_tool_api.py —— register_tool 公开 API 测试

Phase 47 4.2: 验证运行时工具注册 API（对标 Claude Code MCP 工具扩展）。

覆盖:
1. 基本注册 — register_tool() 后 get_tool() / list_tools() 可见
2. 重复拒绝 — 同名工具默认 raise ValueError
3. 幂等 — 同一 ToolMeta 实例重复注册静默跳过
4. overwrite=True — 显式覆盖
5. unregister_tool() — 注销后不可见
6. 无效输入 — 非 ToolMeta / 空名 / 不可调用 func
7. 集成 — mcp_bridge 不再直接访问 _tools 私有字典
"""
import os
import sys
import asyncio
import pytest

_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools.registry import (
    ToolMeta,
    register_tool,
    unregister_tool,
    get_tool,
    list_tools,
    _tools,
)


# ── 测试用工具 handler ──

async def _dummy_handler(**kwargs):
    return {"ok": True, "result": kwargs}


def _make_meta(name: str = "test_custom_tool", **overrides) -> ToolMeta:
    defaults = dict(
        name=name,
        func=_dummy_handler,
        agents=["hermes"],
        description="Test tool for register_tool API",
        parameters={"type": "object", "properties": {}},
        confirmable=False,
        is_read_only=True,
        concurrency_safety="safe",
        groups=["test"],
    )
    defaults.update(overrides)
    return ToolMeta(**defaults)


# ── fixture: 确保测试后清理注册的工具 ──

@pytest.fixture
def cleanup_test_tools():
    """注册测试工具后自动清理，避免污染全局 registry。"""
    registered = []
    yield registered
    for name in registered:
        unregister_tool(name)


# ═══════════════════════════════════════════
# 1. 基本注册
# ═══════════════════════════════════════════

def test_register_tool_basic(cleanup_test_tools):
    """register_tool() 后 get_tool() 能找到工具。"""
    meta = _make_meta("test_register_basic")
    register_tool(meta)
    cleanup_test_tools.append("test_register_basic")

    found = get_tool("test_register_basic")
    assert found is not None
    assert found.name == "test_register_basic"
    assert found.description == "Test tool for register_tool API"
    assert found.is_read_only is True


def test_register_tool_visible_in_list_tools(cleanup_test_tools):
    """注册后 list_tools() 包含新工具。"""
    meta = _make_meta("test_register_list")
    register_tool(meta)
    cleanup_test_tools.append("test_register_list")

    tools = list_tools(agent_id="hermes")
    names = [t["name"] for t in tools]
    assert "test_register_list" in names


def test_register_tool_agent_filter(cleanup_test_tools):
    """注册到特定 agent 的工具只对该 agent 可见。"""
    meta = _make_meta("test_register_agent", agents=["openclaw"])
    register_tool(meta)
    cleanup_test_tools.append("test_register_agent")

    hermes_tools = list_tools(agent_id="hermes")
    hermes_names = [t["name"] for t in hermes_tools]
    assert "test_register_agent" not in hermes_names

    oc_tools = list_tools(agent_id="openclaw")
    oc_names = [t["name"] for t in oc_tools]
    assert "test_register_agent" in oc_names


# ═══════════════════════════════════════════
# 2. 重复拒绝
# ═══════════════════════════════════════════

def test_register_tool_duplicate_rejected(cleanup_test_tools):
    """同名工具默认拒绝覆盖。"""
    meta1 = _make_meta("test_register_dup")
    register_tool(meta1)
    cleanup_test_tools.append("test_register_dup")

    meta2 = _make_meta("test_register_dup", description="Different description")
    with pytest.raises(ValueError, match="already registered"):
        register_tool(meta2)


def test_register_tool_does_not_overwrite_existing(cleanup_test_tools):
    """拒绝覆盖时，原工具不变。"""
    meta1 = _make_meta("test_register_keep", description="Original")
    register_tool(meta1)
    cleanup_test_tools.append("test_register_keep")

    meta2 = _make_meta("test_register_keep", description="Modified")
    try:
        register_tool(meta2)
    except ValueError:
        pass

    found = get_tool("test_register_keep")
    assert found.description == "Original"


# ═══════════════════════════════════════════
# 3. 幂等
# ═══════════════════════════════════════════

def test_register_tool_idempotent_same_instance(cleanup_test_tools):
    """同一 ToolMeta 实例重复注册静默跳过（不 raise）。"""
    meta = _make_meta("test_register_idem")
    register_tool(meta)
    cleanup_test_tools.append("test_register_idem")

    # 同一实例再次注册——不 raise
    register_tool(meta)


# ═══════════════════════════════════════════
# 4. overwrite=True
# ═══════════════════════════════════════════

def test_register_tool_overwrite(cleanup_test_tools):
    """overwrite=True 显式覆盖。"""
    meta1 = _make_meta("test_register_over", description="Original")
    register_tool(meta1)
    cleanup_test_tools.append("test_register_over")

    meta2 = _make_meta("test_register_over", description="Replaced")
    register_tool(meta2, overwrite=True)

    found = get_tool("test_register_over")
    assert found.description == "Replaced"


# ═══════════════════════════════════════════
# 5. unregister_tool
# ═══════════════════════════════════════════

def test_unregister_tool_removes_from_registry(cleanup_test_tools):
    """unregister_tool() 后 get_tool() 返回 None。"""
    meta = _make_meta("test_unregister")
    register_tool(meta)
    cleanup_test_tools.append("test_unregister")

    assert get_tool("test_unregister") is not None
    result = unregister_tool("test_unregister")
    assert result is True
    assert get_tool("test_unregister") is None
    cleanup_test_tools.remove("test_unregister")  # 已清理


def test_unregister_tool_returns_false_for_unknown():
    """注销不存在的工具返回 False（不 raise）。"""
    result = unregister_tool("nonexistent_tool_xyz_123")
    assert result is False


def test_unregister_tool_clears_usage_count(cleanup_test_tools):
    """注销后使用计数也被清理。"""
    from tools.registry import record_tool_usage, get_tool_usage_stats
    meta = _make_meta("test_unregister_usage")
    register_tool(meta)
    cleanup_test_tools.append("test_unregister_usage")

    record_tool_usage("test_unregister_usage")
    assert "test_unregister_usage" in get_tool_usage_stats()

    unregister_tool("test_unregister_usage")
    cleanup_test_tools.remove("test_unregister_usage")
    assert "test_unregister_usage" not in get_tool_usage_stats()


# ═══════════════════════════════════════════
# 6. 无效输入
# ═══════════════════════════════════════════

def test_register_tool_rejects_non_toolmeta():
    """非 ToolMeta 实例 raise TypeError。"""
    with pytest.raises(TypeError, match="must be ToolMeta"):
        register_tool({"name": "not_a_meta"})  # type: ignore


def test_register_tool_rejects_empty_name():
    """空名 raise ValueError。"""
    meta = ToolMeta(
        name="",
        func=_dummy_handler,
        agents=["hermes"],
        description="",
        parameters={},
    )
    with pytest.raises(ValueError, match="name must not be empty"):
        register_tool(meta)


def test_register_tool_rejects_non_callable_func():
    """不可调用 func raise ValueError。"""
    meta = ToolMeta(
        name="test_bad_func",
        func="not_a_function",  # type: ignore
        agents=["hermes"],
        description="",
        parameters={},
    )
    with pytest.raises(ValueError, match="func must be a callable"):
        register_tool(meta)


# ═══════════════════════════════════════════
# 7. 集成 — mcp_bridge 使用公开 API
# ═══════════════════════════════════════════

def test_mcp_bridge_uses_public_api():
    """mcp_bridge._register_tool 不再直接访问 _tools 私有字典。

    静态检查：源码中不应出现 _registry._tools 或 registry._tools 的直接赋值。
    """
    mcp_path = os.path.join(_SRC, "services", "mcp_bridge.py")
    with open(mcp_path, encoding="utf-8") as f:
        source = f.read()
    assert "._tools[" not in source, (
        "mcp_bridge.py should use register_tool() public API, "
        "not direct _tools[...] assignment"
    )
    assert "register_tool(" in source, (
        "mcp_bridge.py should call register_tool() public API"
    )


def test_mcp_bridge_register_tool_integration(cleanup_test_tools):
    """mcp_bridge._register_tool 通过公开 API 注册工具。"""
    from services.mcp_bridge import _register_tool, _mcp_tool_registry
    # 清理可能残留的同名工具
    unregister_tool("test_mcp_integration")
    _mcp_tool_registry.pop("test_mcp_integration", None)

    meta = {
        "name": "test_mcp_integration",
        "server_id": "test_server",
        "original_name": "test_tool",
        "description": "MCP integration test tool",
        "parameters": {"type": "object", "properties": {}},
    }
    _register_tool(meta)
    cleanup_test_tools.append("test_mcp_integration")

    found = get_tool("test_mcp_integration")
    assert found is not None
    assert found.description == "MCP integration test tool"
    assert "mcp" in found.groups


def test_mcp_bridge_collision_skipped(cleanup_test_tools):
    """MCP 工具与原生工具同名时跳过（不覆盖）。"""
    from services.mcp_bridge import _register_tool, _mcp_tool_registry
    # 先注册一个原生工具
    native = _make_meta("test_mcp_collision", description="Native tool")
    register_tool(native)
    cleanup_test_tools.append("test_mcp_collision")
    _mcp_tool_registry.pop("test_mcp_collision", None)

    # MCP 尝试注册同名工具——应跳过
    meta = {
        "name": "test_mcp_collision",
        "server_id": "test_server",
        "original_name": "test_tool",
        "description": "MCP attempt to override",
        "parameters": {"type": "object", "properties": {}},
    }
    _register_tool(meta)

    found = get_tool("test_mcp_collision")
    assert found.description == "Native tool"  # 原生工具未被覆盖
