"""adapters/native.py —— Yuncat 原生 Agent 适配器（进程内，无子进程）

这是 Yuncat 自身的强 agent 后端：不 spawn 任何外部子进程，而是**直接调用用户
在 ModelConfigService 中配置的 Provider API**（OpenAI 兼容 /chat/completions）。

它复用已有的：
- `build_openai_messages`（消息构建，含历史截断 / 图片附件 / token 预算）
- `iter_sse_events`（SSE 流解析）
- `services/agent/agent_tool_loop.py` 的 ReAct 循环 + `ToolService`（工具执行）
- `MemoryTree`（记忆）——由编排层通过 context 注入
- `delegate_to_agent`（跨 agent 委托）——由编排层通过 context["tools"] 注入

即：native 只负责"LLM 推理"这一环，工具执行 / 记忆 / 委托仍走 Yuncat 既有链路。
"""
import asyncio
import logging
from typing import Any, AsyncIterator

import aiohttp

from config import (
    ADAPTER_HEALTH_CHECK_TIMEOUT,
    ADAPTER_INVOKE_TIMEOUT,
    ADAPTER_STREAM_IDLE_TIMEOUT,
    YUNCAT_AGENT_ID,
)


from adapters.base import (
    AgentCapability,
    AgentInvoker,
    BaseProcessAdapter,
    build_openai_messages,
)
from adapters.sse_utils import iter_sse_events
from services.llm_usage import normalize_usage, stream_usage_options
from services.provider_protocol import auth_headers, chat_url, resolve_protocol
from services.reasoning import (
    is_optional_param_rejection,
    reasoning_spec,
    strip_optional_params,
    to_payload_with_disable,
)

_log = logging.getLogger("yuncat.adapter.native")


def _with_proxy_hint(message: str, cfg: dict) -> str:
    """给「需要中转的厂商」的失败信息补一句可执行建议。

    Anthropic / 百度等厂商没有 OpenAI 兼容的 /chat/completions 端点，
    直连其官方域名必然 404/401。裸状态码对用户毫无信息量，
    这里把 config/models/*.yaml 里声明的 proxy_hint 拼上（未声明则原样返回）。
    """
    hint = cfg.get("proxy_hint")
    if not hint or hint in message:
        return message
    return f"{message}\n提示：{hint}"


