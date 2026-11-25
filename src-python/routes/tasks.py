"""routes/tasks.py —— 任务 CRUD + 搜索 + 置顶 + 取消 + Plan 执行"""

import asyncio
import json as _json
import logging
import uuid as _uuid

_log = logging.getLogger("yuncat.tasks")

from fastapi import APIRouter, HTTPException, Request
from models.requests import TaskSubmitRequest, TaskPinRequest, TaskTagsRequest
from config import DEFAULT_AGENT_ID, TASK_LIST_DEFAULT_LIMIT, TASK_SEARCH_DEFAULT_LIMIT

router = APIRouter(prefix="/api/v1", tags=["tasks"])


# ---- 任务 CRUD ----

@router.post("/tasks")
async def submit_task(body: TaskSubmitRequest, request: Request) -> dict:
    prompt = body.prompt
    explicit_agent = body.agent_id
    if explicit_agent:
        task_id = await request.app.state.agent_service.invoke(
            agent_id=explicit_agent, prompt=prompt,
            context=body.context, language=body.language,
        )
        return {"task_id": task_id, "route_info": {"route": "explicit", "agent_id": explicit_agent}}
    # Phase 33: 意图路由 + 能力路由双保险（与 WS 路径保持一致）
    route = await request.app.state.intent_service.analyze(prompt)
    task_id = route.get("task_id")
    if not task_id:
        from services.capability_router import build_profiles, route_by_capability
        profiles = await build_profiles(request.app.state.agent_service.adapters)
        # Phase 48 T2-1: route_by_capability 改 async，加 await
        fallback_agent = await route_by_capability(prompt, profiles, default=DEFAULT_AGENT_ID)
        task_id = await request.app.state.agent_service.invoke(
            agent_id=fallback_agent, prompt=prompt,
            language=body.language,
        )
        return {"task_id": task_id, "route_info": {"route": "capability_fallback", "agent_id": fallback_agent}}
    return {
        "task_id": task_id,
        "route_info": route.get("route_info"),
        "complexity": route.get("complexity"),
        "should_deliberate": route.get("should_deliberate", False),
    }


@router.get("/tasks")
async def list_tasks(
    status: str = None,
    limit: int = TASK_LIST_DEFAULT_LIMIT,
    offset: int = 0,
    request: Request = None,
) -> list[dict]:
    return await request.app.state.agent_service.list_tasks(status, limit, offset)


@router.get("/tasks/search")
async def search_tasks(q: str, limit: int = TASK_SEARCH_DEFAULT_LIMIT, request: Request = None) -> list[dict]:
    """Phase 18: 全文搜索任务——LIKE 在 input/output 中搜索"""
    # Phase 48 T1-14: 转义 LIKE 通配符 %/_,防用户输入意外匹配
    escaped_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped_q}%"
    cursor = await request.app.state.db.execute(
        "SELECT id, input, status, agent_id, created_at, "
        "SUBSTR(COALESCE(output,''), 1, 100) AS output_preview "
        "FROM tasks WHERE input LIKE ? ESCAPE '\\' OR output LIKE ? ESCAPE '\\' "
        "ORDER BY created_at DESC LIMIT ?",
        (pattern, pattern, limit),
    )
    rows = await cursor.fetchall()
    return [
        {"id": r[0], "input": (r[1] or "")[:120], "status": r[2],
         "agent_id": r[3], "created_at": r[4], "output_preview": r[5] or ""}
        for r in rows
    ]


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request) -> dict:
    task = await request.app.state.agent_service.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


async def _require_child_task(parent_task_id: str, child_task_id: str, request: Request) -> dict:
    child = await request.app.state.agent_service.get_task(child_task_id)
    if not child or child.get("parent_task_id") != parent_task_id:
        raise HTTPException(404, "Sub-agent task not found under this parent")
    return child


@router.get("/tasks/{task_id}/subagents")
async def list_subagents(task_id: str, request: Request) -> dict:
    parent = await request.app.state.agent_service.get_task(task_id)
    if not parent:
        raise HTTPException(404, "Task not found")
    return {"ok": True, "items": parent.get("children", [])}


@router.post("/tasks/{task_id}/subagents/{child_task_id}/stop")
async def stop_subagent(task_id: str, child_task_id: str, request: Request) -> dict:
    child = await _require_child_task(task_id, child_task_id, request)
    if child.get("status") in ("completed", "failed", "cancelled"):
        return {"ok": True, "already_terminal": True, "status": child.get("status")}
    await request.app.state.agent_service.cancel_task(child_task_id)
    return {"ok": True, "status": "cancelled"}


