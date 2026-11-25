"""adapters/ext_http.py —— 外部 HTTP Gateway 连接器基类

用于 OpenClaw / Hermes 这类「由用户自行启动 API server / gateway」的外部 agent。

Phase 融合重构核心变化：
- **不再 spawn 任何 vendored 子进程**（删除 node-embed / HA subprocess 拉起逻辑）。
- Yuncat 仅作为客户端连接用户自启的 gateway：
    - 默认 http://127.0.0.1:{port_hint}（可用环境变量覆盖，见 port_env）
    - health() 优先探测 gateway，不可达时回退检测 CLI 是否在 PATH
- invoke / stream 走 OpenAI 兼容 /v1/chat/completions
- 外部 agent 使用它们自己的密钥与工具；Yuncat 不注入凭据、不做 ReAct。

子类（OpenClawAdapter / HermesAdapter）只需补充网关专属路径（端口、CLI 名、
工具桥接 /tools/invoke 等）。
"""
import asyncio
import logging
import os
import shutil
from typing import Any, AsyncIterator

import aiohttp
from config import (
    ADAPTER_HEALTH_CHECK_TIMEOUT,
    ADAPTER_INVOKE_TIMEOUT,
    ADAPTER_STREAM_IDLE_TIMEOUT,
)
from adapters.base import AgentCapability, AgentInvoker, BaseProcessAdapter, build_openai_messages
from adapters.sse_utils import iter_sse_named_events
from services.llm_usage import normalize_usage

_log = logging.getLogger("yuncat.adapter.external_http")


def _map_openai_delta(data: dict) -> list[dict]:
    """把一条 OpenAI 兼容 SSE chunk 映射成 Yuncat 的流式事件列表。"""
    choice = (data.get("choices") or [{}])[0]
    delta = choice.get("delta") or {}
    chunks: list[dict] = []
    if delta.get("reasoning_content"):
        chunks.append({"type": "thinking", "data": delta["reasoning_content"]})
    if delta.get("tool_calls"):
        chunks.append({"type": "tool_call", "data": delta["tool_calls"]})
    if delta.get("content"):
        chunks.append({"type": "text", "data": delta["content"]})
    if choice.get("finish_reason"):
        chunks.append({"type": "finish", "data": choice["finish_reason"]})
    return chunks


