"""routes/sessions.py —— 会话管理 + 对话历史 + 导出 + 消息"""

import logging
import re
import uuid as _uuid

from fastapi import APIRouter, HTTPException, Request

from config import (
    DEFAULT_AGENT_ID,
    LOOP_SESSION_TITLE_MAX_CHARS,
    SESSION_LIST_DEFAULT_LIMIT,
    SESSION_MESSAGES_DEFAULT_LIMIT,
    SESSION_CONVERSATION_DEFAULT_LIMIT,
    SESSION_EXPORT_MESSAGE_LIMIT,
    SESSION_TITLE_MAX_CHARS,
    SESSION_TITLE_PREVIEW_CHARS,
    SESSION_EXPORT_CONTENT_CHARS,
)
from session_titles import (
    SOURCE_FALLBACK,
    SOURCE_USER,
    derive_fallback_title,
    normalize_title,
)

router = APIRouter(prefix="/api/v1", tags=["sessions"])

_log = logging.getLogger("yuncat.sessions")


# ---- 会话列表 ----

@router.get("/sessions")
async def list_sessions(
    limit: int = SESSION_LIST_DEFAULT_LIMIT,
    offset: int = 0,
    request: Request = None,
):
    """列出最近的 session（按最后活动时间排序，含预览和标题 + 用量统计）"""
    from repositories import list_sessions_summary
    from services.session_stats import attach_session_stats

    rows = await list_sessions_summary(request.app.state.db, limit, offset)
    return await attach_session_stats(request.app.state.db, rows)


@router.get("/sessions/stats")
async def get_sessions_stats(days: int = 30, request: Request = None):
    """跨会话统计：会话/消息/任务数、token 用量、状态与 agent 分布、每日序列。

    与用量看板同一账本口径（输入 + 输出），不另起一套算法。
    """
    from services.session_stats import session_stats_overview

    return await session_stats_overview(request.app.state.db, days)


@router.post("/sessions/new")
async def new_session(request: Request = None):
    """创建新会话——仅生成 session_id，不写 DB"""
    return {"session_id": str(_uuid.uuid4())}


@router.patch("/sessions/{session_id}")
async def rename_session(session_id: str, payload: dict, request: Request = None):
    """重命名会话 —— 持久化到 session_titles，并置顶（自动生成不再覆盖）。"""
    title = normalize_title((payload or {}).get("title", ""), SESSION_TITLE_MAX_CHARS)
    if not title:
        raise HTTPException(400, "Missing 'title'")
    from repositories import set_session_title as _set_stored_title

    row = await _set_stored_title(
        request.app.state.db, session_id, title, SOURCE_USER, auto_attempted=1)
    if not row:
        raise HTTPException(400, "Invalid 'title'")
    service = getattr(request.app.state, "session_title_service", None)
    if service is not None:
        await service.publish_title(row)
    return {"status": "ok", "session_id": session_id,
            "title": row["title"], "source": row["source"]}


@router.get("/sessions/{session_id}/title")
async def get_session_title(session_id: str, request: Request = None):
    """获取会话标题：已存标题优先，没有则按首条用户消息派生。"""
    from repositories import get_session_title as _get_stored_title

    db = request.app.state.db
    row = await _get_stored_title(db, session_id)
    if row and row["title"]:
        return {"session_id": session_id, "title": row["title"], "source": row["source"]}
    first = await _first_user_message(db, session_id)
    return {"session_id": session_id,
            "title": derive_fallback_title(first, LOOP_SESSION_TITLE_MAX_CHARS),
            "source": SOURCE_FALLBACK}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request = None):
    """删除整个会话及其所有消息"""
    from repositories import (
        delete_session_deliverables,
        delete_session_messages,
        delete_session_title,
        delete_session_todos,
    )
    deleted = await delete_session_messages(request.app.state.db, session_id)
    titles = await delete_session_title(request.app.state.db, session_id)
    todos = await delete_session_todos(request.app.state.db, session_id)
    deliverables = await delete_session_deliverables(request.app.state.db, session_id)
    if deleted == 0 and titles == 0 and todos == 0 and deliverables == 0:
        raise HTTPException(404, "Session not found or already empty")
    return {"status": "ok", "deleted": deleted}


