"""routes/workflows.py —— 工作流 CRUD + 运行 + 恢复 + 模板"""

import json as _json
import logging
import time as _time
import uuid as _uuid

from fastapi import APIRouter, HTTPException, Request

from config import WORKFLOW_RUNS_LIST_LIMIT

router = APIRouter(prefix="/api/v1", tags=["workflows"])
_log = logging.getLogger("yuncat.workflows")


@router.post("/workflows")
async def create_workflow(payload: dict, request: Request):
    wid = await request.app.state.workflow_service.create(payload)
    return {"workflow_id": wid}


@router.put("/workflows/{workflow_id}")
async def update_workflow(workflow_id: str, payload: dict, request: Request):
    """就地更新工作流（不创建新记录）"""
    name = payload.get("name", "")
    graph = payload.get("graph", {})
    if isinstance(graph, dict):
        graph = _json.dumps(graph, ensure_ascii=False)
    from repositories import upsert_workflow
    await upsert_workflow(request.app.state.db, workflow_id, name, graph)
    return {"status": "ok", "workflow_id": workflow_id}


@router.get("/workflows")
async def list_workflows(
    request: Request,
    scope: str | None = None,
    project_id: str | None = None,
    category: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """Phase 99: 支持按 scope/project_id/category 过滤。

    - scope: 'global' | 'project'
    - project_id: 当 scope='project' 时指定项目
    - category: 'automation' | 'template' | 'agent_skill' | 'custom'
    - limit/offset: Batch 7.10 分页参数
    """
    return await request.app.state.workflow_service.list_all(
        scope=scope, project_id=project_id, category=category, limit=limit, offset=offset,
    )



@router.get("/workflows/templates")
async def list_templates(request: Request):
    return await request.app.state.workflow_service.list_templates()



@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str, request: Request):
    wf = await request.app.state.workflow_service.get(workflow_id)
    if not wf:
        raise HTTPException(404, "Workflow not found")
    return wf


@router.delete("/workflows/{workflow_id}")
async def delete_workflow(workflow_id: str, request: Request):
    from repositories import delete_workflow_cascade
    await delete_workflow_cascade(request.app.state.db, workflow_id)
    return {"status": "ok"}


@router.post("/workflows/{workflow_id}/run")
async def run_workflow(workflow_id: str, payload: dict, request: Request):
    run_id = await request.app.state.workflow_service.run(
        workflow_id, payload.get("initial_input", ""),
    )
    return {"run_id": run_id}


@router.get("/workflows/runs/{run_id}")
async def get_workflow_run(run_id: str, request: Request):
    run = await request.app.state.workflow_service.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return run


