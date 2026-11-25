"""Agent 执行中向用户提问的通用工具。"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time

from ..registry import tool
from tools.result import ToolResult

_log = logging.getLogger("yuncat.ask_user")


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


DEFAULT_TIMEOUT_SEC = _env_int("YUNCAT_ASK_USER_TIMEOUT_SEC", 300)
MAX_TIMEOUT_SEC = _env_int("YUNCAT_ASK_USER_MAX_TIMEOUT_SEC", 3600)
MAX_WAITERS = _env_int("YUNCAT_ASK_USER_MAX_WAITERS", 50)
MAX_OPTIONS = 8
MAX_OPTION_CHARS = 200
MAX_QUESTION_CHARS = 2000
MAX_ANSWER_CHARS = _env_int("YUNCAT_ASK_USER_MAX_ANSWER_CHARS", 8000)

_WAITERS: dict[str, tuple[str, asyncio.Future[dict]]] = {}


def resolve_user_question(token: str, answer: str, cancelled: bool = False) -> bool:
    entry = _WAITERS.get(token)
    if not entry or entry[1].done():
        return False
    entry[1].set_result({"answer": str(answer or "").strip(), "cancelled": bool(cancelled)})
    return True


def cancel_questions_for_task(task_id: str) -> list[str]:
    tokens = [token for token, (tid, _future) in _WAITERS.items() if tid == task_id]
    for token in tokens:
        resolve_user_question(token, "", cancelled=True)
        _WAITERS.pop(token, None)
    return tokens


def _agent_service():
    from context import get_app

    app = get_app()
    return getattr(app.state, "agent_service", None) if app else None


def _normalize_options(options: list[str] | None) -> list[str]:
    if options is None:
        return []
    if not isinstance(options, list) or len(options) > MAX_OPTIONS:
        raise ValueError(f"options must be an array with at most {MAX_OPTIONS} items")
    normalized: list[str] = []
    for value in options:
        if not isinstance(value, str):
            raise ValueError("every option must be a string")
        text = value.strip()
        if len(text) > MAX_OPTION_CHARS:
            raise ValueError(f"each option must be at most {MAX_OPTION_CHARS} characters")
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _validate_input(
    question: str, options: list[str] | None, allow_free_text: bool,
) -> tuple[str, list[str], str]:
    text = str(question or "").strip()
    if not text:
        return "", [], "question is required"
    if len(text) > MAX_QUESTION_CHARS:
        return "", [], f"question must be at most {MAX_QUESTION_CHARS} characters"
    try:
        normalized = _normalize_options(options)
    except ValueError as exc:
        return "", [], str(exc)
    if not allow_free_text and not normalized:
        return "", [], "options are required when free text is disabled"
    return text, normalized, ""


def _tool_error(error: str, **meta) -> dict:
    return ToolResult(success=False, error=error, meta={"tool_name": "ask_user", **meta}).to_dict()


def _valid_timeout(value: int) -> int:
    try:
        timeout = int(value or DEFAULT_TIMEOUT_SEC)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SEC
    return max(1, min(timeout, MAX_TIMEOUT_SEC))


def _task_context() -> tuple[str, str]:
    from context import get_current_agent_id, get_current_task_id

    try:
        return get_current_task_id() or "", get_current_agent_id() or "unknown"
    except LookupError:
        return "", "unknown"


async def _notify_question_closed(task_id: str, token: str, reason: str) -> None:
    service = _agent_service()
    if service is None:
        return
    try:
        await service._safe_send(task_id, {
            "type": "user_question_closed", "task_id": task_id,
            "token": token, "reason": reason,
        })
    except Exception:
        _log.warning("推送 user_question_closed 失败（不影响工具返回）", exc_info=True)


async def _ask_via_ws(task_id: str, agent_id: str, payload: dict, future: asyncio.Future) -> dict:
    service = _agent_service()
    if service is None:
        raise RuntimeError("Agent service unavailable")
    await service._safe_send(task_id, {
        "type": "user_question_request", "task_id": task_id,
        "agent_id": agent_id, **payload,
    })
    return await future


@tool(
    name="ask_user", agents=["yuncat", "hermes", "openclaw"],
    description=(
        "Ask the user a blocking question when a decision cannot be inferred and would materially "
        "change the result. Use approval/confirmation flows for destructive actions instead. "
        "Returns the user's answer, or an explicit cancelled/timeout result."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "One concise question."},
            "options": {"type": "array", "items": {"type": "string"}, "description": "Optional suggested answers."},
            "allow_free_text": {"type": "boolean", "default": True},
            "timeout_sec": {"type": "integer", "description": "Wait timeout in seconds."},
        },
        "required": ["question"],
    },
    is_read_only=True, concurrency_safety="exclusive", groups=["interaction"],
    tool_timeout=MAX_TIMEOUT_SEC + 10,
)
async def ask_user(
    question: str, options: list[str] | None = None,
    allow_free_text: bool = True, timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> dict:
    text, normalized_options, input_error = _validate_input(question, options, allow_free_text)
    if input_error:
        return _tool_error(input_error)
    task_id, agent_id = _task_context()
    if not task_id:
        return _tool_error("ask_user requires a running task context")
    if len(_WAITERS) >= MAX_WAITERS:
        return _tool_error("Too many pending user questions")
    timeout = _valid_timeout(timeout_sec)
    token = secrets.token_hex(16)
    future = asyncio.get_running_loop().create_future()
    _WAITERS[token] = (task_id, future)
    payload = {
        "token": token, "question": text, "options": normalized_options,
        "allow_free_text": bool(allow_free_text), "expires_at": int(time.time()) + timeout,
    }
    try:
        result = await asyncio.wait_for(_ask_via_ws(task_id, agent_id, payload, future), timeout=timeout)
    except asyncio.TimeoutError:
        await _notify_question_closed(task_id, token, "timeout")
        return _tool_error(f"User did not answer within {timeout}s", timeout=True)
    finally:
        _WAITERS.pop(token, None)
    if result.get("cancelled"):
        return _tool_error("User cancelled the question", cancelled=True)
    answer = result.get("answer", "")
    if not answer:
        return _tool_error("User returned an empty answer")
    if normalized_options and not allow_free_text and answer not in normalized_options:
        return _tool_error("User answer is not one of the allowed options")
    return ToolResult(
        success=True, data={"question": text, "answer": answer},
        observation=f"用户回答：{answer[:300]}", meta={"tool_name": "ask_user"},
    ).to_dict()
