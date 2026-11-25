"""services/memory/hybrid_searcher.py —— 借鉴 zvec MultiQuery 的混合检索层

设计要点（对标 zvec v0.5.0 / Weaviate / Elasticsearch 混合查询）：
1. 一次查询同时发起向量检索 + FTS5 全文检索，并行执行。
2. 元数据/标量过滤下推到两个引擎（chromadb `where` + SQLite WHERE）。
3. 用 RRF（Reciprocal Rank Fusion）融合多路结果，避免不同分数量纲直接比较。
4. 统一的 Pydantic Filter 模型，保证前后端/schema 一致。
5. 内建可观测性：延迟、命中数日志，可选 Prometheus 指标。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from pydantic import BaseModel, Field

import config
# Task 6.15: 截断溯源指针工具
from text_utils import truncate_with_pointer

_log = logging.getLogger("yuncat.memory.hybrid")

# ---- 可选 Prometheus 指标 ----
try:
    from prometheus_client import Histogram, Gauge

    _METRICS_AVAILABLE = True
except Exception:  # pragma: no cover - prometheus 为可选依赖
    _METRICS_AVAILABLE = False

    class _NoopHistogram:
        def __init__(self, *args, **kwargs):
            pass

        def labels(self, **kwargs) -> "_NoopHistogram":
            return self

        def observe(self, value: float) -> None:
            pass

    class _NoopGauge:
        def __init__(self, *args, **kwargs):
            pass

        def labels(self, **kwargs) -> "_NoopGauge":
            return self

        def set(self, value: float) -> None:
            pass

    Histogram = _NoopHistogram  # type: ignore[misc]
    Gauge = _NoopGauge  # type: ignore[misc]

HYBRID_LATENCY = Histogram(
    "yuncat_hybrid_search_latency_seconds",
    "Hybrid search latency",
    ["source"],
)
HYBRID_HITS = Gauge(
    "yuncat_hybrid_search_hits",
    "Hybrid search returned hit count",
    ["source"],
)


# ---- 过滤器模型 ----

class MemoryFilter(BaseModel):
    """语义记忆检索过滤器。

    所有字段均为可选；同时提供 chromadb `where` 和 SQLite WHERE 两种表达。
    """

    source: str | None = None
    owner: str | None = Field(default=None, description="会话/项目 owner，可复用为 session_id")
    memory_type: str | None = None
    verified: bool | None = None
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    project_id: str | None = Field(
        default=None,
        description="Task 6.5: 项目级隔离——NULL=不过滤；非空时匹配 memories.project_id "
                    "或 project_id IS NULL（global memory 对所有项目可见，对标 CLAUDE.md global）",
    )

    def to_chroma_where(self) -> dict[str, Any] | None:
        """转换为 chromadb where 字典（仅支持等值过滤的字段）。

        Task 6.5: project_id 在 chromadb 侧用 $or 表达 "匹配项目 OR global" 语义。
        """
        where: dict[str, Any] = {}
        if self.source is not None:
            where["source"] = self.source
        if self.owner is not None:
            where["owner"] = self.owner
        if self.memory_type is not None:
            where["memory_type"] = self.memory_type
        if self.verified is not None:
            where["verified"] = 1 if self.verified else 0
        if self.project_id is not None:
            # chromadb 不支持 None 作为 where 比较值；用 "" 作 global sentinel
            # (对标 LangChain Chroma / LlamaIndex——NoSQL 文档库不支持 null 的标准模式)
            # 映射: SQLite NULL(global) → chromadb ""(global sentinel)
            where["$or"] = [
                {"project_id": self.project_id},
                {"project_id": ""},
            ]
        return where if where else None

    def to_sql_conditions(self) -> tuple[list[str], list[Any]]:
        """返回 (conditions, params)，用于拼接 memories 表 WHERE 子句。"""
        conds: list[str] = []
        params: list[Any] = []
        if self.source is not None:
            conds.append("m.source = ?")
            params.append(self.source)
        if self.owner is not None:
            conds.append("m.owner = ?")
            params.append(self.owner)
        if self.memory_type is not None:
            conds.append("m.memory_type = ?")
            params.append(self.memory_type)
        if self.verified is not None:
            conds.append("m.verified = ?")
            params.append(1 if self.verified else 0)
        if self.min_confidence is not None:
            conds.append("m.confidence >= ?")
            params.append(self.min_confidence)
        if self.project_id is not None:
            # NULL = global memory（对标 CLAUDE.md global），对所有项目可见
            conds.append("(m.project_id = ? OR m.project_id IS NULL)")
            params.append(self.project_id)
        return conds, params


class KnowledgeFilter(BaseModel):
    """Memory Tree 知识节点检索过滤器。"""

    source: str | None = None
    source_ref: str | None = None
    project_id: str | None = Field(
        default=None,
        description="Task 6.5: 项目级隔离——NULL=不过滤；非空时匹配 knowledge_nodes.project_id "
                    "或 project_id IS NULL（global knowledge 对所有项目可见，对标 CLAUDE.md global）",
    )

    def to_chroma_where(self) -> dict[str, Any] | None:
        where: dict[str, Any] = {}
        if self.source is not None:
            where["source"] = self.source
        if self.source_ref is not None:
            where["source_ref"] = self.source_ref
        if self.project_id is not None:
            # chromadb 不支持 None 作为 where 比较值；用 "" 作 global sentinel
            where["$or"] = [
                {"project_id": self.project_id},
                {"project_id": ""},
            ]
        return where if where else None

    def to_sql_conditions(self) -> tuple[list[str], list[Any]]:
        conds: list[str] = []
        params: list[Any] = []
        if self.source is not None:
            conds.append("n.source = ?")
            params.append(self.source)
        if self.source_ref is not None:
            conds.append("n.source_ref = ?")
            params.append(self.source_ref)
        if self.project_id is not None:
            # NULL = global knowledge（对标 CLAUDE.md global），对所有项目可见
            conds.append("(n.project_id = ? OR n.project_id IS NULL)")
            params.append(self.project_id)
        return conds, params


# ---- RRF 融合 ----

def _rrf_fuse(
    result_lists: list[list[dict[str, Any]]],
    *,
    k: float = 60.0,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion。

    对多路有序结果按排名融合，返回统一排序列表。
    同一 id 在不同来源中出现时分数累加。
    """
    scores: dict[str, float] = {}
    metas: dict[str, dict[str, Any]] = {}
    sources: dict[str, set[str]] = {}

    for rlist in result_lists:
        for rank, item in enumerate(rlist):
            rid = item.get("id")
            if not rid:
                continue
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
            if rid not in metas:
                metas[rid] = dict(item)
            # 记录它来自哪些检索通道
            sources.setdefault(rid, set()).add(item.get("source", "unknown"))

    sorted_ids = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for rid, score in sorted_ids:
        item = dict(metas[rid])
        item["score"] = score
        item["sources"] = sorted(sources.get(rid, {"unknown"}))
        out.append(item)
    return out


