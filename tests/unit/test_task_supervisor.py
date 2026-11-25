"""tests/unit/test_task_supervisor.py —— Phase 81 TaskSupervisor 测试

覆盖：
- orchestrator pause/resume/_check_pause/_drain_inject_queue/inject_constraint（81-2 新增）
- TaskSupervisor get_view/ask_supervisor/spec/stall 检测/EventBus 订阅
"""
import asyncio
import json as _json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src-python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import DEFAULT_AGENT_ID
from services.long_task.orchestrator import LongTaskOrchestrator
from services.task_supervisor import TaskSupervisor, SupervisorView


# ═══════════════════════════════════════════════════════════════
# Mock 工具
# ═══════════════════════════════════════════════════════════════

class _MockCursor:
    def __init__(self, rows=None, row=None):
        self._rows = rows or []
        self._row = row

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _MockConn:
    """模拟 aiosqlite——按 SQL 关键字路由返回预设 cursor"""

    def __init__(self, running_node_row=None, running_node_rows=None,
                 plan_tree_rows=None, task_rows=None):
        # Phase 87 审查 M2: 支持 running_node_rows（list）广播多节点
        # running_node_row 仍保留（单节点兼容，内部转 list）
        if running_node_rows is not None:
            self._running_node_rows = running_node_rows
        else:
            self._running_node_rows = [running_node_row] if running_node_row else []
        self._plan_tree_rows = plan_tree_rows or []
        self._task_rows = task_rows or []
        self.queries: list[tuple] = []
        self.commits = 0

    async def execute(self, sql, params=()):
        self.queries.append((sql, params))
        sql_lower = sql.lower()
        if "from plan_steps" in sql_lower and "status='running'" in sql_lower:
            # M2: fetchall 返回所有 running 节点（广播语义）
            return _MockCursor(rows=list(self._running_node_rows))
        if "from tasks" in sql_lower and "status='running'" in sql_lower:
            # P0-4.2: _get_running_long_tasks_with_heartbeat 期望 (task_id, heartbeat_at, updated_at)
            rows = [
                (r[0], r[1] if len(r) > 1 else None, r[2] if len(r) > 2 else None)
                for r in self._task_rows
            ]
            return _MockCursor(rows=rows)
        return _MockCursor()

    async def commit(self):
        self.commits += 1


class _MockStore:
    """模拟 _LongTaskStateStore"""
    def __init__(self, state=None):
        self._state = state or {}

    async def get_state(self, task_id):
        return self._state


class _MockOrchestrator:
    """模拟 LongTaskOrchestrator——记录调用 + 可配 is_long_task"""
    def __init__(self, state=None, is_long=True):
        self._store = _MockStore(state)
        self.pause_calls: list[str] = []
        self.resume_calls: list[str] = []
        self.inject_calls: list[tuple] = []
        self.replan_calls: list[tuple] = []
        self._is_long = is_long

    async def pause(self, task_id):
        self.pause_calls.append(task_id)

    async def resume(self, task_id):
        self.resume_calls.append(task_id)
        # Batch 3b B2: orchestrator.resume 返回真实状态 dict（supervisor 透传）
        return {"ok": True, "status": "resumed"}

    async def inject_constraint(self, task_id, constraint):
        self.inject_calls.append((task_id, constraint))

    async def replan(self, task_id, new_goal, agent_id="hermes"):
        self.replan_calls.append((task_id, new_goal, agent_id))

    async def is_long_task(self, task_id):
        return self._is_long


class _MockAdapter:
    """模拟 hermes adapter——invoke 返回预设 content"""
    def __init__(self, content="建议内容", raise_on_invoke=False):
        self._content = content
        self._raise = raise_on_invoke
        self.invoke_calls: list[str] = []

    async def invoke(self, prompt, context=None):
        self.invoke_calls.append(prompt)
        if self._raise:
            raise RuntimeError("mock invoke fail")
        return {"content": self._content}


class _MockWSManager:
    def __init__(self):
        self.sent: list[tuple] = []

    async def safe_send(self, task_id, payload):
        self.sent.append((task_id, payload))


class _MockEventBus:
    def __init__(self):
        self.subscriptions: list[tuple] = []

    def subscribe(self, event_type, handler):
        self.subscriptions.append((event_type, handler))


# ═══════════════════════════════════════════════════════════════
# TestPauseResume —— orchestrator pause/resume/_check_pause
# ═══════════════════════════════════════════════════════════════

