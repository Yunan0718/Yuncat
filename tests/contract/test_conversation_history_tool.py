"""contract: 跨会话历史检索 —— repo 单源 + agent 工具暴露（4.3 补充）

背景：Hermes 的「FTS5 session search with LLM summarization for cross-session
recall」在 Yuncat 里，跨会话注入（session_summaries + _inject_inherited_summary）
早已闭环，但**检索**只存在于 HTTP 端点 /api/v1/conversations/search，
agent 无法自主翻找更早的历史（只能被动继承上一个会话摘要）。

本文件锁住：
- conversations_repo.search_conversations 为 FTS5 + LIKE 回退的**单一来源**
- HTTP 端点与 agent 工具共用该实现（防两端逻辑漂移）
- agent 工具 search_conversation_history 已注册且可由原生 agent 调用
"""
import os
import sys

import aiosqlite
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from repositories.schema import SCHEMA_SQL  # noqa: E402


class _FakeState:
    def __init__(self, db):
        self.db = db


class _FakeApp:
    """最小 FastAPI 桩——只提供 tools 需要的 app.state.db。"""

    def __init__(self, db):
        self.state = _FakeState(db)


async def _new_db():
    """创建带完整 schema 的内存库（含 conversations + conversations_fts）。"""
    db = await aiosqlite.connect(":memory:")
    await db.executescript(SCHEMA_SQL)
    await db.commit()
    return db


async def _seed(db, rows):
    """写入对话记录。rows: [(session_id, role, content), ...]"""
    from repositories.conversations_repo import insert_conversation

    for session_id, role, content in rows:
        await insert_conversation(db, session_id, role, content)
    await db.commit()


# ---- repo 层：单一来源 ----

@pytest.mark.asyncio
async def test_repo_search_finds_inserted_conversation():
    """写入的对话应能被检索到（FTS5 触发器自动填充索引）。"""
    from repositories.conversations_repo import search_conversations

    db = await _new_db()
    await _seed(db, [
        ("sess-old", "user", "讨论过数据库连接池的配置上限"),
        ("sess-new", "assistant", "已把连接池上限调整为 50"),
        ("sess-other", "user", "今天的天气不错"),
    ])
    hits = await search_conversations(db, "连接池")
    assert hits, "FTS5 应命中含关键词的对话"
    assert any(h["session_id"] in ("sess-old", "sess-new") for h in hits)
    assert all({"session_id", "preview", "role", "created_at"} <= set(h) for h in hits)
    await db.close()


@pytest.mark.asyncio
async def test_repo_search_empty_query_returns_empty():
    """空查询直接返回空，不发起 SQL。"""
    from repositories.conversations_repo import search_conversations

    db = await _new_db()
    await _seed(db, [("s", "user", "任意内容")])
    assert await search_conversations(db, "") == []
    assert await search_conversations(db, "   ") == []
    await db.close()


@pytest.mark.asyncio
async def test_repo_search_like_fallback_when_fts_unavailable():
    """FTS5 虚表缺失（旧库未跑到迁移 #32）时必须回退 LIKE，召回不退化。"""
    from repositories.conversations_repo import search_conversations

    db = await _new_db()
    await _seed(db, [("sess-a", "user", "这是关于灰度发布的讨论")])
    # 模拟旧库：无 conversations_fts 虚表
    await db.execute("DROP TABLE conversations_fts")
    await db.commit()

    hits = await search_conversations(db, "灰度发布")
    assert hits, "FTS 不可用时必须回退 LIKE 命中"
    assert hits[0]["session_id"] == "sess-a"
    await db.close()


@pytest.mark.asyncio
async def test_repo_search_respects_limit():
    """limit 生效。"""
    from repositories.conversations_repo import search_conversations

    db = await _new_db()
    await _seed(db, [(f"s{i}", "user", f"重复出现的关键词 第{i}条") for i in range(6)])
    hits = await search_conversations(db, "关键词", limit=2)
    assert len(hits) <= 2
    await db.close()


# ---- 单源：HTTP 端点不再自带查询逻辑 ----

def test_http_route_delegates_to_repo():
    """main.py 路由必须复用 repo 实现，不得保留第二份 FTS/LIKE 逻辑。"""
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "main.py"),
        encoding="utf-8",
    ).read()
    assert "conversations_fts MATCH" not in src, \
        "main.py 仍内联 FTS 查询——应改为复用 conversations_repo.search_conversations"
    assert "_LIKE_SQL" not in src, "main.py 仍内联 LIKE 回退"
    assert "search_conversations as _search" in src


# ---- agent 工具 ----

def _load_all_builtin_tools() -> None:
    import importlib
    import pkgutil

    import tools.builtin as _b
    for m in pkgutil.iter_modules(_b.__path__):
        try:
            importlib.import_module("tools.builtin." + m.name)
        except Exception:
            pass


def test_tool_registered_read_only_in_memory_group():
    """工具必须注册，且只读、归属 memory 组。"""
    _load_all_builtin_tools()
    from tools.registry import get_all_tools

    t = get_all_tools().get("search_conversation_history")
    assert t is not None, "search_conversation_history 未注册——agent 无法自主检索历史"
    assert t.is_read_only is True
    assert "memory" in (t.groups or [])
    assert "query" in t.parameters.get("required", [])


@pytest.mark.asyncio
async def test_tool_searches_through_app_db():
    """端到端：工具经 app.state.db 检索并返回命中。"""
    _load_all_builtin_tools()
    from context import set_app
    from tools.builtin.memory_tool import search_conversation_history

    db = await _new_db()
    await _seed(db, [("sess-x", "user", "上次我们定的重构方案是拆分为三个文件")])
    set_app(_FakeApp(db))
    try:
        result = await search_conversation_history("重构方案")
    finally:
        set_app(None)
        await db.close()

    assert result.get("success") is True, result.get("error")
    assert result["data"]["count"] >= 1
    assert result["data"]["results"][0]["session_id"] == "sess-x"


@pytest.mark.asyncio
async def test_tool_reports_error_without_app():
    """无 app（DB 不可用）时必须诚实报错，不得伪装成功。"""
    _load_all_builtin_tools()
    from context import set_app
    from tools.builtin.memory_tool import search_conversation_history

    set_app(None)
    result = await search_conversation_history("任意")
    assert result.get("success") is False
    assert result.get("error")


@pytest.mark.asyncio
async def test_native_agent_can_execute_history_search():
    """原生 agent 必须能实际执行该工具（drift 回归守卫）。

    2026-08-31 前：原生 agent 因 tool.agents 校验被拒，所有内置工具全灭。
    """
    _load_all_builtin_tools()
    from context import set_app
    from config import YUNCAT_AGENT_ID
    from services.tools.tool_service import ToolService

    db = await _new_db()
    await _seed(db, [("sess-y", "user", "关于缓存失效策略的结论")])
    set_app(_FakeApp(db))
    try:
        ts = ToolService()
        result = await ts.execute("search_conversation_history",
                                  {"query": "缓存失效"}, agent_id=YUNCAT_AGENT_ID)
    finally:
        set_app(None)
        await db.close()
    assert result.get("success") is True, result.get("error")
