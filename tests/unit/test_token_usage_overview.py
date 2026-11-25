"""token 用量汇总口径：必须用账本（输入 + 输出），不能只报输出。

2026-10-22：Dashboard / 会话用量此前读 ``conversations.token_count``——那个字段
**只记输出 token**。真实消耗以输入为主（实测同一批数据 227,959 输入 vs 2,270 输出，
差 101 倍），界面上的"token 用量"因此严重少报。本测试锁住新口径。
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def usage_db(tmp_path):
    import aiosqlite
    from repositories import SCHEMA_SQL

    conn = await aiosqlite.connect(str(tmp_path / "usage.db"))
    await conn.executescript(SCHEMA_SQL)
    now = int(time.time())
    await conn.execute(
        "INSERT INTO conversations(id,session_id,role,content,token_count,created_at) "
        "VALUES(?,?,?,?,?,?)",
        ("m1", "s1", "assistant", "hi", 50, now),
    )
    await conn.execute(
        "INSERT INTO analytics_events(event_type,event_data,created_at) VALUES(?,?,?)",
        ("token_usage", json.dumps({
            "prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
            "cached_tokens": 400, "cache_miss_tokens": 600, "reasoning_tokens": 10,
            "session_id": "s1", "model": "m", "provider": "p",
        }), now),
    )
    await conn.commit()
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_overview_uses_ledger_totals(usage_db):
    from services.token_tracker import get_token_usage_overview

    overview = await get_token_usage_overview(usage_db, days=7)
    assert overview["total_tokens"] == 1050
    assert overview["prompt_tokens"] == 1000
    assert overview["completion_tokens"] == 50
    assert overview["cached_tokens"] == 400
    assert overview["reasoning_tokens"] == 10
    assert overview["call_count"] == 1
    assert overview["daily"] and overview["daily"][0]["tokens"] == 1050


@pytest.mark.asyncio
async def test_overview_scopes_by_session(usage_db):
    from services.token_tracker import get_token_usage_overview

    assert (await get_token_usage_overview(usage_db, days=0, session_id="s1"))["total_tokens"] == 1050
    assert (await get_token_usage_overview(usage_db, days=0, session_id="other"))["total_tokens"] == 0


@pytest.mark.asyncio
async def test_session_usage_endpoint_reports_both_calibres(usage_db):
    """会话用量端点：total_tokens=账本口径，message_tokens=消息级（仅输出）。"""
    import types
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.sessions import router

    app = FastAPI()
    app.state.db = usage_db
    app.include_router(router)
    body = TestClient(app).get("/api/v1/sessions/s1/usage").json()
    assert body["total_tokens"] == 1050
    assert body["message_tokens"] == 50
    assert body["breakdown"]["assistant"]["tokens"] == 50