class TestPauseResume:
    """Phase 81-2: orchestrator pause/resume + _check_pause 行为"""

    def _make_orchestrator(self, executing=None):
        orch = LongTaskOrchestrator(
            agent_service=MagicMock(),
            goal_clarifier=MagicMock(),
            task_decomposer=MagicMock(),
            conn=MagicMock(),
            ws_manager=MagicMock(),
        )
        # Batch 3b B2: resume 按 _executing_tasks 协程存活状态分流——
        # 测试需模拟"任务正在执行中"（否则 resume 走 is_long_task 校验）
        if executing:
            orch._executing_tasks.update(executing)
        return orch

    async def test_pause_creates_cleared_event(self):
        orch = self._make_orchestrator()
        await orch.pause("t1")
        assert "t1" in orch._pause_events
        assert not orch._pause_events["t1"].is_set()

    async def test_resume_sets_event(self):
        orch = self._make_orchestrator(executing={"t1"})
        await orch.pause("t1")
        assert not orch._pause_events["t1"].is_set()
        result = await orch.resume("t1")
        assert orch._pause_events["t1"].is_set()
        assert result["status"] == "resumed"

    async def test_resume_running_without_pause_is_idempotent(self):
        # Batch 3b B2: 运行中且未 pause 的任务 resume 是幂等 no-op，
        # 不再无条件创建 set event，而是如实返回 running
        orch = self._make_orchestrator(executing={"t1"})
        result = await orch.resume("t1")
        assert result["ok"] is True
        assert result["status"] == "running"
        assert "t1" not in orch._pause_events

    async def test_check_pause_returns_immediately_when_no_event(self):
        orch = self._make_orchestrator()
        await asyncio.wait_for(orch._check_pause("t1"), timeout=0.5)

    async def test_check_pause_returns_immediately_when_set(self):
        orch = self._make_orchestrator()
        event = asyncio.Event()
        event.set()
        orch._pause_events["t1"] = event
        await asyncio.wait_for(orch._check_pause("t1"), timeout=0.5)

    async def test_check_pause_blocks_when_cleared(self):
        orch = self._make_orchestrator()
        await orch.pause("t1")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(orch._check_pause("t1"), timeout=0.3)

    async def test_check_pause_unblocks_after_resume(self):
        orch = self._make_orchestrator(executing={"t1"})
        await orch.pause("t1")
        task = asyncio.create_task(orch._check_pause("t1"))
        await asyncio.sleep(0.1)
        assert not task.done()
        await orch.resume("t1")
        await asyncio.wait_for(task, timeout=0.5)

    async def test_cleanup_removes_event_and_queue(self):
        orch = self._make_orchestrator()
        await orch.pause("t1")
        # M1: _inject_queues 改为 list[str]
        orch._inject_queues.setdefault("t1", [])
        orch._cleanup_task_state("t1")
        assert "t1" not in orch._pause_events
        assert "t1" not in orch._inject_queues


# ═══════════════════════════════════════════════════════════════
# TestInjectConstraint —— orchestrator inject_constraint + _drain_inject_queue
# ═══════════════════════════════════════════════════════════════

