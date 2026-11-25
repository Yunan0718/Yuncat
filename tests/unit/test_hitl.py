"""tests/unit/test_hitl.py —— HITL 人机交互测试"""
import os
import sys
import asyncio
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestHumanNodeExecutor:
    """HumanNodeExecutor 决策往返"""

    @pytest.fixture
    def executor(self):
        from workflow.hitl_executor import HumanNodeExecutor
        return HumanNodeExecutor()

    async def test_approve_decision(self, executor):
        """批准决策——返回 approved=True"""
        run_id = "run-1"
        node_id = "node-a"

        async def submit_after_delay():
            await asyncio.sleep(0.05)
            executor.submit_decision(run_id, node_id, {"action": "approve", "note": "看起来不错"})

        asyncio.create_task(submit_after_delay())
        result = await executor.execute(
            {"id": node_id, "type": "human", "prompt": "确认执行？"},
            {"run_id": run_id},
        )
        assert result["approved"] is True
        assert "不错" in result.get("output", "")

    async def test_reject_decision(self, executor):
        """拒绝决策——返回 approved=False"""
        run_id = "run-2"
        node_id = "node-b"

        async def submit_after_delay():
            await asyncio.sleep(0.05)
            executor.submit_decision(run_id, node_id, {"action": "reject", "note": "不对"})

        asyncio.create_task(submit_after_delay())
        result = await executor.execute(
            {"id": node_id, "type": "human", "prompt": "确认？"},
            {"run_id": run_id},
        )
        assert result.get("approved") is False

    async def test_retry_raises(self, executor):
        """重试决策——抛 RuntimeError"""
        run_id = "run-3"
        node_id = "node-c"

        async def submit_after_delay():
            await asyncio.sleep(0.05)
            executor.submit_decision(run_id, node_id, {"action": "retry", "note": "再试一次"})

        asyncio.create_task(submit_after_delay())
        with pytest.raises(RuntimeError, match="重试"):
            await executor.execute(
                {"id": node_id, "type": "human", "prompt": "确认？"},
                {"run_id": run_id},
            )

    async def test_timeout_returns_rejected(self, executor):
        """超时——返回 approved=False + action=timeout"""
        # 不提交决策 → 应该超时。缩小超时时间以加速测试。
        executor._default_timeout = 0.1
        result = await executor.execute(
            {"id": "n1", "type": "human", "prompt": "确认？"},
            {"run_id": "run-timeout"},
        )
        assert result.get("approved") is False
        assert result.get("action") == "timeout"

    async def test_decision_not_found_after_timeout(self, executor):
        """超时后提交决策返回 False"""
        executor._default_timeout = 0.05
        run_id = "run-stale"
        node_id = "node-stale"
        # 先触发超时
        await executor.execute(
            {"id": node_id, "type": "human", "prompt": "x"},
            {"run_id": run_id},
        )
        # 超时后再提交 → 返回 False
        ok = executor.submit_decision(run_id, node_id, {"action": "approve"})
        assert ok is False


class TestHitlInDag:
    """HITL 节点集成到 DAG 引擎"""

    async def test_human_node_in_workflow(self):
        """含 human 节点的工作流可正确暂停和恢复"""
        from workflow.node_executor import MockNodeExecutor
        from workflow.hitl_executor import HumanNodeExecutor
        from workflow.engine import WorkflowEngine

        nodes = [
            {"id": "A", "type": "default"},
            {"id": "H", "type": "human", "prompt": "请审核 A 的输出"},
            {"id": "B", "type": "default"},
        ]
        edges = [{"from": "A", "to": "H"}, {"from": "H", "to": "B"}]

        class MockWorkflowRepo:
            def __init__(self):
                self._snapshots = {}

            async def get_workflow(self, wid):
                return {"nodes": nodes, "edges": edges}

            async def update_snapshot(self, rid, snap):
                self._snapshots[rid] = snap

            async def get_snapshot(self, rid):
                return self._snapshots.get(rid)

        # A 成功，H 被批准，B 成功
        default_exec = MockNodeExecutor({"A": {"output": "a"}, "B": {"output": "b"}})
        hitl_exec = HumanNodeExecutor()

        async def approve_h():
            await asyncio.sleep(0.05)
            hitl_exec.submit_decision("run-hitl", "H", {"action": "approve"})

        asyncio.create_task(approve_h())

        engine = WorkflowEngine(
            repo=MockWorkflowRepo(),
            executors={"default": default_exec, "human": hitl_exec},
        )
        result = await engine.run("wf-hitl", "test", run_id="run-hitl")
        assert result["status"] == "completed"
        assert "B" in result["results"]


# ═══════════════════════════════════════════════════════════════
# Phase 87 审查 M-2 / M-3: _pending 泄漏修复
# ═══════════════════════════════════════════════════════════════

