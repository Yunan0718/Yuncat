"""tests/unit/test_text_utils.py —— Task 6.15 截断溯源指针工具测试

覆盖 truncate_with_pointer 的契约：
  - 未超长 → 原文返回
  - 超长 + node_id → 截断 + "... [see node {id} for full content]"
  - 超长 + 无 node_id → 截断 + "..."（日志场景向后兼容）
  - None / 空串 / 边界值
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from text_utils import truncate_with_pointer


class TestTruncateNoOp:
    """未超长时原样返回"""

    def test_short_text_unchanged(self):
        assert truncate_with_pointer("hello", 100, "n1") == "hello"

    def test_exact_boundary_no_truncation(self):
        """长度恰好等于 max_chars → 不截断"""
        text = "a" * 50
        assert truncate_with_pointer(text, 50, "n1") == text

    def test_empty_text_unchanged(self):
        assert truncate_with_pointer("", 100, "n1") == ""

    def test_none_text_returns_empty(self):
        assert truncate_with_pointer(None, 100, "n1") == ""

    def test_zero_or_negative_max_returns_full(self):
        """max_chars <= 0 视为无限制（不截断）"""
        text = "some content"
        assert truncate_with_pointer(text, 0, "n1") == text
        assert truncate_with_pointer(text, -5, "n1") == text


class TestTruncateWithPointer:
    """超长 + node_id → 截断并附溯源指针"""

    def test_truncated_content_has_pointer(self):
        text = "x" * 200
        result = truncate_with_pointer(text, 100, "step-42")
        assert result.startswith("x" * 100)
        assert "... [see node step-42 for full content]" in result

    def test_pointer_contains_node_id(self):
        result = truncate_with_pointer("long text here" * 20, 50, "plan-node-abc")
        assert "see node plan-node-abc for full content" in result

    def test_truncated_length_is_content_plus_pointer(self):
        text = "a" * 300
        node_id = "n1"
        result = truncate_with_pointer(text, 100, node_id)
        expected = "a" * 100 + f"... [see node {node_id} for full content]"
        assert result == expected

    def test_content_before_pointer_preserved(self):
        text = "ABCDEFGH" * 30  # 240 chars
        result = truncate_with_pointer(text, 80, "k9")
        # 前 80 字符应完整保留
        assert result[:80] == text[:80]


class TestTruncateWithoutPointer:
    """超长 + 无 node_id → 截断 + "..."（日志/纯展示场景）"""

    def test_no_node_id_plain_ellipsis(self):
        text = "y" * 200
        result = truncate_with_pointer(text, 100)
        assert result == "y" * 100 + "..."

    def test_explicit_none_node_id(self):
        text = "z" * 50
        result = truncate_with_pointer(text, 10, None)
        assert result == "z" * 10 + "..."


class TestTruncateEdgeCases:
    """边界场景"""

    def test_unicode_content_truncated_by_char_count(self):
        """CJK 字符按字符数（非字节）截断"""
        text = "中文测试" * 50  # 200 chars
        result = truncate_with_pointer(text, 100, "u1")
        assert result[:100] == text[:100]
        assert "see node u1" in result

    def test_multiline_text_truncated(self):
        text = "line\n" * 100  # 500 chars
        result = truncate_with_pointer(text, 200, "m1")
        assert result.startswith("line\n" * 40)
        assert "see node m1" in result

    def test_node_id_empty_string_falls_back_to_ellipsis(self):
        """空字符串 node_id 视为无指针（falsy）"""
        text = "a" * 200
        result = truncate_with_pointer(text, 100, "")
        assert result == "a" * 100 + "..."
