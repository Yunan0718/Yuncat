"""Phase 87 审查 C-2: resume_workflow 端点 finalize 契约测试。

验证 routes/workflows.py:resume_workflow 在审查修复后的行为：
  1. resume 成功 → workflow_runs.status 被正确 finalize（completed/partial_error），
     不再停留在 resume 前的 paused/failed。
  2. resume 过程中 engine.run 抛异常 → workflow_runs.status='failed'，
     不卡 running，端点返回结构化错误（而非裸 500）。

修复前行为（bug）：
  - 端点不 set running、不 finalize，engine.run 成功后 row 留在 paused/failed；
  - engine.run 抛异常时 FastAPI 直接 500，row 状态不更新。
"""
import json
import os
import sys

import pytest
import aiosqlite

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from repositories.schema import SCHEMA_SQL
from services.workflow.workflow_service import WorkflowService
from workflow.engine import WorkflowEngine
from workflow.node_executor import MockNodeExecutor


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


async def _insert_failed_run(svc: WorkflowService, run_id: str, workflow_id: str, snapshot: dict) -> None:
    """插入一个已失败的工作流运行行（含 snapshot），模拟用户触达 resume 时的初始状态。"""
    import time as _time
    await svc.db.execute(
        "INSERT INTO workflow_runs(id, workflow_id, status, snapshot, started_at) "
        "VALUES(?,?,?,?,?)",
        (run_id, workflow_id, "failed", json.dumps(snapshot), int(_time.time())),
    )
    await svc.db.commit()


class _FakeRequest:
    """最小化的 FastAPI Request mock——只暴露 app.state.db / workflow_service。"""

    class _State:
        def __init__(self, db, svc):
            self.db = db
            self.workflow_service = svc

    def __init__(self, db, svc):
        self.app = type("App", (), {"state": self._State(db, svc)})()


# ============================================================
# 用例 1: resume 成功 → workflow_runs.status 被 finalize 为 completed
# ============================================================
@pytest.mark.asyncio
async def test_resume_success_finalizes_run_status():
    """C-2 修复：resume 成功后 row 状态从 'failed' 更新为 'completed'，
    不再停留在 resume 前的旧状态。"""
    from routes.workflows import resume_workflow

    db, svc = await _init_service()
    nodes = [{"id": "A", "type": "default"}, {"id": "B", "type": "default"}]
    edges = [{"from": "A", "to": "B"}]
    await _create_workflow(svc, "wf-1", nodes, edges)

    # 模拟一次失败运行：A 成功，B 失败
    executor_failed = MockNodeExecutor(responses={
        "A": {"output": "a"},
        "B": {"error": "B boom"},
    })
    engine = WorkflowEngine(repo=svc, executors={"default": executor_failed})
    svc.engine = engine
    run_id = "run-1"
    import time as _t
    await svc.db.execute(
        "INSERT INTO workflow_runs(id, workflow_id, status, started_at) VALUES(?,?,?,?)",
        (run_id, "wf-1", "running", int(_t.time())),
    )
    await svc.db.commit()
    await engine.run("wf-1", "input", run_id=run_id)
    # 引擎本身不改 workflow_runs.status，模拟 _run_and_finalize 的失败 finalize
    await svc.db.execute(
        "UPDATE workflow_runs SET status='failed' WHERE id=?", (run_id,)
    )
    await svc.db.commit()

    snapshot = await engine.recover_from_snapshot(run_id)
    assert snapshot is not None, "前置条件：snapshot 必须存在"

    # resume：B 重试成功
    executor_ok = MockNodeExecutor(responses={"B": {"output": "b-ok"}})
    engine2 = WorkflowEngine(repo=svc, executors={"default": executor_ok})
    svc.engine = engine2  # 端点通过 svc.engine 取引擎

    req = _FakeRequest(db, svc)
    result = await resume_workflow("wf-1", {"run_id": run_id, "action": "retry"}, req)

    # 修复后：返回结构化结果，run_status 为 completed
    assert result["status"] == "ok"
    assert result["run_status"] == "completed"

    # DB 中 row 状态被 finalize
    row = await svc.get_run(run_id)
    assert row["status"] == "completed", "resume 成功后 row 应被 finalize 为 completed"
    assert row["finished_at"] is not None

    await db.close()


