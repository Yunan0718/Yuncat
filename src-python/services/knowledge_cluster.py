"""services/knowledge_cluster.py —— Task 7.2 子任务1: RAPTOR 聚类

RAPTOR (Recursive Abstractive Processing for Tree-Organized Retrieval) 式
知识树聚类——将语义相似的叶子节点聚合为父节点（LLM 摘要），构建多级知识树。

对标:
  - RAPTOR 原论文: 递归聚类 + 摘要 → 树形知识结构
  - LangChain RAPTOR: cluster_embeddings + summarize_cluster + recursive
  - LlamaIndex RAPTOR: similar recursive clustering approach

设计:
  1. 获取叶子节点（parent_id IS NULL）——通过 list_knowledge_leaves
  2. 通过 EmbeddingProvider 计算所有叶子 content 的嵌入向量
  3. 贪心聚类（cosine similarity >= threshold）——O(n²)，n<=100 可接受
  4. 每个簇（size >= min_cluster_size）调用 LLM 生成摘要
  5. 创建父节点（source='raptor_cluster'）+ 更新子节点 parent_id
  6. 递归：新父节点成为下一级的叶子，直到 < min_cluster_size 或 max_levels

关键决策:
  - 贪心聚类而非 GMM/k-means——实现简单，n<=100 时效果相当（YAGNI）
  - summarizer 为注入式 Callable——解耦 AgentService，便于测试
  - 父节点 indexed_at=NULL——Outbox pattern，MemoryIndexerWorker 兜底索引到 chromadb
  - project_id 继承——父节点继承子节点的 project_id（项目隔离一致性）
"""
from __future__ import annotations

import logging
import math
import time
import uuid as _uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from config import (
    RAPTOR_SIMILARITY_THRESHOLD,
    RAPTOR_MIN_CLUSTER_SIZE,
    RAPTOR_MAX_CLUSTERS_PER_RUN,
    RAPTOR_MAX_LEAVES_TO_CONSIDER,
    RAPTOR_SUMMARY_MAX_CHARS,
    RAPTOR_MAX_LEVELS,
)

_log = logging.getLogger("yuncat.knowledge_cluster")

# LLM 摘要 prompt——将多个子节点内容整合为一个父节点摘要
_SUMMARY_PROMPT_TEMPLATE = """## 任务
将以下 {count} 个知识节点的内容整合为一个简洁的摘要，作为它们的父节点。

## 子节点内容
{content}

## 要求
1. 保留关键事实和核心概念
2. 去除重复信息
3. 用简洁的 Markdown 格式输出
4. 不超过 {max_chars} 字符
5. 不要添加子节点中没有的信息（忠实聚合，不创作）
"""


@dataclass
class ClusterConfig:
    """RAPTOR 聚类配置——默认值来自 config.RAPTOR_* 常量。

    所有阈值均可通过构造参数覆盖，支持运行时定制（如测试用更小阈值）。
    """
    similarity_threshold: float = RAPTOR_SIMILARITY_THRESHOLD
    min_cluster_size: int = RAPTOR_MIN_CLUSTER_SIZE
    max_clusters_per_run: int = RAPTOR_MAX_CLUSTERS_PER_RUN
    max_leaves_to_consider: int = RAPTOR_MAX_LEAVES_TO_CONSIDER
    summary_max_chars: int = RAPTOR_SUMMARY_MAX_CHARS
    max_levels: int = RAPTOR_MAX_LEVELS


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度。

    返回值范围 [-1, 1]：
      - 1.0 = 完全相同方向
      - 0.0 = 正交（不相关）
      - -1.0 = 完全相反方向

    零向量返回 0.0（避免除零）。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _greedy_cluster(
    vectors: list[list[float]],
    threshold: float,
) -> list[list[int]]:
    """贪心聚类——将相似向量分组。

    算法:
      1. 对每个未分配的向量 i，以其为种子
      2. 遍历所有未分配的 j > i，若 cosine_sim(i, j) >= threshold 则归入同簇
      3. 标记已分配，继续下一个未分配向量

    返回: 簇列表，每个簇是向量索引列表。
    单元素簇（无相似伙伴）也会返回——调用方按 min_cluster_size 过滤。

    对标 RAPTOR 论文的 greedy clustering（简单且 n<=100 时效果与 GMM 相当）。
    """
    n = len(vectors)
    assigned = [False] * n
    clusters: list[list[int]] = []
    for i in range(n):
        if assigned[i]:
            continue
        cluster = [i]
        assigned[i] = True
        for j in range(i + 1, n):
            if not assigned[j]:
                sim = _cosine_similarity(vectors[i], vectors[j])
                if sim >= threshold:
                    cluster.append(j)
                    assigned[j] = True
        clusters.append(cluster)
    return clusters


