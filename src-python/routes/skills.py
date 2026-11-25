"""routes/skills.py —— Skill 系统 API（安装/卸载/市场/安全审查/评分/创作）

从 main.py 提取（原 ~253 行内联端点），Phase 29-33。
Phase 47 修复（2.3）：8 个端点改用 Pydantic 模型校验（显式构造 + 保留原响应格式）。
"""
import logging
import os
import time
import uuid as _uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from config import (
    MAX_TOKENS_SKILL_TEST,
    SUB_AGENT_ERROR_PREVIEW_CHARS, WORKFLOW_FROM_CHAT_DESCRIPTION_PREVIEW_CHARS,
    SKILL_GENERATED_DESCRIPTION_PREVIEW_CHARS, DEFAULT_AGENT_ID,
)
from schemas.skills import (
    InstallSkillRequest,
    GenerateWorkflowTemplateRequest,
    InstallSkillFromGitRequest,
    InstallSkillFromMarketplaceRequest,
    CheckSkillRequest,
    RateSkillRequest,
    CreateSkillRequest,
)

router = APIRouter(prefix="/api/v1", tags=["skills"])
_log = logging.getLogger("yuncat.skills")


async def _parse_request(request: Request, model_cls):
    """解析 + 校验请求体。返回 (model_instance, error_dict)。

    error_dict 非 None 时表示解析/校验失败，路由应直接 return error_dict。
    保留原 {"ok": False, "error": ...} 响应格式，前端零改动。
    """
    try:
        body = await request.json()
    except Exception:
        return None, {"ok": False, "error": "Invalid JSON"}
    try:
        return model_cls(**body), None
    except ValidationError as e:
        # 提取第一个错误字段，避免响应过长
        try:
            err = e.errors()[0]
            loc = ".".join(str(x) for x in err.get("loc", []))
            msg = err.get("msg", "invalid")
            return None, {"ok": False, "error": f"Field '{loc}': {msg}"}
        except Exception:
            return None, {"ok": False, "error": "Validation failed"}


# ════════════════════════════════════════════════════════════
# Skill 基础 CRUD
# ════════════════════════════════════════════════════════════

@router.get("/skills")
async def list_skills(enabled_only: bool = False, request: Request = None):
    return [s.to_dict() for s in request.app.state.skill_registry.list_installed(enabled_only=enabled_only)]


@router.post("/skills/install")
async def install_skill(request: Request):
    """从本地目录安装 Skill。Body: {"source_dir": "/path/to/skill/"}"""
    req, err = await _parse_request(request, InstallSkillRequest)
    if err:
        raise HTTPException(400, err["error"])
    source_dir = req.source_dir
    if not os.path.isdir(source_dir):
        raise HTTPException(400, "source_dir must be a valid directory")
    skill = await request.app.state.skill_registry.install_from_dir(source_dir)
    if not skill:
        raise HTTPException(400, "Failed to install skill")
    return {"ok": True, "skill": skill.to_dict()}


@router.delete("/skills/{skill_id}")
async def uninstall_skill(skill_id: str, request: Request):
    ok = await request.app.state.skill_registry.uninstall(skill_id)
    return {"ok": ok}


@router.put("/skills/{skill_id}/toggle")
async def toggle_skill(skill_id: str, request: Request):
    ok = request.app.state.skill_registry.toggle(skill_id)
    return {"ok": ok}


# ════════════════════════════════════════════════════════════
# Skill 推荐 + AI 生成工作流模板
# ════════════════════════════════════════════════════════════

@router.get("/skills/recommend")
async def recommend_skills(language: str = "", request: Request = None):
    """根据项目语言返回推荐 Skill 列表。"""
    recent_tasks_str = request.query_params.get("tasks", "") if request else ""
    recent_tasks = recent_tasks_str.split(",") if recent_tasks_str else None
    skills = request.app.state.skill_registry.get_recommended_skills(language, recent_tasks)
    return {"ok": True, "recommended": skills, "language": language}


