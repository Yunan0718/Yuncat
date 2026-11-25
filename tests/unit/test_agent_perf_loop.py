"""tests/unit/test_agent_perf_loop.py —— Task 7.1: agent_perf 学习闭环测试

验证讨论角色表现（agent_perf）数据的完整闭环：
1. 写入端：deliberation_discussion 写入 agent_perf 节点时 tags 包含 agent_id（让查询可达）
2. 读取端：capability_router._query_learned_from_db 能查到 mixed_deliberation source 的 agent_perf 节点
3. 反馈端：feedback_learning.record_deliberation_feedback 接受/拒绝时调整 agent_perf 节点 score

对标 capability_router 现有 agent_delegation 反馈学习闭环（Phase 48 T2-1）。
"""
import os
import sys
import json
import time
import uuid
import pytest

src_path = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
sys.path.insert(0, src_path)


@pytest.fixture
async def fresh_db():
    """每个测试用独立内存 DB，避免互相干扰"""
    from repositories import init_db, run_migrations
    db = await init_db(":memory:")
    await run_migrations(db)
    yield db
    await db.close()


async def _insert_agent_perf_node(db, role_name: str, agent_used: str, score: float = 0.5, tags=None):
    """辅助：插入一个 agent_perf 节点（模拟 deliberation_discussion 写入）"""
    if tags is None:
        # Task 7.1 修复后：tags 应包含 agent_id
        tags = json.dumps([agent_used])
    node_id = str(uuid.uuid4())[:12]
    title = f"agent_perf:{role_name}→{agent_used}"
    await db.execute(
        "INSERT INTO knowledge_nodes(id, title, content, source, score, tags, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (node_id, title, f"Role: {role_name}\nAgent: {agent_used}",
         "mixed_deliberation", score, tags, int(time.time()), int(time.time())),
    )
    await db.commit()
    return node_id


class TestQueryLearnedFromDbAgentPerf:
    """Task 7.1: _query_learned_from_db 能查到 mixed_deliberation 的 agent_perf 节点"""

    @pytest.mark.asyncio
    async def test_agent_perf_node_with_tags_is_queryable(self, fresh_db, monkeypatch):
        """agent_perf 节点 tags 含 agent_id → _query_learned_from_db 能查到"""
        import context
        monkeypatch.setattr(context, "get_db", lambda: fresh_db)
        from services.capability_router import _query_learned_from_db, AgentProfile

        # 写入一个 agent_perf 节点（tags 含 agent_id，修复后行为）
        await _insert_agent_perf_node(fresh_db, "Architect", "hermes", score=0.8)

        profile = AgentProfile(
            agent_id="hermes", name="Hermes", strengths=[], keywords=["architect"],
            tools=[], supports_image=False, supports_browser=False, supports_shell=False, supports_search=False,
        )

        result = await _query_learned_from_db("architect design", [profile])
        assert result is not None
        key = "delegation:hermes"
        assert key in result
        assert result[key]["count"] >= 1
        # score 0.8 应被聚合到 success_rate
        assert result[key]["success_rate"] > 0.7

    @pytest.mark.asyncio
    async def test_agent_delegation_still_works(self, fresh_db, monkeypatch):
        """Task 7.1 改动不破坏现有 agent_delegation 查询"""
        import context
        monkeypatch.setattr(context, "get_db", lambda: fresh_db)
        from services.capability_router import _query_learned_from_db, AgentProfile

        # 写入一个 agent_delegation 节点
        await fresh_db.execute(
            "INSERT INTO knowledge_nodes(id, title, content, source, score, tags, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4())[:12], "user_feedback:test", "content",
             "agent_delegation", 0.9, json.dumps(["hermes"]), int(time.time()), int(time.time())),
        )
        await fresh_db.commit()

        profile = AgentProfile(
            agent_id="hermes", name="Hermes", strengths=[], keywords=["test"],
            tools=[], supports_image=False, supports_browser=False, supports_shell=False, supports_search=False,
        )
        result = await _query_learned_from_db("test query", [profile])
        assert result is not None
        assert result["delegation:hermes"]["count"] >= 1
        assert result["delegation:hermes"]["success_rate"] >= 0.85

    @pytest.mark.asyncio
    async def test_aggregation_merges_both_sources(self, fresh_db, monkeypatch):
        """Task 7.1: agent_delegation + mixed_deliberation 两种 source 加权聚合"""
        import context
        monkeypatch.setattr(context, "get_db", lambda: fresh_db)
        from services.capability_router import _query_learned_from_db, AgentProfile

        # 写入 1 个 agent_delegation（score=0.9）+ 1 个 agent_perf（score=0.5）
        await fresh_db.execute(
            "INSERT INTO knowledge_nodes(id, title, content, source, score, tags, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4())[:12], "user_feedback:t1", "c1",
             "agent_delegation", 0.9, json.dumps(["hermes"]), int(time.time()), int(time.time())),
        )
        await _insert_agent_perf_node(fresh_db, "Architect", "hermes", score=0.5)

        profile = AgentProfile(
            agent_id="hermes", name="Hermes", strengths=[], keywords=["architect"],
            tools=[], supports_image=False, supports_browser=False, supports_shell=False, supports_search=False,
        )
        result = await _query_learned_from_db("architect", [profile])
        assert result is not None
        data = result["delegation:hermes"]
        assert data["count"] == 2  # 两种 source 各 1 条
        # 加权平均 (0.9 + 0.5) / 2 = 0.7
        assert 0.65 < data["success_rate"] < 0.75


