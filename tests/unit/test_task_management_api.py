"""任务状态类端点：不存在的任务必须 404，不能"假装成功"。

2026-10-22：pin/tags/interrupt/pause 此前对不存在的 task_id 直接返回
{"status": "ok"}——前端会以为操作成功了，DB 里什么都没变。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class _FakeAgentService:
    def __init__(self, tasks: dict[str, dict]):
        self._tasks = tasks
        self._task_handles: dict = {}
        self.interrupted: list[str] = []
        self.resumed: list[str] = []
        self.sent: list[tuple[str, dict]] = []
        self.adapters: dict = {}

    async def get_task(self, task_id):
        return self._tasks.get(task_id)

    async def interrupt_task(self, task_id):
        self.interrupted.append(task_id)

    async def resume_task(self, task_id):
        return task_id in self.resumed

    async def _safe_send(self, task_id, payload):
        self.sent.append((task_id, payload))


@pytest.fixture
async def task_api(tmp_path):
    import aiosqlite
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from repositories import SCHEMA_SQL
    from routes.tasks import router

    conn = await aiosqlite.connect(str(tmp_path / "tasks.db"))
    await conn.executescript(SCHEMA_SQL)
    await conn.execute(
        "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
        "VALUES(?,?,?,?,?,?)",
        ("t1", "hello", "running", "yuncat", "s1", 1),
    )
    await conn.commit()

    agent = _FakeAgentService({
        "t1": {"id": "t1", "status": "running", "agent_id": "yuncat"},
    })
    app = FastAPI()
    app.state.db = conn
    app.state.agent_service = agent
    app.include_router(router)
    yield TestClient(app), conn, agent
    await conn.close()


def test_pin_missing_task_404(task_api):
    client, _, _ = task_api
    assert client.post("/api/v1/tasks/missing/pin", json={"pinned": True}).status_code == 404


@pytest.mark.asyncio
async def test_pin_updates_existing_task(task_api):
    client, conn, _ = task_api
    resp = client.post("/api/v1/tasks/t1/pin", json={"pinned": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["pinned"] is True
    cur = await conn.execute("SELECT pinned FROM tasks WHERE id='t1'")
    assert (await cur.fetchone())[0] == 1


def test_tags_missing_task_404(task_api):
    client, _, _ = task_api
    assert client.post("/api/v1/tasks/missing/tags", json={"tags": ["a"]}).status_code == 404


def test_tags_updates_existing_task(task_api):
    client, _, _ = task_api
    resp = client.post("/api/v1/tasks/t1/tags", json={"tags": ["重要", "bug"]})
    assert resp.status_code == 200


def test_pause_missing_task_404(task_api):
    client, _, _ = task_api
    assert client.post("/api/v1/tasks/missing/pause").status_code == 404


def test_pause_existing_task(task_api):
    client, conn, agent = task_api
    resp = client.post("/api/v1/tasks/t1/pause")
    assert resp.status_code == 200
    assert any(payload.get("type") == "task_paused" for _tid, payload in agent.sent)


def test_interrupt_missing_task_404(task_api):
    client, _, agent = task_api
    assert client.post("/api/v1/tasks/missing/interrupt").status_code == 404
    assert agent.interrupted == []


def test_interrupt_existing_task(task_api):
    client, _, agent = task_api
    resp = client.post("/api/v1/tasks/t1/interrupt")
    assert resp.status_code == 200
    assert agent.interrupted == ["t1"]


def test_resume_non_resumable_409(task_api):
    client, _, _ = task_api
    assert client.post("/api/v1/tasks/t1/resume").status_code == 409
