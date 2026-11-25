"""tests/unit/test_dag_engine.py —— DAG 引擎 5 条必过用例

附录 L 状态机 + v32 失败语义:
  1. 单链失败停跑：A→B→C，B 报错 → C 不执行
  2. 并行一端失败：B 失败 → C 仍执行，依赖 B 的节点阻塞
  3. resume 跳过已完成：A 成功 B 失败，resume → A 跳过，B 重试
  4. 环图拒绝：A→B→C→A → _validate_graph 抛 ValueError
  5. checkpoint 往返一致：执行中 snapshot → resume → 结果相同
"""
import os
import sys
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class MockWorkflowRepo:
    """Mock Repository——提供 DAG 引擎需要的 get_workflow, update_snapshot, get_snapshot"""

    def __init__(self, nodes: list, edges: list):
        self._nodes = nodes
        self._edges = edges
        self._snapshots: dict[str, str] = {}

    async def get_workflow(self, workflow_id: str) -> dict:
        return {"nodes": self._nodes, "edges": self._edges}

    async def update_snapshot(self, run_id: str, snapshot_json: str) -> None:
        self._snapshots[run_id] = snapshot_json

    async def get_snapshot(self, run_id: str) -> str | None:
        return self._snapshots.get(run_id)


def _make_nodes(*names: str, node_type: str = "default") -> list:
    """创建节点 list"""
    return [{"id": name, "type": node_type} for name in names]


def _make_edges(*pairs: tuple[str, str]) -> list:
    """创建边 list"""
    return [{"from": f, "to": t} for f, t in pairs]


# ============================================================
# 用例 1: 单链失败停跑
# ============================================================
class TestCase1ChainFailureStop:
    """A→B→C，B 报错 → C 不执行，返回 partial_error"""

    async def test_chain_failure_stops_downstream(self):
        from workflow.node_executor import MockNodeExecutor
        from workflow.engine import WorkflowEngine

        nodes = _make_nodes("A", "B", "C")
        edges = _make_edges(("A", "B"), ("B", "C"))

        # B 返回 error
        responses = {
            "A": {"output": "ok"},
            "B": {"error": "B failed"},
            "C": {"output": "C should not run"},
        }
        executor = MockNodeExecutor(responses)
        engine = WorkflowEngine(
            repo=MockWorkflowRepo(nodes, edges),
            executors={"default": executor},
        )

        result = await engine.run("wf-1", "test input")

        assert result["status"] == "partial_error"
        assert "B" in result["error_nodes"]
        # C 不应该被执行（被 B 阻塞）
        executed = {nid for nid, _ in executor.calls}
        assert "C" not in executed


# ============================================================
# 用例 2: 并行一端失败
# ============================================================
class TestCase2ParallelPartialFailure:
    """A→B, A→C（并行），B 失败 → C 仍执行，D(depends on B) 阻塞"""

    async def test_parallel_partial_failure(self):
        from workflow.node_executor import MockNodeExecutor
        from workflow.engine import WorkflowEngine

        nodes = _make_nodes("A", "B", "C", "D")
        edges = _make_edges(("A", "B"), ("A", "C"), ("B", "D"))

        responses = {
            "A": {"output": "start"},
            "B": {"error": "B crashed"},
            "C": {"output": "C ok"},
            "D": {"output": "D never"},
        }
        executor = MockNodeExecutor(responses)
        engine = WorkflowEngine(
            repo=MockWorkflowRepo(nodes, edges),
            executors={"default": executor},
        )

        result = await engine.run("wf-2", "test")

        executed = {nid for nid, _ in executor.calls}
        assert "C" in executed, "C 应在并行中执行"
        assert "D" not in executed, "D 依赖 B，应被阻塞"
        assert "B" in result["error_nodes"]


