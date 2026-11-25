"""tests/unit/test_deliberation_g14.py —— Phase 43 G1.4: 跨后端 reviewer 路由 + 收敛失败升级到第二后端

覆盖:
- _resolve_spec_role_routing: reviewer 跨后端路由（openclaw 可用时切换 agent）
- _classify_oc_verdict: OC 独立审计裁决分类 accept/revise/inconclusive
- _resolve_spec_provider_config: 跨后端时用 openclaw 自己的 provider 配置
- _escalate_spec_failure: OC 裁决结构化纳入升级记录（second_backend_consulted/oc_recommendation）
"""
import os
import sys
import tempfile

import aiosqlite
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from repositories.schema import SCHEMA_SQL


# ============================================================
# 1. _resolve_spec_role_routing — 跨后端 reviewer 路由
# ============================================================
@pytest.mark.unit
class TestSpecRoleRoutingCrossBackend:
    """G1.4: reviewer 在多后端可用时路由到 openclaw，否则保持 hermes。"""

    def test_reviewer_routes_to_openclaw_when_available(self):
        from services.deliberation.deliberation_spec import _resolve_spec_role_routing
        r = _resolve_spec_role_routing(
            "reviewer", 1, ["deepseek", "anthropic"],
            available_agents=["hermes", "openclaw"],
        )
        assert r["cross_backend_review"] is True
        assert r["agent_id"] == "openclaw"
        assert r["reviewer_agent_id"] == "openclaw"
        # provider 轮换仍生效（N=2 → 第二个 provider）
        assert r["provider_id"] == "anthropic"

    def test_reviewer_stays_hermes_without_openclaw(self):
        """openclaw 不可用时 reviewer 保持 hermes（向后兼容）。"""
        from services.deliberation.deliberation_spec import _resolve_spec_role_routing
        r = _resolve_spec_role_routing(
            "reviewer", 1, ["deepseek", "anthropic"],
            available_agents=["hermes"],
        )
        assert r["cross_backend_review"] is False
        assert r["agent_id"] == "hermes"
        assert r["reviewer_agent_id"] is None
        assert r["provider_id"] == "anthropic"

    def test_reviewer_stays_hermes_when_available_agents_none(self):
        """available_agents=None（旧调用方）→ 保持原行为。"""
        from services.deliberation.deliberation_spec import _resolve_spec_role_routing
        r = _resolve_spec_role_routing("reviewer", 1, ["deepseek", "anthropic"])
        assert r["cross_backend_review"] is False
        assert r["agent_id"] == "hermes"
        assert r["reviewer_agent_id"] is None

    def test_architect_never_cross_backend(self):
        """architect/developer 不参与跨后端——始终 hermes。"""
        from services.deliberation.deliberation_spec import _resolve_spec_role_routing
        for role in ("architect", "developer"):
            r = _resolve_spec_role_routing(
                role, 1, ["deepseek"], available_agents=["hermes", "openclaw"],
            )
            assert r["cross_backend_review"] is False
            assert r["agent_id"] == "hermes"
            assert r["reviewer_agent_id"] is None

    def test_reviewer_provider_rotation_preserved_with_cross_backend(self):
        """N≥4 时 provider 轮换与跨后端正交——两者同时生效。"""
        from services.deliberation.deliberation_spec import _resolve_spec_role_routing
        providers = ["p0", "p1", "p2", "p3", "p4"]
        r1 = _resolve_spec_role_routing(
            "reviewer", 1, providers, available_agents=["hermes", "openclaw"],
        )
        r2 = _resolve_spec_role_routing(
            "reviewer", 2, providers, available_agents=["hermes", "openclaw"],
        )
        # 跨后端始终启用
        assert r1["cross_backend_review"] is True
        assert r2["cross_backend_review"] is True
        # provider 轮换：round1→p1, round2→p2
        assert r1["provider_id"] == "p1"
        assert r2["provider_id"] == "p2"

    def test_vision_analyst_already_openclaw(self):
        """vision_analyst 原生就是 openclaw——不触发跨后端标记。"""
        from services.deliberation.deliberation_spec import _resolve_spec_role_routing
        r = _resolve_spec_role_routing(
            "vision_analyst", 1, ["deepseek"], available_agents=["hermes", "openclaw"],
        )
        assert r["agent_id"] == "openclaw"  # 原生路由
        assert r["cross_backend_review"] is False  # 不是 reviewer 跨后端


