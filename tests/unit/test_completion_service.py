"""tests/unit/test_completion_service.py — Phase 49-O 本地行内补全服务测试"""
import pytest

from services.completion_service import suggest_inline_completions


class TestCompletionService:
    def test_keyword_def_suggestion(self):
        items = suggest_inline_completions("python", "def ", "")
        assert len(items) == 1
        assert "def name(self):" in items[0]["insertText"]

    def test_keyword_function_suggestion_typescript(self):
        items = suggest_inline_completions("typescript", "function ", "")
        assert len(items) == 1
        assert "function name()" in items[0]["insertText"]

    def test_no_suggestion_for_empty_line(self):
        items = suggest_inline_completions("python", "    ", "")
        assert items == []

    def test_repetition_suggestion(self):
        prefix = 'name = "foo"\nname = '
        items = suggest_inline_completions("python", prefix, "")
        assert len(items) > 0
        assert any('"foo"' in i["insertText"] for i in items)

    def test_no_suggestion_when_no_pattern(self):
        items = suggest_inline_completions("python", "xyzzy = ", "")
        assert items == []

    def test_limit_three_items(self):
        prefix = 'a = 1\na = 1\na = 1\na = 1\na = '
        items = suggest_inline_completions("python", prefix, "")
        assert len(items) <= 3

    @pytest.mark.parametrize("kw", ["class ", "if ", "for ", "import ", "from "])
    def test_python_keyword_skeletons(self, kw):
        items = suggest_inline_completions("python", kw, "")
        assert len(items) == 1
        assert items[0]["insertText"].strip()
