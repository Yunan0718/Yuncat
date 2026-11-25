"""tests/contract/test_system_tools.py —— 核心工具契约测试 (v56 ToolResult 格式)

覆盖: read_file, write_file, run_command, list_dir 的全链路行为 (ToolResult 格式)
"""
import os
import sys
import tempfile
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


def _safe_unlink(path: str) -> None:
    if not os.path.exists(path):
        return
    for _ in range(5):
        try:
            os.unlink(path)
            return
        except PermissionError:
            time.sleep(0.05)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = str(tmp_path / "workspace")
    monkeypatch.setenv("YUNCAT_WORKSPACE", ws)
    from security.sandbox import get_workspace
    get_workspace()
    return ws


class TestReadFile:
    async def test_reads_file_within_workspace(self, workspace):
        from tools.builtin.system_tools import read_file
        test_file = os.path.join(workspace, "shared", "test.txt")
        os.makedirs(os.path.dirname(test_file), exist_ok=True)
        with open(test_file, "w") as f:
            f.write("hello world")
        result = await read_file("test.txt")
        assert result["success"] is True
        assert result["data"]["content"] == "hello world"

    async def test_rejects_path_traversal(self, workspace):
        from tools.builtin.system_tools import read_file
        result = await read_file("../../../etc/passwd")
        assert result["success"] is False

    async def test_file_not_found(self, workspace):
        from tools.builtin.system_tools import read_file
        result = await read_file("nonexistent.txt")
        assert result["success"] is False
        assert "不存在" in result["observation"]


class TestListDir:
    async def test_lists_directory(self, workspace):
        from tools.builtin.system_tools import list_dir
        shared = os.path.join(workspace, "shared")
        with open(os.path.join(shared, "a.txt"), "w") as f:
            f.write("a")
        os.makedirs(os.path.join(shared, "subdir"), exist_ok=True)
        result = await list_dir(".")
        assert result["success"] is True
        names = [e["name"] for e in result["data"]["entries"]]
        assert "a.txt" in names
        assert "subdir" in names

    async def test_rejects_outside_workspace(self, workspace):
        from tools.builtin.system_tools import list_dir
        result = await list_dir("../../../")
        assert result["success"] is False

    async def test_not_a_directory(self, workspace):
        from tools.builtin.system_tools import list_dir
        shared = os.path.join(workspace, "shared")
        with open(os.path.join(shared, "file.txt"), "w") as f:
            f.write("x")
        result = await list_dir("file.txt")
        assert result["success"] is False


class TestWriteFile:
    async def test_preview_returns_diff(self, workspace):
        from tools.builtin.system_tools import write_file
        result = await write_file("new.txt", "hello")
        assert result["success"] is True
        assert result["meta"]["phase"] == "preview"
        assert result["data"]["new_content"] == "hello"

    async def test_preview_shows_old_content(self, workspace):
        from tools.builtin.system_tools import write_file
        shared = os.path.join(workspace, "shared")
        with open(os.path.join(shared, "existing.txt"), "w") as f:
            f.write("old content")
        result = await write_file("existing.txt", "new content")
        assert result["success"] is True
        assert result["data"]["old_content"] == "old content"
        assert result["data"]["new_content"] == "new content"

    async def test_rejects_outside_workspace(self, workspace):
        from tools.builtin.system_tools import write_file
        result = await write_file("../../../etc/hosts", "malicious")
        assert result["success"] is False


class TestRunCommand:
    async def test_safe_command_preview(self, workspace):
        from tools.builtin.system_tools import run_command
        result = await run_command("dir")
        assert result["success"] is True
        assert result["meta"]["phase"] == "preview"
        assert result["data"]["command"] == "dir"

    async def test_dangerous_pipe_blocked(self, workspace):
        from tools.builtin.system_tools import run_command
        result = await run_command("dir | findstr test")
        assert result["success"] is False

    async def test_semicolon_blocked(self, workspace):
        from tools.builtin.system_tools import run_command
        result = await run_command("dir; rm -rf /")
        assert result["success"] is False

    async def test_non_whitelist_command_blocked(self, workspace):
        from tools.builtin.system_tools import run_command
        result = await run_command("rm -rf /")
        assert result["success"] is False

    async def test_redirect_blocked(self, workspace):
        from tools.builtin.system_tools import run_command
        result = await run_command("echo hello > file.txt")
        assert result["success"] is False


class TestIsSafeCommand:
    def test_whitelist_commands_pass(self):
        from tools.builtin.system_tools import is_safe_command
        for cmd in ["dir", "ls", "echo hello", "git status", "mkdir test"]:
            assert is_safe_command(cmd), f"Command '{cmd}' should be safe"

    def test_dangerous_chars_blocked(self):
        from tools.builtin.system_tools import is_safe_command
        for cmd in ["a | b", "a > b", "a < b", "a & b", "a `b`", "a $(b)", "a;b"]:
            assert not is_safe_command(cmd), f"Command '{cmd}' should be blocked"

    def test_python_script_allowed(self):
        from tools.builtin.system_tools import is_safe_command
        assert is_safe_command("python test.py", workspace_ok=False)

    def test_python_c_flag_blocked(self):
        from tools.builtin.system_tools import is_safe_command
        assert not is_safe_command("python -c 'print(1)'")

    def test_python_e_flag_blocked(self):
        from tools.builtin.system_tools import is_safe_command
        assert not is_safe_command("python3 -e 'import os'")


