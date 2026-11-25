"""tests/unit/test_memory_tree_decay.py —— M2: MemoryTree 时间衰减策略

覆盖 Ebbinghaus 时间衰减 + 访问增强：
- compute_decay_factor: 纯函数，按 age_days 计算衰减权重
- retrieve_context: 旧节点排名低于新节点（同 base score）；命中后 last_accessed 更新 + score 提升
- periodic_maintenance: 超期未访问的节点被清理（age-based prune）

对标人类记忆 Ebbinghaus 遗忘曲线：旧记忆慢慢失权，重新访问则增强。
修复现有"假衰减"（均匀 score*0.95，与时间/访问无关）。
"""
import math
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def memory_tree_db(tmp_path):
    """创建带 SCHEMA_SQL 的 in-memory DB + MemoryTree 实例（无 vector_repo）"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_decay.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    # 执行列级迁移确保 last_accessed 列存在
    from repositories.schema import COLUMN_MIGRATIONS, _safe_add_column
    for table, column, definition in COLUMN_MIGRATIONS:
        await _safe_add_column(conn, table, column, definition)
    await conn.commit()
    from services.memory.memory_tree import MemoryTree
    mt = MemoryTree(db=conn, summarizer=None, vector_repo=None)
    yield conn, mt
    await conn.close()


async def _insert_node(conn, node_id, title, content, score=1.0, created_at=None, last_accessed=None):
    """直接插入知识节点（绕过 upsert 的 now 时间戳），用于测试旧/新节点"""
    import json
    now = int(time.time())
    await conn.execute(
        "INSERT INTO knowledge_nodes(id, title, content, score, source, source_ref, tags, "
        "created_at, updated_at, last_accessed) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (node_id, title, content, score, "test", "", "[]",
         created_at or now, now, last_accessed),
    )
    # 同步 FTS5
    await conn.execute(
        "INSERT INTO knowledge_fts(node_id, title, content, tags) VALUES(?,?,?,?)",
        (node_id, title, content, "[]"),
    )
    await conn.commit()


class TestComputeDecayFactor:
    """compute_decay_factor 纯函数测试"""

    def test_zero_age_no_decay(self):
        """刚访问的节点（age=0）衰减因子 = 1.0"""
        from services.memory.memory_tree import compute_decay_factor
        assert compute_decay_factor(0, 0.05) == pytest.approx(1.0)

    def test_positive_age_decays(self):
        """age > 0 衰减因子 < 1.0"""
        from services.memory.memory_tree import compute_decay_factor
        assert compute_decay_factor(30, 0.05) < 1.0
        assert compute_decay_factor(30, 0.05) > 0  # 但不为 0

    def test_older_age_lower_factor(self):
        """越旧衰减越多（单调递减）"""
        from services.memory.memory_tree import compute_decay_factor
        factor_recent = compute_decay_factor(1, 0.05)
        factor_old = compute_decay_factor(90, 0.05)
        assert factor_recent > factor_old

    def test_matches_ebbinghaus_formula(self):
        """符合 exp(-rate * age) 公式"""
        from services.memory.memory_tree import compute_decay_factor
        assert compute_decay_factor(30, 0.05) == pytest.approx(math.exp(-0.05 * 30))

    def test_higher_rate_faster_decay(self):
        """衰减率越大，衰减越快"""
        from services.memory.memory_tree import compute_decay_factor
        slow = compute_decay_factor(30, 0.01)
        fast = compute_decay_factor(30, 0.20)
        assert slow > fast


class TestLastAccessedColumn:
    """Schema 迁移——last_accessed 列存在"""

    async def test_last_accessed_column_exists(self, memory_tree_db):
        conn, _ = memory_tree_db
        cursor = await conn.execute("PRAGMA table_info(knowledge_nodes)")
        cols = [row[1] for row in await cursor.fetchall()]
        assert "last_accessed" in cols

    async def test_last_accessed_defaults_null(self, memory_tree_db):
        """新插入节点 last_accessed 默认 NULL（用 created_at 回退）"""
        conn, _ = memory_tree_db
        await _insert_node(conn, "n1", "test", "content", created_at=int(time.time()))
        cursor = await conn.execute("SELECT last_accessed FROM knowledge_nodes WHERE id=?", ("n1",))
        row = await cursor.fetchone()
        assert row[0] is None


class TestRetrieveContextDecay:
    """retrieve_context 时间衰减排序——旧节点排名低于新节点"""

    async def test_recent_node_ranks_higher_than_old(self, memory_tree_db):
        """同 base score 下，最近访问的节点应排在旧节点前"""
        conn, mt = memory_tree_db
        now = int(time.time())
        # 旧节点：30 天前访问
        await _insert_node(conn, "old_node", "important query topic",
                           "old content about query topic",
                           score=1.0, created_at=now - 86400 * 60,
                           last_accessed=now - 86400 * 30)
        # 新节点：刚访问
        await _insert_node(conn, "new_node", "important query topic",
                           "new content about query topic",
                           score=1.0, created_at=now - 86400 * 5,
                           last_accessed=now - 86400 * 1)
        result = await mt.retrieve_context("query topic", limit=5)
        # 两个都应被检索到（FTS5 匹配 "query topic"）
        assert "new_node" in result or "new content" in result
        assert "old_node" in result or "old content" in result
        # 新节点应在旧节点前面
        new_pos = result.find("new content") if "new content" in result else result.find("new_node")
        old_pos = result.find("old content") if "old content" in result else result.find("old_node")
        if new_pos >= 0 and old_pos >= 0:
            assert new_pos < old_pos, "最近访问的节点应排在旧节点前"

    async def test_high_score_old_can_outrank_low_score_recent(self, memory_tree_db):
        """高 base score 的旧节点仍可能胜过低 base score 的新节点（衰减非唯一因子）"""
        conn, mt = memory_tree_db
        now = int(time.time())
        # 旧但高 score
        await _insert_node(conn, "old_high", "critical query topic",
                           "old high content query topic",
                           score=1.0, created_at=now - 86400 * 100,
                           last_accessed=now - 86400 * 100)
        # 新但低 score
        await _insert_node(conn, "new_low", "minor query topic",
                           "new low content query topic",
                           score=0.1, created_at=now - 3600,
                           last_accessed=now - 3600)
        result = await mt.retrieve_context("query topic", limit=5)
        # 两者都应出现
        assert "query topic" in result


class TestAccessBoost:
    """retrieve_context 命中后访问增强——last_accessed 更新 + score 提升"""

    async def test_retrieval_updates_last_accessed(self, memory_tree_db):
        """检索命中后，节点的 last_accessed 应更新为当前时间"""
        conn, mt = memory_tree_db
        now = int(time.time())
        old_time = now - 86400 * 50  # 50 天前
        await _insert_node(conn, "boost_node", "unique boost keyword",
                           "content with unique boost keyword",
                           score=0.5, created_at=old_time, last_accessed=old_time)
        # 检索前 last_accessed 是旧的
        cursor = await conn.execute("SELECT last_accessed FROM knowledge_nodes WHERE id=?", ("boost_node",))
        before = (await cursor.fetchone())[0]
        assert before == old_time

        await mt.retrieve_context("unique boost keyword", limit=5)

        # 检索后 last_accessed 应更新为接近 now
        cursor = await conn.execute("SELECT last_accessed FROM knowledge_nodes WHERE id=?", ("boost_node",))
        after = (await cursor.fetchone())[0]
        assert after is not None
        assert after > old_time + 86400  # 明显比旧时间新

    async def test_retrieval_boosts_score(self, memory_tree_db):
        """检索命中后，score 应提升（ capped at 1.0）"""
        conn, mt = memory_tree_db
        now = int(time.time())
        await _insert_node(conn, "score_node", "score boost keyword xyz",
                           "content score boost keyword xyz",
                           score=0.5, created_at=now, last_accessed=now)
        await mt.retrieve_context("score boost keyword xyz", limit=5)
        cursor = await conn.execute("SELECT score FROM knowledge_nodes WHERE id=?", ("score_node",))
        after_score = (await cursor.fetchone())[0]
        assert after_score > 0.5, "命中后 score 应提升"

    async def test_score_boost_capped_at_one(self, memory_tree_db):
        """score 提升上限 1.0（不超过）"""
        conn, mt = memory_tree_db
        now = int(time.time())
        await _insert_node(conn, "cap_node", "cap boost keyword abc",
                           "content cap boost keyword abc",
                           score=0.95, created_at=now, last_accessed=now)
        await mt.retrieve_context("cap boost keyword abc", limit=5)
        cursor = await conn.execute("SELECT score FROM knowledge_nodes WHERE id=?", ("cap_node",))
        after_score = (await cursor.fetchone())[0]
        assert after_score <= 1.0


class TestAgeBasedPrune:
    """periodic_maintenance age-based 清理——超期未访问的节点被删除"""

    async def test_old_unaccessed_node_pruned(self, memory_tree_db):
        """超过 MAX_AGE_DAYS 未访问的节点被清理"""
        conn, mt = memory_tree_db
        now = int(time.time())
        # 200 天前访问的节点（超过 90 天阈值）
        await _insert_node(conn, "ancient", "ancient topic zzz",
                           "ancient content zzz",
                           created_at=now - 86400 * 200,
                           last_accessed=now - 86400 * 200)
        result = await mt.periodic_maintenance()
        assert result["pruned"] >= 1
        cursor = await conn.execute("SELECT id FROM knowledge_nodes WHERE id=?", ("ancient",))
        assert (await cursor.fetchone()) is None

    async def test_recent_node_not_pruned(self, memory_tree_db):
        """最近访问的节点不被清理"""
        conn, mt = memory_tree_db
        now = int(time.time())
        await _insert_node(conn, "recent", "recent topic yyy",
                           "recent content yyy",
                           created_at=now - 86400 * 10,
                           last_accessed=now - 86400 * 2)
        await mt.periodic_maintenance()
        cursor = await conn.execute("SELECT id FROM knowledge_nodes WHERE id=?", ("recent",))
        assert (await cursor.fetchone()) is not None

    async def test_null_last_accessed_uses_created_at(self, memory_tree_db):
        """last_accessed 为 NULL 时用 created_at 判断年龄"""
        conn, mt = memory_tree_db
        now = int(time.time())
        # created_at 很旧，last_accessed = NULL
        await _insert_node(conn, "null_access", "null access topic www",
                           "null access content www",
                           created_at=now - 86400 * 200,
                           last_accessed=None)
        await mt.periodic_maintenance()
        cursor = await conn.execute("SELECT id FROM knowledge_nodes WHERE id=?", ("null_access",))
        assert (await cursor.fetchone()) is None, "last_accessed NULL 时应用 created_at 判断年龄"


class TestRetrieveContextHybridIntegration:
    """Batch 1.2: retrieve_context 应调用 hybrid_search_knowledge 而非自行拼接向量+FTS5。"""

    async def test_retrieve_context_uses_hybrid_search_knowledge(self, memory_tree_db, monkeypatch):
        """monkeypatch hybrid_search_knowledge，验证 retrieve_context 把搜索委托给它。"""
        conn, mt = memory_tree_db
        calls = []

        async def fake_hybrid_search_knowledge(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return [
                {
                    "id": "n1",
                    "title": "hybrid result",
                    "content": "content from hybrid",
                    "score": 0.9,
                    "sources": ["vector"],
                    "tags": [],
                }
            ]

        import services.memory.hybrid_searcher
        monkeypatch.setattr(
            services.memory.hybrid_searcher, "hybrid_search_knowledge",
            fake_hybrid_search_knowledge,
        )

        result = await mt.retrieve_context("query", limit=3, project_id="proj1")

        assert len(calls) == 1
        assert calls[0]["kwargs"]["limit"] == 3
        assert calls[0]["kwargs"]["filters"] == {"project_id": "proj1"}
        assert calls[0]["kwargs"]["return_raw"] is True
        # 返回结果中应包含 hybrid 搜索结果的内容
        assert "hybrid result" in result
        assert "content from hybrid" in result

    async def test_retrieve_context_passes_project_id_to_filter(self, memory_tree_db, monkeypatch):
        """retrieve_context 的 project_id 应传给 hybrid_search_knowledge filters。"""
        conn, mt = memory_tree_db
        captured = []

        async def fake_hybrid_search_knowledge(*args, **kwargs):
            captured.append(kwargs.get("filters"))
            return []

        import services.memory.hybrid_searcher
        monkeypatch.setattr(
            services.memory.hybrid_searcher, "hybrid_search_knowledge",
            fake_hybrid_search_knowledge,
        )

        await mt.retrieve_context("query", project_id="D:\\MyProject")
        assert captured == [{"project_id": "D:\\MyProject"}]
