"""tests/unit/test_profiles.py —— 权限档位声明（security/profiles.py）

2026-09-16：旧版断言在旧字段上（如 enforce_whitelist）——那些字段
从来没有代码读过。现在断言真正会被执行的开关；判决行为在
tests/unit/test_security_policy.py，"不接受装饰性字段"在 tests/contract/
test_permission_profile_contract.py。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestGetProfile:
    """三档预设的真实语义"""

    def test_strict_is_read_only(self):
        from security.profiles import get_profile
        cfg = get_profile("strict")
        assert cfg.allow_run_command is False
        assert cfg.allow_write is False

    def test_standard_allows_but_asks_first(self):
        from security.profiles import get_profile
        cfg = get_profile("standard")
        assert cfg.allow_run_command is True
        assert cfg.allow_write is True
        assert cfg.require_command_confirmation is True
        assert cfg.require_write_confirmation is True

    def test_developer_allows_without_asking(self):
        from security.profiles import get_profile
        cfg = get_profile("developer")
        assert cfg.allow_run_command is True
        assert cfg.allow_write is True
        assert cfg.require_command_confirmation is False
        assert cfg.require_write_confirmation is False

    def test_unknown_profile_falls_back_to_standard(self):
        from security.profiles import get_profile
        cfg = get_profile("nonexistent")
        assert cfg.require_command_confirmation is True

    def test_enum_values(self):
        """档位 id 是已存入 DB 的历史值，不能改"""
        from security.profiles import SecurityProfile
        assert SecurityProfile.STRICT.value == "strict"
        assert SecurityProfile.STANDARD.value == "standard"
        assert SecurityProfile.DEVELOPER.value == "developer"


class TestCurrentProfile:
    """当前档位的读取"""

    def test_default_to_standard(self, monkeypatch):
        monkeypatch.delenv("YUNCAT_SECURITY_PROFILE", raising=False)
        from security.profiles import get_current_profile_config
        cfg = get_current_profile_config()
        assert cfg.require_command_confirmation is True

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("YUNCAT_SECURITY_PROFILE", "developer")
        from security.profiles import get_current_profile_config
        cfg = get_current_profile_config()
        assert cfg.require_command_confirmation is False

    def test_unknown_env_value_falls_back(self, monkeypatch):
        monkeypatch.setenv("YUNCAT_SECURITY_PROFILE", "whatever")
        from security.profiles import get_active_profile_name
        assert get_active_profile_name() == "standard"

    def test_describe_active_profile_shape(self, monkeypatch):
        monkeypatch.setenv("YUNCAT_SECURITY_PROFILE", "strict")
        from security.profiles import describe_active_profile
        info = describe_active_profile()
        assert info["name"] == "strict"
        assert info["allow_run_command"] is False
        assert info["allow_write"] is False
        assert info["require_command_confirmation"] is True
        assert info["require_write_confirmation"] is True
