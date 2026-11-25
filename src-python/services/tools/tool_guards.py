"""services/tools/tool_guards.py —— 工具执行层硬守卫（模式 + 能力策略）

从 tool_service.execute() 抽出的两个 fail-closed 守卫，抽出来是为了让
execute() 只留编排（否则每次加一行编排逻辑都要挤占 50 行函数预算）：

1. 模式守卫：ask / plan 模式拒绝**所有**工具（对标 Claude Code Ask/Plan mode）。
2. 能力守卫：能力策略（capability flags）禁用某能力时，对依赖该能力的工具
   fail-closed 拦截（原生/外部 Agent 一视同仁）。

两者都在工具真正执行前拦截，返回结构化 ToolResult dict（上层直接返回即可）。
"""
import logging

_log = logging.getLogger("yuncat.tool_service")


def mode_guard(tool_name: str, mode: str) -> dict | None:
    """ask/plan 模式：拒绝所有工具。返回 ToolResult dict 或 None（放行）。"""
    if mode not in ("ask", "plan"):
        return None
    from tools.result import ToolResult
    return ToolResult(
        success=False,
        error=f"'{tool_name}' 在 {mode} 模式下不可用——切换到 agent 模式后重试",
        observation=(
            f"当前 {mode} 模式不允许执行任何工具（包括读取/搜索）。"
            f"请建议用户切换到 Agent 模式以执行操作。"
        ),
        meta={
            "tool_name": tool_name, "mode_blocked": True,
            "current_mode": mode,
        },
    ).to_dict()


def capability_guard(tool_name: str) -> dict | None:
    """能力策略硬门控——被禁用功能 fail-closed 拦截。返回 ToolResult dict 或 None。"""
    from context import get_current_capability_flags
    from config import capability_required_by_tool

    flags = get_current_capability_flags()
    if not flags:
        # capability_flags 为 None（legacy/系统调用/测试）——跳过，不影响既有路径
        return None
    required = capability_required_by_tool(tool_name)
    if not required or flags.get(required, False):
        return None
    _log.info(
        "Capability policy blocked tool '%s' (capability '%s' disabled at level)",
        tool_name, required,
    )
    from tools.result import ToolResult
    return ToolResult(
        success=False,
        error=f"当前能力策略已禁用该功能（{required}）",
        observation=(
            f"工具 '{tool_name}' 需要能力「{required}」，但当前能力等级未启用。"
            f"请切换到「标准」或「全功能」能力策略，或换用不受限的替代工具。"
        ),
        meta={
            "tool_name": tool_name, "capability_blocked": True,
            "required_capability": required,
        },
    ).to_dict()
