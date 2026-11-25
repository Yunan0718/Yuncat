"""tests/unit/test_memory_indexer_worker.py —— Task 4.4: RAG 索引一致性测试

验证 Outbox pattern 实现：
- memories / knowledge_nodes 表 indexed_at 列存在
- mark_memory_indexed / mark_knowledge_indexed 正确标记
- list_pending_memory_index / list_pending_knowledge_index 正确返回待索引行
- MemoryIndexerWorker 单轮扫描：pending → indexed
- 失败时不标记（下次重试）
- vector_repo=None 时 worker 空转
- start/stop 幂等
"""
import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from repositories.memories_repo import (
    insert_memory, mark_memory_indexed, list_pending_memory_index,
)
from repositories.knowledge_repo import (
    upsert_knowledge_node, mark_knowledge_indexed, list_pending_knowledge_index,
)
from services.memory_indexer_worker import MemoryIndexerWorker


# ═══════════════════════════════════════════════════════════
# Schema: indexed_at 列存在
# ═══════════════════════════════════════════════════════════

async def test_memories_table_has_indexed_at_column(db):
    """memories 表有 indexed_at 列"""
    cursor = await db.execute("PRAGMA table_info(memories)")
    cols = [row[1] for row in await cursor.fetchall()]
    assert "indexed_at" in cols


async def test_knowledge_nodes_table_has_indexed_at_column(db):
    """knowledge_nodes 表有 indexed_at 列"""
    cursor = await db.execute("PRAGMA table_info(knowledge_nodes)")
    cols = [row[1] for row in await cursor.fetchall()]
    assert "indexed_at" in cols


async def test_indexed_at_defaults_to_null(db):
    """新插入的 memories 行 indexed_at 默认 NULL（待索引）"""
    mem_id = await insert_memory(db, content="test memory", source="test")
    cursor = await db.execute("SELECT indexed_at FROM memories WHERE id=?", (mem_id,))
    row = await cursor.fetchone()
    assert row[0] is None


# ═══════════════════════════════════════════════════════════
# mark / list_pending 函数
# ═══════════════════════════════════════════════════════════

async def test_mark_memory_indexed_sets_timestamp(db):
    """mark_memory_indexed 设置 indexed_at 为当前时间戳"""
    mem_id = await insert_memory(db, content="test")
    before = int(time.time())
    await mark_memory_indexed(db, mem_id)
    after = int(time.time())
    cursor = await db.execute("SELECT indexed_at FROM memories WHERE id=?", (mem_id,))
    row = await cursor.fetchone()
    assert row[0] is not None
    assert before <= row[0] <= after


async def test_list_pending_memory_index_returns_null_rows(db):
    """list_pending_memory_index 返回 indexed_at IS NULL 的行"""
    m1 = await insert_memory(db, content="pending 1")
    m2 = await insert_memory(db, content="pending 2")
    m3 = await insert_memory(db, content="indexed")
    await mark_memory_indexed(db, m3)

    pending = await list_pending_memory_index(db, limit=10)
    pending_ids = {p["id"] for p in pending}
    assert m1 in pending_ids
    assert m2 in pending_ids
    assert m3 not in pending_ids


async def test_list_pending_memory_index_respects_limit(db):
    """list_pending_memory_index 尊重 limit 参数"""
    for i in range(5):
        await insert_memory(db, content=f"mem {i}")
    pending = await list_pending_memory_index(db, limit=3)
    assert len(pending) == 3


async def test_list_pending_memory_index_returns_fields(db):
    """list_pending_memory_index 返回完整字段"""
    mem_id = await insert_memory(
        db, content="hello", source="conversation",
        owner="user1", memory_type="fact",
    )
    pending = await list_pending_memory_index(db, limit=1)
    assert len(pending) == 1
    item = pending[0]
    assert item["id"] == mem_id
    assert item["content"] == "hello"
    assert item["source"] == "conversation"
    assert item["owner"] == "user1"
    assert item["memory_type"] == "fact"


async def test_mark_knowledge_indexed_sets_timestamp(db):
    """mark_knowledge_indexed 设置 indexed_at"""
    node_id = await upsert_knowledge_node(db, "node1", "title", "content")
    await mark_knowledge_indexed(db, node_id)
    cursor = await db.execute("SELECT indexed_at FROM knowledge_nodes WHERE id=?", (node_id,))
    row = await cursor.fetchone()
    assert row[0] is not None


async def test_list_pending_knowledge_index_returns_null_rows(db):
    """list_pending_knowledge_index 返回 indexed_at IS NULL 的知识节点"""
    n1 = await upsert_knowledge_node(db, "n1", "t1", "c1")
    n2 = await upsert_knowledge_node(db, "n2", "t2", "c2")
    await mark_knowledge_indexed(db, n1)
    pending = await list_pending_knowledge_index(db, limit=10)
    pending_ids = {p["id"] for p in pending}
    assert n2 in pending_ids
    assert n1 not in pending_ids


