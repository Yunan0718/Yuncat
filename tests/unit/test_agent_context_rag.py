"""tests/unit/test_agent_context_rag.py —— Batch 1.1: agent_context RAG 主路径接入 HybridSearcher + Reranker

覆盖:
  - build_agent_context 调用 multi_level_search 而非原始 FTS5/vector_search
  - project_id 使用 os.path.normcase() 规范化
  - source="memory" 过滤注入
  - 异常时 graceful fallback（不抛错）
"""
import os
import sys
import asyncio
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class FakeMemoryService:
    """模拟 MemoryService——记录 multi_level_search 调用参数。"""

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
            "query": query,
            "limit": limit,
            "filters": filters,
            "query_expansion": query_expansion,
            "diversify": diversify,
            "latency_budget_ms": latency_budget_ms,
        })
        return list(self.search_results)

    async def vector_search(self, query: str, limit: int = 10) -> list[dict]:
        pytest.fail("agent_context 不应再直接调用 vector_search")
        return []


class FakeMemoryTree:
    def __init__(self, result: str = "", raw_results: list[dict] | None = None):
        self.result = result
        self.raw_results = raw_results or []

    async def retrieve_context(self, query: str, limit: int = 10, **kwargs) -> str | list[dict]:
        if kwargs.get("return_raw"):
            return list(self.raw_results)
        return self.result


class FakeSettingsService:
    async def get_all(self):
        return {}


@pytest.fixture
async def agent_svc(tmp_path, monkeypatch):
    """创建 AgentService + 内存 DB + fake memory service/tree。"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()

    from services.agent.agent_service import AgentService
    adapters = {}
    memory = FakeMemoryService()
    tree = FakeMemoryTree()
    svc = AgentService(
        conn, adapters=adapters,
        memory_service=memory, memory_tree=tree,
        settings_service=FakeSettingsService(),
    )
    # 注入最小 model config
    async def fake_config(k="", **kw):
        return {"api_key": "sk-test", "model": "test", "context_window": 128000}
    svc._get_model_config = fake_config

    # 固定 workspace 路径，便于验证 normcase。
    # 2026-10-22：原来指向 "D:\Projects\MyApp"——那个路径不存在且不可写，
    # 任何要在工作区下建目录的代码（快照目录等）都会 PermissionError，
    # 把不相关的用例带崩。改用真实临时目录，断言意图不变。
    fake_ws = str(tmp_path / "MyApp")
    import security.sandbox
    monkeypatch.setattr(security.sandbox, "get_workspace", lambda: fake_ws)

    yield svc, memory, tree
    await conn.close()


class TestAgentContextRAG:
    async def test_uses_multi_level_search_with_project_id_and_source(self, agent_svc, tmp_path):
        svc, memory, _tree = agent_svc
        memory.search_results = [
            {"id": "m1", "content": "用户偏好深色模式", "memory_type": "preference", "score": 0.95},
        ]

        from services.agent.agent_context import build_agent_context
        ctx, summary = await build_agent_context(
            svc, task_id="t1", session_id="s1", prompt="深色模式",
            mode="ask", rules_context="", extra_context={}, model_cfg={"context_window": 128000},
        )

        assert len(memory.calls) == 1
        call = memory.calls[0]
        assert call["query"] == "深色模式"
        assert call["filters"]["source"] == "memory"
        # 路径应被 normcase 处理（与 fixture 里的桩一致）
        assert call["filters"]["project_id"] == os.path.normcase(str(tmp_path / "MyApp"))
        # 渲染结果应包含记忆内容
        assert "深色模式" in ctx["memory"]

    async def test_multi_level_search_failure_falls_back_gracefully(self, agent_svc):
        svc, memory, _tree = agent_svc

        async def boom(*args, **kwargs):
            raise RuntimeError("search broken")

        memory.multi_level_search = boom

        from services.agent.agent_context import build_agent_context
        ctx, summary = await build_agent_context(
            svc, task_id="t1", session_id="s1", prompt="hello",
            mode="ask", rules_context="", extra_context={}, model_cfg={"context_window": 128000},
        )
        # 不应抛错，且 memory 上下文仍应存在（可能为空）
        assert "memory" in ctx

    async def test_no_longer_calls_vector_search(self, agent_svc):
        svc, memory, _tree = agent_svc
        memory.search_results = [{"id": "m1", "content": "x", "memory_type": "fact", "score": 0.5}]

        from services.agent.agent_context import build_agent_context
        await build_agent_context(
            svc, task_id="t1", session_id="s1", prompt="hello",
            mode="ask", rules_context="", extra_context={}, model_cfg={"context_window": 128000},
        )
        # vector_search 不应被调用（FakeMemoryService 会 fail）
