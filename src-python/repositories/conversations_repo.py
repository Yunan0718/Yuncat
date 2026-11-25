"""repositories/conversations_repo.py —— 对话记录 CRUD + 会话分支"""
import time as _time
import uuid


async def insert_conversation(
    conn, session_id: str, role: str, content: str, token_count: int | None = None,
    agent_id: str | None = None, tool_calls_json: str | None = None,
    reasoning_content: str | None = None,
) -> str:
    """写入对话记录到 conversations 表。返回生成的 id。

    v66: 默认不再每次 commit——由调用方批量 commit。高频对话时性能提升 ~80%。
    调用方可每 5 条消息或在会话边界调用 await conn.commit()。
    Phase 47: 新增 tool_calls_json——持久化 tool_calls 结构，避免重启失忆。
    """
    conv_id = str(uuid.uuid4())
    # 2026-10-07：补 reasoning_content。此前写入端没有这一列，思考链只存在内存/快照里，
    # 重启后 conversations 表查回来是空的（实测全库 reasoning_content 非空数为 0）。
    await conn.execute(
        "INSERT INTO conversations(id, session_id, agent_id, role, content, token_count, "
        "tool_calls_json, reasoning_content) VALUES(?,?,?,?,?,?,?,?)",
        (conv_id, session_id, agent_id, role, content, token_count, tool_calls_json,
         reasoning_content),
    )
    return conv_id


async def flush_conversations(conn) -> None:
    """批量提交对话记录（在请求结束时或每 5 条消息后调用）"""
    await conn.commit()


async def get_conversation(conn, session_id: str, limit: int = 50) -> list[dict]:
    """获取指定会话最近的对话记录（时间升序）"""
    cursor = await conn.execute(
        "SELECT id, session_id, role, content, token_count, agent_id, "
        "COALESCE(reasoning_content, '') as reasoning_content, "
        "COALESCE(tool_calls_json, '') as tool_calls_json, created_at "
        "FROM conversations WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
        (session_id, limit),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": row[0], "session_id": row[1], "role": row[2],
            "content": row[3], "token_count": row[4], "agent_id": row[5],
            "reasoning_content": row[6], "tool_calls_json": row[7], "created_at": row[8],
        }
        for row in reversed(rows)
    ]


async def fork_conversation(
    conn, session_id: str, from_message_id: str,
) -> str | None:
    """Fork 会话：复制 from_message_id 之前（含）的所有消息到新 session。

    返回新 session_id，或 None（消息不存在/不属于该会话）。
    """
    import time as _ftime

    cursor = await conn.execute(
        "SELECT session_id, created_at FROM conversations WHERE id=?",
        (from_message_id,),
    )
    fork_point = await cursor.fetchone()
    if not fork_point or fork_point[0] != session_id:
        return None

    cutoff = fork_point[1]
    new_session_id = str(uuid.uuid4())

    cursor = await conn.execute(
        "SELECT role, content, token_count FROM conversations "
        "WHERE session_id=? AND created_at <= ? ORDER BY created_at ASC",
        (session_id, cutoff),
    )
    rows = await cursor.fetchall()
    if not rows:
        return None

    for row in rows:
        new_id = str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO conversations(id, session_id, role, content, token_count, "
            "forked_from, created_at) VALUES(?,?,?,?,?,?,?)",
            (new_id, new_session_id, row[0], row[1], row[2],
             session_id, int(_ftime.time())),
        )

    await conn.commit()
    return new_session_id


async def delete_conversations_by_ids(conn, conv_ids: list[str]) -> int:
    """批量删除指定 conv_id 的对话记录——供 purge_context 写穿透 DB 用。

    架构铁律 #2: 先写 DB 再更新内存缓存。purge_context 删除内存前先调此函数。

    Args:
        conn: aiosqlite 连接
        conv_ids: 要删除的 conversation id 列表

    Returns:
        删除的行数
    """
    if not conv_ids:
        return 0
    placeholders = ",".join("?" * len(conv_ids))
    cursor = await conn.execute(
        f"DELETE FROM conversations WHERE id IN ({placeholders})",
        conv_ids,
    )
    await conn.commit()
    return cursor.rowcount


