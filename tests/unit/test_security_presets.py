"""tests/unit/test_security_presets.py —— 权限档位预设（security/presets.py）

预设 = （权限档，审批方式）的命名组合。守的是：组合合法、回推正确、
切档一次写好两个字段（不允许上层只改一个）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from security.approval import MODES  # noqa: E402
from security.presets import (  # noqa: E402
    CUSTOM_ID,
    PRESETS,
    apply_preset,
    describe_current,
    describe_presets,
    get_preset,
    match_preset,
)
from security.profiles import PROFILES, SecurityProfile  # noqa: E402


class TestPresetTable:
    def test_ids_are_unique(self):
        ids = [p.id for p in PRESETS]
        assert len(ids) == len(set(ids))

    def test_every_preset_points_at_real_profile_and_mode(self):
        for preset in PRESETS:
            assert preset.profile in {p.value for p in SecurityProfile}, preset.id
            assert preset.approval in MODES, preset.id

    def test_describe_presets_shape(self):
        described = describe_presets()
        assert [p["id"] for p in described] == [p.id for p in PRESETS]
        assert all(set(p) == {"id", "profile", "approval"} for p in described)


class TestMatchPreset:
    def test_exact_pairs_match(self):
        for preset in PRESETS:
            assert match_preset(preset.profile, preset.approval) == preset.id

    def test_non_default_pair_is_custom(self):
        """底层组合不在预设表里时不能假装成某个预设"""
        assert match_preset("strict", "off") == CUSTOM_ID
        assert match_preset("developer", "ai") == CUSTOM_ID


class TestGetPreset:
    def test_unknown_is_none(self):
        assert get_preset("yolo") is None
        assert get_preset("") is None


class _FakeSettings:
    def __init__(self):
        self.saved: dict[str, str] = {}

    async def save(self, key: str, value: str) -> None:
        self.saved[key] = value


class TestApplyPreset:
    @pytest.mark.asyncio
    async def test_writes_both_switches(self, monkeypatch):
        monkeypatch.delenv("YUNCAT_APPROVAL_MODE", raising=False)
        monkeypatch.delenv("YUNCAT_SECURITY_PROFILE", raising=False)
        service = _FakeSettings()

        result = await apply_preset(service, "ai_review")

        assert service.saved == {"security_profile": "standard", "approval_mode": "ai", "auto_approve": "false"}
        assert os.environ["YUNCAT_SECURITY_PROFILE"] == "standard"
        assert result["preset"] == "ai_review"

    @pytest.mark.asyncio
    async def test_full_access_turns_legacy_flag_on(self, monkeypatch):
        monkeypatch.delenv("YUNCAT_APPROVAL_MODE", raising=False)
        service = _FakeSettings()

        await apply_preset(service, "full_access")

        assert service.saved["security_profile"] == "developer"
        assert service.saved["auto_approve"] == "true"

    @pytest.mark.asyncio
    async def test_unknown_preset_rejected(self):
        service = _FakeSettings()
        with pytest.raises(ValueError):
            await apply_preset(service, "yolo")
        assert service.saved == {}


class TestDescribeCurrent:
    def test_reports_preset_and_raw_switches(self, monkeypatch):
        monkeypatch.setenv("YUNCAT_SECURITY_PROFILE", "standard")
        monkeypatch.setenv("YUNCAT_APPROVAL_MODE", "ai")

        info = describe_current()

        assert info["preset"] == "ai_review"
        assert info["profile"] == "standard"
        assert info["approval"] == "ai"
        assert {p["id"] for p in info["presets"]} >= {"read_only", "full_access"}


def test_profiles_and_presets_agree_on_the_default_pair():
    """默认应该就是“工作区可写”，否则首次打开就显示自定义"""
    assert PROFILES[SecurityProfile.STANDARD].allow_write is True
