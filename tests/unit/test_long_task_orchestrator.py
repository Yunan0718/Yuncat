"""tests/unit/test_long_task_orchestrator.py —— Phase 80 LongTaskOrchestrator 测试

覆盖：
1. _LongTaskStateStore —— 读写 task_state.long_task JSON 子节点
2. start —— 持久化 phase=clarifying + 推 goal_clarification_request
3. handle_clarification_response —— need_more 分支 + 默认分支(refine→decompose→execute)
4. _execute_steps —— 全部成功 / 中途失败 / 跳过 root
5. replan —— 保留 completed 节点 + 删 pending + 重新 decompose
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from config import DEFAULT_AGENT_ID


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
async def db(tmp_path):
    """aiosqlite + SCHEMA_SQL——每个测试独立 DB"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_orchestrator.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


async def _create_task(db, task_id: str, prompt: str = "test prompt") -> None:
    """在 tasks 表插入一行（orchestrator 读写 task_state 需要 task 存在）"""
    import time
    await db.execute(
        "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (task_id, prompt, "running", "hermes", "sess-1", int(time.time())),
    )
    await db.commit()


class _MockAgentService:
    """模拟 AgentService——spawn_subagent 返回可控结果"""
    def __init__(self, db, spawn_results: list[dict] | None = None):
        self.db = db
        self._spawn_results = spawn_results or []
        self._spawn_calls: list[tuple[str, str, str]] = []

    async def spawn_subagent(self, agent_id: str, prompt: str, context_summary: str = "", auto_approve: bool = False, **kwargs) -> dict:
        self._spawn_calls.append((agent_id, prompt, context_summary))
        if self._spawn_results:
            return self._spawn_results.pop(0)
        return {"ok": True, "task_id": "sub-1", "output": "mock output"}


class _MockGoalClarifier:
    """模拟 GoalClarifier

    Task 6.11: 支持 refine_results 列表——连续 refine_goal 调用依次弹出
    （用于 auto_followup 多轮测试）。列表耗尽后回退到 refine_result。
    """
    def __init__(self, questions=None, refine_result=None, refine_results=None):
        self._questions = questions or [{"id": "q1", "type": "scope", "question": "做什么?"}]
        self._refine_result = refine_result or {
            "confirmed_goal": "完成测试目标",
            "grading_levels": ["能运行", "有测试"],
            "assumptions": [],
        }
        self._refine_results = list(refine_results) if refine_results else None
        self.generate_calls: list[dict] = []
        self.refine_calls: list[list[dict]] = []

    async def generate_questions(self, task_input, previous_qa=None, agent_id="hermes", user_context=None):
        self.generate_calls.append({"task_input": task_input, "previous_qa": previous_qa})
        return self._questions

    async def refine_goal(self, qa_list, agent_id="hermes", user_context=None):
        self.refine_calls.append(qa_list)
        if self._refine_results:
            return self._refine_results.pop(0)
        return self._refine_result


class _MockTaskDecomposer:
    """模拟 TaskDecomposer——decompose 写 plan_steps（用真实 repo）"""
    def __init__(self, db, nodes=None):
        self._db = db
        self._nodes = nodes
        self.decompose_calls: list[tuple] = []

    async def decompose(self, task_id, confirmed_goal, agent_id="hermes", standards_context=None):
        self.decompose_calls.append((task_id, confirmed_goal, agent_id, standards_context))
        if self._nodes is None:
            # 默认：单节点 root + 2 个子节点
            from repositories.plan_step_repo import create_plan_node
            await create_plan_node(self._db, {
                "task_id": task_id, "level": 0, "title": confirmed_goal[:80],
                "description": "root", "acceptance_criteria": ["done"],
            })
            await create_plan_node(self._db, {
                "task_id": task_id, "parent_id": "n0", "level": 1,
                "title": "步骤1", "description": "做A", "acceptance_criteria": ["A完成"],
            })
            await create_plan_node(self._db, {
                "task_id": task_id, "parent_id": "n0", "level": 1,
                "title": "步骤2", "description": "做B", "acceptance_criteria": ["B完成"],
            })
        return {"nodes": [], "root_count": 1, "total": 3, "source": "mock"}


class _MockWsManager:
    """模拟 WsManager——收集 safe_send 消息"""
    def __init__(self):
        self.sent: list[dict] = []

    async def safe_send(self, task_id: str, payload: dict) -> None:
        payload = dict(payload)
        payload["_task_id"] = task_id
        self.sent.append(payload)


def _make_orchestrator(db, agent_service=None, clarifier=None, decomposer=None, ws_manager=None, analysis_agent=None, standards_lookup_agent=None):
    """构造 LongTaskOrchestrator——默认用 mock 组件

    Task 6.10: analysis_agent 默认 None（向后兼容——跳过 analyzing phase）。
    传 mock analysis_agent 时启用 analyzing phase 集成测试。
    Task 6.13: standards_lookup_agent 默认 None（向后兼容——跳过 decompose 前置查询）。
    """
    from services.long_task.orchestrator import LongTaskOrchestrator
    agent_service = agent_service or _MockAgentService(db)
    clarifier = clarifier or _MockGoalClarifier()
    decomposer = decomposer or _MockTaskDecomposer(db)
    ws_manager = ws_manager or _MockWsManager()
    return LongTaskOrchestrator(
        agent_service=agent_service,
        goal_clarifier=clarifier,
        task_decomposer=decomposer,
        conn=db,
        ws_manager=ws_manager,
        analysis_agent=analysis_agent,
        standards_lookup_agent=standards_lookup_agent,
    ), agent_service, clarifier, decomposer, ws_manager


# ═══════════════════════════════════════════════════════════════
# TestStateStore
# ═══════════════════════════════════════════════════════════════

