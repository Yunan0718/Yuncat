"""tests/unit/test_agent_tool_loop_max_rounds.py —— Batch 3.1: 最大轮数可配

覆盖:
  - build_agent_context 读取 settings 注入 context["max_rounds"]
  - 缺失/非法设置时不注入（使用默认路径）
  - 超范围值被 clamp
  - calc_max_tool_rounds 的硬上限生效
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class FakeMemoryService:
    async def get_session_context(self, session_id: str) -> list[dict]:
        return []

    async def multi_level_search(self, *args, **kwargs) -> list[dict]:
        return []


class FakeMemoryTree:
    async def retrieve_context(self, *args, **kwargs) -> str | list[dict]:
        if kwargs.get("return_raw"):
            return []
        return ""


class FakeSettingsService:
    def __init__(self, values: dict | None = None):
        self.values = values or {}

    async def get_raw(self, key: str) -> str | None:
        # 真实 SettingsService.get_raw 是 async（2026-09-10 修复了调用方漏 await
        # 导致 max_tool_rounds 设置永不生效的 bug）——夹具必须同形，否则永远测不到注入。
        return self.values.get(key)

    async def get_all(self) -> dict:
        return dict(self.values)


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
        memory_service=FakeMemoryService(), memory_tree=FakeMemoryTree(),
        settings_service=FakeSettingsService(),
    )

    async def fake_config(k="", **kw):
        return {"api_key": "sk-test", "model": "test", "context_window": 128000}
    svc._get_model_config = fake_config

    import security.sandbox
    # 2026-10-22：指向真实临时目录（原来那个 D:\Projects\MyApp 不存在且不可写）。
    monkeypatch.setattr(security.sandbox, "get_workspace",
                        lambda: str(tmp_path / "MyApp"))

    yield svc
    await conn.close()


class TestResolveMaxToolRounds:
    async def test_no_setting_does_not_inject_max_rounds(self, agent_svc):
        from services.agent.agent_context import build_agent_context
        ctx, _summary = await build_agent_context(
            agent_svc, task_id="t1", session_id="s1", prompt="hello",
            mode="agent", rules_context="", extra_context={},
            model_cfg={"context_window": 128000},
        )
        assert "max_rounds" not in ctx

    async def test_setting_injected_into_context(self, agent_svc):
        agent_svc._settings_service = FakeSettingsService({"max_tool_rounds": "200"})
        from services.agent.agent_context import build_agent_context
        ctx, _summary = await build_agent_context(
            agent_svc, task_id="t1", session_id="s1", prompt="hello",
            mode="agent", rules_context="", extra_context={},
            model_cfg={"context_window": 128000},
        )
        assert ctx.get("max_rounds") == 200

    async def test_setting_clamped_to_max(self, agent_svc):
        from config import USER_TOOL_ROUNDS_MAX
        agent_svc._settings_service = FakeSettingsService({"max_tool_rounds": "5000"})
        from services.agent.agent_context import build_agent_context
        ctx, _summary = await build_agent_context(
            agent_svc, task_id="t1", session_id="s1", prompt="hello",
            mode="agent", rules_context="", extra_context={},
            model_cfg={"context_window": 128000},
        )
        assert ctx.get("max_rounds") == USER_TOOL_ROUNDS_MAX

    async def test_setting_clamped_to_min(self, agent_svc):
        from config import USER_TOOL_ROUNDS_MIN
        agent_svc._settings_service = FakeSettingsService({"max_tool_rounds": "3"})
        from services.agent.agent_context import build_agent_context
        ctx, _summary = await build_agent_context(
            agent_svc, task_id="t1", session_id="s1", prompt="hello",
            mode="agent", rules_context="", extra_context={},
            model_cfg={"context_window": 128000},
        )
        assert ctx.get("max_rounds") == USER_TOOL_ROUNDS_MIN

    async def test_invalid_setting_falls_back_to_default(self, agent_svc):
        agent_svc._settings_service = FakeSettingsService({"max_tool_rounds": "not-a-number"})
        from services.agent.agent_context import build_agent_context
        ctx, _summary = await build_agent_context(
            agent_svc, task_id="t1", session_id="s1", prompt="hello",
            mode="agent", rules_context="", extra_context={},
            model_cfg={"context_window": 128000},
        )
        assert "max_rounds" not in ctx


class TestCalcMaxToolRounds:
    def test_hard_limit_overrides_dynamic_for_large_model(self):
        from config import calc_max_tool_rounds
        # 1M 上下文模型动态派生约为 500 轮，硬上限应生效
        model_cfg = {"context_window": 1_000_000}
        assert calc_max_tool_rounds(model_cfg, hard_limit=200) == 200