@router.post("/workflows/generate-template")
async def generate_workflow_template(request: Request):
    """用 LLM 根据需求描述自动生成 DAG 工作流模板。
    Body: {"description": "需求描述", "name": "模板名称"}"""
    req, err = await _parse_request(request, GenerateWorkflowTemplateRequest)
    if err:
        raise HTTPException(400, err["error"])
    desc = req.description
    name = req.name

    if not hasattr(request.app.state, "model_config"):
        raise HTTPException(503, "model_config not available")

    cfg = await request.app.state.model_config.get_active_config()
    api_key = cfg.get("api_key", "")
    if not api_key:
        raise HTTPException(401, "API key not configured")

    import json as _json2
    gen_prompt = f"""Generate a DAG workflow graph as JSON for this task: {desc}.\nOutput format:\n{{"nodes":[{{"id":"step1","type":"default","prompt":"..."}},{{"id":"step2","type":"human","prompt":"waiting for approval"}}],"edges":[{{"from":"step1","to":"step2"}}]}}\nUse 3-6 nodes. One can be type "human" for approval. JSON only:"""

    adapter = (request.app.state.adapters or {}).get(DEFAULT_AGENT_ID)
    if not adapter:
        raise HTTPException(503, "Default agent adapter not available")

    try:
        result = await adapter.invoke(
            gen_prompt,
            context={
                "system": "You are a workflow designer. Output valid JSON only.",
                "max_tokens": MAX_TOKENS_SKILL_TEST,
                "temperature": 0.3,
            },
        )
        if result.get("error"):
            raise HTTPException(400, f"LLM call failed: {result['error']}")
        content = result.get("content", "")
        json_start = content.find("{")
        graph = _json2.loads(content[json_start:content.rfind("}")+1]) if json_start >= 0 else None
        if not graph or "nodes" not in graph:
            raise HTTPException(400, "LLM did not return valid graph JSON")
    except Exception as e:
        raise HTTPException(400, f"LLM call failed: {e}")

    # 保存为模板
    import uuid
    wid = str(uuid.uuid4())[:12]
    await request.app.state.db.execute(
        "INSERT INTO workflows(id, name, description, graph, is_template, created_at, updated_at) VALUES(?,?,?,?,1,?,?)",
        (wid, name, f"AI generated: {desc[:SKILL_GENERATED_DESCRIPTION_PREVIEW_CHARS]}", _json2.dumps(graph, ensure_ascii=False),
         int(time.time()), int(time.time())),
    )
    await request.app.state.db.commit()
    return {"ok": True, "workflow_id": wid, "name": name, "graph": graph, "nodes": len(graph.get("nodes", []))}


# ════════════════════════════════════════════════════════════
# Skill Marketplace
# ════════════════════════════════════════════════════════════

@router.get("/skills/marketplace/search")
async def search_marketplace(q: str = "", category: str = "", page: int = 1, request: Request = None):
    from services.evolution.skill_registry import get_marketplace
    mp = get_marketplace()
    return await mp.search(q, category=category, page=page)


@router.get("/skills/marketplace/{skill_id}")
async def marketplace_detail(skill_id: str, request: Request):
    from services.evolution.skill_registry import get_marketplace
    mp = get_marketplace()
    detail = await mp.get_detail(skill_id)
    if not detail:
        raise HTTPException(404, "Skill not found in marketplace")
    return detail


@router.post("/skills/install-git")
async def install_skill_from_git(request: Request):
    """从 Git 仓库安装 Skill。Body: {"url": "...", "branch": "..."}"""
    req, err = await _parse_request(request, InstallSkillFromGitRequest)
    if err:
        raise HTTPException(400, err["error"])
    url = req.url
    branch = req.branch
    try:
        skill = await request.app.state.skill_registry.install_from_git(url, branch)
    except ValueError as e:
        # P0 安全修复: url_guard 拒绝的 URL——把原因透传给用户而非笼统 400
        raise HTTPException(400, str(e))
    if not skill:
        raise HTTPException(400, "Failed to install skill from git")
    return {"ok": True, "skill": skill.to_dict()}


