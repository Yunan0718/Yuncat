"""tests/unit/test_scheduled_trigger_atomic.py —— A3: 触发器原子抢占

覆盖两个调度路径:
1. scheduled_tasks: claim 已原子（UPDATE...RETURNING），补 complete/fail 乐观锁
2. workflow_triggers: cron_tick 缺原子 claim（SELECT-then-fire TOCTOU），新增 claim_due_cron_triggers

竞态场景:
- scheduled task 被 cron_delete 暂停后，complete 不应把它从 paused 翻回 pending
- workflow trigger 执行 >30s 时，下个 tick 不应重复触发同一 trigger
"""
import os
import sys
import time
import json
import pytest
import aiosqlite

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


def _safe_unlink(path: str) -> None:
    if not os.path.exists(path):
        return
    for _ in range(5):
        try:
            os.unlink(path)
            return
        except PermissionError:
            time.sleep(0.05)


@pytest.fixture
async def db(tmp_path):
    """创建带 schema + migrations 的 aiosqlite 连接"""
    db_path = os.path.join(str(tmp_path), "test_a3.db")
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    from repositories import SCHEMA_SQL, run_migrations
    await conn.executescript(SCHEMA_SQL)
    await run_migrations(conn)
    yield conn
    await conn.close()
    _safe_unlink(db_path)


# ============================================================
# scheduled_tasks: complete/fail 乐观锁
# ============================================================

class TestScheduledCompleteOptimisticLock:
    """complete_scheduled_task 乐观锁——防御 task 被并发暂停后误翻回 pending"""

    async def test_complete_cyclic_with_matching_status(self, db):
        """running→pending（循环 cron）状态匹配时成功，返回 True"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, claim_due_scheduled_tasks,
            complete_scheduled_task,
        )
        now = int(time.time()) - 10  # 10s 前到期
        tid = await insert_scheduled_task(db, task_input="t", cron_expr="* * * * *", next_run_at=now)
        # claim → running
        claimed = await claim_due_scheduled_tasks(db, now=int(time.time()), limit=5)
        assert len(claimed) == 1

        result = await complete_scheduled_task(db, tid, next_run_at=now + 60)
        assert result is True
        # 验证恢复 pending + next_run_at 更新
        cursor = await db.execute("SELECT status, next_run_at FROM scheduled_tasks WHERE id=?", (tid,))
        row = await cursor.fetchone()
        assert row[0] == "pending"
        assert row[1] == now + 60

    async def test_complete_single_fire_with_matching_status(self, db):
        """running→completed（单次触发）状态匹配时成功"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, claim_due_scheduled_tasks,
            complete_scheduled_task,
        )
        now = int(time.time()) - 10
        tid = await insert_scheduled_task(db, task_input="t", cron_expr="* * * * *", next_run_at=now)
        await claim_due_scheduled_tasks(db, now=int(time.time()), limit=5)

        result = await complete_scheduled_task(db, tid, next_run_at=None)
        assert result is True
        cursor = await db.execute("SELECT status FROM scheduled_tasks WHERE id=?", (tid,))
        row = await cursor.fetchone()
        assert row[0] == "completed"

    async def test_complete_skipped_when_status_mismatch(self, db):
        """task 被并发 cron_delete 暂停后，complete 不应把 paused 翻回 pending"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, claim_due_scheduled_tasks,
            cancel_scheduled_task, complete_scheduled_task,
        )
        now = int(time.time()) - 10
        tid = await insert_scheduled_task(db, task_input="t", cron_expr="* * * * *", next_run_at=now)
        # claim → running
        await claim_due_scheduled_tasks(db, now=int(time.time()), limit=5)
        # 用户在 fire-and-forget 期间通过其他路径把它暂停（模拟并发干预）
        await db.execute("UPDATE scheduled_tasks SET status='paused' WHERE id=?", (tid,))
        await db.commit()

        result = await complete_scheduled_task(db, tid, next_run_at=now + 60)
        assert result is False
        # paused 状态应保持，不被翻回 pending
        cursor = await db.execute("SELECT status FROM scheduled_tasks WHERE id=?", (tid,))
        row = await cursor.fetchone()
        assert row[0] == "paused"

    async def test_complete_backward_compat_no_expected_from(self, db):
        """不传 expected_from 时保持向后兼容（返回 None，无条件更新）"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, complete_scheduled_task,
        )
        now = int(time.time()) - 10
        tid = await insert_scheduled_task(db, task_input="t", cron_expr="* * * * *", next_run_at=now)
        # 直接 complete（不经 claim，status 仍是 pending）
        result = await complete_scheduled_task(db, tid, next_run_at=now + 60, expected_from=None)
        assert result is None
        cursor = await db.execute("SELECT status FROM scheduled_tasks WHERE id=?", (tid,))
        row = await cursor.fetchone()
        assert row[0] == "pending"  # 被无条件更新


class TestScheduledFailOptimisticLock:
    """fail_scheduled_task 乐观锁——失败恢复不应覆盖并发取消"""

    async def test_fail_with_matching_status(self, db):
        """running→pending（失败恢复）状态匹配时成功"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, claim_due_scheduled_tasks,
            fail_scheduled_task,
        )
        now = int(time.time()) - 10
        tid = await insert_scheduled_task(db, task_input="t", cron_expr="* * * * *", next_run_at=now)
        await claim_due_scheduled_tasks(db, now=int(time.time()), limit=5)

        result = await fail_scheduled_task(db, tid, error="boom")
        assert result is True
        cursor = await db.execute("SELECT status, error_count FROM scheduled_tasks WHERE id=?", (tid,))
        row = await cursor.fetchone()
        assert row[0] == "pending"
        assert row[1] == 1

    async def test_fail_skipped_when_status_mismatch(self, db):
        """task 被暂停后，fail 不应把 paused 翻回 pending"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, claim_due_scheduled_tasks,
            fail_scheduled_task,
        )
        now = int(time.time()) - 10
        tid = await insert_scheduled_task(db, task_input="t", cron_expr="* * * * *", next_run_at=now)
        await claim_due_scheduled_tasks(db, now=int(time.time()), limit=5)
        # 并发暂停
        await db.execute("UPDATE scheduled_tasks SET status='paused' WHERE id=?", (tid,))
        await db.commit()

        result = await fail_scheduled_task(db, tid, error="boom")
        assert result is False
        cursor = await db.execute("SELECT status, error_count FROM scheduled_tasks WHERE id=?", (tid,))
        row = await cursor.fetchone()
        assert row[0] == "paused"
        assert row[1] == 0  # error_count 不递增


