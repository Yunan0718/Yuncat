"""tests/unit/test_memory_sessions_batch.py —— D1: v66 批量 commit 写穿透修复测试

覆盖:
- 批量 commit 触发条件（5 条 / assistant 角色）
- commit 失败时回滚 + 计数器重置
- flush_pending() 显式刷新
- flush_pending() 失败处理
"""
import os
import sys
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
async def memory_service(tmp_path):
    """创建 MemoryService 实例——使用真实 aiosqlite 连接"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_batch.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    from services.memory.memory_service import MemoryService
    svc = MemoryService(conn)
    yield svc
    await conn.close()
    _safe_unlink(db_path)


class TestBatchCommitTriggers:
    """批量 commit 触发条件

    Batch 5a C8 语义变更：user/assistant 消息立即 commit（持久化一致性优先，
    流式中途崩溃不丢最近 user 消息）；非 user/assistant 角色（如 system）
    仍走 5 条阈值批量 commit。
    """

    async def test_user_and_assistant_trigger_immediate_commit(self, memory_service):
        """user/assistant 消息立即触发 commit（Batch 5a C8）"""
        svc = memory_service
        for i in range(4):
            await svc.add_message("sess-assistant", "user", f"Q{i}")
            assert svc._pending_writes == 0, "user 消息应立即 commit，不累积 pending"

        await svc.add_message("sess-assistant", "assistant", "A4")
        assert svc._pending_writes == 0, "assistant 消息也立即 commit"

    async def test_5th_message_triggers_commit(self, memory_service):
        """非 user/assistant 角色第 5 条消息触发 commit（阈值机制仍存）"""
        svc = memory_service
        for i in range(4):
            await svc.add_message("sess-5", "system", f"S{i}")
        assert svc._pending_writes == 4, "system 消息低于阈值时累积 pending"

        await svc.add_message("sess-5", "system", "S4")
        assert svc._pending_writes == 0, "第 5 条消息后 pending_writes 应清零"

    async def test_pending_writes_accumulate_under_threshold(self, memory_service):
        """低于阈值时 system 消息累积 pending"""
        svc = memory_service
        await svc.add_message("sess-acc", "system", "S1")
        assert svc._pending_writes == 1
        await svc.add_message("sess-acc", "system", "S2")
        assert svc._pending_writes == 2
        await svc.add_message("sess-acc", "system", "S3")
        assert svc._pending_writes == 3


class TestCommitFailureHandling:
    """commit 失败时的错误处理——D1 核心修复"""

    async def test_commit_failure_resets_counter(self, memory_service):
        """commit 失败时 _pending_writes 必须重置，避免后续状态错乱"""
        svc = memory_service
        # 4 条 system 消息累积 pending（不触发 commit）
        for i in range(4):
            await svc.add_message("sess-fail", "system", f"S{i}")
        assert svc._pending_writes == 4

        # 注入 commit 失败
        original_commit = svc.db.commit
        call_count = {"rollback": 0}

        async def failing_commit():
            raise RuntimeError("simulated commit failure")

        async def spy_rollback():
            call_count["rollback"] += 1

        svc.db.commit = failing_commit
        svc.db.rollback = spy_rollback

        # 第 5 条触发 commit——应失败
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            await svc.add_message("sess-fail", "system", "S4")

        # 验证计数器已重置
        assert svc._pending_writes == 0, "commit 失败后 pending_writes 必须重置为 0"
        # 验证 rollback 被调用
        assert call_count["rollback"] >= 1, "commit 失败后必须调用 rollback"

        # 恢复
        svc.db.commit = original_commit

    async def test_commit_failure_does_not_update_memory(self, memory_service):
        """commit 失败时内存缓存不应更新——保持 DB 与内存一致"""
        svc = memory_service
        for i in range(4):
            await svc.add_message("sess-nomem", "user", f"Q{i}")

        async def failing_commit():
            raise RuntimeError("commit failed")

        async def noop_rollback():
            pass

        svc.db.commit = failing_commit
        svc.db.rollback = noop_rollback

        with pytest.raises(RuntimeError):
            await svc.add_message("sess-nomem", "user", "Q4-fail")

        # 内存中应有前 4 条（已 INSERT 但未 commit），第 5 条不应进入
        ctx = await svc.get_session_context("sess-nomem")
        contents = [m["content"] for m in ctx]
        assert "Q4-fail" not in contents, "commit 失败的消息不应进入内存缓存"
        assert len(contents) == 4, "前 4 条应保留在内存"

    async def test_recovery_after_commit_failure(self, memory_service):
        """commit 失败后系统能正常恢复——后续写入工作正常"""
        svc = memory_service
        for i in range(4):
            await svc.add_message("sess-recover", "system", f"S{i}")

        fail_once = [True]

        original_commit = svc.db.commit

        async def flaky_commit():
            if fail_once[0]:
                fail_once[0] = False
                raise RuntimeError("transient failure")
            await original_commit()

        async def noop_rollback():
            pass

        svc.db.commit = flaky_commit
        svc.db.rollback = noop_rollback

        # 第 5 条——commit 失败
        with pytest.raises(RuntimeError):
            await svc.add_message("sess-recover", "system", "S4-fail")

        # 恢复正常 commit
        svc.db.commit = original_commit

        # 后续写入应正常（system 消息恢复累积语义）
        await svc.add_message("sess-recover", "system", "S5-ok")
        assert svc._pending_writes == 1
        ctx = await svc.get_session_context("sess-recover")
        contents = [m["content"] for m in ctx]
        assert "S5-ok" in contents


class TestFlushPending:
    """flush_pending() 显式刷新——D1 新增方法"""

    async def test_flush_pending_commits_writes(self, memory_service):
        """flush_pending() 提交所有 pending writes"""
        svc = memory_service
        # 3 条 system 消息累积 pending（不触发批量 commit）
        for i in range(3):
            await svc.add_message("sess-flush", "system", f"S{i}")
        assert svc._pending_writes == 3

        # 显式 flush
        await svc.flush_pending()
        assert svc._pending_writes == 0, "flush 后 pending_writes 应清零"

    async def test_flush_pending_noop_when_clean(self, memory_service):
        """无 pending 时 flush 是 no-op"""
        svc = memory_service
        assert svc._pending_writes == 0
        # 不应抛异常
        await svc.flush_pending()
        assert svc._pending_writes == 0

    async def test_flush_pending_persists_to_db(self, memory_service):
        """flush 后即使内存缓存被清，DB 中仍有记录"""
        svc = memory_service
        for i in range(3):
            await svc.add_message("sess-persist", "user", f"Q{i}")
        await svc.flush_pending()

        # 清除内存缓存
        svc._sessions.pop("sess-persist", None)

        # 从 DB 重新加载
        ctx = await svc.get_session_context("sess-persist")
        assert len(ctx) == 3, "flush 后 DB 应有全部 3 条记录"
        contents = [m["content"] for m in ctx]
        assert contents == ["Q0", "Q1", "Q2"]

    async def test_flush_pending_failure_resets_counter(self, memory_service):
        """flush_pending() commit 失败时也必须重置计数器 + rollback"""
        svc = memory_service
        await svc.add_message("sess-flush-fail", "system", "S0")
        assert svc._pending_writes == 1

        rollback_called = [0]

        async def failing_commit():
            raise RuntimeError("flush commit failed")

        async def spy_rollback():
            rollback_called[0] += 1

        svc.db.commit = failing_commit
        svc.db.rollback = spy_rollback

        with pytest.raises(RuntimeError, match="flush commit failed"):
            await svc.flush_pending()

        assert svc._pending_writes == 0, "flush 失败后计数器必须重置"
        assert rollback_called[0] >= 1, "flush 失败后必须 rollback"