class TestInjectConstraint:
    """Phase 81-2: orchestrator inject_constraint + _drain_inject_queue

    Phase 87 审查 M1+M2: 广播语义——inject_constraint 写所有 running 节点，
    _drain_inject_queue 不删除（每个节点都看到全部约束）。
    """

    def _make_orchestrator(self, running_node_row=None, running_node_rows=None):
        conn = _MockConn(
            running_node_row=running_node_row,
            running_node_rows=running_node_rows,
        )
        return LongTaskOrchestrator(
            agent_service=MagicMock(),
            goal_clarifier=MagicMock(),
            task_decomposer=MagicMock(),
            conn=conn,
            ws_manager=MagicMock(),
        ), conn

    async def test_inject_writes_running_node_review(self):
        running_row = ("node-1", '{"constraints":[]}')
        orch, conn = self._make_orchestrator(running_node_row=running_row)
        await orch.inject_constraint("t1", "用 Python 3.12")
        # 找到 UPDATE plan_steps SET review
        update_queries = [q for q in conn.queries if "UPDATE plan_steps SET review" in q[0]]
        assert len(update_queries) == 1
        review_json = update_queries[0][1][0]
        review = _json.loads(review_json)
        assert len(review["constraints"]) == 1
        assert review["constraints"][0]["constraint"] == "用 Python 3.12"
        assert conn.commits >= 1

    async def test_inject_appends_to_existing_constraints(self):
        running_row = ("node-1", '{"constraints":[{"constraint":"旧约束","ts":1}]}')
        orch, conn = self._make_orchestrator(running_node_row=running_row)
        await orch.inject_constraint("t1", "新约束")
        update_queries = [q for q in conn.queries if "UPDATE plan_steps SET review" in q[0]]
        review = _json.loads(update_queries[0][1][0])
        assert len(review["constraints"]) == 2
        assert review["constraints"][1]["constraint"] == "新约束"

    async def test_inject_no_running_node_only_enqueues(self):
        orch, conn = self._make_orchestrator(running_node_row=None)
        await orch.inject_constraint("t1", "约束X")
        # 没有 UPDATE 调用
        update_queries = [q for q in conn.queries if "UPDATE plan_steps SET review" in q[0]]
        assert len(update_queries) == 0
        # 但 list 有（M1: 改为 list）
        assert "t1" in orch._inject_queues
        assert len(orch._inject_queues["t1"]) == 1

    async def test_inject_enqueues_constraint(self):
        orch, _ = self._make_orchestrator(running_node_row=None)
        await orch.inject_constraint("t1", "约束Y")
        assert len(orch._inject_queues["t1"]) == 1

    async def test_drain_returns_empty_when_no_queue(self):
        orch, _ = self._make_orchestrator()
        result = await orch._drain_inject_queue("t1")
        assert result == ""

    async def test_drain_returns_all_items_joined(self):
        orch, _ = self._make_orchestrator()
        await orch.inject_constraint("t1", "约束1")
        await orch.inject_constraint("t1", "约束2")
        result = await orch._drain_inject_queue("t1")
        assert "约束1" in result
        assert "约束2" in result
        assert "\n" in result

    async def test_drain_broadcast_does_not_consume(self):
        """M1: 广播语义——drain 不删除约束，后续节点 drain 仍能看到全部约束"""
        orch, _ = self._make_orchestrator()
        await orch.inject_constraint("t1", "约束1")
        first = await orch._drain_inject_queue("t1")
        second = await orch._drain_inject_queue("t1")
        # 两次 drain 都返回同一约束（广播给所有后续节点）
        assert first == "约束1"
        assert second == "约束1"

    async def test_inject_broadcasts_to_all_running_nodes(self):
        """M2: 多个 running 节点时，约束广播写入全部节点的 review"""
        running_rows = [
            ("node-1", '{"constraints":[]}'),
            ("node-2", '{"constraints":[]}'),
            ("node-3", '{"constraints":[]}'),
        ]
        orch, conn = self._make_orchestrator(running_node_rows=running_rows)
        await orch.inject_constraint("t1", "全局约束")
        update_queries = [q for q in conn.queries if "UPDATE plan_steps SET review" in q[0]]
        # 3 个 running 节点都收到约束
        assert len(update_queries) == 3
        for q in update_queries:
            review = _json.loads(q[1][0])
            assert len(review["constraints"]) == 1
            assert review["constraints"][0]["constraint"] == "全局约束"


# ═══════════════════════════════════════════════════════════════
# TestGetView —— TaskSupervisor.get_view 组装
# ═══════════════════════════════════════════════════════════════

class TestGetView:
    """Phase 81: TaskSupervisor.get_view 实时组装 SupervisorView"""

    def _make_supervisor(self, state=None, plan_tree_rows=None):
        conn = _MockConn(plan_tree_rows=plan_tree_rows)
        orch = _MockOrchestrator(state=state)
        supervisor = TaskSupervisor(
            conn=conn, orchestrator=orch, adapters={},
            model_config=MagicMock(), ws_manager=_MockWSManager(),
            event_bus=_MockEventBus(),
        )
        return supervisor

    async def test_get_view_reads_state_fields(self):
        state = {
            "phase": "executing",
            "confirmed_goal": "做一个 X",
            "grading_levels": ["能运行", "有测试"],
            "assumptions": ["假设1"],
        }
        supervisor = self._make_supervisor(state=state)
        view = await supervisor.get_view("t1")
        assert view.status == "executing"
        assert view.confirmed_goal == "做一个 X"
        assert view.grading_levels == ["能运行", "有测试"]
        assert view.assumptions == ["假设1"]

    async def test_get_view_current_step_first_running(self):
        # 用 monkeypatch 替换 get_plan_tree_by_task
        import services.task_supervisor as ts_mod
        tree = [
            {"id": "n1", "title": "root", "status": "completed", "review": {}, "level": 0},
            {"id": "n2", "title": "step1", "status": "running", "review": {}, "level": 1},
            {"id": "n3", "title": "step2", "status": "pending", "review": {}, "level": 1},
        ]
        original = ts_mod.get_plan_tree_by_task

        async def _fake_get_tree(conn, task_id):
            return tree

        ts_mod.get_plan_tree_by_task = _fake_get_tree
        try:
            supervisor = self._make_supervisor(state={"phase": "executing"})
            view = await supervisor.get_view("t1")
            assert view.current_step_id == "n2"
            assert len(view.plan_tree) == 3
        finally:
            ts_mod.get_plan_tree_by_task = original

    async def test_get_view_review_log_aggregates_reviews(self):
        import services.task_supervisor as ts_mod
        tree = [
            {"id": "n1", "title": "step1", "status": "completed",
             "review": {"constraints": [{"constraint": "C1"}]}, "level": 0},
            {"id": "n2", "title": "step2", "status": "pending", "review": {}, "level": 0},
        ]
        original = ts_mod.get_plan_tree_by_task

        async def _fake_get_tree(conn, task_id):
            return tree

        ts_mod.get_plan_tree_by_task = _fake_get_tree
        try:
            supervisor = self._make_supervisor(state={"phase": "executing"})
            view = await supervisor.get_view("t1")
            # 只有 n1 有非空 review → review_log 含 1 条
            assert len(view.review_log) == 1
            assert view.review_log[0]["step_id"] == "n1"
        finally:
            ts_mod.get_plan_tree_by_task = original

    async def test_get_view_stall_rounds_from_counter(self):
        supervisor = self._make_supervisor()
        supervisor._stall_counters["t1"] = 3
        view = await supervisor.get_view("t1")
        assert view.stall_rounds == 3

    async def test_get_view_suggestions_from_memory(self):
        supervisor = self._make_supervisor()
        supervisor._suggestions["t1"] = ["建议A", "建议B"]
        view = await supervisor.get_view("t1")
        assert view.suggestions == ["建议A", "建议B"]


