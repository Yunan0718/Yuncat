"""统一 Job 聚合 API。"""
import json
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class _AgentService:
    async def get_task(self, task_id):
        if task_id != "running-job":
            return None
        return {"id": task_id, "input": "job", "status": "running"}

    async def get_task_state(self, _task_id):
        return {"task_state": {"round": 2, "generated_response": "partial output"}}


@pytest.fixture
async def jobs_client(tmp_path):
    import aiosqlite
    from repositories import SCHEMA_SQL
    from routes.jobs import router

    conn = await aiosqlite.connect(str(tmp_path / "jobs.db"))
    await conn.executescript(SCHEMA_SQL)
    state = json.dumps({"round": 2, "tool_round": 1, "model_used": {"model": "m"}})
    for task_id, status in (("running-job", "running"), ("done-job", "completed")):
        await conn.execute(
            "INSERT INTO tasks(id,input,status,agent_id,session_id,created_at,task_state) "
            "VALUES(?,?,?,?,?,?,?)",
            (task_id, f"{task_id} input", status, "yuncat", "s1", 1, state),
        )
    await conn.commit()
    app = FastAPI()
    app.state.db = conn
    app.state.agent_service = _AgentService()
    app.include_router(router)
    yield TestClient(app), conn
    await conn.close()


def test_list_active_and_terminal_jobs(jobs_client):
    client, _ = jobs_client
    active = client.get("/api/v1/jobs?status=active").json()["items"]
    terminal = client.get("/api/v1/jobs?status=terminal").json()["items"]
    assert [item["id"] for item in active] == ["running-job"]
    assert active[0]["model_used"]["model"] == "m"
    assert [item["id"] for item in terminal] == ["done-job"]
    assert client.get("/api/v1/jobs?status=all&session_id=other").json()["items"] == []
    assert client.get("/api/v1/jobs?status=bogus").status_code == 400


def test_get_job_and_missing(jobs_client):
    client, _ = jobs_client
    body = client.get("/api/v1/jobs/running-job").json()
    assert body["state"]["round"] == 2
    assert client.get("/api/v1/jobs/missing").status_code == 404
