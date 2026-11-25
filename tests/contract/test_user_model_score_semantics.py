"""contract: 用户建模的评分语义与字段完整性

背景（2026-09-02 修复）：knowledge_nodes 的 score 同时被两种语义消费——
「节点持久化置信度」与「BM25 检索相关性」。search_knowledge_tree 返回的
"score" 键是后者，但 feedback_learning / memory_tree 四处把它当成前者，
造成一组同源缺陷：

  1. 反馈永不累积 —— 每次 record_feedback 都把评分覆盖成「相关性 ± delta」，
     实测连续接受 5 次评分纹丝不动（0.915606 × 5）。
  2. 字段被覆盖 —— 用 upsert_knowledge_node 回写时不传 source/tags/project_id，
     UPDATE 分支即写入默认值：source→auto_dream、tags→[]、project_id→NULL。
     capability_router._query_learned_from_db 同时依赖
     `source='agent_delegation'` 与 `tags LIKE '%agent_id%'`，
     命中数实测归 0，委托反馈闭环断裂；project_id 置 NULL 还破坏项目隔离。
  3. 语义污染 —— get_personalization_context 无 source 过滤，
     workflow_summary / spec_loop_escalation / agent_perf 等内部记账节点
     以「## 你的偏好（已学习）」标题被拼进 system prompt。

本文件锁定修复后的不变量，防止回归。
"""
import json
import os
import sys
import time

import aiosqlite
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from config import (  # noqa: E402
    KNOWLEDGE_SCORE_CEILING,
    KNOWLEDGE_SCORE_FLOOR,
    PERSONALIZATION_EXCLUDED_SOURCES,
    PERSONALIZATION_SCORE_THRESHOLD,
)
from repositories import adjust_knowledge_score  # noqa: E402
from repositories.knowledge_repo import search_knowledge_tree  # noqa: E402
from repositories.schema import SCHEMA_SQL  # noqa: E402
from services.feedback_learning import FeedbackLearner  # noqa: E402


async def _new_db():
    """创建带完整 schema 的内存库（含 knowledge_fts 与其触发器）。"""
    db = await aiosqlite.connect(":memory:")
    await db.executescript(SCHEMA_SQL)
    await db.commit()
    return db


async def _insert(db, node_id, title, content, source, score,
                  tags=None, project_id=None):
    await db.execute(
        "INSERT INTO knowledge_nodes(id, title, content, source, score, tags, "
        "project_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (node_id, title, content, source, score,
         json.dumps(tags or []), project_id, int(time.time()), int(time.time())),
    )
    await db.commit()


async def _row(db, node_id):
    cur = await db.execute(
        "SELECT source, tags, project_id, score FROM knowledge_nodes WHERE id=?",
        (node_id,),
    )
    return await cur.fetchone()


# ---- 1. 反馈必须累积 ----

@pytest.mark.asyncio
async def test_feedback_accumulates_across_events():
    """回归守卫：连续接受必须逐次累加，而不是被相关性反复钉死在同一值。

    修复前实测序列为 [0.915606] * 5——因为每次都用 BM25 相关性当基线。
    """
    db = await _new_db()
    await _insert(db, "n1", "python 异步模式",
                  "python 异步编程最佳实践 asyncio", "conversation", 0.5)
    learner = FeedbackLearner(db=db)

    seq = []
    for _ in range(5):
        await learner.record_feedback(
            event_type="human_accepted", task_id="", context="python 异步模式",
        )
        seq.append(round((await _row(db, "n1"))[3], 6))

    assert seq == [0.6, 0.7, 0.8, 0.9, 1.0], f"反馈未累积，实测序列 {seq}"
    await db.close()


@pytest.mark.asyncio
async def test_reject_decrements_from_accumulated_score():
    """拒绝必须从累积值上扣减，而不是把评分砸到地板。"""
    db = await _new_db()
    await _insert(db, "n1", "python 异步模式",
                  "python 异步编程最佳实践 asyncio", "conversation", 0.9)
    learner = FeedbackLearner(db=db)

    await learner.record_feedback(
        event_type="human_rejected", task_id="", context="python 异步模式",
    )
    # 0.9 + (-0.15) = 0.75
    assert round((await _row(db, "n1"))[3], 6) == pytest.approx(0.75)
    await db.close()


