"""services/session_title_service.py —— 会话标题服务

对标 deepseek-harness 的 `session-title` 能力缝（packages/session/session-title）：

- 确定性 fallback 永远可用——没有模型、模型超时、模型报错，标题都不会空；
- provider（模型润色）是可选增强，失败时 fallback 继续生效；
- 用户手动命名置顶后，provider 永不覆盖（除非显式 force 解除置顶）；
- provider 请求走**独立适配器实例**，不污染主任务适配器的 last_usage——
  否则并发任务会把标题请求的 token 记到主任务头上（token 统计口径会被污染）。

provider 调用放在独立任务里，且用空 contextvars.Context 启动：
避免继承主任务的 current_task_id（否则标题请求会被算进该任务的 token 预算）。
"""
from __future__ import annotations

import asyncio
import contextvars
import logging

from config import LOOP_SESSION_TITLE_MAX_CHARS, SESSION_TITLE_MAX_CHARS
from repositories.session_titles_repo import (
    claim_auto_attempt,
    get_session_title,
    set_session_title,
)
from session_titles import (
    SOURCE_FALLBACK,
    SOURCE_PROVIDER,
    SOURCE_USER,
    derive_fallback_title,
    normalize_title,
)
from settings import settings as _settings

_log = logging.getLogger("yuncat.session_title")

# 运行期参数（env 可覆盖：YUNCAT_SESSION_TITLE_LLM_ENABLED 等）
SESSION_TITLE_LLM_ENABLED = _settings.session_title_llm_enabled
SESSION_TITLE_LLM_MAX_TOKENS = _settings.session_title_llm_max_tokens
SESSION_TITLE_LLM_TIMEOUT_SEC = _settings.session_title_llm_timeout_sec
SESSION_TITLE_LLM_TEMPERATURE = 0.3
SESSION_TITLE_LLM_TARGET_WORDS = 8
SESSION_TITLE_LLM_TARGET_CJK_CHARS = 16

_TITLE_SYSTEM = (
    "You write concise titles for AI assistant conversations. "
    "Reply with the title only: one short line, plain text, no quotes, no markdown, "
    "no explanation, no terminal control codes. Use the same language as the user message. "
    f"Aim for about {SESSION_TITLE_LLM_TARGET_WORDS} words "
    f"(or about {SESSION_TITLE_LLM_TARGET_CJK_CHARS} Chinese characters). "
    "If the message gives little to name, still return a short best-effort title."
)


