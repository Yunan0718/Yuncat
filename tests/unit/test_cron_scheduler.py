"""tests/unit/test_cron_scheduler.py —— AUDIT #9 cron 调度器集成测试

覆盖：
1. scheduled_tasks_repo CRUD（insert/list/cancel/claim/complete/fail）
2. cron_schedule/cron_list/cron_delete 工具端到端（mock DB）
3. 关键性质：cron 表达式真正被评估（不再插入即触发）
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ═══════════════════════════════════════════════════════════════
# DB fixture
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
async def db(tmp_path):
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_cron.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


# ═══════════════════════════════════════════════════════════════
# scheduled_tasks_repo CRUD 测试
# ═══════════════════════════════════════════════════════════════

class TestScheduledTasksRepo:
    async def test_insert_and_list(self, db):
        """插入 + 列出 scheduled_tasks"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, list_scheduled_tasks,
        )
        tid = await insert_scheduled_task(
            db, task_input="每日报告", cron_expr="0 9 * * *",
            next_run_at=int(time.time()) + 3600, label="daily_report",
        )
        assert len(tid) == 12
        tasks = await list_scheduled_tasks(db)
        assert len(tasks) == 1
        assert tasks[0]["id"] == tid
        assert tasks[0]["cron_expr"] == "0 9 * * *"
        assert tasks[0]["task_input"] == "每日报告"
        assert tasks[0]["label"] == "daily_report"

    async def test_cancel_by_id(self, db):
        """按 id 取消 → status='paused'，list 不再返回"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, list_scheduled_tasks, cancel_scheduled_task,
        )
        tid = await insert_scheduled_task(
            db, task_input="test", cron_expr="* * * * *",
            next_run_at=int(time.time()) + 60,
        )
        affected = await cancel_scheduled_task(db, task_id=tid)
        assert affected == 1
        tasks = await list_scheduled_tasks(db)
        assert len(tasks) == 0  # paused 不在 pending 列表中

    async def test_cancel_by_label(self, db):
        """按 label 批量取消"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, list_scheduled_tasks, cancel_scheduled_task,
        )
        await insert_scheduled_task(
            db, task_input="t1", cron_expr="* * * * *",
            next_run_at=int(time.time()) + 60, label="batch",
        )
        await insert_scheduled_task(
            db, task_input="t2", cron_expr="* * * * *",
            next_run_at=int(time.time()) + 60, label="batch",
        )
        affected = await cancel_scheduled_task(db, label="batch")
        assert affected == 2
        tasks = await list_scheduled_tasks(db)
        assert len(tasks) == 0

    async def test_cancel_without_id_or_label_returns_zero(self, db):
        """不提供 id 和 label → 返回 0"""
        from repositories.scheduled_tasks_repo import cancel_scheduled_task
        affected = await cancel_scheduled_task(db)
        assert affected == 0


# ═══════════════════════════════════════════════════════════════
# claim_due_scheduled_tasks 测试——AUDIT #9 核心
# ═══════════════════════════════════════════════════════════════

