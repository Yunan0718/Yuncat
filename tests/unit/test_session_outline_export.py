"""会话 outline 与 Markdown/JSONL 导出。"""
import json
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def client_db(tmp_path):
    import aiosqlite
    from repositories import SCHEMA_SQL
    from routes.sessions import router

    conn = await aiosqlite.connect(str(tmp_path / "sessions.db"))
    await conn.executescript(SCHEMA_SQL)
    await conn.execute(
        "INSERT INTO tasks(id,input,status,agent_id,session_id,created_at) VALUES(?,?,?,?,?,?)",
        ("t1", "修复登录", "completed", "yuncat", "s1", 1),
    )
    for index, role, content in (
        (1, "user", "修复登录"), (2, "assistant", "已检查认证流程"),
        (3, "user", "继续补测试"), (4, "assistant", "测试通过"),
    ):
        await conn.execute(
            "INSERT INTO conversations(id,session_id,role,content,created_at) VALUES(?,?,?,?,?)",
            (f"m{index}", "s1", role, content, index),
        )
    await conn.commit()
    app = FastAPI()
    app.state.db = conn
    app.include_router(router)
    yield TestClient(app), conn
    await conn.close()


def test_outline_groups_turns_and_links_task(client_db):
    client, _ = client_db
    body = client.get("/api/v1/sessions/s1/outline").json()
    assert len(body["turns"]) == 2
    assert body["turns"][0]["task_id"] == "t1"
    assert body["turns"][0]["preview"] == "已检查认证流程"
    assert body["turns"][1]["title"] == "继续补测试"


def test_export_jsonl_and_markdown(client_db):
    client, _ = client_db
    jsonl = client.get("/api/v1/sessions/s1/export?format=jsonl").json()["jsonl"]
    records = [json.loads(line) for line in jsonl.splitlines()]
    assert records[0]["type"] == "session"
    assert any(r.get("content") == "测试通过" for r in records[1:])
    markdown = client.get("/api/v1/sessions/s1/export").json()["markdown"]
    assert "# Yuncat 对话导出" in markdown
    assert "继续补测试" in markdown
    assert client.get("/api/v1/sessions/s1/export?format=xml").status_code == 400