# ============================================================
# 2. _classify_oc_verdict — OC 裁决分类
# ============================================================
@pytest.mark.unit
class TestClassifyOCVerdict:
    """G1.4: OC 独立审计文本启发式分类。"""

    def test_accept_signals(self):
        from services.deliberation.deliberation_spec_roles import _classify_oc_verdict
        assert _classify_oc_verdict("实现正确，可以接受") == "accept"
        assert _classify_oc_verdict("Looks good, no issue found") == "accept"

    def test_revise_signals(self):
        from services.deliberation.deliberation_spec_roles import _classify_oc_verdict
        assert _classify_oc_verdict("发现缺陷，需修复") == "revise"
        assert _classify_oc_verdict("There is a MISMATCH in the implementation") == "revise"

    def test_revise_takes_priority_over_accept(self):
        """同时含正负信号时 → revise（保守，倾向让人工审查）。"""
        from services.deliberation.deliberation_spec_roles import _classify_oc_verdict
        assert _classify_oc_verdict("实现可以接受但存在 bug") == "revise"

    def test_inconclusive(self):
        from services.deliberation.deliberation_spec_roles import _classify_oc_verdict
        assert _classify_oc_verdict("无明显问题但也说不上") == "inconclusive"
        assert _classify_oc_verdict("") == "inconclusive"


# ============================================================
# 3. _resolve_spec_provider_config — 跨后端 provider 配置
# ============================================================
class _FakeModelConfig:
    """模拟 ModelConfigService——支持 per-agent provider + provider config。"""

    def __init__(self, providers: dict[str, dict], agent_providers: dict[str, str] | None = None):
        self._providers = providers
        self._agent_providers = agent_providers or {}

    def get_agent_provider(self, agent_id: str) -> str | None:
        return self._agent_providers.get(agent_id)

    def get_active_provider(self) -> str:
        return next(iter(self._providers)) if self._providers else "deepseek"

    async def get_provider_config(self, provider_id: str) -> dict | None:
        return self._providers.get(provider_id)


class _FakeAgentServiceForRouting:
    def __init__(self, model_config, adapters_keys):
        self._model_config = model_config
        self.adapters = {k: object() for k in adapters_keys}

    async def _get_model_config(self, model_key: str) -> dict:
        return {"api_key": "fallback-key", "model": "fallback-model"}