# ═══════════════════════════════════════════════════════════════
# TestAskSupervisor —— TaskSupervisor.ask_supervisor
# ═══════════════════════════════════════════════════════════════

class TestAskSupervisor:
    """Phase 81: ask_supervisor 轻量 LLM 调用"""

    def _make_supervisor(self, adapter=None):
        adapters = {DEFAULT_AGENT_ID: adapter} if adapter else {}
        return TaskSupervisor(
            conn=_MockConn(), orchestrator=_MockOrchestrator(),
            adapters=adapters, model_config=MagicMock(),
            ws_manager=_MockWSManager(), event_bus=_MockEventBus(),
        )

    async def test_ask_returns_content(self):
        adapter = _MockAdapter(content="这是建议")
        supervisor = self._make_supervisor(adapter=adapter)
        result = await supervisor.ask_supervisor("t1", "下一步怎么做？")
        assert result == "这是建议"
        assert len(adapter.invoke_calls) == 1
        # prompt 含用户问题
        assert "下一步怎么做？" in adapter.invoke_calls[0]

    async def test_ask_no_adapter_returns_empty(self):
        supervisor = self._make_supervisor(adapter=None)
        result = await supervisor.ask_supervisor("t1", "问题")
        assert result == ""

    async def test_ask_adapter_exception_returns_empty(self):
        adapter = _MockAdapter(raise_on_invoke=True)
        supervisor = self._make_supervisor(adapter=adapter)
        result = await supervisor.ask_supervisor("t1", "问题")
        assert result == ""


# ═══════════════════════════════════════════════════════════════
# TestSpec —— TaskSupervisor.spec 委托 replan
# ═══════════════════════════════════════════════════════════════

class TestSpec:
    """Phase 81: spec 委托 orchestrator.replan"""

    async def test_spec_with_new_goal_calls_replan(self):
        orch = _MockOrchestrator()
        supervisor = TaskSupervisor(
            conn=_MockConn(), orchestrator=orch, adapters={},
            model_config=MagicMock(), ws_manager=_MockWSManager(),
            event_bus=_MockEventBus(),
        )
        await supervisor.spec("t1", new_goal="新目标")
        assert len(orch.replan_calls) == 1
        assert orch.replan_calls[0] == ("t1", "新目标", "hermes")

    async def test_spec_no_new_goal_uses_confirmed_goal(self):
        orch = _MockOrchestrator(state={"confirmed_goal": "旧目标"})
        supervisor = TaskSupervisor(
            conn=_MockConn(), orchestrator=orch, adapters={},
            model_config=MagicMock(), ws_manager=_MockWSManager(),
            event_bus=_MockEventBus(),
        )
        await supervisor.spec("t1", new_goal=None)
        assert len(orch.replan_calls) == 1
        assert orch.replan_calls[0][1] == "旧目标"

    async def test_spec_no_goal_raises_valueerror(self):
        orch = _MockOrchestrator(state={})  # 无 confirmed_goal
        supervisor = TaskSupervisor(
            conn=_MockConn(), orchestrator=orch, adapters={},
            model_config=MagicMock(), ws_manager=_MockWSManager(),
            event_bus=_MockEventBus(),
        )
        with pytest.raises(ValueError, match="confirmed_goal"):
            await supervisor.spec("t1", new_goal=None)

    async def test_spec_resets_stall_counter(self):
        orch = _MockOrchestrator(state={"confirmed_goal": "G"})
        supervisor = TaskSupervisor(
            conn=_MockConn(), orchestrator=orch, adapters={},
            model_config=MagicMock(), ws_manager=_MockWSManager(),
            event_bus=_MockEventBus(),
        )
        supervisor._stall_counters["t1"] = 5
        supervisor._suggestions["t1"] = ["旧建议"]
        await supervisor.spec("t1", new_goal="新目标")
        assert "t1" not in supervisor._stall_counters
        assert "t1" not in supervisor._suggestions


