"""tests/unit/test_network_guard.py —— Round 1 S7 网络隔离守卫测试

覆盖:
- 三档模式：off / warn / block
- 网络命令识别（pip/npm/git push 等）
- 非网络命令总是放行
- 无效模式默认 off
- 模式切换
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture(autouse=True)
def reset_mode():
    """每个测试后重置为 off，防测试间污染"""
    from security.network_guard import set_network_mode
    set_network_mode("off")
    yield
    set_network_mode("off")


class TestOffMode:
    """S7: off 模式（默认）——不禁网"""

    def test_off_mode_allows_pip_install(self):
        from security.network_guard import is_network_allowed
        allowed, reason = is_network_allowed("pip install requests")
        assert allowed is True
        assert reason == ""

    def test_off_mode_allows_git_push(self):
        from security.network_guard import is_network_allowed
        allowed, _ = is_network_allowed("git push origin main")
        assert allowed is True

    def test_off_mode_allows_non_network(self):
        from security.network_guard import is_network_allowed
        allowed, _ = is_network_allowed("dir")
        assert allowed is True


class TestWarnMode:
    """S7: warn 模式——允许但告警"""

    def test_warn_mode_allows_pip_install(self):
        from security.network_guard import set_network_mode, is_network_allowed
        set_network_mode("warn")
        allowed, reason = is_network_allowed("pip install requests")
        assert allowed is True
        assert reason == ""  # warn 不返回拒绝原因

    def test_warn_mode_allows_non_network(self):
        from security.network_guard import set_network_mode, is_network_allowed
        set_network_mode("warn")
        allowed, _ = is_network_allowed("echo hello")
        assert allowed is True


class TestBlockMode:
    """S7: block 模式——拒绝网络命令"""

    def test_block_mode_blocks_pip_install(self):
        from security.network_guard import set_network_mode, is_network_allowed
        set_network_mode("block")
        allowed, reason = is_network_allowed("pip install requests")
        assert allowed is False
        assert "pip" in reason.lower() or "block" in reason.lower()

    def test_block_mode_blocks_npm_install(self):
        from security.network_guard import set_network_mode, is_network_allowed
        set_network_mode("block")
        allowed, reason = is_network_allowed("npm install lodash")
        assert allowed is False
        assert "npm" in reason.lower() or "block" in reason.lower()

    def test_block_mode_blocks_git_push(self):
        from security.network_guard import set_network_mode, is_network_allowed
        set_network_mode("block")
        allowed, _ = is_network_allowed("git push origin main")
        assert allowed is False

    def test_block_mode_blocks_git_clone(self):
        from security.network_guard import set_network_mode, is_network_allowed
        set_network_mode("block")
        allowed, _ = is_network_allowed("git clone https://github.com/evil/repo")
        assert allowed is False

    def test_block_mode_allows_non_network(self):
        """block 模式下非网络命令仍放行"""
        from security.network_guard import set_network_mode, is_network_allowed
        set_network_mode("block")
        allowed, reason = is_network_allowed("dir")
        assert allowed is True
        assert reason == ""

    def test_block_mode_allows_git_status(self):
        """git status 不是网络操作，block 模式放行"""
        from security.network_guard import set_network_mode, is_network_allowed
        set_network_mode("block")
        allowed, _ = is_network_allowed("git status")
        assert allowed is True

    def test_block_mode_allows_git_log(self):
        """git log 不是网络操作"""
        from security.network_guard import set_network_mode, is_network_allowed
        set_network_mode("block")
        allowed, _ = is_network_allowed("git log --oneline")
        assert allowed is True


class TestModeSwitching:
    """S7: 模式切换"""

    def test_invalid_mode_defaults_to_off(self):
        from security.network_guard import set_network_mode, get_network_mode
        set_network_mode("invalid")
        assert get_network_mode() == "off"

    def test_empty_mode_defaults_to_off(self):
        from security.network_guard import set_network_mode, get_network_mode
        set_network_mode("")
        assert get_network_mode() == "off"

    def test_none_mode_defaults_to_off(self):
        from security.network_guard import set_network_mode, get_network_mode
        set_network_mode(None)
        assert get_network_mode() == "off"

    def test_case_insensitive(self):
        from security.network_guard import set_network_mode, get_network_mode
        set_network_mode("BLOCK")
        assert get_network_mode() == "block"

    def test_get_network_mode_returns_current(self):
        from security.network_guard import set_network_mode, get_network_mode
        set_network_mode("warn")
        assert get_network_mode() == "warn"
        set_network_mode("block")
        assert get_network_mode() == "block"


class TestNetworkCommandDetection:
    """S7: 网络命令识别覆盖"""

    def test_pip3_install_detected(self):
        from security.network_guard import set_network_mode, is_network_allowed
        set_network_mode("block")
        allowed, _ = is_network_allowed("pip3 install flask")
        assert allowed is False

    def test_yarn_add_detected(self):
        from security.network_guard import set_network_mode, is_network_allowed
        set_network_mode("block")
        allowed, _ = is_network_allowed("yarn add express")
        assert allowed is False

    def test_pnpm_install_detected(self):
        from security.network_guard import set_network_mode, is_network_allowed
        set_network_mode("block")
        allowed, _ = is_network_allowed("pnpm install")
        assert allowed is False

    def test_docker_pull_detected(self):
        from security.network_guard import set_network_mode, is_network_allowed
        set_network_mode("block")
        allowed, _ = is_network_allowed("docker pull ubuntu")
        assert allowed is False

    def test_cargo_install_detected(self):
        from security.network_guard import set_network_mode, is_network_allowed
        set_network_mode("block")
        allowed, _ = is_network_allowed("cargo install serde")
        assert allowed is False

    def test_go_get_detected(self):
        from security.network_guard import set_network_mode, is_network_allowed
        set_network_mode("block")
        allowed, _ = is_network_allowed("go get github.com/gin-gonic/gin")
        assert allowed is False

    def test_git_fetch_detected(self):
        from security.network_guard import set_network_mode, is_network_allowed
        set_network_mode("block")
        allowed, _ = is_network_allowed("git fetch origin")
        assert allowed is False
