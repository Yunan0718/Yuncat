"""security/policy.py --- the single decision point for the permission level

security/profiles.py only declares switches; this module turns them into a verdict
for one tool call:

  allowed               may the call run at all
  requires_confirmation must the user approve it first
  code / reason         why not, in wording the user can act on

Callers: ToolService (hard gate before any side effect) and the agent confirmable
flow (which builds its confirm list from here). A new switch therefore touches
profiles.py + this file only -- there is no third place that rules on the level.
"""
from dataclasses import dataclass

from security.profiles import ProfileConfig, get_current_profile_config

# Tool classification comes from the groups declared in the registry, so adding a
# tool never means editing a long name list here.
_COMMAND_GROUPS = frozenset({"runtime"})
_FILE_GROUPS = frozenset({"fs"})


@dataclass(frozen=True)
class ToolPolicy:
    """Verdict for one tool call under one permission level."""

    allowed: bool
    requires_confirmation: bool
    code: str = ""
    reason: str = ""
    kind: str = "other"


def classify_tool(is_read_only: bool, groups: list[str] | None = None) -> str:
    """Classify a tool as read / command / file / other."""
    if is_read_only:
        return "read"
    declared = set(groups or [])
    if declared & _COMMAND_GROUPS:
        return "command"
    if declared & _FILE_GROUPS:
        return "file"
    return "other"


def decide_tool_call(
    tool_name: str,
    is_read_only: bool = False,
    groups: list[str] | None = None,
    confirmable: bool = False,
    profile: ProfileConfig | None = None,
) -> ToolPolicy:
    """Decide one tool call. `confirmable` is the registry declaration."""
    cfg = profile or get_current_profile_config()
    kind = classify_tool(is_read_only, groups)

    if kind == "command":
        if not cfg.allow_run_command:
            return ToolPolicy(
                allowed=False, requires_confirmation=False, kind=kind,
                code="profile_read_only_command",
                reason="当前权限档「只读」不允许执行命令；需要跑命令请在输入区切到「工作区可写」",
            )
        return ToolPolicy(
            allowed=True,
            requires_confirmation=bool(confirmable and cfg.require_command_confirmation),
            kind=kind,
        )

    if kind == "file":
        if not cfg.allow_write:
            return ToolPolicy(
                allowed=False, requires_confirmation=False, kind=kind,
                code="profile_read_only_write",
                reason="当前权限档「只读」不允许写入或删除文件；需要改动请在输入区切到「工作区可写」",
            )
        return ToolPolicy(
            allowed=True,
            requires_confirmation=bool(confirmable and cfg.require_write_confirmation),
            kind=kind,
        )

    # Reads and every other confirmable tool (browser script, checkpoint, sub-agent)
    # keep whatever the registry declared -- the level says nothing about them.
    return ToolPolicy(allowed=True, requires_confirmation=bool(confirmable), kind=kind)


def confirmable_tool_names(profile: ProfileConfig | None = None) -> set[str]:
    """Names that still need user approval at the current level."""
    from tools.registry import get_all_tools

    cfg = profile or get_current_profile_config()
    names: set[str] = set()
    for name, meta in get_all_tools().items():
        decision = decide_tool_call(
            name,
            is_read_only=bool(getattr(meta, "is_read_only", False)),
            groups=list(getattr(meta, "groups", None) or []),
            confirmable=bool(getattr(meta, "confirmable", False)),
            profile=cfg,
        )
        if decision.allowed and decision.requires_confirmation:
            names.add(name)
    return names
