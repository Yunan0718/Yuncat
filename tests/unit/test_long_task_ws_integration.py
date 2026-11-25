"""tests/unit/test_long_task_ws_integration.py —— Phase 80 WS 三层触发 + 委托集成测试

覆盖 routes/ws.py 中 Phase 80 新增的 4 个集成点：
1. _handle_submit_task long_task_mode=true → 调 orchestrator.start（Tier 1 显式）
2. _handle_submit_task complexity=extreme → 发 long_task_suggested（Tier 2 建议）
3. _handle_submit_task complexity≠extreme → 不发 long_task_suggested
4. _handle_long_task_confirm → 创建新 task + orchestrator.start（Tier 3 确认）
5. _handle_long_task_confirm orchestrator 未初始化 → 推 error
6. _handle_goal_clarification_response 长任务委托 → 调 orchestrator.handle_clarification_response
"""
import sys
import types
from pathlib import Path

import pytest

# 把 src-python 加入 path（对齐 test_ws_goal_clarification.py 风格）
_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src-python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import DEFAULT_AGENT_ID  # noqa: E402

# 导入被测函数
from routes.ws import (  # noqa: E402
    _handle_goal_clarification_response,
    _handle_long_task_confirm,
    _handle_submit_task,
)


# ═══════════════════════════════════════════════════════════════
# Mock 工具
# ═══════════════════════════════════════════════════════════════

class _MockWS:
    """模拟 FastAPI WebSocket——只记录 send_json 调用"""
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, msg: dict) -> None:
        self.sent.append(msg)


class _MockCursor:
    """模拟 aiosqlite cursor——fetchone 返回预设行"""
    def __init__(self, row=None):
        self._row = row

    async def fetchone(self):
        return self._row


class _MockDB:
    """模拟 aiosqlite 连接——execute 返回 cursor，commit no-op，记录查询"""
    def __init__(self, row=None):
        self._row = row
        self.queries: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, params: tuple = ()) -> _MockCursor:
        self.queries.append((sql, params))
        return _MockCursor(self._row)

    async def commit(self):
        pass


class _MockAgentService:
    """模拟 AgentService——db / register_ws / start_task / _safe_send"""
    def __init__(self, db=None, start_task_return="task-from-start"):
        self.db = db or _MockDB()
        self._start_task_return = start_task_return
        self.register_ws_calls: list[tuple] = []
        self.start_task_calls: list[dict] = []
        self.sent: list[tuple[str, dict]] = []  # _safe_send 记录

    def register_ws(self, task_id: str, ws) -> None:
        """sync——对齐真实 AgentService.register_ws（ws.py 调用时不 await）"""
        self.register_ws_calls.append((task_id, ws))

    async def start_task(self, agent_id, prompt, session_id, ws, mode="agent", extra_context=None, **kwargs):
        self.start_task_calls.append({
            "agent_id": agent_id, "prompt": prompt, "session_id": session_id,
            "ws": ws, "mode": mode, "extra_context": extra_context,
        })
        return self._start_task_return

    async def _safe_send(self, task_id: str, msg: dict) -> None:
        self.sent.append((task_id, msg))


class _MockOrchestrator:
    """模拟 LongTaskOrchestrator——is_long_task / start / handle_clarification_response"""
    def __init__(self, is_long_task_return=False, raise_on_start=False, raise_on_handle=False):
        self._is_long_task_return = is_long_task_return
        self._raise_on_start = raise_on_start
        self._raise_on_handle = raise_on_handle
        self.start_calls: list[dict] = []
        self.handle_calls: list[dict] = []
        self.is_long_task_calls: list[str] = []

    async def is_long_task(self, task_id: str) -> bool:
        self.is_long_task_calls.append(task_id)
        return self._is_long_task_return

    async def start(self, task_id: str, prompt: str, agent_id: str = "hermes",
                    language: str = "zh", max_mode: bool = False,
                    capability_level: int = 1, reasoning_effort: str = "") -> None:
        # reasoning_effort 是 2026-09-10 思考强度链路新增的形参——mock 必须同步，
        # 否则真实 ws.py 传参时 TypeError（且只在集成路径暴露）
        self.start_calls.append({
            "task_id": task_id, "prompt": prompt, "agent_id": agent_id,
            "language": language, "max_mode": max_mode,
            "capability_level": capability_level,
            "reasoning_effort": reasoning_effort,
        })
        if self._raise_on_start:
            raise RuntimeError("mock orchestrator.start fail")

    async def handle_clarification_response(self, task_id, answers, need_more=False):
        self.handle_calls.append({
            "task_id": task_id, "answers": answers, "need_more": need_more,
        })
        if self._raise_on_handle:
            raise RuntimeError("mock handle_clarification_response fail")