# ---- 归一化辅助（RRF 不需要，但保留用于加权场景） ----

def _min_max_normalize(items: list[dict[str, Any]], key: str = "score") -> None:
    """对 items 的 key 字段做 min-max 归一化到 [0, 1]（in-place 修改）。

    Task 4.5 修复边界：
      - 空列表：直接 return（无操作）
      - 单元素 / 所有值相同：设为 0.5（中性分数——原代码设为 0.0 会让该通道
        贡献被压平，不合理。相同分数意味着无法区分相关性，给中性值最稳妥）
    """
    values = [float(i.get(key, 0.0)) for i in items]
    if not values:
        return
    min_v, max_v = min(values), max(values)
    denom = max_v - min_v
    for i in items:
        raw = float(i.get(key, 0.0))
        if denom <= 0:
            # 所有值相同——无法区分，给中性分数 0.5
            i[key] = 0.5
        else:
            i[key] = (raw - min_v) / denom


def _weighted_fuse(
    vector_results: list[dict[str, Any]],
    fts_results: list[dict[str, Any]],
    *,
    vector_weight: float = 0.7,
    fts_weight: float = 0.3,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """加权分数融合（备用）。要求先对两路分数做 min-max 归一化。

    Task 4.5 修复：
      - 配合 search_memories_fts 返回的真实 BM25 分数（原恒 0.5 导致 FTS 通道
        相关性信号丢失，加权融合时 FTS 贡献被压平）
      - _min_max_normalize 边界处理（空列表/单元素/相同值）
      - 拷贝输入 list 避免归一化 in-place 修改影响调用方
    """
    # 拷贝避免 in-place 修改原 list（hybrid_search_memories 中 vector_results/fts_results
    # 可能被 _weighted_rrf_fuse 复用——虽然当前代码不会同时调用，但防御性拷贝更安全）
    vector_copy = [dict(r) for r in vector_results]
    fts_copy = [dict(r) for r in fts_results]
    _min_max_normalize(vector_copy)
    _min_max_normalize(fts_copy)

    scores: dict[str, float] = {}
    metas: dict[str, dict[str, Any]] = {}
    for r in vector_copy:
        rid = r.get("id")
        if rid:
            scores[rid] = scores.get(rid, 0.0) + vector_weight * float(r.get("score", 0.0))
            metas.setdefault(rid, dict(r))
    for r in fts_copy:
        rid = r.get("id")
        if rid:
            scores[rid] = scores.get(rid, 0.0) + fts_weight * float(r.get("score", 0.0))
            metas.setdefault(rid, dict(r))

    sorted_ids = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
    out = []
    for rid, score in sorted_ids:
        item = dict(metas[rid])
        item["score"] = score
        out.append(item)
    return out


# ---- zvec v0.5.0 风格增强：查询扩展 / 自适应融合 / 结果多样化 ----

def _expand_query(query: str) -> list[str]:
    """轻量查询扩展：生成 2-3 个语义变体，供 MultiQuery 并行检索。

    策略（无 LLM，零延迟开销）：
      - 保留原查询
      - CJK 查询：按字/词生成「包含核心名词」的紧缩版
      - 英文：去除停用词后的关键词版
    对标 zvec MultiQuery / vstash query variants。
    """
    variants = [query.strip()]
    if not query:
        return variants

    # CJK：提取连续 CJK 字符作为核心词，生成短查询
    import re
    cjk = re.findall(r"[一-鿿]+", query)
    if cjk and len(query) > 8:
        core = " ".join(cjk[:3])
        if core and core != query.strip():
            variants.append(core)

    # 英文/通用：去停用词后的关键词版
    stopwords = {"the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on", "at", "for", "with"}
    tokens = re.findall(r"[a-zA-Z0-9_]+", query.lower())
    keywords = [t for t in tokens if t not in stopwords]
    if len(keywords) >= 2 and len(keywords) < len(tokens):
        keyword_query = " ".join(keywords[:5])
        if keyword_query not in variants:
            variants.append(keyword_query)

    return variants[:3]


def _estimate_query_idf(query: str) -> float:
    """简单 IDF 启发式：查询越具体（罕见词越多），FTS 权重应越高。

    返回 0.0~1.0，用于自适应 RRF 权重。
    """
    if not query:
        return 0.5
    import re
    tokens = re.findall(r"[一-鿿]|[a-zA-Z0-9_]+", query)
    if not tokens:
        return 0.5
    # 长查询、含数字/代码/专有名词 → 更依赖 FTS
    specific_score = min(1.0, (len(tokens) - 1) / 6.0)
    has_specific = any(
        bool(re.search(r"\d|[A-Z]{2,}|[_-]", t)) for t in tokens
    )
    if has_specific:
        specific_score = min(1.0, specific_score + 0.2)
    return specific_score


def _adaptive_rrf_weights(query: str) -> tuple[float, float]:
    """根据查询特征动态调整向量/FTS 的 RRF 贡献。

    对标 vstash adaptive RRF：具体术语多 → 提升 FTS；语义开放 → 提升向量。
    返回 (vector_weight, fts_weight)。
    """
    idf = _estimate_query_idf(query)
    # 默认向量 0.6 / FTS 0.4；具体查询最多调到向量 0.45 / FTS 0.55
    fts_weight = 0.4 + (idf - 0.5) * 0.3
    fts_weight = max(0.3, min(0.55, fts_weight))
    vector_weight = 1.0 - fts_weight
    return vector_weight, fts_weight


def _jaccard_similarity(a: str, b: str) -> float:
    """两段文本的 Jaccard 相似度（基于字符/词集合），用于 MMR 去重。"""
    import re
    set_a = set(re.findall(r"[一-鿿]|[a-zA-Z0-9_]+", a.lower()))
    set_b = set(re.findall(r"[一-鿿]|[a-zA-Z0-9_]+", b.lower()))
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union else 0.0


def _mmr_diversify(
    items: list[dict[str, Any]],
    *,
    lambda_param: float = 0.5,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Maximal Marginal Relevance 结果多样化。

    在相关性和多样性之间取平衡，减少重复内容（zvec / Elasticsearch 常用后处理）。
    lambda_param: 0=纯多样，1=纯相关。
    """
    if not items:
        return []
    if lambda_param <= 0:
        lambda_param = 0.5
    if lambda_param >= 1:
        return items[:limit]

    selected: list[dict[str, Any]] = []
    remaining = list(items)
    # 归一化 RRF 分数到 [0,1]
    max_score = max(float(i.get("score", 0.0)) for i in items) or 1.0

    while remaining and len(selected) < limit:
        best_idx = 0
        best_mmr = -1.0
        for idx, item in enumerate(remaining):
            relevance = float(item.get("score", 0.0)) / max_score
            content = str(item.get("content", "")) + " " + str(item.get("title", ""))
            max_sim = 0.0
            for sel in selected:
                sel_content = str(sel.get("content", "")) + " " + str(sel.get("title", ""))
                max_sim = max(max_sim, _jaccard_similarity(content, sel_content))
            diversity = 1.0 - max_sim
            mmr = lambda_param * relevance + (1.0 - lambda_param) * diversity
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = idx
        selected.append(remaining.pop(best_idx))

    return selected


async def _search_with_deadline(
    coro,
    timeout: float | None,
    fallback: Any,
    label: str,
) -> Any:
    """带 latency budget 的检索包装：超时时返回 fallback 并记日志。"""
    if timeout is None:
        return await coro
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        _log.warning("%s search exceeded latency budget %.1fms", label, timeout * 1000)
        return fallback


def _merge_multi_query_results(result_lists: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """合并 MultiQuery 同一检索通道的多路结果：同 id 取最佳排名，保持排序。"""
    best_rank: dict[str, tuple[int, dict[str, Any]]] = {}
    for rlist in result_lists:
        for rank, item in enumerate(rlist):
            rid = item.get("id")
            if not rid:
                continue
            if rid not in best_rank or rank < best_rank[rid][0]:
                best_rank[rid] = (rank, item)
    # 按最佳排名排序
    sorted_items = sorted(best_rank.values(), key=lambda x: x[0])
    return [item for _, item in sorted_items]


def _weighted_rrf_fuse(
    result_lists: list[list[dict[str, Any]]],
    weights: list[float],
    *,
    k: float = 60.0,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """加权 RRF：每路结果按给定权重参与融合（权重和不必为 1）。"""
    scores: dict[str, float] = {}
    metas: dict[str, dict[str, Any]] = {}
    sources: dict[str, set[str]] = {}

    for rlist, weight in zip(result_lists, weights):
        for rank, item in enumerate(rlist):
            rid = item.get("id")
            if not rid:
                continue
            scores[rid] = scores.get(rid, 0.0) + weight * (1.0 / (k + rank + 1))
            if rid not in metas:
                metas[rid] = dict(item)
            sources.setdefault(rid, set()).add(item.get("source", "unknown"))

    sorted_ids = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for rid, score in sorted_ids:
        item = dict(metas[rid])
        item["score"] = score
        item["sources"] = sorted(sources.get(rid, {"unknown"}))
        out.append(item)
    return out


# ---- 对外 API ----

async def hybrid_search_memories(
    db,
    vector_repo,
    query: str,
    *,
    limit: int = 20,
    filters: MemoryFilter | dict[str, Any] | None = None,
    use_rrf: bool = True,
    rrf_k: float = 60.0,
    query_expansion: bool = False,
    diversify: bool = False,
    latency_budget_ms: float | None = None,
    reranker=None,  # Task 6.14: BGE cross-encoder 重排器（None=跳过，向后兼容）
) -> list[dict[str, Any]]:
    """语义记忆混合检索：chromadb 向量 + memories_fts 全文 + RRF 融合。

    Args:
        db: aiosqlite 连接
        vector_repo: VectorRepository 实例（可为 None，则只走 FTS）
        query: 查询文本
        limit: 返回条数上限
        filters: MemoryFilter 或其 dict
        use_rrf: True=RRF，False=加权融合
        rrf_k: RRF 常数，越小排名差异对分数影响越大
        query_expansion: 是否启用 zvec 风格 MultiQuery 扩展
        diversify: 是否启用 MMR 结果多样化
        latency_budget_ms: 总延迟预算（毫秒），超时时单路返回空但不熔断整体
        reranker: Task 6.14 Reranker 实例（None=跳过 rerank，向后兼容）。
                  RRF 融合后取 top RERANKER_TOP_K 送入 rerank，再按 min_score 过滤 + top_n 截断。
    """
    start = time.monotonic()
    if isinstance(filters, dict):
        filters = MemoryFilter(**filters)

    from repositories.memories_repo import search_memories_fts

    queries = _expand_query(query) if query_expansion else [query]
    per_query_timeout = (
        (latency_budget_ms / 1000.0) / max(1, len(queries)) / 2
        if latency_budget_ms else None
    )

    async def _apply_decay_to_vector_results(
        vector_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Batch 2.5: 对 vector 结果应用 Ebbinghaus 时间衰减，与 FTS5 路径对齐。"""
        if not db or not vector_results:
            return vector_results
        ids = [r.get("id") for r in vector_results if r.get("id")]
        if not ids:
            return vector_results
        try:
            from repositories.memories_repo import _compute_memory_decay_factor
            placeholders = ",".join("?" * len(ids))
            cursor = await db.execute(
                f"SELECT id, created_at FROM memories WHERE id IN ({placeholders})",
                ids,
            )
            ts_map = {row[0]: row[1] for row in await cursor.fetchall()}
            now = int(time.time())
            for r in vector_results:
                created_at = ts_map.get(r.get("id"))
                if not created_at:
                    continue
                age_days = (now - created_at) / 86400.0
                decay = _compute_memory_decay_factor(age_days)
                r["score"] = float(r.get("score", 0.0)) * decay
        except Exception:
            _log.debug("Failed to apply decay to vector results", exc_info=True)
        return vector_results

    async def _search_one(q: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        async def _vector_safe() -> list[dict[str, Any]]:
            if not vector_repo:
                return []
            try:
                where = filters.to_chroma_where() if filters else None
                results = await vector_repo.search(q, limit=limit * 2, filters=where)
                for r in results:
                    r["source"] = "vector"
                return results
            except Exception:
                _log.exception("Vector search failed for variant %r", q[:30])
                return []

        async def _fts_safe() -> list[dict[str, Any]]:
            try:
                return await search_memories_fts(
                    db, q,
                    limit=limit * 2,
                    filters=filters.model_dump(exclude_none=True) if filters else None,
                )
            except Exception:
                _log.exception("FTS5 search failed for variant %r", q[:30])
                return []

        v, f = await asyncio.gather(
            _search_with_deadline(_vector_safe(), per_query_timeout, [], "vector"),
            _search_with_deadline(_fts_safe(), per_query_timeout, [], "fts"),
        )
        # Batch 2.5: vector 结果统一应用 Ebbinghaus 衰减，保证与 FTS5 通道语义一致
        v = await _apply_decay_to_vector_results(v)
        return v, f

    vector_lists: list[list[dict[str, Any]]] = []
    fts_lists: list[list[dict[str, Any]]] = []
    for q in queries:
        v, f = await _search_one(q)
        vector_lists.append(v)
        fts_lists.append(f)

    vector_results = _merge_multi_query_results(vector_lists)
    fts_results = _merge_multi_query_results(fts_lists)

    # Task 6.14: reranker 存在时多取 top RERANKER_TOP_K 供 rerank，否则只取 limit（向后兼容）
    fuse_limit = max(limit, config.RERANKER_TOP_K) if reranker else limit

    if use_rrf:
        v_weight, f_weight = _adaptive_rrf_weights(query)
        fused = _weighted_rrf_fuse(
            [vector_results, fts_results], [v_weight, f_weight],
            k=rrf_k, limit=fuse_limit,
        )
    else:
        fused = _weighted_fuse(
            vector_results, fts_results, limit=fuse_limit,
        )

    # Task 6.14: BGE cross-encoder 重排——RRF 融合后重排，提升 top-N 精确度
    # best-effort：reranker=None / 模型不可用 → 跳过（rerank 内部 fallback 到原列表截断）
    if reranker:
        fused = await reranker.rerank(query, fused, top_n=limit)

    if diversify:
        fused = _mmr_diversify(fused, limit=limit)

    latency = time.monotonic() - start
    _log.info(
        "hybrid_search_memories query=%r variants=%d limit=%d vector=%d fts=%d fused=%d "
        "latency_ms=%.1f",
        query[:40], len(queries), limit, len(vector_results), len(fts_results),
        len(fused), latency * 1000,
    )
    HYBRID_LATENCY.labels(source="memories").observe(latency)
    HYBRID_HITS.labels(source="memories").set(len(fused))
    return fused


async def hybrid_search_knowledge(
    db,
    vector_repo,
    query: str,
    *,
    limit: int = 5,
    filters: KnowledgeFilter | dict[str, Any] | None = None,
    use_rrf: bool = True,
    rrf_k: float = 60.0,
    reranker=None,  # Task 6.14: BGE cross-encoder 重排器（None=跳过，向后兼容）
    return_raw: bool = False,
) -> str | list[dict[str, Any]]:
    """Memory Tree 知识混合检索，返回可直接拼入 prompt 的 Markdown 文本。

    对标 zvec MultiQuery：向量 + 全文 + 标量过滤一次完成。
    Task 6.14: reranker 存在时在 RRF 融合后加 cross-encoder 重排。

    Args:
        return_raw: 为 True 时返回原始融合结果列表（供调用方做后处理，如
                    Ebbinghaus 衰减、访问增强），不渲染 Markdown。
    """
    start = time.monotonic()
    if isinstance(filters, dict):
        filters = KnowledgeFilter(**filters)

    from repositories.knowledge_repo import search_knowledge_tree

    async def _vector_safe() -> list[dict[str, Any]]:
        if not vector_repo:
            return []
        try:
            where = filters.to_chroma_where() if filters else None
            # Batch 2.2: 知识节点向量检索走独立 collection
            results = await vector_repo.search_knowledge(query, limit=limit * 2, filters=where)
            formatted = []
            for r in results:
                formatted.append({
                    "id": r.get("id", ""),
                    "title": r.get("metadata", {}).get("title", r.get("id", "")[:60]),
                    "content": r.get("content", ""),
                    "score": r.get("score", 0.5),
                    "source": "vector",
                    "tags": r.get("metadata", {}).get("tags", []),
                })
            return formatted
        except Exception:
            _log.exception("Vector search failed (knowledge)")
            return []

    async def _fts_safe() -> list[dict[str, Any]]:
        try:
            results = await search_knowledge_tree(
                db, query,
                limit=limit * 2,
                filters=filters.model_dump(exclude_none=True) if filters else None,
            )
            for r in results:
                r["source"] = "fts5"
            return results
        except Exception:
            _log.exception("FTS5 search failed (knowledge)")
            return []

    vector_results, fts_results = await asyncio.gather(
        _vector_safe(), _fts_safe(), return_exceptions=False,
    )

    # Task 6.14: reranker 存在时多取 top RERANKER_TOP_K 供 rerank
    fuse_limit = max(limit, config.RERANKER_TOP_K) if reranker else limit

    fused = _rrf_fuse([vector_results, fts_results], k=rrf_k, limit=fuse_limit) if use_rrf else _weighted_fuse(
        vector_results, fts_results, limit=fuse_limit,
    )

    # Task 6.14: BGE cross-encoder 重排
    if reranker:
        fused = await reranker.rerank(query, fused, top_n=limit)

    latency = time.monotonic() - start
    _log.info(
        "hybrid_search_knowledge query=%r limit=%d vector=%d fts=%d fused=%d latency_ms=%.1f",
        query[:40], limit, len(vector_results), len(fts_results), len(fused), latency * 1000,
    )
    HYBRID_LATENCY.labels(source="knowledge").observe(latency)
    HYBRID_HITS.labels(source="knowledge").set(len(fused))

    if not fused:
        return [] if return_raw else ""

    if return_raw:
        return fused

    lines = ["## 相关知识（Memory Tree + 语义搜索）"]
    for r in fused:
        src_label = "🔍语义" if "vector" in r.get("sources", []) else "📝关键词"
        tags = r.get("tags", [])
        tags_str = f" [{', '.join(tags[:3])}]" if tags else ""
        title = r.get("title", "")[:80]
        score = float(r.get("score", 0.0))
        lines.append(f"- **{title}**{tags_str} {src_label} (评分: {score:.1f})")
        content = r.get("content", "")
        if content:
            # Task 6.15: 截断附溯源指针，完整 content 在 knowledge_nodes 节点 {r['id']}
            lines.append(f"  {truncate_with_pointer(content, 300, r.get('id'))}")
    return "\n".join(lines)
