"""services/llm_request_log.py —— 模型请求与尝试的持久化日志

借鉴 deepseek-harness 的 `request/header` + `request/context` + `assistant/attempt`：
回答问题"这一次模型到底看到了什么、用了哪个模型、结果如何"。

设计取舍：
- **只存摘要与哈希，不存正文**：system/memory/history/prompt 的字符数与 SHA-256 前缀，
  以及工具名列表。避免把用户隐私与超大上下文写进 analytics_events。
- **零 schema 变更**：复用 `analytics_events`
  （event_type = `llm_request` / `llm_attempt`），已有按 task 查询与清理能力。
- **best-effort**：日志失败不影响模型调用；失败时记 debug 日志留痕。
- 每次 `_stream_with_fallback` 调用记一条 request；结束后记一条 attempt
  （ok / error / cancelled）。一次任务多轮 = 多条 request。

事件字段（llm_request）：
  task_id, session_id, agent_id, provider, model, mode, capability_level,
  reasoning_effort, prompt_chars, system_chars, memory_chars, history_messages,
  tool_count, tool_names, prompt_hash, at

事件字段（llm_attempt）：
  request_id, task_id, session_id, agent_id, provider, model, status,
  chunks, duration_ms, error, usage, at
"""
from __future__ import annotations

import hashlib
import json
import logging
import time

_log = logging.getLogger("yuncat.llm_request")

REQUEST_EVENT = "llm_request"
ATTEMPT_EVENT = "llm_attempt"

_MAX_TOOL_NAMES = 100
_MAX_ERROR_CHARS = 500
_PROMPT_HASH_CHARS = 16


def _prompt_hash(prompt: str, context: dict) -> str:
    """对模型可见输入做稳定哈希（不保存正文）。"""
    h = hashlib.sha256()
    h.update(str(prompt or "").encode("utf-8", errors="replace"))
    for key in ("system", "memory"):
        h.update(str(context.get(key) or "").encode("utf-8", errors="replace"))
    for msg in context.get("history") or []:
        if not isinstance(msg, dict):
            continue
        h.update(str(msg.get("role", "")).encode("utf-8", errors="replace"))
        h.update(str(msg.get("content", "")).encode("utf-8", errors="replace"))
    return h.hexdigest()[:_PROMPT_HASH_CHARS]


def summarize_request(task_id: str, agent_id: str, context: dict, prompt: str = "") -> dict:
    """生成一条 request 摘要（纯函数，便于测试）。"""
    context = context or {}
    cfg = context.get("model_config") or {}
    tools = context.get("tools") or []
    tool_names = [
        str(t.get("function", {}).get("name") or t.get("name") or "")
        for t in tools if isinstance(t, dict)
    ]
    return {
        "task_id": task_id,
        "session_id": str(context.get("session_id") or ""),
        "agent_id": agent_id,
        "provider": str(cfg.get("provider") or ""),
        "model": str(cfg.get("model") or ""),
        "mode": str(context.get("mode") or ""),
        "capability_level": context.get("capability_level"),
        "reasoning_effort": context.get("reasoning_effort"),
        "prompt_chars": len(prompt or ""),
        "system_chars": len(str(context.get("system") or "")),
        "memory_chars": len(str(context.get("memory") or "")),
        "history_messages": len(context.get("history") or []),
        "tool_count": len(tool_names),
        "tool_names": [n for n in tool_names if n][:_MAX_TOOL_NAMES],
        "prompt_hash": _prompt_hash(prompt, context),
        "at": int(time.time()),
    }


async def _insert(db, event_type: str, payload: dict) -> int | None:
    try:
        cursor = await db.execute(
            "INSERT INTO analytics_events(event_type, event_data) VALUES(?,?)",
            (event_type, json.dumps(payload, ensure_ascii=False)),
        )
        await db.commit()
        return cursor.lastrowid
    except Exception:
        _log.debug("写入 %s 失败（不影响模型调用）", event_type, exc_info=True)
        return None


