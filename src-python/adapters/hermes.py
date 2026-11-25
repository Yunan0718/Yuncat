"""adapters/hermes.py —— Hermes Agent 外部连接器（对齐 hermes-agent 0.16 真实 API 面）

Hermes 由用户自行启动，Yuncat 作为客户端连接。**真实启动方式**（已用安装包内的
`hermes --help` / `gateway --help` 核实，本文件旧注释里的 `hermes api-server` 命令
并不存在）：

    set API_SERVER_ENABLED=true
    set API_SERVER_KEY=<随机密钥>
    set API_SERVER_PORT=8642          # 可选，默认就是 8642
    hermes gateway run

Hermes 的 API server 是 gateway 的一个 platform（gateway/platforms/api_server.py），
真实端点（0.16.0）：

    GET  /health                    健康检查（免鉴权）
    GET  /health/detailed           详细状态（免鉴权）
    GET  /v1/models                 模型名（默认 hermes-agent 或 profile 名）
    GET  /v1/capabilities           机器可读的能力清单（features + endpoints）
    GET  /v1/skills                 已安装技能
    GET  /v1/toolsets               **工具集**（含每个工具集展开后的工具名）
    POST /v1/chat/completions       OpenAI 兼容；支持 SSE
    POST /v1/runs                   结构化运行 + 审批/停止
    /api/sessions/...               会话资源

鉴权：API_SERVER_KEY **是必填的**（服务端拒绝无 key 启动），所有 /v1 与 /api 端点
要求 `Authorization: Bearer <key>`。旧适配器从不发这个头，因此即使网关起来了也会
全部 401——这是“Hermes 连不上/不能用”的主因之一。

会话连续性：聊天请求可带 `X-Hermes-Session-Id` 复用服务端会话历史；不带时 Hermes
按首条用户消息指纹派生。Yuncat 用自身 session_id 作为该头，保证多轮连续。

工具执行：Hermes 在**服务端**执行工具（capabilities.runtime.tool_execution = "server"），
没有“外部按名字执行单个工具”的端点（旧代码里的 /tools/execute 是虚构的）。因此
supports_tools=False；外部请求单个工具应走 delegate_to_agent 让 Hermes 自己调用。
工具**清单**改为从 /v1/toolsets 读取。

保留的向后兼容属性：`_ha_available` / `_ha_process` / `set_tool_service` /
`set_model_config`（由基类或本类提供）。
"""
from __future__ import annotations

import logging

import aiohttp

from adapters.base import AgentCapability
from adapters.ext_http import HttpGatewayAgentAdapter
from adapters.registry import register_adapter
from config import GATEWAY_TOOLS_TIMEOUT_SEC, HERMES_AGENT_ID

_log = logging.getLogger("yuncat.adapter.hermes")

# hermes-agent 的 api_server 默认端口（gateway/platforms/api_server.py: DEFAULT_PORT）
HERMES_DEFAULT_PORT = 8642

# 真实能力端点（旧代码误用 /tools/schema、/tools/execute）
_PATH_CAPABILITIES = "/v1/capabilities"
_PATH_MODELS = "/v1/models"
_PATH_TOOLSETS = "/v1/toolsets"
_PATH_SKILLS = "/v1/skills"


