"""tests/unit/test_window_rollover.py — 长任务窗口翻滚引擎单测

覆盖 (均为 DI / best-effort, 不依赖真实 DB 与 LLM):
- should_rollover: 混合触发边界 (进度带 / AI 自判 / token 兜底 / 代数上限)
- run_supervision_cycle: 三分支 (proceed / constrain / escalate)
- rollover_window: 开新 Worker 会话 + 目标摘要种子 + 记录翻滚状态
- supervise_round: 翻滚触发 + 约束回写 + 双向交叉验证 + 升级上报主 Agent

引擎设计为可注入: DB 访问 (get_conversation/insert_conversation) 与 LLM
(_get_llm_call) 均在测试中 monkeypatch, 与 services.long_task.window_rollover 的
真实模块函数解耦。
"""
import pytest

import services.long_task.window_rollover as wr


# ── 共用 Fake ──

class FakeWS:
    def __init__(self):
        self.sent = []
    async def safe_send(self, tid, payload):
        self.sent.append(payload)


class FakeConn:
    """最小可提交连接——rollover_window 末尾会 await conn.commit()。
    apply_abort 还会做 UPDATE tasks, 故补 execute + 伪 cursor。"""
    async def commit(self):
        pass
    async def execute(self, *a, **k):
        return FakeCursor()


class FakeCursor:
    def __init__(self):
        self.rowcount = 0


class FakeOrch:
    """内存态 orch: 仅实现 rollover 用到的 get_state/set_state/inject_constraint。"""
    def __init__(self):
        self._state = {}
        self._conn = FakeConn()
        self._ws_manager = FakeWS()
        self.injected = []
    async def get_state(self, tid):
        return self._state.get(tid, {})
    async def set_state(self, tid, **fields):
        self._state.setdefault(tid, {}).update(fields)
    async def inject_constraint(self, tid, constraint):
        self.injected.append(constraint)


@pytest.fixture
def patch_db(monkeypatch):
    """patch 掉真实 DB 访问与 LLM, 返回可控 fake。"""
    calls = {"gc": [], "ins": []}
    async def fake_get_conversation(conn, sid, limit=12):
        calls["gc"].append(sid)
        return [{"role": "user", "content": "近期上下文片段", "agent_id": "yuncat"}]
    async def fake_insert(conn, sid, role, content, **kw):
        calls["ins"].append((sid, role, content))
        return "cid"
    monkeypatch.setattr(wr, "get_conversation", fake_get_conversation)
    monkeypatch.setattr(wr, "insert_conversation", fake_insert)
    return calls


@pytest.fixture
def fake_llm(monkeypatch):
    """可控 LLM: 通过 fixture 闭包切换返回。默认通过。"""
    state = {"mode": "pass"}
    async def _llm(prompt):
        if state["mode"] == "pass":
            return '{"pass":true,"score":95,"issues":[],"corrective_constraint":""}'
        if state["mode"] == "constrain":
            if "评审" in prompt:
                return '{"pass":false,"score":55,"issues":["偏离"],"corrective_constraint":"回到目标A"}'
            return '{"aligned":true,"divergences":[],"verdict":"ok"}'
        if state["mode"] == "supervisor_drift":
            if "评审" in prompt:
                return '{"pass":true,"score":80,"issues":[],"corrective_constraint":""}'
            return '{"aligned":false,"divergences":["监督者复述偏了"],"verdict":"supervisor_drift"}'
        return '{"pass":true,"score":90,"issues":[],"corrective_constraint":""}'
    async def _get_llm_call():
        return _llm
    monkeypatch.setattr(wr, "_get_llm_call", _get_llm_call)
    return state


# ═══════════════════════════════════════════════════════════
# should_rollover (纯函数)
# ═══════════════════════════════════════════════════════════

def test_should_rollover_progress_band():
    ok, reason, sig = wr.should_rollover(progress_fraction=0.62)
    assert ok and sig == "progress"

def test_should_rollover_below_band():
    # 0.90 超出翻滚带 (0.55~0.72) 且无其他信号
    ok, _, sig = wr.should_rollover(progress_fraction=0.90)
    assert not ok and sig == "none"

def test_should_rollover_ai_signal():
    ok, _, sig = wr.should_rollover(progress_fraction=0.40, ai_signal=True)
    assert ok and sig == "ai"