@router.post("/tasks/{task_id}/subagents/{child_task_id}/steer")
async def steer_subagent(task_id: str, child_task_id: str, body: dict, request: Request) -> dict:
    from config import WS_PROMPT_MAX_CHARS
    from services.agent.steer_inbox import SteerQueueFull, SteerTaskUnavailable, enqueue_steer

    await _require_child_task(task_id, child_task_id, request)
    prompt = str((body or {}).get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    if len(prompt) > WS_PROMPT_MAX_CHARS:
        raise HTTPException(413, f"prompt exceeds {WS_PROMPT_MAX_CHARS} characters")
    try:
        _child, queued = await enqueue_steer(
            request.app.state.agent_service, child_task_id, prompt,
        )
    except SteerTaskUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    except SteerQueueFull as exc:
        raise HTTPException(429, str(exc)) from exc
    await request.app.state.agent_service._safe_send(task_id, {
        "type": "subagent_steer_queued", "task_id": task_id,
        "child_task_id": child_task_id, "queued": queued, "prompt": prompt,
    })
    return {"ok": True, "queued": queued}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request) -> dict:
    await request.app.state.agent_service.cancel_task(task_id)
    return {"status": "ok"}


async def _require_task(request: Request, task_id: str) -> dict:
    """任务必须存在——否则这些"改状态"的端点会对不存在的 id 假装成功。"""
    task = await request.app.state.agent_service.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.post("/tasks/{task_id}/interrupt")
async def interrupt_task(task_id: str, request: Request) -> dict:
    """G4: 中断任务——running → interrupted（可恢复，区别于 cancelled 终态）。
    优雅关闭时 lifespan 会批量调用；用户侧的"暂停/停止"走 pause/cancel。
    任务不存在时返回 404——不存在的 id 不能"中断成功"。
    """
    await _require_task(request, task_id)
    await request.app.state.agent_service.interrupt_task(task_id)
    return {"status": "ok"}


@router.post("/tasks/{task_id}/pin")
async def pin_task(task_id: str, body: TaskPinRequest, request: Request = None) -> dict:
    """Phase 18: 置顶/取消置顶任务"""
    await _require_task(request, task_id)
    pinned = body.pinned
    await request.app.state.db.execute(
        "UPDATE tasks SET pinned=? WHERE id=?", (pinned, task_id),
    )
    await request.app.state.db.commit()
    return {"status": "ok", "pinned": bool(pinned)}


@router.post("/tasks/{task_id}/tags")
async def set_task_tags(task_id: str, body: TaskTagsRequest, request: Request = None) -> dict:
    """Phase 18: 设置任务标签（JSON 数组字符串）"""
    await _require_task(request, task_id)
    tags = _json.dumps(body.tags, ensure_ascii=False)
    await request.app.state.db.execute(
        "UPDATE tasks SET tags=? WHERE id=?", (tags, task_id),
    )
    await request.app.state.db.commit()
    return {"status": "ok"}


@router.post("/tasks/{task_id}/pause")
async def pause_task(task_id: str, request: Request, force: bool = False) -> dict:
    """暂停正在运行的任务——设置 DB 标志，agent loop 下一轮检查并退出。

    Task 6.13: force=true 时额外 terminate adapter 子进程，立即中断适配器端处理。
    对标 Claude Code Ctrl-C SIGINT 中断。watchdog 自动重启子进程（_stopping 未设）。
    """
    await _require_task(request, task_id)
    await request.app.state.db.execute(
        "UPDATE tasks SET status='paused' WHERE id=? AND status='running'",
        (task_id,),
    )
    await request.app.state.db.commit()
    # 同时取消 asyncio 内部 task（如果存在）
    # Phase 48-H 维度 6: 修复 _running_tasks→_task_handles AttributeError bug
    agent_svc = request.app.state.agent_service
    internal_task = agent_svc._task_handles.pop(task_id, None)
    if internal_task and not internal_task.done():
        internal_task.cancel()
    # Task 6.13: 强制暂停——terminate adapter 子进程中断当前处理
    if force:
        task = await agent_svc.get_task(task_id)
        if task:
            adapter = agent_svc.adapters.get(task.get("agent_id"))
            if (adapter and hasattr(adapter, "process")
                    and adapter.process and adapter.process.returncode is None):
                try:
                    adapter.process.terminate()
                    _log.info("Force pause: terminated adapter %s subprocess (pid=%s)",
                              task.get("agent_id"), adapter.process.pid)
                except ProcessLookupError:
                    _log.debug("Force pause: adapter process already exited")
                except Exception:
                    _log.warning("Force pause: failed to terminate adapter subprocess",
                                 exc_info=True)
    # Phase 48-H 维度 6: 推送 WS 状态更新——前端实时反映 paused
    await agent_svc._safe_send(task_id, {"type": "task_paused", "task_id": task_id, "force": force})
    return {"status": "ok"}


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str, request: Request) -> dict:
    """Phase 48-H 维度 6: 恢复已暂停的任务——对标 Claude Code Pause/Resume"""
    agent_svc = request.app.state.agent_service
    ok = await agent_svc.resume_task(task_id)
    if not ok:
        raise HTTPException(409, "Task not resumable (not found or not paused)")
    # Phase 48-H 维度 6: 推送 WS 状态更新——前端实时反映 running
    await agent_svc._safe_send(task_id, {"type": "task_resumed", "task_id": task_id})
    return {"status": "ok"}


