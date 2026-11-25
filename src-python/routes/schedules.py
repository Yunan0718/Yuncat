"""routes/schedules.py —— 定时任务管理（DSH schedule 的"人机确认"那一半）

agent 用 cron_schedule / cron_list / cron_delete 工具就能建任务，但用户侧此前
**没有任何入口**：建了哪些定时任务、下次什么时候跑、上次跑成功没有、怎么取消，
全都看不到。DSH 的 schedule 包专门有一个跨会话的任务页面 + 删除确认。

本模块只做两件事（够用且边界清楚）：
- 列出全部状态的定时任务（pending 排最前）；
- 取消一个待执行任务（status → paused，与 cron_delete 语义一致）。
"""
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/v1", tags=["schedules"])


@router.get("/schedules")
async def list_schedules(status: str = "", limit: int = 100, request: Request = None):
    """列出定时任务（默认全部状态；status 可筛选 pending/paused/...）。"""
    from repositories.scheduled_tasks_repo import list_scheduled_tasks_all

    items = await list_scheduled_tasks_all(request.app.state.db, status, limit)
    return {"items": items}


@router.post("/schedules/{schedule_id}/cancel")
async def cancel_schedule(schedule_id: str, request: Request = None):
    """取消一个待执行的定时任务（status → paused）。"""
    from repositories.scheduled_tasks_repo import cancel_scheduled_task

    changed = await cancel_scheduled_task(request.app.state.db, task_id=schedule_id)
    if not changed:
        raise HTTPException(404, "schedule not found or not pending")
    return {"ok": True, "id": schedule_id}