# ---- 2. 字段完整性：source / tags / project_id 不得被反馈覆盖 ----

@pytest.mark.asyncio
async def test_feedback_preserves_provenance_fields():
    """回归守卫：source / tags / project_id 必须原样保留。

    三者各自支撑一条下游契约：
      source → capability_router 的委托学习过滤
      tags   → 同上，关联到具体 agent
      project_id → 项目隔离（置 NULL 会让项目级知识泄漏为全局）
    """
    db = await _new_db()
    await _insert(db, "n1", "委托任务: python 性能分析",
                  "python 性能分析 由 hermes 完成", "agent_delegation", 0.5,
                  tags=["hermes"], project_id="projA")
    learner = FeedbackLearner(db=db)

    await learner.record_feedback(
        event_type="human_accepted", task_id="", context="python 性能分析",
    )
    source, tags, project_id, score = await _row(db, "n1")

    assert source == "agent_delegation", f"source 被覆盖为 {source!r}"
    assert json.loads(tags) == ["hermes"], f"tags 被覆盖为 {tags!r}"
    assert project_id == "projA", f"project_id 被覆盖为 {project_id!r}"
    assert round(score, 6) == pytest.approx(0.6)
    await db.close()


@pytest.mark.asyncio
async def test_delegation_learning_query_still_finds_adjusted_node():
    """复刻 capability_router._query_learned_from_db 的查询，断言闭环可达。

    修复前该查询在节点被反馈调整后命中数为 0——委托反馈闭环事实上是断的。
    """
    db = await _new_db()
    await _insert(db, "n1", "委托任务: python 性能分析",
                  "python 性能分析 由 hermes 完成", "agent_delegation", 0.5,
                  tags=["hermes"], project_id="projA")
    learner = FeedbackLearner(db=db)
    await learner.record_feedback(
        event_type="human_accepted", task_id="", context="python 性能分析",
    )

    cur = await db.execute(
        "SELECT COUNT(*), AVG(score) FROM knowledge_nodes "
        "WHERE (source='agent_delegation' "
        "       OR (source='mixed_deliberation' AND title LIKE 'agent_perf:%')) "
        "AND tags LIKE ?", ("%hermes%",),
    )
    row = await cur.fetchone()
    assert row[0] == 1, "委托学习查询命中数为 0——反馈闭环断裂"
    await db.close()


# ---- 3. adjust_knowledge_score 的边界 ----

@pytest.mark.asyncio
async def test_adjust_clamps_to_ceiling_and_floor():
    """评分必须钳在 [floor, ceiling]，否则 capability_router 把 AVG(score)
    当 success_rate 与 0.7 阈值比较时，阈值会永久成立（假学习）。"""
    db = await _new_db()
    await _insert(db, "hi", "python 上限", "python 内容", "conversation", 0.95)
    await _insert(db, "lo", "python 下限", "python 内容", "conversation", 0.02)

    assert await adjust_knowledge_score(db, "hi", 0.5) == pytest.approx(KNOWLEDGE_SCORE_CEILING)
    assert await adjust_knowledge_score(db, "lo", -0.5) == pytest.approx(KNOWLEDGE_SCORE_FLOOR)
    await db.close()


@pytest.mark.asyncio
async def test_adjust_missing_node_returns_none():
    """节点不存在时返回 None，而不是凭空创建。"""
    db = await _new_db()
    assert await adjust_knowledge_score(db, "does-not-exist", 0.1) is None
    await db.close()


# ---- 4. search_knowledge_tree 的双 score 语义 ----

@pytest.mark.asyncio
async def test_search_exposes_node_score_distinct_from_relevance():
    """"node_score" 必须等于节点持久化评分；"score" 仍是 BM25 相关性。

    两个键不可互换——这是本次缺陷的根因，必须与调用方一起锁定。
    """
    db = await _new_db()
    await _insert(db, "n1", "python 部署流程",
                  "python 项目的 docker 部署流程", "conversation", 0.42)

    results = await search_knowledge_tree(db, "python", limit=5)
    hit = next((r for r in results if r["id"] == "n1"), None)
    assert hit is not None, "FTS5 未命中测试节点"

    assert "node_score" in hit, "search_knowledge_tree 未暴露 node_score 键"
    assert hit["node_score"] == pytest.approx(0.42), (
        f"node_score 应为持久化评分 0.42，实测 {hit['node_score']}"
    )
    await db.close()