@router.post("/skills/install-marketplace")
async def install_skill_from_marketplace(request: Request):
    """从 agentskills.io 安装 Skill。Body: {"skill_id": "...", "source?": "..."}"""
    req, err = await _parse_request(request, InstallSkillFromMarketplaceRequest)
    if err:
        raise HTTPException(400, err["error"])
    skill_id = req.skill_id
    source = req.source
    try:
        skill = await request.app.state.skill_registry.install_from_marketplace(skill_id, source)
    except ValueError as e:
        # P0 安全修复: skill_id/source 校验失败——把原因透传给用户
        raise HTTPException(400, str(e))
    if not skill:
        raise HTTPException(400, "Failed to install skill from marketplace")
    return {"ok": True, "skill": skill.to_dict()}


@router.get("/skills/parse-md")
async def parse_skill_markdown(path: str, request: Request):
    """预览本地 SKILL.md 的解析结果。Query: ?path=..."""
    if not path or not os.path.isfile(path):
        raise HTTPException(400, "path must point to a file")
    data = request.app.state.skill_registry.parse_skill_md(path)
    if not data:
        raise HTTPException(400, "Failed to parse SKILL.md")
    return {"ok": True, "data": data}


# ════════════════════════════════════════════════════════════
# Phase 43 V.4: ClawHub 外部搜索
# ════════════════════════════════════════════════════════════

@router.get("/skills/search/external")
async def search_external_market(q: str = "", request: Request = None):
    """搜索 ClawHub 技能市场,回退 GitHub topics。"""
    from services.evolution.skill_registry import get_skill_registry
    sr = get_skill_registry()
    if hasattr(sr, '_search_clawhub') and q:
        try:
            result = await sr._search_clawhub(q)
            if result:
                return {"ok": True, "results": result.get("results", result if isinstance(result, list) else [])}
        except Exception:
            _log.debug("搜索 ClawHub 失败", exc_info=True)
    # Fallback: 返回空 — ClawHub 不可用时前端显示 "手动输入 URL"
    return {"ok": True, "results": []}


# ════════════════════════════════════════════════════════════
# Skill 冲突检测 + 配置 + 安全审查
# ════════════════════════════════════════════════════════════

@router.post("/skills/check-conflicts")
async def check_skill_conflicts(request: Request):
    """安装前检测冲突。Body: {source_dir}"""
    req, err = await _parse_request(request, CheckSkillRequest)
    if err:
        raise HTTPException(400, err["error"])
    try:
        source_dir = req.source_dir
        yaml_path = os.path.join(source_dir, "skill.yaml")
        if not os.path.exists(yaml_path):
            raise HTTPException(400, "source_dir must contain skill.yaml")

        skill = request.app.state.skill_registry._parse_yaml(yaml_path, source_dir)
        if not skill:
            raise HTTPException(400, "Failed to parse skill.yaml")

        conflicts = request.app.state.skill_registry.detect_conflicts(skill)
        return {"ok": True, "conflicts": conflicts, "can_install": len(conflicts) == 0}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.get("/skills/{skill_id}/config")
async def get_skill_config(skill_id: str, request: Request):
    schema = request.app.state.skill_registry.get_config_schema(skill_id)
    if not schema:
        raise HTTPException(400, "No config schema")
    return {"ok": True, "schema": schema}


@router.post("/skills/check-security")
async def check_skill_security(request: Request):
    """安装前安全审查。Body: {source_dir}"""
    req, err = await _parse_request(request, CheckSkillRequest)
    if err:
        raise HTTPException(400, err["error"])
    try:
        source_dir = req.source_dir
        yaml_path = os.path.join(source_dir, "skill.yaml")
        if not os.path.exists(yaml_path):
            raise HTTPException(400, "No skill.yaml found")
        skill = request.app.state.skill_registry._parse_yaml(yaml_path, source_dir)
        if not skill:
            raise HTTPException(400, "Failed to parse")
        result = request.app.state.skill_registry.check_permissions(skill)
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(400, str(e))


