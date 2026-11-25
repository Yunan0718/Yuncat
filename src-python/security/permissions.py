"""security/permissions.py —— v41 七级权限分级模型

风险越高，数值越大，需要越强的人工干预。
"""
from enum import IntEnum
from typing import Any


class PermissionLevel(IntEnum):
    """七级权限——风险驱动"""
    ALWAYS_ALLOW = 0       # 只读操作——静默放行
    AUTO_ALLOW = 1         # 白名单命令——自动授权
    CONFIRM_ONCE = 2       # 首次需确认——同 session 后续自动放行
    CONFIRM_ALWAYS = 3     # 每次需确认——写文件/执行命令
    EXPLICIT_CONFIRM = 4   # 强制确认 + 原因说明
    DIRECTORY_SCOPED = 5   # 越界拦截
    ALWAYS_DENY = 6        # 永不执行


# 敏感路径匹配模式
SENSITIVE_PATH_PATTERNS = [".env", "config.yaml", "settings.json", "secrets", "credentials"]


def classify_permission(
    tool_name: str,
    tool_is_read_only: bool,
    tool_is_concurrency_safe: bool,
    arguments: dict[str, Any],
    agent_id: str = "",
) -> PermissionLevel:
    """风险驱动的权限分级——Phase 1 规则引擎。

    决策链:
      1. 只读工具 → ALWAYS_ALLOW
      2. 路径越界 → DIRECTORY_SCOPED
      3. 敏感路径 → EXPLICIT_CONFIRM
      4. 默认 → CONFIRM_ALWAYS
    """
    # Phase 108: 子 Agent 隔离——隔离子 Agent 不允许任何需要人工确认/有副作用的操作
    # (pin approval to never，对标 dsh child isolation)。只读工具仍放行，避免误伤读取类子任务。
    from context import get_isolation_mode
    if get_isolation_mode() and not tool_is_read_only:
        return PermissionLevel.ALWAYS_DENY

    # 规则 1: 只读工具 → 始终放行
    if tool_is_read_only:
        return PermissionLevel.ALWAYS_ALLOW

    # 规则 2: 路径越界 → 直接拒绝
    if tool_name in ("write_file", "read_file", "list_dir"):
        path = arguments.get("path", "")
        if path:
            from .sandbox import validate_path
            try:
                validate_path(path)
            except PermissionError:
                return PermissionLevel.DIRECTORY_SCOPED

    # 规则 3: 敏感路径 → 强制确认
    if tool_name == "write_file":
        path = arguments.get("path", "")
        if any(pattern in path for pattern in SENSITIVE_PATH_PATTERNS):
            return PermissionLevel.EXPLICIT_CONFIRM

    # 规则 4: 非白名单命令 → 直接拒绝（由 is_safe_command 在 run_command 中检查）
    if tool_name == "run_command":
        cmd = arguments.get("command", "")
        if not cmd.strip():
            return PermissionLevel.ALWAYS_DENY

    # Phase 2: 在此接入 ML 分类器
    return PermissionLevel.CONFIRM_ALWAYS
