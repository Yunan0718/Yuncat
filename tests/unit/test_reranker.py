"""tests/unit/test_reranker.py —— Task 6.14 BGE-reranker + min_score 过滤测试

覆盖：
1. Reranker.rerank —— 正常重排（按 cross-encoder 分数降序）
2. min_score 过滤低分结果
3. top_n 截断
4. enabled=False → 返回原列表
5. 模型不可用 → best-effort 返回原列表
6. 空候选 → 返回空列表
7. 保留原 dict 元数据（不丢字段）
8. sigmoid 归一化分数到 0-1 范围
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

class _FakeCrossEncoder:
    """模拟 sentence_transformers.CrossEncoder——predict 返回预设分数

    预测顺序与输入 pairs 顺序一致：[(query, doc1), (query, doc2), ...]
    """

    def __init__(self, scores: list[float]):
        self._scores = scores
        self.predict_calls: list[list[tuple[str, str]]] = []

    def predict(self, pairs):
        self.predict_calls.append(pairs)
        return self._scores[:len(pairs)]


def _make_candidates(n: int) -> list[dict]:
    """构造 n 个测试候选结果"""
    return [
        {"id": f"m{i}", "content": f"content {i}", "score": 0.5,
         "source": "vector", "metadata": {"title": f"Title {i}"}}
        for i in range(n)
    ]


# ═══════════════════════════════════════════════════════════════
# TestReranker
# ═══════════════════════════════════════════════════════════════

class TestReranker:
    """Reranker 类——BGE cross-encoder 重排 + min_score 过滤"""

    async def test_rerank_reorders_by_relevance(self):
        """cross-encoder 分数高的排前面——输入按 0,1,2 顺序，分数 [0.1, 0.9, 0.5] → 输出 [m1, m2, m0]"""
        from services.memory.reranker import Reranker
        r = Reranker(enabled=True)
        r._model = _FakeCrossEncoder(scores=[0.1, 0.9, 0.5])
        r._load_attempted = True  # 跳过懒加载

        candidates = _make_candidates(3)
        result = await r.rerank("query", candidates)

        assert len(result) == 3
        assert result[0]["id"] == "m1"  # 分数 0.9 最高
        assert result[1]["id"] == "m2"  # 分数 0.5
        assert result[2]["id"] == "m0"  # 分数 0.1 最低

    async def test_rerank_filters_below_min_score(self):
        """min_score 过滤——分数低于阈值的被丢弃"""
        from services.memory.reranker import Reranker
        r = Reranker(enabled=True)
        # sigmoid(0.1) ≈ 0.525, sigmoid(-2) ≈ 0.119, sigmoid(3) ≈ 0.953
        r._model = _FakeCrossEncoder(scores=[0.1, -2.0, 3.0])
        r._load_attempted = True

        candidates = _make_candidates(3)
        result = await r.rerank("query", candidates, min_score=0.5)

        # sigmoid(-2.0) ≈ 0.119 < 0.5 → m1 被过滤
        assert len(result) == 2
        ids = [r["id"] for r in result]
        assert "m1" not in ids
        assert "m2" in ids  # sigmoid(3.0) ≈ 0.953
        assert "m0" in ids  # sigmoid(0.1) ≈ 0.525

    async def test_rerank_top_n_limit(self):
        """top_n 截断——只返回前 N 个"""
        from services.memory.reranker import Reranker
        r = Reranker(enabled=True)
        r._model = _FakeCrossEncoder(scores=[0.9, 0.8, 0.7, 0.6, 0.5])
        r._load_attempted = True

        candidates = _make_candidates(5)
        result = await r.rerank("query", candidates, top_n=3)

        assert len(result) == 3

    async def test_rerank_disabled_returns_original(self):
        """enabled=False → 返回原列表（截断到 top_n），不调模型"""
        from services.memory.reranker import Reranker
        r = Reranker(enabled=False)
        fake = _FakeCrossEncoder(scores=[0.9])
        r._model = fake
        r._load_attempted = True

        candidates = _make_candidates(3)
        result = await r.rerank("query", candidates, top_n=2)

        assert len(result) == 2
        assert result[0]["id"] == "m0"  # 原顺序
        assert result[1]["id"] == "m1"
        assert len(fake.predict_calls) == 0  # 未调模型

    async def test_rerank_model_unavailable_fallback(self):
        """模型不可用（_load_attempted=True 但 _model=None）→ best-effort 返回原列表"""
        from services.memory.reranker import Reranker
        r = Reranker(enabled=True)
        r._model = None  # 模型加载失败
        r._load_attempted = True

        candidates = _make_candidates(3)
        result = await r.rerank("query", candidates, top_n=2)

        assert len(result) == 2
        assert result[0]["id"] == "m0"  # 原顺序

    async def test_rerank_empty_candidates(self):
        """空候选 → 返回空列表"""
        from services.memory.reranker import Reranker
        r = Reranker(enabled=True)
        r._model = _FakeCrossEncoder(scores=[])
        r._load_attempted = True

        result = await r.rerank("query", [])
        assert result == []

    async def test_rerank_preserves_metadata(self):
        """rerank 不丢失原 dict 的其他字段（metadata/source 等）"""
        from services.memory.reranker import Reranker
        r = Reranker(enabled=True)
        r._model = _FakeCrossEncoder(scores=[0.9])
        r._load_attempted = True

        candidates = [
            {"id": "m0", "content": "hello", "score": 0.5,
             "source": "vector", "metadata": {"title": "T0", "tags": ["a", "b"]}},
        ]
        result = await r.rerank("query", candidates)

        assert len(result) == 1
        assert result[0]["metadata"]["title"] == "T0"
        assert result[0]["metadata"]["tags"] == ["a", "b"]
        assert result[0]["source"] == "vector"
        assert result[0]["content"] == "hello"

    async def test_rerank_score_normalized_to_0_1(self):
        """rerank 后 score 字段是 sigmoid 归一化的 0-1 值"""
        from services.memory.reranker import Reranker
        import math
        r = Reranker(enabled=True)
        raw_score = 2.5
        r._model = _FakeCrossEncoder(scores=[raw_score])
        r._load_attempted = True

        candidates = _make_candidates(1)
        result = await r.rerank("query", candidates)

        expected = 1.0 / (1.0 + math.exp(-raw_score))  # sigmoid(2.5) ≈ 0.924
        assert abs(result[0]["score"] - expected) < 0.01

    async def test_rerank_passes_query_doc_pairs_to_model(self):
        """模型 predict 收到正确的 (query, doc) pairs"""
        from services.memory.reranker import Reranker
        r = Reranker(enabled=True)
        fake = _FakeCrossEncoder(scores=[0.9, 0.5])
        r._model = fake
        r._load_attempted = True

        candidates = [
            {"id": "m0", "content": "doc A", "score": 0.5},
            {"id": "m1", "content": "doc B", "score": 0.5},
        ]
        await r.rerank("my query", candidates)

        assert len(fake.predict_calls) == 1
        pairs = fake.predict_calls[0]
        assert len(pairs) == 2
        assert pairs[0] == ("my query", "doc A")
        assert pairs[1] == ("my query", "doc B")

    async def test_rerank_is_awaitable(self):
        """rerank 是 async 方法，调用需 await"""
        from services.memory.reranker import Reranker
        import inspect
        r = Reranker(enabled=True)
        assert inspect.iscoroutinefunction(r.rerank)

    async def test_rerank_predict_runs_in_thread(self):
        """rerank 的模型 predict 走 asyncio.to_thread，不阻塞事件循环"""
        import asyncio
        from services.memory.reranker import Reranker
        r = Reranker(enabled=True)
        fake = _FakeCrossEncoder(scores=[0.9])
        r._model = fake
        r._load_attempted = True

        called = {}
        original_to_thread = asyncio.to_thread
        async def spy_to_thread(func, *args, **kwargs):
            called["to_thread"] = True
            return func(*args, **kwargs)

        import services.memory.reranker as reranker_mod
        reranker_mod.asyncio.to_thread = spy_to_thread
        try:
            await r.rerank("query", _make_candidates(1))
        finally:
            reranker_mod.asyncio.to_thread = original_to_thread

        assert called.get("to_thread") is True
        assert len(fake.predict_calls) == 1
