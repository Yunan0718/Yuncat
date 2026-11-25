"""repositories/vector_repo.py —— sqlite-vec 向量检索仓库

替代 chromadb 的轻量级向量存储方案。
基于 sqlite-vec（~200KB）嵌入现有 SQLite 数据库，零额外进程。

架构：
  - vec0 虚拟表（memory_vec / knowledge_vec）存储向量
  - 映射表（memory_vec_map / knowledge_vec_map）关联 vec0.rowid ↔ 主表 id
  - embedding 生成由 embedding_provider.py 独立负责

对标：chromadb PersistentClient → sqlite-vec vec0 virtual table
      chromadb collection → vec0 table + mapping table
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from pydantic import BaseModel, Field, ConfigDict

from config import VEC_DIMENSION

_log = logging.getLogger("yuncat.vector_repo")


def _compute_content_hash(text: str) -> str:
    """计算内容的 SHA256 hash（用于 embedding 缓存去重）。

    用 hashlib.sha256 而非 Python 内置 hash()——后者跨进程不稳定
    （对标 memory_sync.py 的 hash() 跨进程不稳定修复）。
    SHA256 是确定性的，跨进程/跨机器一致。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class MemoryVectorDoc(BaseModel):
    """写入 vec0 的语义记忆向量文档 schema。

    Task 6.5: 添加 project_id 字段——SQLite NULL 表示 global memory。
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    content: str
    source: str = "conversation"
    owner: str | None = None
    memory_type: str = "fact"
    verified: int = Field(default=0, ge=0, le=1)
    project_id: str | None = None


class KnowledgeVectorDoc(BaseModel):
    """写入 vec0 的 Memory Tree 知识节点 schema。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    content: str
    title: str = ""
    source: str = "memory_tree"
    source_ref: str = ""
    tags: list[str] = Field(default_factory=list)
    project_id: str | None = None


