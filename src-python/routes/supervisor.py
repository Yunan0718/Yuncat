"""routes/supervisor.py — LoopSupervisor metrics + dashboard"""
import logging
from fastapi import APIRouter, HTTPException, Request

_log = logging.getLogger("yuncat.supervisor.route")
router = APIRouter(prefix="/api/v1/supervisor", tags=["supervisor"])


@router.get("/status")
async def supervisor_status(request: Request):
    """返回所有 Loop 的运行时状态 + metrics — 供 Dashboard 使用。"""
    supervisor = getattr(request.app.state, "supervisor", None)
    if not supervisor:
        raise HTTPException(400, "Supervisor not initialized")

    loops = []
    for lid, loop in supervisor._loops.items():
        spec = supervisor._specs.get(lid)
        loops.append({
            "loop_id": lid,
            "status": loop.status,
            "loop_type": spec.loop_type if spec else "unknown",
            "interval": spec.interval if spec else 0,
            "priority": spec.priority if spec else 0,
            "last_run": loop.last_run or 0,
            "last_ok": loop.last_ok or 0,
            "restart_count": loop.restart_count,
            "error_count": loop.error_count,
            "iteration_count": loop.iteration_count,
            "last_error": loop.last_error or "",
            "strategy": spec.strategy.value if spec and hasattr(spec.strategy, 'value') else "permanent",
        })

    return {
        "ok": True,
        "total": len(loops),
        "running": sum(1 for l in loops if l["status"] == "running"),
        "degraded": sum(1 for l in loops if l["status"] == "degraded"),
        "loops": sorted(loops, key=lambda l: -l["priority"]),
    }


@router.post("/loops/{loop_id}/restart")
async def restart_loop(loop_id: str, request: Request):
    """手动重启一个 Loop"""
    supervisor = getattr(request.app.state, "supervisor", None)
    if not supervisor:
        raise HTTPException(400, "Supervisor not initialized")
    if loop_id not in supervisor._specs:
        raise HTTPException(404, f"Loop '{loop_id}' not found")
    try:
        await supervisor.start_one(loop_id)
        return {"ok": True, "loop_id": loop_id}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/loops/{loop_id}/interval")
async def set_loop_interval(loop_id: str, interval: int, request: Request):
    """运行时调整 Loop 间隔（秒）。

    边界取 config.LOOP_INTERVAL_*：interval=0 会让循环以最快速度空转（CPU 打满），
    超大值则等于把循环停掉——两者都不是"调间隔"，直接拒绝。
    """
    from runtime_bounds import LOOP_INTERVAL_MAX_SEC, LOOP_INTERVAL_MIN_SEC

    if not LOOP_INTERVAL_MIN_SEC <= interval <= LOOP_INTERVAL_MAX_SEC:
        raise HTTPException(
            400,
            f"interval must be between {LOOP_INTERVAL_MIN_SEC} "
            f"and {LOOP_INTERVAL_MAX_SEC} seconds",
        )
    supervisor = getattr(request.app.state, "supervisor", None)
    if not supervisor:
        raise HTTPException(400, "Supervisor not initialized")
    ok = supervisor.set_interval(loop_id, interval)
    if not ok:
        raise HTTPException(404, f"Loop '{loop_id}' not found")
    return {"ok": True, "loop_id": loop_id, "interval": interval}
