"""tests/unit/test_knowledge_project_isolation.py —— Task 6.5: knowledge_nodes 项目级隔离

覆盖:
  - Schema migration: project_id 列添加到 knowledge_nodes + memories
  - KnowledgeFilter.project_id → to_sql_conditions / to_chroma_where
  - MemoryFilter.project_id → to_sql_conditions / to_chroma_where
  - upsert_knowledge_node 接受 project_id 参数
  - search_knowledge_tree 按 project_id 过滤（NULL = global，对所有项目可见）
  - list_knowledge_roots 按 project_id 过滤

对标 Claude Code CLAUDE.md 项目隔离——global CLAUDE.md 对所有项目生效，
project-level CLAUDE.md 只对该项目生效。NULL project_id = global knowledge。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ============================================================
# Filter 层测试（纯函数，无需 DB）
# ============================================================

class TestKnowledgeFilterProjectId:
    """KnowledgeFilter.project_id 字段——知识节点检索过滤器"""

    def test_default_is_none(self):
        """project_id 默认 None（不过滤）"""
        from services.memory.hybrid_searcher import KnowledgeFilter
        f = KnowledgeFilter()
        assert f.project_id is None

    def test_to_sql_conditions_with_project_id(self):
        """传 project_id 时生成 `(n.project_id = ? OR n.project_id IS NULL)` 条件
        （NULL = global knowledge，对所有项目可见）"""
        from services.memory.hybrid_searcher import KnowledgeFilter
        f = KnowledgeFilter(project_id="proj_a")
        conds, params = f.to_sql_conditions()
        # 应包含 project_id 条件——具体形式为 (n.project_id = ? OR n.project_id IS NULL)
        joined = " AND ".join(conds)
        assert "project_id" in joined
        assert "OR" in joined.upper() or "IS NULL" in joined.upper()
        assert "proj_a" in params

    def test_to_sql_conditions_without_project_id(self):
        """不传 project_id 时无 project_id 条件（向后兼容）"""
        from services.memory.hybrid_searcher import KnowledgeFilter
        f = KnowledgeFilter()
        conds, params = f.to_sql_conditions()
        joined = " AND ".join(conds)
        assert "project_id" not in joined

    def test_to_chroma_where_with_project_id(self):
        """chromadb where 字典包含 project_id（用 $or 表达 "项目 OR global" 语义）
        chromadb 不支持 None 作为 where 比较值，用 "" 作 global sentinel
        (对标 LangChain Chroma / LlamaIndex——NoSQL 文档库不支持 null 的标准模式)"""
        from services.memory.hybrid_searcher import KnowledgeFilter
        f = KnowledgeFilter(project_id="proj_a")
        where = f.to_chroma_where()
        assert where is not None
        # 应有 $or 条件表达 "project_id=proj_a OR project_id=''(global sentinel)"
        or_clause = where.get("$or")
        assert or_clause is not None
        assert {"project_id": "proj_a"} in or_clause
        assert {"project_id": ""} in or_clause  # "" = global sentinel

    def test_to_chroma_where_without_project_id(self):
        """不传 project_id 时 chromadb where 无 project_id 键"""
        from services.memory.hybrid_searcher import KnowledgeFilter
        f = KnowledgeFilter()
        where = f.to_chroma_where()
        # where 可能为 None（无任何过滤）或不含 project_id
        if where is not None:
            assert "project_id" not in where

    def test_combined_with_source_filter(self):
        """project_id 与 source 过滤可同时使用"""
        from services.memory.hybrid_searcher import KnowledgeFilter
        f = KnowledgeFilter(source="conversation", project_id="proj_a")
        conds, params = f.to_sql_conditions()
        assert any("source" in c for c in conds)
        assert any("project_id" in c for c in conds)
        assert "proj_a" in params
        assert "conversation" in params


class TestMemoryFilterProjectId:
    """MemoryFilter.project_id 字段——语义记忆检索过滤器（memories 表）"""

    def test_default_is_none(self):
        from services.memory.hybrid_searcher import MemoryFilter
        f = MemoryFilter()
        assert f.project_id is None

    def test_to_sql_conditions_with_project_id(self):
        """传 project_id 时生成 memories 表的 project_id 过滤条件
        （NULL = global，对所有项目可见）"""
        from services.memory.hybrid_searcher import MemoryFilter
        f = MemoryFilter(project_id="proj_a")
        conds, params = f.to_sql_conditions()
        joined = " AND ".join(conds)
        assert "project_id" in joined
        assert "OR" in joined.upper() or "IS NULL" in joined.upper()
        assert "proj_a" in params

    def test_to_sql_conditions_without_project_id(self):
        from services.memory.hybrid_searcher import MemoryFilter
        f = MemoryFilter()
        conds, params = f.to_sql_conditions()
        joined = " AND ".join(conds)
        assert "project_id" not in joined

    def test_to_chroma_where_with_project_id(self):
        """chromadb where 用 $or 表达 "项目记忆 OR global 记忆" 语义
        chromadb 不支持 None；用 "" 作 global sentinel"""
        from services.memory.hybrid_searcher import MemoryFilter
        f = MemoryFilter(project_id="proj_a")
        where = f.to_chroma_where()
        assert where is not None
        or_clause = where.get("$or")
        assert or_clause is not None
        assert {"project_id": "proj_a"} in or_clause
        assert {"project_id": ""} in or_clause  # "" = global sentinel


# ============================================================
# Schema migration 测试
# ============================================================

@pytest.fixture
async def migrated_db(tmp_path):
    """创建带完整 schema + 列级迁移的 DB（含 project_id 列）"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_proj_iso.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    from repositories.schema import COLUMN_MIGRATIONS, _safe_add_column
    for table, column, definition in COLUMN_MIGRATIONS:
        await _safe_add_column(conn, table, column, definition)
    await conn.commit()
    yield conn
    await conn.close()


