"""tests/unit/test_permissions.py —— 七级权限分级模型测试"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestPermissionLevel:
    """测试 PermissionLevel 枚举"""

    def test_level_ordering(self):
        """权限级别数值递增"""
        from security.permissions import PermissionLevel
        assert PermissionLevel.ALWAYS_ALLOW < PermissionLevel.AUTO_ALLOW
        assert PermissionLevel.AUTO_ALLOW < PermissionLevel.CONFIRM_ONCE
        assert PermissionLevel.CONFIRM_ALWAYS < PermissionLevel.EXPLICIT_CONFIRM
        assert PermissionLevel.EXPLICIT_CONFIRM < PermissionLevel.DIRECTORY_SCOPED
        assert PermissionLevel.DIRECTORY_SCOPED < PermissionLevel.ALWAYS_DENY

    def test_level_values(self):
        """权限级别值正确"""
        from security.permissions import PermissionLevel
        assert PermissionLevel.ALWAYS_ALLOW == 0
        assert PermissionLevel.ALWAYS_DENY == 6


class TestClassifyPermission:
    """测试 classify_permission 决策逻辑"""

    def test_read_only_always_allowed(self):
        """只读工具 → ALWAYS_ALLOW"""
        from security.permissions import classify_permission, PermissionLevel
        result = classify_permission(
            "read_file", tool_is_read_only=True, tool_is_concurrency_safe=True,
            arguments={"path": "test.py"}, agent_id="hermes"
        )
        assert result == PermissionLevel.ALWAYS_ALLOW

    def test_write_file_confirm_always(self, tmp_path, monkeypatch):
        """普通写文件 → CONFIRM_ALWAYS"""
        from security.permissions import classify_permission, PermissionLevel
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        from security.sandbox import get_workspace
        get_workspace()
        result = classify_permission(
            "write_file", tool_is_read_only=False, tool_is_concurrency_safe=False,
            arguments={"path": "output.txt"}, agent_id="hermes"
        )
        assert result == PermissionLevel.CONFIRM_ALWAYS

    def test_sensitive_path_explicit_confirm(self, tmp_path, monkeypatch):
        """敏感路径 → EXPLICIT_CONFIRM"""
        from security.permissions import classify_permission, PermissionLevel
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        from security.sandbox import get_workspace
        get_workspace()
        result = classify_permission(
            "write_file", tool_is_read_only=False, tool_is_concurrency_safe=False,
            arguments={"path": ".env"}, agent_id="hermes"
        )
        assert result == PermissionLevel.EXPLICIT_CONFIRM

    def test_config_path_explicit_confirm(self, tmp_path, monkeypatch):
        """config.yaml 路径 → EXPLICIT_CONFIRM"""
        from security.permissions import classify_permission, PermissionLevel
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        from security.sandbox import get_workspace
        get_workspace()
        result = classify_permission(
            "write_file", tool_is_read_only=False, tool_is_concurrency_safe=False,
            arguments={"path": "config.yaml"}, agent_id="hermes"
        )
        assert result == PermissionLevel.EXPLICIT_CONFIRM

    def test_path_traversal_rejected(self, tmp_path, monkeypatch):
        """路径穿越 → DIRECTORY_SCOPED"""
        from security.permissions import classify_permission, PermissionLevel
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        from security.sandbox import get_workspace
        get_workspace()
        result = classify_permission(
            "write_file", tool_is_read_only=False, tool_is_concurrency_safe=False,
            arguments={"path": "../../../etc/passwd"}, agent_id="hermes"
        )
        assert result == PermissionLevel.DIRECTORY_SCOPED

    def test_empty_command_denied(self):
        """空命令 → ALWAYS_DENY"""
        from security.permissions import classify_permission, PermissionLevel
        result = classify_permission(
            "run_command", tool_is_read_only=False, tool_is_concurrency_safe=False,
            arguments={"command": ""}, agent_id="hermes"
        )
        assert result == PermissionLevel.ALWAYS_DENY

    def test_normal_command_confirm(self):
        """正常命令 → CONFIRM_ALWAYS"""
        from security.permissions import classify_permission, PermissionLevel
        result = classify_permission(
            "run_command", tool_is_read_only=False, tool_is_concurrency_safe=False,
            arguments={"command": "dir"}, agent_id="hermes"
        )
        assert result == PermissionLevel.CONFIRM_ALWAYS
