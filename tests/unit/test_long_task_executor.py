"""tests/unit/test_long_task_executor.py —— Phase 87 长任务执行器测试

覆盖 services/long_task/executor.py 的核心行为：
1. 拓扑排序——线性依赖链按序执行
2. 有限并行——独立节点并行执行
3. 并发上限——Semaphore(3) 限制最大并发
4. 可重试错误——网络超时自动重试 2 次后成功
5. 不可重试错误——编译错误直接标 failed（executor 返回 blocked）
6. blocked 传播——依赖失败的后继节点保持 pending
7. 依赖输出合并——后继节点的 context_summary 含前序 output
8. pre_execution halt——FinalReview 拦截后标 skipped

Mock 策略：复用 test_long_task_orchestrator.py 的 _MockAgentService / _MockWsManager
模式，新增 _TrackingAgentService（跟踪并发）/ _RetryAgentService（可控失败次数）/
_FailingAgentService（总是不可重试失败）。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
async def db(tmp_path):
    """aiosqlite + SCHEMA_SQL——每个测试独立 DB"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_executor.db")
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


# ── Mock 组件 ──

class _MockAgentService:
    """模拟 AgentService——spawn_subagent 按 spawn_results 顺序返回"""
    def __init__(self, db, spawn_results: list[dict] | None = None):
        self.db = db
        self._spawn_results = spawn_results or []
        self._spawn_calls: list[tuple[str, str, str]] = []

    async def spawn_subagent(self, agent_id: str, prompt: str, context_summary: str = "", auto_approve: bool = False, **kwargs) -> dict:
        self._spawn_calls.append((agent_id, prompt, context_summary))
        if self._spawn_results:
            return self._spawn_results.pop(0)
        return {"ok": True, "task_id": "sub-1", "output": "mock output"}


class _TrackingAgentService:
    """跟踪并发数和最大并发数的 mock——用 sleep 制造并发窗口"""
    def __init__(self, db, delay: float = 0.05):
        self.db = db
        self._delay = delay
        self._current = 0
        self._max_concurrent = 0
        self._lock = asyncio.Lock()
        self._spawn_calls: list[tuple[str, str, str]] = []

    async def spawn_subagent(self, agent_id: str, prompt: str, context_summary: str = "", auto_approve: bool = False, **kwargs) -> dict:
        async with self._lock:
            self._current += 1
            if self._current > self._max_concurrent:
                self._max_concurrent = self._current
        await asyncio.sleep(self._delay)  # 制造并发窗口
        async with self._lock:
            self._current -= 1
        self._spawn_calls.append((agent_id, prompt, context_summary))
        return {"ok": True, "output": "done"}


class _RetryAgentService:
    """可控失败次数的 mock——前 failure_count 次返回失败，之后成功"""
    def __init__(self, db, failure_count: int, error: str = "request timeout"):
        self.db = db
        self._failure_count = failure_count
        self._error = error
        self._call_count = 0
        self._spawn_calls: list[tuple[str, str, str]] = []

    async def spawn_subagent(self, agent_id: str, prompt: str, context_summary: str = "", auto_approve: bool = False, **kwargs) -> dict:
        self._call_count += 1
        self._spawn_calls.append((agent_id, prompt, context_summary))
        if self._call_count <= self._failure_count:
            return {"ok": False, "error": self._error}
        return {"ok": True, "output": "done after retry"}


class _FailingAgentService:
    """总是返回不可重试失败的 mock"""
    def __init__(self, db, error: str = "compilation error: syntax error"):
        self.db = db
        self._error = error
        self._spawn_calls: list[tuple[str, str, str]] = []

    async def spawn_subagent(self, agent_id: str, prompt: str, context_summary: str = "", auto_approve: bool = False, **kwargs) -> dict:
        self._spawn_calls.append((agent_id, prompt, context_summary))
        return {"ok": False, "error": self._error}


