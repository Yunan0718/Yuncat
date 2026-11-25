"""routes/task_supervisor.py —— Phase 81 长任务监控 API

6 个端点（prefix /api/v1/tasks/{task_id}/supervisor）：
- GET  ""          —— 返回 SupervisorView（实时组装）
- POST "/inject"   —— 注入约束（写 plan_steps.review + 入队下一步）
- POST "/pause"    —— 暂停长任务（不打断当前 spawn_subagent）
- POST "/resume"   —— 恢复长任务
- POST "/spec"     —— 重新分解（可选 new_goal，保留 completed 节点）
- POST "/ask"      —— 向 Supervisor 提问（轻量 LLM 回答）

TaskSupervisor 实例由 lifespan 创建并注入 app.state.task_supervisor。
与 routes/supervisor.py（LoopSupervisor，system-level）层级不同——本路由是 task-level。
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

_log = logging.getLogger("yuncat.task_supervisor.route")

router = APIRouter(
    prefix="/api/v1/tasks/{task_id}/supervisor",
    tags=["task-supervisor"],
)


def _get_supervisor(request: Request):
    """取 app.state.task_supervisor，未初始化抛 503"""
    supervisor = getattr(request.app.state, "task_supervisor", None)
    if supervisor is None:
        raise HTTPException(status_code=503, detail="TaskSupervisor 未初始化")
    return supervisor


class InjectRequest(BaseModel):
    """POST /inject 请求体"""
    constraint: str = Field(..., description="注入的约束内容（自然语言）")


class SpecRequest(BaseModel):
    """POST /spec 请求体"""
    new_goal: str | None = Field(
        None, description="新的确认目标；None 表示用当前 confirmed_goal 重新分解",
    )


class AskRequest(BaseModel):
    """POST /ask 请求体"""
    question: str = Field(..., description="向 Supervisor 提的问题")


@router.get("")
async def get_supervisor_view(task_id: str, request: Request):
    """返回 SupervisorView——实时从 task_state.long_task + plan_steps + review 组装

    Returns:
        SupervisorView.to_dict() —— 含 status/goal/plan_tree/current_step/review_log 等
    """
    supervisor = _get_supervisor(request)
    try:
        view = await supervisor.get_view(task_id)
    except Exception as e:
        _log.exception("get_view failed: task_id=%s", task_id)
        raise HTTPException(status_code=500, detail=f"组装视图失败: {e}") from e
    return view.to_dict()


@router.post("/inject")
async def inject_constraint(task_id: str, body: InjectRequest, request: Request):
    """注入约束——委托 orchestrator.inject_constraint

    约束写入当前 running 节点的 plan_steps.review.constraints + 入队下一步 prompt。
    """
    supervisor = _get_supervisor(request)
    try:
        await supervisor.inject_constraint(task_id, body.constraint)
    except Exception as e:
        _log.exception("inject_constraint failed: task_id=%s", task_id)
        raise HTTPException(status_code=500, detail=f"注入约束失败: {e}") from e
    return {"ok": True, "task_id": task_id, "constraint": body.constraint}


@router.post("/pause")
async def pause_task(task_id: str, request: Request):
    """暂停长任务——下一步前阻塞，不打断当前 spawn_subagent"""
    supervisor = _get_supervisor(request)
    try:
        await supervisor.pause(task_id)
    except Exception as e:
        _log.exception("pause failed: task_id=%s", task_id)
        raise HTTPException(status_code=500, detail=f"暂停失败: {e}") from e
    return {"ok": True, "task_id": task_id, "status": "paused"}


@router.post("/resume")
async def resume_task(task_id: str, request: Request):
    """恢复长任务——释放阻塞；协程已丢失时从 pending 步骤重入，失败时如实返回

    Batch 3b B2: 不再无条件返回 ok:true 假成功。orchestrator.resume 返回真实
    结果（resumed / running / restarted / not_long_task / phase_x 等）。
    """
    supervisor = _get_supervisor(request)
    try:
        result = await supervisor.resume(task_id)
    except Exception as e:
        _log.exception("resume failed: task_id=%s", task_id)
        raise HTTPException(status_code=500, detail=f"恢复失败: {e}") from e
    if not result.get("ok"):
        # 409 Conflict——任务当前状态不允许恢复（非长任务/无可续执行树/已结束）
        raise HTTPException(status_code=409, detail=result.get("error", "无法恢复"))
    return {"ok": True, "task_id": task_id, "status": result.get("status", "running")}


@router.post("/spec")
async def replan_task(task_id: str, body: SpecRequest, request: Request):
    """重新分解——委托 orchestrator.replan（保留 completed 节点）

    new_goal=None 时用当前 confirmed_goal 重新分解。
    """
    supervisor = _get_supervisor(request)
    try:
        await supervisor.spec(task_id, new_goal=body.new_goal)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        _log.exception("spec failed: task_id=%s", task_id)
        raise HTTPException(status_code=500, detail=f"重新分解失败: {e}") from e
    return {"ok": True, "task_id": task_id, "new_goal": body.new_goal}


@router.post("/ask")
async def ask_supervisor(task_id: str, body: AskRequest, request: Request):
    """向 Supervisor 提问——轻量 LLM 回答，不进主 Agent 循环

    Returns:
        {"task_id": ..., "question": ..., "answer": "..."}
    """
    supervisor = _get_supervisor(request)
    try:
        answer = await supervisor.ask_supervisor(task_id, body.question)
    except Exception as e:
        _log.exception("ask_supervisor failed: task_id=%s", task_id)
        raise HTTPException(status_code=500, detail=f"提问失败: {e}") from e
    return {"task_id": task_id, "question": body.question, "answer": answer}
