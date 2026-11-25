"""统一后台 Job 视图——复用 tasks 表与控制接口。"""
import json
import logging

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/v1", tags=["jobs"])
_log = logging.getLogger("yuncat.jobs")

_ACTIVE_STATUSES = ("running", "paused", "paused_budget", "interrupted", "pending")


def _job_row(row) -> dict:
    raw_state = row[7] or "{}"
    try:
        state = json.loads(raw_state)
    except (TypeError, ValueError):
        state = {}
    generated = str(state.get("generated_response") or row[4] or "")
    return {
        "id": row[0], "prompt": row[1] or "", "status": row[2] or "",
        "agent_id": row[3] or "", "session_id": row[5] or "",
        "created_at": row[6], "output_preview": generated[:240],
        "round": state.get("round"), "tool_round": state.get("tool_round"),
        "model_used": state.get("model_used") or {},
        "parent_task_id": row[8],
    }


@router.get("/jobs")
async def list_jobs(
    request: Request, status: str = "active", limit: int = 50, session_id: str = "",
) -> dict:
    limit = max(1, min(int(limit), 200))
    clauses: list[str] = []
    params: list = []
    if status == "active":
        clauses.append(f"status IN ({','.join('?' * len(_ACTIVE_STATUSES))})")
        params.extend(_ACTIVE_STATUSES)
    elif status not in ("all", "terminal"):
        raise HTTPException(400, "status must be active, terminal, or all")
    elif status == "terminal":
        clauses.append("status IN ('completed','failed','cancelled')")
    if session_id:
        clauses.append("session_id=?")
        params.append(session_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    cursor = await request.app.state.db.execute(
        "SELECT id, input, status, agent_id, output, session_id, created_at, task_state, parent_task_id "
        f"FROM tasks {where} ORDER BY COALESCE(updated_at, created_at) DESC LIMIT ?",
        params,
    )
    items = [_job_row(row) for row in await cursor.fetchall()]
    return {"ok": True, "status": status, "items": items}


@router.get("/jobs/{task_id}")
async def get_job(task_id: str, request: Request) -> dict:
    task = await request.app.state.agent_service.get_task(task_id)
    if not task:
        raise HTTPException(404, "Job not found")
    state = await request.app.state.agent_service.get_task_state(task_id)
    return {"ok": True, "job": task, "state": (state or {}).get("task_state", {})}
