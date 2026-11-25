"""harness/task_manager.py —— 13 状态机 + harness_tasks 系列表持久化

设计要点（数据驱动，不硬编码）：
  - TRANSITIONS 迁移表是唯一事实源——新增状态只需改表，无需改分支逻辑。
  - 所有写路径先 validate_transition 再落库；每次迁移写 state_transitions 审计。
  - 幂等：idempotency_key UNIQUE，重复 create_task 返回既有任务。
  - 信任边界：apply_worker_report 只认 Verifier 交叉验证后的 decision，
    Worker 的 claimed_* 与 RuntimeObservation 不一致即 MISMATCH。
"""
from __future__ import annotations

import json as _json
import time as _time
import uuid as _uuid
from pathlib import Path

from harness.schemas import (
    PermissionRequest,
    RuntimeObservation,
    TaskPackage,
    WorkerReport,
)


# ============================================================
# 13 状态机
# ============================================================
class TaskStatus:
    """13 状态——对齐 TDD v2.0。

    PENDING(已提交待排队) → QUEUED → RUNNING →(Verifier)→ VERIFIED → COMPLETED
    运行中可岔出：WAITING_PERMISSION / MISMATCH / BUDGET_EXCEEDED / TIMEOUT / FAILED
    终态：COMPLETED / REJECTED / BLOCKED / CANCELLED
    """

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_PERMISSION = "WAITING_PERMISSION"
    VERIFIED = "VERIFIED"
    MISMATCH = "MISMATCH"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    BLOCKED = "BLOCKED"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"

    ALL = (
        PENDING, QUEUED, RUNNING, WAITING_PERMISSION, VERIFIED, MISMATCH,
        BUDGET_EXCEEDED, BLOCKED, TIMEOUT, FAILED, COMPLETED, REJECTED, CANCELLED,
    )


_TERMINAL = frozenset({TaskStatus.COMPLETED, TaskStatus.REJECTED, TaskStatus.BLOCKED, TaskStatus.CANCELLED})

# 可重试状态：reject_and_reassign 允许从此状态发起重派
_RETRYABLE = frozenset({TaskStatus.MISMATCH, TaskStatus.FAILED, TaskStatus.TIMEOUT, TaskStatus.REJECTED})

