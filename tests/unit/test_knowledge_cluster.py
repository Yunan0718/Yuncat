"""tests/unit/test_knowledge_cluster.py —— Task 7.2 子任务1: RAPTOR 聚类

验证 RAPTOR 式知识树聚类——将语义相似的叶子节点聚合为父节点（LLM 摘要）。

对标:
  - RAPTOR 原论文 (Recursive Abstractive Processing for Tree-Organized Retrieval)
  - LangChain RAPTOR: cluster_embeddings + summarize_cluster + recursive
  - LlamaIndex RAPTOR: similar recursive clustering approach

测试策略:
  - Mock EmbeddingProvider: 显式向量映射，控制相似度
  - Mock summarizer: 固定返回字符串，验证调用
  - 内存 SQLite + 完整 schema
  - 不依赖真实 LLM / 真实 embedding 模型
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ============================================================
# Mock 工具
# ============================================================

class MockEmbeddingProvider:
    """Mock embedding provider——显式向量映射，控制相似度。

    用法: MockEmbeddingProvider({"text1": [1.0, 0.0], "text2": [0.9, 0.1]})
    相同方向的向量 cosine similarity ≈ 1.0，正交向量 ≈ 0.0。
    """

    def __init__(self, vector_map: dict[str, list[float]] | None = None):
        self._vector_map = vector_map or {}

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector_map.get(t, [0.0, 0.0, 0.0, 0.0]) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector_map.get(text, [0.0, 0.0, 0.0, 0.0])

    def name(self) -> str:
        return "mock_embedding"

    def __call__(self, input: list[str]) -> list[list[float]]:
        return self.embed_documents(input)


async def _mock_summarizer(text: str) -> str:
    """Mock LLM summarizer——返回固定格式摘要。"""
    return f"[SUMMARY] {text[:80]}"


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
async def cluster_db(tmp_path):
    """创建带完整 schema 的 DB（含 knowledge_nodes + parent_id）。"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_cluster.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    from repositories.schema import COLUMN_MIGRATIONS, _safe_add_column
    for table, column, definition in COLUMN_MIGRATIONS:
        await _safe_add_column(conn, table, column, definition)
    await conn.commit()
    yield conn
    await conn.close()


async def _insert_leaf(conn, node_id: str, title: str, content: str,
                       project_id: str | None = None, source: str = "conversation"):
    """插入一个叶子节点（parent_id IS NULL）。"""
    from repositories import upsert_knowledge_node
    await upsert_knowledge_node(
        conn, node_id, title, content,
        source=source, tags=["test"], score=1.0,
        project_id=project_id,
    )


# ============================================================
# list_knowledge_leaves 仓库函数测试
# ============================================================

class TestListKnowledgeLeaves:
    """Task 7.2: list_knowledge_leaves 仓库函数——返回 parent_id IS NULL 的节点"""

    async def test_returns_only_leaf_nodes(self, cluster_db):
        """只返回 parent_id IS NULL 的叶子节点"""
        from repositories.knowledge_repo import list_knowledge_leaves, upsert_knowledge_node
        # 叶子节点
        await upsert_knowledge_node(cluster_db, "leaf1", "Title1", "Content1")
        await upsert_knowledge_node(cluster_db, "leaf2", "Title2", "Content2")
        # 父节点（有子节点指向它）
        await upsert_knowledge_node(cluster_db, "parent1", "Parent", "Parent content")
        await cluster_db.execute(
            "UPDATE knowledge_nodes SET parent_id=? WHERE id=?", ("parent1", "leaf1")
        )
        await cluster_db.commit()

        leaves = await list_knowledge_leaves(cluster_db, limit=100)
        leaf_ids = [l["id"] for l in leaves]
        assert "leaf1" not in leaf_ids  # leaf1 现在有 parent 了
        assert "leaf2" in leaf_ids
        assert "parent1" in leaf_ids  # parent1 本身是叶子（它的 parent_id IS NULL）

    async def test_respects_limit(self, cluster_db):
        """limit 参数限制返回数量"""
        from repositories.knowledge_repo import list_knowledge_leaves
        for i in range(10):
            await _insert_leaf(cluster_db, f"leaf{i}", f"Title{i}", f"Content{i}")
        leaves = await list_knowledge_leaves(cluster_db, limit=5)
        assert len(leaves) == 5

    async def test_project_id_filter(self, cluster_db):
        """project_id 过滤——只返回该项目 + global 的叶子"""
        from repositories.knowledge_repo import list_knowledge_leaves
        await _insert_leaf(cluster_db, "global1", "Global", "Content", project_id=None)
        await _insert_leaf(cluster_db, "proj_a1", "ProjA", "Content A", project_id="proj_a")
        await _insert_leaf(cluster_db, "proj_b1", "ProjB", "Content B", project_id="proj_b")

        leaves = await list_knowledge_leaves(cluster_db, limit=100, project_id="proj_a")
        leaf_ids = [l["id"] for l in leaves]
        assert "global1" in leaf_ids  # global 可见
        assert "proj_a1" in leaf_ids
        assert "proj_b1" not in leaf_ids  # 其他项目不可见

    async def test_returns_content_field(self, cluster_db):
        """返回 content 字段（聚类需要）"""
        from repositories.knowledge_repo import list_knowledge_leaves
        await _insert_leaf(cluster_db, "leaf1", "Title", "Unique content here")
        leaves = await list_knowledge_leaves(cluster_db, limit=10)
        assert len(leaves) == 1
        assert "Unique content" in leaves[0]["content"]