# ============================================================
# 用例 3: resume 跳过已完成
# ============================================================
class TestCase3ResumeSkipsCompleted:
    """A 成功 B 失败，resume → A 跳过，B 重试"""

    async def test_resume_skips_completed(self):
        from workflow.node_executor import MockNodeExecutor
        from workflow.engine import WorkflowEngine

        nodes = _make_nodes("A", "B")
        edges = _make_edges(("A", "B"))

        # 第一次执行：A 成功，B 失败
        responses1 = {"A": {"output": "a1"}, "B": {"error": "fail1"}}
        executor1 = MockNodeExecutor(responses1)
        engine = WorkflowEngine(
            repo=MockWorkflowRepo(nodes, edges),
            executors={"default": executor1},
        )
        result1 = await engine.run("wf-3", "test", run_id="run-1")
        assert result1["status"] == "partial_error"

        # Resume: B 重试成功
        snapshot = await engine.recover_from_snapshot("run-1")
        responses2 = {"B": {"output": "b ok"}}
        executor2 = MockNodeExecutor(responses2)
        engine2 = WorkflowEngine(
            repo=engine.repo,
            executors={"default": executor2},
        )
        result2 = await engine2.run("wf-3", "test", resume_from_snapshot=snapshot)

        # A 不应该再次被执行
        executed = {nid for nid, _ in executor2.calls}
        assert "A" not in executed, "A 已成功，不应重复执行"
        assert "B" in executed, "B 应被重试"
        assert result2["status"] == "completed"


# ============================================================
# 用例 4: 环图拒绝
# ============================================================
class TestCase4CycleRejection:
    """A→B→C→A → _validate_graph 抛 ValueError"""

    def test_cycle_detection_rejects(self):
        from workflow.engine import WorkflowEngine

        nodes = _make_nodes("A", "B", "C")
        edges = _make_edges(("A", "B"), ("B", "C"), ("C", "A"))

        engine = WorkflowEngine()
        with pytest.raises(ValueError, match="cycle"):
            engine._validate_graph(nodes, edges)

    def test_self_loop_rejected(self):
        """自环也被检测"""
        from workflow.engine import WorkflowEngine

        nodes = _make_nodes("X")
        edges = _make_edges(("X", "X"))

        engine = WorkflowEngine()
        with pytest.raises(ValueError, match="cycle"):
            engine._validate_graph(nodes, edges)

    def test_valid_dag_passes(self):
        """有效 DAG 通过验证"""
        from workflow.engine import WorkflowEngine

        nodes = _make_nodes("A", "B", "C", "D")
        edges = _make_edges(("A", "B"), ("A", "C"), ("B", "D"), ("C", "D"))

        engine = WorkflowEngine()
        engine._validate_graph(nodes, edges)  # 不抛异常即为通过


# ============================================================
# 用例 5: checkpoint 往返一致
# ============================================================
class TestCase5CheckpointRoundtrip:
    """执行中 snapshot → 冷重启 resume → 结果相同"""

    async def test_checkpoint_roundtrip(self):
        from workflow.node_executor import MockNodeExecutor
        from workflow.engine import WorkflowEngine

        nodes = _make_nodes("A", "B", "C")
        edges = _make_edges(("A", "B"), ("B", "C"))

        # 全部成功
        responses = {"A": {"output": "a"}, "B": {"output": "b"}, "C": {"output": "c"}}
        executor = MockNodeExecutor(responses)
        repo = MockWorkflowRepo(nodes, edges)
        engine = WorkflowEngine(repo=repo, executors={"default": executor})

        result1 = await engine.run("wf-5", "test", run_id="run-5")
        assert result1["status"] == "completed"

        # 从 snapshot 恢复重新执行
        snapshot = await engine.recover_from_snapshot("run-5")
        assert snapshot is not None
        assert "node_states" in snapshot
        assert "node_results" in snapshot

        # 第二次执行跳过全部已完成节点
        executor2 = MockNodeExecutor({})
        engine2 = WorkflowEngine(repo=repo, executors={"default": executor2})
        result2 = await engine2.run("wf-5", "test", resume_from_snapshot=snapshot)
        assert result2["status"] == "completed"
        # 所有节点都已完成，没有人需要重新执行
        assert len(executor2.calls) == 0