class TestStateStore:
    """_LongTaskStateStore —— 读写 task_state.long_task JSON"""

    async def test_get_state_empty_when_no_task(self, db):
        from services.long_task.orchestrator import _LongTaskStateStore
        store = _LongTaskStateStore(db)
        assert await store.get_state("nonexistent") == {}

    async def test_get_state_empty_when_no_long_task_key(self, db):
        from services.long_task.orchestrator import _LongTaskStateStore
        await _create_task(db, "t1")
        store = _LongTaskStateStore(db)
        assert await store.get_state("t1") == {}

    async def test_set_and_get_state(self, db):
        from services.long_task.orchestrator import _LongTaskStateStore
        await _create_task(db, "t1")
        store = _LongTaskStateStore(db)
        await store.set_state("t1", phase="clarifying", original_prompt="hello")
        state = await store.get_state("t1")
        assert state["phase"] == "clarifying"
        assert state["original_prompt"] == "hello"

    async def test_set_state_merges_not_overwrites(self, db):
        from services.long_task.orchestrator import _LongTaskStateStore
        await _create_task(db, "t1")
        store = _LongTaskStateStore(db)
        await store.set_state("t1", phase="clarifying", original_prompt="hello")
        await store.set_state("t1", phase="executing")  # 只更新 phase
        state = await store.get_state("t1")
        assert state["phase"] == "executing"
        assert state["original_prompt"] == "hello"  # 保留

    async def test_set_state_preserves_other_keys(self, db):
        """set_state 不应冲掉 task_state 中的其他 key（如 snapshot 写的）"""
        from services.long_task.orchestrator import _LongTaskStateStore
        await _create_task(db, "t1")
        # 模拟其他系统写入 task_state
        await db.execute(
            "UPDATE tasks SET task_state=? WHERE id=?",
            (json.dumps({"status": "running", "agent_id": "hermes"}), "t1"),
        )
        await db.commit()
        store = _LongTaskStateStore(db)
        await store.set_state("t1", phase="clarifying")
        # 验证 long_task 子节点写入
        state = await store.get_state("t1")
        assert state["phase"] == "clarifying"
        # 验证其他 key 保留
        cur = await db.execute("SELECT task_state FROM tasks WHERE id=?", ("t1",))
        row = await cur.fetchone()
        full = json.loads(row[0])
        assert full["status"] == "running"
        assert full["agent_id"] == "hermes"
        assert full["long_task"]["phase"] == "clarifying"

    async def test_is_long_task(self, db):
        from services.long_task.orchestrator import _LongTaskStateStore
        await _create_task(db, "t1")
        store = _LongTaskStateStore(db)
        assert await store.is_long_task("t1") is False
        await store.set_state("t1", phase="clarifying")
        assert await store.is_long_task("t1") is True


# ═══════════════════════════════════════════════════════════════
# TestStart
# ═══════════════════════════════════════════════════════════════

class TestStart:
    """LongTaskOrchestrator.start"""

    async def test_start_persists_clarifying_phase(self, db):
        await _create_task(db, "t1", prompt="build a tool")
        orch, _, clarifier, _, ws = _make_orchestrator(db)
        await orch.start("t1", "build a tool")
        state = await orch._store.get_state("t1")
        assert state["phase"] == "clarifying"
        assert state["original_prompt"] == "build a tool"
        assert state["agent_id"] == DEFAULT_AGENT_ID
        assert "started_at" in state

    async def test_start_calls_generate_questions(self, db):
        await _create_task(db, "t1", prompt="build a tool")
        orch, _, clarifier, _, _ = _make_orchestrator(db)
        await orch.start("t1", "build a tool")
        assert len(clarifier.generate_calls) == 1
        assert clarifier.generate_calls[0]["task_input"] == "build a tool"

    async def test_start_pushes_goal_clarification_request(self, db):
        await _create_task(db, "t1", prompt="build a tool")
        orch, _, _, _, ws = _make_orchestrator(db)
        await orch.start("t1", "build a tool")
        # Phase 86: start() 现在也推 task_lifecycle.phase_start，按 type 过滤目标消息
        goal_msgs = [m for m in ws.sent if m.get("type") == "goal_clarification_request"]
        assert len(goal_msgs) == 1
        msg = goal_msgs[0]
        assert msg["task_id"] == "t1"
        assert msg["stage"] == "initial"
        assert "questions" in msg


# ═══════════════════════════════════════════════════════════════
# TestHandleClarificationResponse
# ═══════════════════════════════════════════════════════════════

class TestHandleClarificationResponse:
    """LongTaskOrchestrator.handle_clarification_response"""

    async def test_need_more_pushes_followup(self, db):
        await _create_task(db, "t1", prompt="build a tool")
        orch, _, clarifier, _, ws = _make_orchestrator(db)
        await orch.start("t1", "build a tool")
        ws.sent.clear()
        clarifier.generate_calls.clear()

        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=True,
        )
        # 应再调 generate_questions（带 previous_qa）
        assert len(clarifier.generate_calls) == 1
        assert clarifier.generate_calls[0]["previous_qa"] == [{"question": "Q1", "answer": "A1"}]
        # 应推 followup 请求
        assert len(ws.sent) == 1
        assert ws.sent[0]["type"] == "goal_clarification_request"
        assert ws.sent[0]["stage"] == "followup"

    async def test_default_refine_decompose_execute(self, db):
        """need_more=False → refine → 持久化 → decompose → execute 全流程"""
        await _create_task(db, "t1", prompt="build a tool")
        # spawn_subagent 两次都成功
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "step1 done"},
            {"ok": True, "task_id": "sub-2", "output": "step2 done"},
        ])
        orch, _, clarifier, decomposer, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch.start("t1", "build a tool")

        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )
        # refine 被调用
        assert len(clarifier.refine_calls) == 1
        # decompose 被调用
        assert len(decomposer.decompose_calls) == 1
        # 状态流转到 completed
        state = await orch._store.get_state("t1")
        assert state["phase"] == "completed"
        assert state["confirmed_goal"] == "完成测试目标"
        # spawn_subagent 被调用 2 次（跳过 root，执行 2 个子节点）
        assert len(agent_svc._spawn_calls) == 2
        # 推了 goal_confirmed + 2*step_start + 2*step_done + long_task_completed
        types = [m["type"] for m in ws.sent]
        assert "goal_confirmed" in types
        assert types.count("long_task_step_start") == 2
        assert types.count("long_task_step_done") == 2
        assert "long_task_completed" in types


# ═══════════════════════════════════════════════════════════════
# TestExecuteSteps
# ═══════════════════════════════════════════════════════════════

