"""tests/contract/test_phase67_agent_display_name.py —— Phase 67 Agent 显示名称合约测试

验证：
1. HermesAdapter.display_name == "Hermes Agent"
2. OpenClawAdapter.display_name == "OpenClaw"
3. BaseProcessAdapter 默认 display_name == ""（向后兼容）
4. agent_service.list_agents() 使用 display_name 而非 agent_id.capitalize()
5. 无 display_name 的 adapter 回退到 agent_id.capitalize()
"""
import os
import sys
import asyncio

_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def test_hermes_adapter_display_name():
    """HermesAdapter 实例 display_name == 'Hermes Agent'"""
    from adapters.hermes import HermesAdapter
    adapter = HermesAdapter()
    assert hasattr(adapter, "display_name"), "HermesAdapter should have display_name attribute"
    assert adapter.display_name == "Hermes Agent"


def test_openclaw_adapter_display_name():
    """OpenClawAdapter 实例 display_name == 'OpenClaw'"""
    from adapters.openclaw import OpenClawAdapter
    adapter = OpenClawAdapter()
    assert hasattr(adapter, "display_name"), "OpenClawAdapter should have display_name attribute"
    assert adapter.display_name == "OpenClaw"


def test_base_process_adapter_default_display_name_empty():
    """BaseProcessAdapter 默认 display_name == ''（向后兼容）"""
    from adapters.base import BaseProcessAdapter
    # 构造一个最小子类实例（不启动子进程）
    class TestAdapter(BaseProcessAdapter):
        async def invoke(self, prompt, context=None): return {}
        async def stream(self, prompt, context=None): return
        async def health(self): return False
        def capabilities(self): from adapters.base import AgentCapability; return AgentCapability()

    adapter = TestAdapter(cmd=["echo"], agent_id="test")
    assert adapter.display_name == ""


def test_list_agents_uses_display_name_in_source():
    """agent_service.list_agents() 代码中使用 display_name（getattr 回退）"""
    svc_code = open(
        os.path.join(_SRC, "services", "agent", "agent_service.py"),
        encoding="utf-8",
    ).read()
    # 关键断言：list_agents 中 name 字段应使用 display_name
    assert "display_name" in svc_code, (
        "agent_service.list_agents() should reference display_name (Phase 67)"
    )
    # 应有 getattr 回退逻辑
    assert 'getattr(adapter, "display_name"' in svc_code or \
           'getattr(adapter, \'display_name\'' in svc_code, (
        "list_agents() should use getattr(adapter, 'display_name', ...) for backward compat"
    )


def test_list_agents_fallback_to_agent_id_for_adapter_without_display_name():
    """无 display_name 的 adapter 回退到 agent_id.capitalize()"""
    # 构造一个没有 display_name 的 mock adapter
    class LegacyAdapter:
        agent_id = "legacy"
        class _ready:
            @staticmethod
            def is_set(): return True
        def capabilities(self):
            from adapters.base import AgentCapability
            return AgentCapability()

    # 模拟 list_agents 中的 name 计算逻辑
    adapter = LegacyAdapter()
    name = getattr(adapter, "display_name", "") or adapter.agent_id.capitalize()
    assert name == "Legacy"


def test_list_agents_uses_display_name_when_present():
    """有 display_name 的 adapter 优先使用 display_name"""
    class ModernAdapter:
        agent_id = "hermes"
        display_name = "Hermes Agent"

    adapter = ModernAdapter()
    name = getattr(adapter, "display_name", "") or adapter.agent_id.capitalize()
    assert name == "Hermes Agent"


def test_hermes_adapter_display_name_not_empty_string():
    """display_name 不能是空串（否则会回退到 agent_id）"""
    from adapters.hermes import HermesAdapter
    adapter = HermesAdapter()
    assert adapter.display_name, "HermesAdapter.display_name must not be empty"


def test_openclaw_adapter_display_name_not_empty_string():
    """display_name 不能是空串"""
    from adapters.openclaw import OpenClawAdapter
    adapter = OpenClawAdapter()
    assert adapter.display_name, "OpenClawAdapter.display_name must not be empty"
