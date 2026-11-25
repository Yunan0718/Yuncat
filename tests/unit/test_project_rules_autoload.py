"""tests/unit/test_project_rules_autoload.py —— M1: AGENTS.md/CLAUDE.md 自动加载

覆盖 load_project_rules() —— 自动读取 workspace 根目录的 AGENTS.md / CLAUDE.md，
注入 agent 上下文。对标 Cursor .cursorrules / Claude Code CLAUDE.md 自动读取。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestLoadProjectRules:
    """load_project_rules 行为测试"""

    def test_no_rule_files_returns_empty(self, tmp_path):
        """workspace 无 AGENTS.md/CLAUDE.md → 返回空字符串"""
        from services.agent.agent_context import load_project_rules
        assert load_project_rules(workspace=tmp_path) == ""

    def test_only_agents_md_loaded(self, tmp_path):
        """只有 AGENTS.md → 读取并带 header"""
        (tmp_path / "AGENTS.md").write_text("# Project Rules\n- 使用 pytest\n", encoding="utf-8")
        from services.agent.agent_context import load_project_rules
        result = load_project_rules(workspace=tmp_path)
        assert "使用 pytest" in result
        assert "AGENTS.md" in result  # header 标识来源

    def test_only_claude_md_loaded(self, tmp_path):
        """只有 CLAUDE.md → 读取并带 header"""
        (tmp_path / "CLAUDE.md").write_text("# Claude Rules\n- 函数 <50 行\n", encoding="utf-8")
        from services.agent.agent_context import load_project_rules
        result = load_project_rules(workspace=tmp_path)
        assert "函数 <50 行" in result
        assert "CLAUDE.md" in result

    def test_both_files_agents_first(self, tmp_path):
        """两者都存在 → AGENTS.md 在前（主流标准优先）"""
        (tmp_path / "AGENTS.md").write_text("AGENTS_CONTENT_MARKER", encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("CLAUDE_CONTENT_MARKER", encoding="utf-8")
        from services.agent.agent_context import load_project_rules
        result = load_project_rules(workspace=tmp_path)
        agents_pos = result.find("AGENTS_CONTENT_MARKER")
        claude_pos = result.find("CLAUDE_CONTENT_MARKER")
        assert agents_pos >= 0
        assert claude_pos >= 0
        assert agents_pos < claude_pos  # AGENTS.md 内容在前

    def test_budget_truncation(self, tmp_path):
        """超长内容按 budget 截断并附提示"""
        (tmp_path / "AGENTS.md").write_text("x" * 5000, encoding="utf-8")
        from services.agent.agent_context import load_project_rules
        result = load_project_rules(workspace=tmp_path, budget=1000)
        assert len(result) <= 1100  # 允许截断提示的小余量
        assert "截断" in result or "truncat" in result.lower()

    def test_file_read_error_returns_empty(self, tmp_path):
        """文件读取异常 → 返回空字符串（不抛异常）"""
        from services.agent.agent_context import load_project_rules
        # 传入一个不存在的路径（tmp_path 下的不存在的子目录）
        # load_project_rules 应优雅处理
        result = load_project_rules(workspace=tmp_path / "nonexistent")
        assert result == ""

    def test_default_workspace_when_none(self, tmp_path, monkeypatch):
        """workspace=None → 用 get_workspace() 默认值"""
        # 设置 YUNCAT_WORKSPACE 指向 tmp_path
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path))
        (tmp_path / "AGENTS.md").write_text("DEFAULT_WS_MARKER", encoding="utf-8")
        from services.agent.agent_context import load_project_rules
        result = load_project_rules(workspace=None)
        assert "DEFAULT_WS_MARKER" in result

    def test_empty_file_treated_as_no_rules(self, tmp_path):
        """空文件视为无规则（不注入空 header）"""
        (tmp_path / "AGENTS.md").write_text("", encoding="utf-8")
        from services.agent.agent_context import load_project_rules
        result = load_project_rules(workspace=tmp_path)
        assert result == ""

    def test_unicode_content_preserved(self, tmp_path):
        """中文/Unicode 内容正确读取（不乱码）"""
        (tmp_path / "AGENTS.md").write_text(
            "# 项目规则\n- 所有注释用中文\n- 函数不超过 50 行\n", encoding="utf-8"
        )
        from services.agent.agent_context import load_project_rules
        result = load_project_rules(workspace=tmp_path)
        assert "项目规则" in result
        assert "所有注释用中文" in result
        assert "函数不超过 50 行" in result


class TestProjectRulesIntegration:
    """集成行为——验证 load_project_rules 输出可拼入 rules_context"""

    def test_output_prependable_to_rules_context(self, tmp_path):
        """输出应能直接拼到现有 rules_context 前"""
        (tmp_path / "AGENTS.md").write_text("PROJECT_RULE_A", encoding="utf-8")
        from services.agent.agent_context import load_project_rules
        project_rules = load_project_rules(workspace=tmp_path)
        existing_rules = "[规则] 用户自定义规则"
        combined = project_rules + "\n\n" + existing_rules
        assert "PROJECT_RULE_A" in combined
        assert existing_rules in combined
        # 项目规则应在用户规则前
        assert combined.find("PROJECT_RULE_A") < combined.find(existing_rules)
