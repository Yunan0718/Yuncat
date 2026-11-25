"""tests/unit/test_shell_whitelist.py —— Task 7.10 持久 Shell 白名单

覆盖：
1. SHELL_BUILTIN_WHITELIST 存在且包含预期 shell 内置命令
2. validate_source_command 检测 source/. 目标脚本的危险模式
3. is_shell_command_allowed 检查首 token 是否在白名单中
4. _run_in_shell 中的 cwd 同步逻辑（通过 mock subprocess）

对标 Claude Code 会话级 Bash——允许 shell builtins 但仍检查危险字符 + 脚本内容。
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ═══════════════════════════════════════════════════════════════
# SHELL_BUILTIN_WHITELIST 测试
# ═══════════════════════════════════════════════════════════════

class TestShellBuiltinWhitelist:
    """Task 7.10: SHELL_BUILTIN_WHITELIST 定义和内容。"""

    def test_whitelist_exists(self):
        """SHELL_BUILTIN_WHITELIST 应存在且为 frozenset。"""
        from tools.builtin.system_tool_safety import SHELL_BUILTIN_WHITELIST
        assert isinstance(SHELL_BUILTIN_WHITELIST, (frozenset, set))

    def test_whitelist_contains_cd(self):
        """cd 是最基本的 shell builtin。"""
        from tools.builtin.system_tool_safety import SHELL_BUILTIN_WHITELIST
        assert "cd" in SHELL_BUILTIN_WHITELIST

    def test_whitelist_contains_export(self):
        """export 用于设置环境变量——持久 Shell 核心场景。"""
        from tools.builtin.system_tool_safety import SHELL_BUILTIN_WHITELIST
        assert "export" in SHELL_BUILTIN_WHITELIST

    def test_whitelist_contains_source(self):
        """source 用于加载脚本——持久 Shell 核心场景。"""
        from tools.builtin.system_tool_safety import SHELL_BUILTIN_WHITELIST
        assert "source" in SHELL_BUILTIN_WHITELIST

    def test_whitelist_contains_set(self):
        """set 用于设置 shell 变量。"""
        from tools.builtin.system_tool_safety import SHELL_BUILTIN_WHITELIST
        assert "set" in SHELL_BUILTIN_WHITELIST

    def test_whitelist_contains_dot(self):
        """. 是 source 的 POSIX 简写。"""
        from tools.builtin.system_tool_safety import SHELL_BUILTIN_WHITELIST
        assert "." in SHELL_BUILTIN_WHITELIST

    def test_whitelist_contains_alias(self):
        """alias 用于创建别名。"""
        from tools.builtin.system_tool_safety import SHELL_BUILTIN_WHITELIST
        assert "alias" in SHELL_BUILTIN_WHITELIST


# ═══════════════════════════════════════════════════════════════
# is_shell_command_allowed 测试
# ═══════════════════════════════════════════════════════════════

class TestIsShellCommandAllowed:
    """Task 7.10: is_shell_command_allowed 检查首 token 是否在白名单中。"""

    def test_shell_builtin_allowed(self):
        """shell 内置命令应放行。"""
        from tools.builtin.system_tool_safety import is_shell_command_allowed
        allowed, _ = is_shell_command_allowed("cd src-python")
        assert allowed is True

    def test_safe_external_command_allowed(self):
        """SAFE_COMMAND_WHITELIST 中的外部命令应放行。"""
        from tools.builtin.system_tool_safety import is_shell_command_allowed
        allowed, _ = is_shell_command_allowed("python script.py")
        assert allowed is True

    def test_git_allowed(self):
        """git 应放行。"""
        from tools.builtin.system_tool_safety import is_shell_command_allowed
        allowed, _ = is_shell_command_allowed("git status")
        assert allowed is True

    def test_unknown_command_rejected(self):
        """不在白名单中的命令应拒绝。"""
        from tools.builtin.system_tool_safety import is_shell_command_allowed
        allowed, reason = is_shell_command_allowed("curl http://evil.com")
        assert allowed is False
        assert "curl" in reason or "whitelist" in reason.lower()

    def test_empty_command_rejected(self):
        """空命令应拒绝。"""
        from tools.builtin.system_tool_safety import is_shell_command_allowed
        allowed, _ = is_shell_command_allowed("")
        assert allowed is False

    def test_whitespace_only_rejected(self):
        """纯空格命令应拒绝。"""
        from tools.builtin.system_tool_safety import is_shell_command_allowed
        allowed, _ = is_shell_command_allowed("   ")
        assert allowed is False

    def test_case_insensitive(self):
        """命令大小写不敏感（Windows cmd 不区分大小写）。"""
        from tools.builtin.system_tool_safety import is_shell_command_allowed
        allowed, _ = is_shell_command_allowed("CD src-python")
        assert allowed is True


# ═══════════════════════════════════════════════════════════════
# validate_source_command 测试
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def shell_workspace(tmp_path, monkeypatch):
    """隔离的 shell 工作目录。"""
    ws = str(tmp_path / "shell_ws")
    os.makedirs(ws, exist_ok=True)
    monkeypatch.setenv("YUNCAT_WORKSPACE", ws)
    return ws


class TestValidateSourceCommand:
    """Task 7.10: source/. 命令的脚本内容检测。"""

    def test_non_source_command_passes(self, shell_workspace):
        """非 source 命令直接放行。"""
        from tools.builtin.system_tool_safety import validate_source_command
        safe, _ = validate_source_command("cd src-python", shell_workspace)
        assert safe is True

    def test_source_with_no_path_rejected(self, shell_workspace):
        """source 无路径参数应拒绝。"""
        from tools.builtin.system_tool_safety import validate_source_command
        safe, reason = validate_source_command("source", shell_workspace)
        assert safe is False
        assert "路径" in reason or "path" in reason.lower() or "argument" in reason.lower()

    def test_source_safe_script_passes(self, shell_workspace):
        """source 安全脚本应放行。"""
        from tools.builtin.system_tool_safety import validate_source_command
        script = os.path.join(shell_workspace, "setup.sh")
        with open(script, "w") as f:
            f.write("export FOO=bar\necho 'loaded'\n")
        safe, _ = validate_source_command(f"source {script}", shell_workspace)
        assert safe is True

    def test_source_dangerous_script_rejected(self, shell_workspace):
        """source 含 subprocess 的脚本应拒绝。"""
        from tools.builtin.system_tool_safety import validate_source_command
        script = os.path.join(shell_workspace, "evil.sh")
        with open(script, "w") as f:
            f.write("import subprocess\nsubprocess.call(['whoami'])\n")
        safe, reason = validate_source_command(f"source {script}", shell_workspace)
        assert safe is False
        assert "危险" in reason or "dangerous" in reason.lower() or "subprocess" in reason.lower()

    def test_source_network_script_rejected(self, shell_workspace):
        """source 含 requests 网络库的脚本应拒绝。"""
        from tools.builtin.system_tool_safety import validate_source_command
        script = os.path.join(shell_workspace, "exfil.sh")
        with open(script, "w") as f:
            f.write("import requests\nrequests.get('http://evil.com')\n")
        safe, _ = validate_source_command(f"source {script}", shell_workspace)
        assert safe is False

    def test_dot_alias_for_source(self, shell_workspace):
        """`. script.sh` 应等同于 `source script.sh`。"""
        from tools.builtin.system_tool_safety import validate_source_command
        script = os.path.join(shell_workspace, "setup.sh")
        with open(script, "w") as f:
            f.write("export FOO=bar\n")
        safe, _ = validate_source_command(f". {script}", shell_workspace)
        assert safe is True

    def test_source_relative_path_resolved(self, shell_workspace):
        """source 相对路径应相对于 shell_cwd 解析。"""
        from tools.builtin.system_tool_safety import validate_source_command
        script = os.path.join(shell_workspace, "env.sh")
        with open(script, "w") as f:
            f.write("export PATH=$PATH:/usr/local/bin\n")
        # 用相对路径
        safe, _ = validate_source_command("source env.sh", shell_workspace)
        assert safe is True

    def test_source_nonexistent_script_passes(self, shell_workspace):
        """source 不存在的脚本路径应放行（不误杀——路径可能由 shell 解析）。"""
        from tools.builtin.system_tool_safety import validate_source_command
        safe, _ = validate_source_command("source /nonexistent/path.sh", shell_workspace)
        assert safe is True

    def test_source_dangerous_relative_path_rejected(self, shell_workspace):
        """source 相对路径的危险脚本应被检测到。"""
        from tools.builtin.system_tool_safety import validate_source_command
        script = os.path.join(shell_workspace, "danger.sh")
        with open(script, "w") as f:
            f.write("import os\nos.system('rm -rf /')\n")
        safe, _ = validate_source_command("source danger.sh", shell_workspace)
        assert safe is False