# ---- 5. get_personalization_context 的 source 过滤 ----

@pytest.mark.asyncio
async def test_personalization_excludes_internal_bookkeeping_sources():
    """回归守卫：内部记账节点不得以「你的偏好」的名义注入 system prompt。"""
    db = await _new_db()
    internal = [
        ("wf", "工作流执行摘要: 每日构建", "构建结果", "workflow_summary"),
        ("sp", "Spec 升级: 登录模块", "需要复审", "spec_loop_escalation"),
        ("pf", "agent_perf:架构师→hermes", "路由统计", "mixed_deliberation"),
        ("gc", "gc:deadcode:src/foo.py", "技术债", "gc_agent"),
        ("ad", "来源不明的节点", "auto_dream 默认 source", "auto_dream"),
    ]
    for nid, title, content, source in internal:
        await _insert(db, nid, title, content, source, 0.99)
    learner = FeedbackLearner(db=db)

    ctx = await learner.get_personalization_context(limit=20)
    for nid, title, _content, source in internal:
        assert title not in ctx, (
            f"内部记账节点 {source!r} 泄漏进「你的偏好」: {title}"
        )
    await db.close()


@pytest.mark.asyncio
async def test_personalization_keeps_user_facing_sources():
    """排除表不能反过来把真正的用户知识也挡掉。

    用排除表（而非白名单）的用意就在「新增用户向来源默认可见」，
    这条测试锁定该性质。
    """
    db = await _new_db()
    user_facing = [
        ("m1", "用户的 MEMORY.md 条目", "偏好：回复简洁", "memory_md_import"),
        ("m2", "openclaw 同步记忆", "用户不喜欢 emoji", "openclaw_memory"),
        ("m3", "hermes 同步记忆", "偏好中文回复", "hermes_agent_memory"),
        ("m4", "会话沉淀知识", "用户偏好 TDD", "conversation"),
    ]
    for nid, title, content, source in user_facing:
        await _insert(db, nid, title, content, source, PERSONALIZATION_SCORE_THRESHOLD)
    learner = FeedbackLearner(db=db)

    ctx = await learner.get_personalization_context(limit=20)
    for nid, title, _content, source in user_facing:
        assert title in ctx, f"用户向来源 {source!r} 被误挡: {title}"
    await db.close()


@pytest.mark.asyncio
async def test_excluded_sources_stay_in_sync_with_writers():
    """排除表里登记的 source 必须是真实存在的写入端常量。

    防止排除表腐烂（改名后静默失效）——若某个 source 在代码里再也搜不到，
    说明要么已废弃（应清理），要么拼错了（应修正）。
    """
    import pathlib

    src_root = pathlib.Path(__file__).resolve().parents[2] / "src-python"
    haystack = []
    for path in src_root.rglob("*.py"):
        if "python-embed" in path.parts:
            continue
        try:
            haystack.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    corpus = "\n".join(haystack)

    missing = sorted(s for s in PERSONALIZATION_EXCLUDED_SOURCES if s not in corpus)
    assert not missing, (
        f"排除表中的 source 在 src-python 下已无写入端，请核实拼写或清理: {missing}"
    )


# ---- 6. track_temporary_role 的基线 ----

@pytest.mark.asyncio
async def test_track_temporary_role_accumulates_from_node_score():
    """重复使用的临时角色必须从真实评分累加，而不是被相关性重置。"""
    db = await _new_db()
    await _insert(db, "role1", "临时角色: python 评审",
                  "自动生成的临时讨论角色", "deliberation_tmp_role", 0.5)
    learner = FeedbackLearner(db=db)

    node_id = await learner.track_temporary_role(
        role_name="python 评审", task_prompt="评审 python 代码", was_accepted=True,
    )
    assert node_id == "role1"
    # 0.5 + 0.2 = 0.7（修复前是 min(1.0, 相关性 + 0.2)，与 0.5 无关）
    assert round((await _row(db, "role1"))[3], 6) == pytest.approx(0.7)
    await db.close()
