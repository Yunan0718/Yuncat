"""tests/unit/test_embedding_cache.py —— Task 7.7: embedding 缓存测试

验证 VectorRepository 的 content_hash 缓存机制：
- upsert 前查已有记录的 content_hash
- 内容相同（hash 匹配）时跳过 upsert（节省 embedding 计算）
- 内容变化时重新 upsert
- 新 id 总是索引
- 已有记录无 content_hash 时视为变化（向后兼容，补上 hash）

对标 LangChain Chroma / LlamaIndex 的 embedding cache——
避免对相同内容重复生成 embedding（昂贵 API 调用）。
"""
import asyncio
import hashlib
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ═══════════════════════════════════════════════════════════════
# Mock chromadb collection
# ═══════════════════════════════════════════════════════════════

class _MockCollection:
    """模拟 chromadb Collection——记录 upsert 调用，可预设 get 返回值"""

    def __init__(self):
        self.upsert_calls: list[dict] = []
        self.get_calls: list[dict] = []
        # 预设的已有记录：{id: {"content_hash": str, ...other metadata}}
        self.existing: dict[str, dict] = {}

    def upsert(self, *, documents, ids, metadatas):
        self.upsert_calls.append({
            "ids": list(ids),
            "documents": list(documents),
            "metadatas": list(metadatas),
        })
        # 更新 existing（模拟 chromadb 持久化）
        for mid, doc, meta in zip(ids, documents, metadatas):
            self.existing[mid] = {"content": doc, **meta}

    def get(self, *, ids, include=None):
        self.get_calls.append({"ids": list(ids), "include": include})
        found_ids = []
        found_metas = []
        for mid in ids:
            if mid in self.existing:
                found_ids.append(mid)
                found_metas.append(dict(self.existing[mid]))
            # 注意：不返回 content/document（include 只请求 metadatas）
        return {"ids": found_ids, "metadatas": found_metas}


def _make_repo_with_mock_collection():
    """创建 VectorRepository 并注入 mock collection

    Batch 2.2: 内部逻辑使用 _memory_collection；保留 _collection 作为向后兼容别名。
    """
    from repositories.vector_repo import VectorRepository
    repo = VectorRepository()
    mock_collection = _MockCollection()
    repo._memory_collection = mock_collection
    repo._collection = mock_collection  # 向后兼容别名
    return repo, mock_collection


class _FakeEmbedding:
    """固定向量 embedding provider——避免真实模型调用。"""

    def __init__(self, dim: int = 512):
        self._dim = dim

    def embed_documents(self, texts):
        return [[0.1] * self._dim for _ in texts]

    def embed_query(self, text):
        return [0.1] * self._dim


async def _make_repo_sqlite():
    """创建 sqlite-vec 内存库版 VectorRepository（content_hash 去重真实执行路径）。"""
    import aiosqlite
    from config import VEC_DIMENSION
    from repositories.vector_repo import VectorRepository

    conn = await aiosqlite.connect(":memory:")
    await conn.enable_load_extension(True)
    import sqlite_vec
    await conn.load_extension(sqlite_vec.loadable_path())

    await conn.execute(
        f"CREATE VIRTUAL TABLE memory_vec USING vec0(embedding float[{VEC_DIMENSION}])"
    )
    await conn.execute(
        "CREATE TABLE memory_vec_map ("
        " vec_rowid INTEGER PRIMARY KEY, memory_id TEXT NOT NULL UNIQUE, content_hash TEXT)"
    )
    await conn.commit()

    repo = VectorRepository(db=conn)
    repo._embedding_provider = _FakeEmbedding(VEC_DIMENSION)
    return repo, conn


# ═══════════════════════════════════════════════════════════════
# content_hash 计算测试
# ═══════════════════════════════════════════════════════════════

class TestContentHashComputation:
    """Task 7.7: content_hash 用 SHA256（确定性，跨进程稳定）"""

    def test_same_content_same_hash(self):
        """相同内容 → 相同 hash（确定性）"""
        from repositories.vector_repo import _compute_content_hash
        h1 = _compute_content_hash("hello world")
        h2 = _compute_content_hash("hello world")
        assert h1 == h2

    def test_different_content_different_hash(self):
        """不同内容 → 不同 hash"""
        from repositories.vector_repo import _compute_content_hash
        h1 = _compute_content_hash("hello")
        h2 = _compute_content_hash("world")
        assert h1 != h2

    def test_hash_matches_sha256_hexdigest(self):
        """hash 值 == hashlib.sha256(text.encode('utf-8')).hexdigest()"""
        from repositories.vector_repo import _compute_content_hash
        text = "test content 你好"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert _compute_content_hash(text) == expected

    def test_empty_content_has_valid_hash(self):
        """空内容也有有效 hash（不抛异常）"""
        from repositories.vector_repo import _compute_content_hash
        h = _compute_content_hash("")
        assert isinstance(h, str)
        assert len(h) == 64  # SHA256 hexdigest 长度

    def test_cjk_content_hash_stable(self):
        """CJK 内容 hash 稳定（跨进程一致——对标 memory_sync.py hash() 修复）"""
        from repositories.vector_repo import _compute_content_hash
        text = "你好世界 🌍 これは日本語입니다"
        h1 = _compute_content_hash(text)
        h2 = _compute_content_hash(text)
        assert h1 == h2


# ═══════════════════════════════════════════════════════════════
# _flush_batch 缓存逻辑测试
# ═══════════════════════════════════════════════════════════════

