"""repositories/memories_repo.py —— 语义记忆 CRUD + FTS5 检索"""
import math
import time
import uuid

from config import (
    MEMORY_DECAY_OVERFETCH,
    MEMORY_DECAY_RATE,
    MEMORY_FACT_KEYWORDS_MAX,
)


async def insert_memory(
    conn, content: str, source: str = "", owner: str = "",
    confidence: float = 0.5, visibility: str = "private", memory_type: str = "fact",
    project_id: str | None = None,
) -> str:
    """写入语义记忆到 memories 表。

    Task 6.5: project_id 参数——NULL 或空字符串存储为 NULL（global memory，
    对所有项目可见）。对标 Claude Code CLAUDE.md 项目隔离。
    """
    # 空字符串统一转为 None（避免空串与 NULL 双语义）
    _project_id = project_id if project_id else None
    mem_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO memories(id, content, source, owner, confidence, visibility, memory_type, project_id) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (mem_id, content, source, owner, confidence, visibility, memory_type, _project_id),
    )
    await conn.commit()
    return mem_id


async def delete_memory(conn, mem_id: str) -> bool:
    """Phase 18: 删除语义记忆"""
    cursor = await conn.execute("DELETE FROM memories WHERE id=?", (mem_id,))
    if cursor.rowcount == 0:
        return False
    await conn.commit()
    return True