class VectorRepository:
    """sqlite-vec 向量存储 + 索引管理

    使用 vec0 虚拟表 + 映射表存储向量，替代 chromadb 的 PersistentClient + collection。
    vec0 只存 (rowid, embedding)，metadata 通过映射表关联到主表。

    用法：
        repo = VectorRepository(db)
        await repo.initialize()
        await repo.index_memory("id1", "content", {"source": "chat"})
        results = await repo.search("query text", limit=5)
    """

    MEMORY_VEC_TABLE = "memory_vec"
    KNOWLEDGE_VEC_TABLE = "knowledge_vec"
    MEMORY_MAP_TABLE = "memory_vec_map"
    KNOWLEDGE_MAP_TABLE = "knowledge_vec_map"

    def __init__(self, db=None, vec_dim: int | None = None):
        self._db = db  # aiosqlite.Connection
        self._vec_dim = vec_dim or VEC_DIMENSION
        self._embedding_provider = None
        self._pending_memory_batch: list[tuple[str, str, dict[str, Any] | None]] = []
        self._pending_knowledge_batch: list[tuple[str, str, dict[str, Any] | None]] = []
        self._batch_size = 50
        self._batch_lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()

    async def initialize(self, db=None) -> None:
        """初始化向量仓库。

        加载 sqlite-vec 扩展（如果尚未加载），确保 vec0 虚拟表可用。
        Args:
            db: 可选的 aiosqlite 连接。如果未提供，使用之前传入的 db。
        """
        if db is not None:
            self._db = db
        if self._db is None:
            _log.warning("No database connection, vector search disabled")
            return

        try:
            # 加载 sqlite-vec 扩展（如果尚未加载）
            import sqlite_vec
            await self._db.enable_load_extension(True)
            await self._db.load_extension(sqlite_vec.loadable_path())
            _log.info("VectorRepository: sqlite-vec extension loaded (dim=%d)", self._vec_dim)
        except Exception:
            _log.debug("sqlite-vec extension already loaded or unavailable", exc_info=True)

        # 获取 embedding provider
        from repositories.embedding_provider import get_embedding_provider
        self._embedding_provider = get_embedding_provider()

        # 验证 vec0 虚拟表存在（由 schema.py migration v31 创建）
        try:
            cursor = await self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (self.MEMORY_VEC_TABLE,),
            )
            row = await cursor.fetchone()
            if not row:
                _log.warning("vec0 table '%s' not found, vector search disabled", self.MEMORY_VEC_TABLE)
        except Exception:
            _log.debug("Failed to check vec0 table existence", exc_info=True)

    async def _get_db(self):
        """获取数据库连接，触发初始化。"""
        if self._db is None:
            _log.warning("VectorRepository: no database connection")
            return None
        return self._db

    # 2026-10-07：embedding 必须跑在线程池里。
    #
    # sentence-transformers 的 encode() 是同步 CPU 密集调用（1分钟内可占
    # 满一个 CPU 核）。原来直接 await 调它，等于把整个事件循环堵死
    # ——同一个循环上的 uvicorn create_server()（bind+listen）、WS 心跳、HTTP
    # 全部排队等它。实测 2026-10-06：启动期的一次记忆预热检索导致
    #     就绪信号 16:09:08.217 → 端口真正 listen 16:10:38.166（相差 90s），
    #     主进程在这期间兑换 token 超时 → accessToken=null → 界面全部 403。
    # 放进线程池后，主循环可以继续处理 bind/listen 与请求。
    # （同仓已有先例：routes/agents.py 与 routes/system.py 用 asyncio.to_thread 包异步 I/O）

    async def _get_embedding(self, texts: list[str]) -> list[list[float]]:
        """批量生成 embedding（线程池执行，不阻塞事件循环）。"""
        if self._embedding_provider is None:
            from repositories.embedding_provider import get_embedding_provider
            self._embedding_provider = get_embedding_provider()
        return await asyncio.to_thread(self._embedding_provider.embed_documents, texts)

    async def _get_query_embedding(self, text: str) -> list[float]:
        """生成查询 embedding（线程池执行，不阻塞事件循环）。"""
        if self._embedding_provider is None:
            from repositories.embedding_provider import get_embedding_provider
            self._embedding_provider = get_embedding_provider()
        return await asyncio.to_thread(self._embedding_provider.embed_query, text)

    # ─── 索引方法 ───────────────────────────────────────────

    async def index_memory(self, mem_id: str, content: str, metadata: dict[str, Any] | None = None) -> bool:
        """将记忆内容索引到 memory_vec（单条，会触发批量 flush）。"""
        return await self._index_batch(
            self._pending_memory_batch, self.MEMORY_VEC_TABLE, self.MEMORY_MAP_TABLE,
            [(mem_id, content, metadata)],
            is_knowledge=False,
        )

    async def index_knowledge(
        self, node_id: str, content: str, metadata: dict[str, Any] | None = None,
    ) -> bool:
        """将知识节点索引到 knowledge_vec。"""
        return await self._index_batch(
            self._pending_knowledge_batch, self.KNOWLEDGE_VEC_TABLE, self.KNOWLEDGE_MAP_TABLE,
            [(node_id, content, metadata)],
            is_knowledge=True,
        )

    async def index_batch(
        self,
        items: list[tuple[str, str, dict[str, Any] | None]],
    ) -> bool:
        """批量索引到 memory_vec（向后兼容——旧调用方默认走 memories）。"""
        return await self._index_batch(
            self._pending_memory_batch, self.MEMORY_VEC_TABLE, self.MEMORY_MAP_TABLE,
            items, is_knowledge=False,
        )

    async def _index_batch(
        self,
        pending_batch: list[tuple[str, str, dict[str, Any] | None]],
        vec_table: str,
        map_table: str,
        items: list[tuple[str, str, dict[str, Any] | None]],
        is_knowledge: bool = False,
    ) -> bool:
        if not items:
            return True
        async with self._batch_lock:
            pending_batch.extend(items)
            if len(pending_batch) >= self._batch_size:
                return await self._flush_batch(vec_table, map_table, pending_batch, is_knowledge)
            return True

    async def flush(self) -> bool:
        """强制 flush 两个 vec 的 pending buffer。"""
        async with self._batch_lock:
            ok_mem = await self._flush_batch(
                self.MEMORY_VEC_TABLE, self.MEMORY_MAP_TABLE,
                self._pending_memory_batch, is_knowledge=False,
            )
            ok_know = await self._flush_batch(
                self.KNOWLEDGE_VEC_TABLE, self.KNOWLEDGE_MAP_TABLE,
                self._pending_knowledge_batch, is_knowledge=True,
            )
            return ok_mem and ok_know

    async def _flush_batch(
        self,
        vec_table: str,
        map_table: str,
        pending_batch: list[tuple[str, str, dict[str, Any] | None]],
        is_knowledge: bool = False,
    ) -> bool:
        if not pending_batch:
            return True
        db = await self._get_db()
        if not db:
            pending_batch.clear()
            return False

        try:
            # 分离 ids, docs, metas
            ids, docs, metas = [], [], []
            for item_id, content, metadata in pending_batch:
                ids.append(item_id)
                docs.append(content)
                if is_knowledge:
                    meta = self._validate_knowledge_metadata(metadata)
                else:
                    meta = self._validate_memory_metadata(metadata)
                meta["content_hash"] = _compute_content_hash(content)
                metas.append(meta)

            # 过滤已存在的（content_hash 未变化）
            upsert_ids, upsert_docs, upsert_metas = await self._filter_unchanged(
                map_table, ids, docs, metas,
            )
            if not upsert_ids:
                _log.debug("All %d vectors unchanged, skipped flush", len(ids))
                pending_batch.clear()
                return True

            # 生成 embedding
            embeddings = await self._get_embedding(upsert_docs)

            # 批量写入 vec0 + 映射表（事务内）
            for i, (uid, doc, meta) in enumerate(zip(upsert_ids, upsert_docs, upsert_metas)):
                emb_json = json.dumps(embeddings[i])
                # 先删除旧的映射记录（如果存在）
                cursor = await db.execute(
                    f"SELECT vec_rowid FROM {map_table} WHERE memory_id = ?" if not is_knowledge
                    else f"SELECT vec_rowid FROM {map_table} WHERE node_id = ?",
                    (uid,),
                )
                existing = await cursor.fetchone()
                if existing:
                    old_rowid = existing[0]
                    await db.execute(f"DELETE FROM {vec_table} WHERE rowid = ?", (old_rowid,))
                    await db.execute(
                        f"DELETE FROM {map_table} WHERE vec_rowid = ?", (old_rowid,),
                    )

                # 插入 vec0
                await db.execute(
                    f"INSERT INTO {vec_table}(embedding) VALUES (?)",
                    (emb_json,),
                )
                cursor = await db.execute("SELECT last_insert_rowid()")
                vec_rowid = (await cursor.fetchone())[0]

                # 插入映射表
                if is_knowledge:
                    await db.execute(
                        f"INSERT INTO {map_table}(vec_rowid, node_id, content_hash) VALUES (?, ?, ?)",
                        (vec_rowid, uid, meta.get("content_hash", "")),
                    )
                else:
                    await db.execute(
                        f"INSERT INTO {map_table}(vec_rowid, memory_id, content_hash) VALUES (?, ?, ?)",
                        (vec_rowid, uid, meta.get("content_hash", "")),
                    )

            await db.commit()
            _log.debug(
                "Flushed %d/%d vectors to %s (%d skipped, unchanged)",
                len(upsert_ids), len(ids), vec_table, len(ids) - len(upsert_ids),
            )
            pending_batch.clear()
            return True
        except Exception:
            _log.exception("Failed to flush vector batch to %s", vec_table)
            return False

    async def _filter_unchanged(
        self,
        map_table: str,
        ids: list[str],
        docs: list[str],
        metas: list[dict[str, Any]],
    ) -> tuple[list[str], list[str], list[dict[str, Any]]]:
        """过滤掉 content_hash 未变化的记录。"""
        if not ids:
            return ids, docs, metas
        db = await self._get_db()
        if not db:
            return ids, docs, metas

        try:
            # 批量查询现有 hash
            id_col = "memory_id" if "memory" in map_table else "node_id"
            placeholders = ",".join("?" * len(ids))
            cursor = await db.execute(
                f"SELECT {id_col}, content_hash FROM {map_table} WHERE {id_col} IN ({placeholders})",
                ids,
            )
            rows = await cursor.fetchall()
            existing_hashes = {row[0]: row[1] or "" for row in rows}
        except Exception:
            _log.debug("Failed to query existing hashes, fallback to full upsert", exc_info=True)
            return ids, docs, metas

        up_ids, up_docs, up_metas = [], [], []
        for mid, doc, meta in zip(ids, docs, metas):
            new_hash = meta.get("content_hash", "")
            if existing_hashes.get(mid) and existing_hashes[mid] == new_hash:
                continue
            up_ids.append(mid)
            up_docs.append(doc)
            up_metas.append(meta)
        return up_ids, up_docs, up_metas

    # ─── 检索方法 ───────────────────────────────────────────

    async def search(
        self,
        query: str,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[dict]:
        """语义向量检索 memory_vec，支持 SQL 条件过滤。"""
        return await self._search_collection(
            self.MEMORY_VEC_TABLE, self.MEMORY_MAP_TABLE, "memory_id",
            "memories", query, limit, filters,
        )

    async def search_knowledge(
        self,
        query: str,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[dict]:
        """语义向量检索 knowledge_vec。"""
        return await self._search_collection(
            self.KNOWLEDGE_VEC_TABLE, self.KNOWLEDGE_MAP_TABLE, "node_id",
            "knowledge_nodes", query, limit, filters,
        )

    async def _search_collection(
        self,
        vec_table: str,
        map_table: str,
        id_col: str,
        main_table: str,
        query: str,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[dict]:
        db = await self._get_db()
        if not db:
            return []

        try:
            # 生成查询 embedding
            emb = await self._get_query_embedding(query)
            emb_json = json.dumps(emb)

            # vec0 最近邻搜索
            cursor = await db.execute(
                f"SELECT rowid, distance FROM {vec_table} WHERE embedding MATCH ? AND k=?",
                (emb_json, limit * 2),  # 多取一些用于后续过滤
            )
            vec_rows = await cursor.fetchall()
            if not vec_rows:
                return []

            # 通过映射表关联到主表
            vec_rowids = [r[0] for r in vec_rows]
            distance_map = {r[0]: r[1] for r in vec_rows}
            placeholders = ",".join("?" * len(vec_rowids))

            # 构建 SQL 查询
            sql = f"""
                SELECT m.id as main_id, m.content, m_vec.vec_rowid
                FROM {map_table} m_vec
                JOIN {main_table} m ON m.id = m_vec.{id_col}
                WHERE m_vec.vec_rowid IN ({placeholders})
            """
            params: list[Any] = list(vec_rowids)

            # 应用元数据过滤（仅支持等值过滤）
            if filters:
                filter_conds, filter_params = self._build_filter_conditions(filters, main_table)
                if filter_conds:
                    sql += " AND " + " AND ".join(filter_conds)
                    params.extend(filter_params)

            cursor = await db.execute(sql, params)
            main_rows = await cursor.fetchall()

            # 组装结果
            results = []
            seen_ids = set()
            for row in main_rows:
                mid = row[0]
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
                vec_rowid = row[2]
                distance = distance_map.get(vec_rowid, 1.0)
                score = 1.0 - distance  # cosine → similarity
                results.append({
                    "id": mid,
                    "content": row[1],
                    "score": max(0.0, min(1.0, score)),
                    "metadata": {},
                })
                if len(results) >= limit:
                    break

            return results
        except Exception:
            _log.exception("Vector search failed on %s", vec_table)
            return []

    def _build_filter_conditions(
        self, filters: dict[str, Any], main_table: str,
    ) -> tuple[list[str], list[Any]]:
        """将 filters dict 转换为 SQL WHERE 条件。"""
        conds: list[str] = []
        params: list[Any] = []
        # 支持的过滤字段
        field_map = {
            "source": "m.source",
            "owner": "m.owner",
            "memory_type": "m.memory_type",
            "verified": "m.verified",
            "project_id": "m.project_id",
            "title": "n.title",
            "source_ref": "n.source_ref",
        }
        prefix = "m" if main_table == "memories" else "n"

        for key, value in filters.items():
            if key == "$or":
                # chromadb $or 兼容——展开为 SQL OR
                or_conds = []
                for sub_filter in value:
                    for sub_key, sub_value in sub_filter.items():
                        col = field_map.get(sub_key)
                        if col:
                            or_conds.append(f"{col} = ?")
                            params.append(sub_value)
                if or_conds:
                    conds.append("(" + " OR ".join(or_conds) + ")")
                continue

            col = field_map.get(key)
            if col:
                conds.append(f"{col} = ?")
                params.append(value)

        return conds, params

    # ─── 删除方法 ───────────────────────────────────────────

    async def delete_memory(self, mem_id: str) -> None:
        """从 memory_vec 和映射表删除记忆索引。"""
        await self._vec_delete(self.MEMORY_VEC_TABLE, self.MEMORY_MAP_TABLE, "memory_id", mem_id)

    async def delete_knowledge(self, node_id: str) -> None:
        """从 knowledge_vec 和映射表删除知识节点索引。"""
        await self._vec_delete(self.KNOWLEDGE_VEC_TABLE, self.KNOWLEDGE_MAP_TABLE, "node_id", node_id)

    async def _vec_delete(self, vec_table: str, map_table: str, id_col: str, item_id: str) -> None:
        db = await self._get_db()
        if not db:
            return
        try:
            cursor = await db.execute(
                f"SELECT vec_rowid FROM {map_table} WHERE {id_col} = ?", (item_id,),
            )
            row = await cursor.fetchone()
            if row:
                vec_rowid = row[0]
                await db.execute(f"DELETE FROM {vec_table} WHERE rowid = ?", (vec_rowid,))
                await db.execute(f"DELETE FROM {map_table} WHERE vec_rowid = ?", (vec_rowid,))
                await db.commit()
        except Exception:
            _log.debug("Failed to delete from %s: %s", vec_table, item_id, exc_info=True)

    # ─── Metadata 校验 ──────────────────────────────────────

    def _validate_memory_metadata(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        if metadata is None:
            return MemoryVectorDoc(id="", content="").model_dump()
        return MemoryVectorDoc(id=metadata.get("id", ""), content="", **metadata).model_dump(
            exclude={"id", "content"},
        )

    def _validate_knowledge_metadata(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        if metadata is None:
            return KnowledgeVectorDoc(id="", content="").model_dump()
        return KnowledgeVectorDoc(id=metadata.get("id", ""), content="", **metadata).model_dump(
            exclude={"id", "content"},
        )