"""tests/unit/test_prompts.py —— 提示词模板测试"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestRulesFormatter:
    """测试 Rules 格式化器"""

    def test_empty_rules_returns_empty_string(self):
        """空规则列表返回空字符串"""
        from prompts.rules_formatter import format_rules_as_context
        result = format_rules_as_context([])
        assert result == ""

    def test_formats_single_rule(self):
        """单条规则格式化"""
        from prompts.rules_formatter import format_rules_as_context
        rules = [{"description": "所有文件写入需要确认", "enabled": True}]
        result = format_rules_as_context(rules)
        assert "必须遵守" in result
        assert "所有文件写入需要确认" in result

    def test_skips_disabled_rules(self):
        """跳过禁用的规则"""
        from prompts.rules_formatter import format_rules_as_context
        rules = [
            {"description": "规则A", "enabled": True},
            {"description": "规则B", "enabled": False},
        ]
        result = format_rules_as_context(rules)
        assert "规则A" in result
        assert "规则B" not in result

    def test_multiple_rules_numbered(self):
        """多条规则有编号"""
        from prompts.rules_formatter import format_rules_as_context
        rules = [
            {"description": "规则1", "enabled": True},
            {"description": "规则2", "enabled": True},
        ]
        result = format_rules_as_context(rules)
        assert "1. 规则1" in result
        assert "2. 规则2" in result