class _MockWsManager:
    """模拟 WsManager——收集 safe_send 消息"""
    def __init__(self):
        self.sent: list[dict] = []

    async def safe_send(self, task_id: str, payload: dict) -> None:
        payload = dict(payload)
        payload["_task_id"] = task_id
        self.sent.append(payload)


def _make_orchestrator(db, agent_service=None):
    """构造 LongTaskOrchestrator——默认用 mock 组件

    Returns:
        (orchestrator, agent_service, ws_manager)
    """
    from services.long_task.orchestrator import LongTaskOrchestrator

    agent_service = agent_service or _MockAgentService(db)
    # GoalClarifier / TaskDecomposer 在 executor 测试中不会被调用（直接调 execute_plan_tree）
    # 传 None 占位，orchestrator 不要求非空
    ws_manager = _MockWsManager()
    orch = LongTaskOrchestrator(
        agent_service=agent_service,
        goal_clarifier=None,  # executor 测试不走 clarifier
        task_decomposer=None,  # executor 测试不走 decomposer
        conn=db,
        ws_manager=ws_manager,
    )
    return orch, agent_service, ws_manager


async def _create_root_and_children(db, task_id: str, children: list[dict]) -> None:
    """创建 root（level 0）+ 多个 level 1 子节点

    Args:
        children: [{id, title, dependencies?: list}]
    """
    from repositories.plan_step_repo import create_plan_node
    await create_plan_node(db, {
        "task_id": task_id, "id": "root", "level": 0, "title": "root goal",
    })
    for child in children:
        node = {
            "task_id": task_id, "parent_id": "root", "level": 1,
            "title": child["title"],
        }
        if "id" in child:
            node["id"] = child["id"]
        if child.get("dependencies"):
            node["dependencies"] = child["dependencies"]
        await create_plan_node(db, node)


# ═══════════════════════════════════════════════════════════════
# TestExecutePlanTree
# ═══════════════════════════════════════════════════════════════

