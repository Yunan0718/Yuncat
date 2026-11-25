"""shell 命令改动的文件（DSH workspace-changes 语义）——检测、去重与落库。

背景：交付物面板原本只列"工具层改过的文件"（快照口径），
shell 命令改的文件（拉依赖、生成产物、git checkout）完全看不见。
本模块用 git 脏文件集合的前后差集把这一半补上。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from services import shell_changes


@pytest.fixture(autouse=True)
def _clean_baselines():
    shell_changes._BASELINES.clear()
    yield
    shell_changes._BASELINES.clear()


# ── 解析 ──────────────────────────────────────────────────────────

def test_parse_porcelain_reads_status_entries():
    text = " M src/a.py\x00?? new.txt\x00"
    assert shell_changes._parse_porcelain(text) == {"src/a.py", "new.txt"}


def test_parse_porcelain_skips_rename_source():
    """重命名条目会多一个源路径（NUL 分隔），不能把它当成改动文件。"""
    text = "R  new.py\x00old.py\x00 M keep.py\x00"
    assert shell_changes._parse_porcelain(text) == {"new.py", "keep.py"}


def test_parse_porcelain_handles_spaces_and_unicode():
    text = "?? 我的 文件.txt\x00"
    assert shell_changes._parse_porcelain(text) == {"我的 文件.txt"}


# ── 检测 ──────────────────────────────────────────────────────────

def _fake_git(monkeypatch, dirty_sets, root="D:/repo"):
    """把 _run_git 换成脚本：先回仓库根，再按调用次数回脏文件集合。"""
    calls = {"n": 0}

    async def _run_git(args, cwd):
        if args[0] == "rev-parse":
            return 0, root + "\n"
        calls["n"] += 1
        index = min(calls["n"] - 1, len(dirty_sets) - 1)
        paths = dirty_sets[index]
        payload = "".join(f"?? {p}\x00" for p in paths)
        return 0, payload

    monkeypatch.setattr(shell_changes, "_run_git", _run_git)


@pytest.mark.asyncio
async def test_end_command_reports_newly_dirty_files(monkeypatch):
    _fake_git(monkeypatch, [{"a.py"}, {"a.py", "b.py"}])
    await shell_changes.begin_command("task-1")
    root, paths = await shell_changes.end_command("task-1")
    assert paths == ["b.py"]
    # 路径按平台规范化（Windows 下是反斜杠）
    assert root.replace("\\", "/") == "D:/repo"


@pytest.mark.asyncio
async def test_baseline_advances_between_commands(monkeypatch):
    """基线推进到当前状态——第二条命令只报它自己新改的文件。"""
    _fake_git(monkeypatch, [{"a.py"}, {"a.py", "b.py"}, {"a.py", "b.py", "c.py"}])
    await shell_changes.begin_command("task-1")
    assert (await shell_changes.end_command("task-1"))[1] == ["b.py"]
    assert (await shell_changes.end_command("task-1"))[1] == ["c.py"]


@pytest.mark.asyncio
async def test_non_git_workspace_is_unavailable_not_fake_empty(monkeypatch):
    """不是 git 仓库 → 明确不可用，不假装"没有变化"。"""

    async def _run_git(args, cwd):
        if args[0] == "rev-parse":
            return 128, ""
        return 0, ""

    monkeypatch.setattr(shell_changes, "_run_git", _run_git)
    await shell_changes.begin_command("task-1")
    baseline = shell_changes._BASELINES["task-1"]
    assert baseline.available is False
    root, paths = await shell_changes.end_command("task-1")
    assert paths == []


@pytest.mark.asyncio
async def test_git_failure_does_not_raise(monkeypatch):
    async def _run_git(args, cwd):
        return None

    monkeypatch.setattr(shell_changes, "_run_git", _run_git)
    await shell_changes.begin_command("task-1")
    assert (await shell_changes.end_command("task-1"))[1] == []


# ── 落库 ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_command_changes_writes_shell_rows(db, monkeypatch):
    """检出的文件写进 session_deliverables，source='shell'。"""
    await db.execute(
        "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
        "VALUES(?,?,?,?,?,?)",
        ("task-1", "x", "running", "yuncat", "sess-1", 1),
    )
    await db.commit()

    _fake_git(monkeypatch, [{"a.py"}, {"a.py", "b.py"}])
    await shell_changes.begin_command("task-1")
    recorded = await shell_changes.record_command_changes(db, "task-1", "sess-1")
    assert len(recorded) == 1
    assert recorded[0]["source"] == "shell"

    cursor = await db.execute(
        "SELECT file_path, source FROM session_deliverables WHERE session_id=?", ("sess-1",)
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "shell"


@pytest.mark.asyncio
async def test_repeat_detection_does_not_duplicate_rows(db, monkeypatch):
    """同一条路径被反复检出只留一行（否则多轮下来会攒出一堆重复行）。"""
    await db.execute(
        "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
        "VALUES(?,?,?,?,?,?)",
        ("task-2", "x", "running", "yuncat", "sess-2", 1),
    )
    await db.commit()

    _fake_git(monkeypatch, [{"a.py"}, {"a.py", "b.py"}])
    await shell_changes.begin_command("task-2")
    await shell_changes.record_command_changes(db, "task-2", "sess-2")
    # 再来一次同样的变更（基线被重置）
    shell_changes._BASELINES.pop("task-2")
    await shell_changes.begin_command("task-2")
    await shell_changes.record_command_changes(db, "task-2", "sess-2")

    cursor = await db.execute(
        "SELECT COUNT(*) FROM session_deliverables WHERE session_id=?", ("sess-2",)
    )
    row = await cursor.fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_real_git_detects_changed_files(tmp_path, monkeypatch):
    """真跑一次 git（不 mock）：命令改了哪些文件要能认出来。"""
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("环境里没有 git")
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args):
        subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)

    _git("init", "-q")
    _git("config", "user.email", "t@t.t")
    _git("config", "user.name", "t")
    (repo / "a.txt").write_text("hello", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-qm", "init")

    monkeypatch.setenv("YUNCAT_WORKSPACE", str(repo))
    await shell_changes.begin_command("task-real")
    (repo / "b.txt").write_text("generated", encoding="utf-8")
    (repo / "a.txt").write_text("changed", encoding="utf-8")

    _root, paths = await shell_changes.end_command("task-real")
    assert set(paths) == {"a.txt", "b.txt"}


@pytest.mark.asyncio
async def test_read_only_command_skips_git_entirely(monkeypatch):
    """只读命令不该跑 git——大仓库里 git status 实测 ~200ms，不能逐条付。"""
    calls = {"n": 0}

    async def _run_git(args, cwd):
        calls["n"] += 1
        return (0, "D:/repo\n") if args[0] == "rev-parse" else (0, "")

    monkeypatch.setattr(shell_changes, "_run_git", _run_git)
    await shell_changes.begin_command("task-ro", "ls -la")
    assert "task-ro" not in shell_changes._BASELINES
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_mutating_command_takes_baseline(monkeypatch):
    _fake_git(monkeypatch, [{"a.py"}, {"a.py", "b.py"}])
    await shell_changes.begin_command("task-mut", "python build.py")
    assert "task-mut" in shell_changes._BASELINES
    assert (await shell_changes.end_command("task-mut"))[1] == ["b.py"]


def test_read_only_detection_is_conservative():
    """多用途命令（git 等）一律当作会改文件——漏判的代价是「改动没被记录」。"""
    assert shell_changes.is_read_only_command("ls -la") is True
    assert shell_changes.is_read_only_command("  cat file.txt") is True
    assert shell_changes.is_read_only_command("") is False  # 认不出来 → 保守当作会改
    assert shell_changes.is_read_only_command("git commit -m x") is False
    assert shell_changes.is_read_only_command("npm install") is False
    assert shell_changes.is_read_only_command("python script.py") is False


@pytest.mark.asyncio
async def test_shell_file_diff_uses_git_head_as_old_side(tmp_path, monkeypatch):
    """shell 改动的文件没有快照备份——diff 旧侧取 git HEAD。"""
    import shutil
    import subprocess

    import aiosqlite
    from repositories import SCHEMA_SQL
    from repositories.deliverables_repo import add_session_deliverable
    from services.deliverables import file_diff

    if shutil.which("git") is None:
        pytest.skip("环境里没有 git")
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args):
        subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)

    _git("init", "-q")
    _git("config", "user.email", "t@t.t")
    _git("config", "user.name", "t")
    target = repo / "a.txt"
    target.write_text("line1\n", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-qm", "init")

    monkeypatch.setenv("YUNCAT_WORKSPACE", str(repo))
    target.write_text("line1\nline2\n", encoding="utf-8")

    conn = await aiosqlite.connect(str(tmp_path / "d.db"))
    await conn.executescript(SCHEMA_SQL)
    await add_session_deliverable(
        conn, "s1", str(target), rel_path="a.txt", source="shell", task_id="t1",
    )

    result = await file_diff(conn, "s1", str(target))
    await conn.close()

    assert result["kind"] == "text", result
    assert "+line2" in result["diff"]
    assert result["added"] == 1


@pytest.mark.asyncio
async def test_shell_file_diff_rejects_unregistered_path(tmp_path, monkeypatch):
    """只接受本会话登记过的 shell 改动——否则等于任意文件读取器。"""
    import aiosqlite
    from repositories import SCHEMA_SQL
    from services.deliverables import file_diff

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("YUNCAT_WORKSPACE", str(workspace))
    target = workspace / "secret.txt"
    target.write_text("secret", encoding="utf-8")

    conn = await aiosqlite.connect(str(tmp_path / "d.db"))
    await conn.executescript(SCHEMA_SQL)
    result = await file_diff(conn, "s1", str(target))
    await conn.close()

    assert result.get("status") == "error"
