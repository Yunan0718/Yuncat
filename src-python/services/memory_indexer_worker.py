"""services/memory_indexer_worker.py —— RAG 索引一致性兜底 worker

Task 4.4: Outbox pattern 轻量实现。

问题背景：
  store_memory / MemoryTree._index_to_vector 用 fire-and-forget 索引 chromadb：
    - memory_search.py:_index_to_vector 用 asyncio.create_task（无持久化）
    - memory_tree.py:_index_to_vector 用 await 但失败只 log debug
  失败时记忆已写入 SQLite 但 chromadb 里没有 → vector_search 检索不到 →
  用户感觉"记忆丢了"。进程崩溃 / chromadb 暂时不可用 / event loop 关闭
  都会导致索引任务丢失。

修复方案（Outbox pattern 轻量版——`indexed_at` 列 + 后台 worker）：
  - memories / knowledge_nodes 表加 indexed_at INTEGER（NULL=待索引, ts=已索引）
  - 同步索引路径保留（快速路径）：成功后立即 mark_indexed
  - 本 worker 兜底（慢速路径）：每 MEMORY_INDEXER_INTERVAL_SEC 秒扫描
    indexed_at IS NULL 的行，调 vector_repo.index_memory，成功后标记
  - 启动时立即跑一次——恢复上次进程崩溃未完成的索引任务

对标：pgvector CDC / Elasticsearch reindex / Debezium outbox——
  主流做法都是"标记列 + 后台 worker 兜底"，不引入额外 MQ。

不引入 RestartStrategy 等复杂抽象（YAGNI——单实例 worker，loop_supervisor
已有那套抽象，这里不需要）。
"""
import asyncio
import logging

from config import (
    MEMORY_INDEXER_INTERVAL_SEC,
    MEMORY_INDEXER_BATCH_SIZE,
    MEMORY_INDEXER_INITIAL_DELAY_SEC,
)

_log = logging.getLogger("yuncat.memory_indexer")


