"""harness/policy_engine.py —— L0/L1 两级权限裁决 + path_canonicalize 强制

对齐 docs/harness-supervisor-worker.md §3.2：
  - L0（自动放行）：工具在白名单 且 目标路径 ∈ permissions.write_paths/read_paths
    且 使命/验收标准提及 → 直接放行；
  - L1（Supervisor 意图 + 规则校验）：其余请求生成 PermissionRequest 走
    supervisor approve_permission；但最终放行仍过 policy_engine——
    即使 Supervisor 批准，canonicalize 检测到越权（解析后落在 workspace 外）
    也硬性拒绝并记违规事件（violation=True）；
  - 路径校验只认 realpath 后 is_relative_to，禁止 startswith 前缀匹配——
    path_canonicalize 复用 security/sandbox.canonicalize（单一事实源）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from security.sandbox import canonicalize

# 与 security/sandbox.canonicalize 同一实现——harness 侧命名（方案文档 Phase B）
def path_canonicalize(path: str, root: Path, boundary: Path | None = None) -> Path:
    """路径规范化：resolve + boundary 边界校验（越界抛 PermissionError）。"""
    return canonicalize(path, root=root, boundary=boundary)


# 工具参数里可能携带目标路径的键（与工具 schema 参数名对齐）
_PATH_KEYS = ("path", "old_path", "new_path", "file_path", "directory")

# 写类工具：L0 需落在 write_paths 内
_WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "rename_file", "mkdir",
    "delete_file", "move_file", "copy_file", "apply_patch",
})
# 读类工具：L0 需落在 read_paths（或 write_paths）内
_READ_TOOLS = frozenset({"read_file", "list_dir", "search_files"})


@dataclass(frozen=True)
class PolicyDecision:
    """裁决结果。

    level: L0_ALLOW | L1_REQUEST | DENY
    reason: 裁决理由（DENY 必填）
    canonical_path: 目标路径规范化结果（L0/L1 填充）
    violation: True = 越权违规事件（即使批准也硬拒，需审计/告警）
    """

    level: str
    reason: str = ""
    canonical_path: Path | None = None
    violation: bool = False

    @property
    def is_allow(self) -> bool:
        return self.level == "L0_ALLOW"


def _target_path(arguments: dict[str, Any]) -> str | None:
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _within(path: Path, roots: list[str], workspace: Path) -> bool:
    """path 是否落在任一声明目录（roots，相对 workspace）内。"""
    for rel in roots:
        try:
            declared = canonicalize(rel, root=workspace, boundary=workspace)
        except (PermissionError, ValueError):
            continue  # 声明本身越界 → 视为无效声明，跳过
        if path.is_relative_to(declared):
            return True
    return False


def _mentioned(rel_path: str, mission: str, acceptance_criteria: tuple[str, ...]) -> bool:
    """使命/验收标准是否提及目标路径（大小写不敏感的子串匹配，v1 规则）。"""
    haystack = " ".join((mission, *acceptance_criteria)).lower()
    return rel_path.lower() in haystack or Path(rel_path).name.lower() in haystack


def evaluate(
    permissions,
    tool_name: str,
    arguments: dict[str, Any],
    workspace: Path,
    *,
    mission: str = "",
    acceptance_criteria: tuple[str, ...] = (),
) -> PolicyDecision:
    """L0/L1 两级裁决（数据驱动，无工具名硬编码分支）。

    返回 DENY 的两种情形：
      1. 工具不在白名单（permissions.tools 非空时）；
      2. 路径越出 workspace 边界（violation=True，任何批准都救不回来）。
    """
    workspace = Path(workspace).resolve()

    # 1) 工具白名单
    if permissions.tools and tool_name not in permissions.tools:
        return PolicyDecision("DENY", reason=f"工具 {tool_name} 不在任务白名单", violation=False)

    # 2) 非路径工具（run_command 等）——命令级安全由 ToolService 白名单/LLM 分析兜底
    target = _target_path(arguments)
    if target is None:
        return PolicyDecision(
            "L1_REQUEST",
            reason=f"工具 {tool_name} 无路径参数，需 Supervisor 确认",
        )

    # 3) 路径硬边界：越出 workspace 一律 DENY（Supervisor 也救不回）
    try:
        canon = path_canonicalize(target, root=workspace, boundary=workspace)
    except PermissionError as e:
        return PolicyDecision("DENY", reason=f"路径越出工作区: {e}", violation=True)

    rel = canon.relative_to(workspace).as_posix()
    if tool_name in _WRITE_TOOLS:
        declared = list(permissions.write_paths)
    elif tool_name in _READ_TOOLS:
        declared = list(permissions.read_paths) + list(permissions.write_paths)
    else:  # 路径型但未归类（如 reveal_in_explorer）——按写规则从严
        declared = list(permissions.write_paths)

    if declared and _within(canon, declared, workspace) and _mentioned(rel, mission, acceptance_criteria):
        return PolicyDecision(
            "L0_ALLOW",
            reason=f"{rel} ∈ 声明路径且使命提及，自动放行",
            canonical_path=canon,
        )

    return PolicyDecision(
        "L1_REQUEST",
        reason=f"{rel} 未命中 L0（不在声明路径或未被使命提及），需 Supervisor 确认",
        canonical_path=canon,
    )


def verify_approval(canonical: str | None, workspace: Path, boundary: Path | None = None) -> tuple[bool, str]:
    """Supervisor 批准后的最终硬闸：重新规范化，越界硬拒。

    canonical 为空（无路径的请求如 run_command）→ 放行（无路径可越）。
    返回 (是否放行, 原因)。
    """
    if not canonical:
        return True, "无路径请求"
    workspace = Path(workspace).resolve()
    try:
        path_canonicalize(canonical, root=workspace, boundary=boundary or workspace)
    except PermissionError as e:
        return False, f"审批路径越界，硬性拒绝: {e}"
    return True, "路径在工作区内"
