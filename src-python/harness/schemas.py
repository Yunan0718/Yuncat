"""harness/schemas.py —— Supervisor–Worker Harness 数据模型（Pydantic v2）

对齐 TDD v2.0 TaskPackage：mission / acceptance_criteria / constraints /
non_goals / attempt / permissions / execution_budget 全部由 Supervisor 派发时声明，
落库 harness_tasks，Worker 侧只读注入。

安全语义：WorkerReport 属于"主张"（claim），必须由 Verifier 交叉验证后
才可参与决策——schemas 层不承载信任，信任边界在 task_manager.apply_worker_report。
"""
from __future__ import annotations

import time as _time
import uuid as _uuid
from typing import Any

from pydantic import BaseModel, Field

SUPERVISOR_ROLE = "supervisor"


def new_id(prefix: str) -> str:
    """harness 表主键生成器：ht_/att_/exe_/preq_ 前缀 + 12 位 hex。"""
    return f"{prefix}_{_uuid.uuid4().hex[:12]}"


class HarnessPermissions(BaseModel):
    """任务权限边界——Policy Engine（Phase B）的数据输入，全部由数据声明。"""

    tools: list[str] = Field(default_factory=list, description="允许的工具白名单，空=继承系统白名单")
    write_paths: list[str] = Field(default_factory=list, description="允许写入的路径（相对 workspace）")
    read_paths: list[str] = Field(default_factory=list, description="允许读取的路径（相对 workspace）")
    allow_network: bool = Field(default=False, description="是否允许网络访问（默认拒绝）")
    deny_rules: list[str] = Field(default_factory=list, description="附加拒绝规则（正则）")


class ExecutionBudget(BaseModel):
    """执行预算——BudgetManager（Phase C）硬计数的上限声明。None=不限制该项。"""

    max_tool_invocations: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_seconds: int | None = Field(default=None, ge=1)


class TaskPackage(BaseModel):
    """任务包——assign_task 的产物。idempotency_key 唯一，重复派发返回既有任务。"""

    id: str = Field(default_factory=lambda: new_id("ht"))
    idempotency_key: str
    mission: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    permissions: HarnessPermissions = Field(default_factory=HarnessPermissions)
    execution_budget: ExecutionBudget = Field(default_factory=ExecutionBudget)
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    worker_agent_id: str | None = None
    supervisor_agent_id: str = SUPERVISOR_ROLE
    status: str = "PENDING"
    created_at: int = Field(default_factory=lambda: int(_time.time()))


class PermissionRequest(BaseModel):
    """L1 权限请求——Worker 越权动作进入 WAITING_PERMISSION 时生成。

    level 复用 security.permissions.PermissionLevel 数值（0-6）。
    """

    id: str = Field(default_factory=lambda: new_id("preq"))
    task_id: str
    attempt_id: str = ""
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    path_canonical: str | None = None
    level: int = Field(default=3, ge=0, le=6)
    status: str = "PENDING"
    decided_by: str | None = None
    created_at: int = Field(default_factory=lambda: int(_time.time()))


class WorkerReport(BaseModel):
    """Worker 汇报——视为不可信主张（claim），Verifier 交叉验证前不参与决策。

    claimed_files_modified 必须与 Verifier 的 actual_files_modified 完全一致
    （集合相等）且退出码一致才判 VERIFIED，否则 MISMATCH 驳回重派。
    """

    task_id: str
    attempt_id: str
    summary: str = Field(default="", max_length=2000)
    claimed_files_modified: list[str] = Field(default_factory=list)
    claimed_test_result: str = Field(default="", max_length=2000)
    test_exit_code: int | None = None


class RuntimeObservation(BaseModel):
    """Verifier 的客观取证——来自 git diff / 文件 hash / 测试退出码捕获。

    Phase A 由调用方注入（测试/桥接），Phase B 由 harness/verifier.py 采集。
    """

    actual_files_modified: list[str] = Field(default_factory=list)
    file_hashes: dict[str, str] = Field(default_factory=dict, description="Verifier 采集的 sha256 快照（审计/后续交叉验证用）")
    test_exit_code: int | None = None
    error_message: str | None = None
    verifier_decision: str = "UNVERIFIED"  # UNVERIFIED / VERIFIED / MISMATCH