@pytest.mark.unit
class TestResolveSpecProviderConfigCrossBackend:
    """G1.4: reviewer 跨后端时用 openclaw 自己的 provider 配置。"""

    @pytest.mark.asyncio
    async def test_reviewer_uses_openclaw_provider_when_cross_backend(self):
        from services.deliberation.deliberation_spec import _resolve_spec_provider_config

        providers = {
            "deepseek": {"api_key": "ds-key", "model": "ds-model"},
            "anthropic": {"api_key": "ac-key", "model": "ac-model"},
        }
        mc = _FakeModelConfig(providers, agent_providers={"openclaw": "anthropic"})
        svc = _FakeAgentServiceForRouting(mc, ["hermes", "openclaw"])

        class Self:
            pass
        s = Self()
        s.agent_service = svc

        cfg = await _resolve_spec_provider_config(
            s, "reviewer", 1, ["deepseek", "anthropic"],
            available_agents=["hermes", "openclaw"],
        )
        assert cfg is not None
        assert cfg.get("_reviewer_agent_id") == "openclaw"
        assert cfg.get("api_key") == "ac-key"  # openclaw 的 provider
        assert cfg.get("_spec_role") == "reviewer"

    @pytest.mark.asyncio
    async def test_reviewer_falls_back_when_openclaw_provider_unconfigured(self):
        """openclaw 的 provider 无 api_key → 回退到 hermes provider 路径。"""
        from services.deliberation.deliberation_spec import _resolve_spec_provider_config

        # N=1 provider：cross-backend 路径失败后，hermes 路径用唯一 provider (deepseek)
        providers = {
            "deepseek": {"api_key": "ds-key", "model": "ds-model"},
            "anthropic": {"api_key": "", "model": "ac-model"},  # openclaw 的 provider 无 key
        }
        mc = _FakeModelConfig(providers, agent_providers={"openclaw": "anthropic"})
        svc = _FakeAgentServiceForRouting(mc, ["hermes", "openclaw"])

        class Self:
            pass
        s = Self()
        s.agent_service = svc

        cfg = await _resolve_spec_provider_config(
            s, "reviewer", 1, ["deepseek"],
            available_agents=["hermes", "openclaw"],
        )
        # 回退到 hermes 路径——无 _reviewer_agent_id 标记
        assert cfg is not None
        assert cfg.get("_reviewer_agent_id") is None
        assert cfg.get("api_key") == "ds-key"

    @pytest.mark.asyncio
    async def test_architect_ignores_cross_backend(self):
        """architect 不触发跨后端——正常 provider 解析。"""
        from services.deliberation.deliberation_spec import _resolve_spec_provider_config

        providers = {
            "deepseek": {"api_key": "ds-key", "model": "ds-model"},
        }
        mc = _FakeModelConfig(providers)
        svc = _FakeAgentServiceForRouting(mc, ["hermes", "openclaw"])

        class Self:
            pass
        s = Self()
        s.agent_service = svc

        cfg = await _resolve_spec_provider_config(
            s, "architect", 1, ["deepseek"],
            available_agents=["hermes", "openclaw"],
        )
        assert cfg is not None
        assert cfg.get("_reviewer_agent_id") is None
        assert cfg.get("api_key") == "ds-key"


# ============================================================
# 4. _escalate_spec_failure — OC 裁决结构化纳入升级记录
# ============================================================
@pytest.fixture
async def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = await aiosqlite.connect(path)
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()
    os.unlink(path)


class _FakeOCAdapter:
    """模拟 openclaw adapter——invoke 返回固定审计文本。"""

    def __init__(self, audit_text: str):
        self._audit_text = audit_text
        self._ready = _ReadyFlag()

    async def invoke(self, prompt, context=None):
        return {"content": self._audit_text}


class _ReadyFlag:
    def is_set(self):
        return True


class _FakeAgentServiceForEscalation:
    def __init__(self, db, oc_adapter):
        self.db = db
        self.adapters = {"openclaw": oc_adapter} if oc_adapter else {}