async def edit_conversation(conn, conv_id: str, new_content: str) -> dict:
    """基于原消息创建新的用户消息记录，建立版本链。

    返回 {"conv_id": 新消息 id, "root_id": 版本树根 id}。
    """
    cursor = await conn.execute(
        "SELECT session_id, role, agent_id, COALESCE(variant_of, '') "
        "FROM conversations WHERE id=?",
        (conv_id,),
    )
    row = await cursor.fetchone()
    if not row:
        raise ValueError(f"Conversation {conv_id} not found")
    session_id, role, agent_id, variant_of = row
    root_id = variant_of or conv_id

    cursor = await conn.execute(
        "SELECT COALESCE(MAX(version_index), 0) FROM conversations "
        "WHERE variant_of=? OR id=?",
        (root_id, root_id),
    )
    max_index = (await cursor.fetchone())[0] or 0
    new_index = max_index + 1
    new_id = str(uuid.uuid4())

    await conn.execute(
        "INSERT INTO conversations(id, session_id, role, content, agent_id, "
        "edited_from, variant_of, version_index, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (new_id, session_id, role, new_content, agent_id,
         conv_id, root_id, new_index, int(_time.time())),
    )
    await conn.execute(
        "UPDATE conversations SET edited_to=? WHERE id=?",
        (new_id, conv_id),
    )
    return {"conv_id": new_id, "root_id": root_id}


async def get_conversation_versions(conn, conv_id: str) -> dict:
    """返回以该消息为根的版本树：节点 + 边。"""
    cursor = await conn.execute(
        "SELECT COALESCE(variant_of, id) FROM conversations WHERE id=?",
        (conv_id,),
    )
    row = await cursor.fetchone()
    root_id = row[0] if row else conv_id

    cursor = await conn.execute(
        "SELECT id, role, content, COALESCE(version_index, 0), "
        "COALESCE(edited_from, ''), COALESCE(edited_to, ''), "
        "COALESCE(variant_of, ''), created_at "
        "FROM conversations WHERE id=? OR variant_of=?",
        (root_id, root_id),
    )
    rows = await cursor.fetchall()
    nodes = []
    edges = []
    for r in rows:
        nid, role, content, vidx, edited_from, edited_to, variant_of, created_at = r
        nodes.append({
            "id": nid, "role": role, "content": content,
            "version_index": vidx, "edited_from": edited_from or None,
            "edited_to": edited_to or None, "variant_of": variant_of or None,
            "created_at": created_at,
        })
        if edited_from:
            edges.append({"from": edited_from, "to": nid, "label": "edit"})
        if edited_to:
            edges.append({"from": nid, "to": edited_to, "label": "edit"})
    return {"root_id": root_id, "nodes": nodes, "edges": edges}


def _fts5_phrase(q: str) -> str:
    """Batch 3b B4: 关键词转 FTS5 phrase——双引号包裹，内部引号翻倍转义。

    防止用户输入含 FTS5 查询语法字符（- " ( ) * : 等）导致 MATCH 语法错误。
    """
    return '"' + q.replace('"', '""') + '"'


async def search_conversations(conn, q: str, limit: int = 20) -> list[dict]:
    """全文搜索历史会话（FTS5 优先 + LIKE 回退）——单一来源。

    从 main.py 的 /api/v1/conversations/search 抽取：HTTP 端点与 agent 工具
    （search_conversation_history）共用同一实现，防止两端逻辑漂移。
    三类场景回退 LIKE，保证召回不退化：
    - FTS5 不可用（旧库未跑到迁移 #32，查询抛 no such table）
    - FTS5 0 命中（unicode61 tokenizer 对中文整段无分隔的局限）

    返回: [{"session_id", "preview", "role", "created_at"}, ...]
    """
    if not q or not q.strip():
        return []

    rows = None
    try:
        cursor = await conn.execute(
            "SELECT c.session_id, c.content, c.role, c.created_at, "
            "SUBSTR(c.content, 1, 80) AS preview "
            "FROM conversations_fts f JOIN conversations c ON c.id = f.conversation_id "
            "WHERE conversations_fts MATCH ? "
            "ORDER BY c.created_at DESC LIMIT ?",
            (_fts5_phrase(q), limit),
        )
        rows = await cursor.fetchall()
    except Exception:
        rows = None  # FTS5 不可用（旧库无 conversations_fts 虚表）——回退 LIKE

    if rows is None or not rows:
        cursor = await conn.execute(
            "SELECT c.session_id, c.content, c.role, c.created_at, "
            "SUBSTR(c.content, 1, 80) AS preview "
            "FROM conversations c WHERE c.content LIKE ? "
            "ORDER BY c.created_at DESC LIMIT ?",
            (f"%{q}%", limit),
        )
        fallback = await cursor.fetchall()
        if fallback:
            rows = fallback
        elif rows is None:
            rows = []

    return [
        {"session_id": r[0], "preview": r[4], "role": r[2], "created_at": r[3]}
        for r in rows
    ]