class TestRecordDeliberationFeedback:
    """Task 7.1: feedback_learning.record_deliberation_feedback 调整 agent_perf score"""

    @pytest.mark.asyncio
    async def test_accept_boosts_score(self, fresh_db):
        """接受讨论结果 → agent_perf 节点 score +0.10"""
        from services.feedback_learning import FeedbackLearner
        learner = FeedbackLearner(db=fresh_db)

        node_id = await _insert_agent_perf_node(fresh_db, "Architect", "hermes", score=0.5)

        await learner.record_deliberation_feedback("Architect", "hermes", was_accepted=True)

        cursor = await fresh_db.execute("SELECT score FROM knowledge_nodes WHERE id=?", (node_id,))
        row = await cursor.fetchone()
        assert row is not None
        assert abs(row[0] - 0.6) < 0.01  # 0.5 + 0.10 = 0.6

    @pytest.mark.asyncio
    async def test_reject_lowers_score(self, fresh_db):
        """拒绝讨论结果 → agent_perf 节点 score -0.15"""
        from services.feedback_learning import FeedbackLearner
        learner = FeedbackLearner(db=fresh_db)

        node_id = await _insert_agent_perf_node(fresh_db, "Developer", "openclaw", score=0.7)

        await learner.record_deliberation_feedback("Developer", "openclaw", was_accepted=False)

        cursor = await fresh_db.execute("SELECT score FROM knowledge_nodes WHERE id=?", (node_id,))
        row = await cursor.fetchone()
        assert row is not None
        assert abs(row[0] - 0.55) < 0.01  # 0.7 - 0.15 = 0.55

    @pytest.mark.asyncio
    async def test_score_clamped_to_min(self, fresh_db):
        """score 不会低于 0.01"""
        from services.feedback_learning import FeedbackLearner
        learner = FeedbackLearner(db=fresh_db)

        node_id = await _insert_agent_perf_node(fresh_db, "Reviewer", "hermes", score=0.05)

        await learner.record_deliberation_feedback("Reviewer", "hermes", was_accepted=False)

        cursor = await fresh_db.execute("SELECT score FROM knowledge_nodes WHERE id=?", (node_id,))
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] >= 0.01

    @pytest.mark.asyncio
    async def test_no_matching_node_is_safe(self, fresh_db):
        """无匹配 agent_perf 节点时不报错"""
        from services.feedback_learning import FeedbackLearner
        learner = FeedbackLearner(db=fresh_db)

        # 不写入任何节点，直接调用——不应抛异常
        await learner.record_deliberation_feedback("NonExistent", "hermes", was_accepted=True)

    @pytest.mark.asyncio
    async def test_only_updates_matching_role_agent(self, fresh_db):
        """只更新匹配 role_name + agent_used 的节点，不影响其他"""
        from services.feedback_learning import FeedbackLearner
        learner = FeedbackLearner(db=fresh_db)

        # 写入两个 agent_perf 节点：Architect→hermes 和 Developer→openclaw
        node1 = await _insert_agent_perf_node(fresh_db, "Architect", "hermes", score=0.5)
        node2 = await _insert_agent_perf_node(fresh_db, "Developer", "openclaw", score=0.5)

        # 只接受 Architect→hermes
        await learner.record_deliberation_feedback("Architect", "hermes", was_accepted=True)

        cursor1 = await fresh_db.execute("SELECT score FROM knowledge_nodes WHERE id=?", (node1,))
        cursor2 = await fresh_db.execute("SELECT score FROM knowledge_nodes WHERE id=?", (node2,))
        row1 = await cursor1.fetchone()
        row2 = await cursor2.fetchone()

        assert abs(row1[0] - 0.6) < 0.01  # Architect→hermes 被 +0.10
        assert abs(row2[0] - 0.5) < 0.01  # Developer→openclaw 不受影响