# ═══════════════════════════════════════════════════════════
# MemoryIndexerWorker
# ═══════════════════════════════════════════════════════════

async def test_worker_run_once_indexes_pending_memories(db):
    """worker._run_once 扫描 pending memories 并索引"""
    m1 = await insert_memory(db, content="mem 1", source="conv", owner="u1")
    m2 = await insert_memory(db, content="mem 2", source="conv", owner="u2")

    # mock vector_repo——index_memory 成功 + flush 成功
    mock_vr = MagicMock()
    mock_vr.index_memory = AsyncMock(return_value=True)
    mock_vr.flush = AsyncMock(return_value=True)

    worker = MemoryIndexerWorker(db=db, vector_repo=mock_vr, initial_delay=0.0)
    await worker._run_once()

    # 两条 memory 都应该被索引 + 标记
    assert mock_vr.index_memory.await_count == 2
    assert mock_vr.flush.await_count == 2
    pending = await list_pending_memory_index(db, limit=10)
    assert len(pending) == 0
    assert worker.stats["memory_indexed"] == 2


async def test_worker_run_once_indexes_pending_knowledge(db):
    """worker._run_once 扫描 pending knowledge_nodes 并索引"""
    await upsert_knowledge_node(db, "n1", "title 1", "content 1")
    await upsert_knowledge_node(db, "n2", "title 2", "content 2")

    mock_vr = MagicMock()
    # Batch 2.2: knowledge 节点走独立 collection
    mock_vr.index_knowledge = AsyncMock(return_value=True)
    mock_vr.flush = AsyncMock(return_value=True)

    worker = MemoryIndexerWorker(db=db, vector_repo=mock_vr, initial_delay=0.0)
    await worker._run_once()

    pending = await list_pending_knowledge_index(db, limit=10)
    assert len(pending) == 0
    assert worker.stats["knowledge_indexed"] == 2
    assert mock_vr.index_knowledge.await_count == 2


async def test_worker_does_not_mark_on_flush_failure(db):
    """flush 失败时不标记 indexed_at——下次重试"""
    m1 = await insert_memory(db, content="will fail")

    mock_vr = MagicMock()
    mock_vr.index_memory = AsyncMock(return_value=True)
    mock_vr.flush = AsyncMock(return_value=False)  # flush 失败

    worker = MemoryIndexerWorker(db=db, vector_repo=mock_vr, initial_delay=0.0)
    await worker._run_once()

    # indexed_at 仍然是 NULL——下次 worker 轮会重试
    pending = await list_pending_memory_index(db, limit=10)
    assert len(pending) == 1
    assert worker.stats["memory_failed"] == 1
    assert worker.stats["memory_indexed"] == 0


async def test_worker_does_not_mark_on_index_exception(db):
    """index_memory 抛异常时不标记——下次重试"""
    m1 = await insert_memory(db, content="will throw")

    mock_vr = MagicMock()
    mock_vr.index_memory = AsyncMock(side_effect=RuntimeError("chromadb down"))
    mock_vr.flush = AsyncMock(return_value=True)

    worker = MemoryIndexerWorker(db=db, vector_repo=mock_vr, initial_delay=0.0)
    await worker._run_once()

    pending = await list_pending_memory_index(db, limit=10)
    assert len(pending) == 1
    assert worker.stats["memory_failed"] == 1


async def test_worker_skips_when_vector_repo_none(db):
    """vector_repo=None 时 worker 空转（chromadb 不可用）"""
    m1 = await insert_memory(db, content="pending")
    worker = MemoryIndexerWorker(db=db, vector_repo=None, initial_delay=0.0)
    await worker._run_once()
    # pending 仍在——等 chromadb 可用后补
    pending = await list_pending_memory_index(db, limit=10)
    assert len(pending) == 1
    assert worker.stats["memory_indexed"] == 0


async def test_worker_start_stop_idempotent():
    """start/stop 幂等——重复调用不报错"""
    mock_vr = MagicMock()
    mock_vr.index_memory = AsyncMock(return_value=True)
    mock_vr.flush = AsyncMock(return_value=True)

    worker = MemoryIndexerWorker(db=None, vector_repo=mock_vr, initial_delay=0.0,
                                 interval=0.01)
    await worker.start()
    await worker.start()  # 幂等
    assert worker._task is not None
    await worker.stop()
    assert worker._task is None
    await worker.stop()  # 幂等


