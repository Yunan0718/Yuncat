"""services/memory_tree.py —— Memory Tree 知识图谱（借鉴 OpenHuman 架构）

核心概念（来源：OpenHuman Memory Tree）：
1. 所有知识切成 ≤3k-token 的 Markdown 块，存入 knowledge_nodes 表
2. 每个节点有 parent_id → 形成树形知识结构
3. 评分衰减机制：M2 Ebbinghaus 时间衰减 exp(-rate*age_days) → 旧知识自然失权
4. FTS5 全文检索 → 对话时注入相关知识
5. 背景索引循环：定期扫描 conversations 和 workspace → 增量构建/更新知识树
"""
import asyncio
import logging
import math
import time as _time
import re
import uuid as _uuid

from config import (
    MEMORY_TREE_DEDUP_LIMIT, MEMORY_TREE_RECENT_DREAMS_LIMIT, LOOP_INTERVAL_MEMORY_TREE_SEC,
    MEMORY_TREE_INDEX_CONTENT_CHARS,
    MEMORY_TREE_CONVERSATION_PREVIEW_CHARS,
    MEMORY_TREE_SUMMARY_PROMPT_CHARS,
    MEMORY_TREE_NODE_TITLE_CHARS,
    MEMORY_TREE_RESULT_TITLE_CHARS,
    MEMORY_TREE_RESULT_CONTENT_CHARS,
    MEMORY_TREE_SYMBOL_SIGNATURE_CHARS,
    MEMORY_TREE_TAG_CONTENT_CHARS,
    MEMORY_TREE_DEDUP_TITLE_CHARS,
    MEMORY_TREE_DEDUP_COMPARE_TITLE_CHARS,
    MEMORY_TREE_DEDUP_WORDS_MAX,
    MEMORY_TREE_SYMBOLS_MAX,
    MEMORY_TREE_LINE_MAX_CHARS,
    MEMORY_TREE_FACT_LINE_CHARS,
    MEMORY_TREE_FACTS_MAX,
    MEMORY_TREE_FALLBACK_PREVIEW_CHARS,
    MEMORY_TREE_CHUNK_TITLE_CHARS,
    MEMORY_TREE_CHUNK_CONTENT_CHARS,
    MEMORY_TREE_CHUNKS_MAX,
    MEMORY_TREE_TAGS_MAX,
    MEMORY_TREE_CHUNK_TOKENS,
    MEMORY_TREE_DECAY_RATE,
    MEMORY_TREE_ACCESS_BOOST,
    MEMORY_TREE_MAX_AGE_DAYS,
)

_log = logging.getLogger("yuncat.memory_tree")

# Markdown 转纯文本（优先 tiktoken 精确计数，失败 fallback 到 CJK 估算）
def estimate_tokens(text: str, model_id: str = "gpt-4") -> int:
    from config import estimate_tokens as _estimate_tokens
    return _estimate_tokens(text, model_id)


def compute_decay_factor(age_days: float, rate: float = MEMORY_TREE_DECAY_RATE) -> float:
    """M2: Ebbinghaus 时间衰减因子——exp(-rate * age_days)。

    - age=0 → 1.0（刚访问，无衰减）
    - age 越大 → 因子越小（旧知识失权）
    - rate 越大 → 衰减越快

    对标人类记忆遗忘曲线：旧记忆慢慢失权，重新访问则增强（access boost）。
    替代原"假衰减"（uniform score*0.95，与时间/访问无关）。
    """
    if age_days <= 0:
        return 1.0
    return math.exp(-rate * age_days)


def _age_days(timestamp: int | float | None, fallback: int | float, now: float | None = None) -> float:
    """计算节点年龄（天）。timestamp 为 NULL 时用 fallback（created_at）。

    Returns max(0, (now - ts) / 86400).
    """
    ts = timestamp if timestamp is not None else fallback
    if ts is None:
        return 0.0
    current = now if now is not None else _time.time()
    return max(0.0, (current - ts) / 86400.0)