class TestExecutePlanTree:
    """execute_plan_tree —— 拓扑排序 + 有限并行 + 重试 + blocked 传播"""

    async def test_dependency_order_linear_chain(self, db):
        """A→B→C 线性依赖链，验证执行顺序是 A, B, C"""
        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": "A", "title": "步骤A"},
            {"id": "B", "title": "步骤B", "dependencies": ["A"]},
            {"id": "C", "title": "步骤C", "dependencies": ["B"]},
        ])

        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "A done"},
            {"ok": True, "output": "B done"},
            {"ok": True, "output": "C done"},
        ])
        orch, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        # 验证执行顺序——_build_step_prompt 第一行是 title
        prompts = [call[1] for call in agent_svc._spawn_calls]
        assert "步骤A" in prompts[0]
        assert "步骤B" in prompts[1]
        assert "步骤C" in prompts[2]
        # 3 个节点全部执行
        assert len(agent_svc._spawn_calls) == 3
        # 任务完成
        state = await orch._store.get_state("t1")
        assert state["phase"] == "completed"

    async def test_parallel_independent_nodes_all_executed(self, db):
        """3 个独立节点（无相互依赖），验证全部被执行"""
        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": "A", "title": "步骤A"},
            {"id": "B", "title": "步骤B"},
            {"id": "C", "title": "步骤C"},
        ])

        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "A done"},
            {"ok": True, "output": "B done"},
            {"ok": True, "output": "C done"},
        ])
        orch, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        # 3 个节点全部执行（顺序不重要，只要都执行了）
        assert len(agent_svc._spawn_calls) == 3
        executed_titles = {call[1].split("\n")[0] for call in agent_svc._spawn_calls}
        assert executed_titles == {"步骤A", "步骤B", "步骤C"}
        # 任务完成
        state = await orch._store.get_state("t1")
        assert state["phase"] == "completed"

    async def test_concurrency_limit_respected(self, db):
        """5 个独立节点 + Semaphore(3)，验证最大并发数 == 3（不超过上限）"""
        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": f"N{i}", "title": f"步骤{i}"} for i in range(5)
        ])

        agent_svc = _TrackingAgentService(db, delay=0.05)
        orch, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        from services.long_task.executor import execute_plan_tree, MAX_LONG_TASK_CONCURRENT_NODES
        await execute_plan_tree(orch, "t1", "hermes")

        # 最大并发数不超过上限
        assert agent_svc._max_concurrent <= MAX_LONG_TASK_CONCURRENT_NODES
        # 5 个节点足够达到上限（3 个并发 + 2 个等待）
        assert agent_svc._max_concurrent == MAX_LONG_TASK_CONCURRENT_NODES
        # 全部执行
        assert len(agent_svc._spawn_calls) == 5

    async def test_retriable_error_retries_then_succeeds(self, db, monkeypatch):
        """前 2 次 timeout，第 3 次成功——验证重试逻辑"""
        from services.long_task import executor as executor_mod
        monkeypatch.setattr(executor_mod, "_RETRY_BASE_DELAY", 0.01)  # 避免等待 2s+4s

        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": "A", "title": "步骤A"},
        ])

        agent_svc = _RetryAgentService(db, failure_count=2, error="request timeout")
        orch, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        # 验证 spawn 被调用 3 次（2 失败 + 1 成功）
        assert agent_svc._call_count == 3
        # 任务最终成功
        state = await orch._store.get_state("t1")
        assert state["phase"] == "completed"

    async def test_non_retriable_error_marks_failed(self, db, monkeypatch):
        """不可重试错误（编译错误）→ 节点 failed，任务 failed"""
        from services.long_task import executor as executor_mod
        monkeypatch.setattr(executor_mod, "_RETRY_BASE_DELAY", 0.01)

        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": "A", "title": "步骤A"},
        ])

        agent_svc = _FailingAgentService(db, error="compilation error: syntax error")
        orch, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        # 验证只调用 2 次（1 次步骤执行 + 1 次 blocked 监督者决策，均不可重试）
        assert len(agent_svc._spawn_calls) == 2
        # 节点状态 failed
        from repositories.plan_step_repo import get_plan_tree_by_task
        tree = await get_plan_tree_by_task(db, "t1")
        step = [n for n in tree if n["level"] > 0][0]
        assert step["status"] == "failed"
        # 任务状态 failed
        state = await orch._store.get_state("t1")
        assert state["phase"] == "failed"

    async def test_blocked_propagation_to_dependents(self, db, monkeypatch):
        """A failed（不可重试）→ B（依赖 A）保持 pending（不被执行）"""
        from services.long_task import executor as executor_mod
        monkeypatch.setattr(executor_mod, "_RETRY_BASE_DELAY", 0.01)

        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": "A", "title": "步骤A"},
            {"id": "B", "title": "步骤B", "dependencies": ["A"]},
        ])

        agent_svc = _FailingAgentService(db, error="compilation error: syntax error")
        orch, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        # 只调用 2 次 spawn（A 执行 + A blocked 监督者决策），B 未执行
        assert len(agent_svc._spawn_calls) == 2
        # A 标 failed
        from repositories.plan_step_repo import get_plan_tree_by_task
        tree = await get_plan_tree_by_task(db, "t1")
        a_node = [n for n in tree if n["id"] == "A"][0]
        b_node = [n for n in tree if n["id"] == "B"][0]
        assert a_node["status"] == "failed"
        assert b_node["status"] == "pending"  # B 因 A 失败而保持 pending
        # 任务失败
        state = await orch._store.get_state("t1")
        assert state["phase"] == "failed"

    async def test_dependency_output_merged_into_context(self, db):
        """A 完成后 B（依赖 A）执行时 context_summary 含 A 的 output"""
        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": "A", "title": "步骤A"},
            {"id": "B", "title": "步骤B", "dependencies": ["A"]},
        ])

        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "RESULT_FROM_A"},
            {"ok": True, "output": "B done"},
        ])
        orch, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        # 验证 B 的 spawn 调用 context_summary 含 A 的 output
        # A 是第一次调用，B 是第二次调用
        assert len(agent_svc._spawn_calls) == 2
        b_context = agent_svc._spawn_calls[1][2]  # (agent_id, prompt, context_summary)
        assert "RESULT_FROM_A" in b_context

    async def test_long_dependency_output_truncated_with_pointer(self, db):
        """Task 6.15: A 的超长 output 合并到 B 的 context 时截断附溯源指针"""
        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": "dep-node", "title": "步骤A"},
            {"id": "B", "title": "步骤B", "dependencies": ["dep-node"]},
        ])

        long_output = "Q" * 2000  # 远超 LOG_PREVIEW_ERROR_LEN * 2
        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": long_output},
            {"ok": True, "output": "B done"},
        ])
        orch, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        assert len(agent_svc._spawn_calls) == 2
        b_context = agent_svc._spawn_calls[1][2]
        # 截断附溯源指针指向 plan_steps 节点 dep-node
        assert "see node dep-node for full content" in b_context

    async def test_pre_review_halt_marks_skipped(self, db, monkeypatch):
        """pre_execution review 返回 halt → 节点标 skipped，spawn 未被调用"""
        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": "A", "title": "步骤A"},
        ])

        # monkeypatch get_final_review_agent 返回 halt 决策
        class _HaltReviewAgent:
            async def review(self, task_id, stage, step_id, context):
                return {
                    "action": "halt",
                    "reason": "test halt",
                    "pass": False,
                    "ts": 0,
                }

        from services.long_task import final_review_agent
        monkeypatch.setattr(
            final_review_agent, "get_final_review_agent", lambda: _HaltReviewAgent(),
        )

        agent_svc = _MockAgentService(db)
        orch, _, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        # 节点状态 skipped
        from repositories.plan_step_repo import get_plan_tree_by_task
        tree = await get_plan_tree_by_task(db, "t1")
        step = [n for n in tree if n["level"] > 0][0]
        assert step["status"] == "skipped"
        # spawn 未被调用（halt 在 spawn 之前）
        assert len(agent_svc._spawn_calls) == 0
        # ws 推了 long_task_step_fail（halt 也走 fail 消息）
        types = [m["type"] for m in ws.sent]
        assert "long_task_step_fail" in types


