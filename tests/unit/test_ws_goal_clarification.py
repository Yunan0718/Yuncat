"""tests/unit/test_ws_goal_clarification.py —— Phase 78 WS 消息处理测试

覆盖 routes/ws.py::_handle_goal_clarification_response 的 5 个分支：
1. need_more=false 默认分支 → 推 goal_confirmed
2. need_more=true → 查 tasks.input → 推 goal_clarification_request (stage=followup)
3. clarifier 未初始化 → 推 error
4. task_id / answers 缺失 → 推 error
5. refine_goal 抛异常 → 推 error
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

# 把 src-python 加入 path（对齐其他 tests/unit/test_*.py 的 import 风格）
_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src-python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── Mock 工具 ──

class _MockWS:
    """模拟 FastAPI WebSocket——只记录 send_json 调用"""
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, msg: dict) -> None:
        self.sent.append(msg)


class _MockCursor:
    def __init__(self, row=None):
        self._row = row

    async def fetchone(self):
        return self._row


class _MockDB:
    """模拟 aiosqlite 连接——execute 返回预设 cursor"""
    def __init__(self, row=None):
        self._row = row
        self.queries: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, params: tuple = ()) -> _MockCursor:
        self.queries.append((sql, params))
        return _MockCursor(self._row)


class _MockClarifier:
    """模拟 GoalClarifier"""
    def __init__(self, questions=None, refined=None, raise_on_refine=False, raise_on_generate=False):
        self._questions = questions or [{"id": "q1", "type": "scope", "question": "...?"}]
        self._refined = refined or {
            "confirmed_goal": "目标",
            "grading_levels": ["能运行", "有测试"],
            "assumptions": [],
        }
        self._raise_on_refine = raise_on_refine
        self._raise_on_generate = raise_on_generate
        self.generate_calls: list[dict] = []
        self.refine_calls: list[list[dict]] = []

    async def generate_questions(self, task_input, previous_qa=None, agent_id="hermes"):
        self.generate_calls.append({"task_input": task_input, "previous_qa": previous_qa})
        if self._raise_on_generate:
            raise RuntimeError("mock generate fail")
        return self._questions

    async def refine_goal(self, qa_list, agent_id="hermes"):
        self.refine_calls.append(qa_list)
        if self._raise_on_refine:
            raise RuntimeError("mock refine fail")
        return self._refined


class _MockAgentService:
    """模拟 AgentService——只记录 _safe_send 调用"""
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def _safe_send(self, task_id: str, msg: dict) -> None:
        self.sent.append((task_id, msg))


class _MockApp:
    """模拟 FastAPI app.state"""
    def __init__(self, clarifier=None, db=None, agent_service=None):
        self.state = types.SimpleNamespace(
            goal_clarifier=clarifier,
            db=db,
            agent_service=agent_service,
        )


# ── 导入被测函数 ──

from routes.ws import _handle_goal_clarification_response  # noqa: E402


# ── 测试 ──

class TestGoalClarificationResponse:
    """Phase 78: _handle_goal_clarification_response 5 个分支"""

    @pytest.mark.asyncio
    async def test_default_branch_pushes_goal_confirmed(self):
        """need_more=false → refine_goal → 推 goal_confirmed"""
        clarifier = _MockClarifier(refined={
            "confirmed_goal": "做一个 X",
            "grading_levels": ["能运行", "有测试", "有文档"],
            "assumptions": ["假设1"],
        })
        agent_svc = _MockAgentService()
        app = _MockApp(clarifier=clarifier, agent_service=agent_svc)
        ws = _MockWS()

        await _handle_goal_clarification_response(app, ws, {
            "type": "goal_clarification_response",
            "task_id": "task-1",
            "answers": [{"question": "Q1", "answer": "A1"}],
        })

        # refine_goal 被调用一次
        assert len(clarifier.refine_calls) == 1
        assert clarifier.refine_calls[0] == [{"question": "Q1", "answer": "A1"}]
        # ws 没有 error
        assert all(m.get("type") != "error" for m in ws.sent)
        # agent_service._safe_send 收到 goal_confirmed
        assert len(agent_svc.sent) == 1
        tid, msg = agent_svc.sent[0]
        assert tid == "task-1"
        assert msg["type"] == "goal_confirmed"
        assert msg["task_id"] == "task-1"
        assert msg["confirmed_goal"] == "做一个 X"
        assert msg["grading_levels"] == ["能运行", "有测试", "有文档"]
        assert msg["assumptions"] == ["假设1"]

    @pytest.mark.asyncio
    async def test_need_more_branch_pushes_followup_request(self):
        """need_more=true → 查 tasks.input → generate_questions → 推 goal_clarification_request (stage=followup)"""
        clarifier = _MockClarifier(questions=[
            {"id": "q-fb1", "type": "acceptance", "question": "追问?"}
        ])
        agent_svc = _MockAgentService()
        db = _MockDB(row=("原始 task input",))  # SELECT input FROM tasks
        app = _MockApp(clarifier=clarifier, db=db, agent_service=agent_svc)
        ws = _MockWS()

        await _handle_goal_clarification_response(app, ws, {
            "type": "goal_clarification_response",
            "task_id": "task-2",
            "answers": [{"question": "Q1", "answer": "A1"}],
            "need_more": True,
        })

        # DB 查询发生
        assert len(db.queries) == 1
        assert "SELECT input FROM tasks" in db.queries[0][0]
        assert db.queries[0][1] == ("task-2",)

        # generate_questions 被调用，previous_qa 透传
        assert len(clarifier.generate_calls) == 1
        call = clarifier.generate_calls[0]
        assert call["task_input"] == "原始 task input"
        assert call["previous_qa"] == [{"question": "Q1", "answer": "A1"}]

        # 推 goal_clarification_request stage=followup
        assert len(agent_svc.sent) == 1
        tid, msg = agent_svc.sent[0]
        assert tid == "task-2"
        assert msg["type"] == "goal_clarification_request"
        assert msg["stage"] == "followup"
        assert msg["questions"] == [{"id": "q-fb1", "type": "acceptance", "question": "追问?"}]

    @pytest.mark.asyncio
    async def test_clarifier_not_initialized_pushes_error(self):
        """clarifier=None → ws.send_json error"""
        agent_svc = _MockAgentService()
        app = _MockApp(clarifier=None, agent_service=agent_svc)
        ws = _MockWS()

        await _handle_goal_clarification_response(app, ws, {
            "type": "goal_clarification_response",
            "task_id": "task-3",
            "answers": [{"question": "Q1", "answer": "A1"}],
        })

        assert any(m.get("type") == "error" and "未初始化" in m.get("message", "") for m in ws.sent)
        assert len(agent_svc.sent) == 0

    @pytest.mark.asyncio
    async def test_missing_task_id_pushes_error(self):
        """task_id 缺失 → ws.send_json error"""
        clarifier = _MockClarifier()
        agent_svc = _MockAgentService()
        app = _MockApp(clarifier=clarifier, agent_service=agent_svc)
        ws = _MockWS()

        await _handle_goal_clarification_response(app, ws, {
            "type": "goal_clarification_response",
            "answers": [{"question": "Q1", "answer": "A1"}],
        })

        assert any(m.get("type") == "error" and "缺失" in m.get("message", "") for m in ws.sent)
        assert len(clarifier.refine_calls) == 0

    @pytest.mark.asyncio
    async def test_both_missing_pushes_error(self):
        """task_id 和 answers 都缺失 → ws.send_json error（task_id 是关键校验）"""
        clarifier = _MockClarifier()
        agent_svc = _MockAgentService()
        app = _MockApp(clarifier=clarifier, agent_service=agent_svc)
        ws = _MockWS()

        await _handle_goal_clarification_response(app, ws, {
            "type": "goal_clarification_response",
        })

        assert any(m.get("type") == "error" and "缺失" in m.get("message", "") for m in ws.sent)
        assert len(clarifier.refine_calls) == 0

    @pytest.mark.asyncio
    async def test_empty_answers_allowed_falls_back(self):
        """answers=[] 不报错——refine_goal 兜底返回「请用户确认最终目标」"""
        clarifier = _MockClarifier(refined={
            "confirmed_goal": "请用户确认最终目标",
            "grading_levels": ["能运行", "有测试"],
            "assumptions": [],
        })
        agent_svc = _MockAgentService()
        app = _MockApp(clarifier=clarifier, agent_service=agent_svc)
        ws = _MockWS()

        await _handle_goal_clarification_response(app, ws, {
            "type": "goal_clarification_response",
            "task_id": "task-5b",
            "answers": [],
        })

        # 不报错，调 refine_goal，推 goal_confirmed
        assert all(m.get("type") != "error" for m in ws.sent)
        assert len(clarifier.refine_calls) == 1
        assert len(agent_svc.sent) == 1
        assert agent_svc.sent[0][1]["type"] == "goal_confirmed"

    @pytest.mark.asyncio
    async def test_refine_goal_exception_pushes_error(self):
        """refine_goal 抛异常 → ws.send_json error"""
        clarifier = _MockClarifier(raise_on_refine=True)
        agent_svc = _MockAgentService()
        app = _MockApp(clarifier=clarifier, agent_service=agent_svc)
        ws = _MockWS()

        await _handle_goal_clarification_response(app, ws, {
            "type": "goal_clarification_response",
            "task_id": "task-6",
            "answers": [{"question": "Q1", "answer": "A1"}],
        })

        assert any(m.get("type") == "error" and "提炼失败" in m.get("message", "") for m in ws.sent)
        assert len(agent_svc.sent) == 0

    @pytest.mark.asyncio
    async def test_need_more_generate_exception_pushes_error(self):
        """need_more=true 时 generate_questions 抛异常 → ws.send_json error"""
        clarifier = _MockClarifier(raise_on_generate=True)
        agent_svc = _MockAgentService()
        db = _MockDB(row=("input",))
        app = _MockApp(clarifier=clarifier, db=db, agent_service=agent_svc)
        ws = _MockWS()

        await _handle_goal_clarification_response(app, ws, {
            "type": "goal_clarification_response",
            "task_id": "task-7",
            "answers": [{"question": "Q1", "answer": "A1"}],
            "need_more": True,
        })

        assert any(m.get("type") == "error" and "追问" in m.get("message", "") for m in ws.sent)
        assert len(agent_svc.sent) == 0

    @pytest.mark.asyncio
    async def test_need_more_db_exception_falls_back_to_empty_input(self):
        """need_more=true 时 DB 查询抛异常 → task_input="" 但流程继续"""
        clarifier = _MockClarifier(questions=[{"id": "q", "type": "scope", "question": "?"}])
        agent_svc = _MockAgentService()

        # DB execute 抛异常
        class _BrokenDB:
            async def execute(self, *a, **kw):
                raise RuntimeError("db broken")

        app = _MockApp(clarifier=clarifier, db=_BrokenDB(), agent_service=agent_svc)
        ws = _MockWS()

        await _handle_goal_clarification_response(app, ws, {
            "type": "goal_clarification_response",
            "task_id": "task-8",
            "answers": [],
            "need_more": True,
        })

        # 流程继续：generate_questions 收到空 task_input
        assert len(clarifier.generate_calls) == 1
        assert clarifier.generate_calls[0]["task_input"] == ""
        # 推 followup 请求
        assert len(agent_svc.sent) == 1
        assert agent_svc.sent[0][1]["type"] == "goal_clarification_request"
