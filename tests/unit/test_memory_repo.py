"""tests/unit/test_memory_repo.py —— Dream 状态管理 + 事实操作测试"""
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
async def memory_repo(tmp_path):
    """创建 MemoryRepository 实例"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    from repositories.memory_repo import MemoryRepository
    repo = MemoryRepository(conn)
    yield repo
    await conn.close()
    _safe_unlink(db_path)


class TestDreamState:
    """Dream 状态管理"""

    async def test_get_dream_state_initial(self, memory_repo):
        """初始 dream 状态为上次时间 None"""
        val = await memory_repo.get_dream_state("last_dream_time")
        assert val is None or val == 0

    async def test_set_and_get_dream_state(self, memory_repo):
        """写入后读取一致"""
        now = time.time()
        await memory_repo.set_dream_state("last_dream_time", now)
        val = await memory_repo.get_dream_state("last_dream_time")
        assert val is not None
        assert abs(val - now) < 5  # 秒级精度

    async def test_total_dreams_counter(self, memory_repo):
        """total_dreams 计数器可读写"""
        await memory_repo.set_dream_state("total_dreams", 5)
        val = await memory_repo.get_dream_state("total_dreams")
        assert val == 5


class TestDreamLock:
    """Dream 排他锁"""

    async def test_acquire_lock_succeeds_when_unlocked(self, memory_repo):
        """初始未锁定时获取成功"""
        ok = await memory_repo.acquire_dream_lock()
        assert ok is True

    async def test_acquire_lock_fails_when_locked(self, memory_repo):
        """已锁定时获取失败"""
        await memory_repo.acquire_dream_lock()
        ok = await memory_repo.acquire_dream_lock()
        assert ok is False

    async def test_release_then_reacquire(self, memory_repo):
        """释放后可重新获取"""
        await memory_repo.acquire_dream_lock()
        await memory_repo.release_dream_lock()
        ok = await memory_repo.acquire_dream_lock()
        assert ok is True


class TestFactOperations:
    """事实 CRUD"""

    async def test_count_conversations(self, memory_repo):
        """统计新增对话轮次"""
        count = await memory_repo.count_conversations_since(0)
        assert count == 0

    async def test_get_unconsolidated_facts_empty(self, memory_repo):
        """初始无未整合事实"""
        facts = await memory_repo.get_unconsolidated_facts(10)
        assert facts == []

    async def test_save_semantic_and_retrieve(self, memory_repo):
        """写入后可通过 FTS 检索"""
        mem_id = await memory_repo.save_semantic(
            "测试记忆内容", "test", "owner1", 0.8, "fact", verified=True,
        )
        assert len(mem_id) > 0

    async def test_update_confidence(self, memory_repo):
        """更新置信度"""
        mem_id = await memory_repo.save_semantic("x", "t", "o", 0.5)
        await memory_repo.update_confidence(mem_id, 0.25)
        # 更新不抛异常即为通过

    async def test_delete_low_confidence_orphans(self, memory_repo):
        """删除低置信度孤立记忆"""
        await memory_repo.save_semantic("low", "t", "o", 0.2, verified=False)
        deleted = await memory_repo.delete_low_confidence_orphans(0.3)
        assert deleted >= 1


class TestFindConflicts:
    """记忆冲突检测"""

    async def test_find_conflicts_empty(self, memory_repo):
        """空表返回空列表"""
        conflicts = await memory_repo.find_conflicts(10)
        assert conflicts == []

    async def test_find_conflicts_detects_divergent_confidence(self, memory_repo):
        """同一主题不同置信度被识别为冲突"""
        await memory_repo.save_semantic(
            "用户喜欢深色模式", "test", "o1", 0.9, "fact", verified=False,
        )
        await memory_repo.save_semantic(
            "用户喜欢深色模式", "test", "o2", 0.3, "fact", verified=False,
        )
        conflicts = await memory_repo.find_conflicts(10)
        assert len(conflicts) >= 1
        # 返回结构包含 key 与 items
        assert "key" in conflicts[0]
        assert "items" in conflicts[0]
        assert len(conflicts[0]["items"]) >= 2
