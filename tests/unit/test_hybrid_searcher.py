"""tests/unit/test_hybrid_searcher.py —— 混合检索增强功能单元测试"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestExpandQuery:
    """查询扩展生成 1-3 个语义变体"""

    def test_expand_query_returns_one_to_three_variants(self):
        from services.memory.hybrid_searcher import _expand_query

        assert 1 <= len(_expand_query("hello world")) <= 3
        assert _expand_query("") == [""]

    def test_expand_query_preserves_original_first(self):
        from services.memory.hybrid_searcher import _expand_query

        query = "Implement a Python function to calculate fibonacci numbers"
        variants = _expand_query(query)
        assert variants[0] == query

    def test_expand_query_generates_keyword_variant(self):
        from services.memory.hybrid_searcher import _expand_query

        query = "How to implement a recursive function in Python"
        variants = _expand_query(query)
        assert any("implement" in v and "recursive" in v for v in variants[1:])

    def test_expand_query_handles_cjk(self):
        from services.memory.hybrid_searcher import _expand_query

        query = "如何在 Python 中实现递归函数并优化性能"
        variants = _expand_query(query)
        assert 1 <= len(variants) <= 3
        assert variants[0] == query


class TestAdaptiveRrfWeights:
    """自适应 RRF 权重满足约束"""

    def test_adaptive_rrf_weights_sum_and_bounds(self):
        from services.memory.hybrid_searcher import _adaptive_rrf_weights

        queries = [
            "hello world",
            "Python 3.12 PEP 695 type parameter syntax",
            "如何在 Python 中实现递归",
        ]
        for q in queries:
            vector_weight, fts_weight = _adaptive_rrf_weights(q)
            assert 0.9 <= vector_weight + fts_weight <= 1.1
            assert 0.3 <= fts_weight <= 0.55


class TestMmrDiversify:
    """MMR 减少重复内容"""

    def test_mmr_prefers_dissimilar_content(self):
        from services.memory.hybrid_searcher import _mmr_diversify

        similar = [
            {"id": "1", "content": "python programming language guide", "score": 1.0},
            {"id": "2", "content": "python tutorial for beginners", "score": 1.0},
            {"id": "3", "content": "learn python basics", "score": 1.0},
        ]
        dissimilar = {
            "id": "4",
            "content": "quantum computing with superconducting qubits",
            "score": 1.0,
        }
        items = similar + [dissimilar]
        result = _mmr_diversify(items, lambda_param=0.5, limit=4)
        assert result[1] == dissimilar


class TestRrfFuse:
    """RRF 正确融合多路结果并保留 sources"""

    def test_rrf_fuse_merges_and_preserves_sources(self):
        from services.memory.hybrid_searcher import _rrf_fuse

        vector = [
            {"id": "a", "content": "A", "source": "vector"},
            {"id": "b", "content": "B", "source": "vector"},
        ]
        fts = [
            {"id": "b", "content": "B", "source": "fts5"},
            {"id": "c", "content": "C", "source": "fts5"},
        ]
        fused = _rrf_fuse([vector, fts], k=60.0, limit=10)
        ids = [r["id"] for r in fused]
        assert "a" in ids
        assert "b" in ids
        assert "c" in ids

        b_item = next(r for r in fused if r["id"] == "b")
        assert sorted(b_item["sources"]) == ["fts5", "vector"]


class TestHybridSearchMemories:
    """hybrid_search_memories 在 vector_repo=None 时仅返回 FTS 结果"""

    async def test_fts_only_when_vector_repo_missing(self, monkeypatch):
        from services.memory.hybrid_searcher import hybrid_search_memories

        async def mock_search_memories_fts(conn, query, limit=20, filters=None):
            return [{"id": "m1", "content": "FTS result", "source": "conversation"}]

        monkeypatch.setattr(
            "repositories.memories_repo.search_memories_fts",
            mock_search_memories_fts,
        )

        results = await hybrid_search_memories(None, None, "query")
        assert len(results) == 1
        assert results[0]["id"] == "m1"
        assert "vector" not in results[0].get("sources", [])


# ═══════════════════════════════════════════════════════════════
# Task 6.14: Rerank 集成
# ═══════════════════════════════════════════════════════════════

class _StubReranker:
    """模拟 Reranker——记录调用，返回可控结果"""

    def __init__(self, reorder_to=None, filter_ids=None):
        self.reorder_to = reorder_to  # 按 id 列表重排（模拟 cross-encoder 打分）
        self.filter_ids = filter_ids or set()  # 要过滤掉的 id
        self.rerank_calls: list[tuple[str, list]] = []

    async def rerank(self, query, candidates, top_n=None, min_score=None):
        self.rerank_calls.append((query, candidates))
        if self.reorder_to:
            order = {id_: i for i, id_ in enumerate(self.reorder_to)}
            result = sorted(candidates, key=lambda c: order.get(c["id"], 999))
        else:
            result = list(candidates)
        result = [c for c in result if c["id"] not in self.filter_ids]
        return result[:top_n] if top_n else result


class TestHybridSearchRerank:
    """Task 6.14: hybrid_search_memories 的 rerank 集成"""

    async def test_reranker_invoked_after_fusion(self, monkeypatch):
        """reranker 注入 → RRF 融合后调 rerank"""
        from services.memory.hybrid_searcher import hybrid_search_memories

        async def mock_search_memories_fts(conn, query, limit=20, filters=None):
            return [
                {"id": "m0", "content": "doc A", "source": "conversation"},
                {"id": "m1", "content": "doc B", "source": "conversation"},
            ]

        monkeypatch.setattr(
            "repositories.memories_repo.search_memories_fts",
            mock_search_memories_fts,
        )

        reranker = _StubReranker(reorder_to=["m1", "m0"])
        results = await hybrid_search_memories(None, None, "query", reranker=reranker)

        assert len(reranker.rerank_calls) == 1
        query, candidates = reranker.rerank_calls[0]
        assert query == "query"
        # rerank 后 m1 排前面
        assert results[0]["id"] == "m1"
        assert results[1]["id"] == "m0"

    async def test_no_reranker_backward_compatible(self, monkeypatch):
        """reranker=None → 跳过 rerank，直接返回 RRF 结果（向后兼容）"""
        from services.memory.hybrid_searcher import hybrid_search_memories

        async def mock_search_memories_fts(conn, query, limit=20, filters=None):
            return [{"id": "m0", "content": "doc A", "source": "conversation"}]

        monkeypatch.setattr(
            "repositories.memories_repo.search_memories_fts",
            mock_search_memories_fts,
        )

        results = await hybrid_search_memories(None, None, "query", reranker=None)
        assert len(results) == 1
        assert results[0]["id"] == "m0"

    async def test_reranker_filters_low_quality(self, monkeypatch):
        """reranker 过滤掉低质量结果（filter_ids 模拟 min_score 过滤）"""
        from services.memory.hybrid_searcher import hybrid_search_memories

        async def mock_search_memories_fts(conn, query, limit=20, filters=None):
            return [
                {"id": "m0", "content": "good doc", "source": "conversation"},
                {"id": "m1", "content": "bad doc", "source": "conversation"},
                {"id": "m2", "content": "ok doc", "source": "conversation"},
            ]

        monkeypatch.setattr(
            "repositories.memories_repo.search_memories_fts",
            mock_search_memories_fts,
        )

        # m1 被 min_score 过滤掉
        reranker = _StubReranker(filter_ids={"m1"})
        results = await hybrid_search_memories(None, None, "query", reranker=reranker)

        ids = [r["id"] for r in results]
        assert "m1" not in ids
        assert len(results) == 2


class TestHybridSearchKnowledge:
    """hybrid_search_knowledge 返回 Markdown 字符串且包含标题"""

    async def test_returns_markdown_with_heading_and_title(self, monkeypatch):
        from services.memory.hybrid_searcher import hybrid_search_knowledge

        async def mock_search_knowledge_tree(conn, query, limit=10, filters=None):
            return [
                {
                    "id": "k1",
                    "title": "Knowledge Title",
                    "content": "Some knowledge content",
                    "score": 0.9,
                    "source": "auto_dream",
                    "tags": ["tag1"],
                    "created_at": 0,
                }
            ]

        monkeypatch.setattr(
            "repositories.knowledge_repo.search_knowledge_tree",
            mock_search_knowledge_tree,
        )

        md = await hybrid_search_knowledge(None, None, "query")
        assert isinstance(md, str)
        assert md.startswith("## 相关知识")
        assert "Knowledge Title" in md

    async def test_long_content_truncated_with_pointer(self, monkeypatch):
        """Task 6.15: 长 content 截断附溯源指针指向 knowledge_nodes 节点"""
        from services.memory.hybrid_searcher import hybrid_search_knowledge

        async def mock_search_knowledge_tree(conn, query, limit=10, filters=None):
            return [
                {
                    "id": "know-42",
                    "title": "Long Doc",
                    "content": "Z" * 2000,  # 远超 300 字符
                    "score": 0.9,
                    "source": "auto_dream",
                    "tags": [],
                    "created_at": 0,
                }
            ]

        monkeypatch.setattr(
            "repositories.knowledge_repo.search_knowledge_tree",
            mock_search_knowledge_tree,
        )

        md = await hybrid_search_knowledge(None, None, "query")
        assert "see node know-42 for full content" in md


# ═══════════════════════════════════════════════════════════
# Task 4.5: _weighted_fuse 修复 + FTS5 真实 BM25
# ═══════════════════════════════════════════════════════════

class TestMinMaxNormalize:
    """_min_max_normalize 边界处理"""

    def test_empty_list_no_op(self):
        from services.memory.hybrid_searcher import _min_max_normalize
        items = []
        _min_max_normalize(items)
        assert items == []

    def test_single_element_gets_neutral(self):
        """单元素列表——设为 0.5（中性分数，原代码设为 0.0 压平贡献）"""
        from services.memory.hybrid_searcher import _min_max_normalize
        items = [{"id": "a", "score": 0.5}]
        _min_max_normalize(items)
        assert items[0]["score"] == 0.5

    def test_all_same_values_get_neutral(self):
        """所有值相同——设为 0.5（原代码设为 0.0）"""
        from services.memory.hybrid_searcher import _min_max_normalize
        items = [
            {"id": "a", "score": 0.5},
            {"id": "b", "score": 0.5},
            {"id": "c", "score": 0.5},
        ]
        _min_max_normalize(items)
        for item in items:
            assert item["score"] == 0.5

    def test_normal_range_normalized_to_0_1(self):
        """正常范围归一化到 [0, 1]"""
        from services.memory.hybrid_searcher import _min_max_normalize
        items = [
            {"id": "a", "score": 1.0},
            {"id": "b", "score": 3.0},
            {"id": "c", "score": 5.0},
        ]
        _min_max_normalize(items)
        assert items[0]["score"] == 0.0  # min
        assert items[1]["score"] == 0.5  # mid
        assert items[2]["score"] == 1.0  # max

    def test_negative_values_normalized(self):
        """BM25 转正后的负值也能正确归一化"""
        from services.memory.hybrid_searcher import _min_max_normalize
        items = [
            {"id": "a", "score": 0.5},   # bm25=-0.5 → -bm25=0.5
            {"id": "b", "score": 2.5},   # bm25=-2.5 → -bm25=2.5（更相关）
        ]
        _min_max_normalize(items)
        assert items[0]["score"] == 0.0  # 较不相关
        assert items[1]["score"] == 1.0  # 更相关


class TestWeightedFuse:
    """_weighted_fuse 加权融合"""

    def test_fuses_vector_and_fts(self):
        """加权融合两路结果"""
        from services.memory.hybrid_searcher import _weighted_fuse
        vector = [
            {"id": "a", "content": "A", "score": 0.9},
            {"id": "b", "content": "B", "score": 0.3},
        ]
        fts = [
            {"id": "b", "content": "B", "score": 0.7},
            {"id": "c", "content": "C", "score": 0.1},
        ]
        fused = _weighted_fuse(vector, fts, vector_weight=0.7, fts_weight=0.3, limit=10)
        ids = [r["id"] for r in fused]
        assert "a" in ids
        assert "b" in ids
        assert "c" in ids

    def test_does_not_mutate_input(self):
        """Task 4.5: 不修改输入 list（防御性拷贝）"""
        from services.memory.hybrid_searcher import _weighted_fuse
        vector = [{"id": "a", "content": "A", "score": 0.9}]
        fts = [{"id": "b", "content": "B", "score": 0.1}]
        original_vector_score = vector[0]["score"]
        _weighted_fuse(vector, fts)
        # 输入 list 的 score 不应被修改
        assert vector[0]["score"] == original_vector_score

    def test_empty_inputs_return_empty(self):
        """两路都空——返回空列表"""
        from services.memory.hybrid_searcher import _weighted_fuse
        fused = _weighted_fuse([], [], limit=10)
        assert fused == []

    def test_fts_only_with_constant_score_not_flattened(self):
        """Task 4.5 核心修复：FTS5 所有 score 相同时不被压平为 0.0。

        原代码 _min_max_normalize 在 denom=0 时设为 0.0，导致 FTS 通道贡献被压平。
        修复后设为 0.5（中性），FTS 通道仍有贡献。
        """
        from services.memory.hybrid_searcher import _weighted_fuse
        # 模拟原 search_memories_fts 恒返回 0.5 的场景
        fts = [
            {"id": "a", "content": "A", "score": 0.5},
            {"id": "b", "content": "B", "score": 0.5},
        ]
        fused = _weighted_fuse([], fts, vector_weight=0.7, fts_weight=0.3, limit=10)
        # FTS 通道应该有贡献（原 bug 会返回空或 score=0）
        assert len(fused) == 2
        for item in fused:
            # fts_weight * 0.5 = 0.15
            assert item["score"] > 0

    def test_limit_respected(self):
        """limit 参数限制返回数量"""
        from services.memory.hybrid_searcher import _weighted_fuse
        vector = [{"id": f"v{i}", "content": "x", "score": 1.0 - i * 0.1} for i in range(10)]
        fused = _weighted_fuse(vector, [], limit=3)
        assert len(fused) == 3

    def test_id_in_both_channels_accumulates_score(self):
        """同一 id 在两路出现——分数累加"""
        from services.memory.hybrid_searcher import _weighted_fuse
        # 用 2 个元素让归一化有意义（最高分归一化后为 1.0）
        vector = [
            {"id": "shared", "content": "A", "score": 1.0},  # 归一化后 1.0（max）
            {"id": "other", "content": "B", "score": 0.2},   # 归一化后 0.0（min）
        ]
        fts = [
            {"id": "shared", "content": "A", "score": 0.8},  # 归一化后 1.0（max）
            {"id": "other2", "content": "C", "score": 0.1},  # 归一化后 0.0（min）
        ]
        fused = _weighted_fuse(vector, fts, vector_weight=0.7, fts_weight=0.3, limit=10)
        shared = next(r for r in fused if r["id"] == "shared")
        # 0.7 * 1.0 + 0.3 * 1.0 = 1.0
        assert abs(shared["score"] - 1.0) < 0.001


class TestSearchMemsFtsBm25:
    """search_memories_fts 返回真实 BM25 分数（Task 4.5）"""

    async def test_returns_real_bm25_score_not_constant(self, db):
        """FTS5 搜索返回真实 BM25 分数（非恒 0.5）"""
        from repositories.memories_repo import insert_memory, search_memories_fts
        # 插入 3 条记忆，相关性不同
        await insert_memory(db, content="Python is a programming language", source="test")
        await insert_memory(db, content="Python is great for data science", source="test")
        await insert_memory(db, content="The weather is nice today", source="test")

        results = await search_memories_fts(db, "Python programming", limit=3)
        assert len(results) > 0
        # 分数不应该是恒 0.5
        scores = [r["score"] for r in results]
        assert not all(s == 0.5 for s in scores), f"FTS score 仍是恒 0.5: {scores}"
        # 相关性高的（Python）分数应该比不相关的高
        python_results = [r for r in results if "Python" in r["content"]]
        weather_results = [r for r in results if "weather" in r["content"]]
        if python_results and weather_results:
            assert python_results[0]["score"] > weather_results[0]["score"]

    async def test_bm25_score_is_positive(self, db):
        """BM25 分数转正后是正数（越大越相关）"""
        from repositories.memories_repo import insert_memory, search_memories_fts
        await insert_memory(db, content="test content for bm25", source="test")
        results = await search_memories_fts(db, "test", limit=1)
        assert len(results) == 1
        assert results[0]["score"] > 0  # 转正后应为正数

    async def test_bm25_score_reflects_relevance(self, db):
        """BM25 分数反映相关性——多次匹配的文档分数更高"""
        from repositories.memories_repo import insert_memory, search_memories_fts
        # "python" 出现 3 次 vs 1 次
        await insert_memory(db, content="python python python programming", source="test")
        await insert_memory(db, content="python only once", source="test")
        results = await search_memories_fts(db, "python", limit=2)
        assert len(results) == 2
        # 多次匹配的分数应该更高
        multi = [r for r in results if "python python" in r["content"]][0]
        single = [r for r in results if "python only" in r["content"]][0]
        assert multi["score"] > single["score"]
