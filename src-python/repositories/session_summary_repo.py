"""repositories/session_summary_repo.py —— M3: 跨 session 记忆继承

持久化 session 结构化摘要——对标 Claude Code --continue。

写入时机：上下文压缩（memory_compact.py）产出结构化摘要后 UPSERT。
读取时机：新会话首条消息（agent_context.py）按 workspace 查询上次摘要注入。

表：session_summaries(session_id PK, summary_json, workspace, created_at, updated_at)
"""
import json as _json
import time as _time
import logging as _logging

_log = _logging.getLogger("yuncat.repo.session_summary")


async def save_session_summary(
    conn, session_id: str, summary: dict, workspace: str, *, commit: bool = True,
) -> None:
    """M3: 持久化 session 结构化摘要（UPSERT）。

    Args:
        conn: aiosqlite 连接
        session_id: 会话 ID
        summary: 6-section 结构化摘要 dict
        workspace: workspace 路径（项目维度）
    """
    now = int(_time.time())
    await conn.execute(
        "INSERT INTO session_summaries(session_id, summary_json, workspace, created_at, updated_at) "
        "VALUES(?,?,?,?,?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "summary_json=excluded.summary_json, workspace=excluded.workspace, updated_at=excluded.updated_at",
        (session_id, _json.dumps(summary, ensure_ascii=False), workspace, now, now),
    )
    if commit:
        await conn.commit()


async def get_last_session_summary(
    conn, workspace: str, exclude_session_id: str,
) -> dict | None:
    """M3: 获取同 workspace 上次会话的结构化摘要（排除当前 session）。

    Args:
        conn: aiosqlite 连接
        workspace: 当前 workspace 路径
        exclude_session_id: 排除的当前 session_id

    Returns:
        {"session_id": str, "summary": dict} 或 None（无匹配）
    """
    cursor = await conn.execute(
        "SELECT session_id, summary_json FROM session_summaries "
        "WHERE workspace=? AND session_id!=? "
        "ORDER BY updated_at DESC LIMIT 1",
        (workspace, exclude_session_id),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    try:
        return {"session_id": row[0], "summary": _json.loads(row[1])}
    except (ValueError, TypeError):
        _log.debug("Corrupt summary_json for session %s", row[0])
        return None
