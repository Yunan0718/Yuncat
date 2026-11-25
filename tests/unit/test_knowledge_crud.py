"""tests/unit/test_knowledge_crud.py —— Task 7.23: 知识节点编辑/删除 CRUD

覆盖:
  - update_knowledge_node: partial update（title/content/tags/多字段）
  - update_knowledge_node: 不存在节点返回 False
  - update_knowledge_node: 无效字段返回 False
  - update_knowledge_node: tags list → JSON 序列化
  - update_knowledge_node: 自动更新 updated_at
  - delete_knowledge_subtree: 删除单节点
  - delete_knowledge_subtree: 级联删除子孙
  - delete_knowledge_subtree: 不存在节点返回 0

对标 update_memory 的 allowed-set 模式——白名单过滤 + 动态 SET 子句。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def migrated_db(tmp_path):
    """创建带完整 schema + 列级迁移的 DB（含 project_id 列）"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_knowledge_crud.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    from repositories.schema import COLUMN_MIGRATIONS, _safe_add_column
    for table, column, definition in COLUMN_MIGRATIONS:
        await _safe_add_column(conn, table, column, definition)
    await conn.commit()
    yield conn
    await conn.close()


# ============================================================
# update_knowledge_node 单元测试
# ============================================================

class TestUpdateKnowledgeNode:
    """Task 7.23: update_knowledge_node——partial update 白名单模式"""

    @pytest.mark.asyncio
    async def test_update_title_only(self, migrated_db):
        """只更新 title 字段，其他字段保持不变"""
        from repositories.knowledge_repo import upsert_knowledge_node, update_knowledge_node, get_knowledge_node
        await upsert_knowledge_node(migrated_db, "n1", "原标题", "原内容", source="test")
        ok = await update_knowledge_node(migrated_db, "n1", title="新标题")
        assert ok is True
        node = await get_knowledge_node(migrated_db, "n1")
        assert node["title"] == "新标题"
        assert node["content"] == "原内容"  # 未修改

    @pytest.mark.asyncio
    async def test_update_content_only(self, migrated_db):
        """只更新 content 字段"""
        from repositories.knowledge_repo import upsert_knowledge_node, update_knowledge_node, get_knowledge_node
        await upsert_knowledge_node(migrated_db, "n1", "标题", "原内容", source="test")
        ok = await update_knowledge_node(migrated_db, "n1", content="修改后的内容")
        assert ok is True
        node = await get_knowledge_node(migrated_db, "n1")
        assert node["content"] == "修改后的内容"
        assert node["title"] == "标题"  # 未修改

    @pytest.mark.asyncio
    async def test_update_tags_list_to_json(self, migrated_db):
        """tags 传入 list 时自动 JSON 序列化存储"""
        from repositories.knowledge_repo import upsert_knowledge_node, update_knowledge_node
        await upsert_knowledge_node(migrated_db, "n1", "标题", "内容", source="test", tags=["old"])
        ok = await update_knowledge_node(migrated_db, "n1", tags=["python", "testing"])
        assert ok is True
        cursor = await migrated_db.execute("SELECT tags FROM knowledge_nodes WHERE id='n1'")
        row = await cursor.fetchone()
        import json
        assert json.loads(row[0]) == ["python", "testing"]

    @pytest.mark.asyncio
    async def test_update_multiple_fields(self, migrated_db):
        """同时更新多个字段"""
        from repositories.knowledge_repo import upsert_knowledge_node, update_knowledge_node, get_knowledge_node
        await upsert_knowledge_node(migrated_db, "n1", "原标题", "原内容", source="test", score=0.5)
        ok = await update_knowledge_node(migrated_db, "n1", title="新标题", content="新内容", score=0.9)
        assert ok is True
        node = await get_knowledge_node(migrated_db, "n1")
        assert node["title"] == "新标题"
        assert node["content"] == "新内容"
        assert node["score"] == 0.9

    @pytest.mark.asyncio
    async def test_update_nonexistent_node_returns_false(self, migrated_db):
        """不存在的节点返回 False"""
        from repositories.knowledge_repo import update_knowledge_node
        ok = await update_knowledge_node(migrated_db, "nonexistent", title="标题")
        assert ok is False

    @pytest.mark.asyncio
    async def test_update_no_valid_fields_returns_false(self, migrated_db):
        """所有字段都不在白名单内时返回 False"""
        from repositories.knowledge_repo import upsert_knowledge_node, update_knowledge_node
        await upsert_knowledge_node(migrated_db, "n1", "标题", "内容", source="test")
        # id, updated_at, indexed_at 等不在 allowed 集合中
        ok = await update_knowledge_node(migrated_db, "n1", unknown_field="value", id="hacked")
        assert ok is False

    @pytest.mark.asyncio
    async def test_update_none_values_ignored(self, migrated_db):
        """None 值的字段被忽略（不更新）"""
        from repositories.knowledge_repo import upsert_knowledge_node, update_knowledge_node, get_knowledge_node
        await upsert_knowledge_node(migrated_db, "n1", "原标题", "原内容", source="test")
        # title=None 应被忽略，content 有值应更新
        ok = await update_knowledge_node(migrated_db, "n1", title=None, content="新内容")
        assert ok is True
        node = await get_knowledge_node(migrated_db, "n1")
        assert node["title"] == "原标题"  # None 被忽略
        assert node["content"] == "新内容"

    @pytest.mark.asyncio
    async def test_update_auto_updates_updated_at(self, migrated_db):
        """更新时自动设置 updated_at 时间戳"""
        from repositories.knowledge_repo import upsert_knowledge_node, update_knowledge_node, get_knowledge_node
        await upsert_knowledge_node(migrated_db, "n1", "标题", "内容", source="test")
        before = (await get_knowledge_node(migrated_db, "n1"))["updated_at"]
        time.sleep(1.1)  # 确保时间戳差异 >= 1 秒
        await update_knowledge_node(migrated_db, "n1", title="新标题")
        after = (await get_knowledge_node(migrated_db, "n1"))["updated_at"]
        assert after > before

    @pytest.mark.asyncio
    async def test_update_project_id_empty_string_to_none(self, migrated_db):
        """project_id 空字符串统一转为 None（与 upsert 一致）"""
        from repositories.knowledge_repo import upsert_knowledge_node, update_knowledge_node
        await upsert_knowledge_node(migrated_db, "n1", "标题", "内容", source="test", project_id="proj_a")
        ok = await update_knowledge_node(migrated_db, "n1", project_id="")
        assert ok is True
        cursor = await migrated_db.execute("SELECT project_id FROM knowledge_nodes WHERE id='n1'")
        row = await cursor.fetchone()
        assert row[0] is None  # 空字符串 → None


