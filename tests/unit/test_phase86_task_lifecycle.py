"""tests/unit/test_phase86_task_lifecycle.py —— Phase 86 task_lifecycle 事件测试

覆盖：
1. _stall_suggestion 纯函数（5 测试）——last_tool 分类文案
2. _evaluate_round_progress 纯函数（4 测试）——实质进度判定
3. _compute_estimate 纯函数（3 测试）——预估计算 + confidence 公式
4. orchestrator phase_start 推送（5 测试）——CLARIFYING/DECOMPOSING/EXECUTING/COMPLETED/FAILED
5. orchestrator step_done 推送（1 测试）——step 完成时派生 task_lifecycle.step_done

设计要点：
- 纯函数测试直接 import 调用，无需 mock
- orchestrator 测试复用 test_long_task_orchestrator.py 的 mock 模式（轻量重定义）
- ws_manager.sent 收集所有 WS 消息，断言 task_lifecycle 类型存在
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ═══════════════════════════════════════════════════════════════
# 纯函数测试：_stall_suggestion
# ═══════════════════════════════════════════════════════════════

class TestStallSuggestion:
    """Phase 86: _stall_suggestion 根据 last_tool 生成建议文案"""

    def test_read_file_suggestion(self):
        from services.agent.agent_tool_loop import _stall_suggestion
        assert "搜索关键词" in _stall_suggestion("read_file")

    def test_write_file_suggestion(self):
        from services.agent.agent_tool_loop import _stall_suggestion
        assert "路径权限" in _stall_suggestion("write_file")

    def test_run_command_suggestion(self):
        from services.agent.agent_tool_loop import _stall_suggestion
        assert "命令参数" in _stall_suggestion("run_command")

    def test_none_last_tool(self):
        """last_tool=None——无工具调用场景"""
        from services.agent.agent_tool_loop import _stall_suggestion
        assert "无工具调用" in _stall_suggestion(None)

    def test_unknown_tool_default(self):
        """未知工具名——走默认文案"""
        from services.agent.agent_tool_loop import _stall_suggestion
        result = _stall_suggestion("some_unknown_tool")
        assert "连续 3 轮" in result


# ═══════════════════════════════════════════════════════════════
# 纯函数测试：_evaluate_round_progress
# ═══════════════════════════════════════════════════════════════

class TestEvaluateRoundProgress:
    """Phase 86: _evaluate_round_progress 实质进度判定"""

    def test_successful_tool_is_progress(self):
        """工具返回 success=True → 有进度"""
        from services.agent.agent_tool_loop import _evaluate_round_progress
        tool_results = [{"role": "tool", "content": '{"success": true, "output": "ok"}'}]
        assert _evaluate_round_progress(tool_results, "") is True

    def test_failed_tool_no_progress(self):
        """工具返回 success=False → 无进度（除非文本增长）"""
        from services.agent.agent_tool_loop import _evaluate_round_progress
        tool_results = [{"role": "tool", "content": '{"success": false, "error": "fail"}'}]
        assert _evaluate_round_progress(tool_results, "") is False

    def test_long_text_is_progress(self):
        """无工具但文本 > 100 字符 → 有进度"""
        from services.agent.agent_tool_loop import _evaluate_round_progress
        long_text = "x" * 101
        assert _evaluate_round_progress([], long_text) is True

    def test_no_tool_short_text_no_progress(self):
        """无工具且文本 ≤ 100 字符 → 无进度"""
        from services.agent.agent_tool_loop import _evaluate_round_progress
        assert _evaluate_round_progress([], "short") is False

    def test_non_json_content_is_progress(self):
        """非 JSON content（纯文本工具结果）→ 视为有输出"""
        from services.agent.agent_tool_loop import _evaluate_round_progress
        tool_results = [{"role": "tool", "content": "file contents here"}]
        assert _evaluate_round_progress(tool_results, "") is True

    # ---- AUDIT #7: round_reasoning 纳入进度判定 ----

    def test_long_reasoning_is_progress(self):
        """AUDIT #7: 无工具无文本，但 reasoning_content > 100 字符 → 有进度

        DeepSeek 等重推理模型在思考阶段可能少可见文本但累积大量
        reasoning_content。原实现只看 round_text 会误判为 stall。
        """
        from services.agent.agent_tool_loop import _evaluate_round_progress
        long_reasoning = "r" * 101
        assert _evaluate_round_progress([], "", long_reasoning) is True

    def test_short_reasoning_no_progress(self):
        """AUDIT #7: 无工具无文本，且 reasoning_content ≤ 100 字符 → 无进度"""
        from services.agent.agent_tool_loop import _evaluate_round_progress
        assert _evaluate_round_progress([], "", "short reasoning") is False

    def test_reasoning_compensates_for_short_text(self):
        """AUDIT #7: 文本 < 100 字符但推理 > 100 字符 → 有进度（互补判定）"""
        from services.agent.agent_tool_loop import _evaluate_round_progress
        short_text = "x" * 50
        long_reasoning = "r" * 150
        assert _evaluate_round_progress([], short_text, long_reasoning) is True

    def test_reasoning_default_empty_backward_compat(self):
        """AUDIT #7: 不传 round_reasoning（默认空串）→ 行为与原版一致"""
        from services.agent.agent_tool_loop import _evaluate_round_progress
        # 无工具 + 短文本 + 默认空 reasoning → 无进度
        assert _evaluate_round_progress([], "short") is False
        # 无工具 + 长文本 + 默认空 reasoning → 有进度
        assert _evaluate_round_progress([], "x" * 101) is True


