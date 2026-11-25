"""routes/roles.py —— 临时角色转化 API（pending/promote/reject）

从 main.py 提取（原 ~67 行内联端点），Phase 30。
Phase 47: db.execute → repository 层。
Phase 47 修复（2.3）：2 个端点改用 Pydantic 模型校验。
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from schemas.roles import PromoteTempRoleRequest, RejectTempRoleRequest

router = APIRouter(prefix="/api/v1", tags=["roles"])


async def _parse_request(request: Request, model_cls):
    """解析 + 校验请求体。返回 (model_instance, error_dict)。

    与 routes/skills.py._parse_request 同模式，保留原响应格式。
    """
    try:
        body = await request.json()
    except Exception:
        return None, {"ok": False, "error": "Invalid JSON"}
    try:
        return model_cls(**body), None
    except ValidationError as e:
        try:
            err = e.errors()[0]
            loc = ".".join(str(x) for x in err.get("loc", []))
            msg = err.get("msg", "invalid")
            return None, {"ok": False, "error": f"Field '{loc}': {msg}"}
        except Exception:
            return None, {"ok": False, "error": "Validation failed"}


@router.get("/roles/pending")
async def list_pending_temp_roles(request: Request):
    """列出待转化的临时角色（score >= 0.7）"""
    from repositories import list_pending_temp_roles as _impl
    return await _impl(request.app.state.db)


@router.post("/roles/promote")
async def promote_temp_role(request: Request):
    """将临时角色转化为正式角色。Body: {node_id, role_name, prompt_template}"""
    req, err = await _parse_request(request, PromoteTempRoleRequest)
    if err:
        raise HTTPException(400, err["error"])
    role_name = req.role_name
    prompt = req.prompt_template or f"## 角色\n{role_name}"

    from repositories import promote_temp_role as _impl
    rid = await _impl(request.app.state.db, role_name, prompt, req.node_id)
    return {"ok": True, "role_id": rid, "role_name": role_name}


@router.post("/roles/reject")
async def reject_temp_role(request: Request):
    """拒绝临时角色转化。Body: {node_id}"""
    req, err = await _parse_request(request, RejectTempRoleRequest)
    if err:
        raise HTTPException(400, err["error"])
    node_id = req.node_id
    from repositories import reject_temp_role as _impl
    await _impl(request.app.state.db, node_id)
    return {"ok": True, "rejected": node_id}
