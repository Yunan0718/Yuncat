"""repositories/knowledge_repo.py —— Memory Tree 知识图谱 CRUD（v66）"""
import json
import re
import time

from config import (
    KNOWLEDGE_LIKE_FALLBACK_OVERFETCH,
    KNOWLEDGE_LIKE_QUERY_MAX_CHARS,
    KNOWLEDGE_NODE_CONTENT_PREVIEW_CHARS,
    KNOWLEDGE_NODE_SEARCH_CONTENT_PREVIEW_CHARS,
    KNOWLEDGE_SCORE_CEILING,
    KNOWLEDGE_SCORE_FLOOR,
)


def _safe_tags(raw: object) -> list[str]:
    """容错解析 tags 列——脏数据不得拖垮整个列表接口。

    tags 在 SQLite 里以 JSON 字符串存储，读取时逐个 json.loads。旧实现裸调
    `json.loads(r[7])`：任何一行是非法 JSON（历史脏数据、手工改库、上游写入
    了裸字符串）都会抛 ValueError，让整个 browse/list 接口 500，
    而不是只丢掉那一条节点。

    这里按「单行失败降级为空数组」处理，与前端 MemoryExplorer 的 parseTags
    形成两端容错（任一端先遇到脏数据都不会崩）。
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str)]
    if not isinstance(raw, str):
        return []
    trimmed = raw.strip()
    if not trimmed:
        return []
    try:
        parsed = json.loads(trimmed)
    except (ValueError, TypeError):
        return []
    if isinstance(parsed, list):
        return [t for t in parsed if isinstance(t, str)]
    return []


async def upsert_knowledge_node(
    conn, node_id: str, title: str, content: str,
    parent_id: str | None = None, source: str = "auto_dream",
    source_ref: str = "", tags: list[str] | None = None,
    score: float = 1.0,
    project_id: str | None = None,
) -> str:
    """插入或更新知识节点。返回 node_id。

    Task 6.5: project_id 参数——NULL 或空字符串存储为 NULL（global knowledge，
    对所有项目可见）。非空字符串存储为项目级知识（仅该项目可见）。
    对标 Claude Code CLAUDE.md 项目隔离——global CLAUDE.md 对所有项目生效。
    """
    # 空字符串统一转为 None（避免空串与 NULL 双语义）
    _project_id = project_id if project_id else None
    tags_json = json.dumps(tags or [])
    now = int(time.time())
    cursor = await conn.execute("SELECT id FROM knowledge_nodes WHERE id=?", (node_id,))
    existing = await cursor.fetchone()
    if existing:
        await conn.execute(
            "UPDATE knowledge_nodes SET title=?, content=?, parent_id=?, score=?, "
            "source=?, source_ref=?, tags=?, updated_at=?, project_id=? WHERE id=?",
            (title, content, parent_id, score, source, source_ref, tags_json, now, _project_id, node_id),
        )
    else:
        await conn.execute(
            "INSERT INTO knowledge_nodes(id, title, content, parent_id, score, "
            "source, source_ref, tags, created_at, updated_at, project_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (node_id, title, content, parent_id, score, source, source_ref, tags_json, now, now, _project_id),
        )
    await conn.commit()
    return node_id


async def adjust_knowledge_score(conn, node_id: str, delta: float) -> float | None:
    """原子调整知识节点评分：score = clamp(score + delta, floor, ceiling)。

    必须用 SQL 原地累加，不能在 Python 里读-改-写。两个坑都已实测踩过：

    1. search_knowledge_tree 返回的 "score" 是 BM25 检索相关性，不是节点评分。
       在 Python 里读回它再做 +delta，等于把评分覆盖成「相关性 ± delta」——
       实测连续接受 5 次评分纹丝不动（被同一个相关性值反复钉死），
       反馈学习实际上从未累积过。

    2. 用 upsert_knowledge_node 回写时，其 UPDATE 分支会把 source / source_ref /
       tags / project_id 一并写入。调用方不传即用默认值，于是
       source 被改成 'auto_dream'、tags 清空、project_id 置 NULL。
       capability_router._query_learned_from_db 同时依赖
       `source='agent_delegation'` 与 `tags LIKE '%agent_id%'`，
       一旦被覆盖，该节点就永久退出委托反馈闭环（实测命中数归 0）；
       project_id 置 NULL 还会让项目级知识泄漏为全局。

    返回调整后的新评分；节点不存在时返回 None。
    """
    cursor = await conn.execute(
        "UPDATE knowledge_nodes SET score = MAX(?, MIN(?, COALESCE(score, ?) + ?)), "
        "updated_at = ? WHERE id = ?",
        (KNOWLEDGE_SCORE_FLOOR, KNOWLEDGE_SCORE_CEILING,
         KNOWLEDGE_SCORE_FLOOR, delta, int(time.time()), node_id),
    )
    await conn.commit()
    if not cursor.rowcount:
        return None
    cursor = await conn.execute("SELECT score FROM knowledge_nodes WHERE id=?", (node_id,))
    row = await cursor.fetchone()
    return float(row[0]) if row and row[0] is not None else None


async def get_knowledge_node(conn, node_id: str) -> dict | None:
    cursor = await conn.execute(
        "SELECT id, title, content, parent_id, score, source, source_ref, tags, "
        "created_at, updated_at, project_id FROM knowledge_nodes WHERE id=?", (node_id,),
    )
    r = await cursor.fetchone()
    if not r:
        return None
    return {
        "id": r[0], "title": r[1], "content": r[2], "parent_id": r[3],
        "score": r[4], "source": r[5], "source_ref": r[6],
        "tags": _safe_tags(r[7]), "created_at": r[8], "updated_at": r[9],
        "project_id": r[10],
    }


def _build_knowledge_filter(filters: dict | None) -> tuple[str, list]:
    """根据 filters 生成 knowledge_nodes 查询的 WHERE 附加条件与参数。"""
    conds: list[str] = []
    params: list = []
    if not filters:
        return "", params
    if filters.get("source") is not None:
        conds.append("n.source = ?")
        params.append(filters["source"])
    if filters.get("source_ref") is not None:
        conds.append("n.source_ref = ?")
        params.append(filters["source_ref"])
    # Task 6.5: project_id 过滤——非空字符串才过滤（None / 空串视为不过滤）
    pid = filters.get("project_id")
    if pid:  # 非空字符串
        conds.append("(n.project_id = ? OR n.project_id IS NULL)")
        params.append(pid)
    return (f"AND {' AND '.join(conds)}" if conds else ""), params


async def _search_knowledge_fts(
    conn, query: str, filter_sql: str, filter_params: list, limit: int,
) -> list:
    """Batch 2.3: 用 bm25(knowledge_fts) 做 FTS5 检索，返回原始行。"""
    cursor = await conn.execute(
        "SELECT n.id, n.title, n.content, n.parent_id, n.score, n.source, n.tags, "
        "n.created_at, n.last_accessed, bm25(knowledge_fts) as bm25_score "
        "FROM knowledge_fts f JOIN knowledge_nodes n ON f.node_id = n.id "
        f"WHERE knowledge_fts MATCH ? {filter_sql} "
        "ORDER BY bm25(knowledge_fts) ASC, n.score DESC LIMIT ?",
        (query, *filter_params, limit),
    )
    return await cursor.fetchall()


def _tokenize_for_relevance(text: str) -> list[str]:
    """按非词字符分词并小写化，用于 LIKE fallback 相关性计算。"""
    return [t for t in re.sub(r"[^\w\s一-鿿]", " ", text.lower()).split() if t]


def _compute_like_relevance(query: str, title: str, content: str) -> float:
    """Batch 2.4: 计算 query 与文档的简单 token 匹配度。

    title 命中权重 2，content 命中权重 1；命中多个 token 可累加。
    这是 BM25 的轻量近似，避免 LIKE fallback 时只按 n.score 排序导致不相关文档靠前。
    """
    query_tokens = _tokenize_for_relevance(query)
    if not query_tokens:
        return 0.0
    title_tokens = _tokenize_for_relevance(title)
    content_tokens = _tokenize_for_relevance(content)
    score = 0.0
    for token in query_tokens:
        score += sum(2.0 for t in title_tokens if token in t)
        score += sum(1.0 for t in content_tokens if token in t)
    return score


def _rank_like_results(rows: list, query: str, limit: int) -> list:
    """Batch 2.4: 按 query 相关性降序重排 LIKE 候选，tie-breaker 为 n.score。

    返回的行末尾附加相关性分数的负值（与 FTS5 的 bm25_score 同语义），
    让 _format_knowledge_rows 统一取负后得到正分。
    """
    scored = [
        (-_compute_like_relevance(query, r[1], r[2]), -float(r[4] if r[4] is not None else 0.0), r)
        for r in rows
    ]
    scored.sort(key=lambda x: (x[0], x[1]))
    return [s[2] + (s[0],) for s in scored[:limit]]


async def _search_knowledge_like(
    conn, query: str, filter_sql: str, filter_params: list, limit: int,
) -> list:
    """FTS5 失败时回退到 LIKE；Batch 2.4 按 query 相关性重排。"""
    like = f"%{query[:KNOWLEDGE_LIKE_QUERY_MAX_CHARS]}%"
    pool_limit = max(limit, limit * KNOWLEDGE_LIKE_FALLBACK_OVERFETCH)
    cursor = await conn.execute(
        "SELECT n.id, n.title, n.content, n.parent_id, n.score, n.source, n.tags, "
        "n.created_at, n.last_accessed FROM knowledge_nodes n "
        f"WHERE (n.title LIKE ? OR n.content LIKE ?) {filter_sql} ORDER BY n.score DESC LIMIT ?",
        (like, like, *filter_params, pool_limit),
    )
    rows = await cursor.fetchall()
    return _rank_like_results(rows, query, limit)


def _format_knowledge_rows(rows: list) -> list[dict]:
    """把 knowledge 查询原始行格式化为返回 dict；FTS5 行含 bm25_score，LIKE 行不含。

    ⚠️ "score" 是**检索相关性**（越大越相关），**不是** knowledge_nodes.score
    （节点的持久化置信度）。两个量纲完全不同：
      - 相关性随 query 变化，同一节点换 query 就是另一个值
      - 节点评分是跨 query 累积的历史（接受 +0.10 / 拒绝 -0.15）
    需要按节点置信度做加减或"保留原值"的调用方，必须用 "node_score"。
    把相关性当评分会导致反馈永不累积（每次都被相关性重新钉死）。
    """
    return [
        {
            "id": r[0], "title": r[1],
            "content": r[2][:KNOWLEDGE_NODE_SEARCH_CONTENT_PREVIEW_CHARS],
            "parent_id": r[3],
            # Batch 2.3: bm25_score 负数取正（越大越相关）；fallback 保留 n.score
            "score": -float(r[9]) if len(r) > 9 and r[9] is not None else float(r[4]),
            # 节点真实持久化评分（knowledge_nodes.score），供反馈学习等需要累积的调用方
            "node_score": float(r[4]) if r[4] is not None else 0.0,
            "source": r[5], "tags": _safe_tags(r[6]),
            "created_at": r[7], "last_accessed": r[8],
        }
        for r in rows
    ]


async def search_knowledge_tree(
    conn, query: str, limit: int = 10, filters: dict | None = None,
) -> list[dict]:
    """FTS5 搜索知识树，按 BM25 相关性降序返回。

    优先用 FTS5；FTS5 语法错误时回退到 LIKE。
    Phase 54: 支持下推 source / source_ref 标量过滤。
    Task 6.5: 支持下推 project_id 过滤——NULL = global knowledge 对所有项目可见
              （对标 Claude Code CLAUDE.md global）；空字符串视为不过滤（向后兼容）。
    """
    filter_sql, filter_params = _build_knowledge_filter(filters)
    safe_query = _sanitize_fts5_query(query)

    # L5: 净化后是占位符（全是特殊字符）→ 直接走 LIKE
    if safe_query != "zzz_no_match_placeholder":
        try:
            rows = await _search_knowledge_fts(
                conn, safe_query, filter_sql, filter_params, limit,
            )
            # L5: FTS5 返回 0 结果且 query 含特殊字符 → 回退 LIKE
            if rows or not any(c in query for c in ":.()[]*\"-"):
                return _format_knowledge_rows(rows)
        except Exception:
            pass  # fallback 到 LIKE

    rows = await _search_knowledge_like(
        conn, query, filter_sql, filter_params, limit,
    )
    return _format_knowledge_rows(rows)


def _sanitize_fts5_query(query: str) -> str:
    """净化 FTS5 查询——去除会导致语法错误的特殊字符。

    FTS5 保留字符: . : * " - ( ) 等。
    策略: 只保留字母/数字/中文/空格/下划线，其余去除。
    """
    import re
    safe = re.sub(r'[^\w\s一-鿿]', ' ', query)
    safe = re.sub(r'\s+', ' ', safe).strip()
    if not safe or len(safe) < 2:
        return 'zzz_no_match_placeholder'
    # 各词用 OR 连接——避免隐式 AND 导致全词匹配失败
    terms = safe.split()
    return ' OR '.join(terms) if len(terms) > 1 else terms[0]


async def decay_knowledge_scores(conn, decay_factor: float = 0.95) -> int:
    """[M2 已弃用] 所有知识节点分数衰减（uniform score * decay_factor）。

    M2 已用 Ebbinghaus 动态时间衰减替代——衰减在 retrieve_context 实时计算，
    periodic_maintenance 不再调用本函数。保留仅供向后兼容。
    """
    cursor = await conn.execute(
        "UPDATE knowledge_nodes SET score = score * ? WHERE score > 0.1", (decay_factor,),
    )
    await conn.commit()
    return cursor.rowcount


async def get_knowledge_children(conn, parent_id: str) -> list[dict]:
    """获取某个节点的所有子节点。"""
    cursor = await conn.execute(
        "SELECT id, title, content, score, tags FROM knowledge_nodes "
        "WHERE parent_id=? ORDER BY score DESC", (parent_id,),
    )
    rows = await cursor.fetchall()
    return [
        {"id": r[0], "title": r[1], "content": r[2][:KNOWLEDGE_NODE_CONTENT_PREVIEW_CHARS], "score": r[3],
         "tags": _safe_tags(r[4])}
        for r in rows
    ]


async def list_recent_knowledge_nodes(
    conn, limit: int = 100, project_id: str | None = None,
) -> list[dict]:
    """列出最近知识节点，按 created_at 降序。

    返回完整 content（不截断），供 MemoryExplorer 知识浏览视图展示。
    包含 parent_id 字段以支持简单树形关系。

    Task 6.5: project_id 过滤——非空时只返回该项目 + global 节点
    （NULL = global，对标 CLAUDE.md global，对所有项目可见）。
    """
    if project_id:  # 非空字符串才过滤
        cursor = await conn.execute(
            "SELECT id, title, content, parent_id, score, source, source_ref, tags, created_at, updated_at "
            "FROM knowledge_nodes WHERE (project_id = ? OR project_id IS NULL) "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (project_id, limit),
        )
    else:
        cursor = await conn.execute(
            "SELECT id, title, content, parent_id, score, source, source_ref, tags, created_at, updated_at "
            "FROM knowledge_nodes ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )
    rows = await cursor.fetchall()
    return [
        {"id": r[0], "title": r[1], "content": r[2], "parent_id": r[3],
         "score": r[4], "source": r[5], "source_ref": r[6],
         "tags": _safe_tags(r[7]), "created_at": r[8], "updated_at": r[9]}
        for r in rows
    ]


async def list_knowledge_roots(
    conn, limit: int = 20, project_id: str | None = None,
) -> list[dict]:
    """列出所有根节点（parent_id IS NULL），按评分降序。

    Task 6.5: project_id 过滤——非空时只返回该项目的根节点 + global 根节点
    （NULL = global，对标 CLAUDE.md global，对所有项目可见）；
    None / 空字符串视为不过滤（向后兼容）。
    """
    if project_id:  # 非空字符串才过滤
        cursor = await conn.execute(
            "SELECT id, title, content, score, source, tags, created_at "
            "FROM knowledge_nodes WHERE parent_id IS NULL "
            "AND (project_id = ? OR project_id IS NULL) "
            "ORDER BY score DESC LIMIT ?",
            (project_id, limit),
        )
    else:
        cursor = await conn.execute(
            "SELECT id, title, content, score, source, tags, created_at "
            "FROM knowledge_nodes WHERE parent_id IS NULL "
            "ORDER BY score DESC LIMIT ?", (limit,),
        )
    rows = await cursor.fetchall()
    return [
        {"id": r[0], "title": r[1], "content": r[2][:KNOWLEDGE_NODE_CONTENT_PREVIEW_CHARS], "score": r[3],
         "source": r[4], "tags": _safe_tags(r[5]), "created_at": r[6]}
        for r in rows
    ]


async def list_knowledge_leaves(
    conn, limit: int = 100, project_id: str | None = None,
) -> list[dict]:
    """Task 7.2: 列出叶子节点（parent_id IS NULL），按 created_at 升序。

    与 list_knowledge_roots 的区别:
      - list_knowledge_roots: 按评分降序，content 截断，用于 UI 展示
      - list_knowledge_leaves: 按 created_at 升序，content 不截断，用于 RAPTOR 聚类

    RAPTOR 聚类需要完整 content 计算 embedding 相似度，故不截断。
    按 created_at 升序——优先聚类旧节点（新节点可能仍在增长）。

    Task 6.5: project_id 过滤——非空时只返回该项目 + global 叶子
    （NULL = global，对标 CLAUDE.md global，对所有项目可见）。
    """
    if project_id:  # 非空字符串才过滤
        cursor = await conn.execute(
            "SELECT id, title, content, project_id "
            "FROM knowledge_nodes WHERE parent_id IS NULL "
            "AND (project_id = ? OR project_id IS NULL) "
            "ORDER BY created_at ASC LIMIT ?",
            (project_id, limit),
        )
    else:
        cursor = await conn.execute(
            "SELECT id, title, content, project_id "
            "FROM knowledge_nodes WHERE parent_id IS NULL "
            "ORDER BY created_at ASC LIMIT ?", (limit,),
        )
    rows = await cursor.fetchall()
    return [
        {"id": r[0], "title": r[1], "content": r[2], "project_id": r[3]}
        for r in rows
    ]


async def delete_knowledge_subtree(conn, node_id: str) -> int:
    """删除节点及其所有子孙。返回删除数量。

    Task 7.23: 节点不存在时返回 0（而非 1）——修复预存在 bug：
    原 `to_delete` 列表总是包含初始 `node_id`，即使节点不存在也返回 1。
    这导致 DELETE 路由的 404 检查失效。现在先检查存在性。
    """
    # 检查节点是否存在——不存在返回 0（对标 SQL DELETE rowcount 语义）
    cursor = await conn.execute("SELECT id FROM knowledge_nodes WHERE id=?", (node_id,))
    if not await cursor.fetchone():
        return 0
    # 递归收集所有子孙
    to_delete = [node_id]
    i = 0
    while i < len(to_delete):
        cursor = await conn.execute(
            "SELECT id FROM knowledge_nodes WHERE parent_id=?", (to_delete[i],),
        )
        children = await cursor.fetchall()
        to_delete.extend(c[0] for c in children)
        i += 1
    for nid in to_delete:
        await conn.execute("DELETE FROM knowledge_nodes WHERE id=?", (nid,))
    await conn.commit()
    return len(to_delete)


async def update_knowledge_node(conn, node_id: str, **fields) -> bool:
    """Task 7.23: 编辑知识节点——只更新传入的字段（partial update）。

    对标 update_memory 的 allowed-set 模式：过滤白名单字段，动态构建 SET 子句。
    tags 字段特殊处理：list → JSON 序列化，str → 原样存储。
    自动更新 updated_at 时间戳。
    """
    allowed = {"title", "content", "score", "source", "tags", "parent_id", "project_id"}
    updates: dict[str, object] = {}
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "tags":
            updates[k] = json.dumps(v) if isinstance(v, list) else v
        elif k == "project_id":
            # 空字符串统一转为 None（与 upsert_knowledge_node 一致）
            updates[k] = v if v else None
        else:
            updates[k] = v
    if not updates:
        return False
    # 自动更新 updated_at
    updates["updated_at"] = int(time.time())
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [node_id]
    cursor = await conn.execute(
        f"UPDATE knowledge_nodes SET {set_clause} WHERE id=?", values,
    )
    if cursor.rowcount == 0:
        return False
    await conn.commit()
    return True


# ═════════════════════════════════════════════════════════════
# Task 4.4: RAG 索引一致性 — Outbox pattern 辅助函数
# indexed_at IS NULL 表示待索引到 chromadb；MemoryIndexerWorker 兜底补索引
# ═════════════════════════════════════════════════════════════

async def mark_knowledge_indexed(conn, node_id: str) -> None:
    """标记知识节点为已索引到 chromadb（indexed_at = 当前时间戳）。

    MemoryTree._index_to_vector 同步索引成功后调用；
    MemoryIndexerWorker 兜底索引成功后也调用。
    """
    import time as _time
    await conn.execute(
        "UPDATE knowledge_nodes SET indexed_at=? WHERE id=?",
        (int(_time.time()), node_id),
    )
    await conn.commit()


async def list_pending_knowledge_index(conn, limit: int = 50) -> list[dict]:
    """返回待索引到 chromadb 的知识节点列表（indexed_at IS NULL）。

    MemoryIndexerWorker 每轮扫描时调用。按 created_at 升序——先索引旧的。

    Task 6.5: 返回 project_id 字段——worker 索引时写入 chromadb metadata。
    """
    cursor = await conn.execute(
        "SELECT id, title, content, project_id FROM knowledge_nodes "
        "WHERE indexed_at IS NULL ORDER BY created_at ASC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [
        {"id": r[0], "title": r[1], "content": r[2], "project_id": r[3]}
        for r in rows
    ]
