"""tests/unit/test_plan_step_optimistic_lock.py —— D4: plan_steps 完成标记事务保护

覆盖:
- expected_from 匹配时正常转移
- expected_from 不匹配时跳过（affected_rows=0）+ 返回 False
- expected_from=None 时保持原行为（向后兼容）
- 状态机：pending→running→completed/failed/skipped 全链路
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
async def db(tmp_path):
    """aiosqlite + SCHEMA_SQL + 一个测试 plan_step 节点"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_d4.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    # 插入测试节点——初始状态 pending
    await conn.execute(
        "INSERT INTO plan_steps(id, task_id, level, title, status, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("node-1", "task-1", 0, "test step", "pending", int(time.time()), int(time.time())),
    )
    await conn.commit()
    yield conn
    await conn.close()
    _safe_unlink(db_path)


async def _get_node_status(conn, node_id: str) -> str:
    cursor = await conn.execute("SELECT status FROM plan_steps WHERE id=?", (node_id,))
    row = await cursor.fetchone()
    return row[0] if row else ""


class TestExpectedFromMatch:
    """expected_from 匹配时正常转移"""

    async def test_pending_to_running(self, db):
        """pending→running 转移成功"""
        from repositories.plan_step_repo import update_plan_node_status
        ok = await update_plan_node_status(db, "node-1", "running", expected_from="pending")
        assert ok is True
        assert await _get_node_status(db, "node-1") == "running"

    async def test_running_to_completed(self, db):
        """running→completed 转移成功（带 output）"""
        from repositories.plan_step_repo import update_plan_node_status
        await update_plan_node_status(db, "node-1", "running", expected_from="pending")
        ok = await update_plan_node_status(db, "node-1", "completed",
                                           output="done", expected_from="running")
        assert ok is True
        assert await _get_node_status(db, "node-1") == "completed"
        cursor = await db.execute("SELECT output FROM plan_steps WHERE id=?", ("node-1",))
        assert (await cursor.fetchone())[0] == "done"

    async def test_running_to_failed(self, db):
        """running→failed 转移成功"""
        from repositories.plan_step_repo import update_plan_node_status
        await update_plan_node_status(db, "node-1", "running", expected_from="pending")
        ok = await update_plan_node_status(db, "node-1", "failed",
                                           output="boom", expected_from="running")
        assert ok is True
        assert await _get_node_status(db, "node-1") == "failed"

    async def test_running_to_skipped(self, db):
        """running→skipped 转移成功"""
        from repositories.plan_step_repo import update_plan_node_status
        await update_plan_node_status(db, "node-1", "running", expected_from="pending")
        ok = await update_plan_node_status(db, "node-1", "skipped",
                                           output="halted", expected_from="running")
        assert ok is True
        assert await _get_node_status(db, "node-1") == "skipped"


class TestExpectedFromMismatch:
    """expected_from 不匹配时跳过——D4 核心修复"""

    async def test_skip_when_status_already_changed(self, db):
        """状态已被其他进程改变时，不应覆盖"""
        from repositories.plan_step_repo import update_plan_node_status
        # node-1 当前是 pending，但期望从 running 转移——应跳过
        ok = await update_plan_node_status(db, "node-1", "completed", expected_from="running")
        assert ok is False
        # 状态应保持 pending
        assert await _get_node_status(db, "node-1") == "pending"

    async def test_skip_when_already_terminal(self, db):
        """节点已处于终态时，不应被覆盖"""
        from repositories.plan_step_repo import update_plan_node_status
        # 先把节点标记为 failed
        await update_plan_node_status(db, "node-1", "running", expected_from="pending")
        await update_plan_node_status(db, "node-1", "failed", expected_from="running")

        # 尝试从 running 转移到 completed——应跳过（已是 failed）
        ok = await update_plan_node_status(db, "node-1", "completed", expected_from="running")
        assert ok is False
        assert await _get_node_status(db, "node-1") == "failed"

    async def test_skip_does_not_overwrite_output(self, db):
        """跳过转移时不覆盖已有的 output"""
        from repositories.plan_step_repo import update_plan_node_status
        await update_plan_node_status(db, "node-1", "running", expected_from="pending")
        await update_plan_node_status(db, "node-1", "completed",
                                      output="original", expected_from="running")

        # 尝试从 running 转移到 failed——应跳过（已是 completed）
        ok = await update_plan_node_status(db, "node-1", "failed",
                                           output="overwrite", expected_from="running")
        assert ok is False
        cursor = await db.execute("SELECT output FROM plan_steps WHERE id=?", ("node-1",))
        assert (await cursor.fetchone())[0] == "original", "output 不应被跳过的转移覆盖"


class TestBackwardCompatibility:
    """expected_from=None 时保持原行为（向后兼容）"""

    async def test_unconditional_update_without_expected_from(self, db):
        """不传 expected_from 时，无条件 UPDATE（原行为）"""
        from repositories.plan_step_repo import update_plan_node_status
        # 先改成 completed
        await update_plan_node_status(db, "node-1", "completed")
        # 不传 expected_from，应无条件覆盖为 failed
        await update_plan_node_status(db, "node-1", "failed")
        assert await _get_node_status(db, "node-1") == "failed"

    async def test_unconditional_update_returns_none(self, db):
        """不传 expected_from 时返回 None（原行为，不返回 bool）"""
        from repositories.plan_step_repo import update_plan_node_status
        result = await update_plan_node_status(db, "node-1", "running")
        # 向后兼容：不传 expected_from 时返回 None（不是 bool）
        assert result is None