async def log_request(service, task_id: str, agent_id: str, context: dict,
                      prompt: str = "") -> int | None:
    db = getattr(service, "db", None)
    if db is None:
        return None
    return await _insert(db, REQUEST_EVENT, summarize_request(task_id, agent_id, context, prompt))


async def log_attempt(service, request_id: int | None, task_id: str, agent_id: str,
                      context: dict, *, status: str, chunks: int, duration_ms: int,
                      error: str = "", usage: dict | None = None,
                      ttft_ms: int | None = None) -> int | None:
    db = getattr(service, "db", None)
    if db is None:
        return None
    cfg = (context or {}).get("model_config") or {}
    payload = {
        "request_id": request_id,
        "task_id": task_id,
        "session_id": str((context or {}).get("session_id") or ""),
        "agent_id": agent_id,
        "provider": str(cfg.get("provider") or ""),
        "model": str(cfg.get("model") or ""),
        "status": status,
        "chunks": int(chunks),
        "duration_ms": int(duration_ms),
        # 首 token 延迟（text/thinking 的首个 chunk）；无输出时为 None
        "ttft_ms": ttft_ms,
        "error": str(error or "")[:_MAX_ERROR_CHARS],
        "usage": usage or {},
        "at": int(time.time()),
    }
    return await _insert(db, ATTEMPT_EVENT, payload)


async def logged_stream_with_fallback(service, agent_id: str, prompt: str, context: dict,
                                      primary_provider: str, task_id: str = ""):
    """包装 stream_with_fallback：请求前记 request，结束后记 attempt。

    流里的 ``stream_error`` 也视为失败尝试（适配器不一定抛异常）。
    """
    from services.agent.agent_tool_helpers import stream_with_fallback

    request_id = await log_request(service, task_id, agent_id, context, prompt)
    started = time.monotonic()
    chunks = 0
    error = ""
    status = "ok"
    ttft_ms: int | None = None
    try:
        async for chunk in stream_with_fallback(
            service, agent_id, prompt, context, primary_provider, task_id,
        ):
            chunks += 1
            if ttft_ms is None and isinstance(chunk, dict) and chunk.get("type") in ("text", "thinking"):
                ttft_ms = int((time.monotonic() - started) * 1000)
            if isinstance(chunk, dict) and chunk.get("type") == "stream_error":
                status = "error"
                error = str(chunk.get("data") or chunk.get("error") or "")
            yield chunk
    except Exception as exc:
        status = "error"
        error = str(exc)
        raise
    finally:
        if status == "error" and not error:
            status = "error"
        usage = {}
        adapter = getattr(service, "adapters", {}).get(agent_id)
        if adapter is not None and isinstance(getattr(adapter, "last_usage", None), dict):
            usage = dict(adapter.last_usage)
        await log_attempt(
            service, request_id, task_id, agent_id, context,
            status=status, chunks=chunks,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error, usage=usage, ttft_ms=ttft_ms,
        )


async def list_llm_requests(db, task_id: str, limit: int = 50) -> list[dict]:
    """按任务读取 request / attempt 事件（新→旧）。"""
    if db is None or not task_id:
        return []
    try:
        cursor = await db.execute(
            "SELECT id, event_type, event_data, created_at FROM analytics_events "
            "WHERE event_type IN (?, ?) AND json_extract(event_data, '$.task_id') = ? "
            "ORDER BY id DESC LIMIT ?",
            (REQUEST_EVENT, ATTEMPT_EVENT, task_id, max(1, min(int(limit), 500))),
        )
        rows = await cursor.fetchall()
    except Exception:
        _log.warning("读取模型请求日志失败", exc_info=True)
        return []
    items = []
    for row in rows:
        try:
            data = json.loads(row[2])
        except (TypeError, json.JSONDecodeError):
            continue
        items.append({
            "id": row[0], "event": row[1], "created_at": row[3], "data": data,
        })
    return items
