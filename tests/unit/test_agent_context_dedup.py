"""tests/unit/test_agent_context_dedup.py —— Batch 2.7: 三条检索路径去重 + 统一截断

覆盖:
  - render_memory_context 按 id 去重（memory / knowledge 各自）
  - 跨源内容去重（同一条信息在 memory 与 knowledge 中只出现一次）
  - total_budget 动态计算并限制返回结果数/单条预览长度
  - 截断处使用 truncate_with_pointer（附 node id 溯源指针）
  - build_agent_context 实际调用统一渲染并传入 project_id
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class FakeMemoryService:
    def __init__(self):
        self.calls: list[dict] = []
        self.session_context: list[dict] = []
        self.search_results: list[dict] = []

    async def get_session_context(self, session_id: str) -> list[dict]:
        return list(self.session_context)

    async def multi_level_search(
        self, query: str, limit: int = 20,
        filters: dict | None = None,
        query_expansion: bool = False,
        diversify: bool = False,
        latency_budget_ms: float | None = None,
    ) -> list[dict]:
        self.calls.append({
            "query": query, "limit": limit, "filters": filters,
            "query_expansion": query_expansion, "diversify": diversify,
            "latency_budget_ms": latency_budget_ms,
        })
        return list(self.search_results)


class FakeMemoryTree:
    def __init__(self, raw_results: list[dict] | None = None):
        self.raw_results = raw_results or []

    async def retrieve_context(self, query: str, limit: int = 10, **kwargs) -> str | list[dict]:
        if kwargs.get("return_raw"):
            return list(self.raw_results)
        return ""


class FakeSettingsService:
    async def get_all(self):
        return {}


@pytest.fixture
async def agent_svc(tmp_path, monkeypatch):
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()

    from services.agent.agent_service import AgentService
    svc = AgentService(
        conn, adapters={},
        memory_service=FakeMemoryService(),
        memory_tree=FakeMemoryTree(),
        settings_service=FakeSettingsService(),
    )

    async def fake_config(k="", **kw):
        return {"api_key": "sk-test", "model": "test", "context_window": 128000}
    svc._get_model_config = fake_config

    # 2026-10-22：指向真实临时目录（原来那个 D:\Projects\MyApp 不存在且不可写，
    # 会让任何在工作区下建目录的代码 PermissionError）。
    fake_ws = str(tmp_path / "MyApp")
    import security.sandbox
    monkeypatch.setattr(security.sandbox, "get_workspace", lambda: fake_ws)

    yield svc
    await conn.close()


class TestRenderMemoryContextDedup:
    async def test_dedups_duplicate_memory_ids(self):
        from prompts.memory_context import render_memory_context

        semantic = [
            {"id": "m1", "content": "用户偏好深色模式", "memory_type": "preference", "score": 0.9},
            {"id": "m1", "content": "重复项", "memory_type": "preference", "score": 0.5},
            {"id": "m2", "content": "项目使用 Python 3.12", "memory_type": "fact", "score": 0.7},
        ]
        ctx = render_memory_context([], semantic, "")
        assert ctx.count("用户偏好深色模式") == 1
        assert "m2" not in ctx or "项目使用 Python 3.12" in ctx

    async def test_dedups_duplicate_knowledge_ids(self):
        from prompts.memory_context import render_memory_context

        knowledge = [
            {"id": "k1", "title": "API 设计", "content": "RESTful 原则", "score": 0.9, "sources": ["fts5"]},
            {"id": "k1", "title": "API 设计", "content": "重复知识", "score": 0.4, "sources": ["fts5"]},
            {"id": "k2", "title": "部署", "content": "Docker", "score": 0.6, "sources": ["vector"]},
        ]
        ctx = render_memory_context([], [], "", knowledge_results=knowledge)
        assert ctx.count("RESTful 原则") + ctx.count("重复知识") == 1
        assert "Docker" in ctx

    async def test_dedups_cross_source_by_content(self):
        from prompts.memory_context import render_memory_context

        semantic = [
            {"id": "m1", "content": "使用 asyncio.to_thread 避免阻塞", "memory_type": "fact", "score": 0.95},
        ]
        knowledge = [
            {"id": "k1", "title": "asyncio", "content": "使用 asyncio.to_thread 避免阻塞", "score": 0.8, "sources": ["vector"]},
        ]
        ctx = render_memory_context([], semantic, "", knowledge_results=knowledge)
        # 同内容只出现一次；因 memory 分数更高，保留 memory 格式
        assert ctx.count("使用 asyncio.to_thread 避免阻塞") == 1
        assert "[fact]" in ctx

    async def test_respects_total_budget_and_limits_results(self):
        from prompts.memory_context import render_memory_context

        semantic = [
            {"id": f"m{i}", "content": "x" * 500, "memory_type": "fact", "score": 0.9 - i * 0.05}
            for i in range(5)
        ]
        knowledge = [
            {"id": f"k{i}", "title": f"t{i}", "content": "y" * 500, "score": 0.5, "sources": ["fts5"]}
            for i in range(5)
        ]
        total_budget = 500
        ctx = render_memory_context(
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
            semantic, "", knowledge_results=knowledge, total_budget=total_budget,
        )
        # 预算受限，返回结果数应被压缩，且单条内容被截断
        result_lines = [ln for ln in ctx.splitlines() if ln.strip().startswith(("- [", "- **"))]
        assert len(result_lines) <= 4
        assert len(ctx) <= total_budget + 200  # 允许标题/标签等固定开销

    async def test_uses_truncate_with_pointer(self):
        from prompts.memory_context import render_memory_context

        with patch("prompts.memory_context.truncate_with_pointer") as mock_truncate:
            mock_truncate.side_effect = lambda text, budget, node_id=None: f"TRUNC:{node_id}:{budget}"
            semantic = [
                {"id": "m1", "content": "重要决策", "memory_type": "decision", "score": 0.9},
            ]
            knowledge = [
                {"id": "k1", "title": "决策", "content": "知识内容", "score": 0.8, "sources": ["vector"]},
            ]
            ctx = render_memory_context([], semantic, "", knowledge_results=knowledge)
            assert "TRUNC:m1:" in ctx
            assert "TRUNC:k1:" in ctx

    async def test_backward_compatible_without_knowledge(self):
        from prompts.memory_context import render_memory_context

        session = [{"role": "user", "content": "hello"}]
        semantic = [{"id": "m1", "content": "重要事实", "memory_type": "fact", "score": 0.9}]
        ctx = render_memory_context(session, semantic, "")
        assert "用户: hello" in ctx
        assert "[fact] 重要事实" in ctx


class TestAgentContextUnifiedRender:
    async def test_build_agent_context_passes_knowledge_raw_and_budget(self, agent_svc):
        svc = agent_svc
        svc._memory_service.search_results = [
            {"id": "m1", "content": "记忆 A", "memory_type": "fact", "score": 0.9},
        ]
        svc._memory_tree.raw_results = [
            {"id": "k1", "title": "知识 A", "content": "知识内容", "score": 0.8, "sources": ["vector"]},
        ]

        from services.agent.agent_context import build_agent_context
        ctx, summary = await build_agent_context(
            svc, task_id="t1", session_id="s1", prompt="query",
            mode="ask", rules_context="", extra_context={}, model_cfg={"context_window": 128000},
        )

        memory = ctx["memory"]
        assert "记忆 A" in memory
        assert "知识 A" in memory
        assert "知识内容" in memory
        assert " · 知识树" in summary

    async def test_agent_context_dedups_memory_and_knowledge(self, agent_svc):
        svc = agent_svc
        svc._memory_service.search_results = [
            {"id": "m1", "content": "相同内容", "memory_type": "fact", "score": 0.95},
        ]
        svc._memory_tree.raw_results = [
            {"id": "k1", "title": "标题", "content": "相同内容", "score": 0.7, "sources": ["fts5"]},
        ]

        from services.agent.agent_context import build_agent_context
        ctx, _summary = await build_agent_context(
            svc, task_id="t1", session_id="s1", prompt="query",
            mode="ask", rules_context="", extra_context={}, model_cfg={"context_window": 128000},
        )
        assert ctx["memory"].count("相同内容") == 1

    async def test_dynamic_budget_respects_small_context_window(self, agent_svc):
        svc = agent_svc
        # 大量不同内容，让总预算差异体现在返回结果数量上
        svc._memory_service.search_results = [
            {"id": f"m{i}", "content": f"记忆内容-{i:02d}-" + "x" * 200,
             "memory_type": "fact", "score": 0.9 - i * 0.01}
            for i in range(30)
        ]
        svc._memory_tree.raw_results = [
            {"id": f"k{i}", "title": f"知识-{i:02d}",
             "content": f"知识内容-{i:02d}-" + "y" * 200,
             "score": 0.8 - i * 0.01, "sources": ["vector"]}
            for i in range(30)
        ]

        from services.agent.agent_context import build_agent_context
        ctx_small, _ = await build_agent_context(
            svc, task_id="t1", session_id="s1", prompt="q",
            mode="ask", rules_context="", extra_context={}, model_cfg={"context": 4000},
        )
        ctx_large, _ = await build_agent_context(
            svc, task_id="t1", session_id="s1", prompt="q",
            mode="ask", rules_context="", extra_context={}, model_cfg={"context": 128000},
        )
        assert len(ctx_small["memory"]) < len(ctx_large["memory"])