# ═══════════════════════════════════════════════════════════════
# 纯函数测试：_compute_estimate
# ═══════════════════════════════════════════════════════════════

class TestComputeEstimate:
    """Phase 86: _compute_estimate 预估计算"""

    def test_round_1_returns_none_remaining(self):
        """tool_round=1 → estimated_remaining=None（数据不足）"""
        from services.agent.agent_tool_loop import _compute_estimate
        started = time.time() - 10  # 10 秒前开始
        elapsed, remaining, confidence = _compute_estimate(started, 1, 15)
        assert elapsed >= 10.0
        assert remaining is None
        assert confidence == 0.25  # 0.2 + 1*0.05

    def test_round_5_computes_remaining(self):
        """tool_round=5 → 按 ratio 外推 remaining"""
        from services.agent.agent_tool_loop import _compute_estimate
        started = time.time() - 50  # 50 秒跑了 5 轮
        elapsed, remaining, confidence = _compute_estimate(started, 5, 15)
        assert elapsed >= 50.0
        assert remaining is not None
        # remaining = elapsed * (15-5)/5 = elapsed * 2
        assert remaining >= 100.0
        assert confidence == 0.45  # 0.2 + 5*0.05

    def test_confidence_capped_at_0_9(self):
        """tool_round=20 → confidence 封顶 0.9（0.2+20*0.05=1.2 > 0.9）"""
        from services.agent.agent_tool_loop import _compute_estimate
        started = time.time()
        _, _, confidence = _compute_estimate(started, 20, 25)
        assert confidence == 0.9


# ═══════════════════════════════════════════════════════════════
# orchestrator mock（复用 test_long_task_orchestrator.py 模式）
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
async def db(tmp_path):
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_phase86.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


async def _create_task(db, task_id: str, prompt: str = "test prompt") -> None:
    await db.execute(
        "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (task_id, prompt, "running", "hermes", "sess-1", int(time.time())),
    )
    await db.commit()


class _MockAgentService:
    def __init__(self, db, spawn_results=None):
        self.db = db
        self._spawn_results = spawn_results or []
        self._spawn_calls = []

    async def spawn_subagent(self, agent_id, prompt, context_summary="", auto_approve=False,
                             language="zh", max_mode=False, long_task_step_role="",
                             model_tier="normal"):
        # Phase 87 executor 的 _retry_spawn 会传扩展参数——mock 签名须对齐，
        # 否则 TypeError 被 _retry_spawn 捕获转成 {"ok": False}，节点失败 → 任务 failed
        self._spawn_calls.append((agent_id, prompt, context_summary))
        if self._spawn_results:
            return self._spawn_results.pop(0)
        return {"ok": True, "task_id": "sub-1", "output": "mock output"}


class _MockGoalClarifier:
    def __init__(self):
        self._questions = [{"id": "q1", "type": "scope", "question": "做什么?"}]
        self._refine_result = {
            "confirmed_goal": "完成测试目标",
            "grading_levels": ["能运行"],
            "assumptions": [],
        }
        self.generate_calls = []
        self.refine_calls = []

    async def generate_questions(self, task_input, previous_qa=None, agent_id="hermes"):
        self.generate_calls.append({"task_input": task_input})
        return self._questions

    async def refine_goal(self, qa_list, agent_id="hermes"):
        self.refine_calls.append(qa_list)
        return self._refine_result


class _MockTaskDecomposer:
    def __init__(self, db):
        self._db = db
        self.decompose_calls = []

    async def decompose(self, task_id, confirmed_goal, agent_id="hermes", standards_context=None):
        self.decompose_calls.append((task_id, confirmed_goal))
        from repositories.plan_step_repo import create_plan_node
        await create_plan_node(self._db, {
            "task_id": task_id, "level": 0, "title": confirmed_goal[:80],
            "description": "root", "acceptance_criteria": ["done"],
        })
        await create_plan_node(self._db, {
            "task_id": task_id, "parent_id": "n0", "level": 1,
            "title": "步骤1", "description": "做A", "acceptance_criteria": ["A完成"],
        })
        return {"nodes": [], "root_count": 1, "total": 2, "source": "mock"}


class _MockWsManager:
    def __init__(self):
        self.sent = []

    async def safe_send(self, task_id, payload):
        payload = dict(payload)
        payload["_task_id"] = task_id
        self.sent.append(payload)