# ═══════════════════════════════════════════════════════════════
# TestStatusSemantics —— Phase 87 审查修复（C1 + M6）
# ═══════════════════════════════════════════════════════════════

class TestStatusSemantics:
    """状态语义一致性——审查发现的 C1（对抗反驳状态矛盾）和 M6（全 skipped 误标 completed）"""

    async def test_adversarial_replan_marks_task_failed(self, db, monkeypatch):
        """C1: 对抗验证 replan 时节点 DB=failed，executor 应返回 failed，任务标记 failed

        审查发现 bug：executor.py:249 返回 "completed" 但 DB 标 "failed"，
        导致 failed_count 漏计，叶子节点反驳后任务误标 completed。
        """
        from services.long_task import executor as executor_mod
        monkeypatch.setattr(executor_mod, "_RETRY_BASE_DELAY", 0.01)

        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": "A", "title": "步骤A"},
        ])

        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "A output"},
        ])
        orch, _, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        # mock _adversarial_challenge 返回 replan（反驳输出）
        async def _mock_challenge(task_id, stage, text, task_input, agent_id, step_id=None):
            return {"action": "replan", "reason": "output refuted by adversarial"}

        monkeypatch.setattr(orch, "_adversarial_challenge", _mock_challenge)

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        # 节点 A DB 状态 failed
        from repositories.plan_step_repo import get_plan_tree_by_task
        tree = await get_plan_tree_by_task(db, "t1")
        a_node = [n for n in tree if n["id"] == "A"][0]
        assert a_node["status"] == "failed"
        # 任务状态 failed（非 completed）——有节点被对抗反驳不应标 completed
        state = await orch._store.get_state("t1")
        assert state["phase"] == "failed", f"expected failed, got {state['phase']}"

    async def test_all_skipped_marks_task_failed(self, db, monkeypatch):
        """M6: 所有节点被 pre_review halt → 任务应 failed（非 completed）

        审查发现 bug：全 skipped 时 blocked_count=0, failed_count=0, remaining_pending=0
        → 任务误标 completed，但实际零工作完成。
        """
        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": "A", "title": "步骤A"},
            {"id": "B", "title": "步骤B"},
        ])

        class _HaltReviewAgent:
            async def review(self, task_id, stage, step_id, context):
                return {"action": "halt", "reason": "test halt", "pass": False, "ts": 0}

        from services.long_task import final_review_agent
        monkeypatch.setattr(
            final_review_agent, "get_final_review_agent", lambda: _HaltReviewAgent(),
        )

        agent_svc = _MockAgentService(db)
        orch, _, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        # 所有节点 skipped
        from repositories.plan_step_repo import get_plan_tree_by_task
        tree = await get_plan_tree_by_task(db, "t1")
        for node in tree:
            if node["level"] > 0:
                assert node["status"] == "skipped"
        # 任务 failed（非 completed）——零工作完成不应标 completed
        state = await orch._store.get_state("t1")
        assert state["phase"] == "failed", f"expected failed, got {state['phase']}"


