"""adapters/platform_bridge.py —— AgentPlatformBridge (Adapter v2 · Phase 26)

Agent Platform Bridge 是 BaseProcessAdapter 的**子类**（不替换），新增 3 个能力发现方法:
  - probe_capabilities(): 静态 manifest + 可选动态探测（优先读取 YAML，回退到 CLI --list-tools）
  - list_tools(): 返回该 Agent 可暴露给 ToolService 的工具列表
  - execute_tool(): 转发工具调用到 Agent（通过 HTTP/stdio）

与 BaseProcessAdapter 的关系:
  OpenAiCompatibleAdapter → BaseProcessAdapter（不变——现有文本对话能力）
  AgentPlatformBridge → BaseProcessAdapter（新增——工具发现+执行能力）
  HermesAdapter / OpenClawAdapter 同时继承两者的类型接口（通过 mixin）

静态 Tool Manifest 格式 (YAML):
  每个 Adapter 可选地提供 config/tools/{agent_id}.yaml，声明本 Agent 暴露的工具。
  如果 YAML 不存在，probe_capabilities 返回空（dynamic probe 作为可选增强）。
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger("yuncat.platform")


@dataclass
class PlatformTool:
    """Platform-level 工具描述符——跨 Agent 统一格式"""
    name: str
    description: str = ""
    parameters: dict = field(default_factory=dict)
    agent_id: str = ""
    is_available: bool = True  # probe 后发现不可用 → False
    category: str = "general"  # file | shell | browser | search | code | vision | comms


class AgentPlatformBridge:
    """Adapter v2 —— Agent 工具发现与执行能力。

    **设计注释（BA.2 冲突 7）**: 这是 BaseProcessAdapter 的**新增子类能力**，
    不是替换。旧 adapter 继续用 BaseProcessAdapter；新 adapter 可额外使用此 mixin。

    用法:
        class MyAdapter(BaseProcessAdapter, AgentPlatformBridge):
            pass  # 同时拥有进程管理 + 工具发现能力
    """

    def __init__(self, manifest_dir: str | None = None):
        self._manifest_dir = manifest_dir or os.path.join(
            os.path.dirname(__file__), "..", "config", "tools",
        )
        self._tools_cache: dict[str, list[PlatformTool]] | None = None

    # ---- 公开 API ----

    async def probe_capabilities(self, agent_id: str) -> dict:
        """探测 Agent 能力——静态 manifest 优先 + 动态回退。

        返回:
          {"agent_id": "...", "total_tools": N, "categories": [...],
           "tools": [PlatformTool, ...], "source": "manifest"|"dynamic"|"none"}
        """
        # Step 1: 优先读静态 manifest
        tools = await self.list_tools(agent_id)
        if tools:
            cats = list(set(t.category for t in tools))
            return {
                "agent_id": agent_id,
                "total_tools": len(tools),
                "categories": cats,
                "tools": [self._tool_to_dict(t) for t in tools],
                "source": "manifest",
            }

        # Step 2: 回退——动态 probe（尝试 CLI --list-tools）
        dynamic_tools = await self._dynamic_probe(agent_id)
        if dynamic_tools:
            return {
                "agent_id": agent_id,
                "total_tools": len(dynamic_tools),
                "categories": list(set(t.category for t in dynamic_tools)),
                "tools": [self._tool_to_dict(t) for t in dynamic_tools],
                "source": "dynamic",
            }

        return {
            "agent_id": agent_id,
            "total_tools": 0,
            "categories": [],
            "tools": [],
            "source": "none",
        }

    async def list_tools(self, agent_id: str) -> list[PlatformTool]:
        """从静态 manifest 加载 Agent 暴露的工具列表。

        Manifest 路径: config/tools/{agent_id}.yaml
        """
        if self._tools_cache is None:
            self._tools_cache = {}
        if agent_id in self._tools_cache:
            return self._tools_cache[agent_id]

        manifest_path = os.path.join(self._manifest_dir, f"{agent_id}.yaml")
        if not os.path.exists(manifest_path):
            return []

        try:
            import yaml
            with open(manifest_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except ImportError:
            _log.warning("PyYAML not available for manifest loading")
            return []
        except Exception:
            _log.exception("Failed to load manifest for %s", agent_id)
            return []

        tools = []
        for item in data.get("tools", []):
            tools.append(PlatformTool(
                name=item.get("name", ""),
                description=item.get("description", ""),
                parameters=item.get("parameters", {}),
                agent_id=agent_id,
                category=item.get("category", "general"),
            ))
        self._tools_cache[agent_id] = tools
        return tools

    async def execute_tool(self, agent_id: str, tool_name: str, arguments: dict) -> dict:
        """转发工具调用到 Agent——子类重写此方法实现具体转发逻辑。

        基类实现返回 not_implemented——子类按协议接入自己的 HTTP/stdio 工具调用。
        """
        return {
            "ok": False,
            "error": f"Tool execution not implemented for {agent_id}. "
                     f"Phase 31: use adapter.execute_tool() directly for OpenClaw.",
            "tool_name": tool_name,
        }

    # ---- 内部 ----

    async def _dynamic_probe(self, agent_id: str) -> list[PlatformTool]:
        """动态探测 Agent 工具——尝试 CLI --list-tools 命令。
        基类实现返回空——子类可重写。
        """
        return []

    @staticmethod
    def _tool_to_dict(t: PlatformTool) -> dict:
        return {
            "name": t.name, "description": t.description,
            "parameters": t.parameters, "agent_id": t.agent_id,
            "category": t.category, "is_available": t.is_available,
        }