# ============================================================
# delete_knowledge_subtree 单元测试
# ============================================================

class TestDeleteKnowledgeSubtree:
    """delete_knowledge_subtree——级联删除子树"""

    @pytest.mark.asyncio
    async def test_delete_single_node_no_children(self, migrated_db):
        """删除无子节点的节点——返回 1"""
        from repositories.knowledge_repo import upsert_knowledge_node, delete_knowledge_subtree
        await upsert_knowledge_node(migrated_db, "n1", "标题", "内容", source="test")
        count = await delete_knowledge_subtree(migrated_db, "n1")
        assert count == 1
        cursor = await migrated_db.execute("SELECT COUNT(*) FROM knowledge_nodes WHERE id='n1'")
        assert (await cursor.fetchone())[0] == 0

    @pytest.mark.asyncio
    async def test_delete_cascade_children(self, migrated_db):
        """删除有子节点的节点——级联删除所有子孙"""
        from repositories.knowledge_repo import upsert_knowledge_node, delete_knowledge_subtree
        # n1 → n2 → n3 (三代)
        await upsert_knowledge_node(migrated_db, "n1", "根", "内容1", source="test")
        await upsert_knowledge_node(migrated_db, "n2", "子", "内容2", source="test", parent_id="n1")
        await upsert_knowledge_node(migrated_db, "n3", "孙", "内容3", source="test", parent_id="n2")
        count = await delete_knowledge_subtree(migrated_db, "n1")
        assert count == 3  # n1 + n2 + n3
        cursor = await migrated_db.execute("SELECT COUNT(*) FROM knowledge_nodes WHERE id IN ('n1','n2','n3')")
        assert (await cursor.fetchone())[0] == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_zero(self, migrated_db):
        """删除不存在的节点返回 0"""
        from repositories.knowledge_repo import delete_knowledge_subtree
        count = await delete_knowledge_subtree(migrated_db, "nonexistent")
        assert count == 0


# ============================================================
# Batch 2.3: BM25 排序测试
# ============================================================

class TestSearchKnowledgeTreeBm25Ranking:
    """Batch 2.3: search_knowledge_tree 按 BM25 相关性排序"""

    @pytest.mark.asyncio
    async def test_bm25_sorts_by_relevance(self, migrated_db):
        """BM25 把查询词命中更多次的文档排在前面（不受 n.score 影响）。"""
        from repositories.knowledge_repo import upsert_knowledge_node, search_knowledge_tree

        # 高相关：title + content 都多次命中 python
        await upsert_knowledge_node(
            migrated_db, "high", "Python tutorial",
            "Learn python with python examples and python best practices",
            source="test", score=0.1,
        )
        # 中相关：title 命中 python，content 不含
        await upsert_knowledge_node(
            migrated_db, "medium", "Python guide",
            "A general guide for programming",
            source="test", score=0.5,
        )
        # 低相关：title/content 都不含 python，只靠 score 高
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
    async def test_bm25_score_is_positive(self, migrated_db):
        """返回的 score 字段为 BM25 取负后的正数。"""
        from repositories.knowledge_repo import upsert_knowledge_node, search_knowledge_tree

        await upsert_knowledge_node(
            migrated_db, "n1", "Python tutorial",
            "Learn python with python examples",
            source="test",
        )
        results = await search_knowledge_tree(migrated_db, "python", limit=5)
        assert len(results) >= 1
        assert results[0]["score"] > 0

    @pytest.mark.asyncio
    async def test_bm25_tie_breaker_uses_node_score(self, migrated_db):
        """BM25 相同时用 n.score 作为 tie-breaker（DESC）。"""
        from repositories.knowledge_repo import upsert_knowledge_node, search_knowledge_tree

        # 两个节点 title/content 几乎一样，BM25 分数会相同或非常接近
        await upsert_knowledge_node(
            migrated_db, "a", "Python tutorial",
            "Learn python",
            source="test", score=0.9,
        )
        await upsert_knowledge_node(
            migrated_db, "b", "Python tutorial",
            "Learn python",
            source="test", score=0.1,
        )

        results = await search_knowledge_tree(migrated_db, "python", limit=10)
        ids = [r["id"] for r in results]
        assert ids.index("a") < ids.index("b")
