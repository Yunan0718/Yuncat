"""routes/prompt_library.py —— Phase 72: 统一 Prompt 目录 API

聚合 4 个数据源为统一目录，前端通过单一入口浏览/搜索/选取 Prompt：
- DB roles 表（用户可编辑，DB 优先）
- YAML roles（prompts/roles/*.yaml，内置）
- YAML behaviors（prompts/behaviors/*.yaml，flat mapping）
- Workflow templates（BUILTIN_TEMPLATES，23 个 DAG 模板）

去重规则：DB 角色与 YAML 角色同 id 时 DB 优先；YAML 新增角色启动时种子到 DB。

方案 §Phase 72：
- GET /api/v1/prompt-library/categories —— 顶层分类目录（含各类计数）
- GET /api/v1/prompt-library/templates?q=&category=&type= —— 跨类型搜索
- GET /api/v1/prompt-library/behaviors —— 行为约束列表
- GET /api/v1/prompt-library/roles —— 角色列表（DB+YAML 合并）
"""
from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/v1/prompt-library", tags=["prompt-library"])


def _get_library(request: Request):
    """获取 PromptLibrary 实例——lifespan 已注入 app.state.prompt_library"""
    lib = getattr(request.app.state, "prompt_library", None)
    if lib is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="prompt_library service not initialized")
    return lib


@router.get("/categories")
async def get_categories(request: Request):
    """顶层分类目录——返回 categories 数组（roles/behaviors/workflows）+ 各类计数"""
    lib = _get_library(request)
    return await lib.catalog()


@router.get("/templates")
async def search_templates(
    request: Request,
    q: str = "",
    category: str = "",
    type: str = "",
):
    """跨类型搜索 Prompt 模板

    Args:
        q: 搜索关键词（大小写不敏感，匹配 name/description/id/content）
        category: 分类过滤（roles/behaviors/workflows）
        type: 类型过滤（role/behavior/workflow）
    """
    lib = _get_library(request)
    return {"results": await lib.search(q=q, category=category, type=type)}


@router.get("/behaviors")
async def list_behaviors(request: Request, q: str = ""):
    """列出所有行为约束——可选关键词过滤"""
    lib = _get_library(request)
    behaviors = lib.list_behaviors()
    if q:
        q_lower = q.lower()
        behaviors = [
            b for b in behaviors
            if q_lower in b.get("id", "").lower()
            or q_lower in b.get("name", "").lower()
            or q_lower in b.get("content", "").lower()
            or q_lower in b.get("description", "").lower()
        ]
    return {"results": behaviors, "count": len(behaviors)}


@router.get("/behaviors/{behavior_id}")
async def get_behavior(behavior_id: str, request: Request):
    """获取单个行为约束详情"""
    lib = _get_library(request)
    b = lib.get_behavior(behavior_id)
    if not b:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"behavior '{behavior_id}' not found")
    return b


@router.get("/roles")
async def list_roles(request: Request):
    """列出所有角色——DB+YAML 合并，DB 优先（同 id 去重）"""
    lib = _get_library(request)
    return {"results": await lib.list_roles()}


@router.get("/roles/{role_id}")
async def get_role(role_id: str, request: Request):
    """获取单个角色详情——DB 优先，YAML 回退"""
    lib = _get_library(request)
    role = await lib.get_role(role_id)
    if not role:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"role '{role_id}' not found")
    return role


@router.get("/workflows")
async def list_workflows(request: Request):
    """列出工作流模板（仅元数据，不含 graph）"""
    lib = _get_library(request)
    return {"results": lib.list_workflows()}


@router.get("/workflows/{name}")
async def get_workflow_template(name: str, request: Request):
    """获取工作流模板详情（含 graph 完整定义）"""
    lib = _get_library(request)
    tpl = lib.get_workflow_template(name)
    if not tpl:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"workflow template '{name}' not found")
    return tpl