class TestExecuteSteps:
    """LongTaskOrchestrator._execute_steps"""

    async def _setup_tree(self, db, task_id: str):
        """创建 root + 2 个子节点的 plan_steps 树"""
        from repositories.plan_step_repo import create_plan_node
        await create_plan_node(db, {
            "task_id": task_id, "level": 0, "title": "root goal",
            "description": "root", "acceptance_criteria": ["done"],
        })
        await create_plan_node(db, {
            "task_id": task_id, "parent_id": "n0", "level": 1,
            "title": "步骤1", "description": "做A", "acceptance_criteria": ["A完成"],
        })
        await create_plan_node(db, {
            "task_id": task_id, "parent_id": "n0", "level": 1,
            "title": "步骤2", "description": "做B", "acceptance_criteria": ["B完成"],
        })

    async def test_all_success_marks_completed(self, db):
        await _create_task(db, "t1")
        await self._setup_tree(db, "t1")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "out1"},
            {"ok": True, "output": "out2"},
        ])
        orch, _, _, _, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing")
        await orch._execute_steps("t1", "hermes")
        state = await orch._store.get_state("t1")
        assert state["phase"] == "completed"
        types = [m["type"] for m in ws.sent]
        assert "long_task_completed" in types

    async def test_mid_failure_stops_and_marks_failed(self, db):
        await _create_task(db, "t1")
        await self._setup_tree(db, "t1")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "out1"},
            {"ok": False, "error": "step2 boom"},
        ])
        orch, _, _, _, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing")
        await orch._execute_steps("t1", "hermes")
        state = await orch._store.get_state("t1")
        assert state["phase"] == "failed"
        assert "step2 boom" in state.get("fail_reason", "")
        types = [m["type"] for m in ws.sent]
        assert "long_task_step_fail" in types
        assert "long_task_failed" in types
        assert "long_task_completed" not in types

    async def test_skips_root_when_has_children(self, db):
        """有子节点时 root(level 0) 不执行"""
        await _create_task(db, "t1")
        await self._setup_tree(db, "t1")  # root + 2 子节点
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "out1"},
            {"ok": True, "output": "out2"},
        ])
        orch, _, _, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing")
        await orch._execute_steps("t1", "hermes")
        # 只执行 2 次（跳过 root）
        assert len(agent_svc._spawn_calls) == 2

    async def test_executes_root_when_no_children(self, db):
        """无子节点（fallback 单节点）时执行 root"""
        from repositories.plan_step_repo import create_plan_node
        await _create_task(db, "t1")
        await create_plan_node(db, {
            "task_id": "t1", "level": 0, "title": "single goal",
            "description": "do it", "acceptance_criteria": ["done"],
        })
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "out1"},
        ])
        orch, _, _, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing")
        await orch._execute_steps("t1", "hermes")
        assert len(agent_svc._spawn_calls) == 1

    async def test_no_pending_marks_completed(self, db):
        """无 pending 节点直接标记 completed"""
        from repositories.plan_step_repo import create_plan_node
        await _create_task(db, "t1")
        await create_plan_node(db, {
            "task_id": "t1", "level": 0, "title": "root",
            "description": "done", "acceptance_criteria": [],
            "status": "completed",
        })
        agent_svc = _MockAgentService(db)
        orch, _, _, _, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing")
        await orch._execute_steps("t1", "hermes")
        state = await orch._store.get_state("t1")
        assert state["phase"] == "completed"
        assert len(agent_svc._spawn_calls) == 0


# ═══════════════════════════════════════════════════════════════
# Task 6.15: 截断溯源指针
# ═══════════════════════════════════════════════════════════════

class TestBuildContextSummaryTruncation:
    """Task 6.15: _build_context_summary 截断时附溯源指针"""

    def test_short_output_no_pointer(self, db):
        """短输出不截断、不加指针"""
        orch, _, _, _, _ = _make_orchestrator(db)
        summary = orch._build_context_summary([("n0", "short output")])
        assert summary == "[前序步骤输出摘要]\n步骤 1: short output"
        assert "see node" not in summary

    def test_long_output_has_pointer_with_step_id(self, db):
        """超长输出截断 + 附 `... [see node {step_id} for full content]`"""
        orch, _, _, _, _ = _make_orchestrator(db)
        long_output = "x" * 2000  # 远超 200-800 动态预算
        summary = orch._build_context_summary([("step-abc", long_output)])
        assert "see node step-abc for full content" in summary
        # 截断内容应远小于完整 2000 字符
        assert len(summary) < len(long_output)

    def test_multiple_steps_each_gets_own_pointer(self, db):
        """多步骤各自携带自己的 step_id 溯源指针"""
        orch, _, _, _, _ = _make_orchestrator(db)
        long_out = "y" * 2000
        summary = orch._build_context_summary([
            ("s1", long_out), ("s2", long_out),
        ])
        assert "see node s1 for full content" in summary
        assert "see node s2 for full content" in summary

    def test_empty_prev_outputs(self, db):
        orch, _, _, _, _ = _make_orchestrator(db)
        assert orch._build_context_summary([]) == ""


class TestExecuteStepsOutputPreviewPointer:
    """Task 6.15: _execute_steps 的 task_lifecycle.output_preview 截断附指针"""

    async def test_long_output_preview_has_pointer(self, db):
        """长 output 的 output_preview 附溯源指针指向 plan_steps 节点"""
        await _create_task(db, "t1")
        from repositories.plan_step_repo import create_plan_node
        # 单节点（无子节点 fallback 执行 root），显式指定 id 便于断言溯源指针
        await create_plan_node(db, {
            "task_id": "t1", "id": "n0", "level": 0, "title": "single",
            "description": "do", "acceptance_criteria": ["done"],
        })
        long_output = "A" * 2000  # 远超 LOG_PREVIEW_CHARS
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": long_output},
        ])
        orch, _, _, _, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing")
        await orch._execute_steps("t1", "hermes")
        # 找到 task_lifecycle step_done 事件
        previews = [
            m for m in ws.sent
            if m.get("type") == "task_lifecycle" and m.get("subtype") == "step_done"
        ]
        assert len(previews) == 1
        assert "see node n0 for full content" in previews[0]["output_preview"]


