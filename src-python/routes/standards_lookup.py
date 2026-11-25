"""routes/standards_lookup.py —— Phase 85 标准/文档查询 Agent API

1 个端点（prefix /api/v1/tasks/{task_id}）：
- POST "/lookup-standards" —— 手动触发 4 步降级查询（force=True 跳过冷却）

StandardsLookupAgent 单例由 lifespan 创建并经 set_standards_lookup_agent 注入全局。
本路由供前端 Supervisor 驾驶舱或调试用例手动触发文档查询用。

4 步降级链（agent.lookup 内部实现）：
  1. adapter.invoke 生成英文查询 + 候选库
  2. Context7 查询库文档
  3. OpenClaw openclaw_web_search 降级
  4. adapter.invoke 通用建议（明确不可编造 API）
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

_log = logging.getLogger("yuncat.standards.route")

router = APIRouter(
    prefix="/api/v1/tasks/{task_id}",
    tags=["standards_lookup"],
)

_VALID_DOMAINS = ("general", "python", "frontend", "database", "devops", "system")


class LookupRequest(BaseModel):
    """POST /lookup-standards 请求体"""
    problem_description: str = Field(
        ..., description="技术问题描述（错误信息/不确定点）",
    )
    domain: str = Field(
        "general", description="问题领域: general/python/frontend/database/devops/system",
    )


@router.post("/lookup-standards")
async def trigger_lookup(
    task_id: str, body: LookupRequest, request: Request,
):
    """手动触发标准/文档查询——force=True 跳过冷却窗口

    Returns:
        StandardsLookupAgent.lookup() 结果
        ({ok, sources, summary, recommendations, source_type, ...})
    """
    from services.long_task.standards_lookup_agent import get_standards_lookup_agent
    agent = get_standards_lookup_agent()
    if agent is None:
        raise HTTPException(status_code=503, detail="StandardsLookupAgent 未初始化")
    if not body.problem_description or not body.problem_description.strip():
        raise HTTPException(status_code=400, detail="problem_description 不能为空")
    if body.domain not in _VALID_DOMAINS:
        raise HTTPException(
            status_code=400,
            detail=f"非法 domain: {body.domain}，必须为 {'/'.join(_VALID_DOMAINS)}",
        )
    try:
        result = await agent.lookup(
            problem_description=body.problem_description,
            domain=body.domain,
            force=True,  # 手动触发跳过冷却
        )
    except Exception as e:
        _log.exception(
            "trigger_lookup failed: task_id=%s domain=%s",
            task_id, body.domain,
        )
        raise HTTPException(
            status_code=500, detail=f"标准查询失败: {e}",
        ) from e
    return {
        "ok": True, "task_id": task_id,
        "sources": result.get("sources", []),
        "summary": result.get("summary", ""),
        "recommendations": result.get("recommendations", []),
        "source_type": result.get("source_type", ""),
    }
