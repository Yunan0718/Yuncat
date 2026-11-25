"""交付物：present 工具的路径校验 + 从会话历史还原文件改动。"""
import asyncio
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def deliver_env(tmp_path, monkeypatch):
    import aiosqlite
    from context import set_app, set_current_agent_id, set_current_task_id
    from repositories import SCHEMA_SQL

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "out.txt").write_text("hello", encoding="utf-8")
    (workspace / "sub").mkdir()
    monkeypatch.setenv("YUNCAT_WORKSPACE", str(workspace))

    conn = await aiosqlite.connect(str(tmp_path / "deliv.db"))
    await conn.executescript(SCHEMA_SQL)
    await conn.execute(
        "INSERT INTO tasks(id, input, session_id, status, agent_id) VALUES(?,?,?,?,?)",
        ("t1", "做点东西", "s1", "running", "yuncat"),
    )
    await conn.commit()
    app = types.SimpleNamespace(state=types.SimpleNamespace(
        db=conn, agent_service=types.SimpleNamespace(ws_manager=None)))
    set_app(app)
    task_token = set_current_task_id("t1")
    agent_token = set_current_agent_id("yuncat")
    yield conn, workspace
    set_current_agent_id(agent_token)
    set_current_task_id(task_token)
    await conn.close()


def test_present_records_valid_file(deliver_env):
    from repositories import list_session_deliverables
    from tools.builtin.system_tool_present import present

    conn, _workspace = deliver_env
    result = asyncio.run(present([{"path": "out.txt", "description": "产物"}]))
    assert result["success"] is True
    assert result["observation"].startswith("Presented 1 file(s):")
    rows = asyncio.run(list_session_deliverables(conn, "s1"))
    assert len(rows) == 1
    assert rows[0]["source"] == "present"
    assert rows[0]["description"] == "产物"
    assert rows[0]["file_path"].endswith("out.txt")


@pytest.mark.parametrize("bad_path", ["missing.txt", "sub"])
def test_present_rejects_non_files(deliver_env, bad_path):
    from tools.builtin.system_tool_present import present

    result = asyncio.run(present([{"path": bad_path}]))
    assert result["success"] is False
    assert "regular file" in result["error"]


def test_present_rejects_path_outside_workspace(deliver_env):
    from tools.builtin.system_tool_present import present

    result = asyncio.run(present([{"path": "C:/Windows/win.ini"}]))
    assert result["success"] is False


def test_present_rejects_empty_list(deliver_env):
    from tools.builtin.system_tool_present import present

    result = asyncio.run(present([]))
    assert result["success"] is False


def test_observed_changes_are_reconstructed_from_history(deliver_env):
    from services.deliverables import list_observed_changes

    conn, _workspace = deliver_env
    calls = [
        {"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "a.py"})}},
        {"id": "c2", "type": "function", "function": {"name": "rename_file", "arguments": json.dumps({"old_path": "a.py", "new_path": "b.py"})}},
        {"id": "c3", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "c.py"})}},
    ]
    asyncio.run(conn.execute(
        "INSERT INTO conversations(id,session_id,role,content,tool_calls_json,created_at) "
        "VALUES(?,?,?,?,?,?)", ("m1", "s1", "assistant", "写文件", json.dumps(calls), 1)))
    asyncio.run(conn.commit())

    changed = asyncio.run(list_observed_changes(conn, "s1"))
    paths = {item["path"] for item in changed}
    assert paths == {"a.py", "b.py"}


def test_deliverables_route_returns_both_sources(deliver_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.sessions import router
    from tools.builtin.system_tool_present import present

    conn, _workspace = deliver_env
    asyncio.run(present([{"path": "out.txt"}]))
    calls = [{"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "a.py"})}}]
    asyncio.run(conn.execute(
        "INSERT INTO conversations(id,session_id,role,content,tool_calls_json,created_at) "
        "VALUES(?,?,?,?,?,?)", ("m1", "s1", "assistant", "写", json.dumps(calls), 1)))
    asyncio.run(conn.commit())

    app = FastAPI()
    app.state.db = conn
    app.include_router(router)
    body = TestClient(app).get("/api/v1/sessions/s1/deliverables").json()
    assert len(body["presented"]) == 1
    assert body["changed"][0]["path"] == "a.py"


def test_deliverables_diff_route(deliver_env):
    """改动文件 diff 路由：已记录路径返回文本 diff，未记录路径 404。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.sessions import router
    from services.snapshot_service import SnapshotService

    conn, workspace = deliver_env
    shared = workspace / "shared"
    shared.mkdir(exist_ok=True)
    target = shared / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    asyncio.run(SnapshotService(db=conn).pre_tool("write_file", {"path": "a.txt"}, "t1"))
    target.write_text("new\n", encoding="utf-8")

    app = FastAPI()
    app.state.db = conn
    app.include_router(router)
    client = TestClient(app)
    body = client.get(
        "/api/v1/sessions/s1/deliverables/diff", params={"path": str(target)}).json()
    assert body["kind"] == "text"
    assert "-old" in body["diff"] and "+new" in body["diff"]
    assert client.get(
        "/api/v1/sessions/s1/deliverables/diff",
        params={"path": str(shared / "other.txt")},
    ).status_code == 404


# ── shell 改动（DSH workspace-changes 语义）─────────────────────────


def test_record_shell_changes_writes_shell_source(deliver_env):
    """shell 改的文件单独记 source='shell'，与 present 声明区分开。"""
    from repositories import list_session_deliverables
    from services.deliverables import record_shell_changes

    conn, workspace = deliver_env
    rows = asyncio.run(record_shell_changes(
        conn, "s1", "t1", ["sub/new.txt", "out.txt"], str(workspace),
    ))
    assert len(rows) == 2
    stored = asyncio.run(list_session_deliverables(conn, "s1"))
    assert {r["source"] for r in stored} == {"shell"}
    assert all(r["rel_path"] for r in stored)


def test_record_shell_changes_is_idempotent_per_path(deliver_env):
    """同一条路径反复检出只留一行（多轮下来不能攒出一堆重复行）。"""
    from repositories import list_session_deliverables
    from services.deliverables import record_shell_changes

    conn, workspace = deliver_env
    asyncio.run(record_shell_changes(conn, "s1", "t1", ["a.txt"], str(workspace)))
    asyncio.run(record_shell_changes(conn, "s1", "t1", ["a.txt"], str(workspace)))
    stored = asyncio.run(list_session_deliverables(conn, "s1"))
    assert len(stored) == 1


def test_deliverables_route_separates_shell_rows(deliver_env):
    """路由把 present 与 shell 分开返回——语义别混。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from repositories import add_session_deliverable
    from routes.sessions import router

    conn, workspace = deliver_env
    asyncio.run(add_session_deliverable(
        conn, "s1", str(workspace / "out.txt"), rel_path="out.txt",
        description="产物", source="present", task_id="t1",
    ))
    asyncio.run(add_session_deliverable(
        conn, "s1", str(workspace / "sub" / "gen.txt"), rel_path="sub/gen.txt",
        source="shell", task_id="t1",
    ))

    app = FastAPI()
    app.state.db = conn
    app.include_router(router)
    body = TestClient(app).get("/api/v1/sessions/s1/deliverables").json()

    assert [r["source"] for r in body["presented"]] == ["present"]
    assert [r["source"] for r in body["shell"]] == ["shell"]
    assert body["shell"][0]["rel_path"] == "sub/gen.txt"
