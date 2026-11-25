"""routes/knowledge.py —— 语义记忆 + 知识图谱 + 规则 CRUD"""

import json as _json

from fastapi import APIRouter, HTTPException, Request

from config import (
    MEMORY_SEARCH_DEFAULT_LIMIT,
    MEMORY_CONFLICTS_LIMIT,
    KNOWLEDGE_GRAPH_DEFAULT_LIMIT,
    KNOWLEDGE_GRAPH_MAX_LIMIT,
    KNOWLEDGE_NODE_TITLE_PREVIEW_CHARS,
)
from repositories import _safe_tags

router = APIRouter(prefix="/api/v1", tags=["knowledge"])


# ---- 语义记忆 ----

@router.post("/memories/search")
async def search_memories(payload: dict, request: Request):
    query = payload.get("query")
    if not query:
        raise HTTPException(400, "Missing 'query' in payload")
    return await request.app.state.memory_service.multi_level_search(
        query, payload.get("limit", MEMORY_SEARCH_DEFAULT_LIMIT),
    )


@router.post("/memories/{mem_id}/verify")
async def verify_memory(mem_id: str, request: Request):
    await request.app.state.memory_service.verify(mem_id)
    return {"status": "ok"}


@router.post("/memories")
async def create_memory(payload: dict, request: Request):
    """Phase 18: 手动创建记忆——同步索引（写入即可检索，不等 worker 兜底）。"""
    content = payload.get("content")
    if not content:
        raise HTTPException(400, "Missing 'content'")
    mem_id = await request.app.state.memory_service.store_memory(
        content, source="manual",
        memory_type=payload.get("memory_type", "fact"),
        confidence=float(payload.get("confidence", 0.8)),
    )
    return {"status": "ok", "id": mem_id}


@router.put("/memories/{mem_id}")
async def update_memory_endpoint(mem_id: str, payload: dict, request: Request):
    """Phase 18: 编辑记忆"""
    from repositories import update_memory

    ok = await update_memory(request.app.state.db, mem_id, **payload)
    if not ok:
        raise HTTPException(404, "Memory not found or no valid fields to update")
    return {"status": "ok"}


@router.delete("/memories/{mem_id}")
async def delete_memory_endpoint(mem_id: str, request: Request):
    """Phase 18: 删除记忆"""
    from repositories import delete_memory

    ok = await delete_memory(request.app.state.db, mem_id)
    if not ok:
        raise HTTPException(404, "Memory not found")
    return {"status": "ok"}


@router.get("/memories/conflicts")
async def memory_conflicts(limit: int = MEMORY_CONFLICTS_LIMIT, request: Request = None):
    """Phase 18: 检测记忆冲突"""
    from repositories import find_memory_conflicts

    return await find_memory_conflicts(request.app.state.db, limit)


@router.get("/memories/browse")
async def browse_memories(
    limit: int = MEMORY_SEARCH_DEFAULT_LIMIT,
    project_id: str | None = None,
    request: Request = None,
):
    """列出最近语义记忆，供 MemoryExplorer 浏览视图。

    Task 6.5: project_id 过滤——NULL 或空字符串视为不过滤。
    """
    from repositories import list_recent_memories

    capped = max(1, min(limit, 200))
    return await list_recent_memories(request.app.state.db, limit=capped, project_id=project_id or None)


# ---- 知识图谱 ----