class YuncatNativeAdapter(BaseProcessAdapter, AgentInvoker):
    """Yuncat 原生 agent：进程内直连 Provider，无子进程。

    注册名 "yuncat"（见 @register_adapter）。discover_adapters() 会自动扫描并实例化。
    """

    def __init__(self, cmd: list[str] | None = None, agent_id: str = YUNCAT_AGENT_ID, port_hint: int = 0):
        # 不 spawn 子进程——cmd 留空，port 无意义
        super().__init__(cmd or [], agent_id, port_hint)
        self._session: aiohttp.ClientSession | None = None
        self._model_config = None  # Yuncat 统一模型配置（set_model_config 注入）
        self.display_name = "Yuncat"
        # 直连 Provider 调用计数（cancel 时据此避免误关 session）
        self._active_requests = 0
        self.last_usage: dict[str, int] = {}

    # ---------------------------------------------------------------
    # 模型配置
    # ---------------------------------------------------------------
    def set_model_config(self, model_config) -> None:
        self._model_config = model_config

    async def _get_cfg(self, context: dict | None = None) -> dict:
        """解析当前激活 Provider 的 api_key / base_url / model。

        返回含 error 键的 dict 表示配置缺失。
        """
        ctx_cfg = (context or {}).get("model_config", {})
        cfg: dict = {}
        if self._model_config is not None:
            try:
                cfg = await self._model_config.get_active_config() or {}
            except Exception:
                _log.debug("native: get_active_config failed", exc_info=True)
        api_key = ctx_cfg.get("api_key") or cfg.get("api_key", "")
        # 与 openclaw._invoke_direct 保持一致：base_url 默认含 /v1
        base_url = (ctx_cfg.get("base_url") or cfg.get("base_url")
                    or "https://api.deepseek.com/v1").rstrip("/")
        model = ctx_cfg.get("model") or cfg.get("model")
        if not model:
            return {"error": "未配置模型——请在 设置 > 模型配置 中选择模型。"}
        if not api_key:
            return {"error": await _api_key_missing_reason()}
        return {
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            # 以下四项供「非 OpenAI 兼容厂商」与「厂商自定义思考字段」使用
            # （见 services/provider_protocol.py / services/reasoning.py）——
            # 适配器本身不含任何具体厂商名，新增厂商只改 YAML。
            "provider": cfg.get("provider", ""),
            "protocol": resolve_protocol({**cfg, "base_url": base_url}),
            "reasoning_spec": reasoning_spec(cfg),
            # needs_proxy 的厂商（Anthropic/百度等）直连其官方域名必然 404/401，
            # 这里带出提示语，在失败时拼进报错，避免用户只看到裸状态码。
            "proxy_hint": cfg.get("proxy_hint", "") if cfg.get("needs_proxy") else "",
        }

    # ---------------------------------------------------------------
    # 生命周期（无子进程）
    # ---------------------------------------------------------------
    async def start(self) -> None:
        """native 不 spawn 子进程——start 仅标记就绪。"""
        async with self._start_lock:
            self._stopping = False
            self._ready.set()
            _log.info("Yuncat native agent ready (in-process, no subprocess)")

    async def stop(self) -> None:
        self._stopping = True
        # 2026-10-07 修复：native 没有子进程，`停止` 的语义是「让出就绪态」——
        # status 由 _ready 派生，不清就会「点了停止仍显示运行中」（实测）。
        self._ready.clear()
        if self._session and not self._session.closed:
            await self._session.close()

    async def health(self) -> bool:
        """native 健康 = 已配置可用的 Provider（有 model + api_key）。"""
        return not await self.config_error()

    async def config_error(self) -> str:
        """配置层不可用的原因；配置正常时返回空串。

        health() 只能回答「能不能用」，把原因丢掉之后，上层就无法区分
        「进程挂了（值得重启）」和「配置不对（重启一百次也没用）」。
        2026-09-25 实测：API Key 解不开时，native 被每 30 秒判定一次不健康并退避重启，
        用户侧只看到「发消息没反应、连接慢、又自己重启」，日志里才有真实原因。
        """
        cfg = await self._get_cfg()
        return str(cfg.get("error") or "")

    def capabilities(self) -> AgentCapability:
        return AgentCapability(
            supports_stream=True,
            supports_tools=True,
            supports_browser=False,
            supports_interrupt=True,
            supports_image=True,
            supports_memory=True,
        )

    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    def _build_messages(self, context: dict | None, prompt: str) -> list[dict]:
        return build_openai_messages(prompt, context)

    # ---------------------------------------------------------------
    # 推理：非流式
    # ---------------------------------------------------------------
    async def invoke(
        self, prompt: str, context: dict | None = None,
    ) -> dict[str, Any]:
        await self._ensure_session()
        cfg = await self._get_cfg(context)
        if "error" in cfg:
            return cfg
        messages = self._build_messages(context, prompt)
        model = cfg["model"]
        payload = {"model": model, "messages": messages, "stream": False}
        # Phase X (2026-09-10): 思考强度——按 (provider, 模型, 厂商声明) 解析映射，
        # 不再把任何单一厂商的参数写死在代码里（见 services/reasoning.py）。
        _re = (context or {}).get("reasoning_effort")
        if _re:
            payload.update(to_payload_with_disable(
                _re, model, cfg.get("provider", ""), cfg.get("reasoning_spec")))
        _tools = (context or {}).get("tools")
        if _tools:
            payload["tools"] = _tools
        if context:
            if "max_tokens" in context:
                payload["max_tokens"] = context["max_tokens"]
            if "temperature" in context:
                payload["temperature"] = context["temperature"]

        # LLM 响应缓存（仅非工具调用）
        from services.llm_cache import get_global_cache
        cache = get_global_cache()
        if not _tools:
            cached = await cache.get(model, messages)
            if cached is not None:
                return cached

        self._active_requests += 1
        try:
            url = chat_url(cfg, cfg["protocol"])
            headers = auth_headers(cfg["protocol"], cfg["api_key"])
            headers.setdefault("Content-Type", "application/json")
            timeout = aiohttp.ClientTimeout(total=ADAPTER_INVOKE_TIMEOUT)
            for attempt in (1, 2):
                async with self._session.post(url, json=payload,
                                              headers=headers, timeout=timeout) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        break
                    body = await resp.text()
                    # 通用降级：厂商不认识思考参数 → 摘掉后重试一次（对任意厂商生效，
                    # 不必在代码里预先知道谁能接受该字段）。
                    if attempt == 1 and is_optional_param_rejection(resp.status, body):
                        dropped = strip_optional_params(payload)
                        if dropped:
                            _log.warning(
                                "native invoke: %s 不接受思考参数 %s，已摘除后重试",
                                cfg.get("base_url"), dropped)
                            continue
                    from services.llm_errors import error_facts

                    facts = error_facts(resp.status, resp.headers, body)
                    facts["message"] = _with_proxy_hint(facts["message"], cfg)
                    _log.warning("native invoke HTTP %s [%s] request_id=%s retry_after_ms=%s",
                                 resp.status, facts["error_code"],
                                 facts.get("request_id"), facts.get("retry_after_ms"))
                    return {"error": facts["message"], **facts}
            message = data.get("choices", [{}])[0].get("message", {})
            if isinstance(data.get("usage"), dict):
                self.last_usage = normalize_usage(data["usage"])
            if not _tools and message and "error" not in data:
                try:
                    await cache.put(model, messages, message)
                except Exception:
                    _log.warning("native cache.put failed", exc_info=True)
            return message
        except Exception as e:
            _log.error("native invoke failed: %s", e)
            from services.llm_errors import classify_llm_error
            return {"error": _with_proxy_hint(str(e), cfg),
                    "error_code": classify_llm_error(str(e))}
        finally:
            self._active_requests -= 1

    # ---------------------------------------------------------------
    # 推理：流式（SSE）
    # ---------------------------------------------------------------
    async def stream(
        self, prompt: str, context: dict | None = None,
    ) -> "AsyncIterator[dict]":
        await self._ensure_session()
        cfg = await self._get_cfg(context)
        if "error" in cfg:
            yield {"type": "stream_error", "data": cfg["error"], "error_code": "config_missing"}
            return
        messages = self._build_messages(context, prompt)
        model = cfg["model"]
        payload = {"model": model, "messages": messages, "stream": True}
        # Phase X (2026-09-10): 思考强度——按 (provider, 模型, 厂商声明) 解析映射
        _re = (context or {}).get("reasoning_effort")
        if _re:
            payload.update(to_payload_with_disable(
                _re, model, cfg.get("provider", ""), cfg.get("reasoning_spec")))
        _tools = (context or {}).get("tools")
        if _tools:
            payload["tools"] = _tools
        # 流式必须显式要 usage，否则最后一个 chunk 里没有该对象 → token 统计恒为 0。
        # 网关不认识该参数时走上面的"摘参数重试"降级。
        payload.update(stream_usage_options())
        self.last_usage = {}
        self._active_requests += 1
        url = chat_url(cfg, cfg["protocol"])
        headers = auth_headers(cfg["protocol"], cfg["api_key"])
        headers.setdefault("Content-Type", "application/json")
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=10, sock_read=ADAPTER_STREAM_IDLE_TIMEOUT)
        try:
            for attempt in (1, 2):
                async with self._session.post(
                    url, json=payload, headers=headers, timeout=timeout,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        # 通用降级：厂商不认识思考参数 / stream_options → 摘掉后重试一次
                        if attempt == 1 and is_optional_param_rejection(resp.status, body):
                            dropped = strip_optional_params(payload)
                            if dropped:
                                _log.warning(
                                    "native stream: %s 不接受可选参数 %s，已摘除后重试",
                                    cfg.get("base_url"), dropped)
                                continue
                        from services.llm_errors import error_facts

                        facts = error_facts(resp.status, resp.headers, body)
                        facts["message"] = _with_proxy_hint(facts["message"], cfg)
                        _log.warning(
                            "native stream HTTP %s [%s] request_id=%s retry_after_ms=%s",
                            resp.status, facts["error_code"],
                            facts.get("request_id"), facts.get("retry_after_ms"))
                        yield {"type": "stream_error", "data": facts["message"],
                               "error_code": facts["error_code"], **facts}
                        return
                    async for data in iter_sse_events(resp):
                        if data is None:  # [DONE]
                            return
                        choice = data.get("choices", [{}])[0]
                        delta = choice.get("delta", {})
                        reasoning = delta.get("reasoning_content", "")
                        if reasoning:
                            yield {"type": "thinking", "data": reasoning}
                        tool_calls = delta.get("tool_calls", [])
                        if tool_calls:
                            yield {"type": "tool_call", "data": tool_calls}
                        content = delta.get("content", "")
                        if content:
                            yield {"type": "text", "data": content}
                        finish_reason = choice.get("finish_reason") or ""
                        if finish_reason:
                            yield {"type": "finish", "data": finish_reason}
                        if "usage" in data:
                            # 统一口径（OpenAI 嵌套明细 / DeepSeek 平铺）并补齐
                            # cached_tokens / reasoning_tokens —— 这些是"看不见但真实消耗"的部分
                            self.last_usage = normalize_usage(data.get("usage"))
                    return
        except Exception as e:
            _log.error("native stream failed: %s", e)
            from services.llm_errors import classify_llm_error
            yield {"type": "stream_error", "data": _with_proxy_hint(str(e), cfg),
                   "error_code": classify_llm_error(str(e))}
        finally:
            self._active_requests -= 1

    async def cancel(self, task_id: str | None = None) -> None:
        if self._active_requests > 0:
            _log.debug("native cancel skipped: %d active request(s)", self._active_requests)
            return
        if self._session and not self._session.closed:
            await self._session.close()


# 注册到适配器发现机制——discover_adapters() 会自动实例化
async def _api_key_missing_reason() -> str:
    """区分「从来没填过」与「填过但本机密钥已失效」。

    2026-10-05 实例：重装后 cred.key 被重建，库里 09-22 的密文再也解不开。
    若只报「未配置 API Key」，用户会以为自己没填而反复重试；真正原因是本地
    密钥不可用，而且配置备份里就有可用的 cred.key。

    只有「密钥不可用」且「库里确实还有密文」同时成立才改口径：
    否则全新安装（从没存过凭据）会被误报成「已保存的 Key 解不开」。
    """
    generic = "未配置 API Key——请在 设置 > 模型配置 中填写。"
    try:
        from context import get_db
        from security.credentials import (
            FERNET_TOKEN_PREFIX, KEY_STATE_OK, credential_key_state,
        )
        if credential_key_state() == KEY_STATE_OK:
            return generic            # 密钥好着，就是没填
        db = get_db()
        if db is None:
            return generic
        cursor = await db.execute(
            "SELECT COUNT(*) FROM settings WHERE value LIKE ?",
            (FERNET_TOKEN_PREFIX + "%",),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return (
                "本机加密密钥（cred.key）不可用，已保存的 API Key 无法解密。"
                "若做过配置备份，可从备份包取回 cred.key 覆盖后重启；"
                "否则请在 设置 > 模型配置 重新填写。"
            )
    except Exception:
        # best-effort：探测失败不能阻断错误提示，回退到通用文案
        _log.debug("credential_key_state 探测失败", exc_info=True)
    return generic


from adapters.registry import register_adapter

register_adapter(YUNCAT_AGENT_ID)(YuncatNativeAdapter)