class _MockDeliberationService:
    """模拟 DeliberationService——assess_complexity 返回预设"""
    def __init__(self, complexity=None):
        self._complexity = complexity
        self.assess_calls: list[str] = []

    def assess_complexity(self, prompt: str):
        self.assess_calls.append(prompt)
        return self._complexity


class _MockClarifier:
    """模拟 GoalClarifier——refine_goal 不应在委托分支被调用"""
    def __init__(self):
        self.refine_calls: list = []

    async def refine_goal(self, qa_list, agent_id="hermes"):
        self.refine_calls.append(qa_list)
        return {"confirmed_goal": "x", "grading_levels": [], "assumptions": []}


def _make_app(
    orchestrator=None, clarifier=None, db=None, agent_service=None,
    deliberation_service=None, has_orchestrator_attr=True,
):
    """构造 _MockApp——has_orchestrator_attr=False 时完全不设 long_task_orchestrator 属性"""
    state = types.SimpleNamespace(
        goal_clarifier=clarifier or _MockClarifier(),
        db=db or _MockDB(),
        agent_service=agent_service or _MockAgentService(),
        deliberation_service=deliberation_service,
        slash_commands=None,  # 不测 slash command 分支
        adapters={"hermes": object()},  # explicit_agent 路径不查 adapters，但保留以防万一
    )
    if has_orchestrator_attr:
        state.long_task_orchestrator = orchestrator
    return types.SimpleNamespace(state=state)


# ═══════════════════════════════════════════════════════════════
# TestSubmitTaskLongTaskMode（Tier 1 显式）
# ═══════════════════════════════════════════════════════════════

class TestSubmitTaskLongTaskMode:
    """Tier 1: long_task_mode=true → 调 orchestrator.start，不走 start_task"""

    async def test_long_task_mode_calls_orchestrator_start(self):
        orch = _MockOrchestrator()
        db = _MockDB()
        agent_svc = _MockAgentService(db=db)
        app = _make_app(orchestrator=orch, db=db, agent_service=agent_svc)
        ws = _MockWS()

        await _handle_submit_task(app, ws, {
            "type": "submit_task",
            "prompt": "build a complex tool",
            "agent_id": "hermes",  # explicit_agent 跳过 intent routing
            # 外部 Agent 需 MAX 档才可点名（见 _gate_external_agent）
            "max_mode": True,
            "long_task_mode": True,
        })

        # orchestrator.start 被调用
        assert len(orch.start_calls) == 1
        call = orch.start_calls[0]
        assert call["prompt"] == "build a complex tool"
        assert call["agent_id"] == "hermes"
        # task_id 是 uuid4 生成的非空字符串
        assert len(call["task_id"]) > 0

        # DB INSERT 发生
        insert_queries = [q for q in db.queries if "INSERT INTO tasks" in q[0]]
        assert len(insert_queries) == 1

        # register_ws 被调
        assert len(agent_svc.register_ws_calls) == 1

        # ws 收到 task_created + long_task: True
        task_created_msgs = [m for m in ws.sent if m.get("type") == "task_created"]
        assert len(task_created_msgs) == 1
        assert task_created_msgs[0]["long_task"] is True
        assert task_created_msgs[0]["route_info"]["agent_id"] == "hermes"

        # start_task 未被调用（走长任务路径，不走普通 start_task）
        assert len(agent_svc.start_task_calls) == 0

    async def test_long_task_mode_no_orchestrator_pushes_error(self):
        """long_task_mode=True 但 orchestrator 未初始化 → 推 error"""
        agent_svc = _MockAgentService()
        # has_orchestrator_attr=False → app.state 无 long_task_orchestrator 属性
        app = _make_app(agent_service=agent_svc, has_orchestrator_attr=False)
        # 显式设为 None（模拟 getattr 返回 None）
        app.state.long_task_orchestrator = None
        ws = _MockWS()

        await _handle_submit_task(app, ws, {
            "type": "submit_task",
            "prompt": "do something",
            "agent_id": "hermes",
            "long_task_mode": True,
        })

        assert any(m.get("type") == "error" and "未初始化" in m.get("message", "") for m in ws.sent)
        assert len(agent_svc.start_task_calls) == 0