class TestClaimDueTasks:
    async def test_claim_only_due_tasks(self, db):
        """只认领 next_run_at <= now 的任务"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, claim_due_scheduled_tasks,
        )
        now = int(time.time())
        # 到期的任务
        await insert_scheduled_task(
            db, task_input="due", cron_expr="* * * * *",
            next_run_at=now - 10,  # 10 秒前到期
        )
        # 未到期的任务
        await insert_scheduled_task(
            db, task_input="future", cron_expr="* * * * *",
            next_run_at=now + 3600,  # 1 小时后到期
        )
        claimed = await claim_due_scheduled_tasks(db, now, limit=5)
        assert len(claimed) == 1
        assert claimed[0]["task_input"] == "due"

    async def test_claim_sets_status_running(self, db):
        """认领后 status='running'，不会被二次认领"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, claim_due_scheduled_tasks,
        )
        now = int(time.time())
        await insert_scheduled_task(
            db, task_input="due", cron_expr="* * * * *",
            next_run_at=now - 10,
        )
        # 第一次认领
        claimed = await claim_due_scheduled_tasks(db, now, limit=5)
        assert len(claimed) == 1
        # 第二次认领——应该为空（status 已是 running）
        claimed2 = await claim_due_scheduled_tasks(db, now, limit=5)
        assert len(claimed2) == 0

    async def test_claim_respects_limit(self, db):
        """limit 限制单次认领数量"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, claim_due_scheduled_tasks,
        )
        now = int(time.time())
        for i in range(5):
            await insert_scheduled_task(
                db, task_input=f"task{i}", cron_expr="* * * * *",
                next_run_at=now - 10 - i,  # 按 FIFO 排序
            )
        claimed = await claim_due_scheduled_tasks(db, now, limit=2)
        assert len(claimed) == 2


# ═══════════════════════════════════════════════════════════════
# complete/fail 测试——循环调度恢复
# ═══════════════════════════════════════════════════════════════

class TestCompleteFailTask:
    async def test_complete_with_next_run_restores_pending(self, db):
        """complete_scheduled_task(next_run_at=future) → status='pending', run_count+1"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, claim_due_scheduled_tasks,
            complete_scheduled_task, list_scheduled_tasks,
        )
        now = int(time.time())
        tid = await insert_scheduled_task(
            db, task_input="loop", cron_expr="* * * * *",
            next_run_at=now - 10,
        )
        claimed = await claim_due_scheduled_tasks(db, now)
        assert len(claimed) == 1
        # 完成并设置下次运行时间
        future = now + 60
        await complete_scheduled_task(db, tid, next_run_at=future)
        # 应该重新出现在 pending 列表
        tasks = await list_scheduled_tasks(db)
        assert len(tasks) == 1
        assert tasks[0]["run_count"] == 1
        assert tasks[0]["next_run_at"] == future

    async def test_complete_without_next_run_marks_completed(self, db):
        """complete_scheduled_task(next_run_at=None) → status='completed'"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, claim_due_scheduled_tasks,
            complete_scheduled_task, list_scheduled_tasks,
        )
        now = int(time.time())
        tid = await insert_scheduled_task(
            db, task_input="once", cron_expr="* * * * *",
            next_run_at=now - 10,
        )
        await claim_due_scheduled_tasks(db, now)
        await complete_scheduled_task(db, tid, next_run_at=None)
        # 不应出现在 pending 列表
        tasks = await list_scheduled_tasks(db)
        assert len(tasks) == 0

    async def test_fail_restores_pending_with_error_count(self, db):
        """fail_scheduled_task → status='pending', error_count+1"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, claim_due_scheduled_tasks,
            fail_scheduled_task, list_scheduled_tasks,
        )
        now = int(time.time())
        tid = await insert_scheduled_task(
            db, task_input="fail_test", cron_expr="* * * * *",
            next_run_at=now - 10,
        )
        await claim_due_scheduled_tasks(db, now)
        await fail_scheduled_task(db, tid, error="agent crashed")
        tasks = await list_scheduled_tasks(db)
        assert len(tasks) == 1
        assert tasks[0]["error_count"] == 1


# ═══════════════════════════════════════════════════════════════
# cron_schedule 工具端到端测试——验证 cron 表达式真正被评估
# ═══════════════════════════════════════════════════════════════