@router.get("/sessions/{session_id}/deliverables")
async def get_session_deliverables(session_id: str, request: Request = None):
    """会话交付物：present 声明的最终文件 + 工具层改动 + shell 改动。

    三种来源分开返回，语义别混：
    - presented：present 工具声明的最终文件；
    - changes：快照口径（工具层改过的文件，带增删行数，可看 diff）；
    - shell：shell 命令改动的文件（git 脏文件差集，无行数、无 diff）。
    """
    from repositories import list_session_deliverables
    from services.deliverables import (
        list_changed_files_with_stats, list_observed_changes,
    )

    db = request.app.state.db
    rows = await list_session_deliverables(db, session_id)
    presented = [r for r in rows if r.get("source") != "shell"]
    shell = [r for r in rows if r.get("source") == "shell"]
    changed = await list_observed_changes(db, session_id)
    changes = await list_changed_files_with_stats(db, session_id)
    return {
        "session_id": session_id,
        "presented": presented,
        "shell": shell,
        "changed": changed,   # 工具调用口径（旧字段，保留兼容）
        "changes": changes,   # 快照口径 + 增删行数（DSH workspace/changes 语义）
    }


@router.get("/sessions/{session_id}/deliverables/diff")
async def get_session_deliverable_diff(session_id: str, path: str, request: Request = None):
    """单个改动文件的 unified diff——只接受本会话记录过的路径（fail-closed）。"""
    from services.deliverables import file_diff

    result = await file_diff(request.app.state.db, session_id, path)
    if result.get("status") == "error":
        raise HTTPException(404, result.get("message", "not found"))
    return result


@router.get("/sessions/{session_id}/todos")
async def get_session_todos_route(session_id: str, request: Request = None):
    """读取会话当前任务清单（Agent 用 todo_write 写入的那份）。"""
    from repositories import get_session_todos

    todos = await get_session_todos(request.app.state.db, session_id)
    return {"session_id": session_id, "todos": todos}


# ---- 会话消息 ----

@router.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, limit: int = SESSION_MESSAGES_DEFAULT_LIMIT, request: Request = None):
    """获取指定会话的消息列表"""
    from repositories import get_conversation

    return await get_conversation(request.app.state.db, session_id, limit)


@router.post("/sessions/{session_id}/title")
async def generate_session_title(session_id: str, payload: dict = None, request: Request = None):
    """重新生成会话标题——确定性回退（首条用户消息派生）。

    默认不覆盖用户手动命名的标题（置顶语义）；body 传 {"force": true} 可显式解除置顶。
    """
    from repositories import (
        get_session_title as _get_stored_title,
        set_session_title as _set_stored_title,
    )

    db = request.app.state.db
    force = bool((payload or {}).get("force"))
    existing = await _get_stored_title(db, session_id)
    if existing and existing["source"] == SOURCE_USER and not force:
        return {"session_id": session_id, "title": existing["title"],
                "source": existing["source"], "regenerated": False}
    first = await _first_user_message(db, session_id)
    if not first:
        raise HTTPException(404, "No messages in session")
    title = derive_fallback_title(first, LOOP_SESSION_TITLE_MAX_CHARS)
    row = await _set_stored_title(
        db, session_id, title, SOURCE_FALLBACK, auto_attempted=0, force=force)
    service = getattr(request.app.state, "session_title_service", None)
    if service is not None:
        from services.session_title_service import schedule_title_task
        # 重置 auto_attempted 后，后台再让模型润色一次（对标 DSH 的显式 refresh）
        schedule_title_task(service, session_id)
    return {"session_id": session_id, "title": (row or {}).get("title", title),
            "source": (row or {}).get("source", SOURCE_FALLBACK), "regenerated": True}


async def _first_user_message(db, session_id: str) -> str:
    """取会话首条用户消息正文（按时间 + rowid 排序，避免同秒消息顺序不稳）。"""
    cursor = await db.execute(
        "SELECT content FROM conversations WHERE session_id=? AND role='user' "
        "ORDER BY created_at ASC, rowid ASC LIMIT 1",
        (session_id,),
    )
    row = await cursor.fetchone()
    return str(row[0] or "") if row else ""


@router.get("/sessions/{session_id}/usage")
async def get_session_usage(session_id: str, request: Request = None):
    """返回当前会话的 token 用量统计。

    2026-10-22：拆成两个口径——``total_tokens`` 是账本口径（输入 + 输出，真实消耗），
    ``message_tokens`` 是消息级口径（**只有输出 token**）。此前只报后者，
    比真实消耗少一个数量级（输入占大头）。
    """
    cursor = await request.app.state.db.execute(
        "SELECT role, SUM(token_count) AS total_tokens, COUNT(*) AS msg_count "
        "FROM conversations WHERE session_id=? AND token_count IS NOT NULL "
        "GROUP BY role",
        (session_id,),
    )
    rows = await cursor.fetchall()
    breakdown: dict = {}
    for row in rows:
        breakdown[row[0]] = {"tokens": row[1] or 0, "messages": row[2]}
    from services.token_tracker import get_token_usage_overview

    ledger = await get_token_usage_overview(
        request.app.state.db, days=0, session_id=session_id)
    result: dict = {
        "session_id": session_id,
        "breakdown": breakdown,
        "total_tokens": ledger["total_tokens"],
        "prompt_tokens": ledger["prompt_tokens"],
        "completion_tokens": ledger["completion_tokens"],
        "cached_tokens": ledger["cached_tokens"],
        "reasoning_tokens": ledger["reasoning_tokens"],
        "call_count": ledger["call_count"],
        "message_tokens": sum(v["tokens"] for v in breakdown.values()),
    }
    return result


