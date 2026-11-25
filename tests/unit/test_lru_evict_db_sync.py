"""tests/unit/test_lru_evict_db_sync.py —— Task 3.5: LRU 淘汰 future 时 DB 状态同步

对标:
- 淘汰 future 时同步 UPDATE runtime_states status='expired'——关闭竞态窗口
- 原实现只 cancel future，DB 行仍为 pending，用户仍可通过 Inbox 批准已淘汰的确认

覆盖:
- 正常添加不触发淘汰（future 未 cancel，db 未调用）
- 超过 PENDING_CONFIRM_MAX 触发 LRU 淘汰（最老 future 被 cancel + db.execute 更新 expired）
- db=None 时仅 cancel future 不更新 DB（向后兼容）
- _mark_pending_expired 正确执行 UPDATE 语句（status/resolved_at/resolution/WHERE 条件）
- LRU 淘汰顺序正确（FIFO，move_to_end 后 popitem(last=False)）
- 淘汰后 resolve_confirmation 返回 False（token 不存在）
- 已 done 的 future 淘汰时不报错（防御性）

测试风格: 全部 async——pytest-asyncio AUTO 模式自动管理 event loop，
_add_pending_confirmation 内部的 asyncio.create_task 在 running loop 中可正常调度。
"""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class _FakeDB:
    """简易 async DB mock——记录 execute 调用参数供断言。

    所有 execute/commit 都是 async，匹配 aiosqlite.Connection 协议。
    """

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.commit_count = 0

    async def execute(self, sql: str, params: tuple = ()):
        self.calls.append((sql, params))

    async def commit(self):
        self.commit_count += 1


@pytest.fixture(autouse=True)
def _clear_pending():
    """每个测试前后清空 _pending_confirmations，避免测试间相互污染。"""
    from services.tools.tool_service import _pending_confirmations
    _pending_confirmations.clear()
    yield
    _pending_confirmations.clear()


def _make_future() -> asyncio.Future:
    """在当前 running loop 中创建 Future。"""
    return asyncio.get_running_loop().create_future()


class TestAddPendingConfirmationLRUEvict:
    """_add_pending_confirmation 的 LRU 淘汰 + DB 同步逻辑"""

    async def test_no_evict_when_below_max(self, monkeypatch):
        """添加数量未超上限时不触发淘汰——future 未 cancel，db 未调用"""
        from services.tools import tool_service as ts_mod
        monkeypatch.setattr(ts_mod, "_PENDING_CONFIRM_MAX", 5)

        db = _FakeDB()
        future = _make_future()

        ts_mod._add_pending_confirmation("tok1", future, db=db)

        assert "tok1" in ts_mod._pending_confirmations
        assert not future.cancelled()
        assert db.calls == []  # 未触发淘汰 → 无 DB 调用

    async def test_evict_oldest_when_exceed_max(self, monkeypatch):
        """超过上限时淘汰最老的——future 被 cancel + DB 异步更新 expired"""
        from services.tools import tool_service as ts_mod
        monkeypatch.setattr(ts_mod, "_PENDING_CONFIRM_MAX", 2)

        db = _FakeDB()
        f1 = _make_future()
        f2 = _make_future()
        f3 = _make_future()

        ts_mod._add_pending_confirmation("tok1", f1, db=db)
        ts_mod._add_pending_confirmation("tok2", f2, db=db)
        # 添加第 3 个触发淘汰 tok1
        ts_mod._add_pending_confirmation("tok3", f3, db=db)
        # 让 create_task 调度的 _mark_pending_expired 有机会执行
        await asyncio.sleep(0.01)

        # tok1 被淘汰（LRU FIFO）
        assert "tok1" not in ts_mod._pending_confirmations
        assert "tok2" in ts_mod._pending_confirmations
        assert "tok3" in ts_mod._pending_confirmations
        # f1 被 cancel
        assert f1.cancelled()
        # DB 收到 UPDATE expired 调用
        expired_calls = [c for c in db.calls if "status='expired'" in c[0]]
        assert len(expired_calls) == 1
        sql, params = expired_calls[0]
        assert "tok1" in params
        assert "lru_evicted" in params
        assert db.commit_count >= 1

    async def test_evict_order_is_lru_fifo(self, monkeypatch):
        """LRU 淘汰顺序：最先添加的（最久未使用）先被淘汰"""
        from services.tools import tool_service as ts_mod
        monkeypatch.setattr(ts_mod, "_PENDING_CONFIRM_MAX", 2)

        db = _FakeDB()
        f1 = _make_future()
        f2 = _make_future()
        f3 = _make_future()

        ts_mod._add_pending_confirmation("a", f1, db=db)
        ts_mod._add_pending_confirmation("b", f2, db=db)
        ts_mod._add_pending_confirmation("c", f3, db=db)  # 淘汰 a
        # 访问 b（move_to_end）→ b 变最新，a 已淘汰
        ts_mod._pending_confirmations.move_to_end("b")
        f4 = _make_future()
        ts_mod._add_pending_confirmation("d", f4, db=db)  # 应淘汰 c（c 比 b 旧）
        await asyncio.sleep(0.01)

        assert "a" not in ts_mod._pending_confirmations
        assert "c" not in ts_mod._pending_confirmations
        assert "b" in ts_mod._pending_confirmations
        assert "d" in ts_mod._pending_confirmations
        # DB 收到 a 和 c 两个 expired 更新
        expired_tokens = [c[1][-1] for c in db.calls if "status='expired'" in c[0]]
        assert expired_tokens == ["a", "c"]

    async def test_db_none_skips_db_update(self, monkeypatch):
        """db=None 时仅 cancel future，不调用 DB（向后兼容 / 测试场景）"""
        from services.tools import tool_service as ts_mod
        monkeypatch.setattr(ts_mod, "_PENDING_CONFIRM_MAX", 1)

        f1 = _make_future()
        f2 = _make_future()

        ts_mod._add_pending_confirmation("tok1", f1, db=None)
        ts_mod._add_pending_confirmation("tok2", f2, db=None)
        await asyncio.sleep(0.01)

        assert f1.cancelled()
        assert "tok1" not in ts_mod._pending_confirmations
        assert "tok2" in ts_mod._pending_confirmations

    async def test_already_done_future_not_cancelled(self, monkeypatch):
        """已 done 的 future 淘汰时不 cancel（防御性——cancel 已 done future 是 no-op 但避免日志噪音）"""
        from services.tools import tool_service as ts_mod
        monkeypatch.setattr(ts_mod, "_PENDING_CONFIRM_MAX", 1)

        db = _FakeDB()
        f1 = _make_future()
        f1.set_result({"done": True})  # 已完成
        f2 = _make_future()

        ts_mod._add_pending_confirmation("done_tok", f1, db=db)
        ts_mod._add_pending_confirmation("new_tok", f2, db=db)
        await asyncio.sleep(0.01)

        # f1 已 done 不应被 cancel（set_result 后 cancelled() 仍为 False）
        assert not f1.cancelled()
        assert f1.done()
        assert "done_tok" not in ts_mod._pending_confirmations
        # DB 仍应更新（即使 future 已 done，DB 行可能仍 pending 需清理）
        expired_calls = [c for c in db.calls if "status='expired'" in c[0]]
        assert len(expired_calls) == 1


