"""tests/unit/test_h1_command_risk_passthrough.py —— H1: run_command 风险等级透传测试

验证 run_command 预览阶段调用 analyze_command_safety() 并将 risk_level/risk_reason
透传到前端确认弹窗。对标 Claude Code 命令审批弹窗的风险提示。

H1 范围:
- run_command 预览结果包含 risk_level (LOW/MEDIUM/HIGH)
- run_command 预览结果包含 risk_reason
- run_command 预览结果包含 cwd
- HIGH 风险在 run_command 预览阶段仍正常返回（上游 tool_service._validate_and_prepare 拦截）
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """隔离的工作区——run_command 内部 _get_workspace_path 依赖 workspace"""
    ws = str(tmp_path / "workspace")
    os.makedirs(ws, exist_ok=True)
    monkeypatch.setenv("YUNCAT_WORKSPACE", ws)
    # 重置 sandbox 缓存
    from security.sandbox import get_workspace
    get_workspace()
    return ws


class TestH1RiskPassthrough:
    """H1: run_command 预览阶段透传 risk_level / risk_reason / cwd"""

    @pytest.mark.asyncio
    async def test_low_risk_command_returns_low_level(self, workspace):
        """dir 命令——LOW 风险，预览应包含 risk_level='LOW' 和 risk_reason"""
        from tools.builtin.system_tool_command import run_command
        result = await run_command("dir")
        assert result["success"] is True
        data = result["data"]
        assert data["command"] == "dir"
        assert data["risk_level"] == "LOW"
        assert isinstance(data["risk_reason"], str) and data["risk_reason"]
        assert data["cwd"] == workspace

    @pytest.mark.asyncio
    async def test_medium_risk_pip_install_returns_medium_level(self, workspace):
        """pip install 命令——MEDIUM 风险（匹配 _MED_RISK_KEYWORDS）"""
        from tools.builtin.system_tool_command import run_command
        result = await run_command("pip install foo")
        assert result["success"] is True
        data = result["data"]
        assert data["command"] == "pip install foo"
        assert data["risk_level"] == "MEDIUM"
        assert "pip install" in data["risk_reason"]

    @pytest.mark.asyncio
    async def test_medium_risk_git_reset_hard_returns_medium_level(self, workspace):
        """git reset --hard 命令——MEDIUM 风险"""
        from tools.builtin.system_tool_command import run_command
        result = await run_command("git reset --hard HEAD")
        assert result["success"] is True
        data = result["data"]
        assert data["risk_level"] == "MEDIUM"
        assert "git reset --hard" in data["risk_reason"]

    @pytest.mark.asyncio
    async def test_high_risk_git_push_force_returns_high_level(self, workspace):
        """git push --force 命令——HIGH 风险（匹配 _HIGH_RISK_PATTERNS）

        验证 run_command 预览阶段防御性返回 HIGH——上游 tool_service._validate_and_prepare
        会拦截 HIGH，但 run_command 自身不做拦截，仍正常返回风险信息供前端展示。
        """
        from tools.builtin.system_tool_command import run_command
        result = await run_command("git push --force origin main")
        assert result["success"] is True
        data = result["data"]
        assert data["risk_level"] == "HIGH"
        assert "git push --force" in data["risk_reason"]

    @pytest.mark.asyncio
    async def test_preview_includes_timeout_sec(self, workspace):
        """预览应透传 timeout_sec"""
        from tools.builtin.system_tool_command import run_command
        result = await run_command("dir", timeout_sec=120)
        assert result["success"] is True
        assert result["data"]["timeout_sec"] == 120

    @pytest.mark.asyncio
    async def test_preview_includes_cwd_matching_workspace(self, workspace):
        """预览的 cwd 应匹配 workspace 路径"""
        from tools.builtin.system_tool_command import run_command
        result = await run_command("dir")
        assert result["data"]["cwd"] == workspace

    @pytest.mark.asyncio
    async def test_unsafe_command_skips_risk_analysis(self, workspace):
        """不在白名单的命令——is_safe_command 返回 False，直接返回 blocked 结果，不调用风险分析"""
        from tools.builtin.system_tool_command import run_command
        # rm 不在白名单
        result = await run_command("rm -rf /")
        assert result["success"] is False
        assert result.get("error")
        # blocked 结果不应包含 risk_level（H1 透传只在通过白名单后发生）
        assert "risk_level" not in result.get("data", {})

    @pytest.mark.asyncio
    async def test_risk_level_always_present_when_safe(self, workspace):
        """通过白名单的命令——risk_level 字段必存在（前端依赖此字段渲染徽章）"""
        from tools.builtin.system_tool_command import run_command
        for cmd in ["dir", "echo hello", "git status", "pip install foo"]:
            result = await run_command(cmd)
            assert result["success"] is True, f"命令应通过白名单: {cmd}"
            assert "risk_level" in result["data"], f"预览必须包含 risk_level: {cmd}"
            assert result["data"]["risk_level"] in {"LOW", "MEDIUM", "HIGH"}
