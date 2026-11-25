"""routes/workflow_triggers.py —— Phase 101: 工作流触发器 REST API

端点:
  GET    /api/v1/workflows/{workflow_id}/triggers          列出工作流的触发器
  POST   /api/v1/workflows/{workflow_id}/triggers          创建触发器
  DELETE /api/v1/workflows/{workflow_id}/triggers/{trigger_id}  删除触发器
  POST   /api/v1/workflows/{workflow_id}/triggers/{trigger_id}/fire  手动触发
"""
import json
import logging

from fastapi import APIRouter, Request, HTTPException

_log = logging.getLogger("yuncat.routes.workflow_triggers")

router = APIRouter(prefix="/api/v1", tags=["workflow-triggers"])


@router.get("/workflows/{workflow_id}/triggers")
async def list_triggers(workflow_id: str, request: Request):
    svc = getattr(request.app.state, "workflow_trigger_service", None)
    if svc is None:
        raise HTTPException(503, "Workflow trigger service not available")
    return await svc.list_triggers(workflow_id)


@router.post("/workflows/{workflow_id}/triggers")
async def create_trigger(workflow_id: str, request: Request):
    svc = getattr(request.app.state, "workflow_trigger_service", None)
    if svc is None:
        raise HTTPException(503, "Workflow trigger service not available")
    body = await request.json()
    trigger_type = body.get("trigger_type", "manual")
    if trigger_type not in ("event", "cron", "manual"):
        raise HTTPException(400, f"Invalid trigger_type: {trigger_type}")
    config = body.get("config", {})
    enabled = body.get("enabled", True)
    trigger_id = await svc.create_trigger(workflow_id, trigger_type, config, enabled)
    return {"trigger_id": trigger_id, "workflow_id": workflow_id}


@router.delete("/workflows/{workflow_id}/triggers/{trigger_id}")
async def delete_trigger(workflow_id: str, trigger_id: str, request: Request):
    svc = getattr(request.app.state, "workflow_trigger_service", None)
    if svc is None:
        raise HTTPException(503, "Workflow trigger service not available")
    deleted = await svc.delete_trigger(trigger_id)
    if not deleted:
        raise HTTPException(404, "Trigger not found")
    return {"deleted": True, "trigger_id": trigger_id}


@router.post("/workflows/{workflow_id}/triggers/{trigger_id}/fire")
async def fire_trigger(workflow_id: str, trigger_id: str, request: Request):
    """手动触发——manual 类型触发器或测试用。"""
    svc = getattr(request.app.state, "workflow_trigger_service", None)
    if svc is None:
        raise HTTPException(503, "Workflow trigger service not available")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    initial_input = body.get("initial_input", "")
    result = await svc.fire_trigger(trigger_id, initial_input)
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "fire failed"))
    return result