class TestCronScheduleTool:
    """AUDIT #9 核心断言：cron_schedule 不再插入即触发——next_run_at 在未来"""

    async def test_schedule_sets_future_next_run_at(self, db, monkeypatch):
        """cron_schedule 工具：next_run_at 必须在未来（不是插入即触发）"""
        import context as _context
        from tools.builtin.system_tool_advanced import cron_schedule
        monkeypatch.setattr(_context, "get_db", lambda: db)

        now = int(time.time())
        result = await cron_schedule(
            schedule="0 9 * * *", task="每日报告", label="daily",
        )
        assert result["success"] is True
        assert result["data"]["next_run_at"] > now
        # next_run_str 应该是可读的时间
        assert "next_run_str" in result["data"]

    async def test_schedule_invalid_cron_returns_error(self, db, monkeypatch):
        """无效 cron 表达式 → success=False"""
        import context as _context
        from tools.builtin.system_tool_advanced import cron_schedule
        monkeypatch.setattr(_context, "get_db", lambda: db)

        result = await cron_schedule(
            schedule="invalid cron expr", task="test",
        )
        assert result["success"] is False
        assert "cron" in result["error"].lower() or "无效" in result["error"]

    async def test_schedule_then_list(self, db, monkeypatch):
        """schedule → list 应返回刚创建的任务"""
        import context as _context
        from tools.builtin.system_tool_advanced import cron_schedule, cron_list
        monkeypatch.setattr(_context, "get_db", lambda: db)

        await cron_schedule(
            schedule="*/15 * * * *", task="每15分钟检查", label="check",
        )
        result = await cron_list()
        assert result["success"] is True
        assert result["data"]["count"] == 1
        task = result["data"]["tasks"][0]
        assert task["cron_expr"] == "*/15 * * * *"
        assert task["label"] == "check"

    async def test_schedule_then_delete(self, db, monkeypatch):
        """schedule → delete → list 为空"""
        import context as _context
        from tools.builtin.system_tool_advanced import cron_schedule, cron_list, cron_delete
        monkeypatch.setattr(_context, "get_db", lambda: db)

        r = await cron_schedule(
            schedule="0 0 * * 0", task="每周日任务", label="weekly",
        )
        cron_id = r["data"]["cron_id"]
        del_result = await cron_delete(cron_id=cron_id)
        assert del_result["success"] is True
        assert del_result["data"]["affected"] == 1
        list_result = await cron_list()
        assert list_result["data"]["count"] == 0

    async def test_not_immediately_triggered(self, db, monkeypatch):
        """AUDIT #9 关键：scheduled 任务不会被 _scheduler_tick 立即触发

        修复前：cron_schedule 插入 loop_trigger_queue，~30s 内 spec_events 即触发
        修复后：next_run_at 在未来，_scheduler_tick 不会认领未到期任务
        """
        import context as _context
        from tools.builtin.system_tool_advanced import cron_schedule
        from repositories.scheduled_tasks_repo import claim_due_scheduled_tasks
        monkeypatch.setattr(_context, "get_db", lambda: db)

        now = int(time.time())
        await cron_schedule(
            schedule="0 9 * * *", task="每日报告",
        )
        # 立即查询到期任务——应该为空（next_run_at 在未来）
        claimed = await claim_due_scheduled_tasks(db, now, limit=5)
        assert len(claimed) == 0, "AUDIT #9: cron 任务不应被立即触发"


# ═══════════════════════════════════════════════════════════════
# AUDIT #10: Heartbeat Automations——cron_schedule budget/session_id
# ═══════════════════════════════════════════════════════════════

