"""routes/evolution.py —— Batch 5.3: Self-Evolution 质量门控审批 API

端点：
- GET  /evolution/pending
- GET  /evolution/pending/{pending_id}
- POST /evolution/pending/{pending_id}/approve
- POST /evolution/pending/{pending_id}/reject
"""
import logging

from fastapi import APIRouter, HTTPException, Request

from repositories import evolution_repo

router = APIRouter(prefix="/api/v1/evolution", tags=["evolution"])
_log = logging.getLogger("yuncat.routes.evolution")


@router.get("/pending")
async def list_pending(status: str | None = None, limit: int = 50, request: Request = None):
    """列出 Self-Evolution 待审核/已审核安装记录。"""
    db = request.app.state.db
    items = await evolution_repo.list_pending_installs(db, status=status, limit=limit)
    return {"ok": True, "items": items}


@router.get("/pending/{pending_id}")
async def get_pending(pending_id: str, request: Request):
    """获取单条 pending install 详情。"""
    db = request.app.state.db
    item = await evolution_repo.get_pending_install(db, pending_id)
    if not item:
        raise HTTPException(404, "Pending install not found")
    return {"ok": True, "item": item}


@router.post("/pending/{pending_id}/approve")
async def approve_pending(pending_id: str, request: Request):
    """批准 pending install，调用 SkillRegistry.install_from_dir 激活 skill。"""
    db = request.app.state.db
    item = await evolution_repo.get_pending_install(db, pending_id)
    if not item:
        raise HTTPException(404, "Pending install not found")
    if item["status"] != "pending":
        raise HTTPException(400, f"Cannot approve install with status '{item['status']}'")

    skill_dir = item["skill_dir"]
    skill_registry = request.app.state.skill_registry

    # 安装前再次检测冲突（可能自生成后用户已安装同名 skill）
    skill = await skill_registry.install_from_dir(skill_dir)
    if not skill:
        raise HTTPException(400, "Failed to install skill from generated directory")

    ok = await evolution_repo.update_pending_status(db, pending_id, "approved")
    if not ok:
        raise HTTPException(500, "Failed to update pending status")

    _log.info("Approved evolution pending install %s -> skill %s", pending_id, skill.id)
    return {"ok": True, "skill": skill.to_dict()}


@router.post("/pending/{pending_id}/reject")
async def reject_pending(pending_id: str, request: Request):
    """拒绝 pending install。"""
    db = request.app.state.db
    item = await evolution_repo.get_pending_install(db, pending_id)
    if not item:
        raise HTTPException(404, "Pending install not found")
    if item["status"] != "pending":
        raise HTTPException(400, f"Cannot reject install with status '{item['status']}'")

    ok = await evolution_repo.update_pending_status(db, pending_id, "rejected")
    if not ok:
        raise HTTPException(500, "Failed to update pending status")

    _log.info("Rejected evolution pending install %s", pending_id)
    return {"ok": True}