# ═══════════════════════════════════════════════════════════════
# TestSubmitTaskComplexitySuggestion（Tier 2 建议）
# ═══════════════════════════════════════════════════════════════

class TestSubmitTaskComplexitySuggestion:
    """Tier 2: complexity=extreme → 发 long_task_suggested（不阻塞正常执行）"""

    async def test_complexity_extreme_sends_suggestion(self):
        """complexity.level=='extreme' → ws 收到 long_task_suggested"""
        orch = _MockOrchestrator()
        agent_svc = _MockAgentService(start_task_return="task-extreme")
        delib = _MockDeliberationService(complexity={"level": "extreme", "score": 95})
        app = _make_app(orchestrator=orch, agent_service=agent_svc, deliberation_service=delib)
        ws = _MockWS()

        await _handle_submit_task(app, ws, {
            "type": "submit_task",
            "prompt": "very complex task",
            "agent_id": "hermes",  # 跳过 intent routing
            # 无 long_task_mode → 走普通 start_task
        })

        # start_task 被调（正常执行不阻塞）
        assert len(agent_svc.start_task_calls) == 1
        # assess_complexity 被调
        assert len(delib.assess_calls) == 1

        # ws 收到 task_created
        task_created = [m for m in ws.sent if m.get("type") == "task_created"]
        assert len(task_created) == 1
        assert task_created[0]["task_id"] == "task-extreme"
        assert task_created[0]["complexity"] == {"level": "extreme", "score": 95}

        # ws 收到 long_task_suggested
        suggestions = [m for m in ws.sent if m.get("type") == "long_task_suggested"]
        assert len(suggestions) == 1
        assert suggestions[0]["task_id"] == "task-extreme"
        assert "extreme" in suggestions[0]["reason"]
        assert suggestions[0]["complexity"] == {"level": "extreme", "score": 95}

        # orchestrator.start 未被调（Tier 2 只建议，不自动进入）
        assert len(orch.start_calls) == 0

    async def test_complexity_not_extreme_no_suggestion(self):
        """complexity.level=='moderate' → 不发 long_task_suggested"""
        orch = _MockOrchestrator()
        agent_svc = _MockAgentService(start_task_return="task-moderate")
        delib = _MockDeliberationService(complexity={"level": "moderate", "score": 50})
        app = _make_app(orchestrator=orch, agent_service=agent_svc, deliberation_service=delib)
        ws = _MockWS()

        await _handle_submit_task(app, ws, {
            "type": "submit_task",
            "prompt": "moderate task",
            "agent_id": "hermes",
        })

        # 正常执行
        assert len(agent_svc.start_task_calls) == 1
        # 无 long_task_suggested
        suggestions = [m for m in ws.sent if m.get("type") == "long_task_suggested"]
        assert len(suggestions) == 0

    async def test_no_deliberation_service_no_suggestion(self):
        """deliberation_service=None → 不评估复杂度，不发 long_task_suggested"""
        orch = _MockOrchestrator()
        agent_svc = _MockAgentService(start_task_return="task-no-delib")
        # deliberation_service=None
        app = _make_app(orchestrator=orch, agent_service=agent_svc, deliberation_service=None)
        ws = _MockWS()

        await _handle_submit_task(app, ws, {
            "type": "submit_task",
            "prompt": "task",
            "agent_id": "hermes",
        })

        # 正常执行
        assert len(agent_svc.start_task_calls) == 1
        # 无 long_task_suggested
        suggestions = [m for m in ws.sent if m.get("type") == "long_task_suggested"]
        assert len(suggestions) == 0


# ═══════════════════════════════════════════════════════════════
# TestLongTaskConfirm（Tier 3 确认）
# ═══════════════════════════════════════════════════════════════

