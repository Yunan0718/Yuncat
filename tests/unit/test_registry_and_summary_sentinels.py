"""unit: 注册表原生豁免 + 跨会话摘要哨兵值回归守卫

两条都是「同一数据被多个读取端消费，口径必须一致」类缺陷的回归守卫：
  1. get_active_tools 必须给原生 agent 豁免（2026-08-31 曾因无豁免导致
     43 个内置工具全部 PermissionError 的事故，list_tools / 执行门已修，
     get_active_tools 漏改，一旦启用即复现）。注意过滤 continue 发生在
     CORE_TOOLS 判定之前，若无豁免，get_active_tools("yuncat") 会连 CORE 都丢光。
  2. 跨会话摘要的空节哨兵值 SUMMARY_EMPTY_SENTINEL（"无"）必须在所有读取端
     都被当空跳过。_format_compact_boundary / _summary_size 已跳，
     agent_context._format_inherited_summary 曾漏跳，导致每个新会话注入
     7 行「- 目标: 无」，浪费上下文且误导模型。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


def _reg_tool(name, agents):
    from tools.registry import tool

    @tool(name=name, agents=list(agents), description="d", parameters={})
    async def _fn():
        pass
    return _fn


def test_native_agent_gets_core_tools_with_empty_query():
    """回归守卫：原生 agent 即便 query 为空也必须拿到 CORE_TOOLS。

    无豁免时过滤 continue 发生在 CORE 判定前，会返回 []（即 43 工具事故的变体）。
    """
    from config import YUNCAT_AGENT_ID
    from tools.registry import get_active_tools, _tools

    _tools.clear()
    _reg_tool("read_file", ["hermes", "openclaw"])
    _reg_tool("write_file", ["hermes", "openclaw"])

    native = get_active_tools(YUNCAT_AGENT_ID)
    names = {t["name"] for t in native}
    assert "read_file" in names and "write_file" in names, (
        f"原生 agent 未拿到 CORE 工具: {names}"
    )


def test_native_agent_keeps_extended_tool_via_exemption():
    """原生 agent 能拿到「仅 openclaw 可见」的扩展工具——豁免生效的关键证据。

    用 keyword_map 中真实存在的 search_files + 命中 query，验证豁免放行
    agents=['openclaw'] 的扩展工具；对照 hermes 应拿不到。
    """
    from config import YUNCAT_AGENT_ID
    from tools.registry import get_active_tools, _tools

    _tools.clear()
    _reg_tool("read_file", ["hermes", "openclaw"])  # CORE
    _reg_tool("search_files", ["openclaw"])          # 仅 openclaw 可见的扩展工具

    native = get_active_tools(YUNCAT_AGENT_ID, context={"query": "搜索代码"})
    native_names = {t["name"] for t in native}
    assert "search_files" in native_names, (
        f"原生 agent 的豁免未放行 openclaw 专属扩展工具: {native_names}"
    )

    hermes = get_active_tools("hermes", context={"query": "搜索代码"})
    hermes_names = {t["name"] for t in hermes}
    assert "search_files" not in hermes_names, (
        f"hermes 不应看到 openclaw 专属工具: {hermes_names}"
    )


def test_format_inherited_summary_skips_sentinel():
    """回归守卫：空节哨兵值 "无" 不得被注入 system prompt。"""
    from config import SUMMARY_EMPTY_SENTINEL
    from services.agent.agent_context import _format_inherited_summary

    summary = {
        "session_intent": SUMMARY_EMPTY_SENTINEL,
        "key_decisions": SUMMARY_EMPTY_SENTINEL,
        "files_modified": "src/main.py",
        "pending_tasks": SUMMARY_EMPTY_SENTINEL,
        "errors_and_fixes": SUMMARY_EMPTY_SENTINEL,
        "next_steps": SUMMARY_EMPTY_SENTINEL,
    }
    text = _format_inherited_summary(summary)
    assert SUMMARY_EMPTY_SENTINEL not in text, (
        f"哨兵值被原样注入 system prompt: {text!r}"
    )
    assert "src/main.py" in text and "文件" in text


def test_format_inherited_summary_all_sentinel_returns_empty():
    """全空节时，注入点 return "" 的防线应可达。"""
    from config import SUMMARY_EMPTY_SENTINEL
    from services.agent.agent_context import _format_inherited_summary

    summary = {k: SUMMARY_EMPTY_SENTINEL for k in (
        "session_intent", "key_decisions", "files_modified",
        "pending_tasks", "errors_and_fixes", "next_steps",
    )}
    assert _format_inherited_summary(summary) == ""
