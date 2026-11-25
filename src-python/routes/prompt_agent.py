"""routes/prompt_agent.py —— Phase 74 PromptAgent 路由

端点：
- POST /api/v1/prompt-agent/recommend —— 推荐角色/行为/工作流（Phase 74）
- POST /api/v1/prompt-agent/render      —— 渲染最终 prompt 片段（Phase 74）
- POST /api/v1/prompt-agent/optimize    —— 输入框 ✨ 优化（Phase 73-A）
"""
from fastapi import APIRouter, HTTPException, Request

from config import DEFAULT_AGENT_ID  # P1: 避免硬编码 "hermes" 字符串散落

router = APIRouter(prefix="/api/v1/prompt-agent", tags=["prompt-agent"])


def _get_agent(request: Request):
    """获取 PromptAgent 实例——lifespan 已注入 app.state.prompt_agent"""
    agent = getattr(request.app.state, "prompt_agent", None)
    if agent is None:
        raise HTTPException(status_code=503, detail="prompt_agent service not initialized")
    return agent


@router.post("/recommend")
async def recommend(request: Request):
    """推荐角色/行为/工作流模板

    请求体：
        {
            "task_input": "用户任务输入",
            "agent_id": "hermes",       // 可选，默认 "hermes"
            "project_id": ""            // 可选，影响缓存隔离
        }

    返回：
        {
            "role_ids": [...],
            "behavior_ids": [...],
            "workflow_template": "..." | null,
            "reason": "...",
            "confidence": 0.0,
            "source": "llm" | "rule"
        }
    """
    agent = _get_agent(request)
    body = await request.json()

    task_input = body.get("task_input", "")
    if not task_input or not isinstance(task_input, str):
        raise HTTPException(status_code=400, detail="task_input must be a non-empty string")

    agent_id = body.get("agent_id") or DEFAULT_AGENT_ID
    project_id = body.get("project_id") or ""

    return await agent.recommend(task_input, agent_id=agent_id, project_id=project_id)


@router.post("/render")
async def render(request: Request):
    """渲染最终注入 commander 的 prompt 片段

    请求体：
        {
            "role_ids": ["architect"],
            "behavior_ids": ["simplicity", "naming"],
            "workflow_template": "zh_tdd_loop",  // 可选
            "ctx": {}                            // 可选，保留参数
        }

    返回：
        { "rendered": "..." }
    """
    agent = _get_agent(request)
    body = await request.json()

    role_ids = body.get("role_ids") or []
    behavior_ids = body.get("behavior_ids") or []
    workflow_template = body.get("workflow_template")
    ctx = body.get("ctx")

    if not isinstance(role_ids, list) or not isinstance(behavior_ids, list):
        raise HTTPException(status_code=400, detail="role_ids and behavior_ids must be lists")

    rendered = agent.render(role_ids, behavior_ids, workflow_template, ctx)
    return {"rendered": rendered}


@router.post("/optimize")
async def optimize(request: Request):
    """Phase 73-A / Batch 5.2: 输入框 ✨ 优化——生成多个候选并按质量评分

    请求体：
        {
            "raw_prompt": "用户原始输入",
            "agent_id": "hermes"        // 可选，默认 "hermes"
        }

    返回：
        {
            "optimized": "分数最高的候选 prompt",
            "changes": ["改动点 1", "改动点 2"],
            "candidates": [
                {"optimized": "...", "changes": [...], "score": 0.92},
                {"optimized": "...", "changes": [...], "score": 0.78},
                ...
            ]
        }

    candidates 按 score 降序排列；根字段 optimized/changes 与最高分候选一致。
    失败时（adapter 缺失/LLM 错误/JSON 解析失败）原样返回 raw_prompt，changes=[]，candidates=[]。
    兼容旧格式：LLM 只返回 optimized/changes 时，candidates 自动包装为单候选。
    """
    agent = _get_agent(request)
    body = await request.json()

    raw_prompt = body.get("raw_prompt", "")
    if not isinstance(raw_prompt, str):
        raise HTTPException(status_code=400, detail="raw_prompt must be a string")

    agent_id = body.get("agent_id") or DEFAULT_AGENT_ID

    return await agent.optimize(raw_prompt, agent_id=agent_id)
