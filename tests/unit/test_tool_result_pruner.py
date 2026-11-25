"""tool 结果裁剪（DSH compaction-tool-result-pruner）。"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from services.memory.tool_result_pruner import (  # noqa: E402
    estimate_saved_tokens,
    persist_pruned_tool_results,
    prune_tool_results,
)


def test_small_tool_results_are_untouched():
    msgs = [{"id": "m1", "role": "tool", "content": "短输出"}]
    pruned, saved, originals = prune_tool_results(msgs)
    assert pruned == msgs
    assert saved == 0
    assert originals == {}


def test_big_tool_result_is_head_plus_tail():
    content = "HEAD" + "x" * 9000 + "TAIL"
    msgs = [{"id": "m1", "role": "tool", "content": content}]
    pruned, saved, originals = prune_tool_results(
        msgs, threshold_chars=1000, head_chars=100, tail_chars=50)
    new_content = pruned[0]["content"]
    assert new_content.startswith("HEAD")
    assert new_content.endswith("TAIL")
    assert "tool output pruned" in new_content
    assert saved == len(content) - len(new_content)
    assert originals["m1"] == content
    assert pruned[0]["pruned_from_chars"] == len(content)


def test_non_tool_messages_are_not_pruned():
    msgs = [{"id": "m1", "role": "user", "content": "x" * 9000}]
    pruned, saved, _ = prune_tool_results(msgs, threshold_chars=100)
    assert pruned[0]["content"] == "x" * 9000
    assert saved == 0


def test_saved_tokens_estimate_is_positive():
    assert estimate_saved_tokens(4000) == 1000
    assert estimate_saved_tokens(0) == 0


@pytest.mark.asyncio
async def test_persist_prunes_db_and_keeps_original_in_log(tmp_path):
    import aiosqlite
    from repositories import SCHEMA_SQL

    conn = await aiosqlite.connect(str(tmp_path / "prune.db"))
    try:
        await conn.executescript(SCHEMA_SQL)
        original = "A" * 5000
        await conn.execute(
            "INSERT INTO conversations(id,session_id,role,content,created_at) VALUES(?,?,?,?,?)",
            ("m1", "s1", "tool", original, 1))
        await conn.commit()
        msgs = [{"id": "m1", "role": "tool", "content": original}]
        pruned, saved, originals = prune_tool_results(
            msgs, threshold_chars=1000, head_chars=100, tail_chars=50)
        written = await persist_pruned_tool_results(conn, "s1", pruned, originals)
        assert written == 1
        cur = await conn.execute("SELECT content FROM conversations WHERE id='m1'")
        assert "tool output pruned" in (await cur.fetchone())[0]
        cur = await conn.execute(
            "SELECT content, kind FROM compaction_artifacts WHERE kind='tool_pruned'")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == original
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_compaction_prunes_tool_results_and_skips_summarization():
    """压力被裁剪解决时就不该再花钱做摘要。"""
    from services.memory.memory_service import MemoryService

    svc = MemoryService(db=None, summarizer=None, memory_repo=None)
    big = "x" * 100000
    svc._sessions["s1"] = (
        [{"role": "user", "content": "hi"}] * 19
        + [{"id": "m1", "role": "tool", "content": big}]
    )
    result = await svc.maybe_compact_context("s1", 99000, 100000)
    assert result is None
    tool_msg = [m for m in svc._sessions["s1"] if m.get("role") == "tool"][0]
    assert "tool output pruned" in tool_msg["content"]
    assert len(tool_msg["content"]) < len(big)