class TestCronBudgetAndSession:
    """AUDIT #10/#13: cron_schedule 支持 budget 和 session_id 参数"""

    async def test_schedule_with_budget_stores_max_budget_tokens(self, db, monkeypatch):
        """cron_schedule(budget=10000) → scheduled_tasks.max_budget_tokens=10000"""
        import context as _context
        from tools.builtin.system_tool_advanced import cron_schedule
        monkeypatch.setattr(_context, "get_db", lambda: db)

        result = await cron_schedule(
            schedule="0 9 * * *", task="预算任务", budget=10000,
        )
        assert result["success"] is True
        assert result["data"]["budget"] == 10000
        # 验证 DB 中确实存了 max_budget_tokens
        cursor = await db.execute(
            "SELECT max_budget_tokens FROM scheduled_tasks WHERE id=?",
            (result["data"]["cron_id"],),
        )
        row = await cursor.fetchone()
        assert row[0] == 10000

    async def test_schedule_without_budget_stores_null(self, db, monkeypatch):
        """cron_schedule(budget=0) → max_budget_tokens=NULL（不限）"""
        import context as _context
        from tools.builtin.system_tool_advanced import cron_schedule
        monkeypatch.setattr(_context, "get_db", lambda: db)

        result = await cron_schedule(
            schedule="0 9 * * *", task="无预算任务",
        )
        assert result["success"] is True
        assert result["data"]["budget"] == 0
        cursor = await db.execute(
            "SELECT max_budget_tokens FROM scheduled_tasks WHERE id=?",
            (result["data"]["cron_id"],),
        )
        row = await cursor.fetchone()
        assert row[0] is None

    async def test_schedule_with_session_id(self, db, monkeypatch):
        """cron_schedule(session_id='sess123') → scheduled_tasks.session_id='sess123'"""
        import context as _context
        from tools.builtin.system_tool_advanced import cron_schedule
        monkeypatch.setattr(_context, "get_db", lambda: db)

        result = await cron_schedule(
            schedule="*/15 * * * *", task="会话任务", session_id="sess123",
        )
        assert result["success"] is True
        assert result["data"]["session_id"] == "sess123"
        cursor = await db.execute(
            "SELECT session_id FROM scheduled_tasks WHERE id=?",
            (result["data"]["cron_id"],),
        )
        row = await cursor.fetchone()
        assert row[0] == "sess123"

    async def test_list_returns_budget_fields(self, db, monkeypatch):
        """cron_list 返回 max_budget_tokens 和 used_tokens 字段"""
        import context as _context
        from tools.builtin.system_tool_advanced import cron_schedule, cron_list
        monkeypatch.setattr(_context, "get_db", lambda: db)

        await cron_schedule(schedule="0 9 * * *", task="t1", budget=5000)
        await cron_schedule(schedule="0 10 * * *", task="t2")  # 无预算
        result = await cron_list()
        assert result["success"] is True
        assert result["data"]["count"] == 2
        tasks = result["data"]["tasks"]
        # 找到有预算的任务
        budgeted = [t for t in tasks if t["max_budget_tokens"] is not None]
        assert len(budgeted) == 1
        assert budgeted[0]["max_budget_tokens"] == 5000
        assert budgeted[0]["used_tokens"] == 0
        # 无预算的任务
        unbounded = [t for t in tasks if t["max_budget_tokens"] is None]
        assert len(unbounded) == 1

    async def test_claim_returns_max_budget_tokens(self, db):
        """claim_due_scheduled_tasks 返回 max_budget_tokens 字段"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, claim_due_scheduled_tasks,
        )
        now = int(time.time())
        await insert_scheduled_task(
            db, task_input="budget task", cron_expr="* * * * *",
            next_run_at=now - 10, max_budget_tokens=8000,
        )
        claimed = await claim_due_scheduled_tasks(db, now)
        assert len(claimed) == 1
        assert claimed[0]["max_budget_tokens"] == 8000

    async def test_complete_resets_used_tokens(self, db):
        """complete_scheduled_task(循环 cron) 重置 used_tokens=0"""
        from repositories.scheduled_tasks_repo import (
            insert_scheduled_task, claim_due_scheduled_tasks,
            complete_scheduled_task, list_scheduled_tasks,
        )
        now = int(time.time())
        tid = await insert_scheduled_task(
            db, task_input="loop", cron_expr="* * * * *",
            next_run_at=now - 10, max_budget_tokens=10000,
        )
        # 模拟 used_tokens 累加
        await db.execute("UPDATE scheduled_tasks SET used_tokens=5000 WHERE id=?", (tid,))
        await db.commit()
        # 认领并完成
        await claim_due_scheduled_tasks(db, now)
        future = now + 60
        await complete_scheduled_task(db, tid, next_run_at=future)
        # 验证 used_tokens 已重置
        tasks = await list_scheduled_tasks(db)
        assert len(tasks) == 1
        assert tasks[0]["used_tokens"] == 0
        assert tasks[0]["max_budget_tokens"] == 10000  # 预算上限不重置


# ═══════════════════════════════════════════════════════════════
# 一次性排期（run_at）——对标 DSH schedule 的一次性提醒
# ═══════════════════════════════════════════════════════════════

class TestParseRunAt:
    """一次性排期解析：相对时间 / 时刻 / ISO。"""

    def test_relative_units(self):
        from services.cron_parser import parse_run_at

        now = 1_700_000_000
        assert parse_run_at("30m", now=now) == now + 1800
        assert parse_run_at("2h", now=now) == now + 7200
        assert parse_run_at("1d", now=now) == now + 86400
        assert parse_run_at("in 30 minutes", now=now) == now + 1800

    def test_clock_time_rolls_to_tomorrow_when_passed(self):
        from datetime import datetime
        from services.cron_parser import parse_run_at

        # 2023-11-14 22:13:20 UTC 附近
        now = int(datetime(2023, 11, 14, 22, 13, 20).timestamp())
        nxt = parse_run_at("09:30", now=now)
        assert nxt > now
        assert datetime.fromtimestamp(nxt).strftime("%H:%M") == "09:30"
        # 已经过了 09:30 → 明天
        assert datetime.fromtimestamp(nxt).date() > datetime.fromtimestamp(now).date()

    def test_iso8601(self):
        from datetime import datetime
        from services.cron_parser import parse_run_at

        target = datetime(2026, 10, 23, 9, 0)
        assert parse_run_at("2026-10-23T09:00") == int(target.timestamp())

    def test_invalid_raises(self):
        import pytest
        from services.cron_parser import parse_run_at, CronParseError

        with pytest.raises(CronParseError):
            parse_run_at("tomorrow-ish")
        with pytest.raises(CronParseError):
            parse_run_at("")


class TestOneShotSchedule:
    async def test_run_at_creates_one_shot_task(self, db, monkeypatch):
        import context as _context
        from tools.builtin.system_tool_advanced import cron_schedule

        monkeypatch.setattr(_context, "get_db", lambda: db)
        now = int(time.time())
        result = await cron_schedule(task="提醒我喝水", run_at="30m")

        assert result["success"] is True
        data = result["data"]
        assert data["once"] is True
        assert data["cron_expr"] == ""
        assert now + 1700 <= data["next_run_at"] <= now + 1900

        cursor = await db.execute(
            "SELECT cron_expr, next_run_at FROM scheduled_tasks WHERE id=?", (data["cron_id"],)
        )
        row = await cursor.fetchone()
        assert row[0] == ""
        assert row[1] == data["next_run_at"]

    async def test_run_at_invalid_returns_error(self, db, monkeypatch):
        import context as _context
        from tools.builtin.system_tool_advanced import cron_schedule

        monkeypatch.setattr(_context, "get_db", lambda: db)
        result = await cron_schedule(task="x", run_at="过一会儿")
        assert result["success"] is False
        assert "排期" in result["error"]

    async def test_missing_both_schedule_and_run_at_returns_error(self, db, monkeypatch):
        import context as _context
        from tools.builtin.system_tool_advanced import cron_schedule

        monkeypatch.setattr(_context, "get_db", lambda: db)
        result = await cron_schedule(task="x")
        assert result["success"] is False
        assert "run_at" in result["error"]

    async def test_empty_task_returns_error(self, db, monkeypatch):
        import context as _context
        from tools.builtin.system_tool_advanced import cron_schedule

        monkeypatch.setattr(_context, "get_db", lambda: db)
        result = await cron_schedule(schedule="0 9 * * *", task="   ")
        assert result["success"] is False

    async def test_oneshot_completes_instead_of_rescheduling(self, db):
        """一次性任务触发后标 completed（调度器靠 next_run_at=None 区分）。"""
        from repositories.scheduled_tasks_repo import (
            claim_due_scheduled_tasks, complete_scheduled_task, insert_scheduled_task,
        )

        now = int(time.time())
        tid = await insert_scheduled_task(
            db, task_input="跑一次", cron_expr="", next_run_at=now - 1,
        )
        due = await claim_due_scheduled_tasks(db, now, limit=5)
        assert [d["id"] for d in due] == [tid]
        assert due[0]["cron_expr"] == ""

        await complete_scheduled_task(db, tid)
        cursor = await db.execute(
            "SELECT status, run_count FROM scheduled_tasks WHERE id=?", (tid,)
        )
        row = await cursor.fetchone()
        assert row[0] == "completed"
        assert row[1] == 1