class MemoryIndexerWorker:
    """RAG 索引一致性兜底 worker。

    使用方式（lifespan.py）：
        worker = MemoryIndexerWorker(db=conn, vector_repo=app.state.vector_repo)
        await worker.start()  # 启动后台循环（含启动时立即跑一次恢复）
        ...
        await worker.stop()   # 优雅停止

    依赖：
      - db: aiosqlite 全局单连接
      - vector_repo: VectorRepository 实例（可为 None——此时 worker 空转）
    """

    def __init__(self, db, vector_repo, *, interval: float | None = None,
                 batch_size: int | None = None, initial_delay: float | None = None):
        self._db = db
        self._vector_repo = vector_repo
        self._interval = interval if interval is not None else MEMORY_INDEXER_INTERVAL_SEC
        self._batch_size = batch_size if batch_size is not None else MEMORY_INDEXER_BATCH_SIZE
        self._initial_delay = initial_delay if initial_delay is not None else MEMORY_INDEXER_INITIAL_DELAY_SEC
        self._task: asyncio.Task | None = None
        self._stopping = False
        # 累计统计（供监控/调试查询）
        self.stats = {
            "memory_indexed": 0,
            "knowledge_indexed": 0,
            "memory_failed": 0,
            "knowledge_failed": 0,
            "cycles": 0,
        }

    async def start(self) -> None:
        """启动后台循环。幂等——重复调用不会创建多个 task。"""
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run_loop())
        _log.info("MemoryIndexerWorker started (interval=%ss, batch=%d)",
                  self._interval, self._batch_size)

    async def stop(self) -> None:
        """优雅停止——取消后台 task 并等待退出。"""
        if self._task is None:
            return
        self._stopping = True
        if not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                _log.debug("MemoryIndexerWorker stop raised", exc_info=True)
        self._task = None
        _log.info("MemoryIndexerWorker stopped (stats=%s)", self.stats)

    async def _run_loop(self) -> None:
        """主循环——初始延迟 → 立即跑一次（恢复）→ 周期扫描。"""
        # 启动延迟：等 vector_repo.initialize() 完成（lifespan 中 worker 在 vector_repo 后启动，
        # 但 initialize 内部 SentenceTransformer 模型加载可能仍在后台）
        try:
            await asyncio.sleep(self._initial_delay)
        except asyncio.CancelledError:
            return

        # 启动时立即跑一次——恢复上次进程崩溃未完成的索引任务
        try:
            await self._run_once()
        except asyncio.CancelledError:
            return
        except Exception:
            _log.exception("MemoryIndexerWorker initial recovery cycle failed")

        # 周期扫描
        while not self._stopping:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return
            if self._stopping:
                return
            try:
                await self._run_once()
            except asyncio.CancelledError:
                return
            except Exception:
                _log.exception("MemoryIndexerWorker cycle failed")
                # 异常后继续循环——不因单轮失败永久退出

    async def _run_once(self) -> None:
        """单轮扫描——索引 pending 的 memories + knowledge_nodes。"""
        self.stats["cycles"] += 1
        if self._vector_repo is None:
            return  # chromadb 不可用——空转，等下次再试（可能用户后来装了）

        # 1. memories 表
        try:
            from repositories.memories_repo import list_pending_memory_index, mark_memory_indexed
            pending_mem = await list_pending_memory_index(self._db, limit=self._batch_size)
            for item in pending_mem:
                if self._stopping:
                    return
                ok = await self._index_one_memory(item)
                if ok:
                    await mark_memory_indexed(self._db, item["id"])
                    self.stats["memory_indexed"] += 1
                else:
                    self.stats["memory_failed"] += 1
                    # 不标记 indexed_at——下次轮再试
        except Exception:
            _log.exception("MemoryIndexerWorker: memories scan failed")

        # 2. knowledge_nodes 表
        try:
            from repositories.knowledge_repo import list_pending_knowledge_index, mark_knowledge_indexed
            from config import MEMORY_TREE_INDEX_CONTENT_CHARS
            pending_kn = await list_pending_knowledge_index(self._db, limit=self._batch_size)
            for item in pending_kn:
                if self._stopping:
                    return
                ok = await self._index_one_knowledge(item, MEMORY_TREE_INDEX_CONTENT_CHARS)
                if ok:
                    await mark_knowledge_indexed(self._db, item["id"])
                    self.stats["knowledge_indexed"] += 1
                else:
                    self.stats["knowledge_failed"] += 1
        except Exception:
            _log.exception("MemoryIndexerWorker: knowledge_nodes scan failed")

    async def _index_one_memory(self, item: dict) -> bool:
        """索引单条记忆到 chromadb。返回 True=已持久化, False=失败（下次重试）。

        注意：index_memory 默认加入 batch buffer，返回 True 不代表已写 chromadb。
        这里显式调 flush() 强制刷盘，确保返回 True 时记忆确实已持久化。

        Task 6.5: 传 project_id 到 metadata——chromadb 不支持 None 值，
        MemoryVectorDoc.to_metadata() 会把 None 转为 ""(global sentinel)。
        """
        try:
            metadata = {
                "source": item.get("source", ""),
                "owner": item.get("owner", ""),
                "memory_type": item.get("memory_type", "fact"),
                "verified": item.get("verified", 0),
                "project_id": item.get("project_id"),  # None → "" by to_metadata
            }
            await self._vector_repo.index_memory(
                item["id"], item["content"], metadata=metadata,
            )
            return await self._vector_repo.flush()
        except Exception:
            _log.debug("chromadb index failed for memory %s", item.get("id", "")[:8], exc_info=True)
            return False

    async def _index_one_knowledge(self, item: dict, content_chars: int) -> bool:
        """索引单条知识节点到 chromadb。返回 True=已持久化, False=失败。

        Task 6.5: 传 project_id 到 metadata——同 _index_one_memory。
        """
        try:
            title = item.get("title", "")
            content = item.get("content", "")
            # 与 MemoryTree._index_to_vector 保持一致——title + content 拼接
            metadata = {
                "title": title,
                "source": "memory_tree",
                "project_id": item.get("project_id"),  # None → "" by to_metadata
            }
            # Batch 2.2: 知识节点写入独立 collection
            await self._vector_repo.index_knowledge(
                item["id"],
                f"{title}\n{content[:content_chars]}",
                metadata=metadata,
            )
            return await self._vector_repo.flush()
        except Exception:
            _log.debug("chromadb index failed for knowledge %s", item.get("id", "")[:8], exc_info=True)
            return False