class TestFlushBatchEmbeddingCache:
    """Task 7.7: _flush_batch 的 embedding 缓存逻辑（sqlite-vec 真实路径）"""

    async def _pre_seed(self, conn, entries):
        """预置 map 表记录：{memory_id: content_hash 或 None}

        用大 rowid（100+）避开真实插入的 rowid 空间（vec0 从 1 开始分配）。
        """
        for i, (mid, chash) in enumerate(entries.items(), start=100):
            await conn.execute(
                "INSERT INTO memory_vec_map(vec_rowid, memory_id, content_hash) VALUES (?, ?, ?)",
                (i, mid, chash),
            )
        await conn.commit()

    async def _vec_count(self, conn):
        cur = await conn.execute("SELECT COUNT(*) FROM memory_vec")
        return (await cur.fetchone())[0]

    async def _map_hash(self, conn, mid):
        cur = await conn.execute(
            "SELECT content_hash FROM memory_vec_map WHERE memory_id=?", (mid,)
        )
        row = await cur.fetchone()
        return row[0] if row else None

    async def test_new_id_always_indexed(self):
        """新 id（map 表无记录）总是 upsert"""
        repo, conn = await _make_repo_sqlite()
        await repo.index_batch([("m1", "hello", {"source": "test"})])
        await repo.flush()

        assert await self._vec_count(conn) == 1
        assert await self._map_hash(conn, "m1") == hashlib.sha256(b"hello").hexdigest()

    async def test_unchanged_content_skipped(self):
        """内容相同（content_hash 匹配）时跳过 upsert"""
        repo, conn = await _make_repo_sqlite()
        expected_hash = hashlib.sha256(b"hello").hexdigest()
        await self._pre_seed(conn, {"m1": expected_hash})

        await repo.index_batch([("m1", "hello", {"source": "test"})])
        await repo.flush()

        # 内容未变 → 不产生新 vec 行（跳过 embedding 计算）
        assert await self._vec_count(conn) == 0
        assert await self._map_hash(conn, "m1") == expected_hash

    async def test_changed_content_re_indexed(self):
        """内容变化时重新 upsert"""
        repo, conn = await _make_repo_sqlite()
        old_hash = hashlib.sha256(b"old content").hexdigest()
        await self._pre_seed(conn, {"m1": old_hash})

        await repo.index_batch([("m1", "new content", {"source": "test"})])
        await repo.flush()

        # 内容变化 → 重新索引，hash 更新
        assert await self._vec_count(conn) == 1
        assert await self._map_hash(conn, "m1") == hashlib.sha256(b"new content").hexdigest()

    async def test_missing_hash_treated_as_changed(self):
        """已有记录无 content_hash 时视为变化（向后兼容，补上 hash）"""
        repo, conn = await _make_repo_sqlite()
        await self._pre_seed(conn, {"m1": None})  # 旧数据迁移场景——无 content_hash

        await repo.index_batch([("m1", "hello", {"source": "test"})])
        await repo.flush()

        # 无 content_hash → 视为变化 → 重新 upsert（补上 content_hash）
        assert await self._vec_count(conn) == 1
        assert await self._map_hash(conn, "m1") == hashlib.sha256(b"hello").hexdigest()

    async def test_mixed_batch_partial_skip(self):
        """批量中部分变化部分未变——只 upsert 变化的"""
        repo, conn = await _make_repo_sqlite()
        await self._pre_seed(conn, {
            "m1": hashlib.sha256(b"unchanged").hexdigest(),
            "m3": hashlib.sha256(b"old m3").hexdigest(),
        })

        await repo.index_batch([
            ("m1", "unchanged", {"source": "t"}),
            ("m2", "new content", {"source": "t"}),
            ("m3", "new m3", {"source": "t"}),
        ])
        await repo.flush()

        # 只 upsert m2 + m3（m1 跳过）→ 2 个 vec 行
        assert await self._vec_count(conn) == 2
        assert await self._map_hash(conn, "m3") == hashlib.sha256(b"new m3").hexdigest()

    async def test_all_unchanged_no_upsert_call(self):
        """全部未变时完全不 upsert（最大化节省 embedding）"""
        repo, conn = await _make_repo_sqlite()
        await self._pre_seed(conn, {
            "m1": hashlib.sha256(b"a").hexdigest(),
            "m2": hashlib.sha256(b"b").hexdigest(),
        })

        await repo.index_batch([
            ("m1", "a", {"source": "t"}),
            ("m2", "b", {"source": "t"}),
        ])
        result = await repo.flush()

        assert result is True  # flush 成功（即使无 upsert）
        assert await self._vec_count(conn) == 0

    async def test_get_failure_falls_back_to_full_upsert(self):
        """hash 查询失败时 fallback 到全量 upsert（不丢数据）"""
        repo, conn = await _make_repo_sqlite()
        orig_execute = conn.execute

        async def _failing_hash_query(sql, params=()):
            sql_lower = sql.lower().lstrip()
            if sql_lower.startswith("select") and "content_hash" in sql_lower:
                raise RuntimeError("hash query failed")
            return await orig_execute(sql, params)

        conn.execute = _failing_hash_query
        try:
            await repo.index_batch([("m1", "hello", {"source": "t"})])
            result = await repo.flush()
        finally:
            conn.execute = orig_execute

        # 查询失败 → fallback 全量 upsert（不丢数据）
        assert result is True
        assert await self._vec_count(conn) == 1
        assert await self._map_hash(conn, "m1") == hashlib.sha256(b"hello").hexdigest()

    async def test_content_hash_in_metadata_after_upsert(self):
        """upsert 后的 map 表含正确 content_hash 字段"""
        repo, conn = await _make_repo_sqlite()
        await repo.index_batch([("m1", "hello world", {"source": "test"})])
        await repo.flush()

        assert await self._vec_count(conn) == 1
        assert await self._map_hash(conn, "m1") == hashlib.sha256(b"hello world").hexdigest()
