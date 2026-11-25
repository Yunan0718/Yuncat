"""harness/supervisor_tools.py —— Supervisor 角色 5 工具（数据驱动注册）

约束（docs/harness-supervisor-worker.md §3.1）：
  - 工具通过 @tool(agents=[SUPERVISOR_AGENT_ID]) 注册，
    ToolService.execute() 的 agent 白名单（硬约束 1）即强制边界。
  - 禁止在任何 agent 循环里写 if tool_name in (...) 分支。
  - handler 一律返回 ToolResult（统一契约），错误不外抛。
"""
from __future__ import annotations

import json as _json

from config import SUPERVISOR_AGENT_ID
from context import get_db
from harness.schemas import (
    ExecutionBudget,
    HarnessPermissions,
    TaskPackage,
)
from harness.task_manager import (
    TaskStatus,
    create_task,
    decide_permission,
    get_permission_request,
    get_task,
    get_task_by_idempotency,
    increment_attempt,
    is_retryable,
    transition,
)
from tools.registry import tool
from tools.result import ToolResult

_TOOL_GROUP = ["harness"]
# Phase C: 超时翻倍上限封顶（防无限膨胀）——relax_timeout 每次 ×2，最多 24h
_BUDGET_RELAX_CAP_SEC = 86400


def _db_ready() -> tuple:
    """获取 db 连接。返回 (db, error_str)。"""
    db = get_db()
    if db is None:
        return None, "数据库未就绪（后端未启动或测试未注入 app 上下文）"
    return db, ""


def _err(tool_name: str, error: str) -> dict:
    return ToolResult(success=False, error=error, meta={"tool_name": tool_name}).to_dict()


# ════════════════════════════════════════════════════════════
# 1. assign_task —— 派发任务（幂等）
# ════════════════════════════════════════════════════════════
@tool(
    name="assign_task",
    agents=[SUPERVISOR_AGENT_ID],
    description=(
        "Supervisor 派发任务给 Worker：构造 TaskPackage 落库（状态 PENDING）。"
        "幂等——同 idempotency_key 重复调用返回既有任务，不重复执行。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "mission": {"type": "string", "description": "任务目标（必填，一句话说清要 Worker 完成什么）"},
            "idempotency_key": {"type": "string", "description": "幂等键（必填，同键重复派发不重复执行）"},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "description": "验收标准列表"},
            "constraints": {"type": "array", "items": {"type": "string"}, "description": "硬约束列表"},
            "non_goals": {"type": "array", "items": {"type": "string"}, "description": "明确不做的范围"},
            "permissions": {
                "type": "object",
                "description": "权限边界：tools / write_paths / read_paths / allow_network / deny_rules",
                "properties": {
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "write_paths": {"type": "array", "items": {"type": "string"}},
                    "read_paths": {"type": "array", "items": {"type": "string"}},
                    "allow_network": {"type": "boolean"},
                    "deny_rules": {"type": "array", "items": {"type": "string"}},
                },
            },
            "execution_budget": {
                "type": "object",
                "description": "执行预算：max_tool_invocations / max_tokens / max_seconds（缺省不限）",
                "properties": {
                    "max_tool_invocations": {"type": "integer"},
                    "max_tokens": {"type": "integer"},
                    "max_seconds": {"type": "integer"},
                },
            },
            "max_attempts": {"type": "integer", "description": "最大重试次数，默认 3，耗尽熔断 BLOCKED"},
        },
        "required": ["mission", "idempotency_key"],
    },
    is_concurrency_safe=False,
    groups=_TOOL_GROUP,
)
async def assign_task(
    mission: str,
    idempotency_key: str,
    acceptance_criteria: list[str] | None = None,
    constraints: list[str] | None = None,
    non_goals: list[str] | None = None,
    permissions: dict | None = None,
    execution_budget: dict | None = None,
    max_attempts: int = 3,
) -> dict:
    db, err = _db_ready()
    if db is None:
        return _err("assign_task", err)
    try:
        pkg = TaskPackage(
            idempotency_key=idempotency_key,
            mission=mission,
            acceptance_criteria=acceptance_criteria or [],
            constraints=constraints or [],
            non_goals=non_goals or [],
            permissions=HarnessPermissions(**(permissions or {})),
            execution_budget=ExecutionBudget(**(execution_budget or {})),
            max_attempts=max_attempts,
            supervisor_agent_id=SUPERVISOR_AGENT_ID,
        )
    except Exception as e:  # Pydantic 校验失败（如 max_attempts<=0）
        return _err("assign_task", f"TaskPackage 校验失败: {e}")

    existing = await get_task_by_idempotency(db, idempotency_key)
    task = await create_task(db, pkg)
    return ToolResult(
        success=True,
        data={"task_id": task["id"], "status": task["status"], "idempotent": existing is not None},
        observation=(
            f"任务已排队（{'幂等命中，未重复执行' if existing else '新建'}）: {task['id']}"
            f" | 状态: {task['status']}"
        ),
        meta={"tool_name": "assign_task"},
    ).to_dict()


