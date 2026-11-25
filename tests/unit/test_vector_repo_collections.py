"""tests/unit/test_vector_repo_collections.py —— sqlite-vec 表隔离验证

原 Batch 2.2 测试基于 chromadb collection 架构（yuncat_memories /
yuncat_knowledge 两个 collection 对象）；迁移到 sqlite-vec 后，
隔离语义由物理表承担：

- index_memory / index_batch → 只写 memory_vec + memory_vec_map
- index_knowledge → 只写 knowledge_vec + knowledge_vec_map
- search → 只查 memories 主表；search_knowledge → 只查 knowledge_nodes
- delete_* → 只删对应 vec/map 表
- flush → 同时刷新两个 pending batch

覆盖：
- 数据隔离（index/search/delete 各走各的表）
- 知识节点 metadata 走 KnowledgeVectorDoc 校验（project_id 保留）
- flush 双 batch 语义
"""
import os
import sys

import aiosqlite
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from config import VEC_DIMENSION

_VEC_DDL = f"CREATE VIRTUAL TABLE {{name}} USING vec0(embedding float[{VEC_DIMENSION}])"


class _FakeEmbedding:
    """固定向量 embedding provider——避免真实模型调用。"""

    def embed_documents(self, texts):
        return [[0.1] * VEC_DIMENSION for _ in texts]

    def embed_query(self, text):
        return [0.1] * VEC_DIMENSION


async def _make_repo():
    """创建内存库 + 最小 schema（vec0/map/主表）+ mock embedding。"""
    conn = await aiosqlite.connect(":memory:")
    await conn.enable_load_extension(True)
    import sqlite_vec
    await conn.load_extension(sqlite_vec.loadable_path())

    await conn.execute(_VEC_DDL.format(name="memory_vec"))
    await conn.execute(_VEC_DDL.format(name="knowledge_vec"))
    await conn.execute(
        "CREATE TABLE memory_vec_map ("
        " vec_rowid INTEGER PRIMARY KEY, memory_id TEXT NOT NULL UNIQUE, content_hash TEXT)"
    )
    await conn.execute(
        "CREATE TABLE knowledge_vec_map ("
        " vec_rowid INTEGER PRIMARY KEY, node_id TEXT NOT NULL UNIQUE, content_hash TEXT)"
    )
    await conn.execute(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL, source TEXT,"
        " owner TEXT, memory_type TEXT DEFAULT 'fact', verified INTEGER DEFAULT 0, project_id TEXT)"
    )
    await conn.execute(
        "CREATE TABLE knowledge_nodes (id TEXT PRIMARY KEY, title TEXT, content TEXT NOT NULL,"
        " source TEXT, source_ref TEXT, tags TEXT DEFAULT '[]', project_id TEXT)"
    )
    await conn.commit()

    from repositories.vector_repo import VectorRepository
    repo = VectorRepository(db=conn)
    repo._embedding_provider = _FakeEmbedding()
    return repo, conn


async def _fetch_ids(conn, sql):
    cur = await conn.execute(sql)
    rows = await cur.fetchall()
    return [r[0] for r in rows]


class TestTableIsolation:
    """index/search/delete 只操作各自的 vec/map 表。"""

    @pytest.mark.anyio
    async def test_index_memory_goes_to_memory_vec(self):
        """index_memory 只写入 memory_vec_map。"""
        repo, conn = await _make_repo()
        await repo.index_memory("m1", "memory content", metadata={"source": "conversation"})
        await repo.flush()

        assert await _fetch_ids(conn, "SELECT memory_id FROM memory_vec_map") == ["m1"]
        assert await _fetch_ids(conn, "SELECT node_id FROM knowledge_vec_map") == []

    @pytest.mark.anyio
    async def test_index_knowledge_goes_to_knowledge_vec(self):
        """index_knowledge 只写入 knowledge_vec_map。"""
        repo, conn = await _make_repo()
        await repo.index_knowledge(
            "k1", "knowledge content",
            metadata={"title": "title", "source": "memory_tree", "tags": ["a", "b"]},
        )
        await repo.flush()

        assert await _fetch_ids(conn, "SELECT node_id FROM knowledge_vec_map") == ["k1"]
        assert await _fetch_ids(conn, "SELECT memory_id FROM memory_vec_map") == []

    @pytest.mark.anyio
    async def test_index_batch_still_goes_to_memory_vec(self):
        """向后兼容：index_batch 继续写入 memory_vec。"""
        repo, conn = await _make_repo()
        await repo.index_batch([("m2", "batch content", {"source": "test"})])
        await repo.flush()

        assert await _fetch_ids(conn, "SELECT memory_id FROM memory_vec_map") == ["m2"]
        assert await _fetch_ids(conn, "SELECT node_id FROM knowledge_vec_map") == []

    @pytest.mark.anyio
    async def test_search_uses_memory_tables(self):
        """search 只检索 memories 主表（不触 knowledge 表）。"""
        repo, conn = await _make_repo()
        await conn.execute(
            "INSERT INTO memories(id, content, source) VALUES ('m1', 'memory content', 'conversation')"
        )
        await conn.commit()
        await repo.index_memory("m1", "memory content", metadata={"source": "conversation"})
        await repo.flush()

        results = await repo.search("query", limit=5, filters={"source": "conversation"})
        assert [r["id"] for r in results] == ["m1"]
        # 未索引任何 knowledge——knowledge 表必须无写入
        assert await _fetch_ids(conn, "SELECT node_id FROM knowledge_vec_map") == []

    @pytest.mark.anyio
    async def test_search_knowledge_uses_knowledge_tables(self):
        """search_knowledge 只检索 knowledge_nodes 主表。"""
        repo, conn = await _make_repo()
        await conn.execute(
            "INSERT INTO knowledge_nodes(id, title, content, source) "
            "VALUES ('k1', 'T', 'knowledge content', 'memory_tree')"
        )
        await conn.commit()
        await repo.index_knowledge(
            "k1", "knowledge content",
            metadata={"title": "T", "source": "memory_tree"},
        )
        await repo.flush()

        results = await repo.search_knowledge("query", limit=5, filters={"source": "memory_tree"})
        assert [r["id"] for r in results] == ["k1"]
        assert await _fetch_ids(conn, "SELECT memory_id FROM memory_vec_map") == []

    @pytest.mark.anyio
    async def test_delete_memory_only_deletes_memory(self):
        """delete_memory 只删 memory_vec/map，不碰 knowledge 表。"""
        repo, conn = await _make_repo()
        await repo.index_memory("m1", "m")
        await repo.index_knowledge("k1", "k")
        await repo.flush()

        await repo.delete_memory("m1")
        assert await _fetch_ids(conn, "SELECT memory_id FROM memory_vec_map") == []
        assert await _fetch_ids(conn, "SELECT node_id FROM knowledge_vec_map") == ["k1"]

    @pytest.mark.anyio
    async def test_delete_knowledge_only_deletes_knowledge(self):
        """delete_knowledge 只删 knowledge_vec/map，不碰 memory 表。"""
        repo, conn = await _make_repo()
        await repo.index_memory("m1", "m")
        await repo.index_knowledge("k1", "k")
        await repo.flush()

        await repo.delete_knowledge("k1")
        assert await _fetch_ids(conn, "SELECT node_id FROM knowledge_vec_map") == []
        assert await _fetch_ids(conn, "SELECT memory_id FROM memory_vec_map") == ["m1"]