class TestTwoPhaseConfirm:
    async def test_create_and_pop_pending(self, tmp_path):
        import aiosqlite
        db_path = os.path.join(str(tmp_path), "test.db")
        conn = await aiosqlite.connect(db_path)
        await conn.execute("""
            CREATE TABLE runtime_states (
                id TEXT PRIMARY KEY, state_type TEXT NOT NULL,
                payload TEXT NOT NULL, created_at REAL NOT NULL,
                expires_at REAL, session_id TEXT,
                project_id TEXT, status TEXT DEFAULT 'pending',
                resolved_at REAL, resolution TEXT,
                title TEXT, task_id TEXT
            )
        """)
        await conn.commit()
        from tools.builtin.system_tools import create_pending_state, _pop_pending
        token = await create_pending_state(conn, "pending_write", {"path": "test.txt", "content": "hello"})
        assert len(token) == 32
        result = await _pop_pending(conn, token)
        assert result is not None
        assert result["state_type"] == "pending_write"
        assert result["payload"]["path"] == "test.txt"
        result2 = await _pop_pending(conn, token)
        assert result2 is None
        await conn.close()

    async def test_expired_token_returns_none(self, tmp_path):
        import aiosqlite
        db_path = os.path.join(str(tmp_path), "test.db")
        conn = await aiosqlite.connect(db_path)
        await conn.execute("""
            CREATE TABLE runtime_states (
                id TEXT PRIMARY KEY, state_type TEXT NOT NULL,
                payload TEXT NOT NULL, created_at REAL NOT NULL,
                expires_at REAL, session_id TEXT,
                project_id TEXT, status TEXT DEFAULT 'pending',
                resolved_at REAL, resolution TEXT,
                title TEXT, task_id TEXT
            )
        """)
        await conn.commit()
        from tools.builtin.system_tools import create_pending_state, _pop_pending
        token = await create_pending_state(conn, "pending_write", {"x": 1}, ttl=0)
        import asyncio
        await asyncio.sleep(0.1)
        result = await _pop_pending(conn, token)
        assert result is None
        await conn.close()


    async def test_dispatch_confirm_unknown_type(self, tmp_path):
        """未知 state_type 抛出 ValueError"""
        import aiosqlite
        db_path = os.path.join(str(tmp_path), "test.db")
        conn = await aiosqlite.connect(db_path)
        await conn.execute("""
            CREATE TABLE runtime_states (
                id TEXT PRIMARY KEY, state_type TEXT NOT NULL,
                payload TEXT NOT NULL, created_at REAL NOT NULL,
                expires_at REAL, session_id TEXT,
                project_id TEXT, status TEXT DEFAULT 'pending',
                resolved_at REAL, resolution TEXT,
                title TEXT, task_id TEXT
            )
        """)
        await conn.commit()
        from tools.builtin.system_tools import create_pending_state, dispatch_confirm
        token = await create_pending_state(conn, "unknown_type", {"x": 1})
        with pytest.raises(ValueError, match="Unknown state_type"):
            await dispatch_confirm(conn, token)
        await conn.close()


class TestExecuteCommand:
    async def test_execute_echo(self, workspace):
        from tools.builtin.system_tools import _execute_command
        result = await _execute_command("echo hello world", timeout_sec=5)
        assert "hello world" in result.output

    async def test_execute_dir(self, workspace):
        from tools.builtin.system_tools import _execute_command
        result = await _execute_command("dir", timeout_sec=5)
        assert len(result.output) > 0

    async def test_failing_command_reports_exit_code(self, workspace):
        """2026-10-22：命令失败必须把退出码交回——此前一律 success=True。"""
        from tools.builtin.system_tool_command import _execute_command
        cmd = "exit 3" if sys.platform == "win32" else "false"
        expected = 3 if sys.platform == "win32" else 1
        result = await _execute_command(cmd, timeout_sec=10)
        assert result.exit_code == expected

    async def test_tool_result_marks_failed_command(self, workspace):
        """工具结果层面：非零退出码 → success=False + data.exit_code。"""
        from tools.builtin.system_tool_confirm import _handle_command
        cmd = "exit 3" if sys.platform == "win32" else "false"
        expected = 3 if sys.platform == "win32" else 1
        result = await _handle_command(None, {"command": cmd, "timeout_sec": 10})
        assert result["success"] is False
        assert result["meta"]["exit_code"] == expected
        assert str(expected) in result["observation"]
