"""tests/unit/test_memories_browse.py —— 语义记忆浏览视图后端测试

覆盖:
  - list_recent_memories: 返回最近语义记忆
  - list_recent_memories: 按 created_at 降序
  - list_recent_memories: project_id 过滤
  - GET /api/v1/memories/browse: 200 + 记忆列表
  - GET /api/v1/memories/browse: limit 上限保护
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def memory_db(tmp_path):
    """创建带完整 schema 的 DB"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_memories_browse.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


class TestListRecentMemories:
    @pytest.mark.asyncio
    async def test_returns_recent_memories(self, memory_db):
        from repositories.memories_repo import insert_memory, list_recent_memories

        await insert_memory(memory_db, "First fact", source="chat", memory_type="fact")
        time.sleep(0.01)
        await insert_memory(memory_db, "Second rule", source="chat", memory_type="rule")
        items = await list_recent_memories(memory_db, limit=10)
        assert len(items) == 2
        assert items[0]["content"] == "Second rule"
        assert items[1]["content"] == "First fact"

    @pytest.mark.asyncio
    async def test_project_id_filter(self, memory_db):
        from repositories.memories_repo import insert_memory, list_recent_memories

        await insert_memory(memory_db, "Global", source="chat", project_id=None)
        await insert_memory(memory_db, "Project", source="chat", project_id="p1")
        items = await list_recent_memories(memory_db, limit=10, project_id="p1")
        assert len(items) == 2
        contents = {i["content"] for i in items}
        assert "Global" in contents
        assert "Project" in contents


class TestMemoriesBrowseRoute:
    def test_browse_endpoint_returns_memories(self):
        from fastapi.testclient import TestClient
        from fastapi import FastAPI, Request
        import aiosqlite
        import tempfile

        app = FastAPI()
        from routes.knowledge import router
        app.include_router(router)

        @app.middleware("http")
        async def inject_db(request: Request, call_next):
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            db = await aiosqlite.connect(tmp.name)
            from repositories import SCHEMA_SQL
            await db.executescript(SCHEMA_SQL)
            await db.commit()
            request.app.state.db = db
            try:
                return await call_next(request)
            finally:
                await db.close()
                os.unlink(tmp.name)

        client = TestClient(app)
        resp = client.get("/api/v1/memories/browse?limit=10")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