async def cluster_knowledge_leaves(
    db,
    embedding_provider,
    summarizer: Callable[[str], Awaitable[str]],
    config: ClusterConfig | None = None,
    project_id: str | None = None,
) -> dict:
    """单级 RAPTOR 聚类——将语义相似的叶子节点聚合为父节点。

    流程:
      1. 查询 parent_id IS NULL 的叶子节点（限 max_leaves_to_consider）
      2. 通过 embedding_provider.embed_documents 计算 content 的嵌入向量
      3. 贪心聚类（cosine similarity >= threshold）
      4. 对 size >= min_cluster_size 的簇调用 summarizer 生成摘要
      5. 创建父节点（source='raptor_cluster'）+ 更新子节点 parent_id
         （父节点 indexed_at=NULL——Outbox pattern，worker 兜底索引到 chromadb）

    Args:
        db: aiosqlite 连接
        embedding_provider: EmbeddingProvider（实现 embed_documents）
        summarizer: async Callable[[str], str]——接收子节点拼接内容，返回摘要
        config: 聚类配置（None 用默认值）
        project_id: 仅聚类该项目的叶子（None = 全部，含 global）

    Returns:
        {"clusters_created": int, "leaves_grouped": int}
    """
    from repositories.knowledge_repo import list_knowledge_leaves, upsert_knowledge_node

    cfg = config or ClusterConfig()
    leaves = await list_knowledge_leaves(
        db, limit=cfg.max_leaves_to_consider, project_id=project_id,
    )
    if len(leaves) < cfg.min_cluster_size:
        return {"clusters_created": 0, "leaves_grouped": 0}

    # 计算嵌入向量
    contents = [l["content"] for l in leaves]
    try:
        vectors = embedding_provider.embed_documents(contents)
    except Exception:
        _log.exception("RAPTOR: embed_documents failed, skipping clustering")
        return {"clusters_created": 0, "leaves_grouped": 0}

    # 贪心聚类
    raw_clusters = _greedy_cluster(vectors, cfg.similarity_threshold)
    # 过滤掉 < min_cluster_size 的簇
    valid_clusters = [c for c in raw_clusters if len(c) >= cfg.min_cluster_size]
    # 安全上限——防 LLM 调用风暴
    valid_clusters = valid_clusters[:cfg.max_clusters_per_run]

    if not valid_clusters:
        return {"clusters_created": 0, "leaves_grouped": 0}

    clusters_created = 0
    leaves_grouped = 0

    for cluster_indices in valid_clusters:
        cluster_leaves = [leaves[i] for i in cluster_indices]
        # 拼接子节点内容供 summarizer 使用
        joined_content = "\n\n---\n\n".join(
            f"### {l['title']}\n{l['content']}" for l in cluster_leaves
        )
        prompt = _SUMMARY_PROMPT_TEMPLATE.format(
            count=len(cluster_leaves),
            content=joined_content[:cfg.summary_max_chars * 2],  # 输入预算给 2x 余量
            max_chars=cfg.summary_max_chars,
        )

        try:
            summary = await summarizer(prompt)
        except Exception:
            _log.exception("RAPTOR: summarizer failed for cluster, skipping")
            continue

        if not summary or not summary.strip():
            _log.debug("RAPTOR: empty summary, skipping cluster")
            continue

        # 创建父节点——继承子节点的 project_id（取第一个子节点的）
        parent_project_id = cluster_leaves[0].get("project_id")
        parent_id = f"raptor_{_uuid.uuid4()}"
        parent_title = summary.strip().split("\n")[0][:80] or f"RAPTOR cluster ({len(cluster_leaves)} leaves)"
        await upsert_knowledge_node(
            db, parent_id, parent_title, summary[:cfg.summary_max_chars],
            parent_id=None, source="raptor_cluster", source_ref="",
            tags=["raptor", "cluster"], score=1.0,
            project_id=parent_project_id,
        )

        # 更新子节点 parent_id
        now = int(time.time())
        for leaf in cluster_leaves:
            await db.execute(
                "UPDATE knowledge_nodes SET parent_id=?, updated_at=? WHERE id=?",
                (parent_id, now, leaf["id"]),
            )
        await db.commit()

        clusters_created += 1
        leaves_grouped += len(cluster_leaves)

    _log.info(
        "RAPTOR clustering: %d clusters created, %d leaves grouped (project_id=%s)",
        clusters_created, leaves_grouped, project_id or "all",
    )
    return {"clusters_created": clusters_created, "leaves_grouped": leaves_grouped}


async def build_raptor_tree(
    db,
    embedding_provider,
    summarizer: Callable[[str], Awaitable[str]],
    config: ClusterConfig | None = None,
    project_id: str | None = None,
) -> dict:
    """递归多级 RAPTOR 聚类——构建多级知识树。

    流程:
      1. 调用 cluster_knowledge_leaves 做第一级聚类
      2. 新创建的父节点成为下一级的叶子（parent_id IS NULL）
      3. 递归聚类，直到:
         - 无新簇创建
         - 或达到 max_levels

    对标 RAPTOR 论文的递归树构建 + LangChain RAPTOR 的 recursive summarization。

    Returns:
        {"clusters_created": int, "leaves_grouped": int, "levels": int}
    """
    cfg = config or ClusterConfig()
    total_created = 0
    total_grouped = 0
    levels = 0

    for level in range(cfg.max_levels):
        result = await cluster_knowledge_leaves(
            db, embedding_provider, summarizer, config=cfg, project_id=project_id,
        )
        created = result["clusters_created"]
        if created == 0:
            break
        total_created += created
        total_grouped += result["leaves_grouped"]
        levels += 1
        _log.debug("RAPTOR level %d: %d clusters", level + 1, created)

    return {
        "clusters_created": total_created,
        "leaves_grouped": total_grouped,
        "levels": levels,
    }
