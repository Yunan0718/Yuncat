"""tests/unit/test_system_tool_search.py —— Task 7.11: search_files ripgrep 集成

验证 search_files 的搜索契约（dual-path：ripgrep 可用 + os.walk fallback）：
1. 基本文本搜索——找到匹配文件
2. file_pattern 过滤——只搜指定 glob
3. use_regex=True——正则匹配
4. max_results 上限——超出停止
5. context_lines——上下文行
6. 跳过 node_modules/__pycache__/.git
7. 跳过隐藏目录
8. 无匹配返回空 results
9. 结果结构一致（file/line/excerpt 或 context keys 完整）
10. ripgrep 不可用时优雅回退到 os.walk
11. ripgrep 子进程失败时回退到 os.walk
12. files_scanned 统计

对标 Claude Code Grep 工具（ripgrep 原生引擎 + graceful degradation）。
ripgrep 在用户/CI 环境可能不可用，dual-path 测试保证两条路径行为一致。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from tools.builtin.system_tool_search import search_files


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def ws_workspace(tmp_path, monkeypatch):
    """创建临时 workspace 并设置 YUNCAT_WORKSPACE 环境变量。

    search_files 内部 from security.sandbox import get_workspace——
    get_workspace 从 YUNCAT_WORKSPACE 读取，setenv 即可让搜索落到 tmp_path。
    """
    monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path))
    return tmp_path


@pytest.fixture
def sample_files(ws_workspace):
    """在 workspace 下创建示例文件。

    布局：
      src/foo.py     —— 含 "hello"（def hello）+ "def" + "import"
      src/bar.ts     —— 含 "foo"（function foo）
      node_modules/skip.js  —— 含 "hello"（应被跳过）
      .hidden/secret.py    —— 含 "hello"（应被跳过——隐藏目录）
      big.py         —— 超大文件（应被跳过——超过 _MAX_SIZE）
    """
    src_dir = ws_workspace / "src"
    src_dir.mkdir()
    (src_dir / "foo.py").write_text(
        "import os\n"
        "def hello():\n"
        "    return 'world'\n"
        "def goodbye():\n"
        "    return 'bye'\n",
        encoding="utf-8",
    )
    (src_dir / "bar.ts").write_text(
        "export function foo() {\n"
        "  return 42;\n"
        "}\n",
        encoding="utf-8",
    )
    # node_modules 应被跳过
    nm_dir = ws_workspace / "node_modules"
    nm_dir.mkdir()
    (nm_dir / "skip.js").write_text(
        "const hello = 'should not match';\n", encoding="utf-8"
    )
    # 隐藏目录应被跳过
    hidden_dir = ws_workspace / ".hidden"
    hidden_dir.mkdir()
    (hidden_dir / "secret.py").write_text("hello = 'hidden'\n", encoding="utf-8")
    return ws_workspace


# ═══════════════════════════════════════════════════════════════
# ripgrep 检测（Task 7.11 新接口）
# ═══════════════════════════════════════════════════════════════

class TestRipgrepDetection:
    """Task 7.11: _find_ripgrep 检测 + 缓存"""

    def test_find_ripgrep_returns_string(self):
        """_find_ripgrep 返回字符串（空字符串表示不可用）。"""
        from tools.builtin.system_tool_search import _find_ripgrep, _reset_ripgrep_cache
        _reset_ripgrep_cache()
        result = _find_ripgrep()
        assert isinstance(result, str)

    def test_find_ripgrep_caches_result(self, monkeypatch):
        """_find_ripgrep 缓存结果——第二次调用不重新检测。"""
        from tools.builtin.system_tool_search import _find_ripgrep, _reset_ripgrep_cache
        _reset_ripgrep_cache()
        # 模拟 shutil.which 返回假路径，验证缓存
        call_count = {"n": 0}

        def fake_which(name):
            call_count["n"] += 1
            return "/fake/rg"

        # 直接 patch 模块内 shutil.which（_find_ripgrep 用 shutil.which）
        import shutil
        monkeypatch.setattr(shutil, "which", fake_which)
        first = _find_ripgrep()
        second = _find_ripgrep()
        assert first == second == "/fake/rg"
        # 缓存生效：shutil.which 只调用一次
        assert call_count["n"] == 1
        _reset_ripgrep_cache()


# ═══════════════════════════════════════════════════════════════
# 基本搜索契约（dual-path：ripgrep + os.walk fallback 行为一致）
# ═══════════════════════════════════════════════════════════════

class TestBasicSearch:
    """基本文本搜索——dual-path 一致性"""

    async def test_finds_matches_in_files(self, sample_files):
        """搜索 'hello' 应找到 src/foo.py 中的匹配（def hello）。"""
        result = await search_files(pattern="hello", path="")
        assert result["success"] is True
        assert result["data"]["total_matches"] >= 1
        files_found = [r["file"] for r in result["data"]["results"]]
        assert any("foo.py" in f for f in files_found), f"应找到 foo.py，实际: {files_found}"

    async def test_no_matches_returns_empty(self, sample_files):
        """搜索不存在的字符串返回空 results。"""
        result = await search_files(pattern="zzz_nonexistent_zzz", path="")
        assert result["success"] is True
        assert result["data"]["total_matches"] == 0
        assert result["data"]["results"] == []

    async def test_result_structure_excerpt(self, sample_files):
        """context_lines=0 时每个结果含 file/line/excerpt keys。"""
        result = await search_files(pattern="hello", path="", context_lines=0)
        assert result["success"] is True
        for entry in result["data"]["results"]:
            assert "file" in entry
            assert "line" in entry
            assert "excerpt" in entry

    async def test_result_structure_context(self, sample_files):
        """context_lines>0 时每个结果含 file/line/context keys。"""
        result = await search_files(pattern="hello", path="", context_lines=1)
        assert result["success"] is True
        for entry in result["data"]["results"]:
            assert "file" in entry
            assert "line" in entry
            assert "context" in entry

    async def test_files_scanned_reported(self, sample_files):
        """返回 files_scanned 统计（>=1）。"""
        result = await search_files(pattern="hello", path="")
        assert "files_scanned" in result["data"]
        assert result["data"]["files_scanned"] >= 1

    async def test_pattern_echoed_in_result(self, sample_files):
        """返回的 pattern 字段应回显输入。"""
        result = await search_files(pattern="hello", path="")
        assert result["data"]["pattern"] == "hello"


# ═══════════════════════════════════════════════════════════════
# file_pattern 过滤
# ═══════════════════════════════════════════════════════════════

class TestFilePatternFilter:
    async def test_file_pattern_filters_py_only(self, sample_files):
        """file_pattern='*.py' 只搜 .py 文件（bar.ts 不出现）。"""
        result = await search_files(pattern="foo", path="", file_pattern="*.py")
        assert result["success"] is True
        files_found = [r["file"] for r in result["data"]["results"]]
        assert not any(f.endswith(".ts") for f in files_found), \
            f"*.py 过滤失败，找到 .ts: {files_found}"

    async def test_file_pattern_filters_ts_only(self, sample_files):
        """file_pattern='*.ts' 只搜 .ts 文件。"""
        result = await search_files(pattern="foo", path="", file_pattern="*.ts")
        assert result["success"] is True
        files_found = [r["file"] for r in result["data"]["results"]]
        assert any(f.endswith(".ts") for f in files_found), \
            f"应找到 bar.ts，实际: {files_found}"
        assert not any(f.endswith(".py") for f in files_found)


# ═══════════════════════════════════════════════════════════════
# regex 模式
# ═══════════════════════════════════════════════════════════════

class TestRegexMode:
    async def test_regex_matches_pattern(self, sample_files):
        """use_regex=True 用正则匹配 'def \\w+\\(\\)' 应匹配 hello 和 goodbye。"""
        result = await search_files(
            pattern=r"def \w+\(\)", path="", use_regex=True
        )
        assert result["success"] is True
        # foo.py 有 def hello() 和 def goodbye()——至少 2 个匹配
        assert result["data"]["total_matches"] >= 2, \
            f"regex 应匹配至少 2 处，实际: {result['data']['total_matches']}"

    async def test_fixed_string_default(self, sample_files):
        """默认 use_regex=False 用 fixed-string 匹配（'def' 字面量）。"""
        result = await search_files(pattern="def", path="")
        assert result["success"] is True
        assert result["data"]["total_matches"] >= 1

    async def test_fixed_string_not_treated_as_regex(self, sample_files):
        """fixed-string 模式下 '.' 是字面量，不匹配任意字符。"""
        # 搜 "h.llo" 字面量——foo.py 的 "hello" 不应匹配（因为没有字面 "h.llo"）
        result = await search_files(pattern="h.llo", path="", use_regex=False)
        assert result["success"] is True
        # 不应把 "hello" 当作 "h.llo" 的正则匹配
        assert result["data"]["total_matches"] == 0


# ═══════════════════════════════════════════════════════════════
# max_results 上限
# ═══════════════════════════════════════════════════════════════

class TestMaxResults:
    async def test_max_results_caps_output(self, sample_files):
        """max_results 限制返回结果数。"""
        # 创建多匹配文件
        (sample_files / "multi.py").write_text(
            "hello = 1\nhello = 2\nhello = 3\nhello = 4\nhello = 5\n",
            encoding="utf-8",
        )
        result = await search_files(pattern="hello", path="", max_results=2)
        assert result["success"] is True
        assert len(result["data"]["results"]) <= 2


# ═══════════════════════════════════════════════════════════════
# context_lines 上下文
# ═══════════════════════════════════════════════════════════════

class TestContextLines:
    async def test_context_includes_surrounding_lines(self, sample_files):
        """context_lines=1 时 context 含匹配行 + 前后各 1 行。"""
        result = await search_files(pattern="hello", path="", context_lines=1)
        assert result["success"] is True
        # 找到 foo.py 的匹配
        foo_entries = [e for e in result["data"]["results"] if "foo.py" in e["file"]]
        assert len(foo_entries) >= 1
        ctx = foo_entries[0]["context"]
        # context 应包含 "def hello" 及其上下文
        assert "hello" in ctx
        # 应包含前后行之一（import os 或 return 'world'）
        assert "import os" in ctx or "return" in ctx

    async def test_context_zero_returns_excerpt(self, sample_files):
        """context_lines=0 时返回 excerpt（非 context）。"""
        result = await search_files(pattern="hello", path="", context_lines=0)
        assert result["success"] is True
        for entry in result["data"]["results"]:
            assert "excerpt" in entry
            assert "context" not in entry


# ═══════════════════════════════════════════════════════════════
# 跳过目录
# ═══════════════════════════════════════════════════════════════

class TestSkipDirectories:
    async def test_skips_node_modules(self, sample_files):
        """node_modules 中的匹配不应返回。"""
        result = await search_files(pattern="hello", path="")
        assert result["success"] is True
        files_found = [r["file"] for r in result["data"]["results"]]
        assert not any("node_modules" in f for f in files_found), \
            f"node_modules 未被跳过: {files_found}"

    async def test_skips_hidden_directories(self, sample_files):
        """隐藏目录（.hidden）中的匹配不应返回。"""
        result = await search_files(pattern="hello", path="")
        assert result["success"] is True
        files_found = [r["file"] for r in result["data"]["results"]]
        assert not any(".hidden" in f for f in files_found), \
            f".hidden 未被跳过: {files_found}"


# ═══════════════════════════════════════════════════════════════
# 路径验证
# ═══════════════════════════════════════════════════════════════

class TestPathValidation:
    async def test_invalid_path_returns_error(self, ws_workspace):
        """路径超出 workspace 应返回 success=False。"""
        result = await search_files(pattern="x", path="../../../../etc")
        assert result["success"] is False
        assert "error" in result

    async def test_subpath_search(self, sample_files):
        """path='src' 只搜 src 子目录。"""
        result = await search_files(pattern="hello", path="src")
        assert result["success"] is True
        files_found = [r["file"] for r in result["data"]["results"]]
        # 所有结果应在 src/ 下
        assert all("src" in f for f in files_found)


# ═══════════════════════════════════════════════════════════════
# ripgrep 不可用回退（Task 7.11 核心）
# ═══════════════════════════════════════════════════════════════

class TestRipgrepFallback:
    """Task 7.11: ripgrep 不可用 / 失败时优雅回退到 os.walk"""

    async def test_fallback_to_oswalk_when_unavailable(self, sample_files, monkeypatch):
        """ripgrep 不可用时（缓存=""）回退到 os.walk，搜索仍成功。"""
        import tools.builtin.system_tool_search as mod
        monkeypatch.setattr(mod, "_RIPGREP_CACHE", "")
        result = await search_files(pattern="hello", path="")
        assert result["success"] is True
        assert result["data"]["total_matches"] >= 1
        files_found = [r["file"] for r in result["data"]["results"]]
        assert any("foo.py" in f for f in files_found)

    async def test_fallback_on_subprocess_failure(self, sample_files, monkeypatch):
        """ripgrep 路径无效时（子进程失败）回退到 os.walk。"""
        import tools.builtin.system_tool_search as mod
        # 设置一个不存在的 rg 路径——subprocess 会 FileNotFoundError
        monkeypatch.setattr(mod, "_RIPGREP_CACHE", "/nonexistent/rg/path/bin/rg")
        result = await search_files(pattern="hello", path="")
        # 应回退到 os.walk，不报错
        assert result["success"] is True
        assert result["data"]["total_matches"] >= 1
        files_found = [r["file"] for r in result["data"]["results"]]
        assert any("foo.py" in f for f in files_found)

    async def test_fallback_skips_node_modules(self, sample_files, monkeypatch):
        """os.walk fallback 路径也跳过 node_modules。"""
        import tools.builtin.system_tool_search as mod
        monkeypatch.setattr(mod, "_RIPGREP_CACHE", "")
        result = await search_files(pattern="hello", path="")
        assert result["success"] is True
        files_found = [r["file"] for r in result["data"]["results"]]
        assert not any("node_modules" in f for f in files_found)


# ═══════════════════════════════════════════════════════════════
# dual-path 一致性（仅 ripgrep 可用时验证两路径行为一致）
# ═══════════════════════════════════════════════════════════════

class TestDualPathConsistency:
    """ripgrep 可用时，验证 ripgrep 路径与 os.walk 路径行为一致。"""

    async def test_ripgrep_and_oswalk_find_same_files(self, sample_files, monkeypatch):
        """两路径应找到相同的文件集（顺序可能不同）。"""
        import tools.builtin.system_tool_search as mod
        from tools.builtin.system_tool_search import _find_ripgrep, _reset_ripgrep_cache
        _reset_ripgrep_cache()
        if not _find_ripgrep():
            pytest.skip("ripgrep 不可用——跳过 dual-path 一致性测试")

        # ripgrep 路径
        result_rg = await search_files(pattern="hello", path="")
        assert result_rg["success"] is True
        files_rg = sorted(r["file"] for r in result_rg["data"]["results"])

        # 强制 os.walk 路径
        monkeypatch.setattr(mod, "_RIPGREP_CACHE", "")
        result_os = await search_files(pattern="hello", path="")
        assert result_os["success"] is True
        files_os = sorted(r["file"] for r in result_os["data"]["results"])

        # 两路径应找到相同文件集
        assert files_rg == files_os, \
            f"ripgrep 与 os.walk 结果不一致: rg={files_rg} os={files_os}"
