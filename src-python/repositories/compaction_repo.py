"""压缩 checkpoint 持久化——摘要与来源消息 ID 的单一真源。"""
import json
import logging
import time

_log = logging.getLogger("yuncat.repo.compaction")


async def get_compaction_checkpoint(conn, session_id: str) -> dict | None:
    cursor = await conn.execute(
        "SELECT checkpoint_id, summary_json, source_message_ids_json, "
        "source_message_count, created_at FROM compaction_checkpoints WHERE session_id=?",
        (session_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    try:
        summary = json.loads(row[1])
        source_ids = json.loads(row[2] or "[]")
    except (TypeError, ValueError):
        _log.warning("Corrupt compaction checkpoint for session %s", session_id[:8])
        return None
    if not isinstance(summary, dict) or not isinstance(source_ids, list):
        _log.warning("Invalid compaction checkpoint shape for session %s", session_id[:8])
        return None
    return {
        "checkpoint_id": row[0], "summary": summary,
        "source_message_ids": [str(item) for item in source_ids],
        "source_message_count": row[3], "created_at": row[4],
    }


async def commit_compaction_checkpoint(
    conn, session_id: str, checkpoint_id: str, summary: dict,
    source_message_ids: list[str], workspace: str,
) -> dict:
    """原子提交 checkpoint 和跨会话摘要；失败回滚，不留下半状态。"""
    previous = await get_compaction_checkpoint(conn, session_id)
    merged_ids = list(dict.fromkeys([
        *(previous or {}).get("source_message_ids", []),
        *source_message_ids,
    ]))
    now = int(time.time())
    try:
        await conn.execute(
            "INSERT INTO compaction_checkpoints(session_id, checkpoint_id, summary_json, "
            "source_message_ids_json, source_message_count, created_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET checkpoint_id=excluded.checkpoint_id, "
            "summary_json=excluded.summary_json, "
            "source_message_ids_json=excluded.source_message_ids_json, "
            "source_message_count=excluded.source_message_count, created_at=excluded.created_at",
            (session_id, checkpoint_id, json.dumps(summary, ensure_ascii=False),
             json.dumps(merged_ids, ensure_ascii=False), len(merged_ids), now),
        )
        from repositories.session_summary_repo import save_session_summary
        await save_session_summary(conn, session_id, summary, workspace, commit=False)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return {"source_message_ids": merged_ids, "source_message_count": len(merged_ids)}
