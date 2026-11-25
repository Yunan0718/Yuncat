"""routes/preferences.py —— B: 用户偏好 REST 管理接口

提供「可视化 + 可管理」入口，补齐偏好能力的「三无」缺口
（不可见 / 不可管理）。与 standing grants 修复同一思路：
捕获后既能被系统提示词注入（active use），也能在设置页查看/删除。
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/preferences", tags=["preferences"])


class CaptureReq(BaseModel):
    text: str
    category: str | None = None


def _workspace_pid() -> str:
    from security.sandbox import get_workspace
    return os.path.normcase(str(get_workspace()))


@router.get("")
async def list_prefs(request: Request):
    db = request.app.state.db
    from services.user_preferences import list_preferences
    pid = _workspace_pid()
    rows = await list_preferences(db, project_id=pid)
    return {"ok": True, "preferences": rows}


@router.post("")
async def capture(req: CaptureReq, request: Request):
    db = request.app.state.db
    from services.user_preferences import capture_preference
    pid = _workspace_pid()
    try:
        node = await capture_preference(
            db, req.text, project_id=pid, category=req.category or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "preference": node}


@router.delete("/{node_id}")
async def delete_pref(node_id: str, request: Request):
    db = request.app.state.db
    from services.user_preferences import delete_preference
    ok = await delete_preference(db, node_id)
    if not ok:
        raise HTTPException(status_code=404, detail="preference not found")
    return {"ok": True}
