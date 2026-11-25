"""tests/test_harness_phase_a.py —— Supervisor–Worker Harness Phase A 验收

验收标准（docs/harness-supervisor-worker.md Phase A）：
  1. TaskPackage / PermissionRequest / WorkerReport 序列化 round-trip
  2. 13 状态机：非法迁移拒绝、合法迁移通过、终态判定
  3. assign_task 同 idempotency_key 幂等（不重复执行）
  4. 单轮 happy path：PENDING→QUEUED→RUNNING→VERIFIED→COMPLETED 端到端
  5. Verifier v0：claimed 与事实不符 → MISMATCH → reject_and_reassign 重派
  6. attempt 耗尽 → 熔断 BLOCKED（转 HITL 边界）
  7. supervisor 工具经 context.get_db 落库（无 FastAPI 依赖，数据驱动）
"""
import json
from types import SimpleNamespace

import pytest

from context import set_app
from harness import task_manager as tm
from harness.schemas import (
    ExecutionBudget,
    HarnessPermissions,
    PermissionRequest,
    RuntimeObservation,
    TaskPackage,
    WorkerReport,
)
from harness.supervisor_tools import (
    accept_result,
    approve_permission,
    assign_task,
    reject_and_reassign,
)
from repositories import init_db
from repositories.harness_schema import ensure_harness_schema


def make_package(**kw) -> TaskPackage:
    base = dict(
        idempotency_key="k-happy",
        mission="修复登录页按钮样式",
        acceptance_criteria=["按钮颜色正确", "点击无回归"],
        constraints=["不修改后端代码"],
        non_goals=["重构组件库"],
        permissions=HarnessPermissions(
            tools=["read_file", "write_file", "run_command"],
            write_paths=["src-frontend/src/components"],
        ),
        execution_budget=ExecutionBudget(max_tool_invocations=20, max_tokens=10000),
    )
    base.update(kw)
    return TaskPackage(**base)


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(str(tmp_path / "harness_test.db"))
    await ensure_harness_schema(conn)
    yield conn
    await conn.close()


@pytest.fixture
def app_ctx(db):
    app = SimpleNamespace(state=SimpleNamespace(db=db))
    set_app(app)
    yield app
    set_app(None)


# ═══════════════════════════════════════════════════════════
# 1. 序列化 round-trip
# ═══════════════════════════════════════════════════════════
def test_task_package_roundtrip():
    pkg = make_package()
    restored = TaskPackage.model_validate_json(pkg.model_dump_json())
    assert restored.id == pkg.id
    assert restored.mission == "修复登录页按钮样式"
    assert restored.permissions.write_paths == ["src-frontend/src/components"]
    assert restored.execution_budget.max_tool_invocations == 20
    assert restored.status == "PENDING"


def test_worker_report_and_observation_roundtrip():
    report = WorkerReport(task_id="ht_x", attempt_id="att_x", claimed_files_modified=["a.ts"])
    obs = RuntimeObservation(actual_files_modified=["a.ts"], test_exit_code=0)
    assert WorkerReport.model_validate_json(report.model_dump_json()).claimed_files_modified == ["a.ts"]
    assert RuntimeObservation.model_validate_json(obs.model_dump_json()).test_exit_code == 0


def test_task_package_rejects_empty_mission():
    with pytest.raises(Exception):
        TaskPackage(idempotency_key="k", mission="")


# ═══════════════════════════════════════════════════════════
# 2. 13 状态机规则
# ═══════════════════════════════════════════════════════════
def test_state_machine_count():
    assert len(tm.TaskStatus.ALL) == 13


def test_valid_transition_passes():
    tm.validate_transition(tm.TaskStatus.PENDING, tm.TaskStatus.QUEUED)
    tm.validate_transition(tm.TaskStatus.RUNNING, tm.TaskStatus.MISMATCH)
    tm.validate_transition(tm.TaskStatus.VERIFIED, tm.TaskStatus.COMPLETED)


def test_invalid_transition_raises():
    with pytest.raises(ValueError):
        tm.validate_transition(tm.TaskStatus.COMPLETED, tm.TaskStatus.RUNNING)
    # RUNNING 不可直达 COMPLETED——必须经 Verifier（VERIFIED）
    with pytest.raises(ValueError):
        tm.validate_transition(tm.TaskStatus.RUNNING, tm.TaskStatus.COMPLETED)


def test_terminal_and_retryable():
    assert tm.is_terminal(tm.TaskStatus.COMPLETED)
    assert tm.is_terminal(tm.TaskStatus.BLOCKED)
    assert not tm.is_terminal(tm.TaskStatus.RUNNING)
    assert tm.is_retryable(tm.TaskStatus.MISMATCH)
    assert not tm.is_retryable(tm.TaskStatus.RUNNING)


