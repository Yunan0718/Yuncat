"""tests/unit/test_knowledge_search.py —— Batch 2.4: 知识树 LIKE fallback 加相关性

覆盖:
  - LIKE fallback 路径按 query 与 title/content 的 token 匹配度排序
  - title 命中权重高于 content
  - 多个 query token 命中得分更高
  - project_id 过滤在 fallback 中仍然生效
  - 无匹配 query 返回空列表

通过 monkeypatch `_sanitize_fts5_query` 强制走 LIKE fallback 路径，避免测试
依赖 FTS5 对特殊字符的具体行为。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def migrated_db(tmp_path):
    """创建带完整 schema + 列级迁移的 DB。"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_knowledge_search.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    from repositories.schema import COLUMN_MIGRATIONS, _safe_add_column
    for table, column, definition in COLUMN_MIGRATIONS:
        await _safe_add_column(conn, table, column, definition)
    await conn.commit()
    yield conn
    await conn.close()


@pytest.fixture
def force_like_fallback(monkeypatch):
    """强制 search_knowledge_tree 走 LIKE fallback 路径。"""
    from repositories import knowledge_repo
    monkeypatch.setattr(
        knowledge_repo, "_sanitize_fts5_query",
        lambda q: "zzz_no_match_placeholder",
    )


# ═══════════════════════════════════════════════════════════════
# Batch 2.4: LIKE fallback 相关性排序
# ═══════════════════════════════════════════════════════════════

class TestLikeFallbackRelevanceRanking:
    """LIKE fallback 路径按 query-token 匹配度排序"""

    @pytest.mark.asyncio
    async def test_like_fallback_sorts_by_relevance(self, migrated_db, force_like_fallback):
        """命中次数多的文档排在前面，不受 n.score 影响。"""
        from repositories.knowledge_repo import upsert_knowledge_node, search_knowledge_tree

        await upsert_knowledge_node(
            migrated_db, "high", "Python tutorial",
            "Learn python with python examples and python best practices",
            source="test", score=0.1,
        )
        await upsert_knowledge_node(
            migrated_db, "medium", "Python guide",
            "A general guide for programming",
            source="test", score=0.5,
        )
        await upsert_knowledge_node(
            migrated_db, "low", "Rust programming",
            "Systems programming with Rust",
            source="test", score=1.0,
        )

        results = await search_knowledge_tree(migrated_db, "python", limit=10)
        ids = [r["id"] for r in results]
        assert ids[:2] == ["high", "medium"]
        assert "low" not in ids

    @pytest.mark.asyncio
    async def test_like_fallback_title_weight_higher_than_content(self, migrated_db, force_like_fallback):
        """title 命中权重 2 > content 命中权重 1。"""
        from repositories.knowledge_repo import upsert_knowledge_node, search_knowledge_tree

        # title 命中 1 次 → 2 分；content 未命中
        await upsert_knowledge_node(
            migrated_db, "title_hit", "Python tutorial",
            "A general guide",
            source="test", score=0.1,
        )
        # title 未命中；content 命中 3 次 → 3 分
        await upsert_knowledge_node(
            migrated_db, "content_hit", "General guide",
            "python python python",
            source="test", score=0.9,
        )

        results = await search_knowledge_tree(migrated_db, "python", limit=10)
        ids = [r["id"] for r in results]
        # content_hit 3 分 > title_hit 2 分，应该排第一
        assert ids[:2] == ["content_hit", "title_hit"]

    @pytest.mark.asyncio
    async def test_like_fallback_multi_token_scores_higher(self, migrated_db, force_like_fallback):
        """多个 query token 都命中的文档得分更高。"""
        from repositories.knowledge_repo import upsert_knowledge_node, search_knowledge_tree

        await upsert_knowledge_node(
            migrated_db, "both", "Python asyncio",
            "Guide for python asyncio",
            source="test", score=0.1,
        )
        await upsert_knowledge_node(
            migrated_db, "single", "Python tutorial",
            "Learn python",
            source="test", score=0.9,
        )

        results = await search_knowledge_tree(migrated_db, "python asyncio", limit=10)
        ids = [r["id"] for r in results]
        assert ids[0] == "both"

    @pytest.mark.asyncio
    async def test_like_fallback_returns_empty_for_no_match(self, migrated_db, force_like_fallback):
        """无匹配文档时返回空列表。"""
        from repositories.knowledge_repo import upsert_knowledge_node, search_knowledge_tree

        await upsert_knowledge_node(
            migrated_db, "n1", "Python tutorial",
            "Learn python",
            source="test",
        )

        results = await search_knowledge_tree(migrated_db, "rust", limit=10)
        assert results == []

    @pytest.mark.asyncio
    async def test_like_fallback_respects_project_id_filter(self, migrated_db, force_like_fallback):
        """LIKE fallback 仍然应用 project_id 过滤。"""
        from repositories.knowledge_repo import upsert_knowledge_node, search_knowledge_tree

        await upsert_knowledge_node(
            migrated_db, "a1", "Python basics",
            "learn python",
            source="test", project_id="proj_a",
        )
        await upsert_knowledge_node(
            migrated_db, "b1", "Python other",
            "python in different context",
            source="test", project_id="proj_b",
        )
        await upsert_knowledge_node(
            migrated_db, "g1", "Python global",
            "python general knowledge",
            source="test", project_id="",
        )

        results = await search_knowledge_tree(
            migrated_db, "python", limit=10,
            filters={"project_id": "proj_a"},
        )
        ids = {r["id"] for r in results}
        assert "a1" in ids
        assert "g1" in ids
        assert "b1" not in ids

    @pytest.mark.asyncio
    async def test_like_fallback_score_is_positive(self, migrated_db, force_like_fallback):
        """LIKE fallback 返回的 score 为正数（相关性分数取负转正）。"""
        from repositories.knowledge_repo import upsert_knowledge_node, search_knowledge_tree

        await upsert_knowledge_node(
            migrated_db, "n1", "Python tutorial",
            "Learn python",
            source="test",
        )

        results = await search_knowledge_tree(migrated_db, "python", limit=10)
        assert len(results) >= 1
        assert results[0]["score"] > 0
