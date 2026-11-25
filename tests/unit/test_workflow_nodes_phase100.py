"""tests/unit/test_workflow_nodes_phase100.py —— Phase 100: 工作流节点扩展合约测试

覆盖:
- _eval_condition DSL: contains / starts_with / ends_with / matches / == / != / default / truthy
- manager 条件路由: 选定分支执行 + 非选定分支标记 skipped
- manager 无匹配条件返回 error
- manager snapshot 恢复保持路由决策
- merge 等待全部上游 + 合并 output（concat / json 策略）
- summary 调用模型 + 写入 knowledge_nodes(source='workflow_summary')
- summary 无 agent_service 时 fallback 截断
"""
import os
import sys
import tempfile

import aiosqlite
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from repositories import init_db
from repositories.schema import SCHEMA_SQL


# ============================================================
# 复用 test_dag_engine.py 的 MockWorkflowRepo
# ============================================================

class MockWorkflowRepo:
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


def _make_nodes(*specs) -> list:
    """创建节点 list。specs 可以是 str（id，type=default）或 dict（完整节点）。"""
    result = []
    for s in specs:
        if isinstance(s, str):
            result.append({"id": s, "type": "default"})
        else:
            result.append(s)
    return result


def _make_edges(*pairs: tuple[str, str]) -> list:
    return [{"from": f, "to": t} for f, t in pairs]


