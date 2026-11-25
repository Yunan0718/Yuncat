"""Harness 录制：工具执行后真的会落盘，且只录非私有参数。

背景：`record_turn` 此前**从未被调用**，录制表永远是空的——
三个记录回放端点看起来有功能，实际没有数据。
"""
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def rec_db(tmp_path):
    import aiosqlite
    from repositories import SCHEMA_SQL

    conn = await aiosqlite.connect(str(tmp_path / "rec.db"))
    await conn.executescript(SCHEMA_SQL)
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_record_harness_turn_writes_row(rec_db, monkeypatch):
    import context
    from services.snapshot_service import HarnessRecorder
    from services.tools.tool_service import _record_harness_turn

    monkeypatch.setenv("YUNCAT_HARNESS_RECORD", "1")
    recorder = HarnessRecorder(db=rec_db)
    monkeypatch.setattr(context, "get_app", lambda: SimpleNamespace(
        state=SimpleNamespace(harness_recorder=recorder)))

    await _record_harness_turn(
        "task-1", "read_file",
        {"path": "a.txt", "_task_id": "should-be-stripped"},
        {"success": True, "data": {"content": "x"}},
    )

    rows = await recorder.list_recordings(10)
    assert len(rows) == 1
    detail = await recorder.get_recording(rows[0]["id"])
    assert detail["task_id"] == "task-1"
    assert detail["turn_data"]["tool_name"] == "read_file"
    # 下划线私有键不进录制（回放时会当参数传给工具 -> TypeError）
    assert "_task_id" not in detail["turn_data"]["arguments"]
    assert detail["turn_data"]["arguments"]["path"] == "a.txt"


@pytest.mark.asyncio
async def test_recording_disabled_writes_nothing(rec_db, monkeypatch):
    import context
    from services.snapshot_service import HarnessRecorder
    from services.tools.tool_service import _record_harness_turn

    monkeypatch.delenv("YUNCAT_HARNESS_RECORD", raising=False)
    recorder = HarnessRecorder(db=rec_db)
    assert recorder.enabled is False
    monkeypatch.setattr(context, "get_app", lambda: SimpleNamespace(
        state=SimpleNamespace(harness_recorder=recorder)))

    await _record_harness_turn("task-2", "read_file", {"path": "a.txt"}, {"success": True})
    assert await recorder.list_recordings(10) == []


@pytest.mark.asyncio
async def test_record_failure_does_not_raise(monkeypatch):
    """录制失败只是诊断损失，不能影响工具结果。"""
    import context
    from services.tools.tool_service import _record_harness_turn

    class _Boom:
        async def record_turn(self, *_a, **_k):
            raise RuntimeError("disk full")

    monkeypatch.setattr(context, "get_app", lambda: SimpleNamespace(
        state=SimpleNamespace(harness_recorder=_Boom())))
    # 不抛异常即通过
    await _record_harness_turn("task-3", "read_file", {}, {})
