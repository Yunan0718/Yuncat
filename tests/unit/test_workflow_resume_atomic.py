"""tests/unit/test_workflow_resume_atomic.py —— D5: workflow_runs breakpoint 事务保护

覆盖 Path A (workflow_service.resume_run):
- paused→running 原子转移成功
- 非 paused 状态被拒绝（Python 层 + SQL 层双保险）
- 竞态保护：read 和 write 之间状态被改变时，UPDATE 影响 0 行 → 抛 ValueError
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
async def setup(tmp_path):
    """创建 WorkflowService + 一个 paused 状态的 workflow_run"""
    from services.workflow.workflow_service import WorkflowService
    from repositories import SCHEMA_SQL

    db_path = os.path.join(str(tmp_path), "test_d5.db")
    conn = await aiosqlite.connect(db_path)
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()

    # 插入一个 workflow + 一个 paused run
    await conn.execute(
        "INSERT INTO workflows(id, name, graph, created_at, updated_at) "
        "VALUES(?,?,?,?,?)",
        ("wf-1", "test-wf", '{"nodes":[],"edges":[]}', int(time.time()), int(time.time())),
    )
    await conn.execute(
        "INSERT INTO workflow_runs(id, workflow_id, status, snapshot, "
        "started_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
        ("run-1", "wf-1", "paused", "{}", int(time.time()), int(time.time()), int(time.time())),
    )
    await conn.commit()

    svc = WorkflowService(conn)
    yield conn, svc
    await conn.close()
    _safe_unlink(db_path)


async def _get_run_status(conn, run_id: str) -> str:
    cursor = await conn.execute("SELECT status FROM workflow_runs WHERE id=?", (run_id,))
    row = await cursor.fetchone()
    return row[0] if row else ""


class TestResumeRunAtomic:
    """resume_run 原子 CAS——D5 核心修复"""

    async def test_resume_paused_succeeds(self, setup):
        """paused 状态的 run 可以正常 resume"""
        conn, svc = setup
        # 用 mock engine + mock _resume_and_finalize 避免真跑引擎
        svc.engine = True  # truthy 占位，通过 engine 检查
        svc._resume_and_finalize = _noop_resume

        result = await svc.resume_run("run-1")
        assert result == "run-1"
        assert await _get_run_status(conn, "run-1") == "running"

    async def test_resume_non_paused_rejected(self, setup):
        """非 paused 状态的 run 被拒绝"""
        conn, svc = setup
        svc.engine = True
        svc._resume_and_finalize = _noop_resume

        # 改成 running
        await conn.execute("UPDATE workflow_runs SET status='running' WHERE id=?", ("run-1",))
        await conn.commit()

        with pytest.raises(ValueError, match="not paused"):
            await svc.resume_run("run-1")

    async def test_resume_completed_rejected(self, setup):
        """completed 状态的 run 被拒绝"""
        conn, svc = setup
        svc.engine = True
        svc._resume_and_finalize = _noop_resume

        await conn.execute("UPDATE workflow_runs SET status='completed' WHERE id=?", ("run-1",))
        await conn.commit()

        with pytest.raises(ValueError, match="not paused"):
            await svc.resume_run("run-1")

    async def test_resume_not_found_rejected(self, setup):
        """不存在的 run 被拒绝"""
        _, svc = setup
        svc.engine = True
        svc._resume_and_finalize = _noop_resume
        with pytest.raises(ValueError, match="not found"):
            await svc.resume_run("nonexistent-run")

    async def test_concurrent_resume_atomic_cas(self, setup):
        """D5 核心竞态保护：read 到 paused 后，UPDATE 前状态被改 → 应拒绝

        模拟：get_run 读到 paused，但在 UPDATE 之前另一个进程把状态改成 running。
        修复后 UPDATE 带 AND status='paused' 守卫，affected_rows=0，抛 ValueError。
        """
        conn, svc = setup
        svc.engine = True
        svc._resume_and_finalize = _noop_resume

        original_get_run = svc.get_run

        async def racing_get_run(run_id):
            run = await original_get_run(run_id)
            # 模拟竞态：read 到 paused 后，另一个进程改成 running
            await conn.execute(
                "UPDATE workflow_runs SET status='running' WHERE id=?", (run_id,)
            )
            await conn.commit()
            # 返回原 read 的结果（status=paused）——模拟 read 时还是 paused
            return run

        svc.get_run = racing_get_run

        # resume_run 应检测到 UPDATE affected_rows=0 并抛 ValueError
        with pytest.raises(ValueError, match="status.*changed|not paused"):
            await svc.resume_run("run-1")

        # 状态应保持 running（不被覆盖，没触发 _resume_and_finalize）
        assert await _get_run_status(conn, "run-1") == "running"

    async def test_resume_does_not_spawn_task_on_cas_failure(self, setup):
        """CAS 失败时不应 spawn _resume_and_finalize 后台任务"""
        import asyncio
        conn, svc = setup
        svc.engine = True

        # 记录 create_task 调用
        original_create_task = asyncio.create_task
        task_count = [0]

        def counting_create_task(coro):
            task_count[0] += 1
            coro.close()  # 关闭 coro 避免警告
            return original_create_task(asyncio.sleep(0))

        asyncio.create_task = counting_create_task
        try:
            # 改成 running 模拟竞态
            await conn.execute("UPDATE workflow_runs SET status='running' WHERE id=?", ("run-1",))
            await conn.commit()

            with pytest.raises(ValueError):
                await svc.resume_run("run-1")
        finally:
            asyncio.create_task = original_create_task

        assert task_count[0] == 0, "CAS 失败时不应 spawn 后台任务"


class TestResumeRunNotifiesOnlyOnSuccess:
    """resume 成功才发通知——CAS 失败不发"""

    async def test_no_notify_on_cas_failure(self, setup):
        """CAS 失败时不应调用 _notify"""
        conn, svc = setup
        svc.engine = True
        svc._resume_and_finalize = _noop_resume

        notify_count = [0]

        async def counting_notify(*args, **kwargs):
            notify_count[0] += 1

        svc._notify = counting_notify

        # 改成 running 模拟竞态
        await conn.execute("UPDATE workflow_runs SET status='running' WHERE id=?", ("run-1",))
        await conn.commit()

        with pytest.raises(ValueError):
            await svc.resume_run("run-1")

        assert notify_count[0] == 0, "CAS 失败时不应发 resumed 通知"


async def _noop_resume(run_id, workflow_id):
    """mock _resume_and_finalize——不做任何事"""
    pass