@router.get("/sessions/{session_id}/export")
async def export_session(
    session_id: str, request: Request = None,
    download: bool = False, format: str = "markdown",
):
    """导出会话为 Markdown 或 JSONL（download=true 直接下载）。"""
    from repositories import get_conversation

    msgs = await get_conversation(request.app.state.db, session_id, limit=SESSION_EXPORT_MESSAGE_LIMIT)
    if format == "jsonl":
        body = _build_jsonl_export(session_id, msgs)
        if download:
            from fastapi.responses import Response
            return Response(
                content=body.encode("utf-8"), media_type="application/x-ndjson; charset=utf-8",
                headers={"Content-Disposition": f"attachment; filename=session_{session_id}.jsonl"},
            )
        return {"jsonl": body, "message_count": len(msgs), "format": "jsonl"}
    if format != "markdown":
        raise HTTPException(400, "format must be markdown or jsonl")
    lines = ["# Yuncat 对话导出", "", f"会话ID: {session_id}", ""]
    for m in msgs:
        role_label = "🧑 用户" if m["role"] == "user" else "🤖 Agent"
        lines.append(f"### {role_label}")
        lines.append("")
        lines.append(m["content"][:SESSION_EXPORT_CONTENT_CHARS])
        lines.append("")
    md = "\n".join(lines)
    if download:
        from fastapi.responses import Response

        return Response(
            content=md.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f"attachment; filename=session_{session_id}.md",
            },
        )
    return {"markdown": md, "message_count": len(msgs), "format": "markdown"}


def _build_jsonl_export(session_id: str, msgs: list[dict]) -> str:
    import json as _json
    import time as _time

    records = [{
        "type": "session", "session_id": session_id, "exported_at": int(_time.time()),
        "message_count": len(msgs),
    }]
    records.extend({
        "type": "message", "id": m.get("id", ""), "role": m.get("role", ""),
        "content": m.get("content", ""), "created_at": m.get("created_at"),
        "token_count": m.get("token_count"),
        "reasoning_content": m.get("reasoning_content", ""),
        "tool_calls_json": m.get("tool_calls_json", ""),
    } for m in msgs)
    return "\n".join(_json.dumps(record, ensure_ascii=False) for record in records) + "\n"


@router.get("/sessions/{session_id}/outline")
async def session_outline(session_id: str, request: Request = None) -> dict:
    """按用户消息聚合 turn，返回可跳转的会话大纲。"""
    cursor = await request.app.state.db.execute(
        "SELECT id, role, content, created_at, COALESCE(token_count, 0) "
        "FROM conversations WHERE session_id=? ORDER BY created_at ASC, rowid ASC LIMIT ?",
        (session_id, SESSION_EXPORT_MESSAGE_LIMIT),
    )
    rows = await cursor.fetchall()
    if not rows:
        raise HTTPException(404, "Session not found or empty")
    task_cursor = await request.app.state.db.execute(
        "SELECT id, input, created_at FROM tasks WHERE session_id=? "
        "ORDER BY created_at ASC", (session_id,),
    )
    tasks = [(row[0], str(row[1] or "").strip(), int(row[2] or 0)) for row in await task_cursor.fetchall()]
    used_tasks: set[str] = set()
    turns: list[dict] = []
    current: dict | None = None
    for conv_id, role, content, created_at, token_count in rows:
        if role == "user":
            if current is not None:
                turns.append(current)
            text = str(content or "").strip()
            task_id = next((tid for tid, prompt, _ts in tasks if tid not in used_tasks and prompt == text), "")
            if task_id:
                used_tasks.add(task_id)
            current = {
                "index": len(turns) + 1, "task_id": task_id, "title": _outline_title(text),
                "preview": "", "message_count": 1, "tokens": int(token_count or 0),
                "started_at": created_at, "ended_at": created_at, "user_message_id": conv_id,
            }
        elif current is not None:
            current["message_count"] += 1
            current["tokens"] += int(token_count or 0)
            current["ended_at"] = created_at
            if role == "assistant" and not current["preview"]:
                current["preview"] = str(content or "")[:180]
    if current is not None:
        turns.append(current)
    return {"ok": True, "session_id": session_id, "turns": turns}