# ═══════════════════════════════════════════════════════════════
# TestReplan
# ═══════════════════════════════════════════════════════════════

class TestReplan:
    """LongTaskOrchestrator.replan"""

    async def test_replan_keeps_completed_deletes_pending(self, db):
        from repositories.plan_step_repo import create_plan_node, get_plan_tree_by_task
        await _create_task(db, "t1")
        # 创建 1 completed + 1 pending 节点
        await create_plan_node(db, {
            "task_id": "t1", "level": 1, "title": "已完成步骤",
            "description": "done", "acceptance_criteria": [], "status": "completed",
        })
        await create_plan_node(db, {
            "task_id": "t1", "level": 1, "title": "待执行步骤",
            "description": "todo", "acceptance_criteria": [], "status": "pending",
        })
        # decomposer mock 会创建新节点
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "new1"},
            {"ok": True, "output": "new2"},
        ])
        orch, _, _, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="old goal")

        await orch.replan("t1", "new goal")
        # pending 节点被删，completed 保留
        tree = await get_plan_tree_by_task(db, "t1")
        titles = [n["title"] for n in tree]
        assert "已完成步骤" in titles  # completed 保留
        assert "待执行步骤" not in titles  # pending 删除
        # 新节点由 decomposer mock 创建
        assert "步骤1" in titles or "new goal"[:80] in titles
        # confirmed_goal 更新
        state = await orch._store.get_state("t1")
        assert state["confirmed_goal"] == "new goal"
        assert state["phase"] == "completed"  # 执行完 2 个新子节点

    async def test_replan_updates_confirmed_goal(self, db):
        await _create_task(db, "t1")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "out"},
        ])
        # decomposer 创建单节点（无子节点 → 执行 root）
        decomposer = _MockTaskDecomposer(db, nodes="single")
        # 自定义 single 节点 decompose
        async def _single_decompose(task_id, confirmed_goal, agent_id="hermes"):
            from repositories.plan_step_repo import create_plan_node
            await create_plan_node(db, {
                "task_id": task_id, "level": 0, "title": confirmed_goal[:80],
                "description": "single", "acceptance_criteria": ["done"],
            })
            return {"nodes": [], "root_count": 1, "total": 1, "source": "mock"}
        decomposer.decompose = _single_decompose

        orch, _, _, _, _ = _make_orchestrator(db, agent_service=agent_svc, decomposer=decomposer)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="old")

        await orch.replan("t1", "全新目标")
        state = await orch._store.get_state("t1")
        assert state["confirmed_goal"] == "全新目标"

    async def test_replan_deletes_running_nodes(self, db):
        """M4: replan 应同时清理 running 节点（崩溃残留），否则依赖 running
        的下游节点永远不 ready，replan 后仍卡死。
        """
        from repositories.plan_step_repo import create_plan_node, get_plan_tree_by_task
        await _create_task(db, "t1")
        # 1 completed + 1 running（崩溃残留）+ 1 pending（依赖 running）
        await create_plan_node(db, {
            "task_id": "t1", "level": 1, "title": "已完成步骤",
            "description": "done", "acceptance_criteria": [], "status": "completed",
        })
        await create_plan_node(db, {
            "task_id": "t1", "level": 1, "title": "崩溃残留步骤",
            "description": "crashed", "acceptance_criteria": [], "status": "running",
        })
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "new1"},
        ])
        orch, _, _, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="old goal")

        await orch.replan("t1", "new goal")
        # running 节点应被删除（M4 修复）
        tree = await get_plan_tree_by_task(db, "t1")
        titles = [n["title"] for n in tree]
        assert "已完成步骤" in titles  # completed 保留
        assert "崩溃残留步骤" not in titles  # running 删除（M4 修复）


# ═══════════════════════════════════════════════════════════════
# TestFinalReviewIntegration —— Phase 83 终审接入 orchestrator
# ═══════════════════════════════════════════════════════════════

class _MockFinalReviewAgent:
    """模拟 FinalReviewAgent——按 stage + 调用顺序返回可控结果

    pre_result/post_result 支持三种形式：
    - None: 模拟单例缺失，review 返回 None（best-effort 不阻塞）
    - dict: 每次调用都返回该 dict
    - list[dict]: 按调用顺序索引返回（超出长度则重复最后一个，便于 step 差异化）
    """

    def __init__(self, pre_result=None, post_result=None):
        self._pre = pre_result
        self._post = post_result
        self.pre_calls: list[tuple] = []
        self.post_calls: list[tuple] = []

    @staticmethod
    def _pick(val, idx: int):
        if val is None:
            return None
        if isinstance(val, list):
            return val[idx] if idx < len(val) else val[-1]
        return val

    async def review(self, task_id, stage, context, step_id=None, force=False):
        if stage == "pre_execution":
            self.pre_calls.append((task_id, step_id, context))
            return self._pick(self._pre, len(self.pre_calls) - 1)
        if stage == "post_execution":
            self.post_calls.append((task_id, step_id, context))
            return self._pick(self._post, len(self.post_calls) - 1)
        return {"pass": True, "score": 80, "action": "proceed", "issues": [],
                "reason": "mock", "stage": stage, "task_id": task_id, "ts": 0}


