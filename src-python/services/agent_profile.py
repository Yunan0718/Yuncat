"""services/agent_profile.py —— Phase 51-F: 统一 AgentProfile

聚合 Agent 的能力、MCP 服务器、Skill、记忆来源等元数据，
供前端 Settings / Agent 管理统一展示。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config import HERMES_AGENT_ID, OPENCLAW_AGENT_ID


@dataclass
class AgentProfile:
    """统一 Agent 画像。"""

    agent_id: str
    name: str
    capabilities: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    memory_providers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "capabilities": self.capabilities,
            "mcp_servers": self.mcp_servers,
            "skills": self.skills,
            "memory_providers": self.memory_providers,
        }


async def load_agent_profile(agent_id: str, app_state) -> AgentProfile:
    """根据 agent_id 加载当前可用的 profile。

    目前支持 hermes / openclaw，未来可扩展为从配置中心或 Agent 自描述 API 加载。
    """
    adapters = getattr(app_state, "adapters", {})
    adapter = adapters.get(agent_id)
    status = "running" if adapter and getattr(adapter, "_ready", None) and adapter._ready.is_set() else "stopped"

    # 基础能力标签
    capability_map = {
        "hermes": ["reasoning", "coding", "tool_use", "memory"],
        "openclaw": ["vision", "web_search", "messaging", "pdf", "browser"],
    }

    profile = AgentProfile(
        agent_id=agent_id,
        name=agent_id.capitalize(),
        capabilities=capability_map.get(agent_id, []),
    )

    # MCP 服务器：全局列表
    try:
        from repositories import list_mcp_servers as _list_mcp
        db = getattr(app_state, "db", None)
        if db:
            servers = await _list_mcp(db)
            profile.mcp_servers = [s["name"] for s in servers if s.get("enabled")]
    except Exception:
        pass

    # Skill：全局已启用列表
    try:
        skill_registry = getattr(app_state, "skill_registry", None)
        if skill_registry:
            profile.skills = [s.id for s in skill_registry.list_installed() if getattr(s, "enabled", True)]
    except Exception:
        pass

    # 记忆来源
    if agent_id == HERMES_AGENT_ID:
        profile.memory_providers = ["MEMORY.md", "SQLite FTS5", "HA memory API"]
    elif agent_id == OPENCLAW_AGENT_ID:
        profile.memory_providers = ["OpenClaw native memory"]

    # 运行状态也作为 profile 的一部分
    profile.capabilities.append(f"status:{status}")
    return profile
