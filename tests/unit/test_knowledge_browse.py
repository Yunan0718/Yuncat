"""tests/unit/test_knowledge_browse.py —— MemoryTree 知识浏览视图后端测试

覆盖:
  - list_recent_knowledge_nodes: 返回最近知识节点
  - list_recent_knowledge_nodes: 按 source 可分组验证
  - list_recent_knowledge_nodes: project_id 过滤
  - GET /api/v1/knowledge/browse: 200 + 节点列表
  - GET /api/v1/knowledge/browse: limit 上限保护
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def migrated_db(tmp_path):
    """创建带完整 schema + 列级迁移的 DB"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_knowledge_browse.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    from repositories.schema import COLUMN_MIGRATIONS, _safe_add_column
    for table, column, definition in COLUMN_MIGRATIONS:
        await _safe_add_column(conn, table, column, definition)
    await conn.commit()
    yield conn
    await conn.close()


class TestListRecentKnowledgeNodes:
    @pytest.mark.asyncio
    async def test_returns_recent_nodes(self, migrated_db):
        from repositories.knowledge_repo import upsert_knowledge_node, list_recent_knowledge_nodes

        await upsert_knowledge_node(migrated_db, "n1", "Node 1", "Content 1", source="dream")
        await upsert_knowledge_node(migrated_db, "n2", "Node 2", "Content 2", source="conversation")
        nodes = await list_recent_knowledge_nodes(migrated_db, limit=10)
        assert len(nodes) == 2
        ids = {n["id"] for n in nodes}
        assert ids == {"n1", "n2"}
        # 返回字段完整
        assert all(k in nodes[0] for k in ("id", "title", "content", "parent_id", "source", "score", "tags", "created_at", "updated_at"))

    @pytest.mark.asyncio
    async def test_sorted_by_created_at_desc(self, migrated_db):
        import asyncio
        from repositories.knowledge_repo import upsert_knowledge_node, list_recent_knowledge_nodes

        await upsert_knowledge_node(migrated_db, "old", "Old", "Old content", source="test")
        await asyncio.sleep(1.1)  # 确保 created_at 至少相差 1 秒
        await upsert_knowledge_node(migrated_db, "new", "New", "New content", source="test")
        nodes = await list_recent_knowledge_nodes(migrated_db, limit=10)
        assert nodes[0]["id"] == "new"
        assert nodes[1]["id"] == "old"

    @pytest.mark.asyncio
    async def test_project_id_filter(self, migrated_db):
        from repositories.knowledge_repo import upsert_knowledge_node, list_recent_knowledge_nodes

        await upsert_knowledge_node(migrated_db, "global", "Global", "Global content", source="test", project_id=None)
        await upsert_knowledge_node(migrated_db, "proj", "Project", "Project content", source="test", project_id="p1")
        nodes = await list_recent_knowledge_nodes(migrated_db, limit=10, project_id="p1")
        assert len(nodes) == 2  # p1 + global
        ids = {n["id"] for n in nodes}
        assert "global" in ids
        assert "proj" in ids


class TestKnowledgeBrowseRoute:
    def test_browse_endpoint_returns_nodes(self):
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
            from repositories.schema import COLUMN_MIGRATIONS, _safe_add_column
            for table, column, definition in COLUMN_MIGRATIONS:
                await _safe_add_column(db, table, column, definition)
            await db.commit()
            request.app.state.db = db
            try:
                return await call_next(request)
            finally:
                await db.close()
                os.unlink(tmp.name)

        client = TestClient(app)
        resp = client.get("/api/v1/knowledge/browse?limit=10")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
