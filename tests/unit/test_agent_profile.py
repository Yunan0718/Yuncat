"""tests/unit/test_agent_profile.py —— Phase 51-F: AgentProfile"""
import pytest


def _make_state(mcp=None, skills=None):
    class FakeRegistry:
        def list_installed(self):
            return skills or []

    class FakeState:
        def __init__(self):
            self.adapters = {}
            self.db = "fake_db"
            self.skill_registry = FakeRegistry() if skills is not None else None

    return FakeState()


@pytest.mark.asyncio
async def test_load_agent_profile_hermes():
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.agent_profile import load_agent_profile

    state = _make_state(skills=[])
    profile = await load_agent_profile("hermes", state)
    assert profile.agent_id == "hermes"
    assert "reasoning" in profile.capabilities
    assert "memory" in profile.capabilities
    assert "HA memory API" in profile.memory_providers


@pytest.mark.asyncio
async def test_load_agent_profile_openclaw():
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.agent_profile import load_agent_profile

    state = _make_state(skills=[])
    profile = await load_agent_profile("openclaw", state)
    assert profile.agent_id == "openclaw"
    assert "vision" in profile.capabilities
    assert "messaging" in profile.capabilities