# ============================================================
# cluster_knowledge_leaves 单级聚类测试
# ============================================================

class TestClusterKnowledgeLeaves:
    """Task 7.2: cluster_knowledge_leaves——单级 RAPTOR 聚类"""

    async def test_empty_leaves_no_clusters(self, cluster_db):
        """无叶子节点 → 0 clusters"""
        from services.knowledge_cluster import cluster_knowledge_leaves, ClusterConfig
        provider = MockEmbeddingProvider()
        result = await cluster_knowledge_leaves(
            cluster_db, provider, _mock_summarizer, config=ClusterConfig(),
        )
        assert result["clusters_created"] == 0
        assert result["leaves_grouped"] == 0

    async def test_below_min_cluster_size_no_clusters(self, cluster_db):
        """叶子数 < min_cluster_size → 不聚类"""
        from services.knowledge_cluster import cluster_knowledge_leaves, ClusterConfig
        # 2 个相似叶子，但 min_cluster_size=3
        vectors = {
            "Content A": [1.0, 0.0, 0.0, 0.0],
            "Content A variant": [0.95, 0.05, 0.0, 0.0],
        }
        provider = MockEmbeddingProvider(vectors)
        await _insert_leaf(cluster_db, "l1", "T1", "Content A")
        await _insert_leaf(cluster_db, "l2", "T2", "Content A variant")

        result = await cluster_knowledge_leaves(
            cluster_db, provider, _mock_summarizer,
            config=ClusterConfig(min_cluster_size=3),
        )
        assert result["clusters_created"] == 0
        assert result["leaves_grouped"] == 0

    async def test_similar_leaves_form_one_cluster(self, cluster_db):
        """3 个相似叶子 → 形成 1 个簇，创建父节点"""
        from services.knowledge_cluster import cluster_knowledge_leaves, ClusterConfig
        # 3 个高度相似的向量
        vectors = {
            "Python fact 1": [1.0, 0.0, 0.0, 0.0],
            "Python fact 2": [0.95, 0.05, 0.0, 0.0],
            "Python fact 3": [0.98, 0.02, 0.0, 0.0],
        }
        provider = MockEmbeddingProvider(vectors)
        await _insert_leaf(cluster_db, "l1", "T1", "Python fact 1")
        await _insert_leaf(cluster_db, "l2", "T2", "Python fact 2")
        await _insert_leaf(cluster_db, "l3", "T3", "Python fact 3")

        result = await cluster_knowledge_leaves(
            cluster_db, provider, _mock_summarizer,
            config=ClusterConfig(similarity_threshold=0.75, min_cluster_size=3),
        )
        assert result["clusters_created"] == 1
        assert result["leaves_grouped"] == 3

    async def test_dissimilar_leaves_no_clusters(self, cluster_db):
        """3 个完全不相似的叶子 → 不聚类"""
        from services.knowledge_cluster import cluster_knowledge_leaves, ClusterConfig
        # 正交向量——cosine similarity = 0
        vectors = {
            "Topic A": [1.0, 0.0, 0.0, 0.0],
            "Topic B": [0.0, 1.0, 0.0, 0.0],
            "Topic C": [0.0, 0.0, 1.0, 0.0],
        }
        provider = MockEmbeddingProvider(vectors)
        await _insert_leaf(cluster_db, "l1", "T1", "Topic A")
        await _insert_leaf(cluster_db, "l2", "T2", "Topic B")
        await _insert_leaf(cluster_db, "l3", "T3", "Topic C")

        result = await cluster_knowledge_leaves(
            cluster_db, provider, _mock_summarizer,
            config=ClusterConfig(similarity_threshold=0.75, min_cluster_size=3),
        )
        assert result["clusters_created"] == 0
        assert result["leaves_grouped"] == 0

    async def test_mixed_similarity_only_similar_grouped(self, cluster_db):
        """5 个叶子（3 相似 + 2 不相似）→ 只聚类 3 个相似的"""
        from services.knowledge_cluster import cluster_knowledge_leaves, ClusterConfig
        vectors = {
            "Python 1": [1.0, 0.0, 0.0, 0.0],
            "Python 2": [0.95, 0.05, 0.0, 0.0],
            "Python 3": [0.98, 0.02, 0.0, 0.0],
            "Rust 1": [0.0, 1.0, 0.0, 0.0],
            "Java 1": [0.0, 0.0, 1.0, 0.0],
        }
        provider = MockEmbeddingProvider(vectors)
        for i, content in enumerate(["Python 1", "Python 2", "Python 3", "Rust 1", "Java 1"], 1):
            await _insert_leaf(cluster_db, f"l{i}", f"T{i}", content)

        result = await cluster_knowledge_leaves(
            cluster_db, provider, _mock_summarizer,
            config=ClusterConfig(similarity_threshold=0.75, min_cluster_size=3),
        )
        assert result["clusters_created"] == 1
        assert result["leaves_grouped"] == 3  # only Python cluster

    async def test_parent_node_has_correct_source(self, cluster_db):
        """父节点 source='raptor_cluster'"""
        from services.knowledge_cluster import cluster_knowledge_leaves, ClusterConfig
        vectors = {
            "A": [1.0, 0.0], "B": [0.95, 0.05], "C": [0.98, 0.02],
        }
        provider = MockEmbeddingProvider(vectors)
        await _insert_leaf(cluster_db, "l1", "T1", "A")
        await _insert_leaf(cluster_db, "l2", "T2", "B")
        await _insert_leaf(cluster_db, "l3", "T3", "C")

        await cluster_knowledge_leaves(
            cluster_db, provider, _mock_summarizer,
            config=ClusterConfig(similarity_threshold=0.75, min_cluster_size=3),
        )
        cursor = await cluster_db.execute(
            "SELECT source FROM knowledge_nodes WHERE source='raptor_cluster'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1

    async def test_children_parent_id_updated(self, cluster_db):
        """子节点 parent_id 指向新创建的父节点"""
        from services.knowledge_cluster import cluster_knowledge_leaves, ClusterConfig
        vectors = {
            "A": [1.0, 0.0], "B": [0.95, 0.05], "C": [0.98, 0.02],
        }
        provider = MockEmbeddingProvider(vectors)
        await _insert_leaf(cluster_db, "l1", "T1", "A")
        await _insert_leaf(cluster_db, "l2", "T2", "B")
        await _insert_leaf(cluster_db, "l3", "T3", "C")

        await cluster_knowledge_leaves(
            cluster_db, provider, _mock_summarizer,
            config=ClusterConfig(similarity_threshold=0.75, min_cluster_size=3),
        )
        # 所有 3 个子节点应该有 parent_id
        cursor = await cluster_db.execute(
            "SELECT id, parent_id FROM knowledge_nodes WHERE id IN ('l1','l2','l3')"
        )
        rows = await cursor.fetchall()
        parent_ids = {r[1] for r in rows}
        assert len(parent_ids) == 1  # 同一个父节点
        parent_id = list(parent_ids)[0]
        assert parent_id is not None

    async def test_max_clusters_per_run_cap(self, cluster_db):
        """max_clusters_per_run 限制单次聚类簇数"""
        from services.knowledge_cluster import cluster_knowledge_leaves, ClusterConfig
        # 9 个相似叶子——理论上可形成 3 个簇（每簇 3 个），但 cap=1
        vectors = {}
        for i in range(9):
            vectors[f"content{i}"] = [1.0 - i * 0.01, 0.0]  # 都很相似
        provider = MockEmbeddingProvider(vectors)
        for i in range(9):
            await _insert_leaf(cluster_db, f"l{i}", f"T{i}", f"content{i}")

        result = await cluster_knowledge_leaves(
            cluster_db, provider, _mock_summarizer,
            config=ClusterConfig(
                similarity_threshold=0.75, min_cluster_size=3, max_clusters_per_run=1,
            ),
        )
        assert result["clusters_created"] == 1  # cap 限制

    async def test_project_id_filtering(self, cluster_db):
        """project_id 过滤——只聚类指定项目的叶子"""
        from services.knowledge_cluster import cluster_knowledge_leaves, ClusterConfig
        vectors = {
            "A": [1.0, 0.0], "B": [0.95, 0.05], "C": [0.98, 0.02],
        }
        provider = MockEmbeddingProvider(vectors)
        # proj_a 的 3 个相似叶子
        await _insert_leaf(cluster_db, "a1", "T1", "A", project_id="proj_a")
        await _insert_leaf(cluster_db, "a2", "T2", "B", project_id="proj_a")
        await _insert_leaf(cluster_db, "a3", "T3", "C", project_id="proj_a")
        # proj_b 的 3 个相似叶子（不应被聚类）
        await _insert_leaf(cluster_db, "b1", "T1", "A", project_id="proj_b")
        await _insert_leaf(cluster_db, "b2", "T2", "B", project_id="proj_b")
        await _insert_leaf(cluster_db, "b3", "T3", "C", project_id="proj_b")

        result = await cluster_knowledge_leaves(
            cluster_db, provider, _mock_summarizer,
            config=ClusterConfig(similarity_threshold=0.75, min_cluster_size=3),
            project_id="proj_a",
        )
        assert result["clusters_created"] == 1
        assert result["leaves_grouped"] == 3
        # proj_b 的叶子未被聚类（parent_id 仍为 NULL）
        cursor = await cluster_db.execute(
            "SELECT COUNT(*) FROM knowledge_nodes WHERE id IN ('b1','b2','b3') AND parent_id IS NULL"
        )
        count = (await cursor.fetchone())[0]
        assert count == 3

    async def test_parent_node_inherits_project_id(self, cluster_db):
        """父节点继承子节点的 project_id"""
        from services.knowledge_cluster import cluster_knowledge_leaves, ClusterConfig
        vectors = {"A": [1.0, 0.0], "B": [0.95, 0.05], "C": [0.98, 0.02]}
        provider = MockEmbeddingProvider(vectors)
        await _insert_leaf(cluster_db, "a1", "T1", "A", project_id="proj_a")
        await _insert_leaf(cluster_db, "a2", "T2", "B", project_id="proj_a")
        await _insert_leaf(cluster_db, "a3", "T3", "C", project_id="proj_a")

        await cluster_knowledge_leaves(
            cluster_db, provider, _mock_summarizer,
            config=ClusterConfig(similarity_threshold=0.75, min_cluster_size=3),
            project_id="proj_a",
        )
        cursor = await cluster_db.execute(
            "SELECT project_id FROM knowledge_nodes WHERE source='raptor_cluster'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "proj_a"


# ============================================================
# build_raptor_tree 递归多级聚类测试
# ============================================================

class TestBuildRaptorTree:
    """Task 7.2: build_raptor_tree——递归多级 RAPTOR 聚类"""

    async def test_recursive_multi_level(self, cluster_db):
        """9 个相似叶子 → level 1: 3 簇 × 3 → level 2: 1 簇 × 3 父节点"""
        from services.knowledge_cluster import build_raptor_tree, ClusterConfig
        # 3 组，每组 3 个高度相似的向量
        vectors = {
            # 组 1（Python）
            "py1": [1.0, 0.0, 0.0, 0.0],
            "py2": [0.99, 0.01, 0.0, 0.0],
            "py3": [0.98, 0.02, 0.0, 0.0],
            # 组 2（Rust）
            "rs1": [0.0, 1.0, 0.0, 0.0],
            "rs2": [0.0, 0.99, 0.01, 0.0],
            "rs3": [0.0, 0.98, 0.02, 0.0],
            # 组 3（Java）
            "jv1": [0.0, 0.0, 1.0, 0.0],
            "jv2": [0.0, 0.0, 0.99, 0.01],
            "jv3": [0.0, 0.0, 0.98, 0.02],
        }
        provider = MockEmbeddingProvider(vectors)
        for i, content in enumerate(
            ["py1", "py2", "py3", "rs1", "rs2", "rs3", "jv1", "jv2", "jv3"]
        ):
            await _insert_leaf(cluster_db, f"l{i}", f"T{i}", content)

        result = await build_raptor_tree(
            cluster_db, provider, _mock_summarizer,
            config=ClusterConfig(
                similarity_threshold=0.75, min_cluster_size=3, max_levels=3,
            ),
        )
        # Level 1: 3 个父节点（每组一个）
        # Level 2: 父节点的 embedding 由 summarizer 输出决定——mock 返回固定前缀，
        # 向量相同 → 可能形成 1 个簇（3 个父节点相似）
        assert result["clusters_created"] >= 3  # 至少 level 1 的 3 个
        assert result["levels"] >= 1

    async def test_max_levels_respected(self, cluster_db):
        """max_levels=1 → 只做一级聚类，不递归"""
        from services.knowledge_cluster import build_raptor_tree, ClusterConfig
        vectors = {
            "py1": [1.0, 0.0], "py2": [0.99, 0.01], "py3": [0.98, 0.02],
        }
        provider = MockEmbeddingProvider(vectors)
        await _insert_leaf(cluster_db, "l0", "T0", "py1")
        await _insert_leaf(cluster_db, "l1", "T1", "py2")
        await _insert_leaf(cluster_db, "l2", "T2", "py3")

        result = await build_raptor_tree(
            cluster_db, provider, _mock_summarizer,
            config=ClusterConfig(max_levels=1, similarity_threshold=0.75, min_cluster_size=3),
        )
        assert result["levels"] == 1
        assert result["clusters_created"] == 1

    async def test_empty_tree_returns_zeros(self, cluster_db):
        """空知识树 → 0 clusters, 0 levels"""
        from services.knowledge_cluster import build_raptor_tree, ClusterConfig
        provider = MockEmbeddingProvider()
        result = await build_raptor_tree(
            cluster_db, provider, _mock_summarizer, config=ClusterConfig(),
        )
        assert result["clusters_created"] == 0
        assert result["levels"] == 0
        assert result["leaves_grouped"] == 0


# ============================================================
# ClusterConfig 默认值测试
# ============================================================

class TestClusterConfig:
    """Task 7.2: ClusterConfig 默认值来自 config.py"""

    def test_defaults_from_config(self):
        """ClusterConfig 默认值与 config.RAPTOR_* 一致"""
        from services.knowledge_cluster import ClusterConfig
        from config import (
            RAPTOR_SIMILARITY_THRESHOLD, RAPTOR_MIN_CLUSTER_SIZE,
            RAPTOR_MAX_CLUSTERS_PER_RUN, RAPTOR_MAX_LEAVES_TO_CONSIDER,
            RAPTOR_SUMMARY_MAX_CHARS, RAPTOR_MAX_LEVELS,
        )
        cfg = ClusterConfig()
        assert cfg.similarity_threshold == RAPTOR_SIMILARITY_THRESHOLD
        assert cfg.min_cluster_size == RAPTOR_MIN_CLUSTER_SIZE
        assert cfg.max_clusters_per_run == RAPTOR_MAX_CLUSTERS_PER_RUN
        assert cfg.max_leaves_to_consider == RAPTOR_MAX_LEAVES_TO_CONSIDER
        assert cfg.summary_max_chars == RAPTOR_SUMMARY_MAX_CHARS
        assert cfg.max_levels == RAPTOR_MAX_LEVELS

    def test_custom_config_overrides_defaults(self):
        """自定义参数覆盖默认值"""
        from services.knowledge_cluster import ClusterConfig
        cfg = ClusterConfig(similarity_threshold=0.9, min_cluster_size=5)
        assert cfg.similarity_threshold == 0.9
        assert cfg.min_cluster_size == 5