async def _get_columns(conn, table):
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in await cursor.fetchall()]


class TestSchemaMigration:
    """project_id 列迁移到 knowledge_nodes + memories"""

    @pytest.mark.asyncio
    async def test_knowledge_nodes_has_project_id_column(self, migrated_db):
        """knowledge_nodes 表有 project_id 列"""
        cols = await _get_columns(migrated_db, "knowledge_nodes")
        assert "project_id" in cols

    @pytest.mark.asyncio
    async def test_memories_has_project_id_column(self, migrated_db):
        """memories 表有 project_id 列"""
        cols = await _get_columns(migrated_db, "memories")
        assert "project_id" in cols

    @pytest.mark.asyncio
    async def test_existing_rows_keep_null_project_id(self, migrated_db):
        """旧数据迁移后 project_id 为 NULL（global，对所有项目可见）"""
        await migrated_db.execute(
            "INSERT INTO knowledge_nodes(id, title, content, source, tags) "
            "VALUES('n1', 'title1', 'content1', 'test', '[]')",
        )
        await migrated_db.commit()
        cursor = await migrated_db.execute(
            "SELECT project_id FROM knowledge_nodes WHERE id='n1'",
        )
        row = await cursor.fetchone()
        assert row[0] is None


# ============================================================
# Repository 层测试
# ============================================================

class TestUpsertKnowledgeNodeWithProjectId:
    """upsert_knowledge_node 接受 project_id 参数"""

    @pytest.mark.asyncio
    async def test_upsert_with_project_id(self, migrated_db):
        from repositories.knowledge_repo import upsert_knowledge_node
        await upsert_knowledge_node(
            migrated_db, "n1", "title", "content",
            source="test", project_id="proj_a",
        )
        cursor = await migrated_db.execute(
            "SELECT project_id FROM knowledge_nodes WHERE id='n1'",
        )
        row = await cursor.fetchone()
        assert row[0] == "proj_a"

    @pytest.mark.asyncio
    async def test_upsert_without_project_id_defaults_null(self, migrated_db):
        """不传 project_id 时为 NULL（向后兼容）"""
        from repositories.knowledge_repo import upsert_knowledge_node
        await upsert_knowledge_node(
            migrated_db, "n2", "title", "content", source="test",
        )
        cursor = await migrated_db.execute(
            "SELECT project_id FROM knowledge_nodes WHERE id='n2'",
        )
        row = await cursor.fetchone()
        assert row[0] is None

    @pytest.mark.asyncio
    async def test_upsert_empty_project_id_treated_as_null(self, migrated_db):
        """空字符串 project_id 视为 NULL（避免空串与 NULL 双语义）"""
        from repositories.knowledge_repo import upsert_knowledge_node
        await upsert_knowledge_node(
            migrated_db, "n3", "title", "content",
            source="test", project_id="",
        )
        cursor = await migrated_db.execute(
            "SELECT project_id FROM knowledge_nodes WHERE id='n3'",
        )
        row = await cursor.fetchone()
        assert row[0] is None

    @pytest.mark.asyncio
    async def test_get_knowledge_node_returns_project_id(self, migrated_db):
        from repositories.knowledge_repo import upsert_knowledge_node, get_knowledge_node
        await upsert_knowledge_node(
            migrated_db, "n4", "title", "content",
            source="test", project_id="proj_a",
        )
        node = await get_knowledge_node(migrated_db, "n4")
        assert node is not None
        assert node.get("project_id") == "proj_a"

    @pytest.mark.asyncio
    async def test_update_existing_node_project_id(self, migrated_db):
        """更新已存在节点时 project_id 也更新"""
        from repositories.knowledge_repo import upsert_knowledge_node
        await upsert_knowledge_node(
            migrated_db, "n5", "title", "content",
            source="test", project_id="proj_a",
        )
        await upsert_knowledge_node(
            migrated_db, "n5", "title2", "content2",
            source="test", project_id="proj_b",
        )
        cursor = await migrated_db.execute(
            "SELECT project_id, title FROM knowledge_nodes WHERE id='n5'",
        )
        row = await cursor.fetchone()
        assert row[0] == "proj_b"
        assert row[1] == "title2"