async def update_memory(conn, mem_id: str, **fields) -> bool:
    """Phase 18: 编辑语义记忆——只更新传入的字段

    Task 6.5: 新增 project_id 到 allowed 集合——支持迁移记忆到其他项目。
    """
    allowed = {"content", "confidence", "memory_type", "source", "owner", "project_id"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [mem_id]
    cursor = await conn.execute(
        f"UPDATE memories SET {set_clause} WHERE id=?", values,
    )
    if cursor.rowcount == 0:
        return False
    await conn.commit()
    return True


async def list_recent_memories(
    conn, limit: int = 50, project_id: str | None = None,
) -> list[dict]:
    """列出最近语义记忆，按 created_at 降序。

    Task 6.5: project_id 过滤——非空时只返回该项目 + global 记忆
    （NULL = global，对标 CLAUDE.md global，对所有项目可见）。
    """
    if project_id:  # 非空字符串才过滤
        cursor = await conn.execute(
            "SELECT id, content, source, owner, confidence, memory_type, verified, created_at "
            "FROM memories WHERE (project_id = ? OR project_id IS NULL) "
            "ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        )
    else:
        cursor = await conn.execute(
            "SELECT id, content, source, owner, confidence, memory_type, verified, created_at "
            "FROM memories ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    rows = await cursor.fetchall()
    return [
        {"id": r[0], "content": r[1], "source": r[2], "owner": r[3],
         "confidence": r[4], "memory_type": r[5], "verified": r[6], "created_at": r[7]}
        for r in rows
    ]


async def find_memory_conflicts(conn, limit: int = 10) -> list[dict]:
    """Phase 18: 基于关键词重叠检测潜在冲突的记忆对。
    当两条记忆涉及同一主题但置信度不同方向时，返回为冲突。
    简化实现——按内容前缀聚类 + 置信度差异检测。"""
    cursor = await conn.execute(
        "SELECT id, content, confidence, memory_type FROM memories "
        "WHERE verified=0 ORDER BY RANDOM() LIMIT ?", (limit * 3,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return []

    # 提取每条的简化 key（前 3 个词）
    import re
    def key_of(text: str) -> str:
        words = re.findall(r'[一-鿿\w]+', text.lower())
        return " ".join(words[:MEMORY_FACT_KEYWORDS_MAX])

    groups: dict[str, list[dict]] = {}
    for row in rows:
        k = key_of(row[1])
        groups.setdefault(k, []).append({"id": row[0], "content": row[1], "confidence": row[2], "type": row[3]})
    conflicts = []
    for items in groups.values():
        if len(items) >= 2:
            confs = [i["confidence"] for i in items]
            # 置信度差异 > 0.4 认为可能冲突
            if max(confs) - min(confs) > 0.4:
                conflicts.append({"key": key_of(items[0]["content"]), "items": items})
    return conflicts[:limit]


def _compute_memory_decay_factor(age_days: float, rate: float = MEMORY_DECAY_RATE) -> float:
    """Batch 2.5: Ebbinghaus 时间衰减因子——exp(-rate * age_days)。

    - age <= 0 → 1.0（无衰减）
    - age 越大 → 因子越小（旧记忆失权）
    """
    if age_days <= 0:
        return 1.0
    return math.exp(-rate * age_days)


def _build_memory_filter_sql(filters: dict | None) -> tuple[list[str], list]:
    """根据 filters 生成 memories 表 WHERE 附加条件与参数。"""
    conds: list[str] = []
    params: list = []
    if not filters:
        return conds, params
    if filters.get("source") is not None:
        conds.append("m.source = ?")
        params.append(filters["source"])
    if filters.get("owner") is not None:
        conds.append("m.owner = ?")
        params.append(filters["owner"])
    if filters.get("memory_type") is not None:
        conds.append("m.memory_type = ?")
        params.append(filters["memory_type"])
    if filters.get("verified") is not None:
        conds.append("m.verified = ?")
        params.append(1 if filters["verified"] else 0)
    if filters.get("min_confidence") is not None:
        conds.append("m.confidence >= ?")
        params.append(float(filters["min_confidence"]))
    # Task 6.5: project_id 过滤——非空字符串才过滤（None / 空串视为不过滤）
    pid = filters.get("project_id")
    if pid:  # 非空字符串
        conds.append("(m.project_id = ? OR m.project_id IS NULL)")
        params.append(pid)
    return conds, params


def _apply_decay_ranking(
    rows: list, limit: int, *, now: int | None = None,
) -> list[dict]:
    """Batch 2.5: 按 created_at 计算 age，将 BM25 分数乘以衰减因子后重排。

    输入 row 结构：(id, content, source, owner, confidence, memory_type, verified,
    bm25_score, created_at)。
    返回 dict 列表，score 字段为衰减后的最终分数（越大越相关）。
    """
    now_ts = now if now is not None else int(time.time())
    scored: list[tuple[float, dict]] = []
    for row in rows:
        bm25_score = -float(row[7]) if row[7] is not None else 0.0
        created_at = row[8] or now_ts
        age_days = (now_ts - created_at) / 86400.0
        decay = _compute_memory_decay_factor(age_days)
        final_score = bm25_score * decay
        scored.append((final_score, {
            "id": row[0], "content": row[1], "source": row[2], "owner": row[3],
            "confidence": row[4], "memory_type": row[5], "verified": row[6],
            "score": final_score,
        }))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in scored[:limit]]


async def search_memories_fts(
    conn, query: str, limit: int = 20, filters: dict | None = None,
) -> list[dict]:
    """FTS5 全文搜索语义记忆。

    Phase 54: 支持下推标量过滤（source/owner/memory_type/verified/min_confidence）。
    Task 6.5: 支持下推 project_id 过滤——NULL = global memory 对所有项目可见
              （对标 Claude Code CLAUDE.md global）；空字符串视为不过滤（向后兼容）。
    Batch 2.5: 结果按 Ebbinghaus 时间衰减重排——旧记忆 BM25 分数乘以衰减因子。
    """
    from repositories.knowledge_repo import _sanitize_fts5_query  # Phase 48 T1-11: 净化 FTS5 特殊字符
    sanitized = _sanitize_fts5_query(query)

    filter_conds, filter_params = _build_memory_filter_sql(filters)
    conds = ["memories_fts MATCH ?"] + filter_conds
    params: list = [sanitized, *filter_params]

    # Batch 2.5: 先取 limit*overfetch 候选，再按时间衰减重排。
    pool_limit = max(limit, limit * MEMORY_DECAY_OVERFETCH)
    sql = (
        # Task 4.5: bm25() 返回负数（越小越相关），转成正数供 _weighted_fuse 归一化
        "SELECT m.id, m.content, m.source, m.owner, m.confidence, m.memory_type, m.verified, "
        "bm25(memories_fts) as bm25_score, m.created_at "
        "FROM memories_fts f JOIN memories m ON f.memory_id = m.id "
        f"WHERE {' AND '.join(conds)} ORDER BY rank LIMIT ?"
    )
    params.append(pool_limit)

    cursor = await conn.execute(sql, tuple(params))
    rows = await cursor.fetchall()
    return _apply_decay_ranking(rows, limit)


async def verify_memory_record(conn, mem_id: str) -> None:
    """标记记忆为已验证"""
    await conn.execute("UPDATE memories SET verified=1 WHERE id=?", (mem_id,))
    await conn.commit()


# ═════════════════════════════════════════════════════════════
# Task 4.4: RAG 索引一致性 — Outbox pattern 辅助函数
# indexed_at IS NULL 表示待索引到 chromadb；MemoryIndexerWorker 兜底补索引
# ═════════════════════════════════════════════════════════════

async def mark_memory_indexed(conn, mem_id: str) -> None:
    """标记记忆为已索引到 chromadb（indexed_at = 当前时间戳）。

    store_memory 同步索引成功后调用；MemoryIndexerWorker 兜底索引成功后也调用。
    """
    import time as _time
    await conn.execute(
        "UPDATE memories SET indexed_at=? WHERE id=?",
        (int(_time.time()), mem_id),
    )
    await conn.commit()


async def list_pending_memory_index(conn, limit: int = 50) -> list[dict]:
    """返回待索引到 chromadb 的记忆列表（indexed_at IS NULL）。

    MemoryIndexerWorker 每轮扫描时调用。按 created_at 升序——先索引旧的。

    Task 6.5: 返回 project_id 字段——worker 索引时写入 chromadb metadata。
    """
    cursor = await conn.execute(
        "SELECT id, content, source, owner, memory_type, verified, project_id FROM memories "
        "WHERE indexed_at IS NULL ORDER BY created_at ASC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [
        {"id": r[0], "content": r[1], "source": r[2], "owner": r[3],
         "memory_type": r[4], "verified": r[5], "project_id": r[6]}
        for r in rows
    ]
