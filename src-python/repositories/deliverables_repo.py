"""会话交付物持久化（对齐 DSH `deliverables`：present 声明 + 观测到的改动）。"""
from __future__ import annotations

import time as _time

__all__ = [
    "add_session_deliverable",
    "list_session_deliverables",
    "delete_session_deliverables",
]


async def add_session_deliverable(
    conn, session_id: str, file_path: str, *, rel_path: str = "",
    description: str = "", source: str = "present", task_id: str = "",
) -> int:
    """记录一条交付物；同一路径重复声明时更新描述而不是追加重复行。

    去重口径 = (session_id, file_path, source)：同一会话里同一个文件的同类声明
    只留一行。shell 改动会在多轮里被反复检出，没有去重就会攒出一堆重复行。
    """
    if not session_id or not file_path:
        return 0
    source = source or "present"
    cursor = await conn.execute(
        "SELECT id FROM session_deliverables "
        "WHERE session_id=? AND file_path=? AND source=? LIMIT 1",
        (session_id, file_path, source),
    )
    row = await cursor.fetchone()
    if row:
        await conn.execute(
            "UPDATE session_deliverables SET rel_path=?, description=?, task_id=? WHERE id=?",
            (rel_path or "", description or "", task_id or "", int(row[0])),
        )
        await conn.commit()
        return int(row[0])
    await conn.execute(
        "INSERT INTO session_deliverables(session_id, task_id, file_path, rel_path, "
        "description, source, created_at) VALUES(?,?,?,?,?,?,?)",
        (session_id, task_id or "", file_path, rel_path or "",
         description or "", source, int(_time.time())),
    )
    await conn.commit()
    cursor = await conn.execute("SELECT last_insert_rowid()")
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def list_session_deliverables(conn, session_id: str) -> list[dict]:
    """读取会话的交付物（按写入顺序）。"""
    if not session_id:
        return []
    cursor = await conn.execute(
        "SELECT id, session_id, task_id, file_path, rel_path, description, source, created_at "
        "FROM session_deliverables WHERE session_id=? ORDER BY id ASC",
        (session_id,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r[0], "session_id": r[1], "task_id": r[2], "file_path": r[3],
            "rel_path": r[4], "description": r[5], "source": r[6], "created_at": r[7],
        }
        for r in rows
    ]


async def delete_session_deliverables(conn, session_id: str) -> int:
    """删除会话的交付物（会话删除时联动清理）。"""
    if not session_id:
        return 0
    cursor = await conn.execute(
        "DELETE FROM session_deliverables WHERE session_id=?", (session_id,))
    await conn.commit()
    return cursor.rowcount
