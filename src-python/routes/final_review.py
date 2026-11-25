"""routes/final_review.py —— Phase 83 终审 Agent API

1 个端点（prefix /api/v1/tasks/{task_id}）：
- POST "/review" —— 手动触发终审（force=True 跳过冷却窗口）

FinalReviewAgent 单例由 lifespan 创建并经 set_final_review_agent 注入全局。
本路由供前端 Supervisor 驾驶舱「审查」Tab 手动触发审查用。
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

_log = logging.getLogger("yuncat.final_review.route")

router = APIRouter(
    prefix="/api/v1/tasks/{task_id}",
    tags=["final-review"],
)


class ReviewRequest(BaseModel):
    """POST /review 请求体"""
    stage: str = Field(
        ..., description="审查阶段: pre_execution / post_execution / pre_dangerous_tool",
    )
    context: dict = Field(
        default_factory=dict,
        description="审查上下文（goal/step_title/output/tool_name/args 等）",
    )
    step_id: str | None = Field(
        None, description="plan_step id（pre/post_execution 用，冷却按 step 隔离）",
    )


@router.post("/review")
async def trigger_review(task_id: str, body: ReviewRequest, request: Request):
    """手动触发终审——force=True 跳过冷却窗口

    Returns:
        FinalReviewAgent.review() 结果（{pass, score, issues, action, ...}）
    """
    from services.long_task.final_review_agent import get_final_review_agent
    agent = get_final_review_agent()
    if agent is None:
        raise HTTPException(status_code=503, detail="FinalReviewAgent 未初始化")
    if body.stage not in ("pre_execution", "post_execution", "pre_dangerous_tool"):
        raise HTTPException(
            status_code=400,
            detail=f"非法 stage: {body.stage}，必须为 pre_execution/post_execution/pre_dangerous_tool",
        )
    try:
        review = await agent.review(
            task_id=task_id,
            stage=body.stage,
            context=body.context,
            step_id=body.step_id,
            force=True,  # 手动触发跳过冷却
        )
    except Exception as e:
        _log.exception("trigger_review failed: task_id=%s stage=%s", task_id, body.stage)
        raise HTTPException(status_code=500, detail=f"审查失败: {e}") from e
    return {"ok": True, "task_id": task_id, "review": review}
