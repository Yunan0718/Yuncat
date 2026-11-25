"""tests/unit/test_confirm_atomicity.py —— A1: Token 确认操作原子性残留竞态修复

覆盖:
- 竞态 1: cancel_pending_state 超时后同步标记 DB 为 rejected（关闭 60s 窗口）
- 竞态 3: resolve_rejected 唤醒等待的 future（避免 agent 卡 30 分钟）
- 核心原子性回归: _pop_pending 双批准只有第一个成功
"""
import os
import sys
import time
import json
import asyncio
import pytest
import aiosqlite

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
async def db(tmp_path):
    """创建带 schema 的 aiosqlite 连接"""
    db_path = os.path.join(str(tmp_path), "test_a1.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()
    _safe_unlink(db_path)


async def _insert_pending_state(conn, token: str = "tok-1") -> str:
    """插入一个 pending 状态的 runtime_state"""
    now = time.time()
    await conn.execute(
        "INSERT INTO runtime_states(id, state_type, payload, created_at, expires_at, status) "
        "VALUES(?,?,?,?,?,?)",
        (token, "pending_write", json.dumps({"path": "test.txt"}), now, now + 1800, "pending"),
    )
    await conn.commit()
    return token


async def _get_state_status(conn, token: str) -> str:
    cursor = await conn.execute("SELECT status FROM runtime_states WHERE id=?", (token,))
    row = await cursor.fetchone()
    return row[0] if row else ""


class TestCancelPendingStateRace1:
    """竞态 1: 超时取消时同步标记 DB——关闭 60s 窗口"""

    async def test_cancel_marks_db_rejected(self, db):
        """cancel_pending_state 应同步将 DB 行标记为 rejected"""
        from services.tools.tool_service import cancel_pending_state
        token = await _insert_pending_state(db, "tok-cancel-1")

        # 注册一个 future
        from services.tools.tool_service import _add_pending_confirmation
        future = asyncio.get_event_loop().create_future()
        _add_pending_confirmation(token, future)

        # 取消——应同时 cancel future + 标记 DB
        await cancel_pending_state(token, db)

        assert future.cancelled() or future.done()
        assert await _get_state_status(db, token) == "rejected", \
            "取消后 DB 状态应为 rejected，关闭超时后仍可批准的窗口"

    async def test_cancel_after_timeout_blocks_late_approval(self, db):
        """超时取消后，用户尝试批准应失败（DB 已 rejected）"""
        from services.tools.tool_service import cancel_pending_state
        from tools.builtin.system_tool_confirm import _pop_pending
        token = await _insert_pending_state(db, "tok-cancel-2")

        future = asyncio.get_event_loop().create_future()
        from services.tools.tool_service import _add_pending_confirmation
        _add_pending_confirmation(token, future)

        # 超时取消
        await cancel_pending_state(token, db)

        # 用户尝试批准——_pop_pending 应返回 None（status 已是 rejected）
        result = await _pop_pending(db, token)
        assert result is None, "取消后不应再能批准"

    async def test_cancel_idempotent(self, db):
        """多次取消不报错"""
        from services.tools.tool_service import cancel_pending_state
        token = await _insert_pending_state(db, "tok-cancel-3")

        future = asyncio.get_event_loop().create_future()
        from services.tools.tool_service import _add_pending_confirmation
        _add_pending_confirmation(token, future)

        await cancel_pending_state(token, db)
        # 第二次取消——future 已不在 LRU，DB 已 rejected，不应抛异常
        await cancel_pending_state(token, db)
        assert await _get_state_status(db, token) == "rejected"

    async def test_cancel_without_db_only_cancels_future(self, db):
        """不传 db 时仅 cancel future，不更新 DB（向后兼容）"""
        from services.tools.tool_service import cancel_pending_state
        token = await _insert_pending_state(db, "tok-cancel-4")

        future = asyncio.get_event_loop().create_future()
        from services.tools.tool_service import _add_pending_confirmation
        _add_pending_confirmation(token, future)

        await cancel_pending_state(token, db=None)
        assert future.cancelled() or future.done()
        # DB 状态应保持 pending（未更新）
        assert await _get_state_status(db, token) == "pending"


class TestResolveRejectedWakesFutureRace3:
    """竞态 3: resolve_rejected 应唤醒等待的 future"""

    async def test_resolve_rejected_wakes_future(self, db):
        """reject 后 future 应被唤醒，避免 agent 卡 30 分钟"""
        from services.approval_inbox import ApprovalInboxService
        from services.tools.tool_service import _add_pending_confirmation

        token = await _insert_pending_state(db, "tok-reject-1")
        future = asyncio.get_event_loop().create_future()
        _add_pending_confirmation(token, future)

        svc = ApprovalInboxService(db)
        await svc.resolve_rejected(token, reason="user said no")

        # future 应被唤醒（不是 cancelled，而是 set_result）
        assert future.done(), "reject 后 future 应被唤醒"
        assert not future.cancelled(), "future 应被 set_result 而非 cancel"
        result = future.result()
        assert result["action"] == "rejected"
        assert "用户拒绝" in result["error"] or "rejected" in result.get("error", "")

    async def test_resolve_rejected_db_status(self, db):
        """reject 后 DB 状态应为 rejected"""
        from services.approval_inbox import ApprovalInboxService
        token = await _insert_pending_state(db, "tok-reject-2")

        svc = ApprovalInboxService(db)
        await svc.resolve_rejected(token, reason="no")

        assert await _get_state_status(db, token) == "rejected"

    async def test_resolve_rejected_already_resolved(self, db):
        """已批准的 token 拒绝应返回 already-resolved"""
        from services.approval_inbox import ApprovalInboxService
        from tools.builtin.system_tool_confirm import _pop_pending
        token = await _insert_pending_state(db, "tok-reject-3")

        # 先批准
        await _pop_pending(db, token)

        # 再拒绝——应返回 already-resolved
        svc = ApprovalInboxService(db)
        result = await svc.resolve_rejected(token)
        assert result["error"] == "already-resolved"

    async def test_resolve_rejected_no_future_still_works(self, db):
        """DB 有 pending 但无 future（如 LRU 淘汰后）——reject 仍正常更新 DB"""
        from services.approval_inbox import ApprovalInboxService
        token = await _insert_pending_state(db, "tok-reject-4")
        # 不注册 future——模拟 LRU 淘汰后的场景

        svc = ApprovalInboxService(db)
        result = await svc.resolve_rejected(token, reason="late reject")
        assert result["action"] == "rejected"
        assert await _get_state_status(db, token) == "rejected"


class TestCoreAtomicityRegression:
    """核心原子性回归——_pop_pending 双批准只有第一个成功"""

    async def test_double_approve_only_first_succeeds(self, db):
        """并发批准同一 token，只有第一个成功"""
        from tools.builtin.system_tool_confirm import _pop_pending
        token = await _insert_pending_state(db, "tok-double-1")

        # 第一次 pop——应成功
        result1 = await _pop_pending(db, token)
        assert result1 is not None
        assert result1["state_type"] == "pending_write"

        # 第二次 pop——应返回 None（已 resolved）
        result2 = await _pop_pending(db, token)
        assert result2 is None

    async def test_expired_token_not_poppable(self, db):
        """过期 token 不可批准"""
        from tools.builtin.system_tool_confirm import _pop_pending
        token = "tok-expired-1"
        now = time.time()
        # 已过期的 pending state
        await db.execute(
            "INSERT INTO runtime_states(id, state_type, payload, created_at, expires_at, status) "
            "VALUES(?,?,?,?,?,?)",
            (token, "pending_write", "{}", now - 100, now - 10, "pending"),
        )
        await db.commit()

        result = await _pop_pending(db, token)
        assert result is None
