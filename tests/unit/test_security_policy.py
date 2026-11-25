"""tests/unit/test_security_policy.py —— 权限档位的判决单点（security/policy.py）

守的是行为：哪一档拒绝什么、哪一档要求确认、读取类工具永远放行。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from security.policy import (  # noqa: E402
    classify_tool,
    confirmable_tool_names,
    decide_tool_call,
)
from security.profiles import get_profile  # noqa: E402


def _decide(tool_name, groups, level, confirmable=True, read_only=False):
    return decide_tool_call(
        tool_name, read_only, groups, confirmable, get_profile(level),
    )


class TestClassifyTool:
    """工具归类用注册表声明的 groups，不按名字羗一长串"""

    def test_read_only_tool_is_read(self):
        assert classify_tool(True, ["fs"]) == "read"

    def test_runtime_group_is_command(self):
        assert classify_tool(False, ["runtime"]) == "command"

    def test_fs_group_is_file(self):
        assert classify_tool(False, ["fs"]) == "file"

    def test_unknown_group_is_other(self):
        assert classify_tool(False, ["advanced"]) == "other"

    def test_missing_groups_is_other(self):
        assert classify_tool(False, None) == "other"


class TestReadOnlyProfile:
    """只读档：命令与写入直接拒绝，理由要能直接告诉用户往哪切"""

    def test_command_is_refused(self):
        decision = _decide("run_command", ["runtime"], "strict")
        assert decision.allowed is False
        assert decision.code == "profile_read_only_command"
        assert "只读" in decision.reason
        assert "工作区可写" in decision.reason

    def test_write_is_refused(self):
        decision = _decide("write_file", ["fs"], "strict")
        assert decision.allowed is False
        assert decision.code == "profile_read_only_write"

    def test_reads_still_allowed(self):
        decision = _decide("read_file", ["fs"], "strict", confirmable=False, read_only=True)
        assert decision.allowed is True
        assert decision.requires_confirmation is False

    def test_other_confirmable_tools_still_ask(self):
        """浏览器脚本这类不属于命令/写入，档位不影响它们的确认声明"""
        decision = _decide("browser_eval", ["browser"], "strict")
        assert decision.allowed is True
        assert decision.requires_confirmation is True


class TestWorkspaceWriteProfile:
    """默认档：允许，但两者都先问"""

    def test_command_allowed_but_asks(self):
        decision = _decide("run_command", ["runtime"], "standard")
        assert decision.allowed is True
        assert decision.requires_confirmation is True

    def test_write_allowed_but_asks(self):
        decision = _decide("write_file", ["fs"], "standard")
        assert decision.allowed is True
        assert decision.requires_confirmation is True

    def test_non_confirmable_tool_never_asks(self):
        decision = _decide("git_status", ["runtime"], "standard", confirmable=False)
        assert decision.allowed is True
        assert decision.requires_confirmation is False


class TestFullAccessProfile:
    """完全放开档：允许且不再问（命令仍走全局白名单）"""

    def test_command_allowed_without_asking(self):
        decision = _decide("run_command", ["runtime"], "developer")
        assert decision.allowed is True
        assert decision.requires_confirmation is False

    def test_write_allowed_without_asking(self):
        decision = _decide("write_file", ["fs"], "developer")
        assert decision.allowed is True
        assert decision.requires_confirmation is False


async def _noop(**_kwargs):
    """占位工具函数：判决层的用例不应该真的执行工具"""
    from tools.result import ToolResult
    return ToolResult(success=True, meta={"phase": "preview"}).to_dict()


def _meta(name, groups, confirmable=False, read_only=False, agents=None, func=_noop):
    """真实 ToolMeta（字段与 tools/registry.py 一致，不用假对象）"""
    from tools.registry import ToolMeta

    return ToolMeta(
        name=name, func=func, agents=agents or ["yuncat", "hermes"],
        description=f"fake {name}", parameters={},
        confirmable=confirmable, is_read_only=read_only, groups=list(groups),
    )


@pytest.fixture
def fake_registry(monkeypatch):
    """假注册表：不依赖内置工具是否被 import"""
    tools = {
        "write_file": _meta("write_file", groups=["fs"], confirmable=True),
        "run_command": _meta("run_command", groups=["runtime"], confirmable=True),
        "read_file": _meta("read_file", groups=["fs"], read_only=True),
        "browser_eval": _meta("browser_eval", groups=["browser"], confirmable=True),
    }
    from tools import registry as registry_module

    monkeypatch.setattr(registry_module, "_tools", tools)
    return tools


class TestConfirmableNames:
    """确认列表 = 注册表 confirmable ∩ 档位开关"""

    def test_read_only_keeps_only_unrelated_confirmations(self, fake_registry):
        assert confirmable_tool_names(get_profile("strict")) == {"browser_eval"}

    def test_standard_asks_for_command_and_write(self, fake_registry):
        assert confirmable_tool_names(get_profile("standard")) == {
            "write_file", "run_command", "browser_eval"}

    def test_full_access_drops_command_and_write(self, fake_registry):
        assert confirmable_tool_names(get_profile("developer")) == {"browser_eval"}


class TestToolServiceGate:
    """硬门槛：被档位拒绝的调用连工具函数都不会进"""

    def _fake_command_tool(self):
        async def _run_command(command: str = "", timeout_sec: int = 60):
            from tools.result import ToolResult
            return ToolResult(
                success=True,
                data={"command": command},
                meta={"phase": "preview", "tool_name": "run_command"},
            ).to_dict()

        return _meta("run_command", groups=["runtime"], confirmable=True, func=_run_command)

    @pytest.mark.asyncio
    async def test_read_only_profile_blocks_before_execution(self, monkeypatch):
        from tools import registry as registry_module
        from services.tools.tool_service import ToolService

        monkeypatch.setattr(registry_module, "_tools", {"run_command": self._fake_command_tool()})
        monkeypatch.setenv("YUNCAT_SECURITY_PROFILE", "strict")

        result = await ToolService().execute("run_command", {"command": "dir"}, "yuncat")

        assert result["success"] is False
        assert result["meta"]["policy_blocked"] is True
        assert result["meta"]["policy_code"] == "profile_read_only_command"

    @pytest.mark.asyncio
    async def test_standard_profile_lets_it_through_to_preview(self, monkeypatch):
        from tools import registry as registry_module
        from services.tools.tool_service import ToolService

        monkeypatch.setattr(registry_module, "_tools", {"run_command": self._fake_command_tool()})
        monkeypatch.setenv("YUNCAT_SECURITY_PROFILE", "standard")

        result = await ToolService().execute("run_command", {"command": "dir"}, "yuncat")

        assert result["success"] is True
        assert not result.get("meta", {}).get("policy_blocked")