class TestLongTaskConfirm:
    """Tier 3: _handle_long_task_confirm → 创建新 task + orchestrator.start"""

    async def test_creates_task_and_starts_orchestrator(self):
        orch = _MockOrchestrator()
        db = _MockDB()
        agent_svc = _MockAgentService(db=db)
        app = _make_app(orchestrator=orch, db=db, agent_service=agent_svc)
        ws = _MockWS()

        await _handle_long_task_confirm(app, ws, {
            "type": "long_task_confirm",
            "prompt": "user confirmed long task",
            "agent_id": "hermes",
            # 外部 Agent（hermes/openclaw）仅在 MAX（capability_level>=2）下可被点名调用，
            # 否则回落到原生 yuncat——见 routes/ws.py 的 explicit_agent 分支
            "max_mode": True,
            "session_id": "sess-123",
        })

        # DB INSERT 发生
        insert_queries = [q for q in db.queries if "INSERT INTO tasks" in q[0]]
        assert len(insert_queries) == 1
        # session_id 透传
        assert "sess-123" in insert_queries[0][1]

        # register_ws 被调
        assert len(agent_svc.register_ws_calls) == 1

        # ws 收到 task_created + long_task: True
        task_created = [m for m in ws.sent if m.get("type") == "task_created"]
        assert len(task_created) == 1
        assert task_created[0]["long_task"] is True
        assert task_created[0]["prompt"] == "user confirmed long task"
        assert task_created[0]["route_info"]["agent_id"] == "hermes"

        # orchestrator.start 被调
        assert len(orch.start_calls) == 1
        assert orch.start_calls[0]["prompt"] == "user confirmed long task"
        assert orch.start_calls[0]["agent_id"] == "hermes"
        # task_id 与 task_created 中的一致
        assert orch.start_calls[0]["task_id"] == task_created[0]["task_id"]

    async def test_explicit_external_agent_falls_back_without_max_mode(self):
        """非 MAX 模式点名外部 Agent → 回落到原生 DEFAULT_AGENT_ID。

        2026-09-10 决策：外部 Agent 只在 MAX（全功能）档可被调用，
        轻量/标准档即使用户点名也回落，避免为简单任务拉起外部网关。
        """
        from config import DEFAULT_AGENT_ID, EXTERNAL_AGENT_IDS

        assert "hermes" in EXTERNAL_AGENT_IDS  # 前提：hermes 确实属外部 agent
        orch = _MockOrchestrator()
        db = _MockDB()
        app = _make_app(orchestrator=orch, db=db, agent_service=_MockAgentService(db=db))
        ws = _MockWS()

        await _handle_long_task_confirm(app, ws, {
            "type": "long_task_confirm",
            "prompt": "light task naming an external agent",
            "agent_id": "hermes",
            # 不传 max_mode / capability_level → _cap_level = 1（非 MAX）
        })

        assert orch.start_calls[0]["agent_id"] == DEFAULT_AGENT_ID
        assert DEFAULT_AGENT_ID not in EXTERNAL_AGENT_IDS

    async def test_no_orchestrator_pushes_error(self):
        """orchestrator=None → 推 error，不创建 task"""
        agent_svc = _MockAgentService()
        app = _make_app(agent_service=agent_svc)
        app.state.long_task_orchestrator = None
        ws = _MockWS()

        await _handle_long_task_confirm(app, ws, {
            "type": "long_task_confirm",
            "prompt": "do something",
        })

        assert any(m.get("type") == "error" and "未初始化" in m.get("message", "") for m in ws.sent)
        # DB 未写入
        insert_queries = [q for q in agent_svc.db.queries if "INSERT INTO tasks" in q[0]]
        assert len(insert_queries) == 0

    async def test_missing_prompt_pushes_error(self):
        """prompt 缺失 → 推 error"""
        orch = _MockOrchestrator()
        agent_svc = _MockAgentService()
        app = _make_app(orchestrator=orch, agent_service=agent_svc)
        ws = _MockWS()

        await _handle_long_task_confirm(app, ws, {
            "type": "long_task_confirm",
            # 无 prompt
        })

        assert any(m.get("type") == "error" and "prompt" in m.get("message", "") for m in ws.sent)
        assert len(orch.start_calls) == 0

    async def test_default_agent_id_when_not_specified(self):
        """msg 无 agent_id → 用 DEFAULT_AGENT_ID"""
        orch = _MockOrchestrator()
        db = _MockDB()
        agent_svc = _MockAgentService(db=db)
        app = _make_app(orchestrator=orch, db=db, agent_service=agent_svc)
        ws = _MockWS()

        await _handle_long_task_confirm(app, ws, {
            "type": "long_task_confirm",
            "prompt": "no agent specified",
        })

        # orchestrator.start 用 DEFAULT_AGENT_ID（Phase N 起默认 "yuncat"）
        assert len(orch.start_calls) == 1
        assert orch.start_calls[0]["agent_id"] == DEFAULT_AGENT_ID


