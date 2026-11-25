"""tests/unit/test_tool_service.py —— ToolService 三级硬约束测试"""
import asyncio
import os
import sys
import tempfile

import aiosqlite
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def db():
    """fresh in-memory DB with SCHEMA_SQL"""
    from repositories.schema import SCHEMA_SQL
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = await aiosqlite.connect(path)
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()
    os.unlink(path)


@pytest.fixture(autouse=True)
def _clear_session_allowed_tools():
    """每个测试前后清空 session 级工具允许列表。"""
    from services.tools.tool_service import clear_session_allowed_tools
    clear_session_allowed_tools()
    yield
    clear_session_allowed_tools()


class MockRulesService:
    def __init__(self, rules: list[dict] | None = None):
        self.rules = rules or []

    async def get_enabled_rules(self) -> list[dict]:
        return self.rules


# ---- 模块级工具注册（所有测试类共享） ----
_orig_tools_snapshot: dict | None = None


def _register_test_tools():
    global _orig_tools_snapshot
    from tools.registry import _tools, tool as tool_deco
    if _orig_tools_snapshot is None:
        _orig_tools_snapshot = dict(_tools)
    _tools.clear()

    @tool_deco(
        name="read_file", agents=["hermes"],
        description="Read a file", parameters={},
        is_read_only=True, is_concurrency_safe=True,
    )
    async def read_file(path: str):
        return {"action": "read", "content": "test"}

    @tool_deco(
        name="write_file", agents=["hermes"],
        description="Write a file", parameters={},
        confirmable=True,
    )
    async def write_file(path: str, content: str):
        from tools.result import ToolResult
        return ToolResult(
            success=True,
            data={"path": path, "new_content": content},
            meta={"phase": "preview", "tool_name": "write_file"},
        ).to_dict()

    @tool_deco(
        name="run_command", agents=["hermes"],
        description="Run command", parameters={},
        confirmable=True,
    )
    async def run_command(command: str, timeout_sec: int = 60):
        return {"action": "preview", "command": command}


@pytest.fixture(autouse=True)
def _ensure_tools():
    """每个测试前重新注册工具，测试后恢复原始注册表（避免影响 Phase24 等后续测试）。"""
    _register_test_tools()
    yield
    from tools.registry import _tools
    _tools.clear()
    if _orig_tools_snapshot is not None:
        _tools.update(_orig_tools_snapshot)


class TestToolServiceHardConstraints:
    """三级硬约束验证"""

    async def test_valid_tool_executes(self):
        """有效工具正常执行"""
        from services.tools.tool_service import ToolService
        svc = ToolService()
        result = await svc.execute("read_file", {"path": "test.py"}, agent_id="hermes")
        assert result["action"] == "read"

    async def test_agent_not_allowed_rejected(self):
        """Agent 不在工具代理列表中时拒绝"""
        from services.tools.tool_service import ToolService
        svc = ToolService()
        with pytest.raises(PermissionError, match="not allowed"):
            await svc.execute("write_file", {"path": "x.txt", "content": "y"}, agent_id="openclaw")

    async def test_unknown_tool_raises(self):
        """调用不存在的工具抛出 ValueError"""
        from services.tools.tool_service import ToolService
        svc = ToolService()
        with pytest.raises(ValueError, match="not found"):
            await svc.execute("nonexistent_tool", {})

    async def test_rules_block_non_confirmable(self):
        """规则要求确认但工具未标记 confirmable 时拒绝"""
        from services.tools.tool_service import ToolService
        # 注册非 confirmable 的 "dangerous" 工具
        from tools.registry import tool as tool_deco

        @tool_deco(name="dangerous", agents=["hermes"], description="danger", parameters={})
        async def dangerous():
            return {"action": "done"}

        rules_svc = MockRulesService([
            {
                "id": "r1", "description": "危险操作需确认",
                "trigger_on": ["tool:dangerous"],
                "action": "require_confirmation",
                "enabled": True,
            },
        ])
        svc = ToolService(rules_service=rules_svc)
        with pytest.raises(PermissionError, match="requires confirmation"):
            await svc.execute("dangerous", {}, agent_id="hermes")

    async def test_run_command_blocked_by_whitelist(self):
        """run_command 非白名单命令返回 blocked (v56 ToolResult)"""
        from services.tools.tool_service import ToolService
        svc = ToolService()
        result = await svc.execute("run_command", {"command": "rm -rf /"}, agent_id="hermes")
        assert result["success"] is False
        assert result["meta"]["blocked"] is True