# ============================================================
# workflow_triggers: 原子 claim
# ============================================================

async def _insert_cron_trigger(db, workflow_id="wf-1", cron_expr="* * * * *",
                                next_run_at=None, enabled=True):
    """插入一个 cron 触发器，返回 trigger_id"""
    import uuid
    tid = str(uuid.uuid4())
    config = json.dumps({"cron_expr": cron_expr, "initial_input": ""})
    if next_run_at is None:
        next_run_at = int(time.time()) - 10  # 默认已到期
    await db.execute(
        "INSERT INTO workflow_triggers(id, workflow_id, trigger_type, config, enabled, next_run_at) "
        "VALUES(?,?,?,?,?,?)",
        (tid, workflow_id, "cron", config, 1 if enabled else 0, next_run_at),
    )
    await db.commit()
    return tid


class TestWorkflowTriggerAtomicClaim:
    """workflow_triggers 原子 claim——修复 cron_tick TOCTOU 双触发"""

    async def test_claim_returns_due_triggers(self, db):
        """到期的 cron trigger 被认领并返回"""
        from services.workflow_trigger_service import WorkflowTriggerService
        tid = await _insert_cron_trigger(db)
        svc = WorkflowTriggerService(db)

        claimed = await svc.claim_due_cron_triggers(now=int(time.time()))
        assert len(claimed) == 1
        assert claimed[0]["id"] == tid

    async def test_claim_sets_next_run_at_null(self, db):
        """认领后 next_run_at 置 NULL，防止下个 tick 重复触发"""
        from services.workflow_trigger_service import WorkflowTriggerService
        tid = await _insert_cron_trigger(db)
        svc = WorkflowTriggerService(db)

        await svc.claim_due_cron_triggers(now=int(time.time()))
        cursor = await db.execute("SELECT next_run_at FROM workflow_triggers WHERE id=?", (tid,))
        row = await cursor.fetchone()
        assert row[0] is None

    async def test_second_claim_returns_empty(self, db):
        """已认领的 trigger 不被二次认领（即使 now 不变）"""
        from services.workflow_trigger_service import WorkflowTriggerService
        await _insert_cron_trigger(db)
        svc = WorkflowTriggerService(db)
        now = int(time.time())

        first = await svc.claim_due_cron_triggers(now=now)
        second = await svc.claim_due_cron_triggers(now=now)
        assert len(first) == 1
        assert len(second) == 0

    async def test_disabled_trigger_not_claimed(self, db):
        """enabled=0 的 trigger 不被认领"""
        from services.workflow_trigger_service import WorkflowTriggerService
        await _insert_cron_trigger(db, enabled=False)
        svc = WorkflowTriggerService(db)

        claimed = await svc.claim_due_cron_triggers(now=int(time.time()))
        assert len(claimed) == 0

    async def test_non_cron_trigger_not_claimed(self, db):
        """event/manual 类型触发器不被 cron claim 认领"""
        import uuid
        from services.workflow_trigger_service import WorkflowTriggerService
        tid = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO workflow_triggers(id, workflow_id, trigger_type, config, enabled, next_run_at) "
            "VALUES(?,?,?,?,?,?)",
            (tid, "wf-1", "event", "{}", 1, int(time.time()) - 10),
        )
        await db.commit()
        svc = WorkflowTriggerService(db)

        claimed = await svc.claim_due_cron_triggers(now=int(time.time()))
        assert len(claimed) == 0

    async def test_update_after_fire_restores_next_run_at(self, db):
        """fire 后 _update_after_cron_fire 恢复 next_run_at，下次到期可再被认领"""
        from services.workflow_trigger_service import WorkflowTriggerService
        tid = await _insert_cron_trigger(db, cron_expr="* * * * *")
        svc = WorkflowTriggerService(db)

        claimed = await svc.claim_due_cron_triggers(now=int(time.time()))
        assert len(claimed) == 1
        # fire 后恢复
        await svc._update_after_cron_fire(tid, cron_expr="* * * * *")
        cursor = await db.execute("SELECT next_run_at FROM workflow_triggers WHERE id=?", (tid,))
        row = await cursor.fetchone()
        assert row[0] is not None, "next_run_at 应被恢复为非 NULL"

    async def test_concurrent_claim_only_one_wins(self, db):
        """并发 claim：第一个拿到，第二个拿空（TOCTOU 修复核心）"""
        from services.workflow_trigger_service import WorkflowTriggerService
        await _insert_cron_trigger(db)
        svc = WorkflowTriggerService(db)
        now = int(time.time())

        # 模拟两个 tick 并发（SQLite 单连接串行化，所以顺序执行即可验证）
        first = await svc.claim_due_cron_triggers(now=now)
        second = await svc.claim_due_cron_triggers(now=now)
        assert len(first) == 1
        assert len(second) == 0, "第二个 tick 不应重复认领同一 trigger（TOCTOU 修复）"