class TestMarkPendingExpired:
    """_mark_pending_expired 的 SQL 正确性"""

    async def test_update_sql_correct(self):
        """_mark_pending_expired 执行 UPDATE status='expired' + WHERE status='pending'"""
        from services.tools.tool_service import _mark_pending_expired

        db = _FakeDB()
        before = time.time()
        await _mark_pending_expired("tok_xyz", db)
        after = time.time()

        assert len(db.calls) == 1
        sql, params = db.calls[0]
        # SQL 关键要素
        assert "UPDATE runtime_states" in sql
        assert "status='expired'" in sql
        assert "resolved_at=?" in sql
        assert "resolution=?" in sql
        assert "WHERE id=? AND status='pending'" in sql
        # 参数顺序: (resolved_at, resolution, token)
        assert params[1] == "lru_evicted"
        assert params[2] == "tok_xyz"
        assert before <= params[0] <= after  # resolved_at 时间戳合理
        assert db.commit_count == 1

    async def test_db_exception_swallowed(self):
        """DB 异常时仅记日志不抛出（best-effort）——future 已 cancel，不影响主流程"""
        from services.tools.tool_service import _mark_pending_expired

        class _BombDB:
            async def execute(self, *a, **kw):
                raise RuntimeError("simulated DB failure")

            async def commit(self):
                pass

        # 不应抛异常
        await _mark_pending_expired("tok", _BombDB())


class TestResolveConfirmationAfterEvict:
    """淘汰后 resolve_confirmation 行为——关闭竞态窗口的核心验证"""

    async def test_evicted_token_resolve_returns_false(self, monkeypatch):
        """已淘汰的 token 调 resolve_confirmation 返回 False（token 不存在）"""
        from services.tools import tool_service as ts_mod
        monkeypatch.setattr(ts_mod, "_PENDING_CONFIRM_MAX", 1)

        db = _FakeDB()
        f1 = _make_future()
        f2 = _make_future()

        ts_mod._add_pending_confirmation("evicted", f1, db=db)
        ts_mod._add_pending_confirmation("newer", f2, db=db)
        await asyncio.sleep(0.01)

        # 模拟用户通过 Inbox 批准已淘汰的 token
        result = await ts_mod.resolve_confirmation("evicted", {"result": "ok"})

        assert result is False
        # f1 应被 cancel（淘汰时），不应被 set_result
        assert f1.cancelled()
