"""tests/unit/test_mcp_confirmable.py —— Task 7.9 MCP 工具 confirmable 按 is_read_only

覆盖：
1. McpTool 数据类捕获 annotations 字段（MCP 2025-06-18 规范）
2. StdioTransport.list_tools() 从原始工具 dict 提取 annotations
3. _tool_to_meta() 从 annotations.readOnlyHint 提取 is_read_only
4. _register_tool() 根据 is_read_only 设置 confirmable

对标 MCP 规范 + 主流做法：
  - MCP 协议定义 annotations.readOnlyHint（bool）——true 表示工具只读无副作用
  - 无 annotations 或 readOnlyHint 缺失时默认 is_read_only=False（fail-safe，保守假设有副作用）
  - is_read_only=True → confirmable=False（只读工具无需确认）
  - is_read_only=False → confirmable=True（写操作需用户确认，人在循环）

参考：
  - https://modelcontextprotocol.io/legacy/concepts/tools#tool-annotations
  - https://festi.dev/Plugins/MCPServer/docs/ToolAnnotations/（无注解默认 destructiveHint=true）
"""
import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from services.mcp_client import McpTool, StdioTransport
from services import mcp_bridge


# ═══════════════════════════════════════════════════════════════
# McpTool 数据类——annotations 字段
# ═══════════════════════════════════════════════════════════════

