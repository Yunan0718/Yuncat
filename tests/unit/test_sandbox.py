"""tests/unit/test_sandbox.py —— 工作区隔离与路径验证测试"""
import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestGetWorkspace:
    """测试工作区路径函数"""

    def test_get_workspace_returns_path(self):
        """get_workspace 返回 Path 对象"""
        from security.sandbox import get_workspace
        from pathlib import Path
        ws = get_workspace()
        assert isinstance(ws, Path)

    def test_get_workspace_creates_shared(self):
        """工作区包含 shared 子目录"""
        from security.sandbox import get_workspace
        ws = get_workspace()
        assert (ws / "shared").exists()

    def test_get_workspace_env_override(self, tmp_path, monkeypatch):
        """YUNCAT_WORKSPACE 环境变量可覆盖"""
        from security.sandbox import get_workspace
        custom = str(tmp_path / "custom_ws")
        monkeypatch.setenv("YUNCAT_WORKSPACE", custom)
        ws = get_workspace()
        assert str(ws.resolve()) == str(tmp_path / "custom_ws")


class TestGetAgentWorkspace:
    """测试 Agent 工作区隔离"""

    def test_creates_unique_subdir(self, tmp_path, monkeypatch):
        """不同 session 获得不同子目录"""
        from security.sandbox import get_agent_workspace
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        d1 = get_agent_workspace("hermes", "sess-a")
        d2 = get_agent_workspace("hermes", "sess-b")
        assert d1 != d2
        assert d1.exists()
        assert d2.exists()

    def test_different_agents_isolated(self, tmp_path, monkeypatch):
        """不同 Agent 工作区互相隔离"""
        from security.sandbox import get_agent_workspace
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws2"))
        a1 = get_agent_workspace("hermes", "s1")
        a2 = get_agent_workspace("openclaw", "s1")
        assert a1 != a2


class TestValidatePath:
    """测试路径验证"""

    def test_valid_path_within_workspace(self, tmp_path, monkeypatch):
        """工作区内的路径通过验证"""
        from security.sandbox import validate_path
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        # 先创建工作区
        from security.sandbox import get_workspace
        get_workspace()
        result = validate_path("test.py")
        assert result.name == "test.py"

    def test_rejects_path_traversal(self, tmp_path, monkeypatch):
        """拒绝路径穿越"""
        from security.sandbox import validate_path
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        from security.sandbox import get_workspace
        get_workspace()
        with pytest.raises(PermissionError, match="超出工作区"):
            validate_path("../../../etc/passwd")

    def test_rejects_absolute_path_outside(self, tmp_path, monkeypatch):
        """拒绝工作区外的绝对路径"""
        from security.sandbox import validate_path
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        from security.sandbox import get_workspace
        get_workspace()
        with pytest.raises(PermissionError, match="超出工作区"):
            validate_path("C:/Windows/System32")


class TestPendingAndBackup:
    """测试暂存和备份目录"""

    def test_pending_dir_exists(self, tmp_path, monkeypatch):
        """.pending 目录自动创建"""
        from security.sandbox import get_pending_dir
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        pending = get_pending_dir()
        assert pending.name == ".pending"
        assert pending.exists()

    def test_backup_dir_exists(self, tmp_path, monkeypatch):
        """.backup 目录自动创建"""
        from security.sandbox import get_backup_dir
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        backup = get_backup_dir()
        assert backup.name == ".backup"
        assert backup.exists()


class TestSharedWorkspace:
    """测试共享工作区"""

    def test_shared_workspace_exists(self, tmp_path, monkeypatch):
        """shared 目录自动创建"""
        from security.sandbox import get_shared_workspace
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        shared = get_shared_workspace()
        assert shared.name == "shared"
        assert shared.exists()


class TestWorkspaceStubRobustness:
    """工作区路径派生函数要能吃下"返回字符串"的 get_workspace 桩。

    2026-10-22：有测试把 security.sandbox.get_workspace 桩成 lambda: "D:/..."，
    于是 `get_workspace() / "shared"` 抛 TypeError（unsupported operand type(s) for /），
    并连带让 tests/unit/test_deliverables.py 在**全量跑**时失败、单跑却通过
    （模块在桩生效之后才被 import，绑到了桩函数）。
    """

    def test_derived_paths_accept_string_workspace(self, monkeypatch, tmp_path):
        from pathlib import Path
        import security.sandbox as sandbox

        monkeypatch.setattr(sandbox, "get_workspace", lambda: str(tmp_path / "ws"))
        assert isinstance(sandbox.get_shared_workspace(), Path)
        assert isinstance(sandbox.get_agent_workspace("a", "s"), Path)
        assert isinstance(sandbox.get_pending_dir(), Path)
        assert isinstance(sandbox.get_backup_dir(), Path)

    def test_snapshot_dir_accepts_string_workspace(self, monkeypatch, tmp_path):
        import services.snapshot_service as svc

        monkeypatch.setattr(svc, "get_workspace", lambda: str(tmp_path / "ws"))
        assert svc.snapshot_dir().name == ".yuncat_snapshots"
