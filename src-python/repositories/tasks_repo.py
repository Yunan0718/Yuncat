"""repositories/tasks_repo.py —— 任务级联删除 + Plan 子任务 CRUD"""
import json as _json
import time
import uuid as _uuid


# Phase 39: 应用层级联删除 (SQLite 不支持 ALTER TABLE ADD CONSTRAINT)
async def delete_task_cascade(conn, task_id: str):
    await conn.execute("DELETE FROM task_runs WHERE task_id=?", (task_id,))
    await conn.execute("DELETE FROM task_logs WHERE task_id=?", (task_id,))
    await conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    await conn.commit()


async def delete_workflow_cascade(conn, workflow_id: str):
    await conn.execute("DELETE FROM workflow_runs WHERE workflow_id=?", (workflow_id,))
    await conn.execute("DELETE FROM workflows WHERE id=?", (workflow_id,))
    await conn.commit()


# ---- Phase 14: Plan 子任务 CRUD ----

async def create_plan_steps(
    conn, parent_task_id: str, steps: list[dict], agent_id: str,
) -> list[str]:
    task_ids = []
    for s in steps:
        tid = str(_uuid.uuid4())
        input_text = f"[Plan 步骤 {s['step']}/{len(steps)}] {s['task']}"
        await conn.execute(
            "INSERT INTO tasks(id, parent_task_id, agent_id, input, status, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (tid, parent_task_id, agent_id, input_text, "pending", int(time.time())),
        )
        task_ids.append(tid)
    await conn.commit()
    return task_ids


async def get_plan_steps(conn, parent_task_id: str) -> list[dict]:
    cursor = await conn.execute(
        "SELECT id, input, status, error_message, output, agent_id "
        "FROM tasks WHERE parent_task_id=? ORDER BY created_at ASC",
        (parent_task_id,),
    )
    rows = await cursor.fetchall()
    return [
        {"id": r[0], "input": r[1], "status": r[2], "error_message": r[3],
         "output": r[4], "agent_id": r[5]}
        for r in rows
    ]


# ---- Phase 108: 真实子任务树 ----
# 与 get_plan_steps 类似，但面向「任意 parent_task_id 的子任务」
# （spawn_subagent / Plan 子步骤 / retry 版本链 统一走 parent_task_id 列）。
# 返回前端渲染子任务树所需的精简字段。
async def get_child_tasks(conn, parent_task_id: str) -> list[dict]:
    cursor = await conn.execute(
        "SELECT id, input, status, agent_id, created_at, "
        "SUBSTR(COALESCE(output, ''), 1, 160) AS output_preview "
        "FROM tasks WHERE parent_task_id=? ORDER BY created_at ASC",
        (parent_task_id,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r[0],
            "input": (r[1] or "")[:200],
            "status": r[2],
            "agent_id": r[3],
            "created_at": r[4],
            "output_preview": r[5] or "",
        }
        for r in rows
    ]


async def update_task_state(conn, task_id: str, state: dict) -> None:
    """更新 tasks.task_state JSON 字段（不 commit）。

    Phase 61: 同时更新 heartbeat_at + updated_at，作为完整 snapshot 的一部分。
    """
    now = time.time()
    await conn.execute(
        "UPDATE tasks SET task_state=?, heartbeat_at=?, updated_at=? WHERE id=?",
        (_json.dumps(state, ensure_ascii=False), now, int(now), task_id),
    )


async def update_task_heartbeat(conn, task_id: str) -> None:
    """Phase 61: 只更新 heartbeat_at + updated_at，不覆盖 task_state。

    用于长耗时工具（run_command / spawn_subagent）执行期间周期性心跳，
    避免与完整 _write_task_snapshot 互相覆盖。
    """
    now = time.time()
    await conn.execute(
        "UPDATE tasks SET heartbeat_at=?, updated_at=? WHERE id=?",
        (now, int(now), task_id),
    )
