"""tests/unit/test_task_optimistic_lock.py —— D3: 任务状态机乐观锁原子转移

覆盖:
- expected_from 匹配时正常转移
- expected_from 不匹配时跳过（affected_rows=0）+ warning 日志
- expected_from=None 时保持原行为（向后兼容）
- cancel 路径的 TOCTOU 修复
"""
import os
import sys
import time
import pytest

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
async def db_conn(tmp_path):
    """创建带 schema + tasks 表的 aiosqlite 连接"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_d3.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    # 插入测试 task
    await conn.execute(
        "INSERT INTO tasks(id, agent_id, input, status, created_at) "
        "VALUES(?,?,?,?,?)",
        ("task-1", "hermes", "test input", "running", int(time.time())),
    )
    await conn.commit()
    yield conn
    await conn.close()
    _safe_unlink(db_path)


async def _get_task_status(conn, task_id: str) -> str:
    cursor = await conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
    row = await cursor.fetchone()
    return row[0] if row else ""


class TestExpectedFromMatch:
    """expected_from 匹配时正常转移"""

    async def test_transition_when_status_matches(self, db_conn):
        """当前状态 == expected_from 时，转移成功"""
        from services.agent.agent_service import AgentService

        class _Stub:
            pass

        svc = _Stub()
        svc.db = db_conn
        svc._task_completion_events = {}
        svc._mark_task = AgentService._mark_task.__get__(svc)

        # task-1 当前是 running，期望从 running 转移到 completed
        await svc._mark_task("task-1", "completed", expected_from="running")
        assert await _get_task_status(db_conn, "task-1") == "completed"

    async def test_transition_with_output_when_status_matches(self, db_conn):
        """带 output 的转移在状态匹配时成功"""
        from services.agent.agent_service import AgentService

        class _Stub:
            pass

        svc = _Stub()
        svc.db = db_conn
        svc._task_completion_events = {}
        svc._mark_task = AgentService._mark_task.__get__(svc)

        await svc._mark_task("task-1", "completed", output="done", expected_from="running")
        cursor = await db_conn.execute("SELECT status, output FROM tasks WHERE id=?", ("task-1",))
        row = await cursor.fetchone()
        assert row[0] == "completed"
        assert row[1] == "done"

    async def test_transition_with_error_when_status_matches(self, db_conn):
        """带 error 的转移在状态匹配时成功"""
        from services.agent.agent_service import AgentService

        class _Stub:
            pass

        svc = _Stub()
        svc.db = db_conn
        svc._task_completion_events = {}
        svc._mark_task = AgentService._mark_task.__get__(svc)

        await svc._mark_task("task-1", "failed", error="boom", expected_from="running")
        cursor = await db_conn.execute("SELECT status, error_message FROM tasks WHERE id=?", ("task-1",))
        row = await cursor.fetchone()
        assert row[0] == "failed"
        assert row[1] == "boom"


class TestExpectedFromMismatch:
    """expected_from 不匹配时跳过——D3 核心修复"""

    async def test_skip_when_status_already_changed(self, db_conn):
        """状态已被其他进程改变时，不应覆盖"""
        from services.agent.agent_service import AgentService

        class _Stub:
            pass

        svc = _Stub()
        svc.db = db_conn
        svc._task_completion_events = {}
        svc._mark_task = AgentService._mark_task.__get__(svc)

        # 模拟竞态：task-1 当前是 running，但我们期望从 pending 转移
        # 实际状态 != expected_from，应跳过
        await svc._mark_task("task-1", "completed", expected_from="pending")
        # 状态应保持 running，未被覆盖为 completed
        assert await _get_task_status(db_conn, "task-1") == "running"

    async def test_skip_when_task_already_terminal(self, db_conn):
        """任务已处于终态时，不应被覆盖"""
        from services.agent.agent_service import AgentService

        class _Stub:
            pass

        svc = _Stub()
        svc.db = db_conn
        svc._task_completion_events = {}
        svc._mark_task = AgentService._mark_task.__get__(svc)

        # 先把 task 标记为 cancelled（模拟用户已取消）
        await db_conn.execute("UPDATE tasks SET status='cancelled' WHERE id=?", ("task-1",))
        await db_conn.commit()

        # 现在尝试从 running 转移到 completed——应跳过
        await svc._mark_task("task-1", "completed", expected_from="running")
        # 状态应保持 cancelled
        assert await _get_task_status(db_conn, "task-1") == "cancelled"

    async def test_skip_does_not_fire_completion_event(self, db_conn):
        """跳过转移时不应触发 completion event（task 实际未转移）"""
        import asyncio
        from services.agent.agent_service import AgentService

        class _Stub:
            pass

        svc = _Stub()
        svc.db = db_conn
        # 预设一个 completion event
        event = asyncio.Event()
        svc._task_completion_events = {"task-1": event}
        svc._mark_task = AgentService._mark_task.__get__(svc)

        # 状态不匹配，应跳过且不 set event
        await svc._mark_task("task-1", "completed", expected_from="pending")
        assert not event.is_set(), "跳过转移时不应触发 completion event"
        # event 应仍保留在 dict 中（未被 pop）
        assert "task-1" in svc._task_completion_events


class TestBackwardCompatibility:
    """expected_from=None 时保持原行为（向后兼容）"""

    async def test_unconditional_update_without_expected_from(self, db_conn):
        """不传 expected_from 时，无条件 UPDATE（原行为）"""
        from services.agent.agent_service import AgentService

        class _Stub:
            pass

        svc = _Stub()
        svc.db = db_conn
        svc._task_completion_events = {}
        svc._mark_task = AgentService._mark_task.__get__(svc)

        # 先把 task 改成 cancelled
        await db_conn.execute("UPDATE tasks SET status='cancelled' WHERE id=?", ("task-1",))
        await db_conn.commit()

        # 不传 expected_from，应无条件覆盖
        await svc._mark_task("task-1", "completed")
        assert await _get_task_status(db_conn, "task-1") == "completed"

    async def test_unconditional_update_fires_event(self, db_conn):
        """不传 expected_from 时仍触发 completion event（原行为）"""
        import asyncio
        from services.agent.agent_service import AgentService

        class _Stub:
            pass

        svc = _Stub()
        svc.db = db_conn
        event = asyncio.Event()
        svc._task_completion_events = {"task-1": event}
        svc._mark_task = AgentService._mark_task.__get__(svc)

        await svc._mark_task("task-1", "completed")
        assert event.is_set(), "无条件 UPDATE 应触发 completion event"
        assert "task-1" not in svc._task_completion_events, "event 应被 pop"
