"""tests/unit/test_llm_request_log.py — 模型请求/尝试日志（借鉴 dsh request/header）

覆盖：
- 摘要字段完整、prompt 哈希稳定、不泄露 api_key/正文；
- request + attempt 落 analytics_events，按任务可查；
- stream 成功 / stream_error / 抛异常 三种 attempt 状态；
- 工具名上限。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src-python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from services import llm_request_log as L  # noqa: E402


DDL = """
CREATE TABLE analytics_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  event_data TEXT NOT NULL,
  created_at INTEGER DEFAULT (strftime('%s','now'))
)
"""


class _FakeService:
    def __init__(self, db, adapters=None):
        self.db = db
        self.adapters = adapters or {}


@pytest.fixture
def db(tmp_path):
    import aiosqlite

    async def _make():
        conn = await aiosqlite.connect(str(tmp_path / "llm.db"))
        await conn.execute(DDL)
        await conn.commit()
        return conn

    conn = asyncio.run(_make())
    yield conn
    asyncio.run(conn.close())


def _ctx(**extra):
    base = {
        "system": "SYS",
        "memory": "MEM",
        "history": [{"role": "user", "content": "hi"}],
        "model_config": {"provider": "deepseek", "model": "deepseek-v4-pro", "api_key": "sk-secret"},
        "tools": [{"function": {"name": "read_file"}}, {"function": {"name": "run_command"}}],
        "mode": "agent",
        "capability_level": 2,
        "reasoning_effort": "high",
        "session_id": "sess-1",
    }
    base.update(extra)
    return base


def test_summarize_has_no_secret_or_body():
    payload = L.summarize_request("t1", "yuncat", _ctx(), "PROMPT-BODY")
    raw = json.dumps(payload, ensure_ascii=False)
    assert "sk-secret" not in raw
    assert "PROMPT-BODY" not in raw
    assert payload["tool_count"] == 2
    assert payload["tool_names"] == ["read_file", "run_command"]
    assert payload["provider"] == "deepseek"
    assert payload["model"] == "deepseek-v4-pro"
    assert payload["prompt_chars"] == len("PROMPT-BODY")
    assert len(payload["prompt_hash"]) == 16


def test_prompt_hash_is_stable_and_sensitive():
    a = L.summarize_request("t1", "yuncat", _ctx(), "A")["prompt_hash"]
    b = L.summarize_request("t1", "yuncat", _ctx(), "A")["prompt_hash"]
    c = L.summarize_request("t1", "yuncat", _ctx(), "B")["prompt_hash"]
    assert a == b
    assert a != c


def test_tool_names_are_capped():
    tools = [{"function": {"name": f"t{i}"}} for i in range(L._MAX_TOOL_NAMES + 20)]
    payload = L.summarize_request("t1", "yuncat", _ctx(tools=tools), "x")
    assert payload["tool_count"] == L._MAX_TOOL_NAMES + 20
    assert len(payload["tool_names"]) == L._MAX_TOOL_NAMES


def test_request_and_attempt_roundtrip(db):
    svc = _FakeService(db)

    async def run():
        rid = await L.log_request(svc, "task-1", "yuncat", _ctx(), "p")
        assert rid
        await L.log_attempt(
            svc, rid, "task-1", "yuncat", _ctx(),
            status="ok", chunks=3, duration_ms=120,
            usage={"total_tokens": 42},
        )
        return await L.list_llm_requests(db, "task-1")

    items = asyncio.run(run())
    assert [i["event"] for i in items] == ["llm_attempt", "llm_request"]
    assert items[0]["data"]["status"] == "ok"
    assert items[0]["data"]["usage"]["total_tokens"] == 42
    assert items[1]["data"]["provider"] == "deepseek"


def test_list_is_scoped_to_task(db):
    svc = _FakeService(db)

    async def run():
        await L.log_request(svc, "task-a", "yuncat", _ctx(), "a")
        await L.log_request(svc, "task-b", "yuncat", _ctx(), "b")
        return await L.list_llm_requests(db, "task-a")

    items = asyncio.run(run())
    assert len(items) == 1
    assert items[0]["data"]["task_id"] == "task-a"


def test_logged_stream_success(db, monkeypatch):
    svc = _FakeService(db)

    async def fake_stream(*args, **kwargs):
        yield {"type": "text", "data": "hello"}

    monkeypatch.setattr("services.agent.agent_tool_helpers.stream_with_fallback", fake_stream)

    async def run():
        out = []
        async for chunk in L.logged_stream_with_fallback(svc, "yuncat", "p", _ctx(), "deepseek", "task-s"):
            out.append(chunk)
        return out, await L.list_llm_requests(db, "task-s")

    out, items = asyncio.run(run())
    assert out == [{"type": "text", "data": "hello"}]
    attempt = [i for i in items if i["event"] == "llm_attempt"][0]
    assert attempt["data"]["status"] == "ok"
    assert attempt["data"]["chunks"] == 1
    # 首个 text chunk 到达即记录 TTFT（毫秒，允许为 0）
    assert isinstance(attempt["data"]["ttft_ms"], int)
    assert attempt["data"]["ttft_ms"] >= 0


def test_logged_stream_error_chunk_marks_error(db, monkeypatch):
    svc = _FakeService(db)

    async def fake_stream(*args, **kwargs):
        yield {"type": "stream_error", "data": "boom"}

    monkeypatch.setattr("services.agent.agent_tool_helpers.stream_with_fallback", fake_stream)

    async def run():
        async for _ in L.logged_stream_with_fallback(svc, "yuncat", "p", _ctx(), "deepseek", "task-e"):
            pass
        return await L.list_llm_requests(db, "task-e")

    items = asyncio.run(run())
    attempt = [i for i in items if i["event"] == "llm_attempt"][0]
    assert attempt["data"]["status"] == "error"
    assert "boom" in attempt["data"]["error"]


def test_logged_stream_exception_still_logs_and_reraises(db, monkeypatch):
    svc = _FakeService(db)

    async def fake_stream(*args, **kwargs):
        raise RuntimeError("kaboom")
        yield  # pragma: no cover

    monkeypatch.setattr("services.agent.agent_tool_helpers.stream_with_fallback", fake_stream)

    async def run():
        with pytest.raises(RuntimeError):
            async for _ in L.logged_stream_with_fallback(svc, "yuncat", "p", _ctx(), "deepseek", "task-x"):
                pass
        return await L.list_llm_requests(db, "task-x")

    items = asyncio.run(run())
    attempt = [i for i in items if i["event"] == "llm_attempt"][0]
    assert attempt["data"]["status"] == "error"
    assert "kaboom" in attempt["data"]["error"]
