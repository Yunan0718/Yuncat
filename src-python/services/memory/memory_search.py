"""services/memory_search.py —— 多级检索与记忆存储（从 memory_service.py 拆分）

包含: multi_level_search / summarize_context / store_memory
      / _index_to_vector / vector_search / verify / summarize_conversation
"""
import asyncio
import logging

from config import (
    MEMORY_SEARCH_DEFAULT_LIMIT,
    MEMORY_SEARCH_CONV_PREVIEW_CHARS,
    MEMORY_SEARCH_FALLBACK_PREVIEW_CHARS,
    HYBRID_QUERY_EXPANSION_DEFAULT,
    HYBRID_DIVERSIFY_DEFAULT,
    HYBRID_LATENCY_BUDGET_MS_DEFAULT,
)

_log = logging.getLogger("yuncat.memory")


async def summarize_context(self, session_id: str) -> str:
    """把长会话压缩为一页摘要，减少传给 Agent 的 token。

    v27: summarizer 在 __init__ 注入，不再作为方法参数传递。
    Phase 1 降级：未注入摘要器时返回截断原文。
    """
    if not self._summarizer:
        # Phase 1 fallback
        return await self.summarize_conversation(
            self._sessions.get(session_id, [])
        )
    conv = self._sessions.get(session_id, [])
    if not conv:
        return ""
    last_msg_hash = hash(conv[-1]["content"]) if conv else 0
    cached = self._summaries.get(session_id)
    if cached and cached.get("last_hash") == last_msg_hash:
        return cached["summary"]

    full_text = "\n".join(m["content"][:MEMORY_SEARCH_CONV_PREVIEW_CHARS] for m in conv[-self._max_turns * 2:])
    try:
        summary = await self._summarizer(full_text)
        self._summaries[session_id] = {"summary": summary, "last_hash": last_msg_hash}
        return summary
    except Exception:
        _log.exception("Summarizer failed, falling back to truncation")
        self._summaries.pop(session_id, None)
        return full_text[-MEMORY_SEARCH_FALLBACK_PREVIEW_CHARS:] if len(full_text) > MEMORY_SEARCH_FALLBACK_PREVIEW_CHARS else full_text


async def multi_level_search(
    self, query: str, limit: int = MEMORY_SEARCH_DEFAULT_LIMIT,
    filters: dict | None = None,
    query_expansion: bool = HYBRID_QUERY_EXPANSION_DEFAULT,
    diversify: bool = HYBRID_DIVERSIFY_DEFAULT,
    latency_budget_ms: float = HYBRID_LATENCY_BUDGET_MS_DEFAULT,
) -> list[dict]:
    """v56 → Phase 54: 多级混合检索。

    借鉴 zvec MultiQuery：chromadb 向量 + FTS5 全文并行执行，RRF 融合，
    元数据过滤下推到两个引擎。
    """
    from services.memory.hybrid_searcher import hybrid_search_memories

    return await hybrid_search_memories(
        self.db, self._vector_repo, query,
        limit=limit, filters=filters, use_rrf=True,
        query_expansion=query_expansion,
        diversify=diversify,
        latency_budget_ms=latency_budget_ms,
        reranker=getattr(self, "_reranker", None),  # Task 6.14: BGE rerank
    )


async def store_memory(
    self, content: str, source: str = "conversation", owner: str = "",
    memory_type: str = "fact", project_id: str | None = None,
    confidence: float = 0.5,
) -> str:
    from repositories import insert_memory
    mem_id = await insert_memory(
        self.db, content, source=source, owner=owner, memory_type=memory_type,
        project_id=project_id, confidence=confidence,
    )
    # v56: 同时索引到 chromadb（如果可用）
    # Task 4.4: 同步索引成功后 mark_memory_indexed；失败时 indexed_at 保持 NULL，
    # 由 MemoryIndexerWorker 兜底补索引（Outbox pattern——避免"写入但检索不到"）
    await self._index_to_vector(
        mem_id, content, source=source, owner=owner,
        memory_type=memory_type, project_id=project_id,
    )
    return mem_id


async def _index_to_vector(
    self, mem_id: str, content: str,
    source: str = "", owner: str = "", memory_type: str = "fact",
    project_id: str | None = None,
) -> None:
    """索引到向量数据库——成功后标记 indexed_at。

    Task 4.4 改造：原 fire-and-forget asyncio.create_task 失败时索引任务丢失，
    记忆写入 DB 但 chromadb 检索不到。改为 await 同步索引 + flush 强制刷盘 +
    成功后 mark_memory_indexed；失败时 indexed_at 保持 NULL，MemoryIndexerWorker 兜底补索引。

    Task 2.1: project_id 写入 chromadb metadata，与 SQLite 侧 project_id 保持一致，
    供 MemoryFilter.to_chroma_where() 做项目级隔离。

    注意：vector_repo.index_memory 默认加入 batch buffer（返回 True 不代表已写 chromadb）。
    这里显式调 flush() 强制刷盘，保证 indexed_at 标记时记忆确实已持久化到 chromadb。
    """
    if not self._vector_repo:
        return  # vector_repo 不可用——indexed_at 保持 NULL，worker 兜底（等 chromadb 可用后补）
    try:
        metadata = {
            "source": source, "owner": owner,
            "memory_type": memory_type, "verified": 0,
        }
        # Batch 2.1: project_id 写入向量 metadata；None/空串统一为 global sentinel ""
        metadata["project_id"] = project_id or ""
        await self._vector_repo.index_memory(mem_id, content, metadata=metadata)
        # 强制 flush——确保写入 chromadb 持久化存储（而非仅 buffer）
        flushed = await self._vector_repo.flush()
        if flushed:
            from repositories import mark_memory_indexed
            await mark_memory_indexed(self.db, mem_id)
    except Exception:
        _log.debug("chromadb index failed for memory %s", mem_id[:8], exc_info=True)
        # 不抛——store_memory 主路径（写 SQLite）已成功，索引失败由 worker 兜底


async def vector_search(self, query: str, limit: int = 10) -> list[dict]:
    """v56: chromadb 语义向量检索"""
    if not self._vector_repo:
        return []
    try:
        return await self._vector_repo.search(query, limit)
    except Exception:
        # 留痕：返回 [] 与"真的没有相似记忆"无法区分，会让语义检索静默降级
        # （混合检索仍会回退 BM25，但检索质量下降这件事必须可见）
        _log.warning("向量检索失败，本次语义召回为空（已回退关键词检索）", exc_info=True)
        return []


async def verify(self, mem_id: str) -> None:
    from repositories import verify_memory_record
    await verify_memory_record(self.db, mem_id)


async def summarize_conversation(self, messages: list[dict]) -> str:
    """摘要生成——Phase 1 fallback 截断文本"""
    if not messages:
        return ""
    recent = messages[-6:]
    lines = []
    for m in recent:
        role = "用户" if m["role"] == "user" else "Agent"
        lines.append(f"{role}: {m['content'][:200]}")
    return "\n".join(lines)
