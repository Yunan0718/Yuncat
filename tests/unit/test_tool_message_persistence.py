"""tests/unit/test_tool_message_persistence.py — 工具结果写穿透与历史重建

背景：Yuncat 此前只把 assistant 的 tool_calls 落库，工具结果只进内存 history。
重启后 assistant(tool_calls) 没有配对的 tool 消息，恢复出的 OpenAI 请求不合法。

覆盖：
- assistant(tool_calls) + tool(tool_call_id) 往返；
- get_session_context 还原两种角色；
- build_openai_messages 产出合法的 call/result 序列；
- persist_tool_results 逐条写库。
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src-python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _make_service(conn):
    from services.memory.memory_service import MemoryService

    svc = MemoryService.__new__(MemoryService)
    svc.db = conn
    svc._sessions, svc._session_locks, svc._session_last_access = {}, {}, {}
    svc._global_session_lock, svc._evict_lock = asyncio.Lock(), asyncio.Lock()
    svc._pending_writes, svc._max_turns, svc._session_idle_ttl = 0, 50, 3600
    svc._max_active_sessions = 100
    svc._summarizer = svc._memory_repo = svc._calc_max_turns_fn = None
    return svc


@pytest.fixture
def conn():
    import aiosqlite
    from repositories import SCHEMA_SQL

    async def _make():
        c = await aiosqlite.connect(os.path.join(tempfile.mkdtemp(), "conv.db"))
        await c.executescript(SCHEMA_SQL)
        await c.commit()
        return c

    c = asyncio.run(_make())
    yield c
    asyncio.run(c.close())


def test_tool_call_and_result_roundtrip(conn):
    from services.memory.memory_sessions import add_message, get_session_context

    async def run():
        svc = _make_service(conn)
        await add_message(
            svc, "s1", "assistant", "calling",
            agent_id="yuncat",
            tool_calls=[{"id": "call_1", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}],
        )
        await add_message(
            svc, "s1", "tool", '{"success": true}',
            agent_id="yuncat", tool_call_id="call_1",
        )
        return await get_session_context(svc, "s1")

    msgs = asyncio.run(run())
    assert [m["role"] for m in msgs] == ["assistant", "tool"]
    assert msgs[0]["tool_calls"][0]["id"] == "call_1"
    assert msgs[1]["tool_call_id"] == "call_1"
    assert msgs[1]["content"] == '{"success": true}'


def test_restored_history_is_valid_openai_sequence(conn):
    from adapters.base import build_openai_messages
    from services.memory.memory_sessions import add_message, get_session_context

    async def run():
        svc = _make_service(conn)
        await add_message(
            svc, "s1", "assistant", "calling",
            tool_calls=[{"id": "call_1", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}],
        )
        await add_message(svc, "s1", "tool", "result", tool_call_id="call_1")
        return await get_session_context(svc, "s1")

    msgs = asyncio.run(run())
    wire = build_openai_messages(None, {"history": msgs})
    assert wire[0]["role"] == "assistant"
    assert wire[0]["tool_calls"][0]["id"] == "call_1"
    assert wire[1]["role"] == "tool"
    assert wire[1]["tool_call_id"] == "call_1"


def test_persist_tool_results_writes_each_result(conn):
    from services.agent.tool_result_persist import persist_tool_results

    svc = _make_service(conn)

    async def run():
        written = await persist_tool_results(
            svc, "s1", "yuncat",
            [
                {"role": "tool", "tool_call_id": "c1", "content": "r1"},
                {"role": "tool", "tool_call_id": "c2", "content": "r2"},
            ],
        )
        return written, await conn.execute(
            "SELECT role, content, tool_calls_json FROM conversations ORDER BY created_at"
        )

    written, cursor = asyncio.run(run())
    rows = asyncio.run(cursor.fetchall())
    assert written == 2
    assert [r[0] for r in rows] == ["tool", "tool"]
    assert [r[1] for r in rows] == ["r1", "r2"]
    assert "c1" in rows[0][2] and "c2" in rows[1][2]


def test_codec_rejects_malformed_without_crashing():
    from services.memory.tool_message_codec import decode_tool_meta

    assert decode_tool_meta("not-json") == (None, None)
    assert decode_tool_meta("{}") == (None, None)
    assert decode_tool_meta(None) == (None, None)