async def _setup_project_isolation_data(conn):
    """准备测试数据：proj_a 2 条 + proj_b 1 条 + global(NULL) 1 条"""
    from repositories.knowledge_repo import upsert_knowledge_node
    await upsert_knowledge_node(
        conn, "a1", "Python basics", "learn python syntax",
        source="test", project_id="proj_a", tags=["python"],
    )
    await upsert_knowledge_node(
        conn, "a2", "Python advanced", "decorators and metaclasses",
        source="test", project_id="proj_a", tags=["python"],
    )
    await upsert_knowledge_node(
        conn, "b1", "Python other project", "python in different context",
        source="test", project_id="proj_b", tags=["python"],
    )
    await upsert_knowledge_node(
        conn, "g1", "Python global", "python general knowledge",
        source="test", project_id="",  # 空 → NULL → global
        tags=["python"],
    )
    cursor = await conn.execute("SELECT COUNT(*) FROM knowledge_nodes")
    assert (await cursor.fetchone())[0] == 4


class TestSearchKnowledgeTreeWithProjectId:
    """search_knowledge_tree 按 project_id 过滤——NULL = global 可见"""

    @pytest.mark.asyncio
    async def test_search_proj_a_returns_proj_a_and_global(self, migrated_db):
        """proj_a 搜索 → 返回 proj_a 节点 + global 节点，排除 proj_b"""
        await _setup_project_isolation_data(migrated_db)
        from repositories.knowledge_repo import search_knowledge_tree
        results = await search_knowledge_tree(
            migrated_db, "python", limit=10,
            filters={"project_id": "proj_a"},
        )
        ids = {r["id"] for r in results}
        assert "a1" in ids
        assert "a2" in ids
        assert "g1" in ids  # global visible to proj_a
        assert "b1" not in ids  # proj_b excluded

    @pytest.mark.asyncio
    async def test_search_proj_b_returns_proj_b_and_global(self, migrated_db):
        """proj_b 搜索 → 返回 proj_b 节点 + global 节点，排除 proj_a"""
        await _setup_project_isolation_data(migrated_db)
        from repositories.knowledge_repo import search_knowledge_tree
        results = await search_knowledge_tree(
            migrated_db, "python", limit=10,
            filters={"project_id": "proj_b"},
        )
        ids = {r["id"] for r in results}
        assert "b1" in ids
        assert "g1" in ids  # global visible to proj_b
        assert "a1" not in ids
        assert "a2" not in ids

    @pytest.mark.asyncio
    async def test_search_without_project_id_returns_all(self, migrated_db):
        """不传 project_id → 返回全部（向后兼容）"""
        await _setup_project_isolation_data(migrated_db)
        from repositories.knowledge_repo import search_knowledge_tree
        results = await search_knowledge_tree(migrated_db, "python", limit=10)
        ids = {r["id"] for r in results}
        assert ids == {"a1", "a2", "b1", "g1"}

    @pytest.mark.asyncio
    async def test_search_with_empty_project_id_returns_all(self, migrated_db):
        """空字符串 project_id → 视为不过滤（与 None 等价）"""
        await _setup_project_isolation_data(migrated_db)
        from repositories.knowledge_repo import search_knowledge_tree
        results = await search_knowledge_tree(
            migrated_db, "python", limit=10,
            filters={"project_id": ""},
        )
        ids = {r["id"] for r in results}
        assert ids == {"a1", "a2", "b1", "g1"}


async def _setup_roots_data(conn):
    """准备根节点测试数据：proj_a + proj_b + global"""
    from repositories.knowledge_repo import upsert_knowledge_node
    await upsert_knowledge_node(
        conn, "root_a", "Root A", "content a",
        source="test", project_id="proj_a",
    )
    await upsert_knowledge_node(
        conn, "root_b", "Root B", "content b",
        source="test", project_id="proj_b",
    )
    await upsert_knowledge_node(
        conn, "root_global", "Root Global", "content global",
        source="test", project_id="",  # → NULL → global
    )


class TestListKnowledgeRootsWithProjectId:
    """list_knowledge_roots 按 project_id 过滤"""

    @pytest.mark.asyncio
    async def test_list_roots_proj_a(self, migrated_db):
        await _setup_roots_data(migrated_db)
        from repositories.knowledge_repo import list_knowledge_roots
        roots = await list_knowledge_roots(migrated_db, limit=20, project_id="proj_a")
        ids = {r["id"] for r in roots}
        assert "root_a" in ids
        assert "root_global" in ids  # global visible
        assert "root_b" not in ids

    @pytest.mark.asyncio
    async def test_list_roots_no_filter(self, migrated_db):
        await _setup_roots_data(migrated_db)
        from repositories.knowledge_repo import list_knowledge_roots
        roots = await list_knowledge_roots(migrated_db, limit=20)
        ids = {r["id"] for r in roots}
        assert ids == {"root_a", "root_b", "root_global"}

    @pytest.mark.asyncio
    async def test_list_roots_empty_project_id(self, migrated_db):
        """空字符串 project_id → 不过滤（与 None 等价）"""
        await _setup_roots_data(migrated_db)
        from repositories.knowledge_repo import list_knowledge_roots
        roots = await list_knowledge_roots(migrated_db, limit=20, project_id="")
        ids = {r["id"] for r in roots}
        assert ids == {"root_a", "root_b", "root_global"}