def test_should_rollover_ai_signal_floor():
    # AI 信号但进度低于地板 (0.35) 不应触发
    ok, _, sig = wr.should_rollover(progress_fraction=0.20, ai_signal=True)
    assert not ok and sig == "none"

def test_should_rollover_token_fallback():
    ok, _, sig = wr.should_rollover(progress_fraction=0.50, token_occupancy=0.90)
    assert ok and sig == "token"

def test_should_rollover_generation_cap():
    ok, _, sig = wr.should_rollover(progress_fraction=0.62, already_rolled_in_window=6)
    assert not ok and sig == "cap"


# ═══════════════════════════════════════════════════════════
# run_supervision_cycle (三分支)
# ═══════════════════════════════════════════════════════════

async def test_supervision_pass(fake_llm):
    orch = FakeOrch()
    fake_llm["mode"] = "pass"
    rep = await wr.run_supervision_cycle(
        await wr._get_llm_call(), orch, "T",
        worker_artifact="z", original_intent="A",
    )
    assert rep["action"] == "proceed"
    assert rep["constraint"] == ""
    assert orch.injected == []

async def test_supervision_constrain(fake_llm):
    orch = FakeOrch()
    fake_llm["mode"] = "constrain"
    rep = await wr.run_supervision_cycle(
        await wr._get_llm_call(), orch, "T",
        worker_artifact="z", original_intent="A",
        supervisor_summary="A", worker_summary="B",
    )
    assert rep["action"] == "constrain"
    assert orch.injected == ["回到目标A"]  # 约束已回写

async def test_supervision_escalate(fake_llm):
    orch = FakeOrch()
    fake_llm["mode"] = "supervisor_drift"
    rep = await wr.run_supervision_cycle(
        await wr._get_llm_call(), orch, "T",
        worker_artifact="z", original_intent="A",
        supervisor_summary="偏了", worker_summary="B",
    )
    assert rep["action"] == "escalate"
    assert rep["crossval"]["verdict"] == "supervisor_drift"


# ═══════════════════════════════════════════════════════════
# rollover_window (开新 Worker + 记录状态)
# ═══════════════════════════════════════════════════════════

async def test_rollover_window_seeds_worker(patch_db):
    orch = FakeOrch()
    orch._state["T"] = {"confirmed_goal": "完成原始目标A"}
    wid, meta = await wr.rollover_window(
        FakeConn(), orch, "T", supervisor_session_id="SES1",
        progress_fraction=0.62, goal_summary="完成原始目标A",
    )
    assert isinstance(wid, str) and len(wid) > 0
    # 写入了 目标摘要种子 + 近期窗口 (patch_db 记录 2 次 insert)
    assert len(patch_db["ins"]) == 2
    seed_role, seed_content = patch_db["ins"][0][1], patch_db["ins"][0][2]
    assert seed_role == "system"
    assert "完成原始目标A" in seed_content
    # 状态记录: supervisor=原会话, worker=新会话, generation=1
    assert meta["current_generation"] == 1
    assert meta["supervisor_session_id"] == "SES1"
    assert meta["worker_session_id"] == wid
    # orch 状态里也能查到 rollover
    assert orch._state["T"]["rollover"]["worker_session_id"] == wid


# ═══════════════════════════════════════════════════════════
# supervise_round (翻滚触发 + 约束回写 + 升级上报)
# ═══════════════════════════════════════════════════════════

async def test_supervise_round_rollover_and_escalate(patch_db, fake_llm, monkeypatch):
    orch = FakeOrch()
    orch._state["T"] = {"confirmed_goal": "完成原始目标A"}

    async def fake_sid(o, t): return "SES1"
    async def fake_latest(o, t): return "Worker 最新产出: 模块X"
    monkeypatch.setattr(wr, "_fetch_task_session_id", fake_sid)
    monkeypatch.setattr(wr, "_fetch_latest_completed_output", fake_latest)
    fake_llm["mode"] = "supervisor_drift"

    # 进度 0.62 → 翻滚 + 监督(偏离) → 升级上报主 Agent
    r1 = await wr.supervise_round(orch, "T", total=10, completed_count=6)
    assert r1["rolled"] is True
    types = [e["type"] for e in orch._ws_manager.sent]
    assert "long_task_window_rollover" in types
    assert "long_task_supervisor_escalation" in types  # 双向验证发现监督者本身跑偏
    assert orch._state["T"]["rollover"].get("pending_arbitration") is True