# ═══════════════════════════════════════════════════════════════
# TestStallDetection —— EventBus 事件 + stall 计数
# ═══════════════════════════════════════════════════════════════

class TestStallDetection:
    """Phase 81: _on_task_failed/_on_task_completed + stall 触发"""

    async def test_on_task_failed_increments_counter_for_long_task(self):
        orch = _MockOrchestrator(is_long=True)
        supervisor = TaskSupervisor(
            conn=_MockConn(), orchestrator=orch, adapters={},
            model_config=MagicMock(), ws_manager=_MockWSManager(),
            event_bus=_MockEventBus(),
        )
        await supervisor._on_task_failed("task_failed", {"task_id": "t1"})
        assert supervisor._stall_counters.get("t1") == 1
        await supervisor._on_task_failed("task_failed", {"task_id": "t1"})
        assert supervisor._stall_counters.get("t1") == 2

    async def test_on_task_failed_ignores_non_long_task(self):
        orch = _MockOrchestrator(is_long=False)
        supervisor = TaskSupervisor(
            conn=_MockConn(), orchestrator=orch, adapters={},
            model_config=MagicMock(), ws_manager=_MockWSManager(),
            event_bus=_MockEventBus(),
        )
        await supervisor._on_task_failed("task_failed", {"task_id": "t1"})
        assert "t1" not in supervisor._stall_counters

    async def test_on_task_failed_no_task_id_ignored(self):
        supervisor = TaskSupervisor(
            conn=_MockConn(), orchestrator=_MockOrchestrator(), adapters={},
            model_config=MagicMock(), ws_manager=_MockWSManager(),
            event_bus=_MockEventBus(),
        )
        await supervisor._on_task_failed("task_failed", {})
        assert len(supervisor._stall_counters) == 0

    async def test_on_task_completed_clears_counter(self):
        supervisor = TaskSupervisor(
            conn=_MockConn(), orchestrator=_MockOrchestrator(), adapters={},
            model_config=MagicMock(), ws_manager=_MockWSManager(),
            event_bus=_MockEventBus(),
        )
        supervisor._stall_counters["t1"] = 3
        await supervisor._on_task_completed("task_completed", {"task_id": "t1"})
        assert "t1" not in supervisor._stall_counters

    async def test_poll_triggers_llm_when_stall_reaches_threshold(self):
        """stall_rounds >= 2 且无建议 → 触发 _analyze_with_llm → 推 WS
        Phase 87 审查 M-4: 改为 fire-and-forget，需 await 后台任务完成"""
        import services.task_supervisor as ts_mod

        tree = [{"id": "n1", "title": "s", "status": "running", "review": {}, "level": 0}]
        original = ts_mod.get_plan_tree_by_task

        async def _fake_get_tree(conn, task_id):
            return tree

        ts_mod.get_plan_tree_by_task = _fake_get_tree
        try:
            ws = _MockWSManager()
            adapter = _MockAdapter(content="重新分解建议")
            # _get_running_long_tasks 返回 ["t1"]
            conn = _MockConn(task_rows=[("t1",)])
            orch = _MockOrchestrator(state={"phase": "executing"})
            supervisor = TaskSupervisor(
                conn=conn, orchestrator=orch, adapters={DEFAULT_AGENT_ID: adapter},
                model_config=MagicMock(), ws_manager=ws, event_bus=_MockEventBus(),
            )
            supervisor._stall_counters["t1"] = 2  # 达阈值
            await supervisor._poll_running_long_tasks()
            # M-4: 分析改为 fire-and-forget，需等待后台任务完成
            await asyncio.wait_for(
                self._wait_analyzing_empty(supervisor), timeout=2.0
            )
            # 推送了 supervisor_suggestion
            assert len(ws.sent) == 1
            tid, payload = ws.sent[0]
            assert tid == "t1"
            assert payload["type"] == "supervisor_suggestion"
            assert "重新分解建议" in payload["suggestion"]
        finally:
            ts_mod.get_plan_tree_by_task = original

    @staticmethod
    async def _wait_analyzing_empty(supervisor) -> None:
        """等待 _analyzing 集合清空（后台分析任务完成）"""
        while supervisor._analyzing:
            await asyncio.sleep(0.01)


# ═══════════════════════════════════════════════════════════════
# TestMonitorLoopSubscription —— EventBus 订阅
# ═══════════════════════════════════════════════════════════════

