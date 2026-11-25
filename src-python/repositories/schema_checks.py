"""repositories/schema_checks.py —— Schema 漂移检测

Phase 48 T3-10: 从 schema.py 拆分（_check_schema_drift 函数 ~70 行）。
保留在独立模块以便 schema.py 聚焦于 DDL/迁移，同时维护架构铁律 #5
（SCHEMA_SQL + MIGRATIONS 同文件）。

依赖 SCHEMA_SQL（延迟导入避免循环依赖）。
"""
import logging

logger = logging.getLogger("yuncat.repositories")


async def _check_schema_drift(conn) -> None:
    """检测 knowledge_nodes 表的列是否与 SCHEMA_SQL 一致。

    如果 SCHEMA_SQL 改了列但 migration v4 没同步，这里会输出 warning。
    """
    # Phase 48 T3-10: 延迟导入 SCHEMA_SQL 避免与 schema.py 的循环依赖
    from repositories.schema import SCHEMA_SQL
    import re
    expected_cols = set()
    m = re.search(
        r'CREATE TABLE IF NOT EXISTS knowledge_nodes \((.*?)\);',
        SCHEMA_SQL, re.DOTALL,
    )
    if m:
        for line in m.group(1).strip().split("\n"):
            line = line.strip().rstrip(",")
            if not line or line.startswith("FOREIGN") or line.startswith("--"):
                continue
            # 提取列名（第一个单词）
            col_name = line.split()[0].strip()
            if col_name and not col_name.upper() == col_name:
                expected_cols.add(col_name)

    cursor = await conn.execute("PRAGMA table_info(knowledge_nodes)")
    actual_cols = {row[1] for row in await cursor.fetchall()}

    if expected_cols and expected_cols != actual_cols:
        missing = expected_cols - actual_cols
        extra = actual_cols - expected_cols
        parts = []
        if missing:
            parts.append(f"missing columns: {missing}")
        if extra:
            parts.append(f"extra columns: {extra}")
        logger.warning(
            "Schema drift detected in knowledge_nodes: %s. "
            "SCHEMA_SQL and migration v4 may be out of sync.",
            "; ".join(parts),
        )

    # Phase 39: 全表 schema 漂移检测
    _EXPECTED_TABLES = {
        "tasks": ["id", "input", "output", "status", "agent_id", "session_id",
                   "error_message", "created_at", "completed_at", "parent_task_id",
                   "workflow_id", "pinned"],
        "conversations": ["id", "session_id", "role", "content", "agent_id",
                          "token_count", "created_at"],
        "snapshots": ["id", "task_id", "turn_seq", "phase", "files_affected", "created_at"],
        "workflows": ["id", "name", "description", "graph", "is_template",
                      "created_at", "updated_at"],
        "knowledge_nodes": ["id", "title", "content", "source", "source_ref",
                           "score", "tags", "parent_id", "created_at", "updated_at",
                           "last_accessed", "indexed_at"],
        # Phase 45: Loop 状态持久化
        "loop_registry": ["loop_id", "loop_type", "status", "config", "state",
                         "last_run_at", "last_ok_at", "restart_count", "error_count",
                         "last_error", "created_at", "updated_at"],
        "loop_heartbeats": ["loop_id", "last_beat", "iteration_count"],
        "loop_trigger_queue": ["id", "loop_id", "trigger_type", "payload", "priority",
                               "status", "claimed_by", "claimed_at", "completed_at",
                               "created_at"],
        # Phase 48 T0-7: Agent ↔ Skill 关联表
        "agent_skills": ["agent_id", "skill_name"],
    }
    for table, cols in _EXPECTED_TABLES.items():
        cursor = await conn.execute(f"PRAGMA table_info({table})")
        actual = [row[1] for row in await cursor.fetchall()]
        missing = [c for c in cols if c not in actual]
        extra = [c for c in actual if c not in cols]
        if missing:
            logger.warning("Schema drift: %s missing columns %s", table, missing)
        if extra:
            logger.warning("Schema drift: %s has extra columns %s (may be from newer migration)", table, extra)