# ═══════════════════════════════════════════════════════════
# 3. 幂等
# ═══════════════════════════════════════════════════════════
async def test_create_task_idempotent(db):
    first = await tm.create_task(db, make_package())
    second = await tm.create_task(db, make_package())
    assert first["id"] == second["id"]
    cursor = await db.execute("SELECT COUNT(*) FROM harness_tasks")
    row = await cursor.fetchone()
    assert row[0] == 1


# ═══════════════════════════════════════════════════════════
# 4. happy path 端到端
# ═══════════════════════════════════════════════════════════
async def test_happy_path(db):
    task = await tm.create_task(db, make_package())
    assert task["status"] == tm.TaskStatus.PENDING

    task = await tm.transition(db, task["id"], tm.TaskStatus.QUEUED, "调度器入队")
    task = await tm.transition(db, task["id"], tm.TaskStatus.RUNNING, "派发 Worker")
    assert task["status"] == tm.TaskStatus.RUNNING

    attempt_id = await tm.record_attempt(db, task["id"], 1, worker_agent_id="worker-1")
    report = WorkerReport(
        task_id=task["id"], attempt_id=attempt_id,
        summary="已修复按钮样式",
        claimed_files_modified=["src-frontend/src/components/Button.tsx"],
        claimed_test_result="pytest 通过", test_exit_code=0,
    )
    obs = RuntimeObservation(
        actual_files_modified=["src-frontend/src/components/Button.tsx"], test_exit_code=0,
    )
    result = await tm.apply_worker_report(db, report, obs)
    assert result["verifier_decision"] == tm.TaskStatus.VERIFIED

    done = await tm.transition(db, task["id"], tm.TaskStatus.COMPLETED, "Supervisor 接受结果")
    assert done["status"] == tm.TaskStatus.COMPLETED

    # 迁移审计可查
    cursor = await db.execute(
        "SELECT COUNT(*) FROM state_transitions WHERE task_id=?", (task["id"],),
    )
    row = await cursor.fetchone()
    assert row[0] == 5  # created + 4 次显式迁移（QUEUED/RUNNING/VERIFIED/COMPLETED）


# ═══════════════════════════════════════════════════════════
# 5. Verifier v0：MISMATCH → 重派
# ═══════════════════════════════════════════════════════════
async def test_verifier_mismatch_then_reassign(db):
    task = await tm.create_task(db, make_package(idempotency_key="k-mismatch"))
    await tm.transition(db, task["id"], tm.TaskStatus.QUEUED)
    await tm.transition(db, task["id"], tm.TaskStatus.RUNNING)
    attempt_id = await tm.record_attempt(db, task["id"], 1)

    # Worker 谎报改了文件（实际没改）→ MISMATCH
    report = WorkerReport(
        task_id=task["id"], attempt_id=attempt_id,
        claimed_files_modified=["src-frontend/src/components/Button.tsx"],
    )
    obs = RuntimeObservation(actual_files_modified=[])
    result = await tm.apply_worker_report(db, report, obs)
    assert result["verifier_decision"] == tm.TaskStatus.MISMATCH

    task = await tm.get_task(db, task["id"])
    assert task["status"] == tm.TaskStatus.MISMATCH
    assert task["attempt"] == 0  # 重派前 attempt 未增

    # Supervisor 驳回重派 → QUEUED，attempt=1
    task = await tm.increment_attempt(db, task["id"])
    assert task["status"] == tm.TaskStatus.QUEUED
    assert task["attempt"] == 1


# ═══════════════════════════════════════════════════════════
# 6. attempt 耗尽 → BLOCKED
# ═══════════════════════════════════════════════════════════
async def test_attempt_exhaustion_blocks(db):
    task = await tm.create_task(db, make_package(idempotency_key="k-block", max_attempts=1))
    await tm.transition(db, task["id"], tm.TaskStatus.QUEUED)
    await tm.transition(db, task["id"], tm.TaskStatus.RUNNING)
    await tm.transition(db, task["id"], tm.TaskStatus.MISMATCH, "Verifier 驳回")

    task = await tm.increment_attempt(db, task["id"])
    assert task["status"] == tm.TaskStatus.BLOCKED
    assert task["attempt"] == 1
    assert tm.is_terminal(task["status"])


