"""security/presets.py —— 权限档位预设（权限 + 审批打包成一组选择）

Codex 把 sandbox 与 approval 分成两个轴，但界面上给的是一组命名预设
（`/approvals`）。我们同样处理：底层仍然是两个字段

  security_profile  定义在 security/profiles.py（能不能做）
  approval_mode     定义在 security/approval.py（要不要问、问谁）

但用户看到的只有一个选择器，选项就是下面这四个预设。预设表只在这里写一次，
后端读写与前端渲染都从这里来（接口 GET /api/v1/security/preset）。
"""
from dataclasses import dataclass

from security.approval import APPROVAL_AI, APPROVAL_ASK, APPROVAL_OFF, normalize_mode
from security.profiles import get_active_profile_name


@dataclass(frozen=True)
class Preset:
    """一个命名预设 = 一组（权限档，审批方式）"""

    id: str
    profile: str
    approval: str


PRESETS: tuple[Preset, ...] = (
    Preset("read_only", "strict", APPROVAL_ASK),
    Preset("workspace_write", "standard", APPROVAL_ASK),
    Preset("ai_review", "standard", APPROVAL_AI),
    Preset("full_access", "developer", APPROVAL_OFF),
)

CUSTOM_ID = "custom"


def get_preset(preset_id: str) -> Preset | None:
    """按 id 取预设；不存在返回 None（调用方决定怎么报错）"""
    for preset in PRESETS:
        if preset.id == preset_id:
            return preset
    return None


def describe_presets() -> list[dict]:
    """供前端渲染的预设列表"""
    return [
        {"id": p.id, "profile": p.profile, "approval": p.approval}
        for p in PRESETS
    ]


def match_preset(profile: str, approval: str) -> str:
    """把（权限档，审批方式）对回预设 id；对不上就是自定义组合"""
    mode = normalize_mode(approval)
    for preset in PRESETS:
        if preset.profile == profile and preset.approval == mode:
            return preset.id
    return CUSTOM_ID


def current_preset() -> str:
    """当前生效的预设 id（环境里的两个开关推出来）"""
    return match_preset(get_active_profile_name(), _current_mode())


def _current_mode() -> str:
    """审批方式统一从 security/approval.py 读（env 镜像）"""
    from security.approval import current_mode

    return current_mode()


def describe_current() -> dict:
    """当前预设 + 底层两个开关，前端一次拿齐"""
    profile = get_active_profile_name()
    mode = _current_mode()
    return {
        "preset": match_preset(profile, mode),
        "profile": profile,
        "approval": mode,
        "presets": describe_presets(),
    }


async def apply_preset(settings_service, preset_id: str) -> dict:
    """切到某个预设：一次写好两个开关（不允许上层只改一个）"""
    preset = get_preset(str(preset_id or "").strip())
    if preset is None:
        raise ValueError(f"unknown preset: {preset_id!r}")

    from security.approval import set_approval_mode

    await settings_service.save("security_profile", preset.profile)
    import os

    os.environ["YUNCAT_SECURITY_PROFILE"] = preset.profile
    await set_approval_mode(settings_service, preset.approval)
    return describe_current()
