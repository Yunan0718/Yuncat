"""repositories/scheduled_tasks_repo.py —— AUDIT #9 cron 调度任务 CRUD

独立于 loop_repo.py（Phase 47 拆分模式的延续）：
- loop_repo.py: loop_trigger_queue + deliberation 任务
- scheduled_tasks_repo.py: cron 调度任务（real cron expression evaluation）

关键设计：
- claim_due_scheduled_tasks 用 UPDATE ... RETURNING 原子认领（防并发双触发）
- complete_scheduled_task 重新计算 next_run_at 恢复 pending（循环 cron）
- fail_scheduled_task 恢复 pending + error_count+1（下次仍会触发）
"""
import time as _t
import uuid as _uuid


async def insert_scheduled_task(
    conn, task_input: str, cron_expr: str, next_run_at: int,
    agent_id: str = "hermes", session_id: str | None = None,
    label: str = "", notify: str = "", created_by: str | None = None,
    max_budget_tokens: int | None = None,
) -> str:
    """插入 scheduled_tasks 行，返回 task_id（uuid hex[:12]）

    AUDIT #10/#13: max_budget_tokens 为该 cron 任务设置 Token 预算上限。
    每次触发执行时，_scheduler_tick 将此值传入 task 的 max_budget_tokens。
    """
    tid = str(_uuid.uuid4())[:12]
    await conn.execute(
        "INSERT INTO scheduled_tasks("
        "id, task_input, agent_id, session_id, cron_expr, next_run_at, "
        "status, label, notify, created_by, created_at, max_budget_tokens"
        ") VALUES(?,?,?,?,?,?, 'pending', ?, ?, ?, ?, ?)",
        (tid, task_input, agent_id, session_id, cron_expr, next_run_at,
         label, notify, created_by, int(_t.time()), max_budget_tokens),
    )
    await conn.commit()
    return tid


async def list_scheduled_tasks(conn, limit: int = 50) -> list[dict]:
    """列出所有 pending 的 scheduled_tasks（按 next_run_at 升序）

    AUDIT #10/#13: 返回 max_budget_tokens / used_tokens 供 cron_list 展示预算状态。
    """
    cursor = await conn.execute(
        "SELECT id, task_input, agent_id, cron_expr, next_run_at, label, "
        "created_at, last_run_at, run_count, error_count, "
        "max_budget_tokens, used_tokens "
        "FROM scheduled_tasks WHERE status='pending' "
        "ORDER BY next_run_at ASC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r[0], "task_input": r[1], "agent_id": r[2],
            "cron_expr": r[3], "next_run_at": r[4], "label": r[5],
            "created_at": r[6], "last_run_at": r[7],
            "run_count": r[8], "error_count": r[9],
            "max_budget_tokens": r[10],
            "used_tokens": r[11] if r[11] is not None else 0,
        }
        for r in rows
    ]


async def cancel_scheduled_task(
    conn, task_id: str = "", label: str = "",
) -> int:
    """按 id 或 label 取消 scheduled_tasks（status='paused'）。返回受影响行数"""
    if task_id:
        cursor = await conn.execute(
            "UPDATE scheduled_tasks SET status='paused' WHERE id=? AND status='pending'",
            (task_id,),
        )
    elif label:
        cursor = await conn.execute(
            "UPDATE scheduled_tasks SET status='paused' WHERE label=? AND status='pending'",
            (label,),
        )
    else:
        return 0
    await conn.commit()
    return cursor.rowcount


async def list_scheduled_tasks_all(
    conn, status: str = "", limit: int = 100,
) -> list[dict]:
    """列出定时任务（管理面板用）——默认全部状态，可按 status 筛选。

    与 list_scheduled_tasks 的区别：后者只给 agent 看 pending 的；
    管理面板要能看到 paused / completed / failed，才能回答"上次跑成功没有"。
    """
    limit = max(1, min(int(limit or 100), 500))
    columns = (
        "SELECT id, task_input, agent_id, session_id, cron_expr, next_run_at, "
        "status, label, created_at, last_run_at, run_count, error_count, "
        "max_budget_tokens, used_tokens FROM scheduled_tasks"
    )
    if status:
        cursor = await conn.execute(
            f"{columns} WHERE status=? ORDER BY next_run_at ASC LIMIT ?",
            (status, limit),
        )
    else:
        # pending 排最前（接下来会跑的），其余按最近活动时间
        cursor = await conn.execute(
            f"{columns} ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'running' THEN 1 "
            f"ELSE 2 END, next_run_at ASC LIMIT ?",
            (limit,),
        )
    rows = await cursor.fetchall()
    return [
        {
            "id": r[0], "task_input": r[1], "agent_id": r[2], "session_id": r[3],
            "cron_expr": r[4], "next_run_at": r[5], "status": r[6], "label": r[7],
            "created_at": r[8], "last_run_at": r[9], "run_count": r[10],
            "error_count": r[11], "max_budget_tokens": r[12],
            "used_tokens": r[13] if r[13] is not None else 0,
        }
        for r in rows
    ]