class TestFinalReviewIntegration:
    """Phase 83: orchestrator 接入 pre/post_execution 终审"""

    async def _setup_tree(self, db, task_id: str):
        """root + 2 个子节点"""
        from repositories.plan_step_repo import create_plan_node
        await create_plan_node(db, {
            "task_id": task_id, "level": 0, "title": "root goal",
            "description": "root", "acceptance_criteria": ["done"],
        })
        await create_plan_node(db, {
            "task_id": task_id, "parent_id": "n0", "level": 1,
            "title": "步骤1", "description": "做A", "acceptance_criteria": ["A完成"],
        })
        await create_plan_node(db, {
            "task_id": task_id, "parent_id": "n0", "level": 1,
            "title": "步骤2", "description": "做B", "acceptance_criteria": ["B完成"],
        })

    def _inject_mock_agent(self, mock_agent):
        """注入 mock FinalReviewAgent 单例，返回原始单例供恢复"""
        from services.long_task.final_review_agent import (
            get_final_review_agent, set_final_review_agent,
        )
        original = get_final_review_agent()
        set_final_review_agent(mock_agent)
        return original

    def _restore_agent(self, original):
        from services.long_task.final_review_agent import set_final_review_agent
        set_final_review_agent(original)

    async def test_pre_execution_halt_skips_step(self, db):
        """pre_execution 返回 halt——该步标记 skipped，继续下一步"""
        await _create_task(db, "t1")
        await self._setup_tree(db, "t1")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "out2"},  # 步骤1 被 skip，只有步骤2 执行
        ])
        orch, _, _, _, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="G")

        halt_review = {
            "pass": False, "score": 30, "action": "halt",
            "issues": ["目标偏离"], "reason": "偏离",
            "stage": "pre_execution", "task_id": "t1", "ts": 0,
        }
        proceed_review = {
            "pass": True, "score": 90, "action": "proceed", "issues": [],
            "reason": "ok", "stage": "pre_execution", "task_id": "t1", "ts": 0,
        }
        # step1 halt（跳过），step2 proceed（正常执行）
        mock = _MockFinalReviewAgent(
            pre_result=[halt_review, proceed_review],
            post_result={
                "pass": True, "score": 90, "action": "proceed", "issues": [],
                "reason": "ok", "stage": "post_execution", "task_id": "t1", "ts": 0,
            },
        )
        original = self._inject_mock_agent(mock)
        try:
            await orch._execute_steps("t1", "hermes")
        finally:
            self._restore_agent(original)

        # 步骤1 被 skip（spawn 未调用），步骤2 正常执行
        assert len(agent_svc._spawn_calls) == 1
        # 任务最终 completed（skip 不 fail 整个任务）
        state = await orch._store.get_state("t1")
        assert state["phase"] == "completed"
        # 步骤1 状态为 skipped
        from repositories.plan_step_repo import get_plan_tree_by_task
        tree = await get_plan_tree_by_task(db, "t1")
        statuses = {n["title"]: n["status"] for n in tree}
        assert statuses["步骤1"] == "skipped"
        assert statuses["步骤2"] == "completed"

    async def test_pre_execution_proceed_executes_normally(self, db):
        """pre_execution 返回 proceed——正常执行"""
        await _create_task(db, "t1")
        await self._setup_tree(db, "t1")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "out1"},
            {"ok": True, "output": "out2"},
        ])
        orch, _, _, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="G")

        mock = _MockFinalReviewAgent(
            pre_result={"pass": True, "score": 85, "action": "proceed",
                        "issues": [], "reason": "ok",
                        "stage": "pre_execution", "task_id": "t1", "ts": 0},
            post_result={"pass": True, "score": 90, "action": "proceed",
                         "issues": [], "reason": "ok",
                         "stage": "post_execution", "task_id": "t1", "ts": 0},
        )
        original = self._inject_mock_agent(mock)
        try:
            await orch._execute_steps("t1", "hermes")
        finally:
            self._restore_agent(original)

        # 两步都执行
        assert len(agent_svc._spawn_calls) == 2
        assert len(mock.pre_calls) == 2
        assert len(mock.post_calls) == 2
        state = await orch._store.get_state("t1")
        assert state["phase"] == "completed"

    async def test_post_execution_writes_review_to_db(self, db):
        """post_execution 结果写入 plan_steps.review.final_reviews"""
        await _create_task(db, "t1")
        await self._setup_tree(db, "t1")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "out1"},
            {"ok": True, "output": "out2"},
        ])
        orch, _, _, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="G")

        post_review = {
            "pass": False, "score": 60, "action": "ask_user",
            "issues": ["需确认"], "reason": "产出有疑问",
            "stage": "post_execution", "task_id": "t1", "ts": 12345,
        }
        mock = _MockFinalReviewAgent(
            pre_result={"pass": True, "score": 85, "action": "proceed", "issues": [],
                        "reason": "ok", "stage": "pre_execution", "task_id": "t1", "ts": 0},
            post_result=post_review,
        )
        original = self._inject_mock_agent(mock)
        try:
            await orch._execute_steps("t1", "hermes")
        finally:
            self._restore_agent(original)

        # 读 plan_steps.review 验证 final_reviews 写入
        from repositories.plan_step_repo import get_plan_tree_by_task
        tree = await get_plan_tree_by_task(db, "t1")
        for node in tree:
            if node["title"] == "步骤1":
                # get_plan_tree_by_task 已自动反序列化 review 为 dict
                review = node["review"]
                assert isinstance(review, dict)
                assert "final_reviews" in review
                assert len(review["final_reviews"]) == 1
                fr = review["final_reviews"][0]
                assert fr["stage"] == "post_execution"
                assert fr["action"] == "ask_user"
                assert fr["score"] == 60
                assert fr["reason"] == "产出有疑问"
                assert fr["issues"] == ["需确认"]
                break
        else:
            pytest.fail("未找到步骤1 节点")

    async def test_final_review_agent_missing_no_block(self, db):
        """FinalReviewAgent 单例缺失（get 返回 None）——不阻塞，正常执行"""
        await _create_task(db, "t1")
        await self._setup_tree(db, "t1")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "out1"},
            {"ok": True, "output": "out2"},
        ])
        orch, _, _, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="G")

        # 注入 None 单例（模拟未初始化）
        original = self._inject_mock_agent(None)
        try:
            await orch._execute_steps("t1", "hermes")
        finally:
            self._restore_agent(original)

        # 正常执行，未被阻塞
        assert len(agent_svc._spawn_calls) == 2
        state = await orch._store.get_state("t1")
        assert state["phase"] == "completed"


# ═══════════════════════════════════════════════════════════════
# Task 6.10: Analyzing Phase 集成测试
# ═══════════════════════════════════════════════════════════════

