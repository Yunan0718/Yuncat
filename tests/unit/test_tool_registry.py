"""tests/unit/test_tool_registry.py —— 工具注册中心测试"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture(autouse=True)
def _restore_registry():
    """每个测试后恢复原始工具注册表，避免清空影响后续测试（如 Phase24）。"""
    from tools.registry import _tools

    original = dict(_tools)
    yield
    _tools.clear()
    _tools.update(original)


class TestToolMeta:
    """测试 ToolMeta dataclass"""

    def test_toolmeta_defaults(self):
        """ToolMeta 默认字段值正确"""
        from tools.registry import ToolMeta
        meta = ToolMeta(
            name="test",
            func=lambda: None,
            agents=["hermes"],
            description="test tool",
            parameters={},
        )
        assert meta.confirmable is False
        assert meta.is_concurrency_safe is False
        assert meta.is_read_only is False

    def test_toolmeta_explicit_flags(self):
        """显式设置安全标志"""
        from tools.registry import ToolMeta
        meta = ToolMeta(
            name="read",
            func=lambda: None,
            agents=["hermes"],
            description="read only",
            parameters={},
            is_read_only=True,
            is_concurrency_safe=True,
            confirmable=False,
        )
        assert meta.is_read_only is True
        assert meta.is_concurrency_safe is True


class TestToolDecorator:
    """测试 @tool 装饰器"""

    def test_tool_registration(self):
        """@tool 装饰器正确注册工具"""
        from tools.registry import tool, get_tool, _tools
        # 清理注册表
        _tools.clear()

        @tool(name="my_tool", agents=["hermes"], description="desc", parameters={"type": "object"})
        async def my_tool():
            return "ok"

        meta = get_tool("my_tool")
        assert meta is not None
        assert meta.name == "my_tool"
        assert meta.agents == ["hermes"]
        assert meta.description == "desc"

    def test_tool_confirmable_flag(self):
        """confirmable 标记正确传递"""
        from tools.registry import tool, get_tool, _tools
        _tools.clear()

        @tool(name="write", agents=["hermes"], description="w", parameters={}, confirmable=True)
        async def write_tool():
            return None

        meta = get_tool("write")
        assert meta.confirmable is True

    def test_tool_read_only_flag(self):
        """is_read_only 标记正确传递"""
        from tools.registry import tool, get_tool, _tools
        _tools.clear()

        @tool(name="read", agents=["hermes"], description="r", parameters={}, is_read_only=True, is_concurrency_safe=True)
        async def read_tool():
            return None

        meta = get_tool("read")
        assert meta.is_read_only is True
        assert meta.is_concurrency_safe is True


class TestListTools:
    """测试 list_tools"""

    def test_list_tools_filters_by_agent(self):
        """按 agent_id 过滤工具"""
        from tools.registry import tool, list_tools, _tools
        _tools.clear()

        @tool(name="t1", agents=["hermes"], description="d1", parameters={})
        async def t1():
            pass

        @tool(name="t2", agents=["openclaw"], description="d2", parameters={})
        async def t2():
            pass

        hermes_tools = list_tools("hermes")
        # Phase 47: hermes 工具列表含自身工具 + 注入的 OC 工具（6个来自 config/tools/openclaw.yaml）
        # Phase 48 T3-17: OC 工具注册到 _tools 统一来源，hermes 可见
        assert len(hermes_tools) >= 1
        assert any(t["name"] == "t1" for t in hermes_tools)
        # t2 是 openclaw 专属，hermes 不可见
        assert not any(t["name"] == "t2" for t in hermes_tools)

        all_tools = list_tools()
        # Phase 48 T3-17: OC 工具注册到 _tools 后，list_tools() 也返回它们（agents=["hermes"]）
        # 用 >= 2 适应 OC 工具是否已加载的两种状态
        assert len(all_tools) >= 2
        assert any(t["name"] == "t1" for t in all_tools)
        assert any(t["name"] == "t2" for t in all_tools)

    def test_list_tools_includes_confirmable(self):
        """list_tools 输出包含 confirmable 标记"""
        from tools.registry import tool, list_tools, _tools
        _tools.clear()

        @tool(name="cf", agents=["hermes"], description="d", parameters={}, confirmable=True)
        async def cf():
            pass

        result = list_tools()
        assert result[0]["confirmable"] is True

    def test_get_tool_missing(self):
        """获取不存在的工具返回 None"""
        from tools.registry import get_tool
        assert get_tool("nonexistent") is None


class TestFailClosed:
    """v40: fail-closed 默认行为"""

    def test_tool_default_not_concurrency_safe(self):
        """新工具默认 is_concurrency_safe=False（串行）"""
        from tools.registry import ToolMeta
        meta = ToolMeta(
            name="unsafe", func=lambda: None, agents=["h"], description="d", parameters={}
        )
        assert meta.is_concurrency_safe is False, "默认应串行执行"


class TestActiveTools:
    """v40: 工具惰性加载"""

    async def test_active_tools_includes_core(self):
        """核心 4 工具始终注入"""
        from tools.registry import _tools, tool as tool_deco, get_active_tools
        _tools.clear()

        @tool_deco(name="read_file", agents=["hermes"], description="r", parameters={}, is_read_only=True, is_concurrency_safe=True)
        async def t1(path: str):
            pass

        @tool_deco(name="write_file", agents=["hermes"], description="w", parameters={}, confirmable=True)
        async def t2(path: str, content: str):
            pass

        active = get_active_tools("hermes")
        names = {t["name"] for t in active}
        assert "read_file" in names
        assert "write_file" in names

    async def test_active_tools_filters_by_agent(self):
        """不是该 agent 的工具不注入"""
        from tools.registry import _tools, tool as tool_deco, get_active_tools
        _tools.clear()

        @tool_deco(name="read_file", agents=["openclaw"], description="r", parameters={}, is_read_only=True)
        async def t1(path: str):
            pass

        active = get_active_tools("hermes")
        assert len(active) == 0

    def test_core_tools_list(self):
        """CORE_TOOLS 包含 4 个工具名"""
        from tools.registry import CORE_TOOLS
        assert "read_file" in CORE_TOOLS
        assert "write_file" in CORE_TOOLS
        assert "list_dir" in CORE_TOOLS
        assert "run_command" in CORE_TOOLS
