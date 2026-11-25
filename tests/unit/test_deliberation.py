"""tests/unit/test_deliberation.py —— 讨论引擎测试"""
import os, sys, asyncio, pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
def deliberation_svc():
    from services.deliberation.deliberation_service import DeliberationService
    return DeliberationService()


class TestComplexity:
    def test_empty_low(self, deliberation_svc):
        assert deliberation_svc.assess_complexity("")["level"] == "low"

    def test_fix_low(self, deliberation_svc):
        r = deliberation_svc.assess_complexity("修复一个空指针")
        assert r["level"] == "low"

    def test_refactor_high(self, deliberation_svc):
        r = deliberation_svc.assess_complexity("重构整个权限系统，设计数据库迁移方案")
        assert r["level"] in ("high", "extreme")

    def test_architecture_high(self, deliberation_svc):
        r = deliberation_svc.assess_complexity("设计微服务架构和分布式事务")
        assert r["level"] in ("high", "extreme")

    def test_negated_filtered(self, deliberation_svc):
        r = deliberation_svc.assess_complexity("我不想用微服务，单体就好")
        assert r["level"] in ("low", "medium")

    def test_score_non_negative(self, deliberation_svc):
        assert deliberation_svc.assess_complexity("修bug")["score"] >= 0


class TestRoles:
    async def test_no_db_returns_empty(self, deliberation_svc):
        assert await deliberation_svc.select_roles("medium") == []


class TestRunDeliberation:
    async def test_no_agent_service_error(self, deliberation_svc):
        r = await deliberation_svc.run_deliberation("x", [], "t1")
        assert "error" in r

    async def test_no_adapter_error(self, deliberation_svc):
        from services.agent.agent_service import AgentService
        deliberation_svc.agent_service = AgentService(None, adapters={})
        r = await deliberation_svc.run_deliberation("x", [], "t2")
        assert "error" in r

    async def test_api_key_check(self, deliberation_svc):
        from services.agent.agent_service import AgentService
        svc = AgentService(None, adapters={"t": _F()})
        async def empty_config(k="", **kw): return {"api_key": "", "model": "x"}
        svc._get_model_config = empty_config
        deliberation_svc.agent_service = svc
        r = await deliberation_svc.run_deliberation("x", [{"id":"r1","name":"T","prompt_template":"p"}], "t3")
        assert r.get("error") and ("API Key" in r["error"] or "api_key" in r["error"].lower())

    async def test_valid_api_key_proceeds(self, deliberation_svc):
        from services.agent.agent_service import AgentService
        svc = AgentService(None, adapters={"t": _F()})
        async def valid_config(k="", **kw): return {"api_key": "sk-ok", "model": "x"}
        svc._get_model_config = valid_config
        deliberation_svc.agent_service = svc
        r = await deliberation_svc.run_deliberation("x", [{"id":"r1","name":"T","prompt_template":"p"}], "t4")
        assert "error" not in r


class _F:
    def __init__(self):
        self._ready = asyncio.Event(); self._ready.set(); self.agent_id = "t"
    async def health(self): return True
    async def start(self): pass
    async def wait_ready(self): pass
    async def stream(self, p, c=None): yield "ok"
    def capabilities(self):
        from adapters.base import AgentCapability
        return AgentCapability()
