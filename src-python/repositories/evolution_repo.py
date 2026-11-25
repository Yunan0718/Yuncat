"""repositories/evolution_repo.py —— Self-Evolution 模式持久化

Batch 2.6: 为 SelfEvolutionEngine._patterns 提供数据库持久化，
解决进程重启后计数归零、workflow 永远不会自动生成的问题。
"""
import json as _json
import logging
import time as _time
from typing import Any

logger = logging.getLogger("yuncat.repositories")


async def load_patterns(db) -> dict[str, dict]:
    """加载全部 evolution_patterns 到内存结构。

    Returns:
        {signature: {"count": int, "examples": list, "last_seen": float,
                     "_workflow_generated": bool, "_skill_generated": bool,
                     "_skill_exported": bool}}
    """
    cursor = await db.execute(
        "SELECT signature, count, examples, last_seen, workflow_generated, "
        "skill_generated, skill_exported FROM evolution_patterns"
    )
    rows = await cursor.fetchall()
    patterns: dict[str, dict] = {}
    for row in rows:
        sig = row[0]
        try:
            examples = _json.loads(row[2]) if row[2] else []
        except Exception:
            examples = []
        patterns[sig] = {
            "count": row[1] or 0,
            "examples": examples,
            "last_seen": row[3] or 0.0,
            "_workflow_generated": bool(row[4]),
            "_skill_generated": bool(row[5]),
            "_skill_exported": bool(row[6]),
        }
    return patterns


async def upsert_pattern(
    db,
    signature: str,
    count: int,
    examples: list[str],
    last_seen: float,
    workflow_generated: int = 0,
    skill_generated: int = 0,
    skill_exported: int = 0,
) -> None:
    """UPSERT 单个 pattern 行。"""
    examples_json = _json.dumps(examples[-10:] if examples else [], ensure_ascii=False)
    now = int(_time.time())
    await db.execute(
        """
        INSERT INTO evolution_patterns(
            signature, count, examples, last_seen,
            workflow_generated, skill_generated, skill_exported,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(signature) DO UPDATE SET
            count = excluded.count,
            examples = excluded.examples,
            last_seen = excluded.last_seen,
            workflow_generated = excluded.workflow_generated,
            skill_generated = excluded.skill_generated,
            skill_exported = excluded.skill_exported,
            updated_at = excluded.updated_at
        """,
        (
            signature,
            count,
            examples_json,
            last_seen,
            int(workflow_generated),
            int(skill_generated),
            int(skill_exported),
            now,
            now,
        ),
    )
    await db.commit()


async def delete_pattern(db, signature: str) -> None:
    """删除单个 pattern 行。"""
    await db.execute("DELETE FROM evolution_patterns WHERE signature = ?", (signature,))
    await db.commit()


async def delete_stale_patterns(db, cutoff_timestamp: float) -> int:
    """删除 last_seen < cutoff_timestamp 的 stale patterns。

    Returns:
        删除行数
    """
    cursor = await db.execute(
        "DELETE FROM evolution_patterns WHERE last_seen < ?",
        (cutoff_timestamp,),
    )
    await db.commit()
    return cursor.rowcount


# ============================================================
# Batch 5.3: Self-Evolution 质量门控——pending install 记录
# ============================================================

async def create_pending_install(
    db,
    pending_id: str,
    pattern_signature: str,
    task_type: str,
    workflow_id: str,
    skill_dir: str,
    examples: list[str],
    sandbox_result: dict,
    ab_result: dict,
    evaluation: dict,
    status: str = "pending",
) -> str:
    """创建一条待审核安装记录。"""
    now = int(_time.time())
    await db.execute(
        """
        INSERT INTO evolution_pending_installs(
            id, pattern_signature, task_type, workflow_id, skill_dir,
            examples, sandbox_result, ab_result, evaluation, status,
            notification_sent, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pending_id,
            pattern_signature,
            task_type,
            workflow_id,
            skill_dir,
            _json.dumps(examples[-10:] if examples else [], ensure_ascii=False),
            _json.dumps(sandbox_result or {}, ensure_ascii=False),
            _json.dumps(ab_result or {}, ensure_ascii=False),
            _json.dumps(evaluation or {}, ensure_ascii=False),
            status,
            0,
            now,
            now,
        ),
    )
    await db.commit()
    return pending_id


async def list_pending_installs(
    db,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """列出 pending install 记录。"""
    if status:
        cursor = await db.execute(
            "SELECT * FROM evolution_pending_installs WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM evolution_pending_installs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    return [_row_to_pending_install(dict(zip(cols, row))) for row in rows]


async def get_pending_install(db, pending_id: str) -> dict | None:
    """获取单条 pending install 记录。"""
    cursor = await db.execute(
        "SELECT * FROM evolution_pending_installs WHERE id=?", (pending_id,)
    )
    row = await cursor.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cursor.description]
    return _row_to_pending_install(dict(zip(cols, row)))


async def update_pending_status(db, pending_id: str, status: str) -> bool:
    """更新 pending install 状态。"""
    now = int(_time.time())
    cursor = await db.execute(
        "UPDATE evolution_pending_installs SET status=?, updated_at=? WHERE id=?",
        (status, now, pending_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def mark_notification_sent(db, pending_id: str) -> bool:
    """标记通知已发送。"""
    now = int(_time.time())
    cursor = await db.execute(
        "UPDATE evolution_pending_installs SET notification_sent=1, updated_at=? WHERE id=?",
        (now, pending_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete_pending_install(db, pending_id: str) -> bool:
    """删除单条 pending install 记录。"""
    cursor = await db.execute(
        "DELETE FROM evolution_pending_installs WHERE id=?", (pending_id,)
    )
    await db.commit()
    return cursor.rowcount > 0


def _row_to_pending_install(row: dict) -> dict:
    """把 DB 行转换为 dict，并解析 JSON 字段。"""
    def _load(key: str) -> Any:
        try:
            return _json.loads(row[key]) if row.get(key) else {}
        except Exception:
            return {}

    return {
        "id": row["id"],
        "pattern_signature": row["pattern_signature"],
        "task_type": row["task_type"],
        "workflow_id": row["workflow_id"],
        "skill_dir": row["skill_dir"],
        "examples": _load("examples"),
        "sandbox_result": _load("sandbox_result"),
        "ab_result": _load("ab_result"),
        "evaluation": _load("evaluation"),
        "status": row["status"],
        "notification_sent": bool(row.get("notification_sent", 0)),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