class SessionTitleService:
    """会话标题服务——DB 持久化 + 可选模型润色。"""

    def __init__(self, db, model_config=None):
        self._db = db
        self._model_config = model_config
        self._ws_manager = None

    def set_ws_manager(self, ws_manager) -> None:
        """注入 WS 管理器——标题生成完成后把新标题推给前端。"""
        self._ws_manager = ws_manager

    def start_event_subscription(self) -> None:
        """订阅 ASSISTANT_TURN_COMPLETE——assistant 回复落库后触发标题生成。

        对标 DSH session-title：标题在首条人类消息之后异步生成，不阻塞主链路。
        """
        from services.event_bus import Events, get_event_bus
        get_event_bus().subscribe(Events.ASSISTANT_TURN_COMPLETE, self._on_assistant_turn)

    async def _on_assistant_turn(self, event_type: str, data: dict) -> None:
        session_id = str((data or {}).get("session_id") or "")
        if not session_id:
            return
        schedule_title_task(self, session_id)

    async def ensure_title(self, session_id: str) -> dict | None:
        """确保会话有标题：先落确定性回退，再尝试一次模型润色。

        幂等：已存在用户命名 / 已生成过 provider 标题 / 已尝试过自动生成时直接返回。
        """
        if not session_id:
            return None
        row = await get_session_title(self._db, session_id)
        if row and (row["source"] == SOURCE_USER or row["auto_attempted"]):
            return row
        first = await self._first_user_message(session_id)
        if not first:
            return None
        fallback = derive_fallback_title(first, LOOP_SESSION_TITLE_MAX_CHARS)
        claimed = await claim_auto_attempt(self._db, session_id, fallback)
        if not claimed:
            # 已被用户置顶或此前已尝试过——不调模型
            return await get_session_title(self._db, session_id)
        current = await get_session_title(self._db, session_id)
        if not SESSION_TITLE_LLM_ENABLED:
            return current
        generated = await self._generate_with_model(first)
        if not generated:
            return current
        title, provider, model = generated
        updated = await set_session_title(
            self._db, session_id, title, SOURCE_PROVIDER,
            provider=provider, model=model,
        )
        if updated and updated["source"] == SOURCE_PROVIDER:
            await self._broadcast(updated)
        return updated or current

    async def _first_user_message(self, session_id: str) -> str:
        cursor = await self._db.execute(
            "SELECT content FROM conversations WHERE session_id=? AND role='user' "
            "ORDER BY created_at ASC, rowid ASC LIMIT 1",
            (session_id,),
        )
        row = await cursor.fetchone()
        return str(row[0] or "") if row else ""

    def _create_title_adapter(self):
        """创建专用于标题的原生适配器实例。

        独立实例是刻意的：主任务适配器的 last_usage 会被流式请求写入，
        标题请求共用它会把 token 记到别处的任务头上。
        """
        try:
            from adapters.native import YuncatNativeAdapter
        except Exception:
            _log.warning("native adapter unavailable for session title", exc_info=True)
            return None
        adapter = YuncatNativeAdapter()
        if self._model_config is not None and hasattr(adapter, "set_model_config"):
            adapter.set_model_config(self._model_config)
        return adapter

    async def _generate_with_model(self, message: str) -> tuple[str, str, str] | None:
        """调用模型生成标题；失败返回 None（fallback 继续生效）。"""
        if self._model_config is None:
            return None
        try:
            cfg = await self._model_config.get_active_config()
        except Exception:
            _log.warning("session title: 读取模型配置失败，跳过润色", exc_info=True)
            return None
        if not cfg or not cfg.get("model") or not cfg.get("api_key"):
            return None
        prompt = (
            "Generate the session title from this user message:\n"
            f"{message[:SESSION_TITLE_MAX_CHARS * 10]}"
        )
        adapter = self._create_title_adapter()
        if adapter is None:
            return None
        try:
            result = await asyncio.wait_for(
                adapter.invoke(prompt, context={
                    "model_config": cfg,
                    "system": _TITLE_SYSTEM,
                    "max_tokens": SESSION_TITLE_LLM_MAX_TOKENS,
                    "temperature": SESSION_TITLE_LLM_TEMPERATURE,
                }),
                timeout=SESSION_TITLE_LLM_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            _log.warning("session title: 模型润色超时（fallback 标题已落库）")
            return None
        except Exception:
            _log.warning("session title: 模型润色失败（fallback 标题已落库）", exc_info=True)
            return None
        finally:
            # 标题调用是低频的一次性请求，用完即关——不留悬挂的 aiohttp session
            try:
                await adapter.stop()
            except Exception:
                _log.debug("session title adapter stop failed", exc_info=True)
        if not isinstance(result, dict) or result.get("error"):
            _log.warning("session title: 模型返回错误（fallback 标题已落库）")
            return None
        title = normalize_title(result.get("content") or "", SESSION_TITLE_MAX_CHARS)
        if not title:
            return None
        return title, str(cfg.get("provider") or ""), str(cfg.get("model") or "")

    async def _broadcast(self, row: dict) -> None:
        if self._ws_manager is None:
            return
        try:
            await self._ws_manager.broadcast({
                "type": "session_title_updated",
                "session_id": row["session_id"],
                "title": row["title"],
                "source": row["source"],
            })
        except Exception:
            _log.warning("推送 session_title_updated 失败（标题已落库）", exc_info=True)

    async def publish_title(self, row: dict) -> None:
        """把一次标题变更广播给所有窗口（多窗口下保持一致）。"""
        if not row:
            return
        await self._broadcast(row)


def _on_title_task_done(task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.warning("会话标题任务失败", exc_info=exc)


def schedule_title_task(service, session_id: str) -> None:
    """在**空 context** 中启动标题生成任务。

    空 context 是必须的：否则会继承调用方（Agent 任务）的 current_task_id，
    标题请求会被记进该任务的 token 预算，甚至触发任务级工具上下文。
    """
    if service is None or not session_id:
        return
    try:
        task = asyncio.create_task(
            service.ensure_title(session_id), context=contextvars.Context())
    except Exception:
        _log.warning("创建会话标题任务失败", exc_info=True)
        return
    task.add_done_callback(_on_title_task_done)


__all__ = [
    "SessionTitleService",
    "schedule_title_task",
    "SOURCE_FALLBACK",
    "SOURCE_PROVIDER",
    "SOURCE_USER",
]
