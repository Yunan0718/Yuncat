"""repositories/loop_repo.py —— Loop trigger queue + 任务 CRUD 辅助"""
import json as _json
import uuid as _uuid

from config import (
    LOOP_CANDIDATE_ROLES_LIMIT,
    LOOP_SESSION_TITLE_MAX_CHARS,
    LOOP_SESSION_PREVIEW_CHARS,
    SESSION_TITLE_MAX_CHARS,
)
from session_titles import resolve_title


async def insert_loop_trigger(conn, loop_id: str | None, trigger_type: str,
                               payload: dict, priority: int = 0) -> str:
    """写入 loop_trigger_queue，返回 event_id"""
    eid = str(_uuid.uuid4())[:12]
    await conn.execute(
        "INSERT INTO loop_trigger_queue(loop_id, trigger_type, payload, priority) "
        "VALUES(?,?,?,?)",
        (loop_id, trigger_type, _json.dumps(payload, ensure_ascii=False), priority),
    )
    await conn.commit()
    return eid


async def create_deliberation_task(conn, session_id: str, prompt: str,
                                     agent_id: str) -> str:
    """为审议创建任务行，返回 task_id"""
    import time
    task_id = str(_uuid.uuid4())
    await conn.execute(
        "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (task_id, prompt, "running", agent_id, session_id, int(time.time())),
    )
    await conn.commit()
    return task_id


async def complete_task(conn, task_id: str, output: str = "") -> None:
    """标记任务完成"""
    import time
    await conn.execute(
        "UPDATE tasks SET output=?, status='completed', completed_at=? WHERE id=?",
        (output, int(time.time()), task_id),
    )
    await conn.commit()


async def fail_task(conn, task_id: str, error: str) -> None:
    """标记任务失败"""
    import time
    await conn.execute(
        "UPDATE tasks SET status='failed', error_message=?, completed_at=? WHERE id=?",
        (error, int(time.time()), task_id),
    )
    await conn.commit()


async def upsert_workflow(conn, workflow_id: str, name: str, graph: str) -> None:
    """就地更新工作流"""
    import time
    await conn.execute(
        "UPDATE workflows SET name=?, graph=?, updated_at=? WHERE id=?",
        (name, graph, int(time.time()), workflow_id),
    )
    await conn.commit()


async def delete_session_messages(conn, session_id: str) -> int:
    """删除会话所有消息，返回删除行数"""
    cursor = await conn.execute(
        "DELETE FROM conversations WHERE session_id=?", (session_id,),
    )
    await conn.commit()
    return cursor.rowcount


