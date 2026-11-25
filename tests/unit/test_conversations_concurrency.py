"""tests/unit/test_conversations_concurrency.py —— T7: conversations 并发写测试

D1/D2 已覆盖单线程批量 commit + commit 失败回滚。本文件补并发场景空白:

- 同会话并发写: asyncio.gather 同时写同一 session_id → 全部持久化、内存缓存一致
- 跨会话并发写: 多 session_id 并发 → 各会话隔离、无串扰
- 并发写 + flush_pending: add_message 与 flush_pending 竞争 → 不丢消息
- 并发写中 commit 失败: 注入失败 commit → 恰好 1 个协程触发 commit 失败、rollback 被调用、计数器归零范围、连接可恢复

架构约束（源自 CLAUDE.md 铁律）:
- SQLite 单连接: aiosqlite 全局一个连接，内部串行化，不会触发 "database is locked"
- _pending_writes 是共享计数器: 并发下精确值不确定，只断言范围 (0 <= counter < 5) 或 flush 后归零
- 记忆写穿透: 先 INSERT (await) → 增计数器 (sync) → 决定 commit (sync/await) → append 内存缓存 (await)
- commit 失败 rollback 回滚同事务所有 INSERT: 并发下未触发 commit 的协程的 INSERT 也会被回滚
  (此为 D1 接受的乐观缓存设计——内存缓存可能含未持久化消息，本测试不断言 DB==内存)
"""
import asyncio
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
    """创建 MemoryService 实例——使用真实 aiosqlite 连接（与 test_memory_sessions_batch.py 一致）"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_concurrent.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    from services.memory.memory_service import MemoryService
    svc = MemoryService(conn)
    yield svc
    await conn.close()
    _safe_unlink(db_path)


async def _count_rows(svc, session_id: str | None = None) -> int:
    """统计 conversations 表行数（可选按 session_id 过滤）"""
    if session_id:
        cursor = await svc.db.execute(
            "SELECT COUNT(*) FROM conversations WHERE session_id=?", (session_id,)
        )
    else:
        cursor = await svc.db.execute("SELECT COUNT(*) FROM conversations")
    row = await cursor.fetchone()
    return int(row[0])


class TestConcurrentSameSession:
    """同会话并发写——asyncio.gather 同时写同一 session_id"""

    async def test_all_messages_persist_to_db(self, memory_service):
        """10 条并发 user 消息全部持久化到 DB"""
        svc = memory_service
        results = await asyncio.gather(
            *[svc.add_message("sess-same", "user", f"m{i}") for i in range(10)]
        )
        assert len(results) == 10
        assert all(isinstance(r, dict) for r in results), "所有 add_message 应返回 msg dict"
        # flush 兜底提交剩余 pending
        await svc.flush_pending()
        assert await _count_rows(svc, "sess-same") == 10, "10 条消息应全部在 DB"

    async def test_all_messages_in_memory_cache(self, memory_service):
        """10 条并发消息全部进入内存缓存（写穿透）"""
        svc = memory_service
        await asyncio.gather(
            *[svc.add_message("sess-cache", "user", f"msg{i}") for i in range(10)]
        )
        await svc.flush_pending()
        ctx = await svc.get_session_context("sess-cache")
        assert len(ctx) == 10, "内存缓存应有 10 条"
        contents = sorted(m["content"] for m in ctx)
        assert contents == [f"msg{i}" for i in range(10)], "消息内容应完整无丢失"

    async def test_counter_sane_after_concurrent_writes(self, memory_service):
        """并发写后计数器在合理范围；flush 后归零"""
        svc = memory_service
        await asyncio.gather(
            *[svc.add_message("sess-ctr", "user", f"m{i}") for i in range(8)]
        )
        # 并发下精确值不确定，但必须非负且 < 5（否则应已触发 commit 归零）
        assert 0 <= svc._pending_writes < 5, \
            f"counter 应在 [0,5) 范围，实际 {svc._pending_writes}"
        await svc.flush_pending()
        assert svc._pending_writes == 0, "flush 后计数器应归零"

    async def test_no_exception_under_concurrency(self, memory_service):
        """并发写不应抛异常（happy path）"""
        svc = memory_service
        results = await asyncio.gather(
            *[svc.add_message("sess-noerr", "user", f"m{i}") for i in range(12)],
            return_exceptions=True,
        )
        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, \
            f"并发写不应有异常，但捕获到: {exceptions}"


class TestConcurrentCrossSession:
    """跨会话并发写——多 session_id 并发，验证会话隔离"""

    async def test_each_session_isolated(self, memory_service):
        """5 个会话各 4 条消息并发写 → 各会话 DB 行数 = 4，无串扰"""
        svc = memory_service
        tasks = []
        for s in range(5):
            for m in range(4):
                tasks.append(svc.add_message(f"sess{s}", "user", f"m{s}-{m}"))
        await asyncio.gather(*tasks)
        await svc.flush_pending()
        for s in range(5):
            assert await _count_rows(svc, f"sess{s}") == 4, \
                f"sess{s} 应有 4 条消息"
        assert await _count_rows(svc) == 20, "总计 20 条"

    async def test_memory_cache_per_session_correct(self, memory_service):
        """各会话内存缓存只含自己的消息"""
        svc = memory_service
        await asyncio.gather(
            svc.add_message("sess-a", "user", "a1"),
            svc.add_message("sess-b", "user", "b1"),
            svc.add_message("sess-a", "user", "a2"),
            svc.add_message("sess-b", "user", "b2"),
        )
        await svc.flush_pending()
        ctx_a = await svc.get_session_context("sess-a")
        ctx_b = await svc.get_session_context("sess-b")
        contents_a = sorted(m["content"] for m in ctx_a)
        contents_b = sorted(m["content"] for m in ctx_b)
        assert contents_a == ["a1", "a2"], f"sess-a 应只含 a1/a2，实际 {contents_a}"
        assert contents_b == ["b1", "b2"], f"sess-b 应只含 b1/b2，实际 {contents_b}"


class TestConcurrentWithFlush:
    """并发 add_message 与 flush_pending 竞争"""

    async def test_no_message_lost_with_concurrent_flush(self, memory_service):
        """8 条 add_message + 1 flush_pending 并发 → 8 条全部持久化"""
        svc = memory_service
        tasks = [svc.add_message("sess-flush", "user", f"m{i}") for i in range(8)]
        tasks.append(svc.flush_pending())  # 与 add_message 竞争
        results = await asyncio.gather(*tasks, return_exceptions=True)
        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, f"不应有异常: {exceptions}"
        # 兜底 flush 确保剩余 pending 提交
        await svc.flush_pending()
        assert await _count_rows(svc, "sess-flush") == 8, \
            "并发 flush 不应导致消息丢失"

    async def test_counter_zero_after_final_flush(self, memory_service):
        """并发 flush 后再 flush 一次，计数器归零"""
        svc = memory_service
        await asyncio.gather(
            *[svc.add_message("sess-fc", "user", f"m{i}") for i in range(6)],
            svc.flush_pending(),
            svc.flush_pending(),  # 多次 flush 竞争
            return_exceptions=True,
        )
        await svc.flush_pending()
        assert svc._pending_writes == 0, "最终 flush 后计数器应归零"
        assert await _count_rows(svc, "sess-fc") == 6


class TestConcurrentCommitFailure:
    """并发写中 commit 失败——验证 rollback + 计数器 + 连接可恢复

    5 条并发 system 消息: 共享计数器递增，恰有 1 个协程拿到值 5 触发 commit。
    commit 始终失败 → 该协程 raise + rollback + 计数器重置。
    其余 4 个协程不触发 commit（拿到值 1-4），正常返回（但其 INSERT 会被 rollback 回滚）。

    Batch 5a C8 语义变更: user/assistant 消息立即 commit（不再累积），
    故必须用 system 角色才能保留"凑满 5 条才触发 commit"的阈值路径。
    """

    async def test_exactly_one_failure_with_always_fail_commit(self, memory_service):
        """commit 始终失败时，5 条并发恰好 1 个 raise（拿到 counter=5 的那个）"""
        svc = memory_service
        rollback_count = [0]

        async def always_fail_commit():
            raise RuntimeError("simulated commit failure")

        async def counting_rollback():
            rollback_count[0] += 1

        svc.db.commit = always_fail_commit
        svc.db.rollback = counting_rollback

        results = await asyncio.gather(
            *[svc.add_message("sess-fail", "system", f"m{i}") for i in range(5)],
            return_exceptions=True,
        )
        failures = [r for r in results if isinstance(r, RuntimeError)]
        successes = [r for r in results if isinstance(r, dict)]
        assert len(failures) == 1, \
            f"应恰好 1 个 commit 失败，实际 {len(failures)}"
        assert len(successes) == 4, \
            f"应 4 个未触发 commit 的协程正常返回，实际 {len(successes)}"
        assert rollback_count[0] >= 1, "commit 失败必须调用 rollback"

    async def test_counter_in_range_after_concurrent_failure(self, memory_service):
        """并发 commit 失败后计数器在 [0,5) 范围"""
        svc = memory_service

        async def always_fail_commit():
            raise RuntimeError("commit failed")

        async def noop_rollback():
            pass

        svc.db.commit = always_fail_commit
        svc.db.rollback = noop_rollback

        await asyncio.gather(
            *[svc.add_message("sess-ctr-fail", "user", f"m{i}") for i in range(5)],
            return_exceptions=True,
        )
        # 失败协程重置为 0；其余协程可能递增到 1-4。范围 [0,5)
        assert 0 <= svc._pending_writes < 5, \
            f"并发失败后 counter 应在 [0,5)，实际 {svc._pending_writes}"

    async def test_connection_recovered_after_concurrent_failure(self, memory_service):
        """并发 commit 失败后恢复 commit，连接仍可用"""
        svc = memory_service
        original_commit = svc.db.commit

        async def always_fail_commit():
            raise RuntimeError("transient")

        async def noop_rollback():
            pass

        svc.db.commit = always_fail_commit
        svc.db.rollback = noop_rollback

        await asyncio.gather(
            *[svc.add_message("sess-recover", "user", f"m{i}") for i in range(5)],
            return_exceptions=True,
        )

        # 恢复正常 commit
        svc.db.commit = original_commit

        # 连接应仍可用——新消息能写入并持久化
        await svc.add_message("sess-recover", "user", "after-recovery")
        await svc.flush_pending()
        count = await _count_rows(svc, "sess-recover")
        assert count >= 1, \
            f"恢复后至少 1 条消息应持久化，实际 {count}"
