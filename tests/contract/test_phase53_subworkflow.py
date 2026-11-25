"""Phase 53: 子工作流嵌套契约测试。"""
import asyncio
import json
import pytest

import aiosqlite

from repositories.schema import SCHEMA_SQL
from services.workflow.workflow_service import WorkflowService
from workflow.engine import WorkflowEngine
from workflow.node_executor import MockNodeExecutor, SubWorkflowExecutor


async def _init_service():
    """创建带真实 schema 的内存 WorkflowService。"""
    db = await aiosqlite.connect(":memory:")
    await db.executescript(SCHEMA_SQL)
    await db.commit()
    svc = WorkflowService(db)
    return db, svc


def _workflow(wf_id: str, nodes: list, edges: list):
    return {"id": wf_id, "name": wf_id, "graph": {"nodes": nodes, "edges": edges}}


async def _create_workflow(svc: WorkflowService, wf_id: str, nodes: list, edges: list) -> None:
    await svc.create(_workflow(wf_id, nodes, edges))


async def _insert_run(svc: WorkflowService, run_id: str, workflow_id: str) -> None:
    """直接创建 workflow_runs 行，模拟 WorkflowService.run 的起始行为。"""
    import time as _time
    await svc.db.execute(
        "INSERT INTO workflow_runs(id, workflow_id, status, started_at) VALUES(?,?,?,?)",
        (run_id, workflow_id, "running", int(_time.time())),
    )
    await svc.db.commit()


@pytest.mark.asyncio
async def test_subworkflow_node_runs_child():
    """父工作流的 sub_workflow 节点能同步运行子工作流并返回结果。"""
    db, svc = await _init_service()
    agent_exec = MockNodeExecutor(responses={
        "start": {"output": "start-done"},
        "a": {"output": "child-a-done"},
        "b": {"output": "child-b-done"},
        "end": {"output": "end-done"},
    })
    sub_exec = SubWorkflowExecutor(svc)
    engine = WorkflowEngine(repo=svc, executors={"agent": agent_exec, "sub_workflow": sub_exec})
    svc.engine = engine

    child_nodes = [
        {"id": "a", "type": "agent"},
        {"id": "b", "type": "agent"},
    ]
    child_edges = [{"from": "a", "to": "b"}]
    await _create_workflow(svc, "child-wf", child_nodes, child_edges)

    parent_nodes = [
        {"id": "start", "type": "agent"},
        {"id": "sub", "type": "sub_workflow", "sub_workflow_id": "child-wf", "prompt": "child-input"},
        {"id": "end", "type": "agent"},
    ]
    parent_edges = [{"from": "start", "to": "sub"}, {"from": "sub", "to": "end"}]
    await _create_workflow(svc, "parent-wf", parent_nodes, parent_edges)

    run_id = "parent-run-1"
    await _insert_run(svc, run_id, "parent-wf")
    result = await engine.run("parent-wf", "hello", run_id=run_id)

    assert result["status"] == "completed"
    assert result["results"]["sub"]["output"] == "child-b-done"
    assert result["final_output"] == "end-done"

    child_run_id = f"{run_id}:sub"
    child_run = await svc.get_run(child_run_id)
    assert child_run is not None
    assert child_run["parent_run_id"] == run_id
    assert child_run["parent_node_id"] == "sub"
    assert child_run["workflow_id"] == "child-wf"

    await db.close()


class _GateExecutor:
    """在指定节点阻塞，测试可控制放行。"""

    def __init__(self, gate_nodes: list[str]):
        self.gates: dict[str, asyncio.Event] = {n: asyncio.Event() for n in gate_nodes}
        self.executed: list[str] = []

    async def execute(self, node: dict, context: dict) -> dict:
        node_id = node["id"]
        self.executed.append(node_id)
        gate = self.gates.get(node_id)
        if gate:
            await gate.wait()
        return {"output": f"out-{node_id}"}

    def release(self, node_id: str) -> None:
        gate = self.gates.get(node_id)
        if gate:
            gate.set()


@pytest.mark.asyncio
async def test_subworkflow_pause_propagates_to_parent():
    """子工作流 paused 时，父工作流能暂停并在恢复后继续。"""
    db, svc = await _init_service()
    gate = _GateExecutor(gate_nodes=["b"])
    sub_exec = SubWorkflowExecutor(svc)
    engine = WorkflowEngine(repo=svc, executors={"agent": gate, "sub_workflow": sub_exec})
    svc.engine = engine

    child_nodes = [
        {"id": "a", "type": "agent"},
        {"id": "b", "type": "agent"},
        {"id": "c", "type": "agent"},
    ]
    child_edges = [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]
    await _create_workflow(svc, "child-wf", child_nodes, child_edges)

    parent_nodes = [
        {"id": "sub", "type": "sub_workflow", "sub_workflow_id": "child-wf"},
        {"id": "end", "type": "agent"},
    ]
    parent_edges = [{"from": "sub", "to": "end"}]
    await _create_workflow(svc, "parent-wf", parent_nodes, parent_edges)

    parent_run_id = "parent-run-2"
    child_run_id = f"{parent_run_id}:sub"
    await _insert_run(svc, parent_run_id, "parent-wf")
    task = asyncio.create_task(engine.run("parent-wf", "hello", run_id=parent_run_id))

    # 等待子节点 b 开始执行（说明 a 已完成），然后请求暂停子运行
    while "b" not in gate.executed:
        await asyncio.sleep(0)
    engine.pause(child_run_id)

    # 放行 b；b 完成后子引擎应暂停，父引擎随之暂停
    gate.release("b")
    parent_result = await asyncio.wait_for(task, timeout=2.0)

    assert parent_result["status"] == "paused"
    assert parent_result["results"]["sub"]["status"] == "paused"
    child_run = await svc.get_run(child_run_id)
    assert child_run["status"] == "paused"

    # 放行 c，恢复父工作流
    gate.release("c")
    resumed = await asyncio.wait_for(engine.resume(parent_run_id), timeout=2.0)

    assert resumed["status"] == "completed"
    assert resumed["results"]["sub"]["status"] == "completed"
    assert resumed["results"]["sub"]["output"] == "out-c"
    assert resumed["results"]["end"]["output"] == "out-end"

    await db.close()