# ═══════════════════════════════════════════════════════════
# 7. supervisor 工具（经 context.get_db，数据驱动）
# ═══════════════════════════════════════════════════════════
async def test_assign_task_tool_idempotent(app_ctx):
    payload = dict(
        mission="写一个函数 is_prime",
        idempotency_key="k-tool-1",
        acceptance_criteria=["正确判断素数"],
        permissions={"tools": ["write_file", "run_command"], "write_paths": ["src-python/utils"]},
        execution_budget={"max_tool_invocations": 10},
    )
    first = await assign_task(**payload)
    second = await assign_task(**payload)
    assert first["success"] is True
    assert second["success"] is True
    assert first["data"]["task_id"] == second["data"]["task_id"]
    assert first["data"]["idempotent"] is False
    assert second["data"]["idempotent"] is True
    assert first["data"]["status"] == "PENDING"


async def test_assign_task_tool_rejects_invalid_budget(app_ctx):
    result = await assign_task(
        mission="x", idempotency_key="k-bad",
        execution_budget={"max_tool_invocations": 0},  # ge=1 校验失败
    )
    assert result["success"] is False


async def test_accept_result_requires_verified(app_ctx):
    # RUNNING 状态直接 approved → 拒绝（Verifier 未通过）
    first = await assign_task(mission="x", idempotency_key="k-accept")
    task_id = first["data"]["task_id"]
    await tm.transition(app_ctx.state.db, task_id, tm.TaskStatus.QUEUED)
    await tm.transition(app_ctx.state.db, task_id, tm.TaskStatus.RUNNING)

    result = await accept_result(task_id=task_id, verdict="approved")
    assert result["success"] is False
    assert "VERIFIED" in result["error"]


async def test_full_supervisor_loop_via_tools(app_ctx):
    """端到端走 4 个工具：assign → 执行 → 汇报 → accept。"""
    db = app_ctx.state.db
    payload = dict(mission="修复 README 拼写", idempotency_key="k-loop", max_attempts=2)
    assigned = await assign_task(**payload)
    task_id = assigned["data"]["task_id"]

    await tm.transition(db, task_id, tm.TaskStatus.QUEUED)
    await tm.transition(db, task_id, tm.TaskStatus.RUNNING)
    attempt_id = await tm.record_attempt(db, task_id, 1, "worker-9")
    report = WorkerReport(
        task_id=task_id, attempt_id=attempt_id,
        claimed_files_modified=["README.md"], test_exit_code=0,
    )
    await tm.apply_worker_report(db, report, RuntimeObservation(actual_files_modified=["README.md"], test_exit_code=0))

    accepted = await accept_result(task_id=task_id, verdict="approved", reason="验收通过")
    assert accepted["success"] is True
    assert accepted["data"]["status"] == tm.TaskStatus.COMPLETED

    # 回归：重派已被终态拦截
    rejected = await reject_and_reassign(task_id=task_id, reason="再验证一下")
    assert rejected["success"] is False
    assert "不允许重派" in rejected["error"]


async def test_approve_permission_flow(app_ctx, tmp_path, monkeypatch):
    db = app_ctx.state.db
    # Phase B：approve 前过 policy 硬闸（verify_approval），隔离 workspace 使路径判定可测
    monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
    task = await tm.create_task(db, make_package(idempotency_key="k-perm"))
    await tm.transition(db, task["id"], tm.TaskStatus.QUEUED)
    await tm.transition(db, task["id"], tm.TaskStatus.RUNNING)
    attempt_id = await tm.record_attempt(db, task["id"], 1)

    req = PermissionRequest(
        task_id=task["id"], attempt_id=attempt_id, tool_name="write_file",
        arguments={"path": "src-frontend/src/components/Button.tsx"},
        path_canonical="src-frontend/src/components/Button.tsx",
        level=3,
    )
    recorded = await tm.record_permission_request(db, req)
    await tm.transition(db, task["id"], tm.TaskStatus.WAITING_PERMISSION, "越权请求")

    # Supervisor 批准 → 任务回 RUNNING
    result = await approve_permission(permission_request_id=recorded["id"], allow=True, reason="业务需要")
    assert result["success"] is True
    assert result["data"]["status"] == "APPROVED"
    task = await tm.get_task(db, task["id"])
    assert task["status"] == tm.TaskStatus.RUNNING

    # 拒绝路径
    req2 = PermissionRequest(task_id=task["id"], attempt_id=attempt_id, tool_name="run_command", arguments={})
    recorded2 = await tm.record_permission_request(db, req2)
    result2 = await approve_permission(permission_request_id=recorded2["id"], allow=False, reason="不允许")
    assert result2["success"] is True
    assert result2["data"]["status"] == "REJECTED"


async def test_state_transitions_audited(db):
    task = await tm.create_task(db, make_package(idempotency_key="k-audit"))
    await tm.transition(db, task["id"], tm.TaskStatus.QUEUED)
    rows = await tm.get_state_transitions(db, task["id"])
    assert [r["to_status"] for r in rows] == ["PENDING", "QUEUED"]
