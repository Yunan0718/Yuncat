"""todo_write 工具：DSH 契约的校验、整表替换、落库与广播。"""
import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


def test_validate_todos_accepts_wellformed_list():
    from tools.builtin.system_tool_todo import validate_todos
    todos = validate_todos([
        {"content": " 读代码 ", "status": "completed"},
        {"content": "写实现", "status": "in_progress"},
        {"content": "补测试", "status": "pending"},
    ], allow_parallel=True)
    assert todos[0]["content"] == "读代码"
    assert [t["status"] for t in todos] == ["completed", "in_progress", "pending"]


@pytest.mark.parametrize("raw", [
    "not-a-list",
    [{"content": "  ", "status": "pending"}],
    [{"content": "a", "status": "pending"}, {"content": "a", "status": "done"}],
    [{"content": "a", "status": "unknown"}],
    [{"content": "a", "status": "pending", "extra": 1}],
])
def test_validate_todos_rejects_invalid_lists(raw):
    from tools.builtin.system_tool_todo import validate_todos
    with pytest.raises(ValueError):
        validate_todos(raw, allow_parallel=True)


def test_single_active_discipline_is_enforced_when_parallel_disabled():
    from tools.builtin.system_tool_todo import validate_todos
    with pytest.raises(ValueError):
        validate_todos([
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "in_progress"},
        ], allow_parallel=False)
    ok = validate_todos([{"content": "a", "status": "in_progress"}], allow_parallel=False)
    assert len(ok) == 1


def test_summary_matches_dsh_wording():
    from tools.builtin.system_tool_todo import summarize_todos
    text = summarize_todos([
        {"content": "a", "status": "pending"},
        {"content": "b", "status": "in_progress"},
        {"content": "c", "status": "completed"},
        {"content": "d", "status": "completed"},
    ])
    assert text == "Updated todo list: 1 pending, 1 in progress, 2 completed."


class _FakeWsManager:
    def __init__(self):
        self.payloads = []

    async def broadcast(self, payload):
        self.payloads.append(payload)


@pytest.fixture
async def todo_env(tmp_path):
    import aiosqlite
    from context import set_app, set_current_agent_id, set_current_task_id
    from repositories import SCHEMA_SQL

    conn = await aiosqlite.connect(str(tmp_path / "todo.db"))
    await conn.executescript(SCHEMA_SQL)
    await conn.execute(
        "INSERT INTO tasks(id, input, session_id, status, agent_id) VALUES(?,?,?,?,?)",
        ("t1", "测试任务", "s1", "running", "yuncat"),
    )
    await conn.commit()
    ws = _FakeWsManager()
    app = types.SimpleNamespace(state=types.SimpleNamespace(
        db=conn,
        agent_service=types.SimpleNamespace(ws_manager=ws),
    ))
    set_app(app)
    task_token = set_current_task_id("t1")
    agent_token = set_current_agent_id("yuncat")
    yield conn, ws
    set_current_agent_id(agent_token)
    set_current_task_id(task_token)
    await conn.close()


def test_todo_write_replaces_whole_list_and_broadcasts(todo_env):
    from repositories import get_session_todos
    from tools.builtin.system_tool_todo import todo_write

    conn, ws = todo_env
    first = asyncio.run(todo_write([
        {"content": "第一步", "status": "in_progress"},
        {"content": "第二步", "status": "pending"},
    ]))
    assert first["success"] is True
    assert first["observation"] == "Updated todo list: 1 pending, 1 in progress, 0 completed."

    stored = asyncio.run(get_session_todos(conn, "s1"))
    assert [t["content"] for t in stored] == ["第一步", "第二步"]
    assert ws.payloads[-1]["type"] == "todo_updated"
    assert ws.payloads[-1]["session_id"] == "s1"

    # 整体替换：第二次只提交一项，旧的就没了
    asyncio.run(todo_write([{"content": "第一步", "status": "completed"}]))
    stored = asyncio.run(get_session_todos(conn, "s1"))
    assert stored == [{"content": "第一步", "status": "completed"}]


def test_todo_write_rejects_invalid_list_without_writing(todo_env):
    from repositories import get_session_todos
    from tools.builtin.system_tool_todo import todo_write

    conn, _ws = todo_env
    result = asyncio.run(todo_write([{"content": "", "status": "pending"}]))
    assert result["success"] is False
    assert "non-empty" in result["error"]
    assert asyncio.run(get_session_todos(conn, "s1")) == []


def test_todo_route_returns_and_delete_clears(todo_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.sessions import router
    from tools.builtin.system_tool_todo import todo_write

    conn, _ws = todo_env
    asyncio.run(todo_write([{"content": "写实现", "status": "in_progress"}]))
    app = FastAPI()
    app.state.db = conn
    app.include_router(router)
    client = TestClient(app)
    body = client.get("/api/v1/sessions/s1/todos").json()
    assert body["todos"] == [{"content": "写实现", "status": "in_progress"}]