@router.get("/knowledge/graph")
async def knowledge_graph(
    limit: int = KNOWLEDGE_GRAPH_DEFAULT_LIMIT,
    offset: int = 0,
    project_id: str | None = None,
    request: Request = None,
):
    """返回 knowledge_nodes 树形结构，供 react-flow 渲染知识图谱。

    Phase 49-I：默认 100 由后端控制，前端不再硬编码 limit。
    Task 6.5: project_id 过滤——非空时只返回该项目的节点 + global 节点
    （NULL = global，对标 CLAUDE.md global，对所有项目可见）。

    tags 解析走 _safe_tags 容错：单行脏数据降级为空数组，不让整个图谱接口 500。
    Batch 7.10: 支持 limit/offset 分页。
    """
    db = request.app.state.db
    offset = max(0, offset)
    capped_limit = min(limit, KNOWLEDGE_GRAPH_MAX_LIMIT)
    # Task 6.5: 按 project_id 过滤（NULL = global 可见）
    if project_id:
        cursor = await db.execute(
            "SELECT id, title, score, source, tags, parent_id "
            "FROM knowledge_nodes WHERE (project_id = ? OR project_id IS NULL) "
            "ORDER BY score DESC LIMIT ? OFFSET ?",
            (project_id, capped_limit, offset),
        )
    else:
        cursor = await db.execute(
            "SELECT id, title, score, source, tags, parent_id "
            "FROM knowledge_nodes ORDER BY score DESC LIMIT ? OFFSET ?",
            (capped_limit, offset),
        )
    rows = await cursor.fetchall()
    if not rows:
        return {"nodes": [], "edges": []}

    nodes = []
    edges = []
    seen: set[str] = set()
    for r in rows:
        nid, title, score, source, raw_tags, parent_id = r
        if nid in seen:
            continue
        seen.add(nid)
        # 容错解析：单行脏数据降级为空 tags，不拖垮整个图谱接口
        tags = _safe_tags(raw_tags)
        nodes.append({
            "id": nid, "title": title[:KNOWLEDGE_NODE_TITLE_PREVIEW_CHARS], "score": round(score, 3),
            "source": source or "", "tags": tags,
        })
        if parent_id and parent_id in seen:
            edges.append({
                "id": f"{parent_id}->{nid}", "source": parent_id, "target": nid,
            })

    # 补充 parent 节点（批量查询替代 N+1）
    parent_ids_in_edges = {e["source"] for e in edges} - seen
    if parent_ids_in_edges:
        placeholders = ",".join("?" for _ in parent_ids_in_edges)
        pc = await db.execute(
            f"SELECT id, title, score, source, tags FROM knowledge_nodes WHERE id IN ({placeholders})",
            tuple(parent_ids_in_edges),
        )
        for prow in await pc.fetchall():
            ptags = _safe_tags(prow[4])
            nodes.append({
                "id": prow[0], "title": prow[1][:KNOWLEDGE_NODE_TITLE_PREVIEW_CHARS],
                "score": round(prow[2], 3),
                "source": prow[3] or "", "tags": ptags,
            })
            seen.add(prow[0])
    return {"nodes": nodes, "edges": edges}


@router.get("/knowledge/browse")
async def browse_knowledge_nodes(
    limit: int = KNOWLEDGE_GRAPH_DEFAULT_LIMIT,
    project_id: str | None = None,
    request: Request = None,
):
    """列出最近知识节点，供 MemoryExplorer 知识浏览视图。

    返回节点列表（含 parent_id），前端可按 source 分组展示。
    Task 6.5: project_id 过滤——NULL 或空字符串视为不过滤。
    Batch 7.10: 支持 limit/offset 分页。
    """
    from repositories import list_recent_knowledge_nodes

    capped = max(1, min(limit, KNOWLEDGE_GRAPH_MAX_LIMIT))
    return await list_recent_knowledge_nodes(
        request.app.state.db, limit=capped, project_id=project_id or None,
    )


# ---- 知识节点编辑/删除（Task 7.23） ----

@router.put("/knowledge/{node_id}")
async def update_knowledge_endpoint(node_id: str, payload: dict, request: Request):
    """Task 7.23: 编辑知识节点——partial update，只更新传入的白名单字段。

    支持字段: title, content, score, source, tags, parent_id, project_id
    """
    from repositories import update_knowledge_node

    ok = await update_knowledge_node(request.app.state.db, node_id, **payload)
    if not ok:
        raise HTTPException(404, "Knowledge node not found or no valid fields to update")
    return {"status": "ok"}


@router.delete("/knowledge/{node_id}")
async def delete_knowledge_endpoint(node_id: str, request: Request):
    """Task 7.23: 删除知识节点及其所有子孙（cascade delete）。

    使用 delete_knowledge_subtree 递归删除子树——对标 Git 删除文件夹。
    返回删除的节点总数。
    """
    from repositories import delete_knowledge_subtree

    deleted_count = await delete_knowledge_subtree(request.app.state.db, node_id)
    if deleted_count == 0:
        raise HTTPException(404, "Knowledge node not found")
    return {"status": "ok", "deleted": deleted_count}


# ---- 规则 ----

@router.get("/rules")
async def list_rules(request: Request):
    return await request.app.state.rules_service.get_all()


@router.post("/rules")
async def create_rule(payload: dict, request: Request):
    rid = await request.app.state.rules_service.create_rule(
        description=payload["description"],
        trigger_on=payload.get("trigger_on", []),
        action=payload["action"],
        enabled=payload.get("enabled", True),
    )
    return {"rule_id": rid}


@router.patch("/rules/{rule_id}")
async def toggle_rule(rule_id: str, payload: dict, request: Request):
    await request.app.state.rules_service.toggle(
        rule_id, payload.get("enabled", True),
    )
    return {"status": "ok"}


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str, request: Request):
    await request.app.state.rules_service.delete(rule_id)
    return {"status": "ok"}
