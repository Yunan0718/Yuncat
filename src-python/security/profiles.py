"""security/profiles.py --- permission presets (three switchable levels)

2026-09-16 rewrite: only declare switches that are actually enforced.

The previous version declared 14 fields, but no code read any of them: the level
was written into settings and read back by two routes for display, while the tool
execution path was unchanged (strict behaved exactly like developer). Declaring a
switch that does nothing is worse than not declaring it -- the user believes the
permission changed when it did not.

Constant policies (independent of the level, enforced elsewhere, so not repeated here):
  - command whitelist + inline interpreter rejection: tools/builtin/system_tool_safety.py
  - workspace boundary / denied system paths: security/sandbox.validate_path,
    system_tool_safety.is_denied_path
  - network / browser / vision / sub-agent: config.CAPABILITY_TOOL_MAP (by capability level)

Level semantics (mirrors the Codex sandbox + approval combination):
  strict    read only: commands and writes are refused outright
  standard  workspace write (default): commands follow the whitelist, both ask first
  developer full access: commands still follow the whitelist, but nothing asks

The single decision point lives in security/policy.py; this module only declares.
"""
from dataclasses import dataclass
from enum import Enum


class SecurityProfile(str, Enum):
    STRICT = "strict"        # read only
    STANDARD = "standard"    # workspace write (default)
    DEVELOPER = "developer"  # full access


@dataclass
class ProfileConfig:
    """One permission level. Every field here is read by security/policy.py."""

    allow_run_command: bool = True
    require_command_confirmation: bool = True
    allow_write: bool = True
    require_write_confirmation: bool = True


PROFILES: dict[SecurityProfile, ProfileConfig] = {
    SecurityProfile.STRICT: ProfileConfig(
        allow_run_command=False,
        require_command_confirmation=True,
        allow_write=False,
        require_write_confirmation=True,
    ),
    SecurityProfile.STANDARD: ProfileConfig(
        allow_run_command=True,
        require_command_confirmation=True,
        allow_write=True,
        require_write_confirmation=True,
    ),
    SecurityProfile.DEVELOPER: ProfileConfig(
        allow_run_command=True,
        require_command_confirmation=False,
        allow_write=True,
        require_write_confirmation=False,
    ),
}


def get_profile(name: str = "standard") -> ProfileConfig:
    """Return one level; unknown names fall back to standard (fail-safe)."""
    try:
        profile = SecurityProfile(name)
    except ValueError:
        profile = SecurityProfile.STANDARD
    return PROFILES[profile]


def get_active_profile_name() -> str:
    """Active level name, from YUNCAT_SECURITY_PROFILE (lifespan syncs it from settings)."""
    import os
    name = os.environ.get("YUNCAT_SECURITY_PROFILE", "standard")
    return name if name in PROFILES else SecurityProfile.STANDARD.value


def get_current_profile_config() -> ProfileConfig:
    """Config of the active level."""
    return get_profile(get_active_profile_name())


def describe_active_profile() -> dict:
    """Active level as a plain dict for routes / prompts / frontend."""
    cfg = get_current_profile_config()
    return {
        "name": get_active_profile_name(),
        "allow_run_command": cfg.allow_run_command,
        "require_command_confirmation": cfg.require_command_confirmation,
        "allow_write": cfg.allow_write,
        "require_write_confirmation": cfg.require_write_confirmation,
    }