# ═══════════════════════════════════════════════════════════════
# TestGoalClarificationResponseDelegation（委托）
# ═══════════════════════════════════════════════════════════════

class TestGoalClarificationResponseDelegation:
    """_handle_goal_clarification_response 长任务委托 orchestrator"""

    async def test_delegates_to_orchestrator_when_is_long_task(self):
        """orchestrator.is_long_task=True → 调 orchestrator.handle_clarification_response，不调 clarifier"""
        orch = _MockOrchestrator(is_long_task_return=True)
        clarifier = _MockClarifier()
        agent_svc = _MockAgentService()
        app = _make_app(orchestrator=orch, clarifier=clarifier, agent_service=agent_svc)
        ws = _MockWS()

        await _handle_goal_clarification_response(app, ws, {
            "type": "goal_clarification_response",
            "task_id": "task-delegate",
            "answers": [{"question": "Q1", "answer": "A1"}],
            "need_more": False,
        })

        # orchestrator.is_long_task 被调
        assert orch.is_long_task_calls == ["task-delegate"]
        # orchestrator.handle_clarification_response 被调
        assert len(orch.handle_calls) == 1
        assert orch.handle_calls[0]["task_id"] == "task-delegate"
        assert orch.handle_calls[0]["answers"] == [{"question": "Q1", "answer": "A1"}]
        assert orch.handle_calls[0]["need_more"] is False
        # clarifier.refine_goal 未被调（委托给 orchestrator 了）
        assert len(clarifier.refine_calls) == 0
        # ws 无 error
        assert all(m.get("type") != "error" for m in ws.sent)

    async def test_not_long_task_falls_through_to_clarifier(self):
        """orchestrator.is_long_task=False → 走原 clarifier 路径（不委托）"""
        orch = _MockOrchestrator(is_long_task_return=False)
        clarifier = _MockClarifier()
        agent_svc = _MockAgentService()
        app = _make_app(orchestrator=orch, clarifier=clarifier, agent_service=agent_svc)
        ws = _MockWS()

        await _handle_goal_clarification_response(app, ws, {
            "type": "goal_clarification_response",
            "task_id": "task-normal",
            "answers": [{"question": "Q1", "answer": "A1"}],
        })

        # orchestrator.is_long_task 被调
        assert orch.is_long_task_calls == ["task-normal"]
        # orchestrator.handle_clarification_response 未被调
        assert len(orch.handle_calls) == 0
        # clarifier.refine_goal 被调（走原路径）
        assert len(clarifier.refine_calls) == 1

    async def test_orchestrator_handle_exception_pushes_error(self):
        """orchestrator.handle_clarification_response 抛异常 → 推 error"""
        orch = _MockOrchestrator(is_long_task_return=True, raise_on_handle=True)
        clarifier = _MockClarifier()
        agent_svc = _MockAgentService()
        app = _make_app(orchestrator=orch, clarifier=clarifier, agent_service=agent_svc)
        ws = _MockWS()

        await _handle_goal_clarification_response(app, ws, {
            "type": "goal_clarification_response",
            "task_id": "task-fail",
            "answers": [{"question": "Q1", "answer": "A1"}],
        })

        # ws 收到 error（含「长任务处理失败」）
        errors = [m for m in ws.sent if m.get("type") == "error"]
        assert len(errors) == 1
        assert "长任务处理失败" in errors[0]["message"]