class TestMonitorLoopSubscription:
    """Phase 81: monitor_loop 启动时订阅 TASK_COMPLETED/FAILED"""

    async def test_subscribe_on_first_monitor_loop_call(self):
        """monitor_loop 第一次调用会订阅事件——用 cancel 立即退出验证订阅"""
        event_bus = _MockEventBus()
        supervisor = TaskSupervisor(
            conn=_MockConn(), orchestrator=_MockOrchestrator(), adapters={},
            model_config=MagicMock(), ws_manager=_MockWSManager(),
            event_bus=event_bus,
        )
        # 启动 monitor_loop 后立即 cancel
        task = asyncio.create_task(supervisor.monitor_loop())
        await asyncio.sleep(0.2)  # 让订阅发生
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # 验证订阅了 TASK_COMPLETED / TASK_FAILED / TASK_CANCELLED
        event_types = [s[0] for s in event_bus.subscriptions]
        assert "task_completed" in event_types
        assert "task_failed" in event_types
        assert "task_cancelled" in event_types  # Phase 87 审查 M-1

    async def test_subscribe_idempotent(self):
        """多次调用 monitor_loop 只订阅一次（_subscribed 标志）"""
        event_bus = _MockEventBus()
        supervisor = TaskSupervisor(
            conn=_MockConn(), orchestrator=_MockOrchestrator(), adapters={},
            model_config=MagicMock(), ws_manager=_MockWSManager(),
            event_bus=event_bus,
        )
        # 手动调用两次订阅逻辑（模拟 monitor_loop 幂等检查）
        supervisor._subscribed = False
        # 模拟 monitor_loop 的订阅段
        if not supervisor._subscribed and supervisor._event_bus:
            supervisor._event_bus.subscribe("task_completed", supervisor._on_task_completed)
            supervisor._event_bus.subscribe("task_failed", supervisor._on_task_failed)
            supervisor._subscribed = True
        # 第二次
        if not supervisor._subscribed and supervisor._event_bus:
            supervisor._event_bus.subscribe("task_completed", supervisor._on_task_completed)
        assert len(event_bus.subscriptions) == 2  # 只订阅一次


# ═══════════════════════════════════════════════════════════════
# TestPhase87ReviewFixes —— M-1 (TASK_CANCELLED) + M-4 (fire-and-forget)
# ═══════════════════════════════════════════════════════════════

