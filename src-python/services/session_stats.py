"""services/session_stats.py —— 会话维度的用量/任务统计

会话列表此前只有「消息数 + 最后活动时间」，看不到**这个会话花了多少 token**、
跑过多少任务、失败过几次——用户问「各个信息的统计、用量」时无处可查。

口径（与既有看板一致，不另起一套）：
- token：`services/token_tracker` 的**账本口径**（输入 + 输出，真实消耗），
  事件类型与字段名一律走 `services/llm_usage.py` 的常量；
- 任务：`tasks` 表按会话聚合；
- 时长：首条消息 → 最后一条消息。
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger("yuncat.session_stats")


async def attach_session_stats(db, sessions: list[dict]) -> list[dict]:
    """给会话列表补上 token / 任务统计（就地附加字段后返回同一列表）。"""
    if not sessions:
        return sessions
    from repositories.loop_repo import get_session_task_stats
    from services.token_tracker import get_session_token_totals

    ids = [str(s.get("session_id") or "") for s in sessions if s.get("session_id")]
    try:
        tokens = await get_session_token_totals(db, days=0)
        tasks = await get_session_task_stats(db, ids)
    except Exception:
        # 统计失败不该让整个会话列表 500——退回"没有统计"，界面显示 0
        _log.warning("会话统计读取失败（列表照常返回，统计显示为 0）", exc_info=True)
        tokens, tasks = {}, {}
    for row in sessions:
        sid = str(row.get("session_id") or "")
        tk = tokens.get(sid, {})
        st = tasks.get(sid, {})
        row["total_tokens"] = tk.get("total_tokens", 0)
        row["prompt_tokens"] = tk.get("prompt_tokens", 0)
        row["completion_tokens"] = tk.get("completion_tokens", 0)
        row["llm_calls"] = tk.get("call_count", 0)
        row["task_count"] = st.get("tasks", 0)
        row["completed_tasks"] = st.get("completed", 0)
        row["failed_tasks"] = st.get("failed", 0)
    return sessions


async def _conversation_counts(db, cutoff: int) -> tuple[int, int]:
    """(会话数, 消息数)——按时间窗统计。"""
    cursor = await db.execute(
        "SELECT COUNT(DISTINCT session_id), COUNT(*) FROM conversations WHERE created_at > ?",
        (cutoff,),
    )
    row = await cursor.fetchone()
    return (row[0] or 0, row[1] or 0)


async def _task_breakdown(db, cutoff: int) -> tuple[int, dict, list]:
    """(任务数, 状态分布, agent 分布)——按时间窗统计。"""
    cursor = await db.execute("SELECT COUNT(*) FROM tasks WHERE created_at > ?", (cutoff,))
    task_count = (await cursor.fetchone())[0] or 0

    cursor = await db.execute(
        "SELECT status, COUNT(*) FROM tasks WHERE created_at > ? GROUP BY status",
        (cutoff,),
    )
    by_status = {str(r[0] or "unknown"): r[1] or 0 for r in await cursor.fetchall()}

    cursor = await db.execute(
        "SELECT agent_id, COUNT(*), COUNT(DISTINCT session_id) FROM tasks "
        "WHERE created_at > ? GROUP BY agent_id ORDER BY 2 DESC",
        (cutoff,),
    )
    by_agent = [
        {"agent_id": str(r[0] or "unknown"), "tasks": r[1] or 0, "sessions": r[2] or 0}
        for r in await cursor.fetchall()
    ]
    return task_count, by_status, by_agent


async def _daily_series(db, cutoff: int) -> list[dict]:
    """按天的会话数与消息数。"""
    cursor = await db.execute(
        "SELECT DATE(created_at, 'unixepoch') AS day, COUNT(DISTINCT session_id), COUNT(*) "
        "FROM conversations WHERE created_at > ? GROUP BY day ORDER BY day",
        (cutoff,),
    )
    return [
        {"day": str(r[0]), "sessions": r[1] or 0, "messages": r[2] or 0}
        for r in await cursor.fetchall()
    ]


async def session_stats_overview(db, days: int = 30) -> dict:
    """跨会话汇总：会话/消息/任务数、用量、状态与 agent 分布、每日序列。"""
    from services.token_tracker import get_token_usage_overview

    from runtime_bounds import SESSION_STATS_DEFAULT_DAYS, SESSION_STATS_MAX_DAYS

    days = max(1, min(int(days or SESSION_STATS_DEFAULT_DAYS), SESSION_STATS_MAX_DAYS))
    cutoff = int(time.time()) - days * 86400

    session_count, message_count = await _conversation_counts(db, cutoff)
    task_count, by_status, by_agent = await _task_breakdown(db, cutoff)
    daily = await _daily_series(db, cutoff)

    usage = await get_token_usage_overview(db, days=days)
    return {
        "days": days,
        "session_count": session_count,
        "message_count": message_count,
        "task_count": task_count,
        "by_status": by_status,
        "by_agent": by_agent,
        "daily": daily,
        "total_tokens": usage.get("total_tokens", 0),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "cached_tokens": usage.get("cached_tokens", 0),
        "reasoning_tokens": usage.get("reasoning_tokens", 0),
        "llm_calls": usage.get("call_count", 0),
    }