class TestToolServiceSerialLock:
    """架构铁律 #4: 非并发工具串行"""

    async def test_concurrent_write_serialized(self):
        """write_file 的串行锁存在且可用"""
        from services.tools.tool_service import ToolService
        svc = ToolService()
        lock = svc._get_serial_lock("write_file")
        assert lock is not None
        # 验证锁可以正常 acquire/release
        await lock.acquire()
        lock.release()

    async def test_read_file_no_serial_lock_needed(self):
        """read_file 不需要串行锁执行"""
        from services.tools.tool_service import ToolService
        svc = ToolService()
        result = await svc.execute("read_file", {"path": "test.py"}, agent_id="hermes")
        assert result["action"] == "read"


class TestSessionAllowedTools:
    """Batch 1.4: session 级 "Always allow" 允许列表。"""

    async def test_add_and_check_session_allowed_tool(self):
        """add_session_allowed_tool + is_session_allowed 基本流程。"""
        from services.tools.tool_service import (
            add_session_allowed_tool, is_session_allowed, clear_session_allowed_tools,
        )
        assert not is_session_allowed("write_file")
        add_session_allowed_tool("write_file")
        assert is_session_allowed("write_file")
        clear_session_allowed_tools()
        assert not is_session_allowed("write_file")

    async def test_add_empty_tool_name_is_no_op(self):
        """空 tool_name 不应加入允许列表。"""
        from services.tools.tool_service import add_session_allowed_tool, is_session_allowed
        add_session_allowed_tool("")
        assert not is_session_allowed("")

    async def test_execute_confirmable_auto_approves_when_session_allowed(self, db):
        """工具已在 session 允许列表中时，execute_confirmable 直接执行不弹窗。"""
        from services.tools.tool_service import ToolService, add_session_allowed_tool
        from tools.builtin.system_tool_confirm import CONFIRM_HANDLERS

        async def _fake_handle_write(_db, payload):
            return {"success": True, "data": {"path": payload.get("path")},
                    "meta": {"tool_name": "write_file"}, "_auto_approved": True}

        original = CONFIRM_HANDLERS.get("pending_write")
        CONFIRM_HANDLERS["pending_write"] = _fake_handle_write
        try:
            svc = ToolService()
            add_session_allowed_tool("write_file")
            result = await svc.execute_confirmable(
                db, "write_file", {"path": "auto.txt", "content": "hello"},
                agent_id="hermes", task_id="t1",
            )
            # 自动执行路径会返回 _auto_approved=True
            assert result.get("_auto_approved") is True
            assert result["success"] is True
        finally:
            if original is not None:
                CONFIRM_HANDLERS["pending_write"] = original
            else:
                CONFIRM_HANDLERS.pop("pending_write", None)


class TestConfirmationBatchHelpers:
    """Batch 3.2: resolve_confirmation_batch / cancel_pending_confirmation_batch"""

    async def test_resolve_confirmation_batch_wakes_all_futures(self):
        from services.tools.tool_service import (
            _add_pending_confirmation, resolve_confirmation_batch,
        )

        futures = []
        for i in range(3):
            fut = asyncio.get_running_loop().create_future()
            _add_pending_confirmation(f"tok-batch-{i}", fut)
            futures.append(fut)

        results = await resolve_confirmation_batch(
            [f"tok-batch-{i}" for i in range(3)],
            lambda _t: {"success": True, "output": "executed"},
        )

        assert len(results) == 3
        assert all(ok for _, ok in results)
        assert all(fut.done() for fut in futures)

    async def test_resolve_confirmation_batch_missing_token_returns_false(self):
        from services.tools.tool_service import resolve_confirmation_batch

        results = await resolve_confirmation_batch(
            ["tok-missing"], lambda _t: {"success": True},
        )
        assert results == [("tok-missing", False)]

    async def test_cancel_pending_confirmation_batch_cancels_futures(self):
        from services.tools.tool_service import (
            _add_pending_confirmation, cancel_pending_confirmation_batch,
        )

        futures = []
        for i in range(2):
            fut = asyncio.get_running_loop().create_future()
            _add_pending_confirmation(f"tok-cancel-{i}", fut)
            futures.append(fut)

        await cancel_pending_confirmation_batch([f"tok-cancel-{i}" for i in range(2)])

        assert all(fut.cancelled() for fut in futures)
