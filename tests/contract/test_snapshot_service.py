"""快照/回退链路的行为契约（2026-10-21 根因修复后的真实断言）。

修复前这套功能整体失效：路径按工作区根解析（工具入参是 shared/ 相对）→ 永不命中；
task_id 取 arguments["_task_id"]（工具循环从不写）→ 每行落成随机 uuid；hook 只匹配
write_file|run_command → edit/delete/rename 无快照。旧测试只断言「不存在的文件不建
快照」，等于把 bug 写成了契约。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def snap_env(tmp_path, monkeypatch):
    import aiosqlite
    from repositories import SCHEMA_SQL

    workspace = tmp_path / "ws"
    shared = workspace / "shared"
    shared.mkdir(parents=True)
    monkeypatch.setenv("YUNCAT_WORKSPACE", str(workspace))

    conn = await aiosqlite.connect(str(tmp_path / "snap.db"))
    await conn.executescript(SCHEMA_SQL)
    await conn.execute(
        "INSERT INTO tasks(id, input, session_id, status) VALUES(?,?,?,?)",
        ("t1", "改文件", "s1", "pending"),
    )
    await conn.commit()
    yield conn, shared, workspace
    await conn.close()


def test_path_resolution_matches_tool_anchors(snap_env):
    from services.snapshot_service import resolve_tool_paths

    _conn, shared, workspace = snap_env
    expected = str(shared / "a.txt")
    assert resolve_tool_paths("write_file", {"path": "a.txt"}) == [(expected, "file")]
    assert resolve_tool_paths("write_file", {"path": "shared/a.txt"}) == [(expected, "file")]
    assert resolve_tool_paths("run_command", {"command": "echo hi"}) == []
    rename = resolve_tool_paths(
        "rename_file", {"old_path": "a.txt", "new_path": "b.txt"})
    assert [p for p, _ in rename] == [str(shared / "a.txt"), str(shared / "b.txt")]
    assert workspace.exists()


def test_hook_pattern_covers_all_file_tools():
    from services.snapshot_service import snapshot_hook_pattern

    pattern = snapshot_hook_pattern()
    for name in ("write_file", "edit_file", "delete_file", "rename_file",
                 "checkpoint_restore", "run_command"):
        assert name in pattern, name


@pytest.mark.asyncio
async def test_restore_brings_back_previous_content(snap_env):
    from services.snapshot_service import SnapshotService

    conn, shared, _ws = snap_env
    target = shared / "a.txt"
    target.write_text("one\n", encoding="utf-8")
    svc = SnapshotService(db=conn)

    sid = await svc.pre_tool("write_file", {"path": "a.txt"}, "t1")
    assert sid, "相对路径必须命中真实文件并建立快照"
    target.write_text("two\n", encoding="utf-8")
    await svc.post_tool("write_file", {"path": "a.txt"}, {"success": True}, "t1", 0, sid)

    result = await svc.restore_turn("t1")
    assert result["status"] == "ok"
    assert result["restored_files"] == 1
    assert target.read_text(encoding="utf-8") == "one\n"


@pytest.mark.asyncio
async def test_restore_removes_files_created_by_task(snap_env):
    from services.snapshot_service import SnapshotService

    conn, shared, _ws = snap_env
    target = shared / "new.txt"
    svc = SnapshotService(db=conn)
    sid = await svc.pre_tool("write_file", {"path": "new.txt"}, "t1")
    assert sid, "新建文件也要登记 existed=false，否则回退删不掉"
    target.write_text("fresh\n", encoding="utf-8")
    await svc.post_tool("write_file", {"path": "new.txt"}, {"success": True}, "t1", 0, sid)

    result = await svc.restore_turn("t1")
    assert result["removed_new_files"] == 1
    assert not target.exists()


@pytest.mark.asyncio
async def test_restore_uses_earliest_pre_for_repeated_edits(snap_env):
    from services.snapshot_service import SnapshotService

    conn, shared, _ws = snap_env
    target = shared / "a.txt"
    svc = SnapshotService(db=conn)
    target.write_text("v1\n", encoding="utf-8")
    sid1 = await svc.pre_tool("edit_file", {"path": "a.txt"}, "t1")
    target.write_text("v2\n", encoding="utf-8")
    await svc.post_tool("edit_file", {"path": "a.txt"}, {"success": True}, "t1", 0, sid1)
    sid2 = await svc.pre_tool("edit_file", {"path": "a.txt"}, "t1")
    target.write_text("v3\n", encoding="utf-8")
    await svc.post_tool("edit_file", {"path": "a.txt"}, {"success": True}, "t1", 0, sid2)

    await svc.restore_turn("t1")
    assert target.read_text(encoding="utf-8") == "v1\n"


@pytest.mark.asyncio
async def test_restore_deleted_file_and_same_basename(snap_env):
    from services.snapshot_service import SnapshotService

    conn, shared, _ws = snap_env
    (shared / "d1").mkdir()
    (shared / "d2").mkdir()
    one = shared / "d1" / "a.txt"
    two = shared / "d2" / "a.txt"
    one.write_text("AAA\n", encoding="utf-8")
    two.write_text("BBB\n", encoding="utf-8")
    svc = SnapshotService(db=conn)

    sid1 = await svc.pre_tool("delete_file", {"path": "d1/a.txt"}, "t1")
    one.unlink()
    await svc.post_tool("delete_file", {"path": "d1/a.txt"}, {"success": True}, "t1", 0, sid1)
    sid2 = await svc.pre_tool("write_file", {"path": "d2/a.txt"}, "t1")
    two.write_text("CCC\n", encoding="utf-8")
    await svc.post_tool("write_file", {"path": "d2/a.txt"}, {"success": True}, "t1", 0, sid2)

    await svc.restore_turn("t1")
    assert one.read_text(encoding="utf-8") == "AAA\n"
    assert two.read_text(encoding="utf-8") == "BBB\n"


@pytest.mark.asyncio
async def test_restore_refuses_paths_outside_workspace(snap_env, tmp_path):
    import json
    import time

    from services.snapshot_service import SnapshotService, snapshot_dir, snapshot_entry_name

    conn, _shared, _ws = snap_env
    outside = tmp_path / "outside.txt"
    outside.write_text("original\n", encoding="utf-8")
    sid = "malicious"
    backup = snapshot_dir() / sid / snapshot_entry_name(str(outside))
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text("evil\n", encoding="utf-8")
    await conn.execute(
        "INSERT INTO snapshots(id, task_id, turn_seq, phase, tool_name, files_affected, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (sid, "t1", 0, "pre", "write_file",
         json.dumps([{"path": str(outside), "kind": "file", "existed": True}]),
         int(time.time())),
    )
    await conn.commit()

    result = await SnapshotService(db=conn).restore_turn("t1")
    assert result["status"] == "partial"
    assert str(outside) in result["skipped"]
    assert outside.read_text(encoding="utf-8") == "original\n"


@pytest.mark.asyncio
async def test_restore_removes_empty_new_directory_but_keeps_nonempty(snap_env):
    """任务新建的空目录回退时删掉；用户往里放了文件就保守跳过并报 skipped。"""
    from services.snapshot_service import SnapshotService

    conn, shared, _ws = snap_env
    svc = SnapshotService(db=conn)
    await svc.pre_tool("mkdir", {"path": "empty"}, "t1")
    (shared / "empty").mkdir()
    await svc.pre_tool("mkdir", {"path": "kept"}, "t1")
    (shared / "kept").mkdir()
    (shared / "kept" / "user.txt").write_text("keep\n", encoding="utf-8")

    result = await svc.restore_turn("t1")
    assert not (shared / "empty").exists()
    assert (shared / "kept" / "user.txt").read_text(encoding="utf-8") == "keep\n"
    assert result["status"] == "partial"
    assert any("kept" in p for p in result["skipped"])


@pytest.mark.asyncio
async def test_undo_refuses_running_task(snap_env):
    from services.snapshot_service import SnapshotService

    conn, shared, _ws = snap_env
    (shared / "a.txt").write_text("v1\n", encoding="utf-8")
    svc = SnapshotService(db=conn)
    await svc.pre_tool("write_file", {"path": "a.txt"}, "t1")
    await conn.execute("UPDATE tasks SET status='running' WHERE id='t1'")
    await conn.commit()
    result = await svc.restore_turn("t1")
    assert result["status"] == "error"
    assert "运行" in result["message"]


@pytest.mark.asyncio
async def test_undo_skips_file_modified_after_task_end(snap_env):
    """任务结束后被改过的文件不覆盖——撤销只撤任务自己的改动。"""
    import os
    import time

    from services.snapshot_service import SnapshotService

    conn, shared, _ws = snap_env
    target = shared / "a.txt"
    target.write_text("v1\n", encoding="utf-8")
    svc = SnapshotService(db=conn)
    await svc.pre_tool("write_file", {"path": "a.txt"}, "t1")
    target.write_text("task-wrote\n", encoding="utf-8")
    done = time.time() - 60
    os.utime(target, (done - 30, done - 30))
    await conn.execute(
        "UPDATE tasks SET status='completed', completed_at=? WHERE id='t1'", (int(done),))
    await conn.commit()

    # 任务结束后又被改：mtime 晚于完成时间
    target.write_text("user-edited\n", encoding="utf-8")
    result = await svc.restore_turn("t1")
    assert result["status"] == "partial"
    assert str(target) in result["skipped"]
    assert target.read_text(encoding="utf-8") == "user-edited\n"


@pytest.mark.asyncio
async def test_changed_files_with_stats(snap_env):
    from services.deliverables import list_changed_files_with_stats
    from services.snapshot_service import SnapshotService

    conn, shared, _ws = snap_env
    target = shared / "a.txt"
    target.write_text("keep\nold\n", encoding="utf-8")
    svc = SnapshotService(db=conn)
    sid = await svc.pre_tool("write_file", {"path": "a.txt"}, "t1")
    target.write_text("keep\nnew\nextra\n", encoding="utf-8")
    await svc.post_tool("write_file", {"path": "a.txt"}, {"success": True}, "t1", 0, sid)

    created = shared / "created.txt"
    sid2 = await svc.pre_tool("write_file", {"path": "created.txt"}, "t1")
    created.write_text("a\nb\n", encoding="utf-8")
    await svc.post_tool("write_file", {"path": "created.txt"}, {"success": True}, "t1", 0, sid2)

    changes = await list_changed_files_with_stats(conn, "s1")
    by_name = {row["display"].split("/")[-1]: row for row in changes}
    assert by_name["a.txt"]["added"] == 2 and by_name["a.txt"]["deleted"] == 1
    assert by_name["created.txt"]["added"] == 2 and by_name["created.txt"]["deleted"] == 0
    # 绝对 path 给 diff 端点，rel_path 给工作区打开文件（斜杠统一）
    assert by_name["a.txt"]["path"].endswith("a.txt")
    assert by_name["a.txt"]["rel_path"] == "shared/a.txt"


@pytest.mark.asyncio
async def test_file_diff_text_new_and_deleted(snap_env):
    from services.deliverables import file_diff
    from services.snapshot_service import SnapshotService

    conn, shared, _ws = snap_env
    svc = SnapshotService(db=conn)

    target = shared / "a.txt"
    target.write_text("keep\nold\n", encoding="utf-8")
    await svc.pre_tool("write_file", {"path": "a.txt"}, "t1")
    target.write_text("keep\nnew\n", encoding="utf-8")
    diff = await file_diff(conn, "s1", str(target))
    assert diff["kind"] == "text"
    assert "-old" in diff["diff"] and "+new" in diff["diff"]
    assert diff["added"] == 1 and diff["deleted"] == 1
    assert diff["before"] is True and diff["after"] is True

    created = shared / "new.txt"
    await svc.pre_tool("write_file", {"path": "new.txt"}, "t1")
    created.write_text("a\nb\n", encoding="utf-8")
    new_diff = await file_diff(conn, "s1", str(created))
    assert new_diff["before"] is False and new_diff["after"] is True
    assert new_diff["added"] == 2 and new_diff["deleted"] == 0

    gone = shared / "gone.txt"
    gone.write_text("bye\n", encoding="utf-8")
    await svc.pre_tool("delete_file", {"path": "gone.txt"}, "t1")
    gone.unlink()
    deleted_diff = await file_diff(conn, "s1", str(gone))
    assert deleted_diff["after"] is False
    assert deleted_diff["deleted"] == 1 and deleted_diff["added"] == 0


@pytest.mark.asyncio
async def test_file_diff_rejects_unrecorded_path(snap_env):
    from services.deliverables import file_diff

    conn, shared, _ws = snap_env
    secret = shared / "not-recorded.txt"
    secret.write_text("secret\n", encoding="utf-8")
    result = await file_diff(conn, "s1", str(secret))
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_file_diff_marks_binary(snap_env):
    from services.deliverables import file_diff
    from services.snapshot_service import SnapshotService

    conn, shared, _ws = snap_env
    target = shared / "bin.dat"
    target.write_bytes(b"PK\x00\x01")
    svc = SnapshotService(db=conn)
    await svc.pre_tool("write_file", {"path": "bin.dat"}, "t1")
    target.write_bytes(b"PK\x00\x02")
    result = await file_diff(conn, "s1", str(target))
    assert result["kind"] == "binary"
    assert result["diff"] == ""


def test_unified_diff_text_truncates():
    from services.deliverables import unified_diff_text

    old = "".join(f"old{i}\n" for i in range(20))
    new = "".join(f"new{i}\n" for i in range(20))
    text, truncated = unified_diff_text("a.txt", old, new, 8)
    assert truncated is True
    assert "已截断" in text


@pytest.mark.asyncio
async def test_list_snapshots_normalizes_entries(snap_env):
    from services.snapshot_service import SnapshotService

    conn, shared, _ws = snap_env
    (shared / "a.txt").write_text("x\n", encoding="utf-8")
    svc = SnapshotService(db=conn)
    await svc.pre_tool("write_file", {"path": "a.txt"}, "t1")
    rows = await svc.list_snapshots("t1")
    assert len(rows) == 1
    assert rows[0]["files_affected"][0]["existed"] is True
    assert rows[0]["files_affected"][0]["path"].endswith("a.txt")


@pytest.mark.asyncio
async def test_tool_service_execute_records_snapshot_with_real_task_id(snap_env):
    """真链路回归：ToolService.execute(write_file) → 快照行带真实 task_id。

    这里同时守住两个坑：① task_id 必须经 contextvar 传播（旧实现落随机 uuid）；
    ② 快照 hook 写进 arguments 的 `_snapshot_pre_id` 不能混进工具函数签名，
    否则 write_file 会因 unexpected keyword 直接 TypeError。
    """
    import importlib
    import pkgutil
    import types

    import tools.builtin as _builtin
    from config import YUNCAT_AGENT_ID
    from context import set_app
    from hooks import registry as hooks_registry
    from services.snapshot_service import SnapshotService
    from services.tools.tool_service import ToolService

    for module in pkgutil.iter_modules(_builtin.__path__):
        importlib.import_module("tools.builtin." + module.name)

    conn, shared, _ws = snap_env
    (shared / "e2e.txt").write_text("before\n", encoding="utf-8")
    hooks_registry.set_snapshot_service(SnapshotService(db=conn))
    app = types.SimpleNamespace(state=types.SimpleNamespace(db=conn))
    set_app(app)
    try:
        result = await ToolService().execute(
            "write_file", {"path": "e2e.txt", "content": "after\n"},
            agent_id=YUNCAT_AGENT_ID, task_id="t1",
        )
    finally:
        set_app(None)
        hooks_registry.set_snapshot_service(None)

    assert result.get("success") is True, result
    cursor = await conn.execute(
        "SELECT task_id, phase, files_affected FROM snapshots WHERE task_id='t1'")
    rows = await cursor.fetchall()
    assert rows, "工具执行必须留下快照行"
    assert all(row[0] == "t1" for row in rows)
    assert all(row[1] == "pre" for row in rows)


@pytest.mark.asyncio
async def test_auto_approve_path_keeps_task_identity(snap_env):
    """自动批准路径（execute_confirmable）也必须把 task_id 透传下去。"""
    import importlib
    import pkgutil
    import types

    import tools.builtin as _builtin
    from config import YUNCAT_AGENT_ID
    from context import set_app
    from hooks import registry as hooks_registry
    from services.snapshot_service import SnapshotService
    from services.tools.tool_service import ToolService

    for module in pkgutil.iter_modules(_builtin.__path__):
        importlib.import_module("tools.builtin." + module.name)

    conn, shared, _ws = snap_env
    hooks_registry.set_snapshot_service(SnapshotService(db=conn))
    set_app(types.SimpleNamespace(state=types.SimpleNamespace(db=conn)))
    try:
        result = await ToolService().execute_confirmable(
            conn, "write_file", {"path": "auto.txt", "content": "x\n"},
            agent_id=YUNCAT_AGENT_ID, auto_approve=True, task_id="t1",
        )
    finally:
        set_app(None)
        hooks_registry.set_snapshot_service(None)

    assert result.get("success") is True, result
    assert (shared / "auto.txt").read_text(encoding="utf-8") == "x\n"
    cursor = await conn.execute("SELECT COUNT(*) FROM snapshots WHERE task_id='t1'")
    assert (await cursor.fetchone())[0] >= 1


@pytest.mark.asyncio
async def test_harness_recorder_disabled_by_default(tmp_path, monkeypatch):
    import aiosqlite
    from repositories import SCHEMA_SQL
    from services.snapshot_service import HarnessRecorder

    monkeypatch.delenv("YUNCAT_HARNESS_RECORD", raising=False)
    conn = await aiosqlite.connect(str(tmp_path / "rec.db"))
    await conn.executescript(SCHEMA_SQL)
    recorder = HarnessRecorder(db=conn)
    assert recorder.enabled is False
    assert await recorder.record_turn("task-1", {"tool": "test"}) is None
    await conn.close()
