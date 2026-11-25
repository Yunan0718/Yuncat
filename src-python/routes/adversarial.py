"""routes/adversarial.py —— Phase 84 对抗验证 Agent API

1 个端点（prefix /api/v1/tasks/{task_id}）：
- POST "/challenge" —— 手动触发对抗验证（force=True 跳过冷却窗口）

AdversarialAgent 单例由 lifespan 创建并经 set_adversarial_agent 注入全局。
本路由供前端 Supervisor 驾驶舱「审查」Tab 手动触发对抗验证用。
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

_log = logging.getLogger("yuncat.adversarial.route")

router = APIRouter(
    prefix="/api/v1/tasks/{task_id}",
    tags=["adversarial"],
)

_VALID_STAGES = ("goal_confirmed", "plan_generated", "output_merge")


class ChallengeRequest(BaseModel):
    """POST /challenge 请求体"""
    stage: str = Field(
        ..., description="检查点: goal_confirmed / plan_generated / output_merge",
    )
    plan_text: str = Field(
        ..., description="待验证的文本（目标/计划/产出）",
    )
    task_input: str = Field(
        "", description="原始任务输入（供 reviewer 理解上下文）",
    )
    step_id: str | None = Field(
        None, description="plan_step id（output_merge 用，冷却按 step 隔离）",
    )


@router.post("/challenge")
async def trigger_challenge(task_id: str, body: ChallengeRequest, request: Request):
    """手动触发对抗验证——force=True 跳过冷却窗口

    Returns:
        AdversarialAgent.challenge_plan() 结果
        ({pass, refuted_count, surviving_count, action, reason, ...})
    """
    from services.long_task.adversarial_agent import get_adversarial_agent
    agent = get_adversarial_agent()
    if agent is None:
        raise HTTPException(status_code=503, detail="AdversarialAgent 未初始化")
    if body.stage not in _VALID_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"非法 stage: {body.stage}，必须为 {'/'.join(_VALID_STAGES)}",
        )
    if not body.plan_text or not body.plan_text.strip():
        raise HTTPException(status_code=400, detail="plan_text 不能为空")
    try:
        review = await agent.challenge_plan(
            task_id=task_id,
            stage=body.stage,
            plan_text=body.plan_text,
            task_input=body.task_input,
            step_id=body.step_id,
            force=True,  # 手动触发跳过冷却
        )
    except Exception as e:
        _log.exception(
            "trigger_challenge failed: task_id=%s stage=%s", task_id, body.stage,
        )
        raise HTTPException(status_code=500, detail=f"对抗验证失败: {e}") from e
    return {"ok": True, "task_id": task_id, "review": review}