class TestPhase87PendingLeakFixes:
    """M-2: node_executor._request_approval 用 finally 清理 _pending
    M-3: hitl_executor.execute 将 push_hitl_request 移入 try 块"""

    async def test_request_approval_cleans_pending_on_cancel(self):
        """M-2: _request_approval 被 cancel 时 _pending 不泄漏

        修复前：try/except 仅捕获 TimeoutError，CancelledError 直接传播，
        _pending 中的 entry 永不清理。
        修复后：finally 无条件 pop，任意异常路径都清理。
        """
        from workflow.node_executor import HumanNodeExecutor

        class _PassThroughAgentService:
            """push_hitl_request 立即返回，让 _request_approval 进入 event.wait()"""
            async def push_hitl_request(self, *args, **kwargs):
                pass

        executor = HumanNodeExecutor(
            agent_service=_PassThroughAgentService(), timeout_sec=10,
        )

        # 启动 _request_approval——它会阻塞在 event.wait()
        task = asyncio.create_task(executor._request_approval("r1", "n1", "p"))
        await asyncio.sleep(0.05)  # 让它进入 wait

        # 前置验证：_pending 已有 entry
        assert ("r1", "n1") in executor._pending

        # 取消任务——触发 CancelledError
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # M-2 修复后：_pending 应被 finally 清理
        assert ("r1", "n1") not in executor._pending, \
            "_pending 应在 CancelledError 路径被 finally 清理"

    async def test_request_approval_cleans_pending_on_timeout(self):
        """M-2 回归：正常 TimeoutError 路径仍清理 _pending（不破坏原行为）"""
        from workflow.node_executor import HumanNodeExecutor

        class _PassThroughAgentService:
            async def push_hitl_request(self, *args, **kwargs):
                pass

        executor = HumanNodeExecutor(
            agent_service=_PassThroughAgentService(), timeout_sec=0.1,
        )
        # 不提交审批 → 超时
        result = await executor._request_approval("r2", "n2", "p")
        assert result is False  # 超时拒绝
        assert ("r2", "n2") not in executor._pending

    async def test_request_approval_cleans_pending_on_resolve(self):
        """M-2 回归：正常 approve 路径仍清理 _pending"""
        from workflow.node_executor import HumanNodeExecutor

        class _PassThroughAgentService:
            async def push_hitl_request(self, *args, **kwargs):
                pass

        executor = HumanNodeExecutor(
            agent_service=_PassThroughAgentService(), timeout_sec=2,
        )

        async def approve_after_delay():
            await asyncio.sleep(0.05)
            executor.resolve_approval("r3", "n3", approved=True, feedback="ok")

        asyncio.create_task(approve_after_delay())
        result = await executor._request_approval("r3", "n3", "p")
        assert result is True
        assert ("r3", "n3") not in executor._pending

    async def test_hitl_execute_cleans_pending_when_push_raises(self):
        """M-3: hitl_executor.execute 中 push_hitl_request 抛异常时
        _pending_decisions 不泄漏（push 移入 try 块后 finally 清理）"""
        from workflow.hitl_executor import HumanNodeExecutor

        class _BoomAgentService:
            """push_hitl_request 永远抛异常——模拟 WS 断开"""
            async def push_hitl_request(self, **kwargs):
                raise RuntimeError("WS 连接断开")

        executor = HumanNodeExecutor(agent_service=_BoomAgentService())
        executor._default_timeout = 1.0

        # execute 应该抛 RuntimeError（push 失败传播到 engine 的 execute_node）
        with pytest.raises(RuntimeError, match="WS 连接断开"):
            await executor.execute(
                {"id": "n1", "type": "human", "prompt": "x"},
                {"run_id": "r1"},
            )

        # M-3 修复后：_pending_decisions 应被 finally 清理
        assert "r1:n1" not in executor._pending_decisions, \
            "_pending_decisions 应在 push 失败时被 finally 清理"

    async def test_hitl_execute_cleans_pending_on_normal_path(self):
        """M-3 回归：正常 approve 路径 _pending_decisions 仍清理（原行为）"""
        from workflow.hitl_executor import HumanNodeExecutor

        executor = HumanNodeExecutor()  # 无 agent_service，push 不调用
        executor._default_timeout = 2.0

        async def approve_after_delay():
            await asyncio.sleep(0.05)
            executor.submit_decision("r2", "n2", {"action": "approve", "note": "ok"})

        asyncio.create_task(approve_after_delay())
        result = await executor.execute(
            {"id": "n2", "type": "human", "prompt": "x"},
            {"run_id": "r2"},
        )
        assert result["approved"] is True
        assert "r2:n2" not in executor._pending_decisions