class TestKnowledgeMetadataValidation:
    """知识节点 metadata 校验与 project_id 保留。"""

    @pytest.mark.anyio
    async def test_validate_knowledge_metadata_fills_defaults(self):
        """_validate_knowledge_metadata 补齐默认字段（source/source_ref/tags/project_id）。"""
        from repositories.vector_repo import VectorRepository
        repo = VectorRepository()
        meta = repo._validate_knowledge_metadata({"title": "My Title", "tags": ["x", "y"]})
        assert meta["title"] == "My Title"
        assert meta["source"] == "memory_tree"
        assert meta["source_ref"] == ""
        assert meta["tags"] == ["x", "y"]
        assert meta["project_id"] is None  # SQLite NULL = global

    @pytest.mark.anyio
    async def test_validate_knowledge_metadata_preserves_project_id(self):
        """_validate_knowledge_metadata 保留非空 project_id。"""
        from repositories.vector_repo import VectorRepository
        repo = VectorRepository()
        meta = repo._validate_knowledge_metadata({"title": "T", "project_id": "proj_a"})
        assert meta["project_id"] == "proj_a"


class TestFlushBothBatches:
    """flush 同时刷新 memory/knowledge 两个 pending batch。"""

    @pytest.mark.anyio
    async def test_flush_triggers_both_batches(self):
        """两个 batch 都有数据时，flush 分别写入对应表。"""
        repo, conn = await _make_repo()
        await repo.index_memory("m1", "m")
        await repo.index_knowledge("k1", "k")
        ok = await repo.flush()

        assert ok is True
        assert await _fetch_ids(conn, "SELECT memory_id FROM memory_vec_map") == ["m1"]
        assert await _fetch_ids(conn, "SELECT node_id FROM knowledge_vec_map") == ["k1"]

    @pytest.mark.anyio
    async def test_flush_returns_false_if_one_fails(self):
        """memory flush 失败返回 False；knowledge 仍尝试 flush（不丢数据）。"""
        repo, conn = await _make_repo()
        await repo.index_memory("m1", "m")
        await repo.index_knowledge("k1", "k")

        orig_flush = repo._flush_batch

        async def _failing_memory_flush(vec_table, map_table, pending_batch, is_knowledge=False):
            if not is_knowledge:
                return False  # memory 分支失败
            return await orig_flush(vec_table, map_table, pending_batch, is_knowledge)

        repo._flush_batch = _failing_memory_flush
        ok = await repo.flush()

        assert ok is False
        # knowledge 分支仍执行写入
        assert await _fetch_ids(conn, "SELECT node_id FROM knowledge_vec_map") == ["k1"]
        # memory 分支失败——batch 未清空（保留下次重试）
        assert len(repo._pending_memory_batch) == 1


class TestBatchSeparation:
    """内存 pending batch 分离（对标原 _collection 别名语义）。"""

    def test_batches_are_separate_objects(self):
        from repositories.vector_repo import VectorRepository
        repo = VectorRepository()
        assert repo._pending_memory_batch is not repo._pending_knowledge_batch

    @pytest.mark.anyio
    async def test_index_memory_only_queues_memory_batch(self):
        """index_memory 只进 memory batch，不污染 knowledge batch。"""
        repo, _conn = await _make_repo()
        await repo.index_memory("m1", "m")
        assert [x[0] for x in repo._pending_memory_batch] == ["m1"]
        assert repo._pending_knowledge_batch == []
