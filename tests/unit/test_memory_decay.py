"""tests/unit/test_memory_decay.py —— Batch 2.5: 语义记忆 Ebbinghaus 时间衰减

覆盖:
  - _compute_memory_decay_factor 纯函数行为
  - search_memories_fts 对同 BM25 分数的记忆按 created_at 衰减重排
  - 高 BM25 分数的旧记忆仍可胜过低 BM25 分数的新记忆
  - 衰减后仍遵守 limit 和 filters

对标 MemoryTree 的 compute_decay_factor 与 retrieve_context 衰减策略。
"""
import math
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def memory_db(tmp_path):
    """创建带 SCHEMA_SQL + 列级迁移的 DB。"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_memory_decay.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    from repositories.schema import COLUMN_MIGRATIONS, _safe_add_column
    for table, column, definition in COLUMN_MIGRATIONS:
        await _safe_add_column(conn, table, column, definition)
    await conn.commit()
    yield conn
    await conn.close()


async def _insert_memory(conn, mem_id, content, created_at, project_id=None):
    """直接插入 memory（绕过 insert_memory 的自动时间戳），用于控制 created_at。"""
    await conn.execute(
        "INSERT INTO memories(id, content, source, owner, confidence, visibility, "
        "memory_type, verified, created_at, project_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (mem_id, content, "test", "owner1", 0.5, "private", "fact", 0, created_at, project_id),
    )
    # 同步 FTS5
    await conn.execute(
        "INSERT INTO memories_fts(memory_id, content) VALUES(?,?)",
        (mem_id, content),
    )
    await conn.commit()


# ═══════════════════════════════════════════════════════════════
# Batch 2.5: decay 因子纯函数
# ═══════════════════════════════════════════════════════════════

class TestComputeMemoryDecayFactor:
    """_compute_memory_decay_factor 纯函数测试"""

    def test_zero_age_no_decay(self):
        """age=0 时衰减因子为 1.0"""
        from repositories.memories_repo import _compute_memory_decay_factor
        assert _compute_memory_decay_factor(0) == pytest.approx(1.0)

    def test_positive_age_decays(self):
        """age>0 时衰减因子 < 1.0 且 > 0"""
        from repositories.memories_repo import _compute_memory_decay_factor
        factor = _compute_memory_decay_factor(30)
        assert 0 < factor < 1.0

    def test_older_age_lower_factor(self):
        """年龄越大衰减越多"""
        from repositories.memories_repo import _compute_memory_decay_factor
        recent = _compute_memory_decay_factor(1)
        old = _compute_memory_decay_factor(90)
        assert recent > old

    def test_matches_ebbinghaus_formula(self):
        """符合 exp(-rate * age) 公式"""
        from repositories.memories_repo import _compute_memory_decay_factor, MEMORY_DECAY_RATE
        assert _compute_memory_decay_factor(30) == pytest.approx(math.exp(-MEMORY_DECAY_RATE * 30))


# ═══════════════════════════════════════════════════════════════
# Batch 2.5: search_memories_fts 时间衰减排序
# ═══════════════════════════════════════════════════════════════

class TestSearchMemoriesDecay:
    """search_memories_fts 按 created_at 衰减重排"""

    @pytest.mark.asyncio
    async def test_recent_memory_ranks_higher_than_old(self, memory_db):
        """同 BM25 分数下，新记忆排在旧记忆前。"""
        from repositories.memories_repo import search_memories_fts

        now = int(time.time())
        # 内容相同 → BM25 分数相同；旧记忆 60 天前创建
        await _insert_memory(memory_db, "old", "python tutorial", now - 86400 * 60)
        await _insert_memory(memory_db, "new", "python tutorial", now - 86400 * 1)

        results = await search_memories_fts(memory_db, "python", limit=10)
        ids = [r["id"] for r in results]
        assert ids.index("new") < ids.index("old")

    @pytest.mark.asyncio
    async def test_high_relevance_old_can_outrank_low_relevance_new(self, memory_db):
        """BM25 足够高的旧记忆仍可胜过 BM25 低的新记忆。"""
        from repositories.memories_repo import search_memories_fts

        now = int(time.time())
        # 旧记忆：大量命中 python → BM25 很高；只旧 10 天，衰减还不太严重
        await _insert_memory(
            memory_db, "old_high",
            "python python python python python python python python python python tutorial",
            now - 86400 * 10,
        )
        # 新记忆：只命中 1 次 python → BM25 较低
        await _insert_memory(
            memory_db, "new_low",
            "python",
            now - 86400 * 1,
        )

        results = await search_memories_fts(memory_db, "python", limit=10)
        # old_high 应排第一（衰减后仍高于 new_low）
        assert results[0]["id"] == "old_high"

    @pytest.mark.asyncio
    async def test_decay_respects_limit(self, memory_db):
        """衰减重排后仍遵守 limit。"""
        from repositories.memories_repo import search_memories_fts

        now = int(time.time())
        for i in range(5):
            await _insert_memory(memory_db, f"m{i}", "python tutorial", now - 86400 * i)

        results = await search_memories_fts(memory_db, "python", limit=3)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_decay_works_with_filters(self, memory_db):
        """衰减与 filters（source/owner/project_id）同时生效。"""
        from repositories.memories_repo import search_memories_fts

        now = int(time.time())
        await _insert_memory(memory_db, "a", "python tutorial", now - 86400, project_id="proj_a")
        await _insert_memory(memory_db, "b", "python tutorial", now - 86400 * 2, project_id="proj_b")

        results = await search_memories_fts(
            memory_db, "python", limit=10,
            filters={"project_id": "proj_a"},
        )
        ids = [r["id"] for r in results]
        assert "a" in ids
        assert "b" not in ids

    @pytest.mark.asyncio
    async def test_decay_score_is_positive(self, memory_db):
        """返回的 score 字段为正数（衰减后的 BM25 分数）。"""
        from repositories.memories_repo import search_memories_fts

        now = int(time.time())
        await _insert_memory(memory_db, "n1", "python tutorial", now - 86400)

        results = await search_memories_fts(memory_db, "python", limit=10)
        assert len(results) >= 1
        assert results[0]["score"] > 0
