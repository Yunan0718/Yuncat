"""harness/__init__.py —— Supervisor–Worker Harness 包

模块布局（docs/harness-supervisor-worker.md）：
  schemas.py            # TaskPackage v2.0 / PermissionRequest / WorkerReport / RuntimeObservation
  task_manager.py       # 13 状态机 + harness_tasks 系列表持久化
  supervisor_tools.py   # @tool 注册 4 个 supervisor 工具
  policy_engine.py      # Phase B：路径规范化 + L0/L1 裁决
  verifier.py           # Phase B：git diff + hash 快照交叉验证
  budget_manager.py     # Phase C：contextvars 硬计数
  runtime.py            # Phase C：进程组杀统一接口
  scheduler.py          # Phase D：write_paths 冲突检测 + 领取门
  isolation.py          # Phase D：工作区浅拷贝隔离与合并
"""
from harness.schemas import (
    SUPERVISOR_ROLE,
    ExecutionBudget,
    HarnessPermissions,
    PermissionRequest,
    RuntimeObservation,
    TaskPackage,
    WorkerReport,
    new_id,
)

__all__ = [
    "SUPERVISOR_ROLE",
    "ExecutionBudget",
    "HarnessPermissions",
    "PermissionRequest",
    "RuntimeObservation",
    "TaskPackage",
    "WorkerReport",
    "new_id",
]