class _MockAnalysisAgent:
    """模拟 AnalysisAgent——可配置 analyze 返回值，记录调用"""

    def __init__(self, report=None, exc: Exception | None = None):
        self._report = report or {
            "complexity": {"level": "moderate", "summary": "测试", "factors": []},
            "dependencies": {"external": [], "internal": [], "blockers": []},
            "risks": {"items": [], "summary": "无"},
            "resources": {"tech_stack": [], "tools": [], "skills": []},
            "acceptance": {"criteria": ["完成"], "test_points": []},
        }
        self._exc = exc
        self.analyze_calls: list[tuple[str, str, str]] = []

    async def analyze(self, confirmed_goal: str, original_prompt: str, agent_id: str = "hermes") -> dict:
        self.analyze_calls.append((confirmed_goal, original_prompt, agent_id))
        if self._exc is not None:
            raise self._exc
        return self._report


class TestAnalysisPhase:
    """Task 6.10: LongTaskOrchestrator 的 analyzing phase 集成"""

    async def test_analysis_skipped_when_agent_none(self, db):
        """analysis_agent=None → 跳过 analyzing，直接 decomposing（向后兼容）

        验证：不推 analysis_started/analysis_report，decompose 正常调用
        """
        await _create_task(db, "t1", prompt="build a tool")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "done"},
            {"ok": True, "task_id": "sub-2", "output": "done"},
        ])
        # analysis_agent=None（默认）
        orch, _, _, _, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch.start("t1", "build a tool")

        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )

        types = [m["type"] for m in ws.sent]
        # 不应推 analysis 相关事件
        assert "analysis_started" not in types
        assert "analysis_report" not in types
        # 但 decompose 和 execute 正常进行
        assert "goal_confirmed" in types
        assert "long_task_completed" in types

    async def test_analysis_runs_when_agent_provided(self, db):
        """analysis_agent 已注入 → 推 analysis_started + analysis_report"""
        await _create_task(db, "t1", prompt="build a tool")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "done"},
            {"ok": True, "task_id": "sub-2", "output": "done"},
        ])
        analysis_agent = _MockAnalysisAgent()
        orch, _, _, _, ws = _make_orchestrator(
            db, agent_service=agent_svc, analysis_agent=analysis_agent,
        )
        await orch.start("t1", "build a tool")

        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )

        types = [m["type"] for m in ws.sent]
        # 应推 analysis_started 和 analysis_report
        assert "analysis_started" in types
        assert "analysis_report" in types
        # analysis_agent.analyze 被调用一次
        assert len(analysis_agent.analyze_calls) == 1
        # 传入的 confirmed_goal 和 original_prompt 正确
        confirmed_goal, original_prompt, _ = analysis_agent.analyze_calls[0]
        assert confirmed_goal == "完成测试目标"
        assert original_prompt == "build a tool"
        # 后续 decompose + execute 正常进行
        assert "long_task_completed" in types

    async def test_analysis_report_persisted_to_state(self, db):
        """分析报告持久化到 task_state.long_task.analysis_report"""
        await _create_task(db, "t1", prompt="build a tool")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "done"},
            {"ok": True, "task_id": "sub-2", "output": "done"},
        ])
        custom_report = {
            "complexity": {"level": "complex", "summary": "高复杂度", "factors": ["f1"]},
            "dependencies": {"external": ["ext"], "internal": [], "blockers": []},
            "risks": {"items": [], "summary": "无"},
            "resources": {"tech_stack": [], "tools": [], "skills": []},
            "acceptance": {"criteria": ["done"], "test_points": []},
        }
        analysis_agent = _MockAnalysisAgent(report=custom_report)
        orch, _, _, _, _ = _make_orchestrator(
            db, agent_service=agent_svc, analysis_agent=analysis_agent,
        )
        await orch.start("t1", "build a tool")

        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )

        state = await orch._store.get_state("t1")
        # analysis_report 持久化
        assert "analysis_report" in state
        assert state["analysis_report"]["complexity"]["level"] == "complex"
        assert state["analysis_report"]["complexity"]["summary"] == "高复杂度"

    async def test_analysis_failure_does_not_block_decompose(self, db):
        """analysis_agent.analyze 抛异常 → best-effort 跳过，decompose 仍执行"""
        await _create_task(db, "t1", prompt="build a tool")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "done"},
            {"ok": True, "task_id": "sub-2", "output": "done"},
        ])
        # analyze 抛异常
        analysis_agent = _MockAnalysisAgent(exc=RuntimeError("LLM 挂了"))
        orch, _, clarifier, decomposer, ws = _make_orchestrator(
            db, agent_service=agent_svc, analysis_agent=analysis_agent,
        )
        await orch.start("t1", "build a tool")

        # 不应抛异常
        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )

        types = [m["type"] for m in ws.sent]
        # analysis_started 推了（开始分析）
        assert "analysis_started" in types
        # analysis_report 未推（失败跳过）
        assert "analysis_report" not in types
        # 但 decompose 和 execute 正常进行（best-effort）
        assert len(clarifier.refine_calls) == 1
        assert len(decomposer.decompose_calls) == 1
        assert "long_task_completed" in types

    async def test_analysis_phase_state_transition(self, db):
        """状态流转：clarifying → analyzing → decomposing → executing → completed

        验证 analyzing phase 在 phase_start 事件流中出现。
        """
        await _create_task(db, "t1", prompt="build a tool")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "done"},
            {"ok": True, "task_id": "sub-2", "output": "done"},
        ])
        analysis_agent = _MockAnalysisAgent()
        orch, _, _, _, ws = _make_orchestrator(
            db, agent_service=agent_svc, analysis_agent=analysis_agent,
        )
        await orch.start("t1", "build a tool")

        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )

        # task_lifecycle.phase_start 事件按顺序应包含 analyzing
        phase_starts = [
            m.get("phase") for m in ws.sent
            if m.get("type") == "task_lifecycle" and m.get("subtype") == "phase_start"
        ]
        # analyzing 出现在 phase_start 流中
        assert "analyzing" in phase_starts
        # 顺序：clarifying → analyzing → decomposing → executing
        try:
            i_clarify = phase_starts.index("clarifying")
            i_analyze = phase_starts.index("analyzing")
            i_decompose = phase_starts.index("decomposing")
            assert i_clarify < i_analyze < i_decompose
        except ValueError:
            pytest.fail(f"phase_start 序列缺失某阶段: {phase_starts}")


