"""tests/unit/test_self_evolution_persistence.py —— Batch 2.6: Self-Evolution _patterns 持久化

验证 SelfEvolutionEngine 在重启后能从 evolution_patterns 表恢复计数，
确保自进化引擎的核心闭环（计数→阈值→自动生成 workflow）在进程重启后不会停摆。
"""
import os
import sys
import json
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def db():
    """每个测试独立临时文件数据库，并执行完整 SCHEMA_SQL。"""
    import aiosqlite
    import tempfile
    from repositories.schema import SCHEMA_SQL, run_migrations
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = await aiosqlite.connect(path)
    await conn.executescript(SCHEMA_SQL)
    await run_migrations(conn)
    await conn.commit()
    yield conn
    await conn.close()
    os.unlink(path)


@pytest.fixture
def make_engine(db):
    """工厂：创建带 db 的 SelfEvolutionEngine，不依赖外部模型/adapter。"""
    def _make():
        from services.evolution.self_evolution import SelfEvolutionEngine
        return SelfEvolutionEngine(db=db)
    return _make


@pytest.mark.unit
class TestPatternPersistence:
    """Batch 2.6: 模式计数持久化到 evolution_patterns 表。"""

    async def test_engine_loads_patterns_from_db_on_demand(self, db, make_engine):
        """DB 中已有 pattern 行 → 首次 _load_patterns() 时加载到内存。"""
        from repositories.evolution_repo import upsert_pattern
        await upsert_pattern(
            db, "task_type:code_review", count=3,
            examples=["review A", "review B"], last_seen=time.time(),
            workflow_generated=0, skill_generated=0, skill_exported=0,
        )

        engine = make_engine()
        await engine._load_patterns()
        assert "task_type:code_review" in engine._patterns
        assert engine._patterns["task_type:code_review"]["count"] == 3
        assert engine._patterns["task_type:code_review"]["examples"] == ["review A", "review B"]

    async def test_observe_conversation_persists_pattern_count(self, db, make_engine):
        """观察对话后，pattern 计数应写入 evolution_patterns 表。"""
        from repositories.evolution_repo import load_patterns

        engine = make_engine()
        result = await engine.observe_conversation(
            session_id="s1",
            task_prompt="请帮我 review 这段代码",
            agent_output="ok",
            tool_calls=[{"function": {"name": "read_file"}}, {"function": {"name": "write_file"}}],
        )
        assert result["new_patterns"] > 0

        patterns = await load_patterns(db)
        sig = "task_type:code_review"
        assert sig in patterns
        assert patterns[sig]["count"] >= 1
        assert patterns[sig]["last_seen"] > 0

    async def test_workflow_generated_flag_persisted(self, db, make_engine):
        """达到阈值生成 workflow 后，workflow_generated 标志应持久化。"""
        from repositories.evolution_repo import upsert_pattern
        await upsert_pattern(
            db, "task_type:code_review", count=3,
            examples=["e1", "e2", "e3"], last_seen=time.time(),
            workflow_generated=0, skill_generated=0, skill_exported=0,
        )

        engine = make_engine()
        await engine._load_patterns()
        # 模拟还差 1 次达到阈值 AUTO_WORKFLOW_THRESHOLD=4
        engine._patterns["task_type:code_review"]["count"] = 3

        # 构造一个返回固定 workflow JSON 的 adapter/model_config
        from config import DEFAULT_AGENT_ID

        class FakeAdapter:
            async def invoke(self, prompt, context=None):
                return {
                    "content": '{"nodes":[{"id":"n1","type":"human"}],"edges":[]}',
                }

        class FakeModelConfig:
            async def get_active_config(self):
                return {"api_key": "fake"}

        engine._adapters = {DEFAULT_AGENT_ID: FakeAdapter()}
        engine._model_config = FakeModelConfig()

        result = await engine.observe_conversation(
            session_id="s1",
            task_prompt="请帮我 review 这段代码",
            agent_output="ok",
            tool_calls=[{"function": {"name": "read_file"}}, {"function": {"name": "write_file"}}],
        )
        assert result["promoted_workflows"]

        from repositories.evolution_repo import load_patterns
        patterns = await load_patterns(db)
        assert patterns["task_type:code_review"]["_workflow_generated"] is True

    async def test_restart_loads_previous_counts(self, db, make_engine):
        """模拟重启：旧引擎写入 → 新引擎读取，计数不丢。"""
        old_engine = make_engine()
        for i in range(3):
            await old_engine.observe_conversation(
                session_id=f"s{i}",
                task_prompt="fix bug",
                agent_output="ok",
                tool_calls=[{"function": {"name": "read_file"}}, {"function": {"name": "edit_file"}}],
            )

        new_engine = make_engine()
        await new_engine._load_patterns()
        sig = "task_type:bug_fix"
        assert sig in new_engine._patterns
        assert new_engine._patterns[sig]["count"] == 3

    async def test_periodic_cleanup_deletes_stale_patterns_from_db(self, db, make_engine):
        """清理 stale patterns 时，DB 中的对应行也应被删除。"""
        from repositories.evolution_repo import upsert_pattern, load_patterns
        now = time.time()
        await upsert_pattern(
            db, "task_type:old", count=5, examples=["old"],
            last_seen=now - 31 * 86400,  # 31 天前
            workflow_generated=0, skill_generated=0, skill_exported=0,
        )
        await upsert_pattern(
            db, "task_type:recent", count=5, examples=["recent"],
            last_seen=now,
            workflow_generated=0, skill_generated=0, skill_exported=0,
        )

        engine = make_engine()
        result = await engine.periodic_pattern_cleanup()
        assert result["pruned_patterns"] == 1

        patterns = await load_patterns(db)
        assert "task_type:old" not in patterns
        assert "task_type:recent" in patterns

    async def test_examples_truncated_to_ten(self, db, make_engine):
        """examples 列表超过 10 个时应截断保留最近 10 个。"""
        engine = make_engine()
        for i in range(12):
            await engine.observe_conversation(
                session_id=f"s{i}",
                task_prompt="doc",
                agent_output="ok",
                tool_calls=[],
            )

        from repositories.evolution_repo import load_patterns
        patterns = await load_patterns(db)
        assert len(patterns["task_type:documentation"]["examples"]) == 10
