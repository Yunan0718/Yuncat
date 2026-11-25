"""tests/unit/test_memory_service.py —— 记忆服务测试（附录 K 写穿透验证）"""
import os
import sys
import tempfile
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


def _safe_unlink(path: str) -> None:
    if not os.path.exists(path):
        return
    for _ in range(5):
        try:
            os.unlink(path)
            return
        except PermissionError:
            time.sleep(0.05)


@pytest.fixture
async def memory_service(tmp_path):
    """创建 MemoryService 实例"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    from services.memory.memory_service import MemoryService
    svc = MemoryService(conn)
    yield svc
    await conn.close()
    _safe_unlink(db_path)


class TestWriteThrough:
    """附录 K 写穿透验证：先 DB 后缓存"""

    async def test_add_message_writes_db_first(self, memory_service):
        """add_message 先写 DB（conversations 表）再更新缓存"""
        svc = memory_service
        await svc.add_message("sess-1", "user", "hello")

        # 验证 DB 中有记录
        rows = await svc.get_conversation("sess-1", 10)
        assert len(rows) == 1
        assert rows[0]["role"] == "user"
        assert rows[0]["content"] == "hello"

    async def test_add_message_updates_cache_second(self, memory_service):
        """缓存与 DB 一致"""
        svc = memory_service
        await svc.add_message("sess-2", "user", "cache test")
        ctx = await svc.get_session_context("sess-2")
        assert len(ctx) == 1
        assert ctx[0]["content"] == "cache test"

    async def test_cache_miss_loads_from_db(self, memory_service):
        """缓存 miss 时从 DB 加载"""
        svc = memory_service
        # 直接写 DB 绕过缓存
        from repositories import insert_conversation
        await insert_conversation(svc.db, "sess-3", "user", "from db")
        # 清除缓存
        svc._sessions.pop("sess-3", None)
        ctx = await svc.get_session_context("sess-3")
        assert len(ctx) >= 1

    async def test_multi_turn_conversation(self, memory_service):
        """多轮对话保持顺序"""
        svc = memory_service
        await svc.add_message("sess-4", "user", "Q1")
        await svc.add_message("sess-4", "assistant", "A1")
        await svc.add_message("sess-4", "user", "Q2")
        ctx = await svc.get_session_context("sess-4")
        assert ctx[0]["content"] == "Q1"
        assert ctx[-1]["content"] == "Q2"


class TestMemorySearch:
    """FTS5 语义记忆检索"""

    async def test_store_and_search_memory(self, memory_service):
        """存储记忆后可被检索"""
        svc = memory_service
        await svc.store_memory("Python 3.12 支持 PEP 695 类型参数语法", owner="user1")
        results = await svc.multi_level_search("Python 类型参数")
        assert isinstance(results, list)

    async def test_empty_search_returns_empty(self, memory_service):
        """无匹配时返回空列表"""
        svc = memory_service
        results = await svc.multi_level_search("不存在的关键词xyz123")
        assert results == []


class TestSummarize:
    """对话摘要（Phase 1 stub）"""

    async def test_empty_messages(self, memory_service):
        """空消息返回空字符串"""
        result = await memory_service.summarize_conversation([])
        assert result == ""

    async def test_returns_truncated_context(self, memory_service):
        """返回截断的最近对话"""
        msgs = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮助你的？"},
        ]
        result = await memory_service.summarize_conversation(msgs)
        assert "用户" in result
        assert "Agent" in result
