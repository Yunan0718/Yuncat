"""tests/unit/test_deliberation_spec_link.py —— Task 7.1 子任务1: 通用讨论→Spec 自动衔接

验证 run_deliberation 返回值包含 spec_input 字段，将讨论共识格式化为
可直接传入 run_spec_loop 的 task_input。

对标 v1 §3.1-3.3: 讨论（提案→交叉审查→对抗验证→共识）→ Spec（Architect→Developer→Auditor）
两条路径应通过 spec_input 字段衔接，无需调用方手动拼接。
"""
import os
import sys
import pytest

src_path = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
sys.path.insert(0, src_path)


class TestBuildSpecInput:
    """Task 7.1 子任务1: _build_spec_input 生成 Spec-ready 输入"""

    def test_returns_non_empty_string(self):
        """spec_input 为非空字符串"""
        from services.deliberation.deliberation_discussion import _build_spec_input
        result = _build_spec_input(
            task_input="实现一个用户登录功能",
            consensus="推荐使用 JWT + bcrypt 方案",
            proposals=[],
        )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_contains_original_task_input(self):
        """spec_input 包含原始需求"""
        from services.deliberation.deliberation_discussion import _build_spec_input
        result = _build_spec_input(
            task_input="实现一个用户登录功能",
            consensus="推荐方案",
            proposals=[],
        )
        assert "用户登录" in result

    def test_contains_consensus(self):
        """spec_input 包含讨论共识"""
        from services.deliberation.deliberation_discussion import _build_spec_input
        result = _build_spec_input(
            task_input="需求",
            consensus="推荐使用 JWT + bcrypt 方案，分 3 层实现",
            proposals=[],
        )
        assert "JWT" in result
        assert "bcrypt" in result

    def test_includes_structured_risks(self):
        """spec_input 包含结构化风险（来自提案的 _structured.risk）"""
        from services.deliberation.deliberation_discussion import _build_spec_input
        proposals = [
            {"role_name": "Security", "output": "...",
             "_structured": {"proposal": ["用 JWT"], "risk": ["Token 过期处理", "XSS 防护"]}},
            {"role_name": "UX", "output": "...",
             "_structured": {"proposal": ["响应式布局"], "risk": ["移动端兼容"]}},
        ]
        result = _build_spec_input(
            task_input="需求",
            consensus="共识",
            proposals=proposals,
        )
        assert "Token 过期处理" in result
        assert "XSS 防护" in result
        assert "移动端兼容" in result

    def test_no_risks_still_works(self):
        """无风险数据时 spec_input 仍正常生成"""
        from services.deliberation.deliberation_discussion import _build_spec_input
        proposals = [
            {"role_name": "Architect", "output": "...", "_structured": {}},
        ]
        result = _build_spec_input(
            task_input="需求",
            consensus="共识",
            proposals=proposals,
        )
        assert "共识" in result
        assert "需求" in result

    def test_handles_empty_consensus(self):
        """共识为空时 spec_input 仍包含原始需求"""
        from services.deliberation.deliberation_discussion import _build_spec_input
        result = _build_spec_input(
            task_input="原始需求文本",
            consensus="",
            proposals=[],
        )
        assert "原始需求文本" in result
