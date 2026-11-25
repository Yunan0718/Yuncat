"""services/memory/reranker.py —— Task 6.14 BGE cross-encoder 重排 + min_score 过滤

在 RAG 检索流程（hybrid_searcher）的 RRF 融合之后、返回之前，用 cross-encoder
对初检结果重排，提升 top-N 的精确度。

主流对标：
- LlamaIndex SentenceTransformerRerank：cross-encoder predict → 按 score 排序 → top_n 截断
- LangChain ContextualCompressionRetriever：类似 rerank + filter 流程
- Cohere Rerank API：云端 cross-encoder（本项目用本地模型，无需 API key）

设计要点：
1. 懒加载模型——首次 rerank() 调用时加载 CrossEncoder，避免启动卡顿
2. best-effort——sentence-transformers 未安装 / 模型下载失败 → 跳过 rerank，返回原列表
3. sigmoid 归一化——cross-encoder raw score（可正可负）→ 0-1 范围，与 RRF 分数一致量纲
4. min_score 过滤——归一化后低于阈值的结果被丢弃（默认 0.0 不过滤，向后兼容）
5. 批量 predict——RERANKER_BATCH_SIZE 防止大候选集 OOM

依赖：sentence-transformers（chromadb 已隐式依赖，VectorRepository 用它做 embedding）
模型：BAAI/bge-reranker-v2-m3（多语言 cross-encoder，568M 参数；fp32 文件约 2.27GB）
若内置到 src-python/embedding_models/ 则本地加载；否则首次联网下载（离线模式跳过）。
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

import config
from repositories.embedding_provider import _resolve_local_path

_log = logging.getLogger("yuncat.memory.reranker")


def _sigmoid(x: float) -> float:
    """sigmoid 归一化——将 cross-encoder raw score 映射到 (0, 1)"""
    # 防溢出：大正数 → 1.0，大负数 → 0.0
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    return math.exp(x) / (1.0 + math.exp(x))


class Reranker:
    """BGE cross-encoder 重排器——best-effort，模型不可用时跳过

    生命周期：lifespan 创建单例并注入 hybrid_searcher 调用链。
    模型懒加载——首次 rerank() 时加载，失败则永久跳过（_load_attempted=True）。
    """

    def __init__(
        self,
        model_name: str | None = None,
        enabled: bool | None = None,
        batch_size: int | None = None,
    ):
        self._model_name = model_name or config.RERANKER_MODEL
        self._enabled = enabled if enabled is not None else config.RERANKER_ENABLED
        self._batch_size = batch_size or config.RERANKER_BATCH_SIZE
        self._model = None  # CrossEncoder 实例（懒加载）
        self._load_attempted = False  # 是否已尝试加载（避免重复尝试）

    async def _ensure_model(self) -> bool:
        """懒加载 CrossEncoder 模型——返回 True 表示模型可用

        首次调用时尝试 from sentence_transformers import CrossEncoder 并加载模型。
        模型加载走 asyncio.to_thread()，避免阻塞事件循环。
        失败（未安装/下载失败）→ _load_attempted=True, _model=None，后续不再尝试。
        """
        if self._load_attempted:
            return self._model is not None
        self._load_attempted = True
        if not self._enabled:
            return False
        try:
            from sentence_transformers import CrossEncoder  # type: ignore[import-untyped]
        except ImportError:
            _log.warning(
                "sentence-transformers not installed, rerank disabled "
                "(install with: pip install sentence-transformers)"
            )
            return False
        try:
            path = _resolve_local_path(self._model_name) or self._model_name
            self._model = await asyncio.to_thread(CrossEncoder, path)
            _log.info("Reranker model loaded: %s", self._model_name)
            return True
        except Exception:
            _log.warning(
                "Failed to load reranker model %s, rerank disabled (best-effort)",
                self._model_name, exc_info=True,
            )
            return False

    async def rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        top_n: int | None = None,
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        """对候选结果用 cross-encoder 重排

        Args:
            query: 查询文本
            candidates: 初检结果列表（list[dict]，每个 dict 需含 "content" 字段）
            top_n: 返回前 N 个（None=不截断，返回全部）
            min_score: sigmoid 归一化后的最低分数阈值（None=用 config.RERANKER_MIN_SCORE）

        Returns:
            重排后的 list[dict]——score 字段更新为 sigmoid 归一化的 0-1 值，
            其余字段保留。模型不可用 → 返回原 candidates（截断到 top_n）。
        """
        if not candidates:
            return []

        # 默认参数 fallback 到 config
        if min_score is None:
            min_score = config.RERANKER_MIN_SCORE

        # enabled=False 或模型不可用 → best-effort 返回原列表（截断到 top_n）
        if not self._enabled or not await self._ensure_model():
            return candidates[:top_n] if top_n is not None else list(candidates)

        # 构造 (query, doc) pairs——doc 取 content 字段
        pairs = [(query, c.get("content", "")) for c in candidates]

        # 批量 predict（防 OOM），predict 走线程池避免阻塞事件循环
        raw_scores: list[float] = []
        for i in range(0, len(pairs), self._batch_size):
            batch = pairs[i:i + self._batch_size]
            scores = await asyncio.to_thread(self._model.predict, batch)
            raw_scores.extend(float(s) for s in scores)

        # sigmoid 归一化 + 更新 score 字段（保留原 dict 其他字段）
        scored: list[tuple[float, dict[str, Any]]] = []
        for raw, candidate in zip(raw_scores, candidates):
            normalized = _sigmoid(raw)
            # 拷贝避免 in-place 修改原 list
            item = dict(candidate)
            item["score"] = normalized
            scored.append((normalized, item))

        # 按分数降序排序
        scored.sort(key=lambda x: x[0], reverse=True)

        # min_score 过滤
        if min_score > 0:
            scored = [(s, item) for s, item in scored if s >= min_score]

        # top_n 截断
        if top_n is not None:
            scored = scored[:top_n]

        return [item for _, item in scored]
