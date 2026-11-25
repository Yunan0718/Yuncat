"""会话任务清单持久化（对齐 DSH `tool-todo` 的 latest-wins 整表替换语义）。"""
from __future__ import annotations

import json
import time as _time

__all__ = ["get_session_todos", "set_session_todos", "delete_session_todos"]


async def get_session_todos(conn, session_id: str) -> list[dict]:
    """读取会话当前任务清单；没有记录返回空列表。"""
    if not session_id:
        return []
    cursor = await conn.execute(
        "SELECT todos_json FROM session_todos WHERE session_id=?", (session_id,))
    row = await cursor.fetchone()
    if not row:
        return []
    try:
        data = json.loads(row[0] or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


async def set_session_todos(
    conn, session_id: str, todos: list[dict], owner_agent_id: str = "",
) -> list[dict]:
    """整表替换会话任务清单（DSH 语义：每次写入都是完整列表）。"""
    if not session_id:
        return []
    payload = json.dumps(todos or [], ensure_ascii=False)
    await conn.execute(
        "INSERT INTO session_todos(session_id, todos_json, owner_agent_id, updated_at) "
        "VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
        "todos_json=excluded.todos_json, owner_agent_id=excluded.owner_agent_id, "
        "updated_at=excluded.updated_at",
        (session_id, payload, owner_agent_id or "", int(_time.time())),
    )
    await conn.commit()
    return list(todos or [])


async def delete_session_todos(conn, session_id: str) -> int:
    """删除会话任务清单（会话删除时联动清理）。"""
    if not session_id:
        return 0
    cursor = await conn.execute(
        "DELETE FROM session_todos WHERE session_id=?", (session_id,))
    await conn.commit()
    return cursor.rowcount
