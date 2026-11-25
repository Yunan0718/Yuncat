"""tests/unit/test_codegraph_lifecycle.py —— Task 4.2: CodeGraph 单例复用测试

验证 codegraph_context 工具复用 app.state.codegraph 单例，而非每次创建新 client。
同时验证单例不可用/调用失败时正确走 search_files fallback。

对标:
  - Aider repo-map 单例 / Cursor codebase index 复用——启动建索引，运行时复用
  - 原 lifespan 只实例化 app.state.codegraph 不 start()——死对象
  - 原 codegraph_context 每次调用 CodeGraphClient()+start()+stop()——fork 新 Node 子进程
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ═══════════════════════════════════════════════════════════
# 辅助：构造带 codegraph 单例的 app context
# ═══════════════════════════════════════════════════════════

def _set_app_with_codegraph(client):
    """注入一个 mock app，其 state.codegraph = client。"""
    import context
    app = MagicMock()
    app.state.codegraph = client
    context.set_app(app)
    return app


def _clear_app():
    import context
    context._app = None


# ═══════════════════════════════════════════════════════════
# 复用单例——成功路径
# ═══════════════════════════════════════════════════════════

async def test_codegraph_context_reuses_singleton():
    """单例可用时——codegraph_context 应复用 app.state.codegraph，不创建新 client。"""
    mock_client = MagicMock()
    mock_client.context = AsyncMock(return_value=[
        {"name": "auth_login", "kind": "function", "file": "auth.py", "line": 10}
    ])
    _set_app_with_codegraph(mock_client)

    from tools.builtin.system_tool_search import codegraph_context
    result = await codegraph_context(query="find auth functions", max_symbols=10)

    assert result["success"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["symbols"][0]["name"] == "auth_login"
    # 验证复用单例——context 只被调用一次
    mock_client.context.assert_awaited_once_with("find auth functions", 10)


async def test_codegraph_context_singleton_success_returns_correct_structure():
    """单例成功时——返回结构包含 query/symbols/count。"""
    mock_client = MagicMock()
    mock_client.context = AsyncMock(return_value=[
        {"name": "Foo", "kind": "class"},
        {"name": "bar", "kind": "function"},
    ])
    _set_app_with_codegraph(mock_client)

    from tools.builtin.system_tool_search import codegraph_context
    result = await codegraph_context(query="Foo class", max_symbols=20)

    assert result["success"] is True
    assert result["data"]["query"] == "Foo class"
    assert result["data"]["count"] == 2
    assert len(result["data"]["symbols"]) == 2
    assert result["meta"]["tool_name"] == "codegraph_context"
    assert "fallback" not in result["meta"]


# ═══════════════════════════════════════════════════════════
# 复用单例——失败/不可用走 fallback
# ═══════════════════════════════════════════════════════════

async def test_codegraph_context_fallback_when_singleton_none():
    """app.state.codegraph = None（start 失败被置空）——走 search_files fallback。"""
    _set_app_with_codegraph(None)

    from tools.builtin.system_tool_search import codegraph_context
    result = await codegraph_context(query="find function", max_symbols=10)

    assert result["success"] is True
    assert result["meta"]["fallback"] == "search_files"
    # query 含 "function" → 匹配 def 模式
    assert any(p["pattern"] == "def " for p in result["data"]["suggested_searches"])


async def test_codegraph_context_fallback_when_no_app():
    """get_app() 返回 None（lifespan 未运行/测试环境）——走 fallback。"""
    _clear_app()

    from tools.builtin.system_tool_search import codegraph_context
    result = await codegraph_context(query="find function", max_symbols=10)

    assert result["success"] is True
    assert result["meta"]["fallback"] == "search_files"


async def test_codegraph_context_fallback_when_singleton_raises():
    """单例 context() 抛异常（子进程崩溃等）——走 fallback，不传播异常。"""
    mock_client = MagicMock()
    mock_client.context = AsyncMock(side_effect=RuntimeError("subprocess crashed"))
    _set_app_with_codegraph(mock_client)

    from tools.builtin.system_tool_search import codegraph_context
    result = await codegraph_context(query="find function", max_symbols=10)

    assert result["success"] is True
    assert result["meta"]["fallback"] == "search_files"


# ═══════════════════════════════════════════════════════════
# Fallback 模式匹配
# ═══════════════════════════════════════════════════════════

async def test_codegraph_context_fallback_matches_function_keyword():
    """fallback 匹配 function/def/函数/func 关键词 → 返回 def 模式。"""
    _clear_app()
    from tools.builtin.system_tool_search import codegraph_context

    for kw in ("function", "def", "函数", "func"):
        result = await codegraph_context(query=f"find {kw}", max_symbols=10)
        patterns = [p["pattern"] for p in result["data"]["suggested_searches"]]
        assert "def " in patterns, f"关键词 '{kw}' 应匹配 def 模式"


async def test_codegraph_context_fallback_matches_class_keyword():
    """fallback 匹配 class/类/struct 关键词 → 返回 class 模式。"""
    _clear_app()
    from tools.builtin.system_tool_search import codegraph_context

    for kw in ("class", "类", "struct"):
        result = await codegraph_context(query=f"find {kw}", max_symbols=10)
        patterns = [p["pattern"] for p in result["data"]["suggested_searches"]]
        assert "class " in patterns, f"关键词 '{kw}' 应匹配 class 模式"


async def test_codegraph_context_fallback_matches_import_keyword():
    """fallback 匹配 import/导入/require 关键词 → 返回 import 模式。"""
    _clear_app()
    from tools.builtin.system_tool_search import codegraph_context

    for kw in ("import", "导入", "require"):
        result = await codegraph_context(query=f"find {kw}", max_symbols=10)
        patterns = [p["pattern"] for p in result["data"]["suggested_searches"]]
        assert "import " in patterns, f"关键词 '{kw}' 应匹配 import 模式"


async def test_codegraph_context_fallback_extracts_noun_when_no_pattern_match():
    """query 无匹配模式时——提取长度>3 的首词作为 fallback_search 建议。"""
    _clear_app()
    from tools.builtin.system_tool_search import codegraph_context

    result = await codegraph_context(query="authentication middleware", max_symbols=10)
    assert result["success"] is True
    assert result["meta"]["fallback"] == "search_files"
    assert result["data"]["fallback_search"] == "authentication"


async def test_codegraph_context_fallback_no_noun_returns_empty_patterns():
    """query 全是短词（≤3 字符）且无模式匹配——返回空 suggested_searches 列表。"""
    _clear_app()
    from tools.builtin.system_tool_search import codegraph_context

    # 注意：避免使用 "def"/"class"/"import" 等子串（会被模式匹配捕获）
    result = await codegraph_context(query="abc xyz", max_symbols=10)
    assert result["success"] is True
    assert result["meta"]["fallback"] == "search_files"
    # 无匹配模式 + 无长度>3 的词 → suggested_searches 为空
    assert result["data"]["suggested_searches"] == []


# ═══════════════════════════════════════════════════════════
# 不创建新 client——验证死代码已移除
# ═══════════════════════════════════════════════════════════

async def test_codegraph_context_does_not_instantiate_new_client():
    """单例不可用时——不应回退到 CodeGraphClient()+start()+stop()（原死代码路径）。"""
    _clear_app()

    codegraph_client_called = {"instantiated": False, "started": False}

    class _SpyCodeGraphClient:
        def __init__(self, *args, **kwargs):
            codegraph_client_called["instantiated"] = True

        async def start(self):
            codegraph_client_called["started"] = True

        async def stop(self):
            pass

    # 注入 spy 到 services.codegraph_client.CodeGraphClient；测试后必须恢复，
    # 避免污染后续 contract 测试对真实 CodeGraphClient 的引用。
    import sys as _sys
    import types as _types
    original_module = _sys.modules.get("services.codegraph_client")
    try:
        fake_mod = _types.ModuleType("services.codegraph_client")
        fake_mod.CodeGraphClient = _SpyCodeGraphClient
        _sys.modules["services.codegraph_client"] = fake_mod

        from tools.builtin.system_tool_search import codegraph_context
        await codegraph_context(query="find function", max_symbols=10)
    finally:
        if original_module is not None:
            _sys.modules["services.codegraph_client"] = original_module
        else:
            _sys.modules.pop("services.codegraph_client", None)

    # 单例不可用时应走 search_files fallback，不应实例化新 CodeGraphClient
    assert codegraph_client_called["instantiated"] is False, \
        "codegraph_context 不应回退到创建新 CodeGraphClient（应走 search_files fallback）"
    assert codegraph_client_called["started"] is False
