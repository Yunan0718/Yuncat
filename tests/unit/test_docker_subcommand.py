"""tests/unit/test_docker_subcommand.py —— Task 3.7: docker 白名单改 ask/deny

对标:
- 容器逃逸防护: docker run --privileged -v /:/host alpine 可挂载宿主根分区
- 主流做法: docker 子命令分级（只读放行 / 变更需确认 / 未知拒绝）

原问题: SAFE_COMMAND_WHITELIST 盲目包含 docker/docker-compose，任何 docker 子命令
都通过白名单，包括高危的 run --privileged。

修复策略:
- 从白名单移除 docker/docker-compose
- 只读子命令（ps/images/logs/inspect/stats/top/version/info）→ 放行（仍走 confirmable）
- 变更子命令（run/exec/build/push/pull/create/start/stop/rm 等）→ 放行但 LLM 标记风险
- 未知子命令 → 拒绝（默认拒绝优于盲目放行）
- 无子命令的 docker → 拒绝（无意义，可能是探测）

覆盖:
- 只读子命令放行（docker ps / docker images / docker-compose logs）
- 变更子命令放行（docker run / docker exec / docker-compose up）
- 未知子命令拒绝（docker evil-cmd）
- 无子命令拒绝（裸 docker）
- docker-compose 同分级
- 高危参数仍通过 is_safe_command（LLM 风险分析在上游拦截 HIGH）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestDockerSubcommandClassification:
    """docker/docker-compose 子命令分级——只读/变更放行，未知拒绝"""

    @pytest.fixture(autouse=True)
    def _ws(self, tmp_path, monkeypatch):
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
        from security.sandbox import get_workspace
        get_workspace()

    def test_docker_readonly_ps_allowed(self):
        """docker ps（只读子命令）→ 放行"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker ps") is True

    def test_docker_readonly_images_allowed(self):
        """docker images（只读）→ 放行"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker images") is True

    def test_docker_readonly_logs_allowed(self):
        """docker logs（只读）→ 放行"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker logs mycontainer") is True

    def test_docker_readonly_inspect_allowed(self):
        """docker inspect（只读）→ 放行"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker inspect mycontainer") is True

    def test_docker_readonly_stats_allowed(self):
        """docker stats（只读）→ 放行"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker stats") is True

    def test_docker_readonly_top_allowed(self):
        """docker top（只读）→ 放行"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker top mycontainer") is True

    def test_docker_readonly_version_info_allowed(self):
        """docker version / docker info（只读）→ 放行"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker version") is True
        assert is_safe_command("docker info") is True

    def test_docker_ask_run_allowed(self):
        """docker run（变更子命令）→ 放行（走 confirmable + LLM 风险分析）"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker run alpine") is True

    def test_docker_ask_exec_allowed(self):
        """docker exec（变更）→ 放行"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker exec mycontainer ls") is True

    def test_docker_ask_build_allowed(self):
        """docker build（变更）→ 放行"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker build -t myimg .") is True

    def test_docker_ask_push_pull_allowed(self):
        """docker push/pull（变更）→ 放行"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker pull ubuntu") is True
        assert is_safe_command("docker push myimg") is True

    def test_docker_privileged_run_still_passes_safety(self):
        """docker run --privileged -v /:/host alpine → is_safe_command 放行（LLM 风险分析在上游拦截 HIGH）

        注意: is_safe_command 只做白名单分类，不做语义风险评分。
        高危参数（--privileged / -v /:/host）由 services/llm_security.analyze_command_safety
        在 tool_service._validate_and_prepare 中评分并拦截 HIGH。
        本测试固化"is_safe_command 不拦截合法子命令的参数变体"——参数风险由 LLM 层处理。
        """
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker run --privileged -v /:/host alpine") is True

    def test_docker_unknown_subcommand_rejected(self):
        """docker evil-cmd（未知子命令）→ 拒绝（默认拒绝优于盲目放行）"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker evil-cmd") is False

    def test_docker_no_subcommand_rejected(self):
        """裸 docker（无子命令）→ 拒绝（无意义，可能是探测）"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker") is False

    def test_docker_compose_readonly_allowed(self):
        """docker-compose ps / logs（只读）→ 放行"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker-compose ps") is True
        assert is_safe_command("docker-compose logs") is True

    def test_docker_compose_ask_allowed(self):
        """docker-compose up / build / exec（变更）→ 放行"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker-compose up -d") is True
        assert is_safe_command("docker-compose build") is True
        assert is_safe_command("docker-compose exec web ls") is True

    def test_docker_compose_unknown_rejected(self):
        """docker-compose evil-cmd（未知）→ 拒绝"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("docker-compose evil-cmd") is False

    def test_docker_removed_from_whitelist(self):
        """docker 不再在 SAFE_COMMAND_WHITELIST 中（不再盲目放行）"""
        from tools.builtin.system_tool_safety import SAFE_COMMAND_WHITELIST
        assert "docker" not in SAFE_COMMAND_WHITELIST
        assert "docker-compose" not in SAFE_COMMAND_WHITELIST