async def list_sessions_summary(conn, limit: int = 20, offset: int = 0) -> list[dict]:
    """列出最近的 session 摘要（含消息数、标题、Agent）"""
    offset = max(0, offset)
    cursor = await conn.execute(
        "SELECT c.session_id, COUNT(*) AS msg_count, MAX(c.created_at) AS last_at, "
        "GROUP_CONCAT(DISTINCT c.agent_id) AS agents, "
        "COALESCE(u.content, '') AS first_user_msg, "
        "COALESCE(t.title, '') AS stored_title "
        "FROM conversations c "
        "LEFT JOIN ("
        "  SELECT session_id, MIN(created_at) AS min_ts "
        "  FROM conversations WHERE role='user' GROUP BY session_id"
        ") u_ts ON c.session_id = u_ts.session_id "
        "LEFT JOIN conversations u "
        "  ON u_ts.session_id = u.session_id AND u_ts.min_ts = u.created_at AND u.role='user' "
        "LEFT JOIN session_titles t ON t.session_id = c.session_id "
        "GROUP BY c.session_id "
        "ORDER BY last_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = await cursor.fetchall()
    result = []
    for r in rows:
        sid = r[0]
        first_msg = r[4] or ""
        # 标题生效优先级：用户命名 / 模型润色（session_titles 表） > 首条消息派生
        title = resolve_title(r[5] or "", first_msg, SESSION_TITLE_MAX_CHARS,
                              LOOP_SESSION_TITLE_MAX_CHARS) or "新对话"
        preview = first_msg[:LOOP_SESSION_PREVIEW_CHARS].replace("\n", " ") if first_msg else ""
        result.append({
            "session_id": sid, "msg_count": r[1], "last_at": r[2],
            "agents": (r[3] or "").split(","),
            "title": title, "preview": preview,
        })
    return result


async def get_session_task_stats(conn, session_ids: list[str]) -> dict[str, dict]:
    """按会话统计任务数与状态分布——会话列表/统计用。"""
    if not session_ids:
        return {}
    placeholders = ",".join("?" for _ in session_ids)
    cursor = await conn.execute(
        f"SELECT session_id, COUNT(*), "
        f"SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) "
        f"FROM tasks WHERE session_id IN ({placeholders}) GROUP BY session_id",
        list(session_ids),
    )
    rows = await cursor.fetchall()
    return {
        str(r[0]): {"tasks": r[1] or 0, "completed": r[2] or 0, "failed": r[3] or 0}
        for r in rows if r[0]
    }


async def list_pending_temp_roles(conn) -> list[dict]:
    """列出待转化的临时角色"""
    cursor = await conn.execute(
        "SELECT id, title, score, tags, created_at FROM knowledge_nodes "
        "WHERE source='deliberation_tmp_role' AND score >= 0.7 "
        "AND tags NOT LIKE '%rejected%' ORDER BY score DESC LIMIT ?",
        (LOOP_CANDIDATE_ROLES_LIMIT,),
    )
    rows = await cursor.fetchall()
    return [
        {"node_id": r[0], "role_name": r[1].replace("临时角色: ", ""), "score": r[2],
         "tags": r[3], "created_at": r[4]}
        for r in rows
    ]


async def promote_temp_role(conn, role_name: str, prompt_template: str,
                              node_id: str = "") -> str:
    """将临时角色转化为正式角色。返回 role_id"""
    import time
    rid = str(_uuid.uuid4())[:12]
    await conn.execute(
        "INSERT INTO roles(id, name, category, prompt_template, description, "
        "enabled, is_builtin, source, created_at, updated_at) "
        "VALUES(?,?,?,?,?,1,0,'auto_generated',?,?)",
        (rid, role_name, "auto_generated", prompt_template,
         f"AI 自动生成的角色: {role_name}",
         int(time.time()), int(time.time())),
    )
    if node_id:
        await _append_knowledge_tag(conn, node_id, "promoted")
    await conn.commit()
    return rid


async def reject_temp_role(conn, node_id: str) -> None:
    """拒绝临时角色转化"""
    await _append_knowledge_tag(conn, node_id, "rejected")
    await conn.commit()


async def _append_knowledge_tag(conn, node_id: str, tag: str) -> None:
    """Phase 48 T3-11: 向 knowledge_nodes.tags 追加标签（JSON 数组操作）。

    原 `tags || ',promoted'` 破坏 JSON 数组格式（`["x"]` → `["x"],promoted` 无效 JSON）。
    现读取 → json.loads → append → json.dumps → 写回，保持格式一致。
    """
    cursor = await conn.execute(
        "SELECT tags FROM knowledge_nodes WHERE id=?", (node_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return
    try:
        tags = _json.loads(row[0] or '[]')
        if not isinstance(tags, list):
            tags = []
    except (ValueError, TypeError):
        tags = []
    if tag not in tags:
        tags.append(tag)
    await conn.execute(
        "UPDATE knowledge_nodes SET tags=? WHERE id=?",
        (_json.dumps(tags, ensure_ascii=False), node_id),
    )


async def insert_agent_skill(conn, agent_id: str, skill_name: str) -> None:
    """将 Skill 关联到 Agent（幂等——INSERT OR IGNORE）"""
    await conn.execute(
        "INSERT OR IGNORE INTO agent_skills(agent_id, skill_name) VALUES(?,?)",
        (agent_id, skill_name),
    )
    await conn.commit()


async def remove_agent_skill(conn, agent_id: str, skill_name: str) -> None:
    """从 Agent 移除 Skill 关联"""
    await conn.execute(
        "DELETE FROM agent_skills WHERE agent_id=? AND skill_name=?",
        (agent_id, skill_name),
    )
    await conn.commit()


async def get_agent_skills(conn, agent_id: str) -> list[str]:
    """获取 Agent 关联的所有 Skill 名称"""
    cursor = await conn.execute(
        "SELECT skill_name FROM agent_skills WHERE agent_id=?", (agent_id,),
    )
    rows = await cursor.fetchall()
    return [r[0] for r in rows]