# ════════════════════════════════════════════════════════════
# 2. approve_permission —— L1 权限裁决
# ════════════════════════════════════════════════════════════
@tool(
    name="approve_permission",
    agents=[SUPERVISOR_AGENT_ID],
    description=(
        "Supervisor 裁决 L1 权限请求（Worker 越权时进入 WAITING_PERMISSION）。"
        "allow=True 批准后任务回到 RUNNING；最终仍过 Policy Engine 硬闸——"
        "路径越出 workspace 的请求即使批准也会被硬性拒绝（Phase B）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "permission_request_id": {"type": "string", "description": "权限请求 ID"},
            "allow": {"type": "boolean", "description": "true=批准，false=拒绝"},
            "reason": {"type": "string", "description": "裁决理由"},
        },
        "required": ["permission_request_id", "allow"],
    },
    is_concurrency_safe=False,
    groups=_TOOL_GROUP,
)
async def approve_permission(permission_request_id: str, allow: bool, reason: str = "") -> dict:
    db, err = _db_ready()
    if db is None:
        return _err("approve_permission", err)
    req = await get_permission_request(db, permission_request_id)
    if req is None:
        return _err("approve_permission", f"权限请求不存在: {permission_request_id}")

    if allow:
        # Phase B 硬闸：最终放行仍过 policy_engine——即使 Supervisor 批准，
        # 路径越出 workspace 也硬性拒绝并记违规（approve 救不回越权）。
        from harness.policy_engine import verify_approval
        from security.sandbox import get_workspace
        ok, why = verify_approval(req.get("path_canonical"), get_workspace())
        if not ok:
            return _err(
                "approve_permission",
                f"路径越界，审批被硬性拒绝（违规事件）: {why}",
            )
        result = await decide_permission(db, permission_request_id, True, SUPERVISOR_AGENT_ID)
        task = await transition(db, req["task_id"], TaskStatus.RUNNING, f"权限已批准: {reason}")
        result["task_status"] = task["status"]
    else:
        result = await decide_permission(db, permission_request_id, False, SUPERVISOR_AGENT_ID)
        result["task_status"] = req["task_id"]
    return ToolResult(
        success=True,
        data=result,
        observation=f"权限请求 {permission_request_id} 已{'批准' if allow else '拒绝'}",
        meta={"tool_name": "approve_permission"},
    ).to_dict()


# ════════════════════════════════════════════════════════════
# 3. reject_and_reassign —— 驳回重派（attempt 递增 + 熔断）
# ════════════════════════════════════════════════════════════
@tool(
    name="reject_and_reassign",
    agents=[SUPERVISOR_AGENT_ID],
    description=(
        "Supervisor 驳回 Worker 结果并重新指派：attempt+1 回到 QUEUED；"
        "attempt 超过 max_attempts 时熔断为 BLOCKED 转人工。"
        "仅 MISMATCH / FAILED / TIMEOUT / REJECTED 状态可重派。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID"},
            "reason": {"type": "string", "description": "驳回原因（必填，作为审计与 Worker 反馈）"},
            "relax_timeout": {
                "type": "boolean",
                "description": "Phase C 数据驱动开关：True 且上次失败为 TIMEOUT 时，新 attempt 的 "
                                "execution_budget.max_seconds ×2（封顶 24h）。默认 False 不调整。",
            },
        },
        "required": ["task_id", "reason"],
    },
    is_concurrency_safe=False,
    groups=_TOOL_GROUP,
)
async def reject_and_reassign(task_id: str, reason: str, relax_timeout: bool = False) -> dict:
    db, err = _db_ready()
    if db is None:
        return _err("reject_and_reassign", err)
    task = await get_task(db, task_id)
    if task is None:
        return _err("reject_and_reassign", f"任务不存在: {task_id}")
    if not is_retryable(task["status"]):
        return _err(
            "reject_and_reassign",
            f"状态 {task['status']} 不允许重派（仅 MISMATCH/FAILED/TIMEOUT/REJECTED）",
        )
    prev_status = task["status"]
    task = await increment_attempt(db, task_id)
    relaxed = False
    # Phase C: 超时翻倍（数据驱动，不硬编码）——仅重派成功（QUEUED）且上次 TIMEOUT 时生效
    if (
        relax_timeout
        and task["status"] == TaskStatus.QUEUED
        and (prev_status == TaskStatus.TIMEOUT or "timeout" in (reason or "").lower())
    ):
        budget = dict(task.get("execution_budget") or {})
        ms = budget.get("max_seconds")
        if isinstance(ms, (int, float)) and ms > 0:
            budget["max_seconds"] = min(int(ms * 2), _BUDGET_RELAX_CAP_SEC)
            await db.execute(
                "UPDATE harness_tasks SET execution_budget=? WHERE id=?",
                (_json.dumps(budget, ensure_ascii=False), task_id),
            )
            await db.commit()
            relaxed = True
    return ToolResult(
        success=True,
        data={
            "task_id": task_id, "status": task["status"], "attempt": task["attempt"],
            "max_seconds_relaxed": relaxed,
        },
        observation=(
            f"已驳回并{'熔断转人工（BLOCKED）' if task['status'] == TaskStatus.BLOCKED else '重新排队'}，"
            f"原因: {reason}"
            + ("；超时预算 ×2（max_seconds 已放宽）" if relaxed else "")
        ),
        meta={"tool_name": "reject_and_reassign"},
    ).to_dict()