# ═══════════════════════════════════════════════════════════════
# Task 6.11: Auto Followup（低 confidence 自动追问）
# ═══════════════════════════════════════════════════════════════

class TestAutoFollowup:
    """Task 6.11: orchestrator 检测低 confidence 自动追问（双向讨论）"""

    async def test_low_confidence_triggers_auto_followup(self, db):
        """refine_goal 返回 needs_more_clarification=True → 自动追问，不进 decompose"""
        await _create_task(db, "t1", prompt="build a tool")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "done"},
            {"ok": True, "task_id": "sub-2", "output": "done"},
        ])
        # refine_goal 返回低 confidence + needs_more_clarification=True
        clarifier = _MockGoalClarifier(refine_result={
            "confirmed_goal": "模糊目标",
            "grading_levels": ["能运行"],
            "assumptions": [],
            "confidence": 0.3,
            "needs_more_clarification": True,
            "clarification_reason": "验收标准不明确",
        })
        orch, _, _, decomposer, ws = _make_orchestrator(
            db, agent_service=agent_svc, clarifier=clarifier,
        )
        await orch.start("t1", "build a tool")

        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )

        types = [m["type"] for m in ws.sent]
        # 应推 auto_followup 请求（而非 goal_confirmed）
        assert "goal_clarification_request" in types
        # 精确定位 auto_followup 消息（start() 也推了 initial 请求）
        followup_msgs = [
            m for m in ws.sent
            if m["type"] == "goal_clarification_request"
            and m.get("stage") == "auto_followup"
        ]
        assert len(followup_msgs) == 1
        assert "验收标准" in followup_msgs[0].get("reason", "")
        # 不应进 decompose（未确认目标）
        assert len(decomposer.decompose_calls) == 0
        # 不应推 goal_confirmed
        assert "goal_confirmed" not in types

    async def test_high_confidence_proceeds_to_decompose(self, db):
        """refine_goal 返回 confidence=0.9 → 直接进 decompose（无追问）"""
        await _create_task(db, "t1", prompt="build a tool")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "done"},
            {"ok": True, "task_id": "sub-2", "output": "done"},
        ])
        clarifier = _MockGoalClarifier(refine_result={
            "confirmed_goal": "清晰目标",
            "grading_levels": ["能运行", "有测试"],
            "assumptions": [],
            "confidence": 0.9,
            "needs_more_clarification": False,
            "clarification_reason": "",
        })
        orch, _, _, decomposer, ws = _make_orchestrator(
            db, agent_service=agent_svc, clarifier=clarifier,
        )
        await orch.start("t1", "build a tool")

        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )

        types = [m["type"] for m in ws.sent]
        # 应推 goal_confirmed（含 confidence）
        assert "goal_confirmed" in types
        confirmed_msg = next(m for m in ws.sent if m["type"] == "goal_confirmed")
        assert confirmed_msg["confidence"] == 0.9
        # 应进 decompose
        assert len(decomposer.decompose_calls) == 1
        # 不应推 auto_followup
        followups = [m for m in ws.sent if m.get("stage") == "auto_followup"]
        assert len(followups) == 0

    async def test_auto_followup_no_max_rounds(self, db):
        """连续低 confidence → 持续 auto_followup（Task 6.11 无上限，watchdog 兜底）"""
        await _create_task(db, "t1", prompt="build a tool")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "done"},
            {"ok": True, "task_id": "sub-2", "output": "done"},
        ])
        # 3 次都返回低 confidence
        low_conf_result = {
            "confirmed_goal": "模糊目标",
            "grading_levels": ["能运行"],
            "assumptions": [],
            "confidence": 0.3,
            "needs_more_clarification": True,
            "clarification_reason": "仍不明确",
        }
        clarifier = _MockGoalClarifier(refine_result=low_conf_result)
        orch, _, _, decomposer, ws = _make_orchestrator(
            db, agent_service=agent_svc, clarifier=clarifier,
        )
        await orch.start("t1", "build a tool")

        # 第 1 轮：低 confidence → auto_followup
        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )
        assert len([m for m in ws.sent if m.get("stage") == "auto_followup"]) == 1
        assert len(decomposer.decompose_calls) == 0

        # 第 2 轮：仍低 confidence → auto_followup
        ws.sent.clear()
        await orch.handle_clarification_response(
            "t1", [{"question": "Q2", "answer": "A2"}], need_more=False,
        )
        assert len([m for m in ws.sent if m.get("stage") == "auto_followup"]) == 1
        assert len(decomposer.decompose_calls) == 0

        # 第 3 轮：仍低 confidence → 继续 auto_followup（不设上限）
        ws.sent.clear()
        await orch.handle_clarification_response(
            "t1", [{"question": "Q3", "answer": "A3"}], need_more=False,
        )
        assert len([m for m in ws.sent if m.get("stage") == "auto_followup"]) == 1
        assert "goal_confirmed" not in [m["type"] for m in ws.sent]
        assert len(decomposer.decompose_calls) == 0

    async def test_auto_followup_increments_counter(self, db):
        """auto_followup_count 持久化到 task_state.long_task"""
        await _create_task(db, "t1", prompt="build a tool")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "done"},
            {"ok": True, "task_id": "sub-2", "output": "done"},
        ])
        low_conf_result = {
            "confirmed_goal": "模糊目标",
            "grading_levels": ["能运行"],
            "assumptions": [],
            "confidence": 0.3,
            "needs_more_clarification": True,
            "clarification_reason": "不明确",
        }
        clarifier = _MockGoalClarifier(refine_result=low_conf_result)
        orch, _, _, _, _ = _make_orchestrator(
            db, agent_service=agent_svc, clarifier=clarifier,
        )
        await orch.start("t1", "build a tool")

        # 第 1 轮
        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )
        state = await orch._store.get_state("t1")
        assert state.get("auto_followup_count") == 1

        # 第 2 轮
        await orch.handle_clarification_response(
            "t1", [{"question": "Q2", "answer": "A2"}], need_more=False,
        )
        state = await orch._store.get_state("t1")
        assert state.get("auto_followup_count") == 2

    async def test_user_confidence_carried_in_goal_confirmed(self, db):
        """goal_confirmed WS 事件携带 confidence 字段"""
        await _create_task(db, "t1", prompt="build a tool")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "done"},
            {"ok": True, "task_id": "sub-2", "output": "done"},
        ])
        clarifier = _MockGoalClarifier(refine_result={
            "confirmed_goal": "目标",
            "grading_levels": ["能运行", "有测试"],
            "assumptions": [],
            "confidence": 0.75,
            "needs_more_clarification": False,
            "clarification_reason": "",
        })
        orch, _, _, _, ws = _make_orchestrator(
            db, agent_service=agent_svc, clarifier=clarifier,
        )
        await orch.start("t1", "build a tool")

        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )

        confirmed_msgs = [m for m in ws.sent if m["type"] == "goal_confirmed"]
        assert len(confirmed_msgs) == 1
        assert confirmed_msgs[0]["confidence"] == 0.75