# ============================================================
# 用例 2: resume 过程抛异常 → workflow_runs.status='failed'，不卡 running
# ============================================================
@pytest.mark.asyncio
async def test_resume_exception_marks_failed_not_running():
    """C-2 修复：engine.run 抛异常时 row 标记 'failed'（而非卡 'running'），
    端点返回结构化错误而非裸 500。"""
    from routes.workflows import resume_workflow

    db, svc = await _init_service()
    nodes = [{"id": "A", "type": "default"}]
    edges = []
    await _create_workflow(svc, "wf-2", nodes, edges)

    # 准备一个可恢复的 snapshot（A 处于 error 待重试）
    snapshot = {
        "context": {},
        "node_states": {"A": "error"},
        "node_results": {"A": {"error": "previous failure"}},
    }
    run_id = "run-2"
    await _insert_failed_run(svc, run_id, "wf-2", snapshot)

    class _BoomExecutor:
        """永远抛异常的执行器——模拟 resume 时 engine.run 内部节点崩溃。"""

        async def execute(self, node, context):
            raise RuntimeError("resume boom")

    # 用一个会让 engine.run 在 gather 阶段抛出的执行器
    # 注意：execute_node 内部有 try/except 会吞节点异常并返回 error state，
    # 所以要让 engine.run 本身抛异常，需要在节点 execute 之外动手脚。
    # 这里改用 mock engine.run 直接抛异常更精准。
    class _BoomEngine:
        repo = svc

        async def recover_from_snapshot(self, run_id):
            return snapshot

        async def run(self, *args, **kwargs):
            raise RuntimeError("engine boom")

    svc.engine = _BoomEngine()

    req = _FakeRequest(db, svc)
    # 修复前：这里会抛 RuntimeError 导致 FastAPI 500。
    # 修复后：端点 try/except 兜底，返回结构化结果。
    result = await resume_workflow("wf-2", {"run_id": run_id, "action": "retry"}, req)

    assert result["status"] == "ok"
    assert result["run_status"] == "failed"
    assert "error" in result["results"]
    assert "engine boom" in result["results"]["error"]

    # DB 中 row 状态为 failed，不卡 running
    row = await svc.get_run(run_id)
    assert row["status"] == "failed", "异常时 row 应标记 failed，不应卡 running"
    assert row["finished_at"] is not None

    await db.close()


# ============================================================
# 用例 3: resume 前状态被标记为 running（反映进行中）
# ============================================================
@pytest.mark.asyncio
async def test_resume_marks_running_before_execution():
    """C-2 修复：resume 开始时 row 先标记 'running'，
    若引擎同步抛异常则被 except 兜底改写为 failed。此用例验证 running 标记可见。"""
    from routes.workflows import resume_workflow

    db, svc = await _init_service()
    nodes = [{"id": "A", "type": "default"}]
    edges = []
    await _create_workflow(svc, "wf-3", nodes, edges)

    snapshot = {
        "context": {},
        "node_states": {"A": "error"},
        "node_results": {"A": {"error": "prev"}},
    }
    run_id = "run-3"
    await _insert_failed_run(svc, run_id, "wf-3", snapshot)

    seen_status_during_run = []

    class _ObserveEngine:
        repo = svc

        async def recover_from_snapshot(self, run_id):
            return snapshot

        async def run(self, *args, **kwargs):
            # 引擎执行期间观察 row 状态——应为 'running'
            cur = await svc.db.execute(
                "SELECT status FROM workflow_runs WHERE id=?", (run_id,)
            )
            row = await cur.fetchone()
            seen_status_during_run.append(row[0] if row else None)
            return {"status": "completed", "results": {}, "final_output": "ok"}

    svc.engine = _ObserveEngine()
    req = _FakeRequest(db, svc)
    result = await resume_workflow("wf-3", {"run_id": run_id, "action": "retry"}, req)

    assert result["run_status"] == "completed"
    assert seen_status_during_run == ["running"], "引擎执行期间 row 应为 running"

    await db.close()