async def claim_due_scheduled_tasks(conn, now: int, limit: int = 5) -> list[dict]:
    """AUDIT #9: 认领到期的 scheduled_tasks（next_run_at <= now 且 status='pending'）

    原子操作：UPDATE ... WHERE id IN (SELECT ...) RETURNING *
    返回认领到的任务列表（含 id/task_input/agent_id/session_id/cron_expr）。
    """
    cursor = await conn.execute(
        "UPDATE scheduled_tasks SET status='running', last_run_at=? "
        "WHERE id IN ("
        "  SELECT id FROM scheduled_tasks "
        "  WHERE status='pending' AND next_run_at <= ? "
        "  ORDER BY next_run_at ASC LIMIT ?"
        ") RETURNING id, task_input, agent_id, session_id, cron_expr, max_budget_tokens",
        (now, now, limit),
    )
    rows = await cursor.fetchall()
    await conn.commit()
    return [
        {
            "id": r[0], "task_input": r[1], "agent_id": r[2],
            "session_id": r[3], "cron_expr": r[4],
            "max_budget_tokens": r[5],
        }
        for r in rows
    ]


async def complete_scheduled_task(
    conn, task_id: str, next_run_at: int | None = None,
    expected_from: str | None = "running",
) -> bool | None:
    """AUDIT #9: 任务执行完成——重新计算 next_run_at 并恢复 pending（循环调度）

    对单次触发（next_run_at=None）：标记 completed。
    对循环 cron：更新 next_run_at，run_count+1，status 恢复 pending。
    AUDIT #10/#13: 循环 cron 重置 used_tokens=0（下次运行重新计预算）。

    A3 乐观锁:
    - expected_from='running'（默认）：仅当 status='running' 时更新，返回 True/False。
      防御 task 被并发 cron_delete 暂停后，complete 把 paused 误翻回 pending。
    - expected_from=None：向后兼容，无条件更新，返回 None。
    """
    if expected_from is not None:
        where_clause = "WHERE id=? AND status=?"
        params_tail = (task_id, expected_from)
    else:
        where_clause = "WHERE id=?"
        params_tail = (task_id,)

    if next_run_at is None:
        cursor = await conn.execute(
            "UPDATE scheduled_tasks SET status='completed', run_count=run_count+1 "
            + where_clause,
            params_tail,
        )
    else:
        cursor = await conn.execute(
            "UPDATE scheduled_tasks SET status='pending', next_run_at=?, "
            "run_count=run_count+1, used_tokens=0 " + where_clause,
            (next_run_at,) + params_tail,
        )
    await conn.commit()
    if expected_from is not None:
        return cursor.rowcount > 0
    return None


async def fail_scheduled_task(
    conn, task_id: str, error: str = "",
    expected_from: str | None = "running",
) -> bool | None:
    """AUDIT #9: 任务执行失败——error_count+1，status 恢复 pending（下次仍会触发）

    A3 乐观锁:
    - expected_from='running'（默认）：仅当 status='running' 时更新，返回 True/False。
      防御 task 被并发暂停后，fail 把 paused 误翻回 pending。
    - expected_from=None：向后兼容，无条件更新，返回 None。
    """
    if expected_from is not None:
        cursor = await conn.execute(
            "UPDATE scheduled_tasks SET status='pending', error_count=error_count+1 "
            "WHERE id=? AND status=?",
            (task_id, expected_from),
        )
    else:
        cursor = await conn.execute(
            "UPDATE scheduled_tasks SET status='pending', error_count=error_count+1 "
            "WHERE id=?",
            (task_id,),
        )
    await conn.commit()
    if expected_from is not None:
        return cursor.rowcount > 0
    return None