async def test_supervise_round_no_rollover_passthrough(patch_db, fake_llm, monkeypatch):
    orch = FakeOrch()
    orch._state["T"] = {"confirmed_goal": "完成原始目标A",
                        "rollover": {"current_generation": 1, "last_supervision_progress": 0.62}}

    async def fake_sid(o, t): return "SES1"
    async def fake_latest(o, t): return "产出"
    monkeypatch.setattr(wr, "_fetch_task_session_id", fake_sid)
    monkeypatch.setattr(wr, "_fetch_latest_completed_output", fake_latest)
    fake_llm["mode"] = "pass"

    # 进度 0.80 不在翻滚带 → 不翻滚; 节流间隔未到 (0.80-0.62<0.15? 0.18>=0.15 → 会监督一次)
    r = await wr.supervise_round(orch, "T", total=10, completed_count=8)
    assert r["rolled"] is False
    # 通过路径不升级
    assert not any(e["type"] == "long_task_supervisor_escalation" for e in orch._ws_manager.sent)


# ═══════════════════════════════════════════════════════════
# 主 Agent 仲裁消费 (闭环"联动"落点)
# ═══════════════════════════════════════════════════════════

async def test_get_pending_arbitrations(monkeypatch):
    orch = FakeOrch()

    async def fake_pending(o, limit=50):
        return [(
            "T1",
            {"current_generation": 2, "goal_summary": "完成A",
             "worker_session_id": "W1", "supervisor_session_id": "S1",
             "last_escalation": {"verdict": "supervisor_drift"}},
        )]
    monkeypatch.setattr(wr, "_fetch_pending_arbitrations", fake_pending)

    items = await wr.get_pending_arbitrations(orch)
    assert len(items) == 1
    assert items[0]["task_id"] == "T1"
    assert items[0]["pending_arbitration"] is True
    assert items[0]["goal_summary"] == "完成A"
    assert items[0]["last_escalation"]["verdict"] == "supervisor_drift"


async def test_resolve_adjust(patch_db):
    orch = FakeOrch()
    orch._state["T"] = {"rollover": {"pending_arbitration": True}}
    r = await wr.resolve_arbitration(orch, "T", decision="adjust", directive="优先完成模块X")
    assert r["resolved"] is True
    assert r["applied"] == "constraint_injected"
    assert orch.injected == ["优先完成模块X"]          # 约束已广播回 running 节点
    assert orch._state["T"]["rollover"].get("pending_arbitration") is False


async def test_resolve_reanchor(patch_db):
    orch = FakeOrch()
    orch._state["T"] = {"rollover": {"pending_arbitration": True, "goal_summary": "旧目标"}}
    r = await wr.resolve_arbitration(orch, "T", decision="reanchor", directive="新目标")
    assert r["applied"] == "goal_reanchored"
    assert orch._state["T"]["rollover"]["goal_summary"] == "新目标"  # Worker 下一窗口据此对齐
    assert orch.injected == []                          # reanchor 不写约束


async def test_resolve_dismiss(patch_db):
    orch = FakeOrch()
    orch._state["T"] = {"rollover": {"pending_arbitration": True}}
    r = await wr.resolve_arbitration(orch, "T", decision="dismiss")
    assert r["resolved"] is True
    assert r["applied"] is None
    assert orch.injected == []
    assert orch._state["T"]["rollover"].get("pending_arbitration") is False


async def test_resolve_abort(patch_db):
    orch = FakeOrch()
    orch._state["T"] = {"rollover": {"pending_arbitration": True, "current_generation": 1}}
    r = await wr.resolve_arbitration(orch, "T", decision="abort", directive="停止执行")
    assert r["resolved"] is True and r["decision"] == "abort"
    assert orch._state["T"]["rollover"].get("pending_arbitration") is False
    assert orch._state["T"]["rollover"].get("aborted") is True
    assert any(
        e["type"] == "long_task_arbitration_resolved" and e["action"] == "abort"
        for e in orch._ws_manager.sent
    )


async def test_resolve_unknown_decision(patch_db):
    orch = FakeOrch()
    r = await wr.resolve_arbitration(orch, "T", decision="bogus")
    assert r["ok"] is False and r["resolved"] is False


async def test_resolve_adjust_missing_directive(patch_db):
    orch = FakeOrch()
    r = await wr.resolve_arbitration(orch, "T", decision="adjust")
    assert r["ok"] is False and r["resolved"] is False
