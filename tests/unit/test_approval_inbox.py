"""tests/unit/test_approval_inbox.py —— Phase 107: 统一审批 Inbox 单元测试

覆盖：
- create_pending_state 新增 title/task_id/project_id 参数
- _pop_pending 改 UPDATE...RETURNING（保留历史 + 并发保护）
- _expire_pending_states 标记过期 pending → expired
- _cleanup_expired_states 保留 pending 记录
- ApprovalInboxService.get_pending / get_history / resolve_rejected / get_by_token
- resolve_approved 包装 dispatch_confirm（用测试 handler 避免文件系统副作用）
- 并发批准只有第一个成功（原子性）
"""
import asyncio
import os
import tempfile
import time as _time

import aiosqlite
import pytest

from repositories import init_db, run_migrations
from repositories.schema import SCHEMA_SQL


@pytest.fixture(autouse=True)
def _clear_session_allowed_tools():
    """每个测试前后清空 session 级工具允许列表，避免测试间污染。"""
    from services.tools.tool_service import clear_session_allowed_tools
    clear_session_allowed_tools()
    yield
    clear_session_allowed_tools()


@pytest.fixture
async def db():
    """fresh DB——executescript(SCHEMA_SQL) 建表（与项目其他测试一致）"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = await init_db(path)
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()
    os.unlink(path)


@pytest.mark.unit
class TestCreatePendingStateEnhanced:
    """Phase 107: create_pending_state 新增可选参数"""

    async def test_create_with_title_task_project(self, db):
        from tools.builtin.system_tool_confirm import create_pending_state
        token = await create_pending_state(
            db, "pending_write", {"path": "x"},
            title="写入 test.txt", task_id="task-1", project_id="proj-1",
        )
        cursor = await db.execute(
            "SELECT title, task_id, project_id, status FROM runtime_states WHERE id=?",
            (token,),
        )
        row = await cursor.fetchone()
        assert row[0] == "写入 test.txt"
        assert row[1] == "task-1"
        assert row[2] == "proj-1"
        assert row[3] == "pending"

    async def test_create_without_optional_fields_stores_null(self, db):
        from tools.builtin.system_tool_confirm import create_pending_state
        token = await create_pending_state(db, "pending_write", {"path": "x"})
        cursor = await db.execute(
            "SELECT title, task_id, project_id, status FROM runtime_states WHERE id=?",
            (token,),
        )
        row = await cursor.fetchone()
        assert row[0] is None
        assert row[1] is None
        assert row[2] is None
        assert row[3] == "pending"

    async def test_create_default_status_is_pending(self, db):
        from tools.builtin.system_tool_confirm import create_pending_state
        token = await create_pending_state(db, "pending_write", {"path": "x"})
        cursor = await db.execute("SELECT status FROM runtime_states WHERE id=?", (token,))
        row = await cursor.fetchone()
        assert row[0] == "pending"


@pytest.mark.unit
class TestPopPendingUpdateReturning:
    """Phase 107: _pop_pending 改 UPDATE...RETURNING"""

    async def test_pop_returns_payload_and_marks_resolved(self, db):
        from tools.builtin.system_tool_confirm import create_pending_state, _pop_pending
        token = await create_pending_state(
            db, "pending_write", {"path": "test.txt", "content": "hi"},
        )
        result = await _pop_pending(db, token)
        assert result is not None
        assert result["state_type"] == "pending_write"
        assert result["payload"]["path"] == "test.txt"
        # 验证记录仍在 DB（status='resolved'）
        cursor = await db.execute(
            "SELECT status, resolved_at FROM runtime_states WHERE id=?", (token,)
        )
        row = await cursor.fetchone()
        assert row[0] == "resolved"
        assert row[1] is not None

    async def test_pop_twice_returns_none_second_time(self, db):
        """并发保护——第二次 pop 同一 token 返回 None"""
        from tools.builtin.system_tool_confirm import create_pending_state, _pop_pending
        token = await create_pending_state(db, "pending_write", {"path": "x"})
        first = await _pop_pending(db, token)
        second = await _pop_pending(db, token)
        assert first is not None
        assert second is None

    async def test_pop_expired_token_returns_none(self, db):
        from tools.builtin.system_tool_confirm import create_pending_state, _pop_pending
        # 创建一个已过期的 token（ttl=0 → 立即过期）
        token = await create_pending_state(db, "pending_write", {"path": "x"}, ttl=0)
        _time.sleep(0.01)  # 确保 expires_at < now
        result = await _pop_pending(db, token)
        assert result is None


@pytest.mark.unit
class TestExpirePendingStates:
    """Phase 107: _expire_pending_states 标记过期 pending → expired"""

    async def test_expire_marks_pending_as_expired(self, db):
        from tools.builtin.system_tool_confirm import create_pending_state, _expire_pending_states
        # 创建一个已过期的 token
        token = await create_pending_state(db, "pending_write", {"path": "x"}, ttl=0)
        _time.sleep(0.01)
        # 标记前 status='pending'
        cursor = await db.execute("SELECT status FROM runtime_states WHERE id=?", (token,))
        assert (await cursor.fetchone())[0] == "pending"

        count = await _expire_pending_states(db)
        assert count == 1

        cursor = await db.execute("SELECT status FROM runtime_states WHERE id=?", (token,))
        assert (await cursor.fetchone())[0] == "expired"

    async def test_expire_does_not_touch_resolved(self, db):
        from tools.builtin.system_tool_confirm import (
            create_pending_state, _pop_pending, _expire_pending_states,
        )
        # 使用默认 ttl（1800s）确保 _pop_pending 能成功标记 resolved
        token = await create_pending_state(db, "pending_write", {"path": "x"})
        await _pop_pending(db, token)  # 标记为 resolved
        count = await _expire_pending_states(db)
        assert count == 0  # resolved 不受影响
        cursor = await db.execute("SELECT status FROM runtime_states WHERE id=?", (token,))
        assert (await cursor.fetchone())[0] == "resolved"

    async def test_expire_does_not_touch_non_expired_pending(self, db):
        from tools.builtin.system_tool_confirm import (
            create_pending_state, _expire_pending_states,
        )
        # ttl=1800（默认），不会过期
        token = await create_pending_state(db, "pending_write", {"path": "x"})
        count = await _expire_pending_states(db)
        assert count == 0
        cursor = await db.execute("SELECT status FROM runtime_states WHERE id=?", (token,))
        assert (await cursor.fetchone())[0] == "pending"


@pytest.mark.unit
class TestCleanupExpiredStatesPreservesPending:
    """Phase 107: _cleanup_expired_states 保留 pending 记录（让 _expire 先标记）"""

    async def test_cleanup_preserves_expired_pending(self, db):
        from tools.builtin.system_tool_confirm import (
            create_pending_state, _cleanup_expired_states,
        )
        token = await create_pending_state(db, "pending_write", {"path": "x"}, ttl=0)
        _time.sleep(0.01)
        await _cleanup_expired_states(db)
        # pending 记录应保留（让 _expire_pending_states 先标记）
        cursor = await db.execute("SELECT status FROM runtime_states WHERE id=?", (token,))
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "pending"

    async def test_cleanup_deletes_resolved_past_retention(self, db):
        """resolved 状态仅在其 resolved_at 超过审计留存期后才删除（审计留存设计）。

        原测试期望「expires_at 过期 → 立即删 resolved」，与 _cleanup_expired_states
        的审计留存语义矛盾：resolved 是审计记录，保留 CONFIRM_AUDIT_RETENTION_DAYS 天。
        """
        from tools.builtin.system_tool_confirm import (
            create_pending_state, _pop_pending, _cleanup_expired_states,
        )
        from config import CONFIRM_AUDIT_RETENTION_DAYS
        token = await create_pending_state(db, "pending_write", {"path": "x"})
        await _pop_pending(db, token)  # status='resolved', resolved_at=now
        # 将 resolved_at 推到留存期之前，模拟审计期满
        await db.execute(
            "UPDATE runtime_states SET resolved_at=? WHERE id=?",
            (_time.time() - (CONFIRM_AUDIT_RETENTION_DAYS + 1) * 86400, token),
        )
        await db.commit()
        await _cleanup_expired_states(db)
        cursor = await db.execute("SELECT status FROM runtime_states WHERE id=?", (token,))
        assert await cursor.fetchone() is None  # 留存期满 → 删除


@pytest.mark.unit
class TestApprovalInboxService:
    """Phase 107: ApprovalInboxService 核心方法"""

    async def test_get_pending_returns_only_pending(self, db):
        from services.approval_inbox import ApprovalInboxService
        from tools.builtin.system_tool_confirm import (
            create_pending_state, _pop_pending,
        )
        svc = ApprovalInboxService(db=db)
        t1 = await create_pending_state(db, "pending_write", {"path": "a"}, title="A")
        t2 = await create_pending_state(db, "pending_write", {"path": "b"}, title="B")
        await _pop_pending(db, t2)  # t2 → resolved

        pending = await svc.get_pending()
        assert len(pending) == 1
        assert pending[0]["id"] == t1
        assert pending[0]["title"] == "A"

    async def test_get_pending_filter_by_task_id(self, db):
        from services.approval_inbox import ApprovalInboxService
        from tools.builtin.system_tool_confirm import create_pending_state
        svc = ApprovalInboxService(db=db)
        await create_pending_state(db, "pending_write", {"path": "a"}, task_id="task-1")
        await create_pending_state(db, "pending_write", {"path": "b"}, task_id="task-2")
        pending = await svc.get_pending(task_id="task-1")
        assert len(pending) == 1
        assert pending[0]["task_id"] == "task-1"

    async def test_get_pending_filter_by_project_id(self, db):
        from services.approval_inbox import ApprovalInboxService
        from tools.builtin.system_tool_confirm import create_pending_state
        svc = ApprovalInboxService(db=db)
        await create_pending_state(db, "pending_write", {"path": "a"}, project_id="proj-1")
        await create_pending_state(db, "pending_write", {"path": "b"}, project_id="proj-2")
        pending = await svc.get_pending(project_id="proj-1")
        assert len(pending) == 1
        assert pending[0]["project_id"] == "proj-1"

    async def test_get_history_returns_resolved_rejected_expired(self, db):
        from services.approval_inbox import ApprovalInboxService
        from tools.builtin.system_tool_confirm import (
            create_pending_state, _pop_pending, _expire_pending_states,
        )
        svc = ApprovalInboxService(db=db)
        # resolved
        t1 = await create_pending_state(db, "pending_write", {"path": "a"}, title="resolved-one")
        await _pop_pending(db, t1)
        # rejected
        t2 = await create_pending_state(db, "pending_write", {"path": "b"}, title="rejected-one")
        await svc.resolve_rejected(t2, "no need")
        # expired
        t3 = await create_pending_state(db, "pending_write", {"path": "c"}, ttl=0, title="expired-one")
        _time.sleep(0.01)
        await _expire_pending_states(db)

        history = await svc.get_history()
        assert len(history) == 3
        statuses = {h["status"] for h in history}
        assert statuses == {"resolved", "rejected", "expired"}

    async def test_resolve_rejected_sets_status(self, db):
        from services.approval_inbox import ApprovalInboxService
        from tools.builtin.system_tool_confirm import create_pending_state
        svc = ApprovalInboxService(db=db)
        token = await create_pending_state(db, "pending_write", {"path": "x"}, title="to-reject")
        result = await svc.resolve_rejected(token, "user said no")
        assert result["action"] == "rejected"
        assert result["reason"] == "user said no"
        cursor = await db.execute(
            "SELECT status, resolution FROM runtime_states WHERE id=?", (token,)
        )
        row = await cursor.fetchone()
        assert row[0] == "rejected"
        assert row[1] == "user said no"

    async def test_resolve_rejected_on_already_resolved_returns_error(self, db):
        from services.approval_inbox import ApprovalInboxService
        from tools.builtin.system_tool_confirm import (
            create_pending_state, _pop_pending,
        )
        svc = ApprovalInboxService(db=db)
        token = await create_pending_state(db, "pending_write", {"path": "x"})
        await _pop_pending(db, token)  # 已 resolved
        result = await svc.resolve_rejected(token, "too late")
        assert result["error"] == "already-resolved"

    async def test_get_by_token_returns_full_record(self, db):
        from services.approval_inbox import ApprovalInboxService
        from tools.builtin.system_tool_confirm import create_pending_state
        svc = ApprovalInboxService(db=db)
        token = await create_pending_state(
            db, "pending_write", {"path": "x", "content": "hi"},
            title="详情", task_id="t1", project_id="p1",
        )
        item = await svc.get_by_token(token)
        assert item is not None
        assert item["id"] == token
        assert item["state_type"] == "pending_write"
        assert item["title"] == "详情"
        assert item["task_id"] == "t1"
        assert item["project_id"] == "p1"
        assert item["status"] == "pending"
        assert item["payload"]["path"] == "x"

    async def test_get_by_token_nonexistent_returns_none(self, db):
        from services.approval_inbox import ApprovalInboxService
        svc = ApprovalInboxService(db=db)
        assert await svc.get_by_token("nonexistent") is None


@pytest.mark.unit
class TestResolveApprovedViaDispatchConfirm:
    """Phase 107: resolve_approved 包装 dispatch_confirm——用测试 handler 避免文件系统副作用"""

    async def test_resolve_approved_executes_handler_and_records_summary(self, db):
        from services.approval_inbox import ApprovalInboxService
        from tools.builtin.system_tool_confirm import (
            create_pending_state, CONFIRM_HANDLERS,
        )

        # 注册测试 handler——返回固定结果，不触碰文件系统
        async def _test_handler(_db, payload):
            return {"success": True, "data": {"echo": payload.get("path")},
                    "meta": {"tool_name": "test_tool"}}

        CONFIRM_HANDLERS["pending_test_handler"] = _test_handler
        try:
            svc = ApprovalInboxService(db=db)
            token = await create_pending_state(
                db, "pending_test_handler", {"path": "echo.txt"}, title="test-approve",
            )
            result = await svc.resolve_approved(token)
            assert result["success"] is True
            assert result["data"]["echo"] == "echo.txt"
            # 验证 status='resolved' + resolution 含摘要
            cursor = await db.execute(
                "SELECT status, resolution FROM runtime_states WHERE id=?", (token,)
            )
            row = await cursor.fetchone()
            assert row[0] == "resolved"
            assert "test_tool executed" in row[1]
        finally:
            CONFIRM_HANDLERS.pop("pending_test_handler", None)

    async def test_resolve_approved_on_expired_raises_valueerror(self, db):
        from services.approval_inbox import ApprovalInboxService
        from tools.builtin.system_tool_confirm import create_pending_state
        svc = ApprovalInboxService(db=db)
        token = await create_pending_state(db, "pending_write", {"path": "x"}, ttl=0)
        _time.sleep(0.01)
        with pytest.raises(ValueError):
            await svc.resolve_approved(token)

    async def test_resolve_approved_concurrent_only_first_succeeds(self, db):
        """并发批准——只有第一个成功（原子 UPDATE...RETURNING 保护）"""
        from services.approval_inbox import ApprovalInboxService
        from tools.builtin.system_tool_confirm import (
            create_pending_state, CONFIRM_HANDLERS,
        )

        async def _test_handler(_db, payload):
            return {"success": True, "data": {}, "meta": {"tool_name": "concurrent_test"}}

        CONFIRM_HANDLERS["pending_concurrent_test"] = _test_handler
        try:
            svc = ApprovalInboxService(db=db)
            token = await create_pending_state(
                db, "pending_concurrent_test", {"path": "x"},
            )
            # 第一次批准成功
            r1 = await svc.resolve_approved(token)
            assert r1["success"] is True
            # 第二次批准应抛 ValueError（token 已 resolved）
            with pytest.raises(ValueError):
                await svc.resolve_approved(token)
        finally:
            CONFIRM_HANDLERS.pop("pending_concurrent_test", None)


@pytest.mark.unit
class TestResolveApprovedRememberSession:
    """Batch 1.4: resolve_approved 支持 remember——加入 session 级允许列表。"""

    async def test_remember_true_adds_tool_to_session_allow_list(self, db):
        """remember=True 且执行成功 → tool_name 加入 session 允许列表。"""
        from services.approval_inbox import ApprovalInboxService
        from services.tools.tool_service import is_session_allowed
        from tools.builtin.system_tool_confirm import (
            create_pending_state, CONFIRM_HANDLERS,
        )

        async def _test_handler(_db, payload):
            return {"success": True, "data": {"echo": payload.get("path")},
                    "meta": {"tool_name": "write_file"}}

        CONFIRM_HANDLERS["pending_write_file_remember"] = _test_handler
        try:
            svc = ApprovalInboxService(db=db)
            token = await create_pending_state(
                db, "pending_write_file_remember", {"path": "a.txt"},
                title="remember-test",
            )
            assert not is_session_allowed("write_file")
            result = await svc.resolve_approved(token, remember=True, tool_name="write_file")
            assert result["success"] is True
            assert is_session_allowed("write_file")
        finally:
            CONFIRM_HANDLERS.pop("pending_write_file_remember", None)

    async def test_remember_false_does_not_add_tool(self, db):
        """remember=False → 不加入 session 允许列表。"""
        from services.approval_inbox import ApprovalInboxService
        from services.tools.tool_service import is_session_allowed
        from tools.builtin.system_tool_confirm import (
            create_pending_state, CONFIRM_HANDLERS,
        )

        async def _test_handler(_db, payload):
            return {"success": True, "data": {}, "meta": {"tool_name": "run_command"}}

        CONFIRM_HANDLERS["pending_run_command_remember"] = _test_handler
        try:
            svc = ApprovalInboxService(db=db)
            token = await create_pending_state(
                db, "pending_run_command_remember", {"command": "ls"},
                title="no-remember-test",
            )
            result = await svc.resolve_approved(token, remember=False, tool_name="run_command")
            assert result["success"] is True
            assert not is_session_allowed("run_command")
        finally:
            CONFIRM_HANDLERS.pop("pending_run_command_remember", None)

    async def test_remember_derives_tool_name_from_state_type(self, db):
        """未传 tool_name 时，从 state_type 推导并加入允许列表。"""
        from services.approval_inbox import ApprovalInboxService
        from services.tools.tool_service import is_session_allowed
        from tools.builtin.system_tool_confirm import (
            create_pending_state, CONFIRM_HANDLERS,
        )

        async def _test_handler(_db, payload):
            return {"success": True, "data": {}, "meta": {"tool_name": "edit_file"}}

        original_handler = CONFIRM_HANDLERS.get("pending_edit_file")
        CONFIRM_HANDLERS["pending_edit_file"] = _test_handler
        try:
            svc = ApprovalInboxService(db=db)
            token = await create_pending_state(
                db, "pending_edit_file", {"path": "b.txt"}, title="derive-test",
            )
            assert not is_session_allowed("edit_file")
            result = await svc.resolve_approved(token, remember=True)
            assert result["success"] is True
            assert is_session_allowed("edit_file")
        finally:
            if original_handler is not None:
                CONFIRM_HANDLERS["pending_edit_file"] = original_handler
            else:
                CONFIRM_HANDLERS.pop("pending_edit_file", None)

    async def test_failed_execution_does_not_add_to_allow_list(self, db):
        """执行失败时即使 remember=True 也不加入允许列表。"""
        from services.approval_inbox import ApprovalInboxService
        from services.tools.tool_service import is_session_allowed
        from tools.builtin.system_tool_confirm import (
            create_pending_state, CONFIRM_HANDLERS,
        )

        async def _test_handler(_db, payload):
            return {"success": False, "error": "simulated failure",
                    "meta": {"tool_name": "delete_file"}}

        CONFIRM_HANDLERS["pending_delete_file_fail"] = _test_handler
        try:
            svc = ApprovalInboxService(db=db)
            token = await create_pending_state(
                db, "pending_delete_file_fail", {"path": "c.txt"}, title="fail-test",
            )
            result = await svc.resolve_approved(token, remember=True, tool_name="delete_file")
            assert result["success"] is False
            assert not is_session_allowed("delete_file")
        finally:
            CONFIRM_HANDLERS.pop("pending_delete_file_fail", None)


@pytest.mark.unit
class TestBatchResolve:
    """Batch 3.2: ApprovalInboxService 批量批准/拒绝"""

    async def _make_tokens(self, db, count: int, handler_name: str = "pending_batch_test"):
        from tools.builtin.system_tool_confirm import create_pending_state
        tokens = []
        for i in range(count):
            token = await create_pending_state(
                db, handler_name, {"path": f"file{i}.txt"}, title=f"batch-{i}",
            )
            tokens.append(token)
        return tokens

    async def test_resolve_approved_batch_executes_all(self, db):
        from services.approval_inbox import ApprovalInboxService
        from tools.builtin.system_tool_confirm import CONFIRM_HANDLERS

        async def _handler(_db, payload):
            return {"success": True, "data": {"path": payload.get("path")},
                    "meta": {"tool_name": "write_file"}}

        CONFIRM_HANDLERS["pending_batch_test"] = _handler
        try:
            svc = ApprovalInboxService(db=db)
            tokens = await self._make_tokens(db, 3)
            results = await svc.resolve_approved_batch(tokens)
            assert len(results) == 3
            assert all(r["success"] for r in results)
        finally:
            CONFIRM_HANDLERS.pop("pending_batch_test", None)

    async def test_resolve_approved_batch_remember_adds_session_allowed(self, db):
        from services.approval_inbox import ApprovalInboxService
        from services.tools.tool_service import is_session_allowed
        from tools.builtin.system_tool_confirm import CONFIRM_HANDLERS

        async def _handler(_db, payload):
            return {"success": True, "data": {}, "meta": {"tool_name": "write_file"}}

        CONFIRM_HANDLERS["pending_batch_remember"] = _handler
        try:
            svc = ApprovalInboxService(db=db)
            tokens = await self._make_tokens(db, 2, "pending_batch_remember")
            assert not is_session_allowed("write_file")
            await svc.resolve_approved_batch(tokens, remember=True)
            assert is_session_allowed("write_file")
        finally:
            CONFIRM_HANDLERS.pop("pending_batch_remember", None)

    async def test_resolve_approved_batch_partial_already_resolved(self, db):
        from services.approval_inbox import ApprovalInboxService
        from tools.builtin.system_tool_confirm import CONFIRM_HANDLERS, _pop_pending

        async def _handler(_db, payload):
            return {"success": True, "data": {}, "meta": {"tool_name": "write_file"}}

        CONFIRM_HANDLERS["pending_batch_partial"] = _handler
        try:
            svc = ApprovalInboxService(db=db)
            tokens = await self._make_tokens(db, 2, "pending_batch_partial")
            await _pop_pending(db, tokens[0])
            results = await svc.resolve_approved_batch(tokens)
            assert results[0]["success"] is False
            assert results[0]["error"] == "already-resolved"
            assert results[1]["success"] is True
        finally:
            CONFIRM_HANDLERS.pop("pending_batch_partial", None)

    async def test_resolve_rejected_batch_wakes_all_futures(self, db):
        from services.approval_inbox import ApprovalInboxService
        from services.tools.tool_service import _add_pending_confirmation
        from tools.builtin.system_tool_confirm import create_pending_state

        svc = ApprovalInboxService(db=db)
        tokens = []
        futures = []
        for i in range(3):
            token = await create_pending_state(
                db, "pending_write", {"path": f"x{i}.txt"},
            )
            fut = asyncio.get_running_loop().create_future()
            _add_pending_confirmation(token, fut)
            tokens.append(token)
            futures.append(fut)

        results = await svc.resolve_rejected_batch(tokens, reason="batch reject")
        assert len(results) == 3
        assert all(r["success"] for r in results)
        assert all(fut.done() and not fut.cancelled() for fut in futures)