def _make_orchestrator(db, agent_service=None, ws_manager=None):
    from services.long_task.orchestrator import LongTaskOrchestrator
    agent_service = agent_service or _MockAgentService(db)
    clarifier = _MockGoalClarifier()
    decomposer = _MockTaskDecomposer(db)
    ws_manager = ws_manager or _MockWsManager()
    return LongTaskOrchestrator(
        agent_service=agent_service,
        goal_clarifier=clarifier,
        task_decomposer=decomposer,
        conn=db,
        ws_manager=ws_manager,
    ), agent_service, clarifier, decomposer, ws_manager


def _lifecycle_msgs(ws):
    """从 ws.sent 过滤出 task_lifecycle 消息"""
    return [m for m in ws.sent if m.get("type") == "task_lifecycle"]


# ═══════════════════════════════════════════════════════════════
# orchestrator phase_start 测试
# ═══════════════════════════════════════════════════════════════

class TestPhaseStart:
    """Phase 86: orchestrator 在 phase 转换点推送 task_lifecycle.phase_start"""

    async def test_phase_start_clarifying(self, db):
        """start() → 推 phase_start(clarifying)"""
        await _create_task(db, "t1")
        orch, _, _, _, ws = _make_orchestrator(db)
        await orch.start("t1", "build a tool")
        lc_msgs = _lifecycle_msgs(ws)
        phase_starts = [m for m in lc_msgs if m.get("subtype") == "phase_start"]
        assert len(phase_starts) >= 1
        msg = phase_starts[0]
        assert msg["phase"] == "clarifying"
        assert "description" in msg
        assert "started_at" in msg
        assert msg["task_id"] == "t1"

    async def test_phase_start_decomposing(self, db):
        """handle_clarification_response(need_more=False) → 推 phase_start(decomposing)"""
        await _create_task(db, "t1")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "done"},
        ])
        orch, _, _, _, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch.start("t1", "build a tool")
        ws.sent.clear()
        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )
        lc_msgs = _lifecycle_msgs(ws)
        phase_starts = [m for m in lc_msgs if m.get("subtype") == "phase_start"]
        phases = [m["phase"] for m in phase_starts]
        assert "decomposing" in phases

    async def test_phase_start_executing(self, db):
        """全流程 → 推 phase_start(executing)"""
        await _create_task(db, "t1")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "done"},
        ])
        orch, _, _, _, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch.start("t1", "build a tool")
        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )
        lc_msgs = _lifecycle_msgs(ws)
        phase_starts = [m for m in lc_msgs if m.get("subtype") == "phase_start"]
        phases = [m["phase"] for m in phase_starts]
        assert "executing" in phases

    async def test_phase_start_completed(self, db):
        """全流程成功 → 推 phase_start(completed)"""
        await _create_task(db, "t1")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "done"},
        ])
        orch, _, _, _, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch.start("t1", "build a tool")
        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )
        lc_msgs = _lifecycle_msgs(ws)
        phase_starts = [m for m in lc_msgs if m.get("subtype") == "phase_start"]
        phases = [m["phase"] for m in phase_starts]
        assert "completed" in phases

    async def test_phase_start_failed(self, db):
        """spawn_subagent 失败 → 推 phase_start(failed)"""
        await _create_task(db, "t1")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": False, "error": "执行失败"},
        ])
        orch, _, _, _, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch.start("t1", "build a tool")
        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )
        lc_msgs = _lifecycle_msgs(ws)
        phase_starts = [m for m in lc_msgs if m.get("subtype") == "phase_start"]
        phases = [m["phase"] for m in phase_starts]
        assert "failed" in phases


# ═══════════════════════════════════════════════════════════════
# orchestrator step_done 测试
# ═══════════════════════════════════════════════════════════════

class TestStepDone:
    """Phase 86: step 完成时派生 task_lifecycle.step_done"""

    async def test_step_done_pushed_with_preview(self, db):
        """step 完成 → 推 task_lifecycle.step_done，output_preview 截断 + 溯源指针

        Task 6.15: output_preview 不再是裸 output[:200]，而是截断后附
        `... [see node {step_id} for full content]` 指针。
        """
        await _create_task(db, "t1")
        long_output = "A" * 300  # 300 字符，preview 应截断为 200 + 指针
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": long_output},
        ])
        orch, _, _, _, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch.start("t1", "build a tool")
        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )
        lc_msgs = _lifecycle_msgs(ws)
        step_dones = [m for m in lc_msgs if m.get("subtype") == "step_done"]
        assert len(step_dones) >= 1
        msg = step_dones[0]
        assert msg["task_id"] == "t1"
        assert "step_index" in msg
        assert "total_steps" in msg
        # Task 6.15: 截断 200 字符内容 + 溯源指针（指向 plan_steps 节点）
        assert msg["output_preview"].startswith("A" * 200)
        assert "see node" in msg["output_preview"]
        assert "for full content" in msg["output_preview"]
        assert len(msg["output_preview"]) > 200  # 内容 + 指针
