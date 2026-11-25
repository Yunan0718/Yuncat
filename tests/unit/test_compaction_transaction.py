"""压缩事务：并发追加保留、冲突回退、checkpoint 持久化与 facade 参数。"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from services.memory.compaction_txn import capture_snapshot  # noqa: E402


def _msg(message_id: str, content: str, role: str = "user") -> dict:
    return {"id": message_id, "role": role, "content": content}


def test_snapshot_tracks_source_ids_and_preserves_appended_messages():
    original = [_msg("m1", "one"), _msg("m2", "two"), _msg("m3", "three")]
    snapshot = capture_snapshot(original, 2)
    current = [*original, _msg("m4", "arrived during summary")]

    assert snapshot.unchanged_prefix(current)
    assert snapshot.source_message_ids == ("m1", "m2")
    assert current[len(snapshot.signatures):] == [_msg("m4", "arrived during summary")]


def test_snapshot_rejects_mutation_or_removal():
    original = [_msg("m1", "one"), _msg("m2", "two")]
    snapshot = capture_snapshot(original, 1)
    assert not snapshot.unchanged_prefix([_msg("m1", "rewritten"), _msg("m2", "two")])
    assert not snapshot.unchanged_prefix([_msg("m1", "one")])


def test_memory_service_facade_keeps_compaction_parameters(svc_factory):
    svc = svc_factory()
    assert "system_prompt_tokens" in inspect.signature(svc.maybe_compact_context).parameters
    assert "system_prompt_tokens" in inspect.signature(svc._split_messages).parameters
    assert "model_config" in inspect.signature(svc._build_structured_summary).parameters


@pytest.mark.asyncio
async def test_facade_forwards_compaction_parameters(svc_factory, monkeypatch):
    import services.memory.memory_compact as compact

    svc = svc_factory()
    captured = {}

    async def fake_auto(service, session_id, current_tokens, max_tokens=None, **kwargs):
        captured.update(kwargs)
        return None

    def fake_split(service, conv, model_config=None, system_prompt_tokens=0):
        captured["split_system_tokens"] = system_prompt_tokens
        return [], conv

    monkeypatch.setattr(compact, "maybe_compact_context", fake_auto)
    monkeypatch.setattr(compact, "_split_messages", fake_split)
    await svc.maybe_compact_context("s", 10, 20, system_prompt_tokens=7)
    svc._split_messages([], system_prompt_tokens=9)
    assert captured == {"system_prompt_tokens": 7, "split_system_tokens": 9}


@pytest.mark.asyncio
async def test_apply_transaction_preserves_message_appended_during_summary(svc_factory):
    from services.memory.compaction_txn import apply_compaction_transaction

    svc = svc_factory()
    original = [_msg("m1", "one"), _msg("m2", "two"), _msg("m3", "three")]
    svc._sessions["s1"] = [*original]
    snapshot = capture_snapshot(original, 2)
    svc._sessions["s1"].append(_msg("m4", "arrived during summary"))

    applied = await apply_compaction_transaction(
        svc, "s1", snapshot, {"session_intent": "x"}, "summary", original[2:],
    )
    assert applied is True
    assert [message.get("content") for message in svc._sessions["s1"]] == [
        "summary", "three", "arrived during summary",
    ]


@pytest.fixture
def svc_factory():
    def make():
        from services.memory.memory_service import MemoryService
        return MemoryService(db=None)
    return make


@pytest.mark.asyncio
async def test_checkpoint_survives_reload_and_filters_source_messages(tmp_path):
    import aiosqlite
    from repositories import SCHEMA_SQL
    from repositories.compaction_repo import get_compaction_checkpoint
    from services.memory.memory_service import MemoryService

    conn = await aiosqlite.connect(str(tmp_path / "compact.db"))
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    svc = MemoryService(conn)
    for index in range(5):
        await svc.add_message("s1", "user", f"message-{index}")
    conv = await svc.get_session_context("s1")
    snapshot = capture_snapshot(conv, 3)
    recent = conv[3:]
    summary = {"session_intent": "保留压缩事务"}

    applied = await svc._apply_compacted_context("s1", summary, recent, snapshot)
    assert applied is True
    checkpoint = await get_compaction_checkpoint(conn, "s1")
    assert checkpoint["source_message_ids"] == list(snapshot.source_message_ids)

    after = await svc.add_message("s1", "user", "message-after-compaction")
    svc._sessions.pop("s1", None)
    reloaded = await svc.get_session_context("s1")
    ids = [message.get("id") for message in reloaded]
    assert ids[0].startswith("compact:")
    assert after["id"] in ids
    assert not set(snapshot.source_message_ids).intersection(ids)
    assert "保留压缩事务" in reloaded[0]["content"]
    await conn.close()


@pytest.mark.asyncio
async def test_commit_failure_keeps_original_session(monkeypatch, tmp_path):
    import aiosqlite
    from repositories import SCHEMA_SQL
    from services.memory import compaction_txn
    from services.memory.memory_service import MemoryService

    conn = await aiosqlite.connect(str(tmp_path / "compact-fail.db"))
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    svc = MemoryService(conn)
    for index in range(4):
        await svc.add_message("s1", "user", f"message-{index}")
    original = await svc.get_session_context("s1")
    snapshot = capture_snapshot(original, 2)

    async def fail_commit(*_args, **_kwargs):
        raise RuntimeError("simulated checkpoint failure")

    monkeypatch.setattr(
        "repositories.compaction_repo.commit_compaction_checkpoint", fail_commit,
    )
    with pytest.raises(RuntimeError):
        await compaction_txn.apply_compaction_transaction(
            svc, "s1", snapshot, {"session_intent": "x"}, "summary", original[2:],
        )
    assert [message["id"] for message in svc._sessions["s1"]] == [
        message["id"] for message in original
    ]
    await conn.close()
