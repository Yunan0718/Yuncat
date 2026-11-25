"""tests/contract/test_security_preset_contract.py —— 权限档位（权限 + 审批）的端到端接线

用户只选一个预设，底下两个开关由后端一次写好。任一环断了，用户看到的
就是“开关没用”或“两处不一致”——所以下面每一环都留一条断言。
"""
import json
import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_SRC_PY = os.path.join(_ROOT, "src-python")
_SRC_FE = os.path.join(_ROOT, "src-frontend")
_LOCALES = ["zh", "en", "ja", "ko"]
_ENDPOINT = "/api/v1/security/preset"
_PRESET_IDS = ["read_only", "workspace_write", "ai_review", "full_access"]


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_preset_table_is_the_single_source():
    sys_path = _SRC_PY
    import sys
    sys.path.insert(0, sys_path)
    from security.presets import describe_presets

    ids = [p["id"] for p in describe_presets()]
    assert ids == _PRESET_IDS, f"预设表变了：{ids}"


def test_route_reads_and_writes_the_preset():
    src = _read(os.path.join(_SRC_PY, "routes", "tools.py"))
    assert 'router.get("/security/preset")' in src
    assert 'router.post("/security/preset")' in src
    assert "apply_preset" in src and "describe_current" in src


def test_confirmable_flow_uses_the_approval_mode():
    src = _read(os.path.join(_SRC_PY, "services", "agent", "confirmable.py"))
    assert "resolve_approval_mode" in src, "确认流程应先取当前审批方式"
    assert "review_outcome" in src
    assert '"type": "approval_review"' in src, "应把 AI 审批结论推给前端"
    assert "APPROVAL_AI" in src, "只有 AI 档位才允许终审代替用户放行"


def test_tool_service_still_gates_on_the_profile():
    src = _read(os.path.join(_SRC_PY, "services", "tools", "tool_service.py"))
    assert "decide_tool_call" in src and "policy_blocked" in src


def test_lifespan_and_prompt_know_the_switches():
    assert "_sync_approval_mode" in _read(os.path.join(_SRC_PY, "lifespan_services.py"))
    prompt = _read(os.path.join(_SRC_PY, "prompts", "commander.py"))
    assert "_approval_prompt_hint" in prompt and "_permission_prompt_line" in prompt


def test_frontend_has_one_control_for_both_switches():
    lib = _read(os.path.join(_SRC_FE, "src", "lib", "securityPreset.ts"))
    assert _ENDPOINT in lib, "前端应走同一个预设接口"
    for preset_id in _PRESET_IDS:
        assert f'"{preset_id}"' in lib, f"前端缺少预设 {preset_id}"
    control = _read(os.path.join(_SRC_FE, "src", "components", "chat", "SecurityModeControl.tsx"))
    assert "PRESET_IDS.map" in control, "控件应遍历预设表，不是写死分支"
    view = _read(os.path.join(_SRC_FE, "src", "components", "chat", "ChatView.tsx"))
    assert "useSecurityControl" in view, "对话区应用共用 hook"
    section = _read(os.path.join(_SRC_FE, "src", "components", "settings", "SecuritySection.tsx"))
    assert "useSecurityControl" in section, "设置页应用同一个 hook"
    assert '"auto_approve"' not in section and '"security_profile"' not in section, (
        "不允许在界面层直接写底层两个键"
    )


def test_frontend_no_longer_splits_the_two_switches():
    """合并后不应再出现独立的“权限档 + 审批”两个控件"""
    control = _read(os.path.join(_SRC_FE, "src", "components", "chat", "SecurityModeControl.tsx"))
    for leftover in ("securityProfile={", "onSecurityProfileChange", "approvalMode={", "setApprovalMode"):
        assert leftover not in control, f"控件里还有旧的双开关痕迹: {leftover}"
    assert "DropdownMenuRadioGroup" in control, "一个选择器，不是两组"


def test_ai_verdict_reaches_the_trajectory():
    index = _read(os.path.join(_SRC_FE, "src", "hooks", "wsHandlers", "index.ts"))
    handlers = _read(os.path.join(_SRC_FE, "src", "hooks", "wsHandlers", "toolHandlers.ts"))
    trajectory = _read(os.path.join(_SRC_FE, "src", "lib", "trajectory.ts"))
    assert "approval_review: tool.approvalReview" in index
    assert "appendApproval" in handlers
    assert '"approval"' in trajectory


def test_four_locales_have_preset_wording():
    keys = {"securityStrict", "securityStrictDesc", "securityStandard", "securityStandardDesc",
            "securityDeveloper", "securityDeveloperDesc", "securityProfileDesc",
            "approvalAi", "approvalAiDesc", "presetCustom", "presetCustomDesc"}
    chat_keys = {"approvalAi", "approvalAiDesc", "presetCustom", "presetCustomDesc"}
    for lang in _LOCALES:
        path = os.path.join(_SRC_FE, "src", "i18n", "locales", f"{lang}.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        missing = {k for k in keys - chat_keys if k not in data.get("settings", {})}
        missing |= {k for k in chat_keys if k not in data.get("chat", {})}
        assert not missing, f"{lang}.json 缺少: {sorted(missing)}"
        for key in chat_keys:
            assert data["chat"][key].strip(), f"{lang}.json chat.{key} 不能为空"
        traj = data.get("trajectory", {})
        assert traj.get("approvalApproved") and traj.get("approvalDenied")
