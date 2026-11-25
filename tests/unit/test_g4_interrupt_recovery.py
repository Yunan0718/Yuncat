"""tests/unit/test_g4_interrupt_recovery.py —— G4: 任务中断恢复（interrupted 状态）

覆盖：
- interrupt_task: running → interrupted（可恢复，区别于 cancelled 终态）
- interrupt_task: 跳过终态（completed/failed/cancelled 不被覆盖）
- resume_task: interrupted → running（恢复执行）
- interrupt_task: 取消 asyncio handle + 调 adapter.cancel
- interrupt_task: WS 推送 task_interrupted 消息
- resume_after_restart: 不自动恢复 interrupted 任务（仅恢复 running 崩溃任务）

对标 Cursor 断点续传 / Claude Code crash recovery：
- interrupted = 非用户主动的、可恢复的中断（优雅关闭/强杀）
- cancelled = 用户明确放弃（终态，不可恢复）
- paused = 用户主动暂停
"""
import os
import sys
import asyncio
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ---- Mock Adapter（复用 test_agent_service.py 的模式）----
class MockAdapter:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self._ready = asyncio.Event()
        self._ready.set()
        self.cancelled_tasks: list[str] = []
        self.stopped = False

    async def health(self) -> bool:
        return True

    async def start(self):
        self._ready.set()

    async def stop(self):
        self.stopped = True

    async def wait_ready(self):
        await self._ready.wait()

    async def cancel(self, task_id: str):
        self.cancelled_tasks.append(task_id)

    async def stream(self, prompt: str, context: dict | None = None):
        yield f"回复: {prompt[:30]}"
        await asyncio.sleep(0.01)
        yield "...完成"

    def capabilities(self):
        from adapters.base import AgentCapability
        return AgentCapability(supports_stream=True)


class FakeWS:
    def __init__(self):
        self.messages: list[dict] = []

    async def send_json(self, payload: dict):
        self.messages.append(payload)

    async def close(self, code=1000):
        pass


