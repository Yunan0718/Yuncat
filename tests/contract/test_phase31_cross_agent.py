"""tests/contract/test_phase31_cross_agent.py — Phase 31 cross-agent delegation + mixed deliberation"""
import os, sys, pytest


class _FakeReadyAdapter:
    """就绪的假 adapter（_ready 已 set）。"""

    class _Ev:
        def is_set(self) -> bool:
            return True

    def __init__(self):
        self._ready = self._Ev()


class _FakeIdleAdapter:
    """未就绪的假 adapter（模拟网关未启动的外部后端）。"""

    class _Ev:
        def is_set(self) -> bool:
            return False

    def __init__(self):
        self._ready = self._Ev()


# ---- Cross-agent tool delegation ----

def test_delegate_tool_missing_agent():
    """delegate_tool to non-existent agent returns error"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    import asyncio
    from services.capability_router import delegate_tool
    result = asyncio.run(delegate_tool("hermes", "nonexistent", "test_tool", {}, {}))
    assert result["ok"] is False


def test_delegate_tool_no_execute_method():
    """delegate_tool to agent without execute_tool returns error"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    import asyncio
    from services.capability_router import delegate_tool

    class BareAdapter:
        agent_id = "bare"

    result = asyncio.run(delegate_tool("hermes", "bare", "test", {},
                                        {"bare": BareAdapter()}))
    assert result["ok"] is False
    assert "not support" in result["error"].lower()


def test_delegate_tool_prompt_has_mapping():
    """delegate prompt includes agent capabilities"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.capability_router import get_delegate_tool_prompt, AgentProfile
    profiles = [
        AgentProfile(agent_id="openclaw", name="OpenClaw",
                     strengths=["vision analysis", "screenshot OCR", "messaging"],
                     tools=[{"name": "openclaw_vision"}], supports_image=True),
        AgentProfile(agent_id="hermes", name="Hermes",
                     strengths=["shell exec", "code exec", "browser"],
                     tools=[], supports_shell=True),
    ]
    prompt = get_delegate_tool_prompt(profiles)
    assert "OpenClaw" in prompt
    assert "Hermes" in prompt
    assert "delegate" in prompt.lower()
    assert "openclaw_vision" in prompt


def test_delegate_tool_prompt_single_agent():
    """single agent → empty prompt"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.capability_router import get_delegate_tool_prompt, AgentProfile
    profiles = [AgentProfile(agent_id="hermes", name="Hermes")]
    assert get_delegate_tool_prompt(profiles) == ""


# ---- Role-to-agent mapping ----

def test_route_role_to_agent():
    """vision roles → OpenClaw（仅在网关就绪时）, shell/devops roles → 原生 yuncat"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.capability_router import route_role_to_agent
    adapters = {"openclaw": _FakeReadyAdapter(), "yuncat": _FakeReadyAdapter()}

    assert route_role_to_agent("ux", adapters) == "openclaw"
    assert route_role_to_agent("frontend", adapters) == "openclaw"
    assert route_role_to_agent("a11y-architect", adapters) == "openclaw"
    assert route_role_to_agent("devops", adapters) == "yuncat"
    assert route_role_to_agent("tester", adapters) == "yuncat"
    assert route_role_to_agent("build-error-resolver", adapters) == "yuncat"


def test_route_role_external_not_ready_falls_back():
    """2026-09-10: 外部后端未就绪（网关未启动）时，视觉角色回落原生 yuncat——
    否则讨论角色会被派给没启动的 openclaw 网关直接失败。"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.capability_router import route_role_to_agent
    adapters = {"openclaw": _FakeIdleAdapter(), "yuncat": _FakeReadyAdapter()}

    assert route_role_to_agent("ux", adapters) == "yuncat"
    assert route_role_to_agent("a11y-architect", adapters) == "yuncat"


def test_route_role_unknown():
    """unknown role → default"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.capability_router import route_role_to_agent
    adapters = {"openclaw": _FakeReadyAdapter(), "yuncat": _FakeReadyAdapter()}
    assert route_role_to_agent("nonexistent-role", adapters, "yuncat") == "yuncat"


def test_route_role_mapped_unavailable():
    """mapped agent not running → fallback to default"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.capability_router import route_role_to_agent
    adapters = {"hermes": object()}  # OpenClaw not running
    assert route_role_to_agent("frontend", adapters, "hermes") == "hermes"


# ---- Mixed-agent deliberation ----

def test_deliberation_service_has_role_routing():
    """run_deliberation uses per-role agent mapping"""
    ds = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python",
                     "services", "deliberation", "deliberation_discussion.py"),
        encoding="utf-8",
    ).read()
    assert "route_role_to_agent" in ds, "deliberation_discussion should import route_role_to_agent"
    assert "role_agent_map" in ds, "deliberation_discussion should use per-role agent map"


def test_role_agent_map_coverage():
    """all roles have valid agent mappings"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.capability_router import ROLE_AGENT_MAP
    valid_agents = {"yuncat", "openclaw"}
    for role_id, agent in ROLE_AGENT_MAP.items():
        assert agent in valid_agents, f"Role '{role_id}' maps to unknown agent '{agent}'"


# ---- AgentService Commander prompt injection ----

def test_agent_service_has_cross_agent_prompt():
    """Agent context injects cross-agent delegation into Commander prompt"""
    ag = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python",
                     "services", "agent", "agent_context.py"),
        encoding="utf-8",
    ).read()
    assert "get_delegate_tool_prompt" in ag, "agent_context should inject delegate_tool_prompt"
    assert "get_delegation_context" in ag, "agent_context should inject delegation_context"


# ---- Full pipeline: role routing + delegate tool ----

def test_merge_all_tools_cross_agent():
    """merge_all_tools includes agent_id tags"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.capability_router import merge_all_tools, AgentProfile
    profiles = [
        AgentProfile(agent_id="openclaw", name="OpenClaw",
                     tools=[{"name": "oc_vision", "description": "vision", "category": "vision"}]),
        AgentProfile(agent_id="hermes", name="Hermes",
                     tools=[{"name": "hm_shell", "description": "shell", "category": "shell"}]),
    ]
    tools = merge_all_tools(profiles)
    assert len(tools) == 2
    oc_tools = [t for t in tools if t["agent_id"] == "openclaw"]
    hm_tools = [t for t in tools if t["agent_id"] == "hermes"]
    assert len(oc_tools) == 1
    assert oc_tools[0]["name"] == "oc_vision"
    assert len(hm_tools) == 1