class TestExceptionHandling:
    """异常兜底——审查发现的 C2（节点卡 running）和 M3（任务卡 executing）"""

    async def test_node_inner_exception_marks_node_failed(self, db, monkeypatch):
        """C2: _execute_node_inner 内部异常时节点应回写 failed（非卡 running）

        审查发现 bug：_execute_node_inner 无顶层 try/except，update_plan_node_status
        之后的 await 抛异常时节点永久卡 running，依赖它的节点永远不 ready。
        """
        from services.long_task import executor as executor_mod
        monkeypatch.setattr(executor_mod, "_RETRY_BASE_DELAY", 0.01)

        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": "A", "title": "步骤A"},
        ])

        agent_svc = _MockAgentService(db)
        orch, _, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        # mock _build_step_prompt 抛异常（running 之后）——模拟内部错误
        def _broken_prompt(node):
            raise RuntimeError("simulated prompt build failure")
        monkeypatch.setattr(orch, "_build_step_prompt", _broken_prompt)

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        # 节点 A 状态 failed（非 running）
        from repositories.plan_step_repo import get_plan_tree_by_task
        tree = await get_plan_tree_by_task(db, "t1")
        a_node = [n for n in tree if n["id"] == "A"][0]
        assert a_node["status"] == "failed", f"expected failed, got {a_node['status']}"
        # 任务状态 failed
        state = await orch._store.get_state("t1")
        assert state["phase"] == "failed"

    async def test_plan_tree_exception_marks_task_failed(self, db, monkeypatch):
        """M3: execute_plan_tree 内部异常时任务应标记 failed（非卡 executing）

        审查发现 bug：execute_plan_tree 的 while 循环内无 try/except，DB 错误等
        导致函数直接退出，_mark_failed/_mark_completed 都不会被调用。
        """
        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": "A", "title": "步骤A"},
        ])

        agent_svc = _MockAgentService(db)
        orch, _, ws = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        # mock get_plan_tree_by_task 抛异常（模拟 DB 错误）
        from services.long_task import executor as executor_mod

        original_get_tree = executor_mod.get_plan_tree_by_task
        call_count = [0]

        async def _broken_tree(conn, task_id):
            call_count[0] += 1
            # 第一次调用（line 91 初始 tree）正常返回，第二次（循环内）抛异常
            if call_count[0] == 1:
                return await original_get_tree(conn, task_id)
            raise RuntimeError("simulated DB error")

        monkeypatch.setattr(executor_mod, "get_plan_tree_by_task", _broken_tree)

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        # 任务状态 failed（非 executing）
        state = await orch._store.get_state("t1")
        assert state["phase"] == "failed", f"expected failed, got {state['phase']}"