class TestMcpToolAnnotations:
    """Task 7.9: McpTool 应捕获 annotations 字段。"""

    def test_mcp_tool_has_annotations_field(self):
        """McpTool 数据类应有 annotations 字段。"""
        mt = McpTool(name="test_tool")
        assert hasattr(mt, "annotations"), "McpTool must have 'annotations' field"

    def test_mcp_tool_annotations_defaults_to_empty_dict(self):
        """annotations 默认为空 dict（无声明）。"""
        mt = McpTool(name="test_tool")
        assert mt.annotations == {}, "annotations should default to empty dict"

    def test_mcp_tool_accepts_annotations_in_constructor(self):
        """构造器应接受 annotations 参数。"""
        mt = McpTool(
            name="get_weather",
            annotations={"readOnlyHint": True},
        )
        assert mt.annotations == {"readOnlyHint": True}

    def test_mcp_tool_preserves_full_annotations_dict(self):
        """应保留完整 annotations dict（含 readOnlyHint/destructiveHint/idempotentHint 等）。"""
        anns = {
            "title": "Get Weather",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
        mt = McpTool(name="get_weather", annotations=anns)
        assert mt.annotations == anns


# ═══════════════════════════════════════════════════════════════
# StdioTransport.list_tools()——从原始 dict 提取 annotations
# ═══════════════════════════════════════════════════════════════

class TestListToolsExtractsAnnotations:
    """Task 7.9: list_tools() 应从原始工具 dict 提取 annotations。"""

    async def test_list_tools_extracts_annotations_when_present(self):
        """工具 dict 含 annotations 时，McpTool.annotations 应被填充。"""
        transport = StdioTransport(command=["fake"])
        # mock _request 返回含 annotations 的工具
        transport._request = AsyncMock(return_value={
            "result": {
                "tools": [
                    {
                        "name": "get_weather",
                        "description": "Get weather",
                        "inputSchema": {"type": "object"},
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            }
        })
        tools = await transport.list_tools()
        assert len(tools) == 1
        assert tools[0].annotations == {"readOnlyHint": True}

    async def test_list_tools_annotations_defaults_empty_when_absent(self):
        """工具 dict 不含 annotations 时，McpTool.annotations 应为空 dict。"""
        transport = StdioTransport(command=["fake"])
        transport._request = AsyncMock(return_value={
            "result": {
                "tools": [
                    {
                        "name": "write_file",
                        "description": "Write file",
                        "inputSchema": {"type": "object"},
                        # 无 annotations 字段
                    }
                ]
            }
        })
        tools = await transport.list_tools()
        assert len(tools) == 1
        assert tools[0].annotations == {}


# ═══════════════════════════════════════════════════════════════
# _tool_to_meta()——从 annotations.readOnlyHint 提取 is_read_only
# ═══════════════════════════════════════════════════════════════

class TestToolToMetaReadOnly:
    """Task 7.9: _tool_to_meta() 应从 annotations.readOnlyHint 提取 is_read_only。"""

    def test_read_only_hint_true_sets_is_read_only_true(self):
        """annotations.readOnlyHint=True → is_read_only=True。"""
        tool = McpTool(
            name="get_weather",
            description="Get weather",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": True},
        )
        meta = mcp_bridge._tool_to_meta("srv1", tool)
        assert meta.get("is_read_only") is True

    def test_read_only_hint_false_sets_is_read_only_false(self):
        """annotations.readOnlyHint=False → is_read_only=False。"""
        tool = McpTool(
            name="write_file",
            description="Write file",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": False, "destructiveHint": True},
        )
        meta = mcp_bridge._tool_to_meta("srv1", tool)
        assert meta.get("is_read_only") is False

    def test_no_annotations_defaults_is_read_only_false(self):
        """无 annotations → is_read_only=False（fail-safe，保守假设有副作用）。"""
        tool = McpTool(
            name="unknown_tool",
            description="Unknown",
            input_schema={"type": "object"},
        )
        meta = mcp_bridge._tool_to_meta("srv1", tool)
        assert meta.get("is_read_only") is False

    def test_annotations_without_read_only_hint_defaults_false(self):
        """annotations 存在但无 readOnlyHint → is_read_only=False（fail-safe）。"""
        tool = McpTool(
            name="partial_tool",
            description="Partial",
            input_schema={"type": "object"},
            annotations={"title": "Partial Tool"},  # 有 annotations 但无 readOnlyHint
        )
        meta = mcp_bridge._tool_to_meta("srv1", tool)
        assert meta.get("is_read_only") is False

    def test_tool_to_meta_preserves_other_fields(self):
        """_tool_to_meta 仍应保留 name/description/parameters 等原有字段。"""
        tool = McpTool(
            name="get_weather",
            description="Get weather",
            input_schema={"type": "object", "properties": {"loc": {"type": "string"}}},
            annotations={"readOnlyHint": True},
        )
        meta = mcp_bridge._tool_to_meta("srv1", tool)
        assert meta["server_id"] == "srv1"
        assert meta["name"] == "get_weather"
        assert meta["original_name"] == "get_weather"
        assert meta["description"] == "Get weather"
        assert meta["parameters"] == {"type": "object", "properties": {"loc": {"type": "string"}}}


# ═══════════════════════════════════════════════════════════════
# _register_tool()——根据 is_read_only 设置 confirmable
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def clean_mcp_registry():
    """每个测试前后清理 MCP 工具注册表 + 全局 registry，避免状态污染。"""
    from tools import registry as tool_registry
    # 记录原始状态
    orig_mcp_reg = dict(mcp_bridge._mcp_tool_registry)
    orig_tools = dict(tool_registry._tools)
    # 清空
    mcp_bridge._mcp_tool_registry.clear()
    yield
    # 恢复
    mcp_bridge._mcp_tool_registry.clear()
    mcp_bridge._mcp_tool_registry.update(orig_mcp_reg)
    tool_registry._tools.clear()
    tool_registry._tools.update(orig_tools)


class TestRegisterToolConfirmable:
    """Task 7.9: _register_tool() 应根据 is_read_only 设置 confirmable。"""

    def test_read_only_tool_not_confirmable(self, clean_mcp_registry):
        """is_read_only=True 的 MCP 工具 → confirmable=False（只读无需确认）。"""
        meta = {
            "server_id": "srv1",
            "original_name": "get_weather",
            "name": "mcp_srv1_get_weather",
            "description": "Get weather",
            "parameters": {"type": "object"},
            "is_read_only": True,
        }
        mcp_bridge._register_tool(meta)
        from tools.registry import get_tool
        tm = get_tool("mcp_srv1_get_weather")
        assert tm is not None
        assert tm.is_read_only is True
        assert tm.confirmable is False, "read-only tool should not require confirmation"

    def test_write_tool_confirmable(self, clean_mcp_registry):
        """is_read_only=False 的 MCP 工具 → confirmable=True（写操作需确认）。"""
        meta = {
            "server_id": "srv1",
            "original_name": "write_file",
            "name": "mcp_srv1_write_file",
            "description": "Write file",
            "parameters": {"type": "object"},
            "is_read_only": False,
        }
        mcp_bridge._register_tool(meta)
        from tools.registry import get_tool
        tm = get_tool("mcp_srv1_write_file")
        assert tm is not None
        assert tm.is_read_only is False
        assert tm.confirmable is True, "write tool should require confirmation"

    def test_no_is_read_only_defaults_confirmable(self, clean_mcp_registry):
        """meta 不含 is_read_only → 默认 confirmable=True（fail-safe）。"""
        meta = {
            "server_id": "srv1",
            "original_name": "unknown_tool",
            "name": "mcp_srv1_unknown",
            "description": "Unknown",
            "parameters": {"type": "object"},
            # 无 is_read_only 字段
        }
        mcp_bridge._register_tool(meta)
        from tools.registry import get_tool
        tm = get_tool("mcp_srv1_unknown")
        assert tm is not None
        assert tm.is_read_only is False
        assert tm.confirmable is True, "unknown tool should default to confirmable (fail-safe)"

    def test_registered_tool_meta_has_correct_is_read_only(self, clean_mcp_registry):
        """注册的 ToolMeta 应保留正确的 is_read_only 值。"""
        meta = {
            "server_id": "srv1",
            "original_name": "search",
            "name": "mcp_srv1_search",
            "description": "Search (read-only)",
            "parameters": {"type": "object"},
            "is_read_only": True,
        }
        mcp_bridge._register_tool(meta)
        from tools.registry import get_tool
        tm = get_tool("mcp_srv1_search")
        assert tm is not None
        assert tm.is_read_only is True
        assert tm.groups == ["mcp"]

    def test_register_tool_idempotent(self, clean_mcp_registry):
        """重复注册同一工具应幂等（不报错）。"""
        meta = {
            "server_id": "srv1",
            "original_name": "get_weather",
            "name": "mcp_srv1_get_weather",
            "description": "Get weather",
            "parameters": {"type": "object"},
            "is_read_only": True,
        }
        mcp_bridge._register_tool(meta)
        # 第二次注册应幂等跳过
        mcp_bridge._register_tool(meta)
        from tools.registry import get_tool
        tm = get_tool("mcp_srv1_get_weather")
        assert tm is not None
        assert tm.confirmable is False


# ═══════════════════════════════════════════════════════════════
# 端到端：McpTool(annotations) → _tool_to_meta → _register_tool
# ═══════════════════════════════════════════════════════════════

class TestEndToEndReadOnlyToConfirmable:
    """Task 7.9: 端到端——McpTool.annotations.readOnlyHint → confirmable。"""

    def test_read_only_tool_e2e(self, clean_mcp_registry):
        """只读工具端到端：readOnlyHint=True → confirmable=False。"""
        tool = McpTool(
            name="get_time",
            description="Get current time",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": True},
        )
        meta = mcp_bridge._tool_to_meta("srv1", tool)
        mcp_bridge._register_tool(meta)
        from tools.registry import get_tool
        tm = get_tool("get_time")
        assert tm is not None
        assert tm.is_read_only is True
        assert tm.confirmable is False

    def test_write_tool_e2e(self, clean_mcp_registry):
        """写工具端到端：无 annotations → confirmable=True。"""
        tool = McpTool(
            name="create_file",
            description="Create a file",
            input_schema={"type": "object"},
        )
        meta = mcp_bridge._tool_to_meta("srv1", tool)
        mcp_bridge._register_tool(meta)
        from tools.registry import get_tool
        tm = get_tool("create_file")
        assert tm is not None
        assert tm.is_read_only is False
        assert tm.confirmable is True
