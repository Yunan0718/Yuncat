"""adapters/openclaw.py —— OpenClaw 外部连接器（Phase 融合重构）

**不再 vendored 子进程 / node-embed。** OpenClaw 现在作为「外部 agent」被 Yuncat 调用：
- 若用户已自行启动 OpenClaw Gateway（默认 http://127.0.0.1:18789），走 HTTP
  /v1/chat/completions 推理 + /tools/invoke 工具桥接 + /tools、/skills 发现。
- 否则回退检测 `openclaw` CLI 是否在 PATH（健康信号）。

Yuncat 不再 spawn、不注入 Provider 凭据、不做 ReAct——这些都由用户运行的 OpenClaw 自身负责。
保留的契约方法（供 capability_router / agent_tool_loop / agent_healer 使用）：
invoke / stream / health / capabilities / execute_tool / list_available_tools / refresh_skills。
"""
import logging

from config import (
    GATEWAY_TOOLS_TIMEOUT_SEC,
    ADAPTER_HEALTH_CHECK_TIMEOUT,
    OC_PORT_HINT,
    OC_INVOKE_TIMEOUT_SEC,
    OPENCLAW_AGENT_ID,
)
from adapters.base import AgentCapability
from adapters.ext_http import HttpGatewayAgentAdapter
from adapters.registry import register_adapter
import aiohttp

_log = logging.getLogger("yuncat.adapter.openclaw")


@register_adapter("openclaw", {"port_hint": OC_PORT_HINT})
class OpenClawAdapter(HttpGatewayAgentAdapter):
    cli_name = "openclaw"
    default_port = OC_PORT_HINT
    port_env = "OPENCLAW_PORT"
    health_path = "/v1/status"

    def __init__(self, agent_id: str = OPENCLAW_AGENT_ID, port_hint: int = OC_PORT_HINT):
        super().__init__(agent_id=agent_id, port_hint=port_hint)
        self.display_name = "OpenClaw"

    def capabilities(self) -> AgentCapability:
        return AgentCapability(
            supports_stream=True, supports_tools=True,
            supports_browser=True, supports_image=True,
        )

    async def execute_tool(self, tool_name: str, arguments: dict,
                           timeout_sec: int = OC_INVOKE_TIMEOUT_SEC) -> dict:
        """经 OpenClaw Gateway POST /tools/invoke 调用 ClawHub 工具。"""
        if not await self._gateway_reachable():
            return {"ok": False, "error": "OpenClaw gateway 不可达（请先启动 openclaw gateway）"}
        await self._ensure_session()
        try:
            async with self._session.post(  # type: ignore[union-attr]
                f"{self._gateway_url()}/tools/invoke",
                json={"tool": tool_name, "args": arguments},
                timeout=aiohttp.ClientTimeout(total=timeout_sec),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {"ok": True, "success": True,
                            "result": data.get("result", data),
                            "data": data.get("result", data)}
                text = await resp.text()
                return {"ok": False, "error": f"Gateway returned {resp.status}: {text[:200]}"}
        except Exception as e:
            _log.exception("OC tools/invoke failed: %s", tool_name)
            return {"ok": False, "error": str(e)}

    async def list_available_tools(self) -> list[dict]:
        """动态发现 OpenClaw 可用工具（gateway /tools）。不可达时返回空。"""
        if not await self._gateway_reachable():
            return []
        await self._ensure_session()
        try:
            async with self._session.get(  # type: ignore[union-attr]
                f"{self._gateway_url()}/tools",
                timeout=aiohttp.ClientTimeout(total=ADAPTER_HEALTH_CHECK_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("tools", data.get("results", []))
        except Exception as e:
            _log.debug("OC live tool discovery failed: %s", e)
        return []

    async def refresh_skills(self) -> list[dict]:
        """从 OpenClaw gateway /skills 动态拉取最新 Skills 列表。不可达时返回空。"""
        if not await self._gateway_reachable():
            return []
        await self._ensure_session()
        try:
            async with self._session.get(  # type: ignore[union-attr]
                f"{self._gateway_url()}/skills",
                timeout=aiohttp.ClientTimeout(total=GATEWAY_TOOLS_TIMEOUT_SEC),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("skills", data.get("results", []))
        except Exception:
            _log.debug("刷新 OpenClaw Skills 失败", exc_info=True)
        return []
