"""定时任务管理 API（DSH schedule 的"人机确认"那一半）。

agent 用 cron_schedule 建任务，但用户侧此前没有入口：看不到建了哪些、
下次什么时候跑、上次成功没有、怎么取消。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def schedules_client(tmp_path):
    import aiosqlite
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from repositories import SCHEMA_SQL
    from routes.schedules import router

    conn = await aiosqlite.connect(str(tmp_path / "sched.db"))
    await conn.executescript(SCHEMA_SQL)
    now = int(time.time())
    rows = [
        ("s-pending", "每天同步一次", "yuncat", "0 9 * * *", now + 3600, "pending", "daily", 0),
        ("s-paused", "周报", "yuncat", "0 10 * * 1", now + 7200, "paused", "weekly", 3),
        ("s-failed", "月度清理", "yuncat", "0 3 1 * *", now - 100, "failed", "", 2),
    ]
    for rid, task, agent, cron, nxt, status, label, errors in rows:
        await conn.execute(
            "INSERT INTO scheduled_tasks(id, task_input, agent_id, cron_expr, next_run_at, "
            "status, label, created_at, run_count, error_count) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (rid, task, agent, cron, nxt, status, label, now - 1000, 5, errors),
        )
    await conn.commit()

    app = FastAPI()
    app.state.db = conn
    app.include_router(router)
    yield TestClient(app), conn
    await conn.close()


def test_list_returns_all_statuses_with_pending_first(schedules_client):
    client, _ = schedules_client
    body = client.get("/api/v1/schedules").json()
    ids = [r["id"] for r in body["items"]]
    assert ids[0] == "s-pending"
    assert set(ids) == {"s-pending", "s-paused", "s-failed"}
    row = body["items"][0]
    assert row["cron_expr"] == "0 9 * * *"
    assert row["run_count"] == 5


def test_list_filters_by_status(schedules_client):
    client, _ = schedules_client
    body = client.get("/api/v1/schedules?status=failed").json()
    assert [r["id"] for r in body["items"]] == ["s-failed"]


def test_cancel_pending_task(schedules_client):
    client, conn = schedules_client
    resp = client.post("/api/v1/schedules/s-pending/cancel")
    assert resp.status_code == 200, resp.text
    body = client.get("/api/v1/schedules?status=paused").json()
    assert "s-pending" in [r["id"] for r in body["items"]]


def test_cancel_unknown_task_404(schedules_client):
    client, _ = schedules_client
    assert client.post("/api/v1/schedules/nope/cancel").status_code == 404


def test_cancel_already_paused_404(schedules_client):
    """只有 pending 能取消——已取消的再点一次返回 404（不是静默成功）。"""
    client, _ = schedules_client
    assert client.post("/api/v1/schedules/s-paused/cancel").status_code == 404