TRANSITIONS: dict[str, frozenset[str]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.WAITING_PERMISSION, TaskStatus.VERIFIED, TaskStatus.MISMATCH,
        TaskStatus.BUDGET_EXCEEDED, TaskStatus.TIMEOUT, TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.WAITING_PERMISSION: frozenset({
        TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.CANCELLED,
        # Phase C: 崩溃恢复扫描将 L1 挂起残留重置为 TIMEOUT（可重试）
        TaskStatus.TIMEOUT,
    }),
    TaskStatus.VERIFIED: frozenset({TaskStatus.COMPLETED, TaskStatus.REJECTED}),
    TaskStatus.MISMATCH: frozenset({TaskStatus.QUEUED, TaskStatus.BLOCKED, TaskStatus.REJECTED}),
    TaskStatus.BUDGET_EXCEEDED: frozenset({TaskStatus.BLOCKED, TaskStatus.REJECTED}),
    TaskStatus.TIMEOUT: frozenset({TaskStatus.QUEUED, TaskStatus.BLOCKED, TaskStatus.FAILED}),
    TaskStatus.FAILED: frozenset({TaskStatus.QUEUED, TaskStatus.BLOCKED, TaskStatus.REJECTED}),
    TaskStatus.BLOCKED: frozenset({
        # Phase D: HITL 人工裁决——retry 放行重试（BLOCKED→QUEUED），cancel 人工取消
        TaskStatus.QUEUED, TaskStatus.CANCELLED,
    }),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.REJECTED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def validate_transition(from_status: str, to_status: str) -> None:
    """校验状态迁移合法性。同态迁移幂等放行（不重复写审计）。"""
    if from_status == to_status:
        return
    allowed = TRANSITIONS.get(from_status, frozenset())
    if to_status not in allowed:
        raise ValueError(f"非法状态迁移: {from_status} -> {to_status}")


def is_terminal(status: str) -> bool:
    return status in _TERMINAL


def is_retryable(status: str) -> bool:
    return status in _RETRYABLE


# ============================================================
# 行 ↔ dict 转换
# ============================================================
def _row_to_task(row) -> dict:
    return {
        "id": row["id"],
        "idempotency_key": row["idempotency_key"],
        "supervisor_agent_id": row["supervisor_agent_id"],
        "worker_agent_id": row["worker_agent_id"],
        "mission": row["mission"],
        "acceptance_criteria": _json.loads(row["acceptance_criteria"] or "[]"),
        "constraints": _json.loads(row["constraints"] or "[]"),
        "non_goals": _json.loads(row["non_goals"] or "[]"),
        "permissions": _json.loads(row["permissions"] or "{}"),
        "execution_budget": _json.loads(row["execution_budget"] or "{}"),
        "attempt": row["attempt"],
        "max_attempts": row["max_attempts"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def _log_transition(conn, task_id: str, from_status: str | None, to_status: str, reason: str) -> None:
    await conn.execute(
        "INSERT INTO state_transitions(task_id, from_status, to_status, reason, created_at) "
        "VALUES(?,?,?,?,?)",
        (task_id, from_status, to_status, reason, int(_time.time())),
    )


# ============================================================
# 任务 CRUD
# ============================================================
async def create_task(conn, pkg: TaskPackage) -> dict:
    """落库 TaskPackage。幂等：同 idempotency_key 返回既有任务，不重复插入。"""
    existing = await get_task_by_idempotency(conn, pkg.idempotency_key)
    if existing is not None:
        return existing
    await conn.execute(
        "INSERT INTO harness_tasks(id, idempotency_key, supervisor_agent_id, worker_agent_id, "
        "mission, acceptance_criteria, constraints, non_goals, permissions, execution_budget, "
        "attempt, max_attempts, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            pkg.id, pkg.idempotency_key, pkg.supervisor_agent_id, pkg.worker_agent_id,
            pkg.mission,
            _json.dumps(pkg.acceptance_criteria, ensure_ascii=False),
            _json.dumps(pkg.constraints, ensure_ascii=False),
            _json.dumps(pkg.non_goals, ensure_ascii=False),
            pkg.permissions.model_dump_json(),
            pkg.execution_budget.model_dump_json(),
            pkg.attempt, pkg.max_attempts, pkg.status, pkg.created_at,
        ),
    )
    await _log_transition(conn, pkg.id, None, pkg.status, "task created")
    await conn.commit()
    return await get_task(conn, pkg.id)


async def get_task(conn, task_id: str) -> dict | None:
    cursor = await conn.execute("SELECT * FROM harness_tasks WHERE id=?", (task_id,))
    row = await cursor.fetchone()
    return _row_to_task(row) if row else None


async def get_task_by_idempotency(conn, key: str) -> dict | None:
    cursor = await conn.execute("SELECT * FROM harness_tasks WHERE idempotency_key=?", (key,))
    row = await cursor.fetchone()
    return _row_to_task(row) if row else None


async def transition(conn, task_id: str, to_status: str, reason: str = "") -> dict:
    """状态迁移——先校验后落库，写 state_transitions 审计。返回迁移后任务。"""
    task = await get_task(conn, task_id)
    if task is None:
        raise KeyError(f"任务不存在: {task_id}")
    validate_transition(task["status"], to_status)
    await conn.execute(
        "UPDATE harness_tasks SET status=?, updated_at=? WHERE id=?",
        (to_status, int(_time.time()), task_id),
    )
    await _log_transition(conn, task_id, task["status"], to_status, reason)
    await conn.commit()
    return await get_task(conn, task_id)


async def get_state_transitions(conn, task_id: str) -> list[dict]:
    """按时间序返回任务的状态迁移审计。"""
    cursor = await conn.execute(
        "SELECT from_status, to_status, reason, created_at FROM state_transitions "
        "WHERE task_id=? ORDER BY created_at, id",
        (task_id,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ============================================================
# Attempts（执行尝试）
# ============================================================
async def record_attempt(conn, task_id: str, attempt_number: int, worker_agent_id: str = "") -> str:
    """记录一次执行尝试，返回 attempt_id。"""
    aid = f"att_{_uuid.uuid4().hex[:12]}"
    await conn.execute(
        "INSERT INTO attempts(id, task_id, attempt_number, worker_agent_id, status, started_at) "
        "VALUES(?,?,?,?,?,?)",
        (aid, task_id, attempt_number, worker_agent_id, "RUNNING", int(_time.time())),
    )
    await conn.commit()
    return aid


async def finish_attempt(conn, attempt_id: str, status: str, error_message: str = "") -> None:
    await conn.execute(
        "UPDATE attempts SET status=?, finished_at=?, error_message=? WHERE id=?",
        (status, int(_time.time()), error_message, attempt_id),
    )
    await conn.commit()


# ============================================================
# Worker 汇报 + Verifier（Phase B：git diff + hash 客观取证）
# ============================================================
async def apply_worker_report(
    conn,
    report: WorkerReport,
    observation: RuntimeObservation | None = None,
    *,
    workspace: Path | None = None,
    runner=None,
) -> dict:
    """Worker 汇报入口——Verifier 交叉验证后记录 execution 并决策 VERIFIED / MISMATCH。

    Phase B（替换 v0 注入式 actual）：observation 缺失时经
    harness.verifier.collect_observation 客观取证（git diff + hash + 测试退出码，
    非 git 工作区 fail-closed）；两者皆缺 → ValueError。
    决策由 verifier.verify：claimed 集合 == actual 集合 且 退出码一致 → VERIFIED，
    否则 MISMATCH → Supervisor 必须 reject_and_reassign。
    """
    from harness.verifier import collect_observation, verify as verifier_verify

    task = await get_task(conn, report.task_id)
    if task is None:
        raise KeyError(f"任务不存在: {report.task_id}")
    if task["status"] != TaskStatus.RUNNING:
        raise ValueError(f"仅 RUNNING 状态可提交汇报，当前: {task['status']}")

    if observation is None:
        if workspace is None:
            raise ValueError("observation 与 workspace 至少提供一个——Verifier 需要取证来源（fail-closed）")
        observation = await collect_observation(workspace, runner=runner)
    decision = verifier_verify(report, observation)
    observation.verifier_decision = decision

    exec_id = f"exe_{_uuid.uuid4().hex[:12]}"
    await conn.execute(
        "INSERT INTO executions(id, task_id, attempt_id, claimed_files_modified, "
        "actual_files_modified, claimed_test_result, test_exit_code, verifier_decision, "
        "summary, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            exec_id, report.task_id, report.attempt_id,
            _json.dumps(report.claimed_files_modified),
            _json.dumps(observation.actual_files_modified),
            report.claimed_test_result, observation.test_exit_code, decision,
            report.summary, int(_time.time()),
        ),
    )
    await conn.commit()
    task = await transition(conn, report.task_id, decision, f"Verifier: {decision}")
    return {"id": exec_id, "task_id": report.task_id, "verifier_decision": decision, "status": task["status"]}


async def get_executions(conn, task_id: str) -> list[dict]:
    cursor = await conn.execute(
        "SELECT * FROM executions WHERE task_id=? ORDER BY created_at", (task_id,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ============================================================
# 权限请求（L1 审批）
# ============================================================
async def record_permission_request(conn, req: PermissionRequest) -> dict:
    await conn.execute(
        "INSERT INTO permission_requests(id, task_id, attempt_id, tool_name, arguments, "
        "path_canonical, level, status, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            req.id, req.task_id, req.attempt_id, req.tool_name,
            _json.dumps(req.arguments, ensure_ascii=False),
            req.path_canonical, req.level, req.status, req.created_at,
        ),
    )
    await conn.commit()
    return {"id": req.id, "task_id": req.task_id, "status": req.status}


async def get_permission_request(conn, request_id: str) -> dict | None:
    """读取权限请求（approve_permission 放行前过 policy 硬闸需要）。"""
    cursor = await conn.execute("SELECT * FROM permission_requests WHERE id=?", (request_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def decide_permission(conn, request_id: str, allow: bool, decided_by: str) -> dict:
    """裁决 L1 请求。返回 {"id", "task_id", "status"}，task_id 为 None 表示请求不存在。"""
    status = "APPROVED" if allow else "REJECTED"
    cursor = await conn.execute(
        "UPDATE permission_requests SET status=?, decided_by=?, decided_at=? WHERE id=?",
        (status, decided_by, int(_time.time()), request_id),
    )
    if cursor.rowcount == 0:
        return {"id": request_id, "task_id": None, "status": status}
    await conn.commit()
    cursor = await conn.execute("SELECT task_id FROM permission_requests WHERE id=?", (request_id,))
    row = await cursor.fetchone()
    return {"id": request_id, "task_id": row["task_id"] if row else None, "status": status}


# ============================================================
# Supervisor 重派（attempt 递增 + 熔断）
# ============================================================
async def increment_attempt(conn, task_id: str) -> dict:
    """重派前 attempt+1；超过 max_attempts 熔断为 BLOCKED（转 HITL）。

    语义：max_attempts = 含初始执行的总尝试次数。
    初始执行占 1 次（attempt=0），此后每次失败重派 +1；
    已失败次数 + 1 >= max_attempts 时不再重试，熔断 BLOCKED。
    例：max_attempts=3 → 初始 + 2 次重试，第 3 次失败熔断（对齐 TDD"同因连续 3 次"）。
    """
    task = await get_task(conn, task_id)
    if task is None:
        raise KeyError(f"任务不存在: {task_id}")
    next_attempt = task["attempt"] + 1
    await conn.execute(
        "UPDATE harness_tasks SET attempt=? WHERE id=?", (next_attempt, task_id),
    )
    await conn.commit()
    if task["attempt"] + 1 >= task["max_attempts"]:
        return await transition(conn, task_id, TaskStatus.BLOCKED, "attempt 耗尽，熔断转 HITL")
    return await transition(conn, task_id, TaskStatus.QUEUED, f"重新指派 attempt={next_attempt}")
