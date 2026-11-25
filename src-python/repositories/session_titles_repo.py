"""会话标题持久化（对齐 DSH `session/title`：最新生效 + 用户置顶）

表结构见 repositories/schema.py 的 session_titles：
  session_id        主键
  title             当前生效标题
  source            user | fallback | provider
  provider/model    模型润色时记录来源，便于排查"这个标题是谁生成的"
  source_message_id 派生标题所依据的消息 id
  auto_attempted    自动生成是否已认领过（防止每个任务都去调一次模型）
  updated_at        最后更新时间

置顶语义（DSH 同款）：source='user' 的标题不会被自动生成覆盖，
除非调用方显式传 force=True（对应 DSH 的显式 refresh 解除置顶）。
"""
from __future__ import annotations

import time as _time

from session_titles import SOURCE_FALLBACK, SOURCE_PROVIDER, SOURCE_USER

__all__ = [
    "get_session_title",
    "set_session_title",
    "delete_session_title",
    "claim_auto_attempt",
]


async def get_session_title(conn, session_id: str) -> dict | None:
    """读取会话当前标题；没有记录返回 None。"""
    if not session_id:
        return None
    cursor = await conn.execute(
        "SELECT session_id, title, source, provider, model, source_message_id, "
        "auto_attempted, updated_at FROM session_titles WHERE session_id=?",
        (session_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return {
        "session_id": row[0], "title": row[1] or "", "source": row[2] or SOURCE_FALLBACK,
        "provider": row[3] or "", "model": row[4] or "",
        "source_message_id": row[5] or "", "auto_attempted": bool(row[6]),
        "updated_at": row[7],
    }


async def set_session_title(
    conn, session_id: str, title: str, source: str, *,
    provider: str = "", model: str = "", source_message_id: str = "",
    auto_attempted: int | None = None, force: bool = False,
) -> dict | None:
    """写入会话标题。

    - source != user 且已存在 user 标题时**不覆盖**（置顶语义），除非 force=True；
    - auto_attempted=None 表示保持原值，传入 0/1 则显式覆盖。
    """
    text = str(title or "").strip()
    if not text or not session_id:
        return None
    if not force and source != SOURCE_USER:
        existing = await get_session_title(conn, session_id)
        if existing and existing["source"] == SOURCE_USER:
            return existing
    fields = ("title=excluded.title, source=excluded.source, provider=excluded.provider, "
              "model=excluded.model, source_message_id=excluded.source_message_id, "
              "updated_at=excluded.updated_at")
    if auto_attempted is not None:
        fields += ", auto_attempted=excluded.auto_attempted"
    await conn.execute(
        "INSERT INTO session_titles(session_id, title, source, provider, model, "
        "source_message_id, auto_attempted, updated_at) VALUES(?,?,?,?,?,?,?,?) "
        f"ON CONFLICT(session_id) DO UPDATE SET {fields}",
        (session_id, text, source, provider, model, source_message_id,
         int(auto_attempted or 0), int(_time.time())),
    )
    await conn.commit()
    return await get_session_title(conn, session_id)


async def delete_session_title(conn, session_id: str) -> int:
    """删除会话标题（会话删除时联动清理）。"""
    if not session_id:
        return 0
    cursor = await conn.execute(
        "DELETE FROM session_titles WHERE session_id=?", (session_id,))
    await conn.commit()
    return cursor.rowcount


async def claim_auto_attempt(conn, session_id: str, fallback_title: str) -> bool:
    """原子认领一次"自动生成标题"的机会。

    返回 True 表示本次调用方应当去调模型；False 表示
    「用户已置顶」「已有模型润色标题」或「此前已尝试过」——都不应再调模型。

    同时把确定性回退标题落库，保证即使模型不可用也有稳定标题；
    但不会把已有的 user/provider 标题降级成回退标题。
    """
    if not session_id:
        return False
    now = int(_time.time())
    cursor = await conn.execute(
        "INSERT INTO session_titles(session_id, title, source, auto_attempted, updated_at) "
        "VALUES(?,?,?,1,?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "  title=CASE WHEN session_titles.source IN (?, ?) "
        "            THEN session_titles.title ELSE excluded.title END, "
        "  auto_attempted=1, updated_at=excluded.updated_at "
        "WHERE session_titles.auto_attempted=0 "
        "  AND session_titles.source NOT IN (?, ?)",
        (session_id, fallback_title or "", SOURCE_FALLBACK, now,
         SOURCE_USER, SOURCE_PROVIDER, SOURCE_USER, SOURCE_PROVIDER),
    )
    await conn.commit()
    return cursor.rowcount == 1