class TestCleanupAndEvents:
    """内存清理 + 事件发布——审查发现的 C3（内存泄漏）和 C4（stall 检测脱节）"""

    async def test_cleanup_clears_adversarial_retries(self, db):
        """C3: _cleanup_task_state 应清理 _adversarial_retries（原仅清 pause/inject）"""
        orch, _, _ = _make_orchestrator(db)
        orch._adversarial_retries["t1"] = {"goal_confirmed": 1, "plan_generated": 0}
        orch._pause_events["t1"] = asyncio.Event()
        # M1: _inject_queues 改为 list[str]
        orch._inject_queues["t1"] = ["约束1"]

        orch._cleanup_task_state("t1")

        assert "t1" not in orch._adversarial_retries
        assert "t1" not in orch._pause_events
        assert "t1" not in orch._inject_queues

    async def test_cleanup_calls_agent_cleanup_task(self, db, monkeypatch):
        """C3: _cleanup_task_state 应调用 final_review/adversarial agent 的 cleanup_task"""
        orch, _, _ = _make_orchestrator(db)

        cleaned: list[str] = []

        class _MockAgent:
            def cleanup_task(self, task_id: str) -> None:
                cleaned.append(task_id)

        from services.long_task import final_review_agent, adversarial_agent
        monkeypatch.setattr(
            final_review_agent, "get_final_review_agent", lambda: _MockAgent(),
        )
        monkeypatch.setattr(
            adversarial_agent, "get_adversarial_agent", lambda: _MockAgent(),
        )

        orch._cleanup_task_state("t1")

        # 两个 agent 各调一次 cleanup_task("t1")
        assert cleaned == ["t1", "t1"]

    async def test_mark_failed_publishes_task_failed(self, db):
        """C4: _mark_failed 应发布 TASK_FAILED 事件（供 task_supervisor stall 计数）"""
        import time
        await _create_task(db, "t1")
        orch, _, _ = _make_orchestrator(db)
        await orch._store.set_state(
            "t1", phase="executing", confirmed_goal="test", started_at=int(time.time()),
        )

        published: list[tuple] = []

        class _MockEventBus:
            async def publish(self, event_type, data):
                published.append((event_type, data))

        orch.set_event_bus(_MockEventBus())

        await orch._mark_failed("t1", "test reason")

        # 发了 task_failed 事件，含 task_id 和 reason
        evt = next((d for e, d in published if e == "task_failed"), None)
        assert evt is not None
        assert evt["task_id"] == "t1"
        assert evt["reason"] == "test reason"

    async def test_mark_completed_publishes_task_completed(self, db):
        """C4: _mark_completed 应发布 TASK_COMPLETED 事件（供 task_supervisor 清理 stall）"""
        import time
        await _create_task(db, "t1")
        orch, _, _ = _make_orchestrator(db)
        await orch._store.set_state(
            "t1", phase="executing", confirmed_goal="test", started_at=int(time.time()),
        )

        published: list[tuple] = []

        class _MockEventBus:
            async def publish(self, event_type, data):
                published.append((event_type, data))

        orch.set_event_bus(_MockEventBus())

        await orch._mark_completed("t1")

        evt = next((e for e, _ in published if e == "task_completed"), None)
        assert evt is not None
        assert published[0][1]["task_id"] == "t1"

    async def test_mark_failed_without_event_bus_no_error(self, db):
        """C4: 未注入 event_bus 时 _mark_failed 不应报错（向后兼容）"""
        import time
        await _create_task(db, "t1")
        orch, _, _ = _make_orchestrator(db)
        await orch._store.set_state(
            "t1", phase="executing", confirmed_goal="test", started_at=int(time.time()),
        )
        # 不调 set_event_bus（_event_bus 默认 None）

        await orch._mark_failed("t1", "no bus")  # 不应抛异常

        state = await orch._store.get_state("t1")
        assert state["phase"] == "failed"