# ============================================================
# 额外用例
# ============================================================
class TestDAGExtraCases:
    """其他 DAG 边界测试"""

    async def test_single_node_workflow(self):
        """单节点工作流正常执行"""
        from workflow.node_executor import MockNodeExecutor
        from workflow.engine import WorkflowEngine

        nodes = _make_nodes("Solo")
        edges = []
        responses = {"Solo": {"output": "done"}}
        executor = MockNodeExecutor(responses)
        engine = WorkflowEngine(
            repo=MockWorkflowRepo(nodes, edges),
            executors={"default": executor},
        )
        result = await engine.run("wf-solo", "test")
        assert result["status"] == "completed"
        assert result["final_output"] == "done"

    async def test_diamond_workflow(self):
        """菱形依赖：A→B, A→C, B→D, C→D"""
        from workflow.node_executor import MockNodeExecutor
        from workflow.engine import WorkflowEngine

        nodes = _make_nodes("A", "B", "C", "D")
        edges = _make_edges(("A", "B"), ("A", "C"), ("B", "D"), ("C", "D"))
        responses = {
            "A": {"output": "a"}, "B": {"output": "b"},
            "C": {"output": "c"}, "D": {"output": "d"},
        }
        executor = MockNodeExecutor(responses)
        engine = WorkflowEngine(
            repo=MockWorkflowRepo(nodes, edges),
            executors={"default": executor},
        )
        result = await engine.run("wf-diamond", "test")
        assert result["status"] == "completed"
        # D 的输出应该是最后一个
        assert result["final_output"] == "d"


# ============================================================
# Phase 87 审查 L-1 / L-2: pause_events 清理 + 空工作流拒绝
# ============================================================
class TestPhase87EngineFixes:
    """L-1: run 完成后清理 _pause_events（防泄漏）
    L-2: 空工作流抛 ValueError（原代码误标 completed）"""

    async def test_empty_nodes_dict_rejected(self):
        """L-2: 空 nodes dict → ValueError"""
        from workflow.engine import WorkflowEngine
        engine = WorkflowEngine()
        with pytest.raises(ValueError, match="no nodes"):
            engine._validate_graph({}, [])

    async def test_empty_nodes_list_rejected(self):
        """L-2: 空 nodes list → ValueError（list 先转 dict 再校验）"""
        from workflow.engine import WorkflowEngine
        engine = WorkflowEngine()
        with pytest.raises(ValueError, match="no nodes"):
            engine._validate_graph([], [])

    async def test_empty_workflow_run_raises(self):
        """L-2: run() 空工作流抛 ValueError（调用方 _run_and_finalize 捕获标 failed）"""
        from workflow.engine import WorkflowEngine

        class _EmptyRepo:
            async def get_workflow(self, wid):
                return {"nodes": [], "edges": []}

        engine = WorkflowEngine(repo=_EmptyRepo())
        with pytest.raises(ValueError, match="no nodes"):
            await engine.run("wf-empty", "test", run_id="run-empty")

    async def test_pause_events_cleared_after_normal_completion(self):
        """L-1: run 正常完成后 _pause_events[run_id] 被清理

        场景：run_id 的 pause event 残留（例如上一轮 run 异常退出），
        新一轮 run 正常完成时应清理。用 monkeypatch 让 _is_pause_requested
        返回 False（跳过暂停分支），使 run 走正常完成路径。
        """
        from workflow.engine import WorkflowEngine
        from workflow.node_executor import MockNodeExecutor

        nodes = _make_nodes("A")
        edges = []
        executor = MockNodeExecutor(responses={"A": {"output": "ok"}})
        engine = WorkflowEngine(
            repo=MockWorkflowRepo(nodes, edges),
            executors={"default": executor},
        )
        # 注入残留 pause event（模拟上一轮泄漏）
        import asyncio as _asyncio
        engine._pause_events["run-stale"] = _asyncio.Event()
        # 让暂停检查始终返回 False，使 run 走正常完成路径
        engine._is_pause_requested = lambda run_id: False

        await engine.run("wf-1", "test", run_id="run-stale")

        # L-1 修复后：正常完成时清理 _pause_events
        assert "run-stale" not in engine._pause_events, \
            "正常完成后应清理 _pause_events 残留"

    async def test_pause_events_cleared_after_pause_return(self):
        """L-1 回归：暂停分支 _clear_pause_request 仍正常工作（不破坏原行为）"""
        from workflow.engine import WorkflowEngine
        from workflow.node_executor import MockNodeExecutor

        nodes = _make_nodes("A")
        edges = []
        executor = MockNodeExecutor(responses={"A": {"output": "ok"}})
        engine = WorkflowEngine(
            repo=MockWorkflowRepo(nodes, edges),
            executors={"default": executor},
        )
        engine.pause("run-pause")  # 预设暂停

        result = await engine.run("wf-1", "test", run_id="run-pause")
        assert result["status"] == "paused"
        # 暂停分支已 _clear_pause_request
        assert "run-pause" not in engine._pause_events