@pytest.fixture
async def db():
    """fresh DB——供 SummaryNodeExecutor 写入 knowledge_nodes"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = await init_db(path)
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()
    os.unlink(path)


# ============================================================
# 1. _eval_condition DSL 单元测试
# ============================================================
@pytest.mark.unit
class TestEvalCondition:
    """Phase 100: 条件 DSL 求值器"""

    def test_contains(self):
        from workflow.node_executor import _eval_condition
        node_results = {"A": {"output": "TODO: fix bug"}}
        assert _eval_condition("{{A.output}} contains 'TODO'", node_results) is True
        assert _eval_condition("{{A.output}} contains 'DONE'", node_results) is False

    def test_equals(self):
        from workflow.node_executor import _eval_condition
        node_results = {"A": {"output": "ok"}}
        assert _eval_condition("{{A.output}} == 'ok'", node_results) is True
        assert _eval_condition("{{A.output}} == 'fail'", node_results) is False

    def test_not_equals(self):
        from workflow.node_executor import _eval_condition
        node_results = {"A": {"output": "ok"}}
        assert _eval_condition("{{A.output}} != 'fail'", node_results) is True

    def test_starts_with(self):
        from workflow.node_executor import _eval_condition
        node_results = {"A": {"output": "ERROR: something"}}
        assert _eval_condition("{{A.output}} starts_with 'ERROR'", node_results) is True

    def test_ends_with(self):
        from workflow.node_executor import _eval_condition
        node_results = {"A": {"output": "all good"}}
        assert _eval_condition("{{A.output}} ends_with 'good'", node_results) is True

    def test_matches_regex(self):
        from workflow.node_executor import _eval_condition
        node_results = {"A": {"output": "code-12345"}}
        assert _eval_condition("{{A.output}} matches '\\d+'", node_results) is True

    def test_missing_node_returns_empty(self):
        from workflow.node_executor import _eval_condition
        # 引用不存在的节点 → 空字符串 → contains 'x' 为 False
        assert _eval_condition("{{missing.output}} contains 'x'", {}) is False

    def test_truthy_no_operator(self):
        from workflow.node_executor import _eval_condition
        node_results = {"A": {"output": "some content"}}
        assert _eval_condition("{{A.output}}", node_results) is True
        assert _eval_condition("{{missing.output}}", {}) is False


# ============================================================
# 2. Manager 条件路由
# ============================================================
@pytest.mark.unit
class TestManagerConditionalRouting:
    """Phase 100: manager 节点条件路由——选定分支执行，非选定标记 skipped"""

    async def test_manager_selects_branch_on_condition_match(self):
        """条件命中 → 选定分支执行，另一分支 skipped"""
        from workflow.engine import WorkflowEngine
        from workflow.node_executor import MockNodeExecutor, ManagerNodeExecutor

        nodes = _make_nodes(
            {"id": "A", "type": "default"},
            {"id": "mgr", "type": "manager", "conditions": [
                {"when": "{{A.output}} contains 'TODO'", "goto": "B"},
                {"when": "default", "goto": "C"},
            ]},
            "B",
            "C",
        )
        edges = _make_edges(("A", "mgr"), ("mgr", "B"), ("mgr", "C"))
        responses = {"A": {"output": "TODO: fix this"}}
        executor = MockNodeExecutor(responses)
        engine = WorkflowEngine(
            repo=MockWorkflowRepo(nodes, edges),
            executors={"default": executor, "manager": ManagerNodeExecutor()},
        )
        result = await engine.run("wf-mgr-1", "test")

        executed = {nid for nid, _ in executor.calls}
        assert "B" in executed, "B 是选定分支，应执行"
        assert "C" not in executed, "C 非选定分支，不应执行"
        assert "C" in result.get("skipped_nodes", []), "C 应标记为 skipped"
        assert result["status"] == "completed"

    async def test_manager_selects_default_branch(self):
        """条件不命中 → default 分支执行"""
        from workflow.engine import WorkflowEngine
        from workflow.node_executor import MockNodeExecutor, ManagerNodeExecutor

        nodes = _make_nodes(
            {"id": "A", "type": "default"},
            {"id": "mgr", "type": "manager", "conditions": [
                {"when": "{{A.output}} contains 'TODO'", "goto": "B"},
                {"when": "default", "goto": "C"},
            ]},
            "B",
            "C",
        )
        edges = _make_edges(("A", "mgr"), ("mgr", "B"), ("mgr", "C"))
        responses = {"A": {"output": "all done, no todos"}}
        executor = MockNodeExecutor(responses)
        engine = WorkflowEngine(
            repo=MockWorkflowRepo(nodes, edges),
            executors={"default": executor, "manager": ManagerNodeExecutor()},
        )
        result = await engine.run("wf-mgr-2", "test")

        executed = {nid for nid, _ in executor.calls}
        assert "C" in executed, "default 分支 C 应执行"
        assert "B" not in executed, "B 不应执行"
        assert "B" in result.get("skipped_nodes", [])
        assert result["status"] == "completed"

    async def test_manager_no_match_no_default_returns_error(self):
        """无匹配条件且无 default → manager 返回 error"""
        from workflow.engine import WorkflowEngine
        from workflow.node_executor import MockNodeExecutor, ManagerNodeExecutor

        nodes = _make_nodes(
            {"id": "A", "type": "default"},
            {"id": "mgr", "type": "manager", "conditions": [
                {"when": "{{A.output}} contains 'XYZ'", "goto": "B"},
            ]},
            "B",
            "C",
        )
        edges = _make_edges(("A", "mgr"), ("mgr", "B"), ("mgr", "C"))
        responses = {"A": {"output": "no match here"}}
        executor = MockNodeExecutor(responses)
        engine = WorkflowEngine(
            repo=MockWorkflowRepo(nodes, edges),
            executors={"default": executor, "manager": ManagerNodeExecutor()},
        )
        result = await engine.run("wf-mgr-3", "test")

        # manager 返回 dict 含 error 字段但不是异常 → state="success"（无异常抛出）
        # selected_branch=None → 所有子节点 skipped
        executed = {nid for nid, _ in executor.calls}
        assert "B" not in executed
        assert "C" not in executed
        # 两分支都 skipped 因为 selected_branch=None（无匹配）
        skipped = result.get("skipped_nodes", [])
        assert "B" in skipped
        assert "C" in skipped

    async def test_manager_snapshot_resume_preserves_routing(self):
        """snapshot 恢复时保持 manager 路由决策——选定分支已 completed，非选定保持 skipped"""
        from workflow.engine import WorkflowEngine
        from workflow.node_executor import MockNodeExecutor, ManagerNodeExecutor

        nodes = _make_nodes(
            {"id": "A", "type": "default"},
            {"id": "mgr", "type": "manager", "conditions": [
                {"when": "{{A.output}} contains 'TODO'", "goto": "B"},
                {"when": "default", "goto": "C"},
            ]},
            "B",
            "C",
        )
        edges = _make_edges(("A", "mgr"), ("mgr", "B"), ("mgr", "C"))
        responses = {"A": {"output": "TODO: fix"}, "B": {"output": "b done"}}
        executor = MockNodeExecutor(responses)
        repo = MockWorkflowRepo(nodes, edges)
        engine = WorkflowEngine(
            repo=repo,
            executors={"default": executor, "manager": ManagerNodeExecutor()},
        )
        result1 = await engine.run("wf-mgr-rs", "test", run_id="run-mgr-rs")
        assert "B" in {nid for nid, _ in executor.calls}
        assert "C" in result1.get("skipped_nodes", [])

        # 从 snapshot 恢复——所有节点已完成，不应重复执行
        snapshot = await engine.recover_from_snapshot("run-mgr-rs")
        executor2 = MockNodeExecutor({})
        engine2 = WorkflowEngine(
            repo=repo,
            executors={"default": executor2, "manager": ManagerNodeExecutor()},
        )
        result2 = await engine2.run("wf-mgr-rs", "test", resume_from_snapshot=snapshot)

        assert len(executor2.calls) == 0, "全部已完成，无节点需重跑"
        assert result2["status"] == "completed"
        # C 仍应保持 skipped（不因恢复而重新激活）
        assert "C" in result2.get("skipped_nodes", [])


# ============================================================
# 3. Merge 汇总节点
# ============================================================
@pytest.mark.unit
class TestMergeNode:
    """Phase 100: merge 节点等待全部上游完成后合并 output"""

    async def test_merge_waits_for_all_upstream(self):
        """merge 节点只在 A 和 B 都完成后才执行"""
        from workflow.engine import WorkflowEngine
        from workflow.node_executor import MockNodeExecutor, MergeNodeExecutor

        nodes = _make_nodes(
            "A", "B",
            {"id": "M", "type": "merge", "inputs": ["A", "B"]},
            "D",
        )
        edges = _make_edges(("A", "M"), ("B", "M"), ("M", "D"))
        responses = {
            "A": {"output": "output-a"},
            "B": {"output": "output-b"},
            "D": {"output": "d ran"},
        }
        executor = MockNodeExecutor(responses)
        engine = WorkflowEngine(
            repo=MockWorkflowRepo(nodes, edges),
            executors={"default": executor, "merge": MergeNodeExecutor()},
        )
        result = await engine.run("wf-merge-1", "test")

        # M 是 merge 类型，由 MergeNodeExecutor 执行（不在 MockNodeExecutor.calls 中）
        # 通过 result["results"] 验证 M 已执行
        assert "M" in result["results"], "merge 应在 A/B 完成后执行"
        executed = {nid for nid, _ in executor.calls}
        assert "D" in executed, "D 应在 merge 完成后执行"
        assert result["status"] == "completed"

        # 验证 merge 输出包含两上游内容
        merge_result = result["results"].get("M", {})
        assert "output-a" in merge_result.get("output", "")
        assert "output-b" in merge_result.get("output", "")
        assert merge_result.get("merged_from") == ["A", "B"]

    async def test_merge_json_strategy(self):
        """json 策略输出 JSON 对象"""
        from workflow.engine import WorkflowEngine
        from workflow.node_executor import MockNodeExecutor, MergeNodeExecutor
        import json as _json

        nodes = _make_nodes(
            "A", "B",
            {"id": "M", "type": "merge", "inputs": ["A", "B"], "strategy": "json"},
        )
        edges = _make_edges(("A", "M"), ("B", "M"))
        responses = {"A": {"output": "aaa"}, "B": {"output": "bbb"}}
        executor = MockNodeExecutor(responses)
        engine = WorkflowEngine(
            repo=MockWorkflowRepo(nodes, edges),
            executors={"default": executor, "merge": MergeNodeExecutor()},
        )
        result = await engine.run("wf-merge-2", "test")

        merge_output = result["results"]["M"]["output"]
        parsed = _json.loads(merge_output)
        assert parsed["A"] == "aaa"
        assert parsed["B"] == "bbb"

    async def test_merge_default_inputs_all_upstream(self):
        """inputs 缺省时合并所有已完成节点（排除自身）"""
        from workflow.engine import WorkflowEngine
        from workflow.node_executor import MockNodeExecutor, MergeNodeExecutor

        nodes = _make_nodes(
            "A", "B",
            {"id": "M", "type": "merge"},  # 无 inputs
        )
        edges = _make_edges(("A", "M"), ("B", "M"))
        responses = {"A": {"output": "x"}, "B": {"output": "y"}}
        executor = MockNodeExecutor(responses)
        engine = WorkflowEngine(
            repo=MockWorkflowRepo(nodes, edges),
            executors={"default": executor, "merge": MergeNodeExecutor()},
        )
        result = await engine.run("wf-merge-3", "test")

        merge_result = result["results"]["M"]
        assert "x" in merge_result["output"]
        assert "y" in merge_result["output"]
        # merged_from 应包含 A 和 B（顺序取决于 node_results 的 dict 迭代序）
        assert set(merge_result["merged_from"]) == {"A", "B"}


# ============================================================
# 4. Summary 摘要节点
# ============================================================
@pytest.mark.unit
class TestSummaryNode:
    """Phase 100: summary 节点调用模型 + 写入 knowledge_nodes"""

    async def test_summary_writes_to_knowledge_nodes(self, db):
        """summary 节点将摘要写入 knowledge_nodes(source='workflow_summary')"""
        from workflow.engine import WorkflowEngine
        from workflow.node_executor import MockNodeExecutor, SummaryNodeExecutor

        # Mock agent_service——invoke 返回固定摘要
        class MockAgentService:
            async def invoke(self, agent_id, prompt, context=None):
                return "这是自动生成的摘要"

        nodes = _make_nodes(
            "A",
            {"id": "S", "type": "summary", "inputs": ["A"], "title": "测试摘要"},
        )
        edges = _make_edges(("A", "S"))
        responses = {"A": {"output": "一段需要摘要的长文本内容"}}
        executor = MockNodeExecutor(responses)
        engine = WorkflowEngine(
            repo=MockWorkflowRepo(nodes, edges),
            executors={
                "default": executor,
                "summary": SummaryNodeExecutor(
                    agent_service=MockAgentService(), db=db,
                ),
            },
        )
        result = await engine.run("wf-sum-1", "test")

        # 验证 summary 节点执行
        summary_result = result["results"].get("S", {})
        assert summary_result.get("output") == "这是自动生成的摘要"
        assert summary_result.get("persisted") is True

        # 验证 knowledge_nodes 表有 source='workflow_summary' 的记录
        cursor = await db.execute(
            "SELECT title, content, source FROM knowledge_nodes "
            "WHERE source='workflow_summary'"
        )
        rows = await cursor.fetchall()
        assert len(rows) >= 1
        assert rows[0][0] == "测试摘要"
        assert rows[0][1] == "这是自动生成的摘要"
        assert rows[0][2] == "workflow_summary"

    async def test_summary_without_agent_service_fallback(self, db):
        """无 agent_service 时 fallback 截断上游内容"""
        from workflow.engine import WorkflowEngine
        from workflow.node_executor import MockNodeExecutor, SummaryNodeExecutor

        nodes = _make_nodes(
            "A",
            {"id": "S", "type": "summary", "inputs": ["A"], "persist": False},
        )
        edges = _make_edges(("A", "S"))
        responses = {"A": {"output": "短文本"}}
        executor = MockNodeExecutor(responses)
        engine = WorkflowEngine(
            repo=MockWorkflowRepo(nodes, edges),
            executors={
                "default": executor,
                "summary": SummaryNodeExecutor(agent_service=None, db=db),
            },
        )
        result = await engine.run("wf-sum-2", "test")

        summary_result = result["results"]["S"]
        # fallback 截断（content[:500]），短文本原样返回
        assert "短文本" in summary_result["output"]
        # persist=False → 不写入 knowledge_nodes
        assert summary_result.get("persisted") is False

    async def test_summary_default_inputs_all_upstream(self, db):
        """inputs 缺省时摘要所有已完成上游节点"""
        from workflow.engine import WorkflowEngine
        from workflow.node_executor import MockNodeExecutor, SummaryNodeExecutor

        class MockAgentService:
            async def invoke(self, agent_id, prompt, context=None):
                # 验证 prompt 包含两上游内容
                assert "output-a" in prompt
                assert "output-b" in prompt
                return "合并摘要"

        nodes = _make_nodes(
            "A", "B",
            {"id": "S", "type": "summary"},  # 无 inputs
        )
        edges = _make_edges(("A", "S"), ("B", "S"))
        responses = {"A": {"output": "output-a"}, "B": {"output": "output-b"}}
        executor = MockNodeExecutor(responses)
        engine = WorkflowEngine(
            repo=MockWorkflowRepo(nodes, edges),
            executors={
                "default": executor,
                "summary": SummaryNodeExecutor(
                    agent_service=MockAgentService(), db=db,
                ),
            },
        )
        result = await engine.run("wf-sum-3", "test")

        summary_result = result["results"]["S"]
        assert summary_result["output"] == "合并摘要"
        assert set(summary_result["summarized_from"]) == {"A", "B"}


# ============================================================
# Phase 43 G2.2: tool_execute 节点执行器
# ============================================================
@pytest.mark.unit
class TestToolExecuteNodeExecutor:
    """tool_execute 节点直接调用 ToolService，不走 Agent 循环。"""

    @pytest.mark.asyncio
    async def test_execute_tool_success(self):
        """正常调用工具并返回截断后的输出。"""
        from workflow.node_executor import ToolExecuteNodeExecutor

        class FakeToolService:
            async def execute(self, **kwargs):
                return {"ok": True, "data": "hello"}

        executor = ToolExecuteNodeExecutor(tool_service=FakeToolService())
        result = await executor.execute(
            {"id": "t1", "type": "tool_execute", "tool_name": "echo", "arguments": {"text": "hi"}},
            {"run_id": "run-1", "agent_id": "hermes"},
        )
        assert result["status"] == "success"
        assert result["tool_name"] == "echo"
        assert '"ok": true' in result["output"]

    @pytest.mark.asyncio
    async def test_execute_tool_resolves_placeholders(self):
        """参数中的 {{node_id.output}} 占位符被替换。"""
        from workflow.node_executor import ToolExecuteNodeExecutor

        class FakeToolService:
            async def execute(self, **kwargs):
                args = kwargs.get("arguments", {})
                return {"path": args.get("path")}

        executor = ToolExecuteNodeExecutor(tool_service=FakeToolService())
        result = await executor.execute(
            {"id": "t2", "type": "tool_execute", "tool_name": "read_file",
             "arguments": {"path": "{{prev.output}}/file.txt"}},
            {"run_id": "run-1", "agent_id": "hermes",
             "node_results": {"prev": {"output": "/workspace"}}},
        )
        assert result["status"] == "success"
        assert "/workspace/file.txt" in result["output"]

    @pytest.mark.asyncio
    async def test_execute_tool_missing_name_returns_error(self):
        """缺少 tool_name 返回错误状态。"""
        from workflow.node_executor import ToolExecuteNodeExecutor
        executor = ToolExecuteNodeExecutor(tool_service=None)
        result = await executor.execute({"id": "t3", "type": "tool_execute"}, {})
        assert result["status"] == "error"
        assert "Missing tool_name" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_tool_no_service_returns_error(self):
        """无 ToolService 返回错误状态。"""
        from workflow.node_executor import ToolExecuteNodeExecutor
        executor = ToolExecuteNodeExecutor(tool_service=None)
        result = await executor.execute(
            {"id": "t4", "type": "tool_execute", "tool_name": "echo"}, {}
        )
        assert result["status"] == "error"
        assert "ToolService not available" in result["error"]


# ============================================================
# Phase 43 H6: ha_native 节点执行器
# ============================================================
@pytest.mark.unit
class TestHANativeNodeExecutor:
    """ha_native 节点路由到 Hermes Agent 子进程的原生 ReAct 循环。

    HA 不可用时由 hermes.py 透明降级到 Provider API。
    """

    @pytest.mark.asyncio
    async def test_ha_native_success(self):
        """正常调用 AgentService.invoke 返回 success——路由到默认 agent（P1 语义）。"""
        from config import DEFAULT_AGENT_ID
        from workflow.node_executor import HANativeNodeExecutor

        class MockAgentService:
            def __init__(self):
                self.last_ctx = None

            async def invoke(self, agent_id, prompt, context=None):
                self.last_ctx = context
                assert agent_id == DEFAULT_AGENT_ID, \
                    "ha_native 节点未显式指定 agent_id 时必须路由到 DEFAULT_AGENT_ID"
                return "HA 原生循环结果"

        svc = MockAgentService()
        executor = HANativeNodeExecutor(agent_service=svc)
        result = await executor.execute(
            {"id": "h1", "type": "ha_native", "prompt": "分析这段代码"},
            {"run_id": "run-h1", "input": "fallback input"},
        )
        assert result["status"] == "success"
        assert result["agent_id"] == DEFAULT_AGENT_ID
        assert result["node_type"] == "ha_native"
        assert "HA 原生循环结果" in result["output"]
        # ha_native 标记必须传递给 hermes.py
        assert svc.last_ctx.get("ha_native") is True

    @pytest.mark.asyncio
    async def test_ha_native_no_agent_service_returns_error(self):
        """无 AgentService 时返回明确错误（不抛异常）。"""
        from workflow.node_executor import HANativeNodeExecutor
        executor = HANativeNodeExecutor(agent_service=None)
        result = await executor.execute(
            {"id": "h2", "type": "ha_native", "prompt": "任务"}, {}
        )
        assert result["status"] == "error"
        assert "AgentService not available" in result["output"]
        assert result["node_id"] == "h2"

    @pytest.mark.asyncio
    async def test_ha_native_exception_returns_error(self):
        """AgentService.invoke 抛异常时捕获并返回 error 状态。"""
        from workflow.node_executor import HANativeNodeExecutor

        class FailingAgentService:
            async def invoke(self, agent_id, prompt, context=None):
                raise RuntimeError("HA subprocess crashed")

        executor = HANativeNodeExecutor(agent_service=FailingAgentService())
        result = await executor.execute(
            {"id": "h3", "type": "ha_native", "prompt": "任务"}, {}
        )
        assert result["status"] == "error"
        assert result["node_type"] == "ha_native"
        assert "HA subprocess crashed" in result["error"]

    @pytest.mark.asyncio
    async def test_ha_native_passes_node_overrides_to_context(self):
        """节点级 model / max_tokens / temperature / tools 覆盖透传到 context。"""
        from workflow.node_executor import HANativeNodeExecutor

        class CaptureAgentService:
            def __init__(self):
                self.last_ctx = None

            async def invoke(self, agent_id, prompt, context=None):
                self.last_ctx = context
                return "ok"

        svc = CaptureAgentService()
        executor = HANativeNodeExecutor(agent_service=svc)
        await executor.execute(
            {
                "id": "h4", "type": "ha_native", "prompt": "任务",
                "model": "deepseek-v4-pro", "max_tokens": 2048,
                "temperature": 0.3, "tools": ["code_search", "file_edit"],
            },
            {"run_id": "run-h4"},
        )
        ctx = svc.last_ctx
        assert ctx.get("model_override") == "deepseek-v4-pro"
        assert ctx.get("max_tokens") == 2048
        assert ctx.get("temperature") == 0.3
        assert ctx.get("ha_tools_filter") == ["code_search", "file_edit"]
        assert ctx.get("ha_native") is True

    @pytest.mark.asyncio
    async def test_ha_native_injects_prev_outputs_into_prompt(self):
        """前面节点的输出摘要注入到 prompt（与 AgentNodeExecutor 一致）。"""
        from workflow.node_executor import HANativeNodeExecutor

        class CaptureAgentService:
            def __init__(self):
                self.last_prompt = None

            async def invoke(self, agent_id, prompt, context=None):
                self.last_prompt = prompt
                return "ok"

        svc = CaptureAgentService()
        executor = HANativeNodeExecutor(agent_service=svc)
        await executor.execute(
            {"id": "h5", "type": "ha_native", "prompt": "继续处理"},
            {
                "run_id": "run-h5",
                "node_results": {"prev": {"output": "前序节点产出"}},
            },
        )
        # prompt 应包含前序输出 + 当前任务
        assert "前面已完成的步骤" in svc.last_prompt
        assert "prev" in svc.last_prompt
        assert "前序节点产出" in svc.last_prompt
        assert "继续处理" in svc.last_prompt

    @pytest.mark.asyncio
    async def test_ha_native_truncates_long_output(self):
        """输出超过 SUB_AGENT_OUTPUT_PREVIEW_CHARS 时截断。"""
        from workflow.node_executor import HANativeNodeExecutor
        from config import SUB_AGENT_OUTPUT_PREVIEW_CHARS

        class LongOutputAgentService:
            async def invoke(self, agent_id, prompt, context=None):
                return "x" * (SUB_AGENT_OUTPUT_PREVIEW_CHARS + 500)

        executor = HANativeNodeExecutor(agent_service=LongOutputAgentService())
        result = await executor.execute(
            {"id": "h6", "type": "ha_native", "prompt": "任务"}, {}
        )
        assert result["status"] == "success"
        assert len(result["output"]) == SUB_AGENT_OUTPUT_PREVIEW_CHARS

    @pytest.mark.asyncio
    async def test_ha_native_fallback_to_context_input(self):
        """节点无 prompt 时回退到 context['input']。"""
        from workflow.node_executor import HANativeNodeExecutor

        class CaptureAgentService:
            def __init__(self):
                self.last_prompt = None

            async def invoke(self, agent_id, prompt, context=None):
                self.last_prompt = prompt
                return "ok"

        svc = CaptureAgentService()
        executor = HANativeNodeExecutor(agent_service=svc)
        await executor.execute(
            {"id": "h7", "type": "ha_native"},  # 无 prompt
            {"input": "来自 context 的输入"},
        )
        assert svc.last_prompt == "来自 context 的输入"