async def test_worker_stop_interrupts_sleep():
    """stop 能中断 sleep——立即退出"""
    mock_vr = MagicMock()
    mock_vr.index_memory = AsyncMock(return_value=True)
    mock_vr.flush = AsyncMock(return_value=True)

    worker = MemoryIndexerWorker(db=None, vector_repo=mock_vr,
                                 initial_delay=10.0, interval=10.0)
    await worker.start()
    await asyncio.sleep(0.05)  # 让 task 启动
    await worker.stop()  # 应该立即返回（不等待 10s）
    assert worker._task is None


async def test_worker_passes_correct_metadata_to_vector_repo(db):
    """worker 传递正确的 metadata 给 vector_repo.index_memory"""
    m1 = await insert_memory(
        db, content="mem", source="conversation",
        owner="alice", memory_type="fact",
    )
    mock_vr = MagicMock()
    mock_vr.index_memory = AsyncMock(return_value=True)
    mock_vr.flush = AsyncMock(return_value=True)

    worker = MemoryIndexerWorker(db=db, vector_repo=mock_vr, initial_delay=0.0)
    await worker._run_once()

    # 验证 index_memory 调用参数
    call_args = mock_vr.index_memory.await_args
    assert call_args.args[0] == m1  # mem_id
    assert call_args.args[1] == "mem"  # content
    metadata = call_args.kwargs.get("metadata", {})
    assert metadata.get("source") == "conversation"
    assert metadata.get("owner") == "alice"
    assert metadata.get("memory_type") == "fact"


async def test_worker_knowledge_uses_title_content_format(db):
    """worker 索引 knowledge 时用 'title\\ncontent' 格式（与 MemoryTree 一致）"""
    await upsert_knowledge_node(db, "n1", "My Title", "My Content")

    mock_vr = MagicMock()
    # Batch 2.2: knowledge 节点走独立 collection
    mock_vr.index_knowledge = AsyncMock(return_value=True)
    mock_vr.flush = AsyncMock(return_value=True)

    worker = MemoryIndexerWorker(db=db, vector_repo=mock_vr, initial_delay=0.0)
    await worker._run_once()

    call_args = mock_vr.index_knowledge.await_args
    # content 应该是 "My Title\nMy Content..."（content 可能被截断）
    assert call_args.args[1].startswith("My Title\n")
    metadata = call_args.kwargs.get("metadata", {})
    assert metadata.get("title") == "My Title"
    assert metadata.get("source") == "memory_tree"


# ═══════════════════════════════════════════════════════════
# Batch 2.6: store_memory 后 indexed_at 原子标记
# ═══════════════════════════════════════════════════════════

class TestStoreMemoryIndexedAt:
    """Batch 2.6: MemoryService.store_memory 成功后原子标记 indexed_at，失败不标。"""

    @pytest.mark.asyncio
    async def test_store_memory_marks_indexed_when_flush_succeeds(self, db):
        """vector_repo 可用且 flush 成功时，indexed_at 应被标记。"""
        from services.memory.memory_service import MemoryService
        from repositories.memories_repo import list_pending_memory_index

        mock_vr = MagicMock()
        mock_vr.index_memory = AsyncMock(return_value=True)
        mock_vr.flush = AsyncMock(return_value=True)

        svc = MemoryService(db=db, vector_repo=mock_vr)
        mem_id = await svc.store_memory("hello world", source="test")

        pending = await list_pending_memory_index(db, limit=10)
        pending_ids = {p["id"] for p in pending}
        assert mem_id not in pending_ids
        assert mock_vr.index_memory.await_count == 1
        assert mock_vr.flush.await_count == 1

    @pytest.mark.asyncio
    async def test_store_memory_does_not_mark_when_flush_fails(self, db):
        """flush 失败时 indexed_at 保持 NULL，由 worker 兜底重试。"""
        from services.memory.memory_service import MemoryService
        from repositories.memories_repo import list_pending_memory_index

        mock_vr = MagicMock()
        mock_vr.index_memory = AsyncMock(return_value=True)
        mock_vr.flush = AsyncMock(return_value=False)  # flush 失败

        svc = MemoryService(db=db, vector_repo=mock_vr)
        mem_id = await svc.store_memory("will fail flush", source="test")

        pending = await list_pending_memory_index(db, limit=10)
        pending_ids = {p["id"] for p in pending}
        assert mem_id in pending_ids

    @pytest.mark.asyncio
    async def test_store_memory_does_not_mark_when_vector_repo_none(self, db):
        """chromadb 不可用时 indexed_at 保持 NULL，等可用后 worker 补索引。"""
        from services.memory.memory_service import MemoryService
        from repositories.memories_repo import list_pending_memory_index

        svc = MemoryService(db=db, vector_repo=None)
        mem_id = await svc.store_memory("no vector repo", source="test")

        pending = await list_pending_memory_index(db, limit=10)
        pending_ids = {p["id"] for p in pending}
        assert mem_id in pending_ids