# ════════════════════════════════════════════════════════════
# 4. accept_result —— 接受/拒绝最终结果
# ════════════════════════════════════════════════════════════
@tool(
    name="accept_result",
    agents=[SUPERVISOR_AGENT_ID],
    description=(
        "Supervisor 对 Worker 结果做最终裁决：verdict=approved 仅接受 VERIFIED 状态"
        "（Verifier 交叉验证已通过）→ COMPLETED；verdict=rejected → REJECTED。"
        "未验证的结果不可 approved——必须 reject_and_reassign 重试。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID"},
            "verdict": {"type": "string", "enum": ["approved", "rejected"], "description": "最终裁决"},
            "reason": {"type": "string", "description": "裁决理由"},
        },
        "required": ["task_id", "verdict"],
    },
    is_concurrency_safe=False,
    groups=_TOOL_GROUP,
)
async def accept_result(task_id: str, verdict: str, reason: str = "") -> dict:
    db, err = _db_ready()
    if db is None:
        return _err("accept_result", err)
    verdict = (verdict or "").strip().lower()
    if verdict not in ("approved", "rejected"):
        return _err("accept_result", f"verdict 必须为 approved|rejected，收到: {verdict!r}")
    task = await get_task(db, task_id)
    if task is None:
        return _err("accept_result", f"任务不存在: {task_id}")

    if verdict == "approved":
        if task["status"] != TaskStatus.VERIFIED:
            return _err(
                "accept_result",
                f"approved 仅接受 VERIFIED 状态，当前 {task['status']}——"
                "未通过 Verifier 的结果必须 reject_and_reassign 重试",
            )
        task = await transition(db, task_id, TaskStatus.COMPLETED, reason or "Supervisor 接受结果")
    else:
        task = await transition(db, task_id, TaskStatus.REJECTED, reason or "Supervisor 拒绝结果")
    return ToolResult(
        success=True,
        data={"task_id": task_id, "status": task["status"]},
        observation=f"任务 {task_id} 已{'完成（COMPLETED）' if verdict == 'approved' else '拒绝（REJECTED）'}",
        meta={"tool_name": "accept_result"},
    ).to_dict()


# ════════════════════════════════════════════════════════════
# 5. human_resolve —— BLOCKED 人工裁决（Phase D，HITL 出路）
# ════════════════════════════════════════════════════════════
@tool(
    name="human_resolve",
    agents=[SUPERVISOR_AGENT_ID],
    description=(
        "人工裁决 BLOCKED 任务（attempt 耗尽熔断 / 同因 3 次）——HITL 出路："
        "action=retry 放行重试（BLOCKED→QUEUED，attempt 归零重置熔断计数）；"
        "action=cancel 人工取消（BLOCKED→CANCELLED）。仅 BLOCKED 状态可用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "BLOCKED 任务 ID"},
            "action": {
                "type": "string",
                "enum": ["retry", "cancel"],
                "description": "retry=重试（attempt 归零后回 QUEUED）；cancel=取消（终态 CANCELLED）",
            },
            "reason": {"type": "string", "description": "裁决理由（必填，写 state_transitions 审计）"},
        },
        "required": ["task_id", "action"],
    },
    is_concurrency_safe=False,
    groups=_TOOL_GROUP,
)
async def human_resolve(task_id: str, action: str, reason: str = "") -> dict:
    db, err = _db_ready()
    if db is None:
        return _err("human_resolve", err)
    action = (action or "").strip().lower()
    if action not in ("retry", "cancel"):
        return _err("human_resolve", f"action 必须为 retry|cancel，收到: {action!r}")
    task = await get_task(db, task_id)
    if task is None:
        return _err("human_resolve", f"任务不存在: {task_id}")
    if task["status"] != TaskStatus.BLOCKED:
        return _err(
            "human_resolve",
            f"仅 BLOCKED 可人工裁决，当前: {task['status']}（retry/cancel 均从 BLOCKED 出发）",
        )
    if action == "retry":
        # 熔断计数重置：人工介入后 attempt 归零（重新计算，不继承失败历史）
        await db.execute("UPDATE harness_tasks SET attempt=0 WHERE id=?", (task_id,))
        await db.commit()
        task = await transition(db, task_id, TaskStatus.QUEUED, f"人工裁决重试: {reason}")
    else:
        task = await transition(db, task_id, TaskStatus.CANCELLED, f"人工取消: {reason}")
    return ToolResult(
        success=True,
        data={"task_id": task_id, "status": task["status"], "attempt": task["attempt"]},
        observation=(
            f"人工裁决完成: {task_id} → {task['status']}"
            f"{'（attempt 已归零）' if action == 'retry' else ''}，理由: {reason}"
        ),
        meta={"tool_name": "human_resolve"},
    ).to_dict()
