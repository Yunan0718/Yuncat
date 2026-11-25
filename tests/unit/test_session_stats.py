"""会话维度的统计：用量 / 任务数（跨会话汇总 + 会话列表附加字段）。

口径与用量看板一致——账本口径（输入 + 输出），事件类型与字段名走
services/llm_usage.py 的常量。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def stats_env(tmp_path):
    import aiosqlite
    from repositories import SCHEMA_SQL

    conn = await aiosqlite.connect(str(tmp_path / "stats.db"))
    await conn.executescript(SCHEMA_SQL)
    yield conn
    await conn.close()


async def _seed(conn):
    """两个会话：s1 有用量与任务，s2 只有消息。"""
    import time

    now = int(time.time())
    await conn.execute(
        "INSERT INTO conversations(id, session_id, role, content, token_count, agent_id, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("m1", "s1", "user", "第一个问题", 100, "yuncat", now - 30),
    )
    await conn.execute(
        "INSERT INTO conversations(id, session_id, role, content, token_count, agent_id, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("m2", "s1", "assistant", "回答", 200, "yuncat", now - 20),
    )
    await conn.execute(
        "INSERT INTO conversations(id, session_id, role, content, token_count, agent_id, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("m3", "s2", "user", "第二个会话", 50, "yuncat", now - 10),
    )
    await conn.execute(
        "INSERT INTO tasks(id, input, session_id, status, agent_id, created_at) VALUES(?,?,?,?,?,?)",
        ("t1", "x", "s1", "completed", "yuncat", now - 30),
    )
    await conn.execute(
        "INSERT INTO tasks(id, input, session_id, status, agent_id, created_at) VALUES(?,?,?,?,?,?)",
        ("t2", "y", "s1", "failed", "yuncat", now - 25),
    )
    await conn.execute(
        "INSERT INTO analytics_events(event_type, event_data, created_at) VALUES(?,?,?)",
        (
            "token_usage",
            json.dumps({
                "session_id": "s1",
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "total_tokens": 1200,
            }),
            now - 30,
        ),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_attach_session_stats_adds_tokens_and_tasks(stats_env):
    from services.session_stats import attach_session_stats

    conn = stats_env
    await _seed(conn)
    rows = await attach_session_stats(conn, [{"session_id": "s1"}, {"session_id": "s2"}])

    s1, s2 = rows
    assert s1["total_tokens"] == 1200
    assert s1["prompt_tokens"] == 1000
    assert s1["completion_tokens"] == 200
    assert s1["llm_calls"] == 1
    assert s1["task_count"] == 2
    assert s1["completed_tasks"] == 1
    assert s1["failed_tasks"] == 1
    # 没有用量的会话也要有字段（前端不必判空）
    assert s2["total_tokens"] == 0
    assert s2["task_count"] == 0


@pytest.mark.asyncio
async def test_session_stats_overview(stats_env):
    from services.session_stats import session_stats_overview

    conn = stats_env
    await _seed(conn)
    overview = await session_stats_overview(conn, days=3650)

    assert overview["session_count"] == 2
    assert overview["message_count"] == 3
    assert overview["task_count"] == 2
    assert overview["total_tokens"] == 1200
    assert overview["by_status"].get("completed") == 1
    assert overview["by_status"].get("failed") == 1
    assert any(a["agent_id"] == "yuncat" and a["tasks"] == 2 for a in overview["by_agent"])
    assert overview["daily"]


@pytest.mark.asyncio
async def test_session_stats_overview_empty_db(stats_env):
    from services.session_stats import session_stats_overview

    overview = await session_stats_overview(stats_env, days=30)
    assert overview["session_count"] == 0
    assert overview["total_tokens"] == 0
    assert overview["daily"] == []


@pytest.mark.asyncio
async def test_sessions_route_carries_stats(stats_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.sessions import router

    conn = stats_env
    await _seed(conn)
    app = FastAPI()
    app.state.db = conn
    app.include_router(router)
    body = TestClient(app).get("/api/v1/sessions").json()

    assert len(body) == 2
    s1 = next(r for r in body if r["session_id"] == "s1")
    assert s1["total_tokens"] == 1200
    assert s1["task_count"] == 2


@pytest.mark.asyncio
async def test_sessions_stats_route(stats_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.sessions import router

    conn = stats_env
    await _seed(conn)
    app = FastAPI()
    app.state.db = conn
    app.include_router(router)
    resp = TestClient(app).get("/api/v1/sessions/stats?days=3650")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_count"] == 2
    assert body["total_tokens"] == 1200
