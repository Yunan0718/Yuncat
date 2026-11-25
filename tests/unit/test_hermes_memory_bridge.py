"""tests/unit/test_hermes_memory_bridge.py —— Phase 51-F: HermesMemoryBridge"""
import pytest

pytestmark = pytest.mark.skip(
    reason="services.hermes_memory_bridge 模块已移除，相关能力由 HermesAdapter 直接提供；测试待清理或模块恢复。"
)


def _make_adapter(available: bool, results=None):
    class FakeAdapter:
        def __init__(self):
            self.agent_id = "hermes"
            self._ha_available = available
            self._ha_port = 12345 if available else 0
            self._proxy_called = False
            self._proxy_results = results

        async def _proxy_tool(self, tool_name: str, args: dict):
            self._proxy_called = True
            return self._proxy_results or {"ok": False, "error": "not found"}

    return FakeAdapter()


@pytest.mark.asyncio
async def test_query_hermes_memory_unavailable():
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.hermes_memory_bridge import query_hermes_memory

    adapter = _make_adapter(False)
    result = await query_hermes_memory("test", {"hermes": adapter})
    assert result["ok"] is False
    assert "HA subprocess not available" in result["error"]


@pytest.mark.asyncio
async def test_query_hermes_memory_success():
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.hermes_memory_bridge import query_hermes_memory

    adapter = _make_adapter(True, {"ok": True, "results": [{"content": "fact"}]})
    result = await query_hermes_memory("test", {"hermes": adapter})
    assert result["ok"] is True
    assert result["results"][0]["content"] == "fact"
    assert adapter._proxy_called is True


@pytest.mark.asyncio
async def test_query_hermes_memory_no_adapter():
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.hermes_memory_bridge import query_hermes_memory

    result = await query_hermes_memory("test", {})
    assert result["ok"] is False
    assert "Hermes adapter not found" in result["error"]