# ════════════════════════════════════════════════════════════
# Skill 评分 + 创作
# ════════════════════════════════════════════════════════════

@router.post("/skills/{skill_id}/rate")
async def rate_skill(skill_id: str, request: Request):
    """评分 Skill。Body: {rating: 1-5}"""
    req, err = await _parse_request(request, RateSkillRequest)
    if err:
        raise HTTPException(400, err["error"])
    try:
        rating = req.rating
        ok = request.app.state.skill_registry.rate_skill(skill_id, rating)
        return {"ok": ok}
    except Exception:
        raise HTTPException(400, "Invalid request")


@router.get("/skills/{skill_id}/rating")
async def get_skill_rating(skill_id: str, request: Request):
    return request.app.state.skill_registry.get_rating(skill_id)


@router.post("/skills/create")
async def create_skill(request: Request):
    """GUI 创作向导——从表单数据生成 skill.yaml。Body: skill metadata + 四模块内容"""
    import yaml, shutil, json as _json, uuid as _uuid2
    req, err = await _parse_request(request, CreateSkillRequest)
    if err:
        raise HTTPException(400, err["error"])

    # 处理 skill_id（空则自动生成）
    skill_id = (req.id or "").strip()
    if not skill_id:
        skill_id = f"custom-{str(_uuid2.uuid4())[:8]}"
    # P0 安全修复: skill_id 白名单——防 "../x" 穿越到安装目录外（与 registry 同一模式）
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", skill_id):
        raise HTTPException(400, "非法 skill id：仅允许字母/数字/./_/-，且以字母或数字开头")

    # 生成 skill.yaml
    skill_yaml = {
        "name": skill_id, "version": req.version,
        "title": req.title or skill_id, "description": req.description,
        "author": req.author, "category": req.category,
        "icon": req.icon, "tags": req.tags,
        "min_yuncat": "0.2.0",
    }

    # 写入安装目录
    dest_dir = os.path.join(
        request.app.state.skill_registry._install_root, skill_id,
    )
    os.makedirs(dest_dir, exist_ok=True)

    # prompt 模块
    if req.prompt_text:
        with open(os.path.join(dest_dir, "prompt.md"), "w", encoding="utf-8") as f:
            f.write(req.prompt_text)
        skill_yaml["prompt"] = {"file": "prompt.md", "placement": "append", "priority": 50,
                                 "scope": {"tasks": req.scope_tasks,
                                           "languages": req.scope_languages,
                                           "frameworks": [], "modes": ["agent"]}}

    # workflow 模块
    if req.workflow_graph:
        with open(os.path.join(dest_dir, "workflow.yaml"), "w", encoding="utf-8") as f:
            yaml.dump(req.workflow_graph, f, allow_unicode=True)
        skill_yaml["workflow"] = {"file": "workflow.yaml"}

    # commands 模块
    if req.slash_command:
        cmd_data = [{"name": req.slash_command.lstrip("/"),
                      "slash_command": req.slash_command,
                      "description": req.command_description,
                      "category": req.category}]
        with open(os.path.join(dest_dir, "commands.yaml"), "w", encoding="utf-8") as f:
            yaml.dump(cmd_data, f, allow_unicode=True)
        skill_yaml["commands"] = {"file": "commands.yaml"}

    # 写入 skill.yaml
    with open(os.path.join(dest_dir, "skill.yaml"), "w", encoding="utf-8") as f:
        yaml.dump(skill_yaml, f, allow_unicode=True, default_flow_style=False)

    # 安装
    skill = await request.app.state.skill_registry.install_from_dir(dest_dir)
    if not skill:
        raise HTTPException(400, "Install failed")
    return {"ok": True, "skill": skill.to_dict(), "skill_id": skill_id}