# ═══════════════════════════════════════════════════════════════
# TestStaleRunningRecovery —— M4: 启动恢复 stale running 节点
# ═══════════════════════════════════════════════════════════════

class TestStaleRunningRecovery:
    """M4: executor 启动时把 stale running 节点重置为 pending 重新执行

    场景：进程崩溃/重启残留 status='running' 的节点——_ready_nodes_from_tree
    只挑 pending，running 节点永远不会被执行，且其下游节点因依赖未 completed
    永远不 ready，导致任务卡死。
    """

    async def test_stale_running_recovered_to_pending(self, db):
        """stale running 节点在 executor 启动时被重置为 pending 并执行"""
        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            # 模拟崩溃残留：A 已 completed，B 残留 running（实际未完成），C 依赖 B
            {"id": "A", "title": "步骤A"},
            {"id": "B", "title": "步骤B", "dependencies": ["A"]},
            {"id": "C", "title": "步骤C", "dependencies": ["B"]},
        ])
        # 手动设置 A=completed, B=running（模拟崩溃残留）
        from repositories.plan_step_repo import update_plan_node_status
        await update_plan_node_status(db, "A", "completed", output="A done")
        await update_plan_node_status(db, "B", "running")

        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "B done (recovered)"},
            {"ok": True, "output": "C done"},
        ])
        orch, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        # B 被恢复为 pending 并重新执行 → 2 次 spawn（B + C）
        assert len(agent_svc._spawn_calls) == 2
        prompts = [call[1] for call in agent_svc._spawn_calls]
        assert "步骤B" in prompts[0]
        assert "步骤C" in prompts[1]
        # 任务完成（无卡死）
        state = await orch._store.get_state("t1")
        assert state["phase"] == "completed"

    async def test_no_stale_running_no_recovery_needed(self, db):
        """无 stale running 时恢复逻辑不应干扰正常执行"""
        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": "A", "title": "步骤A"},
            {"id": "B", "title": "步骤B", "dependencies": ["A"]},
        ])
        # 全 pending（无 stale running）

        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "A done"},
            {"ok": True, "output": "B done"},
        ])
        orch, _, _ = _make_orchestrator(db, agent_service=agent_svc)
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        # 正常执行 2 个节点
        assert len(agent_svc._spawn_calls) == 2
        state = await orch._store.get_state("t1")
        assert state["phase"] == "completed"


# ═══════════════════════════════════════════════════════════════
# TestStandardsLookupOutsideSemaphore —— M5: standards_lookup 移出信号量
# ═══════════════════════════════════════════════════════════════