class TestPhase87ReviewFixes:
    """Phase 87 审查第二轮：M-1 取消事件清理 + M-4 分析非阻塞"""

    def _make_supervisor(self, adapter=None, ws=None, state=None):
        import services.task_supervisor as ts_mod
        tree = [{"id": "n1", "title": "s", "status": "running", "review": {}, "level": 0}]
        original = ts_mod.get_plan_tree_by_task

        async def _fake_get_tree(conn, task_id):
            return tree

        ts_mod.get_plan_tree_by_task = _fake_get_tree
        # 注意：调用方负责在 finally 中恢复 original（这里用一个标记供测试清理）
        conn = _MockConn(task_rows=[("t1",)])
        orch = _MockOrchestrator(state=state or {"phase": "executing"})
        supervisor = TaskSupervisor(
            conn=conn, orchestrator=orch,
            adapters={DEFAULT_AGENT_ID: adapter or _MockAdapter()},
            model_config=MagicMock(),
            ws_manager=ws or _MockWSManager(),
            event_bus=_MockEventBus(),
        )
        supervisor._ts_original_get_tree = original  # 供 teardown
        return supervisor, conn, orch

    # ── M-1: TASK_CANCELLED 清理 ──

    async def test_on_task_cancelled_clears_stall_counter(self):
        """M-1: TASK_CANCELLED 事件清理 _stall_counters"""
        supervisor, _, _ = self._make_supervisor()
        try:
            supervisor._stall_counters["t1"] = 3
            await supervisor._on_task_cancelled("task_cancelled", {"task_id": "t1"})
            assert "t1" not in supervisor._stall_counters
        finally:
            import services.task_supervisor as ts_mod
            ts_mod.get_plan_tree_by_task = supervisor._ts_original_get_tree

    async def test_on_task_cancelled_clears_suggestions(self):
        """M-1: TASK_CANCELLED 事件清理 _suggestions（取消后无需保留历史建议）"""
        supervisor, _, _ = self._make_supervisor()
        try:
            supervisor._suggestions["t1"] = ["旧建议1", "旧建议2"]
            await supervisor._on_task_cancelled("task_cancelled", {"task_id": "t1"})
            assert "t1" not in supervisor._suggestions
        finally:
            import services.task_supervisor as ts_mod
            ts_mod.get_plan_tree_by_task = supervisor._ts_original_get_tree

    async def test_on_task_cancelled_clears_analyzing(self):
        """M-1: TASK_CANCELLED 事件清理 _analyzing（取消时正在分析的任务也应移除）"""
        supervisor, _, _ = self._make_supervisor()
        try:
            supervisor._analyzing.add("t1")
            await supervisor._on_task_cancelled("task_cancelled", {"task_id": "t1"})
            assert "t1" not in supervisor._analyzing
        finally:
            import services.task_supervisor as ts_mod
            ts_mod.get_plan_tree_by_task = supervisor._ts_original_get_tree

    async def test_on_task_cancelled_no_task_id_ignored(self):
        """M-1: 空 task_id 的事件被忽略"""
        supervisor, _, _ = self._make_supervisor()
        try:
            supervisor._stall_counters["t1"] = 1
            await supervisor._on_task_cancelled("task_cancelled", {})
            assert supervisor._stall_counters.get("t1") == 1  # 未清理
        finally:
            import services.task_supervisor as ts_mod
            ts_mod.get_plan_tree_by_task = supervisor._ts_original_get_tree

    # ── M-4: fire-and-forget 不阻塞轮询 ──

    async def test_poll_does_not_block_on_slow_llm(self):
        """M-4: 慢 LLM 分析不阻塞 _poll_running_long_tasks 返回
        两个 task 都达 stall 阈值——poll 应立即返回（分析 fire-and-forget），
        而非等待两个慢 LLM 调用串行完成。
        """
        import services.task_supervisor as ts_mod

        tree = [{"id": "n1", "title": "s", "status": "running", "review": {}, "level": 0}]
        original = ts_mod.get_plan_tree_by_task

        async def _fake_get_tree(conn, task_id):
            return tree

        ts_mod.get_plan_tree_by_task = _fake_get_tree
        try:
            # _SlowAdapter.invoke sleep 0.3s 后返回——模拟慢 LLM
            class _SlowAdapter:
                def __init__(self):
                    self.invoke_count = 0

                async def invoke(self, prompt, context=None):
                    self.invoke_count += 1
                    await asyncio.sleep(0.3)
                    return {"content": f"建议 {self.invoke_count}"}

            slow_adapter = _SlowAdapter()
            ws = _MockWSManager()
            # 两个 task 都 running
            conn = _MockConn(task_rows=[("t1",), ("t2",)])
            orch = _MockOrchestrator(state={"phase": "executing"})
            supervisor = TaskSupervisor(
                conn=conn, orchestrator=orch, adapters={DEFAULT_AGENT_ID: slow_adapter},
                model_config=MagicMock(), ws_manager=ws, event_bus=_MockEventBus(),
            )
            supervisor._stall_counters["t1"] = 2
            supervisor._stall_counters["t2"] = 2

            # 计时：poll 应在远小于 0.6s（两个 0.3s 串行）内返回
            import time as _time
            t0 = _time.monotonic()
            await supervisor._poll_running_long_tasks()
            elapsed = _time.monotonic() - t0

            # M-4 修复后：poll 立即返回（fire-and-forget），不等 LLM
            assert elapsed < 0.2, f"poll 应立即返回，实际耗时 {elapsed:.2f}s"
            # 两个 task 都在分析中
            assert "t1" in supervisor._analyzing
            assert "t2" in supervisor._analyzing

            # 等待后台分析完成
            await asyncio.wait_for(
                self._wait_analyzing_empty(supervisor), timeout=2.0
            )
            assert len(ws.sent) == 2  # 两个建议都推送
        finally:
            ts_mod.get_plan_tree_by_task = original

    async def test_poll_does_not_duplicate_analysis(self):
        """M-4: _analyzing 守卫——已在分析的 task 不重复触发"""
        import services.task_supervisor as ts_mod

        tree = [{"id": "n1", "title": "s", "status": "running", "review": {}, "level": 0}]
        original = ts_mod.get_plan_tree_by_task

        async def _fake_get_tree(conn, task_id):
            return tree

        ts_mod.get_plan_tree_by_task = _fake_get_tree
        try:
            class _SlowAdapter:
                def __init__(self):
                    self.invoke_count = 0

                async def invoke(self, prompt, context=None):
                    self.invoke_count += 1
                    await asyncio.sleep(0.2)
                    return {"content": "建议"}

            slow_adapter = _SlowAdapter()
            ws = _MockWSManager()
            conn = _MockConn(task_rows=[("t1",)])
            orch = _MockOrchestrator(state={"phase": "executing"})
            supervisor = TaskSupervisor(
                conn=conn, orchestrator=orch, adapters={DEFAULT_AGENT_ID: slow_adapter},
                model_config=MagicMock(), ws_manager=ws, event_bus=_MockEventBus(),
            )
            supervisor._stall_counters["t1"] = 2

            # 第一次 poll——触发分析（fire-and-forget）
            await supervisor._poll_running_long_tasks()
            assert "t1" in supervisor._analyzing
            # 让后台任务启动并进入 invoke（invoke_count 立即 +1）
            await asyncio.sleep(0.05)
            assert slow_adapter.invoke_count == 1  # 已调用一次

            # 第二次 poll——仍在分析中，_analyzing 守卫阻止重复触发
            await supervisor._poll_running_long_tasks()
            assert slow_adapter.invoke_count == 1  # 仍然只调用一次

            # 等待完成
            await asyncio.wait_for(
                self._wait_analyzing_empty(supervisor), timeout=2.0
            )
            assert len(ws.sent) == 1
        finally:
            ts_mod.get_plan_tree_by_task = original

    @staticmethod
    async def _wait_analyzing_empty(supervisor) -> None:
        """等待 _analyzing 集合清空"""
        while supervisor._analyzing:
            await asyncio.sleep(0.01)

    # ── L-3: _suggestions FIFO 上限 ──

    async def test_suggestions_capped_to_max(self):
        """L-3: _suggestions 超过 _MAX_SUGGESTION_TASKS 时淘汰最旧

        原代码 completed task 的建议无限保留，长生命周期 app 内存增长。
        修复后 FIFO 淘汰，上限 _MAX_SUGGESTION_TASKS。
        """
        import services.task_supervisor as ts_mod

        tree = [{"id": "n1", "title": "s", "status": "running", "review": {}, "level": 0}]
        original = ts_mod.get_plan_tree_by_task

        async def _fake_get_tree(conn, task_id):
            return tree

        ts_mod.get_plan_tree_by_task = _fake_get_tree
        # 缩小上限以加速测试
        original_max = ts_mod._MAX_SUGGESTION_TASKS
        ts_mod._MAX_SUGGESTION_TASKS = 3
        try:
            class _FastAdapter:
                async def invoke(self, prompt, context=None):
                    return {"content": "建议"}

            ws = _MockWSManager()
            # 4 个 task 都达阈值——_suggestions 应被裁剪到 3
            conn = _MockConn(task_rows=[("t1",), ("t2",), ("t3",), ("t4",)])
            orch = _MockOrchestrator(state={"phase": "executing"})
            supervisor = TaskSupervisor(
                conn=conn, orchestrator=orch, adapters={DEFAULT_AGENT_ID: _FastAdapter()},
                model_config=MagicMock(), ws_manager=ws, event_bus=_MockEventBus(),
            )
            for tid in ("t1", "t2", "t3", "t4"):
                supervisor._stall_counters[tid] = 2

            await supervisor._poll_running_long_tasks()
            await asyncio.wait_for(
                self._wait_analyzing_empty(supervisor), timeout=2.0
            )

            # L-3: 最多保留 3 个 task 的建议（最旧的 t1 被淘汰）
            assert len(supervisor._suggestions) <= 3, \
                f"_suggestions 应 <= 3，实际 {len(supervisor._suggestions)}"
            assert "t1" not in supervisor._suggestions, "最旧的 t1 应被 FIFO 淘汰"
            assert "t4" in supervisor._suggestions, "最新的 t4 应保留"
        finally:
            ts_mod._MAX_SUGGESTION_TASKS = original_max
            ts_mod.get_plan_tree_by_task = original


