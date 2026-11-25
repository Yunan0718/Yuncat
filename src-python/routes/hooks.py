"""routes/hooks.py —— Phase 45 事件触发端点

Phase 45 D.1: CI 失败 / cron / 自定义事件 → loop_trigger_queue → event loop 处理。
对标 OpenAI Ghost Library: 不是人等 Agent，是系统自己触发自己。
"""
import json as _json
import logging
import time as _time
import uuid as _uuid

from fastapi import APIRouter, HTTPException, Request

from config import (
    CI_ERROR_LOG_PREVIEW_MAX_CHARS,
    HOOK_RULES_LIMIT,
    HOOK_SENDER_PREVIEW_CHARS,
)

_log = logging.getLogger("yuncat.hooks")

router = APIRouter(prefix="/api/v1/hooks", tags=["hooks"])


@router.post("/trigger")
async def create_trigger(payload: dict, request: Request):
    """写入事件到 loop_trigger_queue——由 event loop 异步消费。

    body:
      trigger_type: "ci_failure" / "cron" / "manual" / "file_change"
      payload:      dict — 触发上下文（ci_failure: {repo, commit_sha, error_log}）
      loop_id:      str | None — None=任意可用 event loop, 指定值=特定 loop
      priority:     int — 0=normal, 1=high, 2=critical
    """
    trigger_type = payload.get("trigger_type", "manual")
    loop_id = payload.get("loop_id")
    event_payload = payload.get("payload", {})
    priority = payload.get("priority", 0)

    if not trigger_type:
        raise HTTPException(400, "trigger_type is required")

    event_id = str(_uuid.uuid4())[:12]
    try:
        from repositories import insert_loop_trigger
        await insert_loop_trigger(request.app.state.db, loop_id, trigger_type,
                                   event_payload, priority)
    except Exception as e:
        raise HTTPException(500, f"Failed to write trigger: {e}")

    _log.info("Trigger created: type=%s, id=%s, loop=%s", trigger_type, event_id, loop_id or "any")
    return {"ok": True, "event_id": event_id, "trigger_type": trigger_type}


@router.post("/ci-failure")
async def ci_failure(payload: dict, request: Request):
    """CI 构建失败 → 自动触发 Spec Loop 诊断修复。

    body:
      repo:       str — 仓库名
      commit_sha: str — 失败的 commit SHA
      error_log:  str — CI 错误日志
      priority:   int — 默认 2 (critical)
    """
    repo = payload.get("repo", "unknown")
    commit_sha = payload.get("commit_sha", "unknown")
    error_log = payload.get("error_log", "")
    priority = payload.get("priority", 2)

    task_id = str(_uuid.uuid4())
    trigger_payload = {
        "repo": repo,
        "commit_sha": commit_sha,
        "error_log": error_log[:CI_ERROR_LOG_PREVIEW_MAX_CHARS],
        "task_id": task_id,
    }

    try:
        from repositories import insert_loop_trigger
        await insert_loop_trigger(request.app.state.db, None, "ci_failure",
                                   trigger_payload, priority)
    except Exception as e:
        raise HTTPException(500, f"Failed to write CI trigger: {e}")

    _log.info("CI failure trigger: repo=%s, commit=%s, task=%s", repo, commit_sha[:8], task_id[:8])
    return {"ok": True, "task_id": task_id, "trigger_type": "ci_failure"}


@router.get("/triggers/pending")
async def list_pending_triggers(request: Request):
    """列出待处理的事件（供调试和 Dashboard）。"""
    try:
        cursor = await request.app.state.db.execute(
            "SELECT id, loop_id, trigger_type, priority, status, created_at "
            "FROM loop_trigger_queue WHERE status='pending' "
            f"ORDER BY priority DESC, created_at ASC LIMIT {HOOK_RULES_LIMIT}"
        )
        rows = await cursor.fetchall()
        return [
            {"id": r[0], "loop_id": r[1], "trigger_type": r[2],
             "priority": r[3], "status": r[4], "created_at": r[5]}
            for r in rows
        ]
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Phase 43 F2.1: OpenClaw 渠道消息 Webhook ──

@router.post("/channel")
async def channel_webhook(payload: dict, request: Request):
    """OpenClaw Gateway 消息渠道 → Yuncat 事件系统。

    渠道 (Telegram/Discord/Slack/WhatsApp/微信/QQ…) 的消息通过此端点接入，
    写入 loop_trigger_queue → spec_events Loop (30s) 消费 → run_spec_loop 处理。

    body:
      channel: str — "telegram" / "discord" / "slack" / "whatsapp" / "wechat" / "qq" / ...
      text:    str — 消息正文
      sender:  str — 发送者 ID (可选)
      chat_id: str — 群组/频道 ID (可选, 用于回复)
    """
    channel = payload.get("channel", "unknown")
    text = payload.get("text", "")
    if not text:
        raise HTTPException(400, "text is required")

    try:
        event_id = str(_uuid.uuid4())[:12]
        from repositories import insert_loop_trigger
        await insert_loop_trigger(request.app.state.db, None, f"{channel}_message",
                                   {
                                       "channel": channel, "text": text,
                                       "sender": payload.get("sender", "unknown"),
                                       "chat_id": payload.get("chat_id", ""),
                                       "task_input": text, "event_id": event_id,
                                   }, 1)
    except Exception as e:
        raise HTTPException(500, f"Failed to enqueue channel message: {e}")

    _log.info("Channel webhook: %s · sender=%s · %d chars",
             channel, payload.get("sender", "?")[:HOOK_SENDER_PREVIEW_CHARS], len(text))
    return {"ok": True, "channel": channel, "event_id": event_id}