def _outline_title(text: str) -> str:
    clean = re.sub(r"```[^`]*```", "", text, flags=re.DOTALL)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:SESSION_TITLE_PREVIEW_CHARS] or "(empty prompt)"


# ---- 对话历史 ----

@router.get("/conversations/{session_id}")
async def get_conversation_endpoint(session_id: str, limit: int = SESSION_CONVERSATION_DEFAULT_LIMIT, request: Request = None):
    return await request.app.state.memory_service.get_conversation(session_id, limit)


@router.post("/conversations/{session_id}/fork")
async def fork_conversation_endpoint(
    session_id: str, from_message_id: str, request: Request,
):
    """Phase 6: Fork 会话——从指定消息分支出新会话"""
    from repositories import fork_conversation

    new_session_id = await fork_conversation(
        request.app.state.db, session_id, from_message_id,
    )
    if not new_session_id:
        raise HTTPException(404, "Source message not found or not in this session")
    return {"new_session_id": new_session_id}


# ---- Phase 49-C: 消息版本链 ----

@router.post("/conversations/{conv_id}/edit")
async def edit_conversation_endpoint(conv_id: str, payload: dict, request: Request):
    """编辑消息——基于原消息创建新版本，并可选择从原任务派生新任务。"""
    content = (payload or {}).get("content")
    if not content or not isinstance(content, str):
        raise HTTPException(400, "Missing or invalid 'content'")

    from repositories import edit_conversation

    edit_result = await edit_conversation(request.app.state.db, conv_id, content)
    new_conv_id = edit_result["conv_id"]
    await request.app.state.db.commit()
    response: dict = {"conv_id": new_conv_id}

    parent_task_id = (payload or {}).get("task_id")
    if parent_task_id:
        task = await request.app.state.agent_service.get_task(parent_task_id)
        if not task:
            raise HTTPException(404, "Parent task not found")
        cursor = await request.app.state.db.execute(
            "SELECT session_id, agent_id FROM conversations WHERE id=?",
            (new_conv_id,),
        )
        row = await cursor.fetchone()
        session_id = (row[0] if row else None) or task.get("session_id") or str(_uuid.uuid4())
        agent_id = (row[1] if row else None) or task.get("agent_id") or DEFAULT_AGENT_ID
        language = (payload or {}).get("language", "zh")
        new_task_id = await request.app.state.agent_service.start_task(
            agent_id, content, session_id, mode="agent", language=language,
        )
        await request.app.state.db.execute(
            "UPDATE tasks SET parent_task_id=? WHERE id=?",
            (parent_task_id, new_task_id),
        )
        await request.app.state.db.commit()
        response["task_id"] = new_task_id
    return response


@router.post("/conversations/{task_id}/rate")
async def rate_conversation(task_id: str, payload: dict, request: Request = None):
    """用户对一次回复的赞/踩（rating: 1 / -1）——写入反馈学习闭环。

    2026-09-15 修复：前端 MessageItem 的 👍/👎 一直在调这个端点，后端**没有实现**，
    每次点赞都是 404；前端 catch 里 no-op，用户点完没有任何反应，反馈也从未进入学习。
    这里复用 FeedbackLearner（与工具审批、委托反馈共用一套知识评分调整），
    不另起第二套学习机制。
    """
    rating = payload.get("rating")
    if rating not in (1, -1):
        raise HTTPException(400, "rating must be 1 or -1")

    learner = getattr(request.app.state, "feedback_learner", None)
    if learner is None:
        raise HTTPException(503, "feedback learner unavailable")

    context = ""
    agent_id = None
    try:
        cursor = await request.app.state.db.execute(
            "SELECT input, agent_id FROM tasks WHERE id=?", (task_id,),
        )
        row = await cursor.fetchone()
        if row:
            context, agent_id = row[0] or "", row[1]
    except Exception:
        # 取不到上下文不影响记录反馈本身，留痕后继续
        _log.warning("rate: load task context failed for %s", task_id[:8], exc_info=True)

    await learner.record_feedback(
        "human_accepted" if rating == 1 else "human_rejected",
        task_id,
        context=context,
        agent_id=agent_id,
    )
    return {"ok": True, "rating": rating}


@router.get("/conversations/{conv_id}/versions")
async def get_conversation_versions_endpoint(conv_id: str, request: Request):
    """获取消息版本链树。"""
    from repositories import get_conversation_versions

    return await get_conversation_versions(request.app.state.db, conv_id)