@router.post("/workflows/{workflow_id}/resume")
async def resume_workflow(workflow_id: str, payload: dict, request: Request):
    """从快照或事件日志恢复失败的工作流。action: retry / skip / retry_with_inputs"""
    run_id = payload.get("run_id")
    if not run_id:
        raise HTTPException(400, "Missing 'run_id'")
    action = payload.get("action", "retry")
    if action not in ("retry", "skip", "retry_with_inputs"):
        raise HTTPException(400, "action must be retry, skip, or retry_with_inputs")

    engine = request.app.state.workflow_service.engine
    snap = await engine.recover_from_snapshot(run_id)
    node_states = snap.get("node_states", {})
    node_results = snap.get("node_results", {})
    node_overrides: dict = {}

    for nid, state in node_states.items():
        if state == "error":
            if action == "skip":
                node_states[nid] = "success"
                node_results[nid] = {"output": "(skipped by user)", "skipped": True}
            elif action == "retry_with_inputs":
                overrides = (payload.get("node_overrides") or {}).get(nid, {})
                if overrides:
                    node_overrides[nid] = overrides
                node_states[nid] = "pending"
            else:
                node_states[nid] = "pending"

    # Phase 87 审查 C-2: 标记 running + try/except 兜底 + finalize
    # （与 WorkflowService._run_and_finalize 对齐，避免 resume 后状态停留在
    #   paused/failed，且 engine.run 抛异常时返回结构化错误而非裸 500）
    # D5: 原子 CAS——仅 paused/failed/partial_error 可恢复，防止 running 中的 run 被双执行
    db = request.app.state.db
    now = int(_time.time())
    cursor = await db.execute(
        "UPDATE workflow_runs SET status='running', finished_at=NULL, updated_at=? "
        "WHERE id=? AND status IN ('paused','failed','partial_error')",
        (now, run_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        # 状态不匹配——run 可能正在 running 或已 completed
        cur2 = await db.execute("SELECT status FROM workflow_runs WHERE id=?", (run_id,))
        row = await cur2.fetchone()
        cur_status = row[0] if row else "not found"
        raise HTTPException(409, f"Run {run_id} cannot be resumed (status={cur_status})")

    try:
        result = await engine.run(
            workflow_id, "", run_id=run_id,
            resume_from_snapshot=snap,
            node_overrides=node_overrides or None,
        )
        status = result.get("status", "partial_error")
        if status not in ("completed", "partial_error", "paused"):
            status = "partial_error"
    except Exception as e:
        _log.exception("Workflow resume %s failed", run_id)
        status = "failed"
        result = {"error": str(e)}

    now = int(_time.time())
    await db.execute(
        "UPDATE workflow_runs SET status=?, node_results=?, finished_at=?, updated_at=? WHERE id=?",
        (status, _json.dumps(result), now, now, run_id),
    )
    await db.commit()
    return {"status": "ok", "results": result, "run_status": status}


@router.post("/workflows/templates/{template_id}/edit")
async def edit_template(template_id: str, payload: dict, request: Request = None):
    """Phase 18: 编辑内置工作流模板"""
    graph = payload.get("graph")
    name = payload.get("name")
    if graph and name:
        graph_str = _json.dumps(graph, ensure_ascii=False) if isinstance(graph, dict) else graph
        await request.app.state.db.execute(
            "UPDATE workflows SET name=?, graph=? WHERE id=? AND is_template=1",
            (name, graph_str, template_id),
        )
        await request.app.state.db.commit()
        return {"status": "ok"}
    raise HTTPException(400, "Missing 'graph' or 'name'")


# ---- Phase 31 fix: DAG 运行历史 ----

@router.get("/workflows/{workflow_id}/runs")
async def list_workflow_runs(workflow_id: str, limit: int = WORKFLOW_RUNS_LIST_LIMIT, request: Request = None):
    """P2: 列出某工作流的所有运行历史，含每个节点的输出/错误"""
    cursor = await request.app.state.db.execute(
        "SELECT id, status, error_message, node_results, started_at, finished_at "
        "FROM workflow_runs WHERE workflow_id=? ORDER BY started_at DESC LIMIT ?",
        (workflow_id, limit),
    )
    rows = await cursor.fetchall()
    import json as _json
    runs = []
    for r in rows:
        node_results = {}
        try:
            node_results = _json.loads(r[3] or "{}")
        except Exception:
            _log.debug("解析工作流节点结果 JSON 失败", exc_info=True)
        runs.append({
            "run_id": r[0], "status": r[1], "error_message": r[2],
            "node_results": node_results,
            "started_at": r[4], "finished_at": r[5],
        })
    return {"workflow_id": workflow_id, "runs": runs}


@router.post("/workflows/runs/{run_id}/cancel")
async def cancel_workflow_run(run_id: str, request: Request):
    """取消正在运行的工作流"""
    await request.app.state.db.execute(
        "UPDATE workflow_runs SET status='cancelled', finished_at=? WHERE id=? AND status='running'",
        (int(_time.time()), run_id),
    )
    await request.app.state.db.commit()
    return {"status": "ok", "run_id": run_id}


@router.post("/workflows/runs/{run_id}/pause")
async def pause_workflow_run(run_id: str, request: Request):
    """Phase 51-E: 暂停正在运行的工作流"""
    result = await request.app.state.workflow_service.pause_run(run_id)
    if result.get("status") == "error":
        raise HTTPException(400, result.get("error", "Pause failed"))
    return result


@router.post("/workflows/runs/{run_id}/resume")
async def resume_workflow_run(run_id: str, request: Request):
    """Phase 51-E: 从暂停状态恢复工作流"""
    resumed_run_id = await request.app.state.workflow_service.resume_run(run_id)
    return {"status": "ok", "run_id": resumed_run_id}