# ═══════════════════════════════════════════════════════════════
# Task 6.13: standards_lookup 前置（decompose 前查最新文档）
# ═══════════════════════════════════════════════════════════════

class _MockStandardsLookupAgent:
    """模拟 StandardsLookupAgent——可配置 lookup 返回值，记录调用

    对标 _MockAnalysisAgent 模式：可注入 exc 模拟失败，best-effort 测试用。
    """

    def __init__(self, result=None, exc: Exception | None = None):
        self._result = result or {
            "ok": True,
            "sources": ["https://docs.example.com/latest"],
            "summary": "最新 API 推荐用法：使用 v2 端点",
            "recommendations": [
                "使用 fetch() 替代 XMLHttpRequest",
                "AsyncBoundary 需包裹 Suspense",
            ],
            "source_type": "context7",
            "ts": 1700000000,
        }
        self._exc = exc
        self.lookup_calls: list[tuple[str, str]] = []  # (problem_description, domain)

    async def lookup(self, problem_description: str, domain: str = "general", force: bool = False) -> dict:
        self.lookup_calls.append((problem_description, domain))
        if self._exc is not None:
            raise self._exc
        return self._result


class TestStandardsLookupBeforeDecompose:
    """Task 6.13: decompose 前置查询最新文档——best-effort 集成测试

    设计对标 TestAnalysisPhase：
    - agent 未注入 → 跳过（向后兼容）
    - agent 注入 + lookup 成功 → standards_context 传给 decompose
    - agent 注入 + lookup 抛异常 → best-effort 跳过，decompose 仍执行（standards_context=None）
    """

    async def test_standards_lookup_passes_context_to_decompose(self, db):
        """agent 注入 + lookup 成功 → decompose 收到含 summary + recommendations 的 standards_context"""
        await _create_task(db, "t1", prompt="build a tool")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "done"},
            {"ok": True, "task_id": "sub-2", "output": "done"},
        ])
        standards_agent = _MockStandardsLookupAgent()
        orch, _, _, decomposer, ws = _make_orchestrator(
            db, agent_service=agent_svc, standards_lookup_agent=standards_agent,
        )
        await orch.start("t1", "build a tool")

        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )

        # lookup 被调用一次，入参是 confirmed_goal
        assert len(standards_agent.lookup_calls) == 1
        problem_desc, _domain = standards_agent.lookup_calls[0]
        assert "完成测试目标" in problem_desc
        # decompose 收到非空 standards_context（含 summary + recommendations 内容）
        assert len(decomposer.decompose_calls) == 1
        _tid, _goal, _aid, standards_ctx = decomposer.decompose_calls[0]
        assert standards_ctx is not None
        assert "最新 API 推荐用法" in standards_ctx  # summary 内容
        assert "使用 fetch() 替代 XMLHttpRequest" in standards_ctx  # recommendation 内容
        # 推了 standards_lookup_started + standards_lookup_report
        types = [m["type"] for m in ws.sent]
        assert "standards_lookup_started" in types
        assert "standards_lookup_report" in types

    async def test_standards_lookup_failure_best_effort(self, db):
        """lookup 抛异常 → best-effort 跳过，decompose 仍执行（standards_context=None）"""
        await _create_task(db, "t1", prompt="build a tool")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "done"},
            {"ok": True, "task_id": "sub-2", "output": "done"},
        ])
        standards_agent = _MockStandardsLookupAgent(exc=RuntimeError("Context7 超时"))
        orch, _, _, decomposer, ws = _make_orchestrator(
            db, agent_service=agent_svc, standards_lookup_agent=standards_agent,
        )
        await orch.start("t1", "build a tool")

        # 不应抛异常
        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )

        # lookup 被调用但失败
        assert len(standards_agent.lookup_calls) == 1
        # decompose 仍执行，standards_context=None（best-effort）
        assert len(decomposer.decompose_calls) == 1
        _tid, _goal, _aid, standards_ctx = decomposer.decompose_calls[0]
        assert standards_ctx is None
        # 推了 standards_lookup_started（开始查询）但未推 standards_lookup_report（失败跳过）
        types = [m["type"] for m in ws.sent]
        assert "standards_lookup_started" in types
        assert "standards_lookup_report" not in types
        # 主流程正常完成
        assert "long_task_completed" in types

    async def test_standards_lookup_skipped_when_agent_none(self, db):
        """agent 未注入 → 跳过 lookup，decompose 正常（standards_context=None，向后兼容）"""
        await _create_task(db, "t1", prompt="build a tool")
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "task_id": "sub-1", "output": "done"},
            {"ok": True, "task_id": "sub-2", "output": "done"},
        ])
        # standards_lookup_agent=None（默认）
        orch, _, _, decomposer, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch.start("t1", "build a tool")

        await orch.handle_clarification_response(
            "t1", [{"question": "Q1", "answer": "A1"}], need_more=False,
        )

        # 未推 standards_lookup 相关事件
        types = [m["type"] for m in ws.sent]
        assert "standards_lookup_started" not in types
        assert "standards_lookup_report" not in types
        # decompose 正常执行，standards_context=None
        assert len(decomposer.decompose_calls) == 1
        _tid, _goal, _aid, standards_ctx = decomposer.decompose_calls[0]
        assert standards_ctx is None
        # 主流程正常完成
        assert "long_task_completed" in types