class MemoryTree:
    """内存树——Yuncat 的知识骨架。

    借鉴 OpenHuman 的设计：
    - 扁平 facts → 树形知识节点
    - 评分衰减 → 旧/无关知识自然下沉
    - FTS5 索引 → 毫秒级关键词检索
    - chromadb 向量 → 语义相似度检索（Phase 47）
    - 背景索引 → 持续从对话和文件中提取知识
    """

    def __init__(self, db, summarizer=None, vector_repo=None):
        self.db = db
        self._summarizer = summarizer
        self._vector_repo = vector_repo  # Phase 47: chromadb 混合检索
        self._reranker = None  # Task 6.14: lifespan 注入 Reranker
        self._last_conversation_index_time: float = 0
        self._last_workspace_index_time: float = 0  # Phase 31 fix: separate from conversations
        # Task 6.6: 事件驱动即时提取——debounce task
        self._index_debounce_task: asyncio.Task | None = None

    def set_summarizer(self, summarizer) -> None:
        """Task 7.2: 注入 LLM summarizer——用于 RAPTOR 聚类 + index_from_conversations。

        summarizer 签名: async Callable[[str], str]——接收 prompt，返回 LLM 输出。
        在 lifespan.py 中 AgentService 创建后调用（MemoryTree 先于 AgentService 构造）。
        """
        self._summarizer = summarizer

    def set_reranker(self, reranker) -> None:
        """Task 6.14: 注入 Reranker 实例——供 retrieve_context 的 hybrid_search_knowledge 使用。"""
        self._reranker = reranker

    async def _index_to_vector(
        self, node_id: str, content: str, title: str,
        project_id: str | None = None,
    ) -> None:
        """Phase 47: 将知识节点索引到 chromadb 向量库——成功后标记 indexed_at。

        Task 4.4 改造：原 fire-and-forget 失败时索引任务丢失，知识节点写入 DB 但
        chromadb 检索不到。改为 await 同步索引 + flush 强制刷盘 + 成功后
        mark_knowledge_indexed；失败时 indexed_at 保持 NULL，MemoryIndexerWorker 兜底补索引。

        Task 6.5: project_id 写入 chromadb metadata，供向量检索过滤。
        """
        if not self._vector_repo:
            return  # vector_repo 不可用——indexed_at 保持 NULL，worker 兜底
        try:
            metadata: dict = {"title": title, "source": "memory_tree"}
            if project_id:
                metadata["project_id"] = project_id
            # Batch 2.2: 知识节点写入独立 collection
            await self._vector_repo.index_knowledge(
                node_id, f"{title}\n{content[:MEMORY_TREE_INDEX_CONTENT_CHARS]}",
                metadata=metadata,
            )
            # 强制 flush——确保写入 chromadb 持久化存储（而非仅 buffer）
            flushed = await self._vector_repo.flush()
            if flushed:
                from repositories.knowledge_repo import mark_knowledge_indexed
                await mark_knowledge_indexed(self.db, node_id)
        except Exception:
            _log.debug("chromadb index failed for %s", node_id[:8], exc_info=True)
            # 不抛——主路径（写 SQLite）已成功，索引失败由 worker 兜底

    # ---- 索引：从 conversations 提取知识 ----

    async def index_from_conversations(self) -> int:
        """扫描最近 conversations，提取关键事实 → 写入知识树。

        每次只处理自上次索引以来的新对话（增量）。
        返回新增节点数。
        """
        from repositories import upsert_knowledge_node, search_knowledge_tree

        cursor = await self.db.execute(
            "SELECT id, session_id, role, content FROM conversations "
            "WHERE created_at > ? ORDER BY created_at DESC LIMIT ?",
            (int(self._last_conversation_index_time) if self._last_conversation_index_time else 0,
             MEMORY_TREE_RECENT_DREAMS_LIMIT),
        )
        rows = await cursor.fetchall()
        if not rows:
            return 0

        # 拼接对话文本，按 session 分组
        sessions: dict[str, list[str]] = {}
        for row in rows:
            sid = row[1]
            if sid not in sessions:
                sessions[sid] = []
            sessions[sid].append(f"{row[2]}: {row[3][:MEMORY_TREE_CONVERSATION_PREVIEW_CHARS]}")

        added = 0
        for sid, lines in sessions.items():
            full_text = "\n".join(lines[-20:])  # 最近 20 条
            if estimate_tokens(full_text) < 50:
                continue

            # 用 summarizer 提取关键事实
            facts_text = ""
            if self._summarizer:
                try:
                    prompt = _INDEX_PROMPT.format(conversation=full_text[:MEMORY_TREE_SUMMARY_PROMPT_CHARS])
                    facts_text = await self._summarizer(prompt)
                except Exception:
                    _log.exception("Index summarizer failed for session %s", sid[:8])

            if not facts_text:
                facts_text = _fallback_extract_facts(full_text)

            # 切割为独立知识节点
            chunks = _split_into_chunks(facts_text)
            for title, content in chunks:
                # Phase 30: 语义去重——FTS5 搜索标题+内容，相似度 > 70% 则更新
                existing = await _dedup_by_semantic(self.db, title, content)
                node_id = existing[0]["id"] if existing else str(_uuid.uuid4())

                # 推断标签
                tags = _infer_tags(content)
                await upsert_knowledge_node(
                    self.db, node_id, title, content,
                    source="conversation", source_ref=sid, tags=tags,
                    # "node_score" 才是节点持久化评分；"score" 是 BM25 相关性，
                    # 用它会在节点被重复索引时把累积的反馈评分重置掉。
                    score=1.0 if not existing else (existing[0].get("node_score") or 1.0),
                )
                # Phase 47: 同时索引到 chromadb 向量库
                await self._index_to_vector(node_id, content, title)
                added += 1

        self._last_conversation_index_time = _time.time()
        if added > 0:
            _log.info("Indexed %d knowledge nodes from %d sessions", added, len(sessions))
        return added

    # ---- Phase 33: 从工具结果直接写入知识 ----

    async def index_from_text(
        self, text: str, source: str = "tool_result",
        project_id: str | None = None,
    ) -> None:
        """从单条文本直接写入知识节点（工具调用结果/工作流输出等）。
        Fire-and-forget——不阻塞主流程。

        Task 6.5: project_id 参数——非空时存储为项目级知识（仅该项目可见）；
        None/空字符串存储为 NULL（global，对所有项目可见）。
        """
        import uuid as _uuid
        from repositories import upsert_knowledge_node
        try:
            title = text[:MEMORY_TREE_NODE_TITLE_CHARS].strip().replace("\n", " ")
            # P1-1 修复: 空文本（如空工具结果）会导致 title="" → 前端展示空白。
            # 对标主流知识库（Notion/Obsidian）：空标题回退到占位符，避免 UI 断裂。
            if not title:
                title = f"untitled_{source}"
            node_id = str(_uuid.uuid4())
            tags = _infer_tags(text)
            await upsert_knowledge_node(
                self.db, node_id, title, text[:MEMORY_TREE_INDEX_CONTENT_CHARS],
                source=source, source_ref="", tags=tags, score=0.6,
                project_id=project_id,
            )
            # Phase 47: 同步索引到 chromadb（Task 6.5: 传 project_id 到 metadata）
            await self._index_to_vector(
                node_id, text[:MEMORY_TREE_INDEX_CONTENT_CHARS], title,
                project_id=project_id,
            )
        except Exception:
            _log.debug("Unexpected error", exc_info=True)

    # ---- 检索：对话时注入知识 ----

    async def retrieve_context(
        self, query: str, limit: int = 5,
        project_id: str | None = None,
        return_raw: bool = False,
    ) -> str | list[dict]:
        """M2 + Phase 47: 混合检索——chromadb 向量 + FTS5 关键词，应用 Ebbinghaus 时间衰减。

        流程：
        1. 向量搜索（语义相似）→ 通过 node_id 回查 last_accessed/created_at
        2. FTS5 搜索（精确关键词）→ 已带 last_accessed/created_at
        3. 计算 final_score = base_score * compute_decay_factor(age_days)
           修复原 score 直接排序的尺度不一致 bug（vector cosine 0-1 vs FTS5 decayed 0.05-1.0）
        4. 按 final_score 排序
        5. Fire-and-forget：命中节点 last_accessed=now, score=MIN(1.0, score+BOOST)
           （access boost——对标人类记忆"重新访问则增强"）

        默认返回格式化的 Markdown 文本，直接拼入 Commander prompt。
        Batch 2.7: return_raw=True 时返回原始 dict 列表（含 id/score/tags/sources 等），
        供 agent_context 做跨源去重和统一截断。

        Task 6.5: project_id 参数——非空时只检索该项目的知识 + global 知识
        （NULL = global，对标 CLAUDE.md global，对所有项目可见）；
        None/空字符串视为不过滤（向后兼容）。
        """
        results: list[dict] = []
        hit_node_ids: list[str] = []
        now = _time.time()

        # Task 6.5: 构造 KnowledgeFilter——chromadb 用 "" 作 global sentinel，
        # FTS5 层用 OR IS NULL（NULL = global，对所有项目可见）
        filters = None
        if project_id:
            filters = {"project_id": project_id}

        # Batch 1.2: 统一走 hybrid_search_knowledge，避免重复原始向量+FTS5拼接逻辑
        try:
            from services.memory.hybrid_searcher import hybrid_search_knowledge
            raw_results = await hybrid_search_knowledge(
                self.db, self._vector_repo, query,
                limit=limit, filters=filters,
                use_rrf=True, reranker=getattr(self, "_reranker", None),
                return_raw=True,
            )
        except Exception:
            _log.debug("Hybrid knowledge search failed", exc_info=True)
            return ""

        if not raw_results:
            return ""

        # 回查时间戳用于 Ebbinghaus 衰减（vector 结果原本不带 timestamp）
        all_ids = [r.get("id", "") for r in raw_results if r.get("id")]
        ts_map = await self._fetch_node_timestamps(all_ids)
        for r in raw_results:
            nid = r.get("id", "")
            ts_info = ts_map.get(nid, {})
            r["last_accessed"] = ts_info.get("last_accessed")
            r["created_at"] = ts_info.get("created_at")

        # M2: 计算时间衰减 final_score = base_score * exp(-rate * age_days)
        for r in raw_results:
            base = r.get("score", 0.5)
            age_d = _age_days(r.get("last_accessed"), r.get("created_at"), now)
            r["final_score"] = base * compute_decay_factor(age_d)

        # 按 final_score 排序并截断
        raw_results.sort(key=lambda r: r.get("final_score", 0), reverse=True)
        results = raw_results[:limit]

        # M2: 访问增强——命中节点 last_accessed=now, score+=BOOST (capped 1.0)
        for r in results:
            nid = r.get("id")
            if nid:
                hit_node_ids.append(nid)
        if hit_node_ids:
            await self._boost_accessed_nodes(hit_node_ids)

        # Batch 2.7: 供 agent_context 做跨源去重/统一截断
        if return_raw:
            return results

        lines = ["## 相关知识（Memory Tree + 语义搜索）"]
        for r in results:
            src_label = "🔍语义" if "vector" in r.get("sources", []) else "📝关键词"
            tags_str = f" [{', '.join(r.get('tags', [])[:MEMORY_TREE_TAGS_MAX])}]" if r.get("tags") else ""
            lines.append(f"- **{r.get('title','')[:MEMORY_TREE_RESULT_TITLE_CHARS]}**{tags_str} {src_label} (评分: {r.get('final_score',0):.2f})")
            cnt = r.get("content", "")
            if cnt:
                lines.append(f"  {cnt[:MEMORY_TREE_RESULT_CONTENT_CHARS]}")
        return "\n".join(lines)

    async def _fetch_node_timestamps(self, node_ids: list[str]) -> dict[str, dict]:
        """批量查询知识节点的 last_accessed / created_at（vector 结果回查用）。"""
        if not node_ids:
            return {}
        try:
            placeholders = ",".join("?" * len(node_ids))
            cursor = await self.db.execute(
                f"SELECT id, last_accessed, created_at FROM knowledge_nodes "
                f"WHERE id IN ({placeholders})",
                node_ids,
            )
            rows = await cursor.fetchall()
            return {
                row[0]: {"last_accessed": row[1], "created_at": row[2]}
                for row in rows
            }
        except Exception:
            _log.debug("_fetch_node_timestamps failed", exc_info=True)
            return {}

    async def _boost_accessed_nodes(self, node_ids: list[str]) -> None:
        """M2: 命中检索的节点访问增强——last_accessed=now, score=MIN(1.0, score+BOOST)。

        Fire-and-forget（通过 asyncio.create_task 调用），不阻塞主检索流程。
        """
        if not node_ids:
            return
        try:
            now = int(_time.time())
            placeholders = ",".join("?" * len(node_ids))
            await self.db.execute(
                f"UPDATE knowledge_nodes "
                f"SET last_accessed = ?, "
                f"    score = MIN(1.0, score + ?) "
                f"WHERE id IN ({placeholders})",
                (now, MEMORY_TREE_ACCESS_BOOST, *node_ids),
            )
            await self.db.commit()
        except Exception:
            _log.debug("_boost_accessed_nodes failed", exc_info=True)

    # ---- 维护：评分衰减 + 清理 ----

    async def periodic_maintenance(self) -> dict:
        """M2: 定期维护——age-based prune（删除超期未访问的节点）。

        替代原"假衰减"（uniform score*0.95）：
        - 衰减现已在 retrieve_context 动态计算（compute_decay_factor），无需批量改 score
        - 清理策略改为 age-based：COALESCE(last_accessed, created_at) < now - MAX_AGE_DAYS
          （对标人类记忆——长期未被重新访问的记忆自然遗忘）

        返回 {"decayed": N, "pruned": M}
        """
        now = int(_time.time())
        cutoff = now - MEMORY_TREE_MAX_AGE_DAYS * 86400

        # P0-3.2: 先收集待删除的 node_ids，同步删除 chromadb 孤儿向量（原仅删 SQLite → 向量累积膨胀）
        cursor = await self.db.execute(
            "SELECT id FROM knowledge_nodes WHERE COALESCE(last_accessed, created_at) < ?",
            (cutoff,),
        )
        orphan_rows = await cursor.fetchall()
        orphan_ids = [r[0] for r in orphan_rows] if orphan_rows else []

        # 同步删除 chromadb 向量（防孤儿向量累积）
        if orphan_ids and self._vector_repo:
            for nid in orphan_ids:
                try:
                    await self._vector_repo.delete_memory(nid)
                except Exception:
                    _log.debug("Failed to delete orphan vector %s", nid, exc_info=True)

        # Age-based prune：删除超过 MAX_AGE_DAYS 未访问的节点
        # COALESCE(last_accessed, created_at) → NULL last_accessed 回退 created_at
        cursor = await self.db.execute(
            "DELETE FROM knowledge_nodes WHERE COALESCE(last_accessed, created_at) < ?",
            (cutoff,),
        )
        await self.db.commit()
        pruned = cursor.rowcount

        if pruned > 0:
            _log.info("Memory tree maintenance (M2 age-based prune): %d pruned, %d vectors cleaned",
                      pruned, len(orphan_ids))

        # Task 7.2 子任务1: RAPTOR 自动触发——未分组叶子 >= 阈值时聚类
        raptor_result = await self._maybe_run_raptor()
        if raptor_result and raptor_result.get("clusters_created", 0) > 0:
            _log.info("RAPTOR auto-trigger: %s", raptor_result)

        # decayed 恒为 0——衰减现动态计算，不再批量改 score
        return {"decayed": 0, "pruned": pruned, "raptor": raptor_result}

    async def _maybe_run_raptor(self) -> dict | None:
        """Task 7.2: RAPTOR 自动触发——当未分组叶子数 >= RAPTOR_AUTO_TRIGGER_LEAF_COUNT 时执行聚类。

        条件:
          1. _summarizer 已注入（需要 LLM 生成摘要）
          2. 未分组叶子数 >= RAPTOR_AUTO_TRIGGER_LEAF_COUNT

        失败时不影响 periodic_maintenance 主流程（prune 已完成）。
        """
        if not self._summarizer:
            return None  # 无 summarizer——跳过（对标原 index_from_conversations 的 fallback 策略）
        try:
            from config import RAPTOR_AUTO_TRIGGER_LEAF_COUNT
            cursor = await self.db.execute(
                "SELECT COUNT(*) FROM knowledge_nodes WHERE parent_id IS NULL"
            )
            leaf_count = (await cursor.fetchone())[0]
            if leaf_count < RAPTOR_AUTO_TRIGGER_LEAF_COUNT:
                return {"skipped": True, "leaf_count": leaf_count,
                        "threshold": RAPTOR_AUTO_TRIGGER_LEAF_COUNT}

            from services.knowledge_cluster import build_raptor_tree, ClusterConfig
            from repositories.embedding_provider import get_embedding_provider
            provider = get_embedding_provider()
            return await build_raptor_tree(
                self.db, provider, self._summarizer, config=ClusterConfig(),
            )
        except Exception:
            _log.warning("RAPTOR auto-trigger failed", exc_info=True)
            return None

    # ---- Phase 30: 从 workspace 文件提取知识 ----

    async def index_from_workspace(self) -> int:
        """扫描 workspace 中的代码文件，提取关键符号 → 写入知识树。

        每次只处理自上次索引以来修改过的文件（增量）。
        返回新增节点数。

        Task 6.5: 用 workspace 路径作为 project_id——workspace 文件知识属于该项目，
        切换 workspace（项目）时检索不到不相关项目的代码知识。
        对标 Claude Code CLAUDE.md 项目隔离。
        """
        from security.sandbox import get_workspace
        from repositories import upsert_knowledge_node
        import os as _os

        ws = get_workspace()
        shared = _os.path.join(str(ws), "shared")
        if not _os.path.isdir(shared):
            return 0

        # Task 6.5: workspace 路径作为 project_id（与 session_summaries.workspace 一致）
        workspace_project_id = str(ws)

        added = 0
        cutoff = self._last_workspace_index_time
        for root, dirs, files in _os.walk(shared):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in files:
                if fname.startswith("."):
                    continue
                fpath = _os.path.join(root, fname)
                try:
                    mtime = _os.stat(fpath).st_mtime
                    if mtime < cutoff:
                        continue
                except OSError:
                    continue

                # 只索引代码文件
                ext = _os.path.splitext(fname)[1]
                if ext not in (".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java",
                               ".yaml", ".yml", ".json", ".md", ".sql", ".toml"):
                    continue

                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except OSError:
                    continue

                if not content or len(content) < 100:
                    continue

                # 提取关键符号（类/函数/导入）
                symbols = _extract_code_symbols(fname, content)
                for symbol in symbols:
                    title = f"{fname}:{symbol['name']}"
                    node_content = (
                        f"文件: {fname}\n类型: {symbol['kind']}\n"
                        f"签名: {symbol['signature'][:MEMORY_TREE_SYMBOL_SIGNATURE_CHARS]}\n路径: {fpath}"
                    )
                    existing = await _dedup_by_semantic(self.db, title, node_content)
                    node_id = existing[0]["id"] if existing else str(_uuid.uuid4())
                    tags = _infer_tags(content[:MEMORY_TREE_TAG_CONTENT_CHARS])
                    await upsert_knowledge_node(
                        self.db, node_id, title, node_content,
                        source="workspace", source_ref=fpath, tags=tags,
                        # 同上：用 "node_score"（持久化评分）而非 "score"（BM25 相关性），
                        # 否则重新扫描工作区会把该节点累积的反馈评分重置掉。
                        score=0.7 if not existing else (existing[0].get("node_score") or 0.7),
                        project_id=workspace_project_id,
                    )
                    await self._index_to_vector(
                        node_id, node_content, title,
                        project_id=workspace_project_id,
                    )
                    added += 1

        if added > 0:
            _log.info("Indexed %d knowledge nodes from workspace files", added)
        # P2-3 修复: 原代码用 _os.path.getmtime(shared) 当 cutoff，但 shared 目录 mtime
        # 只在直接子项增删时变化，不反映文件内容修改 → 内容修改后永远不被增量索引。
        # 改为 _time.time()（扫描完成时刻），下次扫描只索引 mtime > 上次扫描时间的文件。
        # 已知限制: 跨设备拷贝（cp -p 保留 mtime）可能导致新文件 mtime < cutoff 被跳过，
        # 但 15 分钟扫描周期下影响极小。
        self._last_workspace_index_time = _time.time()
        return added

    # ---- Task 6.6: 事件驱动即时提取（debounce）----

    def trigger_conversation_index(self) -> None:
        """Task 6.6: 事件驱动即时提取——debounce 模式。

        每轮 assistant 完成后调用（由 ASSISTANT_TURN_COMPLETE 事件触发）。
        若已有 pending 计时器，取消重建（去抖）——避免连续对话 LLM 调用风暴。
        计时器到期后执行 index_from_conversations（增量，只索引新对话）。
        15 分钟 background_loop 仍保留作为兜底（崩溃恢复 + workspace + 维护）。

        对标 Claude Code 每轮即时提取——但用 debounce 聚合连续对话，
        避免工具循环（ReAct）中每个中间 assistant turn 都触发 LLM 摘要。
        """
        if self._index_debounce_task is not None and not self._index_debounce_task.done():
            self._index_debounce_task.cancel()
        self._index_debounce_task = asyncio.create_task(self._debounced_index_run())

    def _cancel_debounce_task(self) -> None:
        """取消 pending 的 debounce task（测试清理 / shutdown 用）。"""
        if self._index_debounce_task is not None and not self._index_debounce_task.done():
            self._index_debounce_task.cancel()
        self._index_debounce_task = None

    async def _debounced_index_run(self) -> None:
        """debounce 到期后执行 index_from_conversations。

        被 CancelledError 取消是正常行为（新一轮 assistant 取消旧计时器）。
        """
        try:
            from config import MEMORY_INDEX_DEBOUNCE_SEC
            await asyncio.sleep(MEMORY_INDEX_DEBOUNCE_SEC)
            await self.index_from_conversations()
        except asyncio.CancelledError:
            # 去抖——被新一轮 assistant turn 取消，不执行索引
            pass
        except Exception:
            _log.debug("Debounced conversation index failed", exc_info=True)

    def start_event_subscription(self) -> None:
        """Task 6.6: 订阅 ASSISTANT_TURN_COMPLETE 事件。

        在 lifespan 中 event bus 启动后调用。handler fire-and-forget——
        只调度 debounce task，不阻塞 event bus 消费者。
        """
        from services.event_bus import get_event_bus, Events
        get_event_bus().subscribe(
            Events.ASSISTANT_TURN_COMPLETE, self._on_assistant_turn,
        )

    async def _on_assistant_turn(self, event_type: str, data: dict) -> None:
        """事件 handler——调度 debounced 即时提取，立即返回不阻塞消费者。"""
        self.trigger_conversation_index()

    # ---- 背景循环 ----

    async def background_loop(self) -> None:
        """每 15 分钟运行一次：索引新对话 + 索引 workspace + 衰减旧知识。

        借鉴 OpenHuman 的 auto-fetch loop——持续自我更新。
        """
        while True:
            await asyncio.sleep(LOOP_INTERVAL_MEMORY_TREE_SEC)  # 15 分钟
            try:
                await self.index_from_conversations()
                await self.index_from_workspace()
                await self.periodic_maintenance()
                # Phase 39: FTS5 index optimization (防碎片)
                await self.db.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('optimize')")
                await self.db.execute("INSERT INTO memories_fts(memories_fts) VALUES('optimize')")
                await self._promote_ready_temp_roles()
            except Exception:
                _log.exception("Memory tree background loop failed")


    async def _promote_ready_temp_roles(self) -> int:
        """Phase 30: Dream 整合——检测 score >= 0.7 的临时角色，推送转化通知。"""
        cursor = await self.db.execute(
            "SELECT id, title, score FROM knowledge_nodes "
            "WHERE source='deliberation_tmp_role' AND score >= 0.7 "
            "AND tags NOT LIKE '%rejected%' ORDER BY score DESC LIMIT ?",
            (MEMORY_TREE_DEDUP_LIMIT,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return 0
        promoted = 0
        for node_id, title, score in rows:
            role_name = title.replace("临时角色: ", "")
            cursor2 = await self.db.execute(
                "SELECT id FROM roles WHERE name=? AND source='auto_generated'", (role_name,),
            )
            if await cursor2.fetchone():
                continue
            try:
                from services.event_bus import get_event_bus
                eb = get_event_bus()
                if hasattr(eb, "publish"):
                    await eb.publish("temp_role_ready", {
                        "node_id": node_id, "role_name": role_name, "score": score,
                        "message": f"临时角色「{role_name}」已累计使用 {int(score/0.2)} 次，建议转化为正式角色",
                    })
            except Exception:
                _log.debug("Unexpected error", exc_info=True)
            promoted += 1
        return promoted


# ---- Phase 30: 语义去重 ----

async def _dedup_by_semantic(db, title: str, content: str) -> list[dict]:
    """FTS5 语义去重——按标题+内容搜索已有节点，相似度 > 70% 则视为重复。

    FTS5 返回的相关度排序天然过滤不相关节点。
    如果 FTS5 搜索无结果，回退到精确标题匹配。
    """
    from repositories import search_knowledge_tree

    # FTS5 全文搜索标题（取前 3 条）
    results = await search_knowledge_tree(db, title[:MEMORY_TREE_DEDUP_TITLE_CHARS], limit=MEMORY_TREE_DEDUP_LIMIT)

    # 检查内容重叠度
    for r in results:
        existing_title = r.get("title", "")
        existing_content = r.get("content", "")
        overlap = _title_content_overlap(
            (title, content),
            (existing_title, existing_content),
        )
        if overlap > 0.7:
            return [r]

    return []


def _title_content_overlap(
    a: tuple[str, str], b: tuple[str, str],
) -> float:
    """计算两个 (title, content) 对的内容重叠度。

    1. 标题完全匹配 → 1.0
    2. 标题前缀匹配（前 30 字）→ 0.9
    3. 内容 Jaccard 相似度 → 0-1
    """
    title_a, content_a = a
    title_b, content_b = b

    if title_a[:MEMORY_TREE_DEDUP_COMPARE_TITLE_CHARS] == title_b[:MEMORY_TREE_DEDUP_COMPARE_TITLE_CHARS]:
        return 0.9

    # 内容词集 Jaccard
    words_a = set(content_a.lower().split()[:MEMORY_TREE_DEDUP_WORDS_MAX])
    words_b = set(content_b.lower().split()[:MEMORY_TREE_DEDUP_WORDS_MAX])
    if not words_a or not words_b:
        return 0.0
    intersection = len(words_a & words_b)
    union = len(words_a | words_b)
    return intersection / union if union > 0 else 0.0


# ---- Phase 30: 代码符号提取 ----

def _extract_code_symbols(filename: str, content: str) -> list[dict]:
    """从代码内容中提取关键符号（类/函数/import）。

    支持 Python / TypeScript / Rust / Go 四种语言。
    每文件最多提取 30 个符号。
    """
    ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
    symbols = []

    if ext == "py":
        # Python: class Xxx, def xxx
        symbols = _extract_python_symbols(content)
    elif ext in ("ts", "tsx", "js", "jsx"):
        # TypeScript/JavaScript: class Xxx, function xxx, const xxx = () =>
        symbols = _extract_ts_symbols(content)
    elif ext == "rs":
        symbols = _extract_rust_symbols(content)
    elif ext == "go":
        symbols = _extract_go_symbols(content)

    return symbols[:MEMORY_TREE_SYMBOLS_MAX]


def _extract_python_symbols(content: str) -> list[dict]:
    import re
    symbols = []
    for m in re.finditer(r'^\s*(?:async\s+)?def\s+(\w+)', content, re.MULTILINE):
        name = m.group(1)
        if not name.startswith("_"):
            symbols.append({"name": name, "kind": "function",
                           "signature": content[m.start():m.start() + 120].strip()})
    for m in re.finditer(r'^\s*class\s+(\w+)', content, re.MULTILINE):
        symbols.append({"name": m.group(1), "kind": "class",
                       "signature": content[m.start():m.start() + 120].strip()})
    return symbols


def _extract_ts_symbols(content: str) -> list[dict]:
    import re
    symbols = []
    for m in re.finditer(r'(?:export\s+)?(?:async\s+)?function\s+(\w+)', content):
        symbols.append({"name": m.group(1), "kind": "function",
                       "signature": content[m.start():m.start() + 120].strip()})
    for m in re.finditer(r'(?:export\s+)?class\s+(\w+)', content):
        symbols.append({"name": m.group(1), "kind": "class",
                       "signature": content[m.start():m.start() + 120].strip()})
    for m in re.finditer(r'(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(', content):
        symbols.append({"name": m.group(1), "kind": "function",
                       "signature": content[m.start():m.start() + 120].strip()})
    return symbols


def _extract_rust_symbols(content: str) -> list[dict]:
    import re
    symbols = []
    for m in re.finditer(r'^\s*(?:pub\s+)?fn\s+(\w+)', content, re.MULTILINE):
        symbols.append({"name": m.group(1), "kind": "function",
                       "signature": content[m.start():m.start() + 120].strip()})
    for m in re.finditer(r'^\s*(?:pub\s+)?(struct|enum|trait|impl)\s+(\w+)', content, re.MULTILINE):
        symbols.append({"name": m.group(2), "kind": m.group(1),
                       "signature": content[m.start():m.start() + 120].strip()})
    return symbols


def _extract_go_symbols(content: str) -> list[dict]:
    import re
    symbols = []
    for m in re.finditer(r'^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)', content, re.MULTILINE):
        symbols.append({"name": m.group(1), "kind": "function",
                       "signature": content[m.start():m.start() + 120].strip()})
    for m in re.finditer(r'^type\s+(\w+)\s+struct', content, re.MULTILINE):
        symbols.append({"name": m.group(1), "kind": "struct",
                       "signature": content[m.start():m.start() + 120].strip()})
    return symbols


# ---- TokenJuice 压缩器（借鉴 OpenHuman TokenJuice）----

def token_juice_compress(text: str) -> str:
    """压缩文本以减少 token 消耗。

    借鉴 OpenHuman 的 TokenJuice 压缩策略：
    1. HTML 标签 → 纯文本（已有 markdown，通常不需要）
    2. 连续空白 → 单个空格
    3. 截断超长行（> 500 字符的行可能已经是数据 dump）
    4. URL → 仅保留域名
    """
    # 连续空白 → 单个空格
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 截断超长行
    lines = text.split("\n")
    lines = [l if len(l) <= MEMORY_TREE_LINE_MAX_CHARS else l[:MEMORY_TREE_LINE_MAX_CHARS] + "...[truncated]" for l in lines]
    text = "\n".join(lines)

    # URL 缩短
    text = re.sub(r'https?://[^\s<>"]+', lambda m: _shorten_url(m.group()), text)

    return text


def _shorten_url(url: str) -> str:
    """URL 缩短：保留协议+域名，丢弃路径和参数"""
    m = re.match(r'(https?://[^/]+)', url)
    return m.group(1) + "/..." if m else url


# ---- 内部辅助 ----

_INDEX_PROMPT = """从以下对话中提取关键事实和知识。每条一行，格式：标题:内容。
规则：
1. 只提取有长期价值的信息（用户偏好、项目决策、技术选型、重要发现）
2. 忽略临时性的问答和闲聊
3. 标题简短（≤20 字），内容 ≤200 字
4. 中文输出

## 对话
{conversation}

## 提取结果"""


def _fallback_extract_facts(text: str) -> str:
    """纯规则提取关键句——不依赖 LLM 时的降级方案"""
    lines = text.split("\n")
    facts = []
    for line in lines:
        line = line.strip()
        if not line or len(line) < 20:
            continue
        # 检测"我"、"偏好"、"项目"、"用"等关键词
        if re.search(r'(偏好|习惯|喜欢|项目|用|选择|决定|配置|技术栈|数据库|语言|框架)', line):
            facts.append(line[:MEMORY_TREE_FACT_LINE_CHARS])
    return "\n".join(facts[:MEMORY_TREE_FACTS_MAX]) if facts else text[:MEMORY_TREE_FALLBACK_PREVIEW_CHARS]


def _split_into_chunks(facts_text: str) -> list[tuple[str, str]]:
    """将提取的事实文本切割为 (title, content) 对"""
    chunks = []
    for line in facts_text.strip().split("\n"):
        line = line.strip()
        if not line or len(line) < 10:
            continue
        # 支持 "标题: 内容" 格式
        if ":" in line and len(line.split(":", 1)[0]) < MEMORY_TREE_CHUNK_TITLE_CHARS:
            title, content = line.split(":", 1)
            title = title.strip()[:MEMORY_TREE_CHUNK_TITLE_CHARS]
            content = content.strip()[:MEMORY_TREE_CHUNK_CONTENT_CHARS]
        else:
            title = line[:MEMORY_TREE_CHUNK_TITLE_CHARS]
            content = line[:MEMORY_TREE_CHUNK_CONTENT_CHARS]
        chunks.append((title, content))
    return chunks[:MEMORY_TREE_CHUNKS_MAX]


def _infer_tags(text: str) -> list[str]:
    """从文本中推断标签"""
    tags = []
    tag_patterns = [
        (r'python|django|flask|fastapi', 'python'),
        (r'javascript|typescript|react|vue|node', 'javascript'),
        (r'rust|cargo|actix|tokio', 'rust'),
        (r'sql|postgres|mysql|sqlite|数据库|database', 'database'),
        (r'docker|k8s|kubernetes|部署|deploy', 'devops'),
        (r'api|rest|graphql|接口', 'api'),
        (r'前端|frontend|ui|css|html', 'frontend'),
        (r'后端|backend|server|服务', 'backend'),
        (r'安全|security|漏洞|加密', 'security'),
        (r'测试|test|pytest|jest', 'testing'),
        (r'git|github|版本|commit', 'git'),
        (r'linux|unix|bash|shell', 'linux'),
    ]
    text_lower = text.lower()
    for pattern, tag in tag_patterns:
        if re.search(pattern, text_lower):
            tags.append(tag)
    return list(set(tags))[:MEMORY_TREE_TAGS_MAX]  # 最多 3 个标签