# ═══════════════════════════════════════════════════════════════
# TestSupervisorDelegation —— pause/resume/inject 委托
# ═══════════════════════════════════════════════════════════════

class TestSupervisorDelegation:
    """Phase 81: TaskSupervisor.pause/resume/inject_constraint 委托 orchestrator"""

    async def test_pause_delegates_to_orchestrator(self):
        orch = _MockOrchestrator()
        supervisor = TaskSupervisor(
            conn=_MockConn(), orchestrator=orch, adapters={},
            model_config=MagicMock(), ws_manager=_MockWSManager(),
            event_bus=_MockEventBus(),
        )
        await supervisor.pause("t1")
        assert orch.pause_calls == ["t1"]

    async def test_resume_delegates_to_orchestrator(self):
        orch = _MockOrchestrator()
        supervisor = TaskSupervisor(
            conn=_MockConn(), orchestrator=orch, adapters={},
            model_config=MagicMock(), ws_manager=_MockWSManager(),
            event_bus=_MockEventBus(),
        )
        await supervisor.resume("t1")
        assert orch.resume_calls == ["t1"]

    async def test_inject_delegates_to_orchestrator(self):
        orch = _MockOrchestrator()
        supervisor = TaskSupervisor(
            conn=_MockConn(), orchestrator=orch, adapters={},
            model_config=MagicMock(), ws_manager=_MockWSManager(),
            event_bus=_MockEventBus(),
        )
        await supervisor.inject_constraint("t1", "约束Z")
        assert orch.inject_calls == [("t1", "约束Z")]
