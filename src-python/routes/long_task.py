"""routes/long_task.py —— Phase 79 长任务分解 API

3 个端点：
- POST /api/v1/tasks/{task_id}/decompose          —— 首次分解（输入 confirmed_goal）
- GET  /api/v1/tasks/{task_id}/decomposition       —— 查询分解树
- POST /api/v1/tasks/{task_id}/nodes/{node_id}/decompose  —— 重分解子树

TaskDecomposer 实例由 lifespan 创建并注入 app.state.task_decomposer。
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from repositories import get_plan_tree_by_task
from services.long_task.orchestrator import get_orchestrator
from services.long_task.window_rollover import (
    get_pending_arbitrations,
    resolve_arbitration,
)

_log = logging.getLogger("yuncat.long_task.route")

router = APIRouter(prefix="/api/v1", tags=["long_task"])


class DecomposeRequest(BaseModel):
    """POST /tasks/{task_id}/decompose 请求体"""
    confirmed_goal: str = Field(..., description="GoalClarifier 提炼的确认目标")
    agent_id: str = Field("hermes", description="使用的 Agent")


@router.post("/tasks/{task_id}/decompose")
async def decompose_task(task_id: str, body: DecomposeRequest, request: Request):
    """Phase 79: 将确认目标分解为多级子任务树，写入 plan_steps

    Returns:
        {
            "task_id": ...,
            "nodes": [...],
            "root_count": int,
            "total": int,
            "source": "llm" | "fallback"
        }
    """
    decomposer = getattr(request.app.state, "task_decomposer", None)
    if decomposer is None:
        raise HTTPException(status_code=503, detail="TaskDecomposer 未初始化")

    try:
        result = await decomposer.decompose(
            task_id=task_id,
            confirmed_goal=body.confirmed_goal,
            agent_id=body.agent_id,
        )
    except Exception as e:
        _log.exception("decompose_task failed: task_id=%s", task_id)
        raise HTTPException(status_code=500, detail=f"分解失败: {e}") from e

    return {"task_id": task_id, **result}


@router.get("/tasks/{task_id}/decomposition")
async def get_decomposition(task_id: str, request: Request):
    """Phase 79: 查询某任务的完整分解树

    Returns:
        {
            "task_id": ...,
            "nodes": [...],  # plan_steps 列表（按 level、created_at 排序）
            "total": int
        }
    """
    db = request.app.state.db
    nodes = await get_plan_tree_by_task(db, task_id)
    return {"task_id": task_id, "nodes": nodes, "total": len(nodes)}


@router.post("/tasks/{task_id}/nodes/{node_id}/decompose")
async def regenerate_subtree(task_id: str, node_id: str, request: Request):
    """Phase 79: 重新分解某节点的子树

    删除 node_id 的所有 pending 子孙 + 重新调 LLM 分解 + 写入新子节点。
    保留 completed/failed 子孙（历史记录）+ node_id 本身。

    Returns:
        {
            "task_id": ...,
            "node_id": ...,
            "nodes": [...],      # 新写入的子节点
            "deleted_count": int,  # 删除的 pending 子孙数
            "source": "llm" | "fallback"
        }
    """
    decomposer = getattr(request.app.state, "task_decomposer", None)
    if decomposer is None:
        raise HTTPException(status_code=503, detail="TaskDecomposer 未初始化")

    try:
        result = await decomposer.regenerate_subtree(
            task_id=task_id,
            node_id=node_id,
        )
    except ValueError as e:
        # node_id 不存在
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        _log.exception("regenerate_subtree failed: task_id=%s node_id=%s", task_id, node_id)
        raise HTTPException(status_code=500, detail=f"重分解失败: {e}") from e

    return {"task_id": task_id, "node_id": node_id, **result}


# ── 主 Agent 仲裁消费 (闭环"联动"落点) ──
# supervise_round 发现 supervisor/worker 偏离时会置 pending_arbitration + 广播
# long_task_supervisor_escalation; 下面两个端点让主 Yuncat Agent (或 UI) 真正消费:
# 拉取待仲裁清单 + 给出决策 (纠偏约束 / 重新锚定 / 中止 / 忽略) 回灌运行中的长任务。


class ArbitrationDecision(BaseModel):
    """POST /tasks/{task_id}/arbitration 请求体"""
    decision: str = Field(..., description="adjust | reanchor | dismiss | abort")
    directive: str | None = Field(
        None, description="主 Agent 给的纠偏/重新锚定指令 (adjust/reanchor 必填)",
    )


@router.get("/tasks/arbitrations/pending")
async def list_pending_arbitrations(request: Request):
    """拉取所有待主 Agent 仲裁的长任务 (pending_arbitration=True)。

    Returns: { "pending": [ {task_id, pending_arbitration, current_generation,
                              goal_summary, worker_session_id, supervisor_session_id,
                              last_escalation}, ... ] }
    """
    orch = get_orchestrator()
    if orch is None:
        raise HTTPException(status_code=503, detail="LongTaskOrchestrator 未初始化")
    try:
        items = await get_pending_arbitrations(orch)
    except Exception as e:
        _log.exception("list_pending_arbitrations failed")
        raise HTTPException(status_code=500, detail=f"查询待仲裁失败: {e}") from e
    return {"pending": items}


@router.post("/tasks/{task_id}/arbitration")
async def resolve_arbitration_endpoint(task_id: str, body: ArbitrationDecision, request: Request):
    """主 Agent 对一次监督升级做出仲裁决策并回灌运行中的长任务。

    decision:
      - adjust:   把 directive 作为纠偏约束广播回 running 节点
      - reanchor: 用 directive 重新锚定目标 (更新 rollover.goal_summary)
      - dismiss:  仅清除待仲裁标志
      - abort:    置任务 interrupted 并停止执行
    """
    orch = get_orchestrator()
    if orch is None:
        raise HTTPException(status_code=503, detail="LongTaskOrchestrator 未初始化")
    if body.decision in ("adjust", "reanchor") and not body.directive:
        raise HTTPException(status_code=422, detail="adjust/reanchor 需要 directive")
    try:
        result = await resolve_arbitration(
            orch, task_id, decision=body.decision, directive=body.directive,
        )
    except Exception as e:
        _log.exception("resolve_arbitration failed: task_id=%s", task_id)
        raise HTTPException(status_code=500, detail=f"仲裁失败: {e}") from e
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "仲裁未生效"))
    return {"task_id": task_id, **result}