@pytest.fixture
async def agent_svc(tmp_path):
    """创建 AgentService + aiosqlite DB + mock adapter"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_g4.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()

    from services.agent.agent_service import AgentService
    adapters = {"hermes": MockAdapter("hermes")}
    svc = AgentService(conn, adapters=adapters)
    async def fake_config(k="", **kw): return {"api_key": "sk-test", "model": "test"}
    svc._get_model_config = fake_config
    yield svc
    await conn.close()


async def _create_running_task(agent_svc, task_id="task-int-1"):
    """创建一个 running 状态的任务（直接 INSERT，不走 start_task 避免 _run_agent）"""
    now = int(time.time())
    await agent_svc.db.execute(
        "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (task_id, "test interrupt", "running", "hermes", "sess-1", now, now),
    )
    await agent_svc.db.commit()
    return task_id


class TestInterruptTask:
    """interrupt_task: running → interrupted"""

    async def test_interrupt_marks_running_as_interrupted(self, agent_svc):
        """running 任务被 interrupt 后状态变为 interrupted"""
        tid = await _create_running_task(agent_svc)
        await agent_svc.interrupt_task(tid)
        task = await agent_svc.get_task(tid)
        assert task["status"] == "interrupted"

    async def test_interrupt_skips_completed(self, agent_svc):
        """已完成任务不被 interrupt"""
        now = int(time.time())
        await agent_svc.db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
            "VALUES(?,?,?,?,?,?)",
            ("task-done", "x", "completed", "hermes", "s1", now),
        )
        await agent_svc.db.commit()
        await agent_svc.interrupt_task("task-done")
        task = await agent_svc.get_task("task-done")
        assert task["status"] == "completed", "已完成任务不应被 interrupt"

    async def test_interrupt_skips_failed(self, agent_svc):
        """已失败任务不被 interrupt"""
        now = int(time.time())
        await agent_svc.db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
            "VALUES(?,?,?,?,?,?)",
            ("task-failed", "x", "failed", "hermes", "s1", now),
        )
        await agent_svc.db.commit()
        await agent_svc.interrupt_task("task-failed")
        task = await agent_svc.get_task("task-failed")
        assert task["status"] == "failed"

    async def test_interrupt_skips_cancelled(self, agent_svc):
        """已取消任务不被 interrupt"""
        now = int(time.time())
        await agent_svc.db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
            "VALUES(?,?,?,?,?,?)",
            ("task-cancelled", "x", "cancelled", "hermes", "s1", now),
        )
        await agent_svc.db.commit()
        await agent_svc.interrupt_task("task-cancelled")
        task = await agent_svc.get_task("task-cancelled")
        assert task["status"] == "cancelled"

    async def test_interrupt_cancels_adapter(self, agent_svc):
        """interrupt 应调用 adapter.cancel"""
        tid = await _create_running_task(agent_svc)
        adapter = agent_svc.adapters["hermes"]
        await agent_svc.interrupt_task(tid)
        assert tid in adapter.cancelled_tasks, "adapter.cancel 应被调用"

    async def test_interrupt_publishes_ws(self, agent_svc):
        """interrupt 应推送 task_interrupted WS 消息"""
        tid = await _create_running_task(agent_svc)
        ws = FakeWS()
        agent_svc.register_ws(tid, ws)
        await agent_svc.interrupt_task(tid)
        assert any(m.get("type") == "task_interrupted" and m.get("task_id") == tid
                   for m in ws.messages), "应推送 task_interrupted WS 消息"

    async def test_interrupt_nonexistent_task_no_error(self, agent_svc):
        """interrupt 不存在的任务不应抛异常"""
        # 应静默返回，不抛 KeyError 或类似错误
        await agent_svc.interrupt_task("nonexistent-task-id")


class TestResumeFromInterrupted:
    """resume_task: interrupted → running"""

    async def test_resume_from_interrupted(self, agent_svc):
        """interrupted 任务可被 resume 恢复为 running"""
        now = int(time.time())
        await agent_svc.db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("task-int-resume", "resume me", "interrupted", "hermes", "sess-r", now, now),
        )
        await agent_svc.db.commit()
        # Mock _run_agent 避免启动真实 agent loop
        async def _mock_run_agent(task_id, agent_id, prompt, session_id,
                                  rules_context="", mode="agent", extra_context=None):
            pass
        agent_svc._run_agent = _mock_run_agent
        ok = await agent_svc.resume_task("task-int-resume")
        assert ok is True
        task = await agent_svc.get_task("task-int-resume")
        assert task["status"] == "running"

    async def test_resume_rejects_completed(self, agent_svc):
        """completed 任务不可 resume"""
        now = int(time.time())
        await agent_svc.db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
            "VALUES(?,?,?,?,?,?)",
            ("task-done-r", "x", "completed", "hermes", "s1", now),
        )
        await agent_svc.db.commit()
        ok = await agent_svc.resume_task("task-done-r")
        assert ok is False, "completed 任务不应被 resume"

    async def test_resume_rejects_cancelled(self, agent_svc):
        """cancelled 任务不可 resume"""
        now = int(time.time())
        await agent_svc.db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
            "VALUES(?,?,?,?,?,?)",
            ("task-cxl-r", "x", "cancelled", "hermes", "s1", now),
        )
        await agent_svc.db.commit()
        ok = await agent_svc.resume_task("task-cxl-r")
        assert ok is False, "cancelled 任务不应被 resume"


class TestResumeAfterRestartSkipsInterrupted:
    """resume_after_restart 不自动恢复 interrupted 任务（仅恢复 running 崩溃任务）"""

    async def test_resume_after_restart_skips_interrupted(self, agent_svc):
        """interrupted 任务不被 resume_after_restart 自动恢复（用户需手动 resume）"""
        long_ago = int(time.time()) - 3600
        await agent_svc.db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at, "
            "updated_at, resume_count, task_state) VALUES(?,?,?,?,?,?,?,?,?)",
            ("task-int-auto", "test", "interrupted", "hermes", "sess-1",
             long_ago, long_ago, 0, '{}'),
        )
        await agent_svc.db.commit()
        agent_svc._run_agent_called = []
        async def _mock(task_id, agent_id, prompt, session_id,
                        rules_context="", mode="agent", extra_context=None):
            agent_svc._run_agent_called.append(task_id)
        agent_svc._run_agent = _mock
        resumed = await agent_svc.resume_after_restart()
        assert resumed == 0, "interrupted 任务不应被自动恢复"
        assert "task-int-auto" not in agent_svc._run_agent_called
