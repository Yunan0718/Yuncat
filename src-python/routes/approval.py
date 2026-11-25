"""routes/approval.py —— Phase 107: 统一审批 Inbox 路由

提供 REST API 供前端 InboxDrawer 查询/操作审批项。
WS 实时推送（approval_required / approval_resolved）由 ws.py 处理。
"""
import logging

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])
_log = logging.getLogger("yuncat.approval")


def _get_service(request: Request):
    svc = getattr(request.app.state, "approval_inbox", None)
    if svc is None:
        raise HTTPException(503, "ApprovalInboxService not initialized")
    return svc


@router.get("/pending")
async def list_pending(
    request: Request,
    project_id: str | None = None,
    task_id: str | None = None,
    limit: int = 100,
):
    """查询待审批项——status='pending' 且未过期。"""
    svc = _get_service(request)
    if limit < 1 or limit > 500:
        limit = 100
    return await svc.get_pending(project_id=project_id, task_id=task_id, limit=limit)


@router.get("/history")
async def list_history(
    request: Request,
    project_id: str | None = None,
    limit: int = 50,
):
    """查询已处理历史——status IN ('resolved', 'rejected', 'expired')。"""
    svc = _get_service(request)
    if limit < 1 or limit > 500:
        limit = 50
    return await svc.get_history(project_id=project_id, limit=limit)


# 注意：字面量路径必须声明在 /{token} 之前——Starlette 按声明顺序匹配，
# 否则 /grants 会被 /{token} 吃掉（token="grants"），/batch/approve 同理。
# 2026-09-17：授权管理页签一直 404，就是这个顺序问题。
@router.post("/batch/approve")
async def approve_batch(payload: dict, request: Request):
    """Batch 3.2: 批量批准并执行。

    payload: {tokens: list[str], remember: bool}
    返回 {status:"ok", results: [{token, success, result|error}]}
    """
    svc = _get_service(request)
    tokens = payload.get("tokens") or []
    if not isinstance(tokens, list) or not tokens:
        raise HTTPException(400, "Missing or empty tokens")
    results = await svc.resolve_approved_batch(
        tokens, remember=bool(payload.get("remember", False)))
    return {"status": "ok", "results": results}


@router.post("/batch/reject")
async def reject_batch(payload: dict, request: Request):
    """Batch 3.2: 批量拒绝。

    payload: {tokens: list[str], reason: str}
    """
    svc = _get_service(request)
    tokens = payload.get("tokens") or []
    if not isinstance(tokens, list) or not tokens:
        raise HTTPException(400, "Missing or empty tokens")
    results = await svc.resolve_rejected_batch(
        tokens, reason=str(payload.get("reason", "")))
    return {"status": "ok", "results": results}


@router.get("/grants")
async def list_grants(
    request: Request,
    include_expired: bool = False,
    include_revoked: bool = False,
):
    """列出持久化授权（standing grants）。

    默认只返回当前有效的项；include_expired/include_revoked 可放宽用于审计。
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "Database not initialized")
    from services.standing_grants import list_grants as _list_grants
    return await _list_grants(
        db, include_expired=bool(include_expired), include_revoked=bool(include_revoked))


@router.post("/grants/{grant_id}/revoke")
async def revoke_grant(grant_id: str, request: Request):
    """撤销单条持久化授权（软删除，保留审计痕迹）。"""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "Database not initialized")
    from services.standing_grants import revoke as _revoke
    ok = await _revoke(db, grant_id)
    if not ok:
        raise HTTPException(404, "Grant not found or already revoked")
    return {"status": "ok", "revoked": grant_id}
@router.get("/{token}")
async def get_approval(token: str, request: Request):
    """按 token 查询单条审批详情。"""
    svc = _get_service(request)
    item = await svc.get_by_token(token)
    if not item:
        raise HTTPException(404, "Approval not found")
    return item


@router.post("/{token}/approve")
async def approve(token: str, payload: dict, request: Request):
    """批准并执行——包装 dispatch_confirm（原子 UPDATE...RETURNING）。

    若 token 已处理/过期/不存在，返回 409 Conflict。
    payload 可选 remember + tool_name，支持 "Always allow in this session"。
    """
    svc = _get_service(request)
    try:
        result = await svc.resolve_approved(
            token,
            remember=bool(payload.get("remember", False)),
            tool_name=str(payload.get("tool_name", "")),
        )
    except ValueError as e:
        # token 已处理/过期/无效
        raise HTTPException(409, str(e))
    return {"status": "ok", "token": token, "result": result}


@router.post("/{token}/reject")
async def reject(token: str, payload: dict, request: Request):
    """拒绝——UPDATE status='rejected'，不执行 handler。"""
    svc = _get_service(request)
    reason = payload.get("reason", "")
    result = await svc.resolve_rejected(token, reason)
    if result.get("error") == "already-resolved":
        raise HTTPException(409, "Approval already resolved or expired")
    return {"status": "ok", "token": token, "result": result}