class TestStandardsLookupOutsideSemaphore:
    """M5: standards_lookup（含 LLM 调用，慢）应在信号量外执行

    场景：节点失败（blocked）触发 standards_lookup 查询权威文档。
    原实现在 _execute_node_core 内调用，导致查询期间仍占用并发槽位，
    阻塞其他 ready 节点。M5 修复后移至 _execute_node 信号量释放后。
    """

    async def test_standards_lookup_called_when_blocked(self, db, monkeypatch):
        """M5: blocked 节点仍应触发 standards_lookup（行为契约保留）"""
        from services.long_task import executor as executor_mod
        monkeypatch.setattr(executor_mod, "_RETRY_BASE_DELAY", 0.01)

        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": "A", "title": "步骤A"},
        ])

        agent_svc = _FailingAgentService(db, error="compilation error: syntax error")
        orch, _, _ = _make_orchestrator(db, agent_service=agent_svc)

        lookup_calls: list[tuple] = []

        async def _track_lookup(task_id, node, err, agent_id):
            lookup_calls.append((task_id, node.get("id"), err, agent_id))

        orch._standards_lookup_hook = _track_lookup
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        # standards_lookup 被调用一次，参数正确
        assert len(lookup_calls) == 1
        assert lookup_calls[0][0] == "t1"
        assert lookup_calls[0][1] == "A"
        assert "compilation error" in lookup_calls[0][2]

    async def test_standards_lookup_not_called_when_completed(self, db):
        """M5: completed 节点不应触发 standards_lookup"""
        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": "A", "title": "步骤A"},
        ])

        agent_svc = _MockAgentService(db, spawn_results=[
            {"ok": True, "output": "A done"},
        ])
        orch, _, _ = _make_orchestrator(db, agent_service=agent_svc)

        lookup_calls: list[tuple] = []

        async def _track_lookup(task_id, node, err, agent_id):
            lookup_calls.append((task_id, node.get("id"), err, agent_id))

        orch._standards_lookup_hook = _track_lookup
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        # 成功节点不触发 standards_lookup
        assert len(lookup_calls) == 0

    async def test_standards_lookup_does_not_hold_semaphore(self, db, monkeypatch):
        """M5: standards_lookup 在信号量外——慢查询不阻塞其他 ready 节点

        信号量=1 + 2 独立节点 A（失败）+ B（成功）。
        若 standards_lookup 在信号量内：B 须等 A 的查询完成（0.3s）后才能启动。
        若在信号量外（M5 修复）：B 在 A 释放信号量后立即启动，与查询并行。
        断言：B 启动时间 < standards_lookup 完成时间。
        """
        from services.long_task import executor as executor_mod
        monkeypatch.setattr(executor_mod, "_RETRY_BASE_DELAY", 0.01)
        monkeypatch.setattr(executor_mod, "MAX_LONG_TASK_CONCURRENT_NODES", 1)

        await _create_task(db, "t1")
        await _create_root_and_children(db, "t1", [
            {"id": "A", "title": "步骤A"},  # 失败 → blocked → standards_lookup
            {"id": "B", "title": "步骤B"},  # 独立，应在查询期间启动
        ])

        b_start_time: list[float] = []
        lookup_finish_time: list[float] = []

        class _TimingAgentService:
            """A 失败 / B 成功——记录 B 启动时间"""
            def __init__(self, db):
                self.db = db
                self._spawn_calls: list[tuple] = []

            async def spawn_subagent(self, agent_id, prompt, context_summary="", auto_approve=False, **kwargs):
                self._spawn_calls.append((agent_id, prompt, context_summary))
                if "步骤B" in prompt:
                    b_start_time.append(asyncio.get_event_loop().time())
                    return {"ok": True, "output": "B done"}
                # A 失败（不可重试）
                return {"ok": False, "error": "compilation error: syntax error"}

        async def _slow_standards_lookup(task_id, node, err, agent_id):
            # 模拟慢 LLM 调用（0.3s）
            await asyncio.sleep(0.3)
            lookup_finish_time.append(asyncio.get_event_loop().time())

        orch, _, _ = _make_orchestrator(db, agent_service=_TimingAgentService(db))
        orch._standards_lookup_hook = _slow_standards_lookup
        await orch._store.set_state("t1", phase="executing", confirmed_goal="test")

        from services.long_task.executor import execute_plan_tree
        await execute_plan_tree(orch, "t1", "hermes")

        # B 应在 standards_lookup 完成前启动（M5: 信号量已释放）
        assert len(b_start_time) == 1, "B should have started"
        assert len(lookup_finish_time) == 1, "standards_lookup should have finished"
        assert b_start_time[0] < lookup_finish_time[0], (
            "B should start BEFORE standards_lookup finishes (M5: lookup outside semaphore). "
            f"B started at {b_start_time[0]}, lookup finished at {lookup_finish_time[0]}"
        )