@pytest.mark.unit
class TestEscalateSpecFailureCrossBackend:
    """G1.4: 收敛失败 → 先 OC 独立审计 → 结构化裁决纳入升级记录。"""

    @pytest.mark.asyncio
    async def test_oc_verdict_revise_recorded_in_escalation(self, db):
        """OC 审计发现缺陷 → escalation_data 含 oc_recommendation=revise。"""
        import json as _json
        from services.deliberation.deliberation_spec_roles import _escalate_spec_failure

        oc = _FakeOCAdapter("发现逻辑缺陷，需修复。MISMATCH in module X")
        svc = _FakeAgentServiceForEscalation(db, oc)

        class Self:
            pass
        s = Self()
        s.agent_service = svc
        pushed = []

        async def _push(cb, tid, msg_type, payload):
            pushed.append((msg_type, payload))

        s._push = _push

        await _escalate_spec_failure(
            s, task_id="task-escalate-1", spec_yaml="spec:\n  name: TestSpec",
            audit_report={"mismatches": 2, "missing": 1, "mismatches_list": ["m1"],
                          "missing_list": ["g1"], "verification_passed": False},
            rounds=5, push_callback=None, auto_fix_attempts=3,
        )

        # 验证 knowledge_nodes 写入
        cursor = await db.execute(
            "SELECT content FROM knowledge_nodes WHERE source='spec_loop_escalation'"
        )
        row = await cursor.fetchone()
        assert row is not None
        data = _json.loads(row[0])
        assert data["second_backend_consulted"] is True
        assert data["oc_recommendation"] == "revise"
        assert "发现逻辑缺陷" in data["oc_verdict"]
        assert "重点检查 Spec 逻辑缺陷" in data["suggested_action"]

        # 验证 WS 推送含 oc_audit + spec_escalated
        msg_types = [m for m, _ in pushed]
        assert "spec_phase" in msg_types
        assert "spec_escalated" in msg_types

    @pytest.mark.asyncio
    async def test_oc_verdict_accept_recorded(self, db):
        """OC 审计认可 → escalation_data 含 oc_recommendation=accept。"""
        import json as _json
        from services.deliberation.deliberation_spec_roles import _escalate_spec_failure

        oc = _FakeOCAdapter("实现正确，可以接受。No issue found.")
        svc = _FakeAgentServiceForEscalation(db, oc)

        class Self:
            pass
        s = Self()
        s.agent_service = svc
        s._push = _push_noop = lambda *a, **k: None

        async def _push(cb, tid, msg_type, payload):
            pass

        s._push = _push

        await _escalate_spec_failure(
            s, task_id="task-escalate-2", spec_yaml=None,
            audit_report={"mismatches": 1, "missing": 0, "verification_passed": False},
            rounds=4, push_callback=None,
        )

        cursor = await db.execute(
            "SELECT content FROM knowledge_nodes WHERE source='spec_loop_escalation'"
        )
        row = await cursor.fetchone()
        data = _json.loads(row[0])
        assert data["second_backend_consulted"] is True
        assert data["oc_recommendation"] == "accept"
        assert "优先考虑接受当前实现" in data["suggested_action"]

    @pytest.mark.asyncio
    async def test_no_oc_adapter_skips_second_backend(self, db):
        """无 openclaw adapter → second_backend_consulted=False，仍写升级记录。"""
        import json as _json
        from services.deliberation.deliberation_spec_roles import _escalate_spec_failure

        svc = _FakeAgentServiceForEscalation(db, None)  # 无 oc

        class Self:
            pass
        s = Self()
        s.agent_service = svc

        async def _push(cb, tid, msg_type, payload):
            pass

        s._push = _push

        await _escalate_spec_failure(
            s, task_id="task-escalate-3", spec_yaml=None,
            audit_report={"mismatches": 1, "missing": 0, "verification_passed": False},
            rounds=4, push_callback=None,
        )

        cursor = await db.execute(
            "SELECT content FROM knowledge_nodes WHERE source='spec_loop_escalation'"
        )
        row = await cursor.fetchone()
        data = _json.loads(row[0])
        assert data["second_backend_consulted"] is False
        assert data["oc_recommendation"] == "not_consulted"
        assert data["oc_verdict"] == ""

    @pytest.mark.asyncio
    async def test_rounds_below_threshold_skips_oc(self, db):
        """rounds < 3 时不咨询 OC（与原 Phase 43 M.5 阈值一致）。"""
        import json as _json
        from services.deliberation.deliberation_spec_roles import _escalate_spec_failure

        oc = _FakeOCAdapter("should not be called")
        svc = _FakeAgentServiceForEscalation(db, oc)

        class Self:
            pass
        s = Self()
        s.agent_service = svc

        async def _push(cb, tid, msg_type, payload):
            pass

        s._push = _push

        await _escalate_spec_failure(
            s, task_id="task-escalate-4", spec_yaml=None,
            audit_report={"mismatches": 1, "missing": 0, "verification_passed": False},
            rounds=2, push_callback=None,  # < 3
        )

        cursor = await db.execute(
            "SELECT content FROM knowledge_nodes WHERE source='spec_loop_escalation'"
        )
        row = await cursor.fetchone()
        data = _json.loads(row[0])
        assert data["second_backend_consulted"] is False
        assert data["oc_recommendation"] == "not_consulted"
