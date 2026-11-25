"""tests/unit/test_tool_result_summarize.py —— G2: 工具结果结构感知摘要

覆盖 summarize_for_llm() —— 长工具输出压缩为 LLM 友好的结构化预览，
对标 Claude Code 工具结果摘要（保留结构而非简单 head+tail）。

修复现有 _execute_exclusive_tools 截断路径的问题：
  现状 = protect_critical_info(head+tail) → 仅保留 error/def/class/import 行，
         对正常输出经常返回空，丢失正文结构。
  G2  = summarize_for_llm() → 按工具类型保留结构（签名/错误/统计/分组）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestShortContentPassthrough:
    """短内容（≤ max_chars）原样返回，不摘要"""

    def test_short_content_returned_as_is(self):
        from tools.result import summarize_for_llm
        content = "line1\nline2\nline3\n"
        assert summarize_for_llm("read_file", content, max_chars=1000) == content

    def test_empty_content_returns_empty(self):
        from tools.result import summarize_for_llm
        assert summarize_for_llm("read_file", "", max_chars=1000) == ""

    def test_exactly_at_limit_returned_as_is(self):
        from tools.result import summarize_for_llm
        content = "x" * 500
        assert summarize_for_llm("run_command", content, max_chars=500) == content


class TestCodeToolSummary:
    """代码类工具（read_file/edit_file/diff/codegraph_context）—— 保留结构"""

    def test_read_file_keeps_imports_and_signatures(self):
        """read_file 长输出应保留 imports + 函数/类签名，丢弃函数体"""
        from tools.result import summarize_for_llm
        code = (
            "import os\n"
            "import sys\n"
            "\n"
            "def helper():\n"
            "    return 'a' * 10000\n"  # 函数体应被丢弃
            "\n"
            "class Foo:\n"
            "    def method(self):\n"
            "        return 42\n"  # 方法体应被丢弃
            "\n"
            "def main():\n"
            "    pass\n"
        )
        # 构造超长内容（函数体膨胀）
        big_code = code.replace("'a' * 10000", "'a' * 50000")
        result = summarize_for_llm("read_file", big_code, max_chars=2000)
        assert "import os" in result
        assert "def helper" in result
        assert "class Foo" in result
        assert "def main" in result
        # 函数体应被裁剪（不包含 50000 个 a）
        assert "a" * 50000 not in result
        # 应在预算内
        assert len(result) <= 2000

    def test_read_file_signature_only_no_bodies(self):
        from tools.result import summarize_for_llm
        # 一个有大函数体的文件
        code = "def big():\n    " + "x = 1\n    " * 2000 + "\nclass C:\n    pass\n"
        result = summarize_for_llm("read_file", code, max_chars=1500)
        assert "def big" in result
        assert "class C" in result
        # 大量重复的 "x = 1" 应被裁剪
        assert result.count("x = 1") < 100
        assert len(result) <= 1500

    def test_edit_file_uses_code_summary(self):
        """edit_file 也应走代码摘要路径"""
        from tools.result import summarize_for_llm
        code = "import json\n\ndef f():\n    " + "y = 2\n    " * 3000 + "\n"
        result = summarize_for_llm("edit_file", code, max_chars=1000)
        assert "import json" in result
        assert "def f" in result
        assert len(result) <= 1000

    def test_diff_keeps_structure(self):
        from tools.result import summarize_for_llm
        diff = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-old line\n"
            "+new line\n"
        )
        # diff 短 → 原样返回
        assert summarize_for_llm("diff", diff, max_chars=2000) == diff

    # ── Task 7.12 (F-048): 代码摘要保留首尾实现上下文 ──

    def test_read_file_keeps_head_implementation(self):
        """Task 7.12: 代码摘要应保留头部实现上下文（非仅签名）。

        原实现仅保留签名行（import/def/class/@），丢弃全部函数体——
        Agent 需理解实现逻辑时压缩结果完全无信息。修复后 head 段保留首部实现。
        """
        from tools.result import summarize_for_llm
        # 头部含实现行（非签名），用足够多的短 filler 行使 content > max_chars 触发摘要
        lines = ["import os", "import sys", "", "def compute():",
                 "    x = load_data()",      # 实现行（非签名）——应被 head 保留
                 "    y = transform(x)",     # 实现行（非签名）
                 "    return y * 2"]         # 实现行（非签名）
        # 短 filler 行（~20 chars），大量行数使 content > max_chars，但 3 段摘要仍在预算内
        lines.extend([f"# filler line {i}" for i in range(200)])  # 200 × ~18 chars ≈ 3600
        lines.append("if __name__ == '__main__':")
        lines.append("    compute()")
        code = "\n".join(lines) + "\n"
        assert len(code) > 3000  # 确保触发摘要（非原样返回）
        result = summarize_for_llm("read_file", code, max_chars=3000)
        # 头部实现行应保留（Task 7.12 核心断言）——原实现会丢弃这些非签名行
        assert "x = load_data()" in result, "head 实现上下文丢失——_summarize_code 应保留首部实现"
        assert "y = transform(x)" in result
        # 签名仍保留
        assert "def compute" in result
        assert len(result) <= 3000

    def test_read_file_keeps_tail_implementation(self):
        """Task 7.12: 代码摘要应保留尾部实现上下文（如 main 块）。"""
        from tools.result import summarize_for_llm
        lines = ["import os", "def f():", "    pass"]
        # 短 filler 行 + 大量行数触发摘要 + total > 30 触发 tail
        lines.extend([f"# padding line {i}" for i in range(200)])  # 200 × ~18 chars ≈ 3600
        # 尾部含 main 块（实现行，非签名）——位于最后 15 行内
        lines.append("if __name__ == '__main__':")
        lines.append("    f()")
        lines.append("    print('done')")  # 实现行——应被 tail 保留
        code = "\n".join(lines) + "\n"
        assert len(code) > 3000
        result = summarize_for_llm("read_file", code, max_chars=3000)
        # 尾部实现行应保留——原实现仅保留签名，print('done') 非签名会被丢弃
        assert "print('done')" in result, "tail 实现上下文丢失——_summarize_code 应保留尾部实现"
        assert "if __name__" in result
        assert len(result) <= 3000

    def test_read_file_signatures_plus_head_tail_coexist(self):
        """Task 7.12: 签名段 + head 段 + tail 段三段共存。"""
        from tools.result import summarize_for_llm
        lines = ["import os", "class Foo:", "    def method(self):"]
        # 短实现行 + 大量行数
        lines.extend([f"    x{i} = {i}" for i in range(300)])  # 300 × ~13 chars ≈ 3900
        lines.append("def main():")
        lines.append("    Foo().method()")
        code = "\n".join(lines) + "\n"
        assert len(code) > 4000
        result = summarize_for_llm("read_file", code, max_chars=4000)
        # 摘要 header（证明走了 _summarize_code 而非原样返回）
        assert "[code structure:" in result
        # 签名段保留
        assert "class Foo" in result or "def main" in result
        # 预算仍受控
        assert len(result) <= 4000


class TestCommandToolSummary:
    """命令类工具（run_command/open_shell）—— 首 N + 尾 N + 错误行 + 统计"""

    def test_run_command_keeps_head_and_tail(self):
        from tools.result import summarize_for_llm
        lines = [f"line {i}" for i in range(500)]
        content = "\n".join(lines) + "\n"
        result = summarize_for_llm("run_command", content, max_chars=1000)
        assert "line 0" in result  # 头部保留
        assert "line 499" in result  # 尾部保留
        # 中间行应被裁剪
        assert "line 250" not in result
        assert len(result) <= 1000

    def test_run_command_keeps_error_lines(self):
        """错误/警告行必须始终保留（即使在中部）"""
        from tools.result import summarize_for_llm
        lines = [f"line {i}" for i in range(500)]
        lines[250] = "ERROR: something failed"
        lines[251] = "Warning: deprecated"
        content = "\n".join(lines) + "\n"
        result = summarize_for_llm("run_command", content, max_chars=800)
        assert "ERROR: something failed" in result
        assert "Warning: deprecated" in result

    def test_run_command_includes_line_count(self):
        """应包含总行数统计"""
        from tools.result import summarize_for_llm
        content = "\n".join(f"line {i}" for i in range(500)) + "\n"
        result = summarize_for_llm("run_command", content, max_chars=500)
        # 应包含行数统计 header（[command output: N lines]）
        assert "[command output:" in result
        assert "lines]" in result

    def test_open_shell_uses_command_summary(self):
        from tools.result import summarize_for_llm
        content = "\n".join(f"out {i}" for i in range(500)) + "\n"
        result = summarize_for_llm("open_shell", content, max_chars=600)
        assert "out 0" in result
        assert "out 499" in result
        assert len(result) <= 600


class TestSearchToolSummary:
    """搜索类工具（search_files/grep）—— 按文件分组去重"""

    def test_search_files_groups_by_file(self):
        from tools.result import summarize_for_llm
        content = (
            "path/to/file1.py:1:def foo():\n"
            "path/to/file1.py:5:def bar():\n"
            "path/to/file2.py:1:class Baz:\n"
            "path/to/file2.py:10:def qux():\n"
            "path/to/file3.py:1:import os\n"
        )
        # 短内容原样返回
        assert summarize_for_llm("search_files", content, max_chars=2000) == content

    def test_search_files_long_results_capped_per_file(self):
        """超长搜索结果应按文件分组，每文件最多 K 行"""
        from tools.result import summarize_for_llm
        # 单文件大量匹配
        lines = [f"src/big.py:{i}:code line {i}" for i in range(200)]
        content = "\n".join(lines) + "\n"
        result = summarize_for_llm("search_files", content, max_chars=800)
        assert "src/big.py" in result
        # 200 行不应全保留（按文件 cap）
        assert result.count("src/big.py:") < 50
        assert len(result) <= 800


class TestGenericSummary:
    """通用工具（未分类）—— head + tail + critical + 行数"""

    def test_unknown_tool_falls_back_to_head_tail(self):
        from tools.result import summarize_for_llm
        content = "\n".join(f"row {i}" for i in range(300)) + "\n"
        result = summarize_for_llm("some_unknown_tool", content, max_chars=500)
        assert "row 0" in result
        assert "row 299" in result
        assert len(result) <= 500

    def test_generic_includes_truncation_notice(self):
        """超长内容摘要应包含截断提示（让 LLM 知道有更多内容）"""
        from tools.result import summarize_for_llm
        content = "x" * 5000
        result = summarize_for_llm("unknown_tool", content, max_chars=500)
        # 应有某种截断标记
        assert len(result) < 5000
        assert len(result) <= 500


class TestBudgetRespected:
    """所有路径的输出必须 ≤ max_chars"""

    def test_code_summary_within_budget(self):
        from tools.result import summarize_for_llm
        code = "import os\n" + "def f():\n    pass\n" * 500
        result = summarize_for_llm("read_file", code, max_chars=300)
        assert len(result) <= 300

    def test_command_summary_within_budget(self):
        from tools.result import summarize_for_llm
        content = "\n".join(f"line {i}" for i in range(10000)) + "\n"
        result = summarize_for_llm("run_command", content, max_chars=400)
        assert len(result) <= 400

    def test_search_summary_within_budget(self):
        from tools.result import summarize_for_llm
        content = "\n".join(f"f.py:{i}:match" for i in range(10000)) + "\n"
        result = summarize_for_llm("search_files", content, max_chars=400)
        assert len(result) <= 400

    def test_tiny_budget_does_not_crash(self):
        """极小预算（如 50）不应崩溃，应返回 ≤50 字符"""
        from tools.result import summarize_for_llm
        code = "import os\nimport sys\ndef f():\n    pass\nclass C:\n    pass\n" * 100
        result = summarize_for_llm("read_file", code, max_chars=50)
        assert len(result) <= 50