@register_adapter("hermes", {})
class HermesAdapter(HttpGatewayAgentAdapter):
    cli_name = "hermes"
    default_port = HERMES_DEFAULT_PORT
    port_env = "HERMES_API_PORT"
    health_path = "/health"

    # 外部模式：不再有内置 HA 子进程
    _ha_available: bool = False

    def __init__(self, agent_id: str = HERMES_AGENT_ID,
                 port_hint: int = HERMES_DEFAULT_PORT):
        super().__init__(agent_id=agent_id, port_hint=port_hint)
        self.display_name = "Hermes Agent"
        self._capabilities_cache: dict | None = None
        self._default_model_cache: str = ""

    @property
    def _ha_process(self):
        """向后兼容 agent_tool_loop.py：代理到基类 self.process（外部模式为 None）。"""
        return self.process

    @_ha_process.setter
    def _ha_process(self, value):
        self.process = value

    # ------------------------------------------------------------------
    # 鉴权 / 会话
    # ------------------------------------------------------------------
    async def _api_key(self) -> str:
        """解析 API_SERVER_KEY：环境变量优先，其次 Yuncat 加密设置。"""
        import os

        for env_name in ("HERMES_API_KEY", "API_SERVER_KEY"):
            value = (os.environ.get(env_name) or "").strip()
            if value:
                return value
        settings = getattr(self._model_config, "_settings", None)
        if settings is not None:
            try:
                value = await settings.get_decrypted("hermes_api_key")
                if value:
                    return str(value).strip()
            except Exception:
                _log.debug("读取 hermes_api_key 设置失败", exc_info=True)
        return ""

    async def _extra_headers(self, context: dict | None = None) -> dict:
        headers: dict[str, str] = {}
        key = await self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        # 会话连续性：Yuncat 自己的 session_id 直接作为 Hermes 的会话 id。
        session_id = str((context or {}).get("session_id") or "").strip()
        if session_id:
            headers["X-Hermes-Session-Id"] = session_id[:256]
            # 长期记忆按稳定 channel 作用域；与 session id 分开，符合 Hermes 语义。
            headers["X-Hermes-Session-Key"] = f"yuncat:{session_id[:200]}"
        return headers

    # ------------------------------------------------------------------
    # 通用请求
    # ------------------------------------------------------------------
    async def _request_json(self, method: str, path: str, *,
                            timeout: float = GATEWAY_TOOLS_TIMEOUT_SEC,
                            payload: dict | None = None):
        """返回 (data, error)。错误信息面向用户、包含真实原因。"""
        if not await self._gateway_reachable():
            return None, (
                "Hermes 网关不可达。请先启动："
                "`set API_SERVER_ENABLED=true` + `set API_SERVER_KEY=<随机密钥>` + "
                f"`hermes gateway run`（默认端口 {HERMES_DEFAULT_PORT}）"
            )
        await self._ensure_session()
        try:
            async with self._session.request(  # type: ignore[union-attr]
                method, f"{self._gateway_url()}{path}",
                json=payload, headers=await self._extra_headers(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 401:
                    return None, (
                        "Hermes 拒绝了请求（401）：API_SERVER_KEY 不匹配。"
                        "请在 Hermes 启动环境里设置 API_SERVER_KEY，并在 Yuncat 里填同一个值"
                        "（环境变量 HERMES_API_KEY，或设置项 hermes_api_key）。"
                    )
                if resp.status != 200:
                    text = ""
                    try:
                        text = (await resp.text())[:200]
                    except Exception:
                        _log.debug("读取 Hermes 错误响应体失败", exc_info=True)
                    return None, f"Hermes {path} 返回 HTTP {resp.status}{('：' + text) if text else ''}"
                return await resp.json(), ""
        except Exception as e:
            _log.debug("Hermes %s %s failed", method, path, exc_info=True)
            return None, str(e)

    # ------------------------------------------------------------------
    # 能力 / 模型 / 工具发现
    # ------------------------------------------------------------------
    async def capabilities_info(self) -> dict:
        """GET /v1/capabilities——运行中 Hermes 的真实能力清单（带缓存）。"""
        if self._capabilities_cache is not None:
            return self._capabilities_cache
        data, error = await self._request_json("GET", _PATH_CAPABILITIES)
        if error or not isinstance(data, dict):
            _log.debug("Hermes capabilities unavailable: %s", error)
            return {}
        self._capabilities_cache = data
        return data

    async def list_models(self) -> list[str]:
        data, error = await self._request_json("GET", _PATH_MODELS)
        if error or not isinstance(data, dict):
            return []
        return [
            str(item.get("id"))
            for item in (data.get("data") or [])
            if isinstance(item, dict) and item.get("id")
        ]

    async def _fallback_model(self) -> str | None:
        """外部 Agent 自带模型：Yuncat 没配模型时用 Hermes 自己的模型名。"""
        if not self._default_model_cache:
            models = await self.list_models()
            self._default_model_cache = models[0] if models else "hermes-agent"
        return self._default_model_cache

    async def list_available_tools(self) -> list[dict]:
        """GET /v1/toolsets——把已启用的工具集展开成工具清单。

        返回结构与 Yuncat 前端 ToolInfo 对齐：{name, description, category}。
        只列 enabled 的工具集（那是 Hermes 实际会加载的工具）。
        """
        data, error = await self._request_json("GET", _PATH_TOOLSETS)
        if error or not isinstance(data, dict):
            if error:
                _log.debug("Hermes toolsets unavailable: %s", error)
            return []
        tools: list[dict] = []
        for toolset in (data.get("data") or []):
            if not isinstance(toolset, dict) or not toolset.get("enabled"):
                continue
            category = toolset.get("label") or toolset.get("name") or ""
            description = toolset.get("description") or ""
            for name in (toolset.get("tools") or []):
                if not name:
                    continue
                tools.append({
                    "name": str(name),
                    "description": description,
                    "category": str(category),
                })
        return tools

    async def list_skills(self) -> list[dict]:
        """GET /v1/skills——Hermes 已安装技能（只读）。"""
        data, error = await self._request_json("GET", _PATH_SKILLS)
        if error or not isinstance(data, dict):
            return []
        return [s for s in (data.get("data") or []) if isinstance(s, dict)]

    # ------------------------------------------------------------------
    # 工具执行（Hermes 不支持外部单工具执行）
    # ------------------------------------------------------------------
    async def execute_tool(self, tool_name: str, arguments: dict,
                           timeout_sec: int = 60) -> dict:
        """Hermes 在服务端执行工具，没有按名字执行单个工具的 HTTP 端点。

        旧实现 POST /tools/execute——该路径在 hermes-agent 0.16 中不存在，
        会稳定 404。这里返回明确的、可操作的错误，而不是伪装成网络故障。
        """
        return {
            "ok": False,
            "error": (
                "Hermes 在服务端自行执行工具，不提供“外部按名字调用单个工具”的端点。"
                "请改用 delegate_to_agent，让 Hermes 在对话中自行调用 "
                f"{tool_name or '目标工具'}；可用工具见 /v1/toolsets。"
            ),
            "unsupported": True,
            "hint": "delegate_to_agent",
        }

    # ------------------------------------------------------------------
    # 生命周期 / 能力
    # ------------------------------------------------------------------
    def capabilities(self) -> AgentCapability:
        return AgentCapability(
            supports_stream=True,
            supports_tools=False,      # 工具在 Hermes 服务端执行，Yuncat 不二次执行
            supports_interrupt=True,   # 断开 SSE 即中断服务端 agent
            supports_image=False,
            supports_memory=True,      # X-Hermes-Session-Id/Key 提供会话与长期记忆作用域
        )

    async def health(self) -> bool:
        """只有 gateway 可达才算健康（CLI 存在不代表 API server 在跑）。"""
        return await self._gateway_reachable()