class HttpGatewayAgentAdapter(BaseProcessAdapter, AgentInvoker):
    """外部 HTTP Gateway 基类——连接用户自行启动的 agent gateway（无子进程）。"""

    is_external: bool = True
    cli_name: str = ""          # 可执行文件名（健康兜底检测）
    default_port: int = 0       # 默认网关端口
    port_env: str = ""          # 覆盖端口的环境变量名（如 OPENCLAW_PORT / HERMES_API_PORT）
    health_path: str = "/v1/status"

    def __init__(self, agent_id: str = "", port_hint: int = 0):
        # BaseProcessAdapter.__init__ 需要 cmd/agent_id/port_hint；外部连接 cmd 留空。
        super().__init__([], agent_id or self.cli_name, port_hint or self.default_port)
        # 2026-10-18：把环境变量覆盖解析进 _port，供 list_agents 的 TCP 探活使用。
        # 旧实现只在 _gateway_url() 里临时读 env，导致「用户设了 HERMES_API_PORT、
        # 探活却仍打默认端口」→ 界面永远显示未检测到。
        self._port = self._effective_port(port_hint or self.default_port)
        self._session: aiohttp.ClientSession | None = None
        self.last_usage: dict[str, int] = {}
        self.last_session_id: str = ""
        self._model_config = None
        if not self.display_name:
            self.display_name = (agent_id or self.cli_name or "external").capitalize()

    # ---- 配置注入（兼容 set_model_config 调用方）----
    def set_model_config(self, model_config) -> None:
        self._model_config = model_config

    def set_tool_service(self, tool_service) -> None:
        self._tool_service = tool_service

    # ---- 网关 URL ----
    def _effective_port(self, fallback: int | None = None) -> int:
        """解析实际端口：环境变量覆盖 > 传入值 > 子类默认值。"""
        port = fallback if fallback is not None else (self._port or self.default_port)
        if self.port_env and os.environ.get(self.port_env):
            try:
                port = int(os.environ[self.port_env])
            except ValueError:
                _log.warning("%s 不是合法端口：%s", self.port_env, os.environ[self.port_env])
        return port or 0

    def _gateway_url(self) -> str:
        self._port = self._effective_port()
        return f"http://127.0.0.1:{self._port}"

    async def _extra_headers(self, context: dict | None = None) -> dict:
        """附加请求头钩子（鉴权 / 会话连续性）——子类覆盖。"""
        return {}

    async def _fallback_model(self) -> str | None:
        """未配置模型时的兜底模型名——子类覆盖（外部 Agent 自带模型）。"""
        return None

    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def _gateway_reachable(self) -> bool:
        """探测 gateway 健康。子类可覆盖 health_path。"""
        if not self._port:
            return False
        try:
            await self._ensure_session()
            async with self._session.get(
                f"{self._gateway_url()}{self.health_path}",
                timeout=aiohttp.ClientTimeout(total=ADAPTER_HEALTH_CHECK_TIMEOUT),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    # ---- 生命周期（无长驻子进程）----
    async def start(self) -> None:
        async with self._start_lock:
            self._stopping = False
            self._ready.set()  # 外部连接：适配器自身就绪，可用性由 health() 决定
            _log.info("External gateway adapter '%s' ready (cli=%s, gateway=%s)",
                      self.agent_id, self.cli_name, self._gateway_url())

    async def stop(self) -> None:
        self._stopping = True
        # 2026-10-07 修复：同 BaseProcessAdapter.stop——停网关后必须退出就绪态，
        # 否则「停止」后该 Agent 仍被判为 running。
        self._ready.clear()
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def health(self) -> bool:
        if await self._gateway_reachable():
            return True
        return bool(self.cli_name) and shutil.which(self.cli_name) is not None

    def capabilities(self) -> AgentCapability:
        return AgentCapability(
            supports_stream=True, supports_tools=False,
            supports_browser=False, supports_interrupt=False,
            supports_image=False, supports_memory=False,
        )

    # ---- 调用 ----
    async def _resolve_model(self, context: dict | None) -> str | None:
        cfg = (context or {}).get("model_config") or {}
        model = cfg.get("model") or (context or {}).get("model")
        return model or await self._fallback_model()

    async def invoke(self, prompt: str, context: dict | None = None) -> dict[str, Any]:
        if not await self._gateway_reachable():
            return {"error": f"外部 agent '{self.agent_id}' 网关不可达（请先启动其 gateway / API server）"}
        model = await self._resolve_model(context)
        if not model:
            return {"error": "未配置模型——请在 设置 > 模型配置 中选择模型。"}
        await self._ensure_session()
        messages = build_openai_messages(prompt, context)
        payload = {"model": model, "messages": messages, "stream": False}
        headers = await self._extra_headers(context)
        try:
            async with self._session.post(  # type: ignore[union-attr]
                f"{self._gateway_url()}/v1/chat/completions",
                json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=ADAPTER_INVOKE_TIMEOUT),
            ) as resp:
                self._capture_session_header(resp)
                data = await resp.json()
                if resp.status != 200:
                    # 不能把上游错误吞成「空内容」——用户需要看到真实原因
                    detail = ""
                    if isinstance(data, dict):
                        err = data.get("error")
                        detail = err.get("message") if isinstance(err, dict) else str(err or "")
                    return {"error": f"外部 agent 返回 HTTP {resp.status}: {detail[:300]}"}
                msg = data.get("choices", [{}])[0].get("message", {})
                content = msg.get("content", "") or msg.get("reasoning_content", "")
                if not content:
                    return {"error": "外部 agent 返回空内容"}
                return {"role": "assistant", "content": content}
        except Exception as e:
            _log.exception("%s invoke failed", self.agent_id)
            from services.llm_errors import classify_llm_error
            return {"error": str(e), "error_code": classify_llm_error(str(e))}

    async def stream(self, prompt: str, context: dict | None = None) -> AsyncIterator[dict]:
        if not await self._gateway_reachable():
            yield {"type": "stream_error",
                   "data": f"外部 agent '{self.agent_id}' 网关不可达（请先启动其 gateway / API server）"}
            return
        model = await self._resolve_model(context)
        if not model:
            yield {"type": "stream_error", "data": "未配置模型——请在 设置 > 模型配置 中选择模型。"}
            return
        await self._ensure_session()
        messages = build_openai_messages(prompt, context)
        payload = {"model": model, "messages": messages, "stream": True}
        self.last_usage = {}
        headers = await self._extra_headers(context)
        try:
            async with self._session.post(  # type: ignore[union-attr]
                f"{self._gateway_url()}/v1/chat/completions",
                json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=None, sock_connect=10, sock_read=ADAPTER_STREAM_IDLE_TIMEOUT),
            ) as resp:
                self._capture_session_header(resp)
                status_error = await self._stream_status_error(resp)
                if status_error:
                    yield status_error
                    return
                async for event_name, data in iter_sse_named_events(resp):
                    if data is None:  # [DONE]
                        return
                    if event_name:
                        # 外部 Agent 的具名事件（如 hermes.tool.progress）——交给进度回调，
                        # 不能落进下面的 OpenAI delta 解析（否则会被当空 chunk 丢弃）。
                        sink = (context or {}).get("_tool_progress_sink")
                        if sink is not None:
                            try:
                                await sink({"event": event_name, **(data or {})})
                            except Exception:
                                _log.debug("tool progress sink failed", exc_info=True)
                        continue
                    for chunk in _map_openai_delta(data):
                        yield chunk
                    if "usage" in data:
                        self.last_usage = normalize_usage(data.get("usage"))
        except Exception as e:
            _log.exception("%s stream failed", self.agent_id)
            from services.llm_errors import classify_llm_error
            yield {"type": "stream_error", "data": str(e),
                   "error_code": classify_llm_error(str(e))}

    async def _stream_status_error(self, resp) -> dict | None:
        """非 200 时读取 body，生成可读的 stream_error（读失败也要留痕）。"""
        if resp.status == 200:
            return None
        try:
            body = (await resp.text())[:300]
        except Exception:
            body = ""
            _log.debug("读取网关错误响应体失败", exc_info=True)
        return {"type": "stream_error",
                "data": f"外部 agent 返回 HTTP {resp.status}: {body}"}

    def _capture_session_header(self, resp) -> None:
        """记录服务端返回的会话 id（Hermes: X-Hermes-Session-Id）。"""
        try:
            sid = resp.headers.get("X-Hermes-Session-Id")
            if sid:
                self.last_session_id = sid
        except Exception:
            _log.debug("读取 X-Hermes-Session-Id 失败", exc_info=True)

    async def cancel(self, task_id: str | None = None) -> None:
        """取消当前调用。

        外部网关没有统一的取消端点；关闭 aiohttp 会话会让 SSE 连接断开，
        服务端（Hermes）据此中断正在跑的 agent（见其 _write_sse_chat_completion
        的 ConnectionReset 分支）。子类可覆盖为更精确的 stop 端点。
        """
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