# ---- Phase 14: Plan 步骤执行 ----

@router.post("/tasks/{task_id}/execute-plan")
async def execute_plan(task_id: str, request: Request) -> dict:
    """执行 Plan 的结构化步骤——从子任务中读取步骤并按序执行。"""
    import asyncio

    task = await request.app.state.agent_service.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    from repositories import create_plan_steps, get_plan_steps

    existing_steps = await get_plan_steps(request.app.state.db, task_id)
    if existing_steps:
        for s in existing_steps:
            if s["status"] == "failed":
                await request.app.state.db.execute(
                    "UPDATE tasks SET status='pending', error_message=NULL WHERE id=?",
                    (s["id"],),
                )
        await request.app.state.db.commit()
    else:
        output = task.get("output", "")
        if output:
            try:
                plan = _json.loads(output)
                steps = plan.get("steps", [])
                if steps:
                    await create_plan_steps(
                        request.app.state.db, task_id, steps, task["agent_id"],
                    )
            except _json.JSONDecodeError:
                pass

    agent_id = task["agent_id"]
    t = asyncio.create_task(
        request.app.state.agent_service.plan_executor.execute_plan_steps(task_id, agent_id),
    )
    t.add_done_callback(lambda _t: _log.exception("execute_plan_steps failed", exc_info=_t.exception()) if _t.exception() else None)
    return {"status": "ok", "task_id": task_id}


# Phase 42: Agent 操作撤销 — 从 snapshots 恢复到 task 执行前
@router.post("/tasks/{task_id}/undo")
async def undo_task(task_id: str, request: Request) -> dict:
    """撤销 Agent 的文件变更 — 从 snapshots 表恢复到 task 执行前的文件状态"""
    if not hasattr(request.app.state, "snapshot_service"):
        raise HTTPException(501, "snapshot_service not available")
    result = await request.app.state.snapshot_service.restore_turn(task_id)
    if result.get("status") == "error":
        raise HTTPException(400, result.get("message", "撤销失败"))
    # status=partial 表示有路径没能恢复（超限/越界/备份缺失）——必须把清单交给用户，
    # 不能让「部分撤销」看起来像「全部成功」。
    return {
        "ok": True,
        "status": result.get("status", "ok"),
        "restored_files": result.get("restored_files", 0),
        "removed_new_files": result.get("removed_new_files", 0),
        "skipped": result.get("skipped", []),
    }


# ---- Phase 49-C/D: 任务重试 / 运行时状态快照 ----

@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, payload: dict, request: Request) -> dict:
    """基于某条对话消息重试任务——新任务 parent_task_id 指向原任务。"""
    conv_id = (payload or {}).get("conv_id")
    if not conv_id:
        raise HTTPException(400, "Missing 'conv_id'")

    cursor = await request.app.state.db.execute(
        "SELECT content, agent_id, session_id FROM conversations WHERE id=?",
        (conv_id,),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Conversation not found")
    content, agent_id, session_id = row
    prompt = (payload or {}).get("content") or content
    agent_id = agent_id or DEFAULT_AGENT_ID

    new_task_id = await request.app.state.agent_service.invoke(
        agent_id=agent_id, prompt=prompt, context={"mode": "agent"},
    )
    await request.app.state.db.execute(
        "UPDATE tasks SET parent_task_id=? WHERE id=?",
        (task_id, new_task_id),
    )
    await request.app.state.db.commit()
    return {"task_id": new_task_id}


@router.get("/tasks/{task_id}/state")
async def get_task_state_endpoint(task_id: str, request: Request) -> dict:
    """获取任务运行时状态快照及会话消息。"""
    state = await request.app.state.agent_service.get_task_state(task_id)
    if not state:
        raise HTTPException(404, "Task not found")

    task = await request.app.state.agent_service.get_task(task_id)
    messages = None
    if task and task.get("session_id"):
        messages = await request.app.state.memory_service.get_conversation(
            task["session_id"],
        )
    return {
        "task_id": task_id,
        "status": state.get("status"),
        "task_state": state.get("task_state"),
        "messages": messages,
    }


@router.get("/tasks/{task_id}/llm-requests")
async def list_task_llm_requests(task_id: str, request: Request, limit: int = 50) -> dict:
    """读取该任务的模型请求/尝试日志（只含摘要与哈希，不含正文）。

    借鉴 deepseek-harness 的 request/header + assistant/attempt：
    用于回答"这次用了哪个模型、带了多少历史/工具、结果是成功还是失败"。
    """
    from services.llm_request_log import list_llm_requests

    items = await list_llm_requests(getattr(request.app.state, "db", None), task_id, limit)
    return {"task_id": task_id, "items": items}
