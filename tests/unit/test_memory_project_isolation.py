"""tests/unit/test_memory_project_isolation.py —— Batch 2.1: 向量层 project_id 写入验证

覆盖:
- MemoryService.store_memory 将 project_id 写入 SQLite memories 表。
- _index_to_vector 将 project_id 写入 chromadb metadata。
- 空 project_id 保持 global sentinel（不污染 metadata）。
- memory_tool.store_memory 自动使用当前 workspace 作为 project_id。
"""
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
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
async def db():
    """带 schema 的临时 SQLite 数据库。"""
    from repositories.schema import SCHEMA_SQL
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = await aiosqlite.connect(path)
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()
    _safe_unlink(path)


class _FakeVectorRepo:
    """记录 index_memory / index_knowledge 调用参数，模拟 flush 成功。"""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.flush = AsyncMock(return_value=True)

    async def index_memory(self, mem_id: str, content: str, metadata: dict | None = None) -> bool:
        self.calls.append((mem_id, content, metadata))
        return True

    async def index_knowledge(
        self, node_id: str, content: str, metadata: dict | None = None,
    ) -> bool:
        self.calls.append((node_id, content, metadata))
        return True


@pytest.fixture
async def memory_service(db):
    """注入 mock vector_repo 的 MemoryService。"""
    from services.memory.memory_service import MemoryService
    svc = MemoryService(db)
    svc._vector_repo = _FakeVectorRepo()
    return svc


class TestStoreMemoryProjectId:
    """Batch 2.1: store_memory 透传 project_id 到 DB 和向量层。"""

    async def test_store_memory_writes_project_id_to_db(self, memory_service):
        """project_id 应写入 memories 表的 project_id 列。"""
        svc = memory_service
        pid = os.path.normcase(r"D:\Projects\myapp")
        mem_id = await svc.store_memory(
            "Python 3.12 支持 PEP 695",
            source="test", owner="user1",
            project_id=pid,
        )
        row = await svc.db.execute(
            "SELECT project_id FROM memories WHERE id=?", (mem_id,)
        )
        fetched = await row.fetchone()
        assert fetched[0] == pid

    async def test_store_memory_writes_project_id_to_vector_metadata(self, memory_service):
        """project_id 应出现在传给 vector_repo.index_memory 的 metadata 中。"""
        svc = memory_service
        pid = os.path.normcase(r"D:\Projects\myapp")
        await svc.store_memory(
            "asyncio.to_thread 避免阻塞事件循环",
            source="test", owner="user1",
            project_id=pid,
        )
        repo = svc._vector_repo
        assert len(repo.calls) == 1
        _mem_id, _content, metadata = repo.calls[0]
        assert metadata is not None
        assert metadata.get("project_id") == pid

    async def test_store_memory_without_project_id_is_global(self, memory_service):
        """未提供 project_id 时，DB 为 NULL，metadata 中写入 global sentinel ""。"""
        svc = memory_service
        await svc.store_memory(
            "global memory without project",
            source="test", owner="user1",
        )
        row = await svc.db.execute(
            "SELECT project_id FROM memories WHERE content=?",
            ("global memory without project",),
        )
        fetched = await row.fetchone()
        assert fetched[0] is None
        repo = svc._vector_repo
        assert len(repo.calls) == 1
        metadata = repo.calls[0][2] or {}
        assert metadata.get("project_id") == ""

    async def test_empty_project_id_is_treated_as_global(self, memory_service):
        """空字符串 project_id 应视为 global，metadata 中写入 global sentinel ""。"""
        svc = memory_service
        await svc.store_memory(
            "empty project id memory",
            source="test", project_id="",
        )
        repo = svc._vector_repo
        assert len(repo.calls) == 1
        metadata = repo.calls[0][2] or {}
        assert metadata.get("project_id") == ""


class TestMemoryToolProjectId:
    """memory_tool.store_memory 自动使用 workspace 路径作为 project_id。"""

    async def test_store_memory_tool_uses_workspace_project_id(self, db, monkeypatch, tmp_path):
        """工具写入记忆时，project_id 应为规范化的 workspace 路径。"""
        from tools.builtin import memory_tool
        from services.memory.memory_tree import MemoryTree

        # 隔离 workspace 到 tmp_path，避免污染用户目录
        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path))

        tree = MemoryTree(db)
        tree._vector_repo = _FakeVectorRepo()

        # 构造最小 app.state 供 memory_tool 读取
        class _State:
            memory_tree = tree
        class _App:
            state = _State()

        from context import set_app, get_app
        app_instance = _App()
        set_app(app_instance)
        assert get_app() is app_instance

        result = await memory_tool.store_memory("项目相关的重要决策", source="task_lesson")
        assert result["success"] is True

        expected_pid = os.path.normcase(str(tmp_path))
        repo = tree._vector_repo
        assert len(repo.calls) == 1
        metadata = repo.calls[0][2] or {}
        assert metadata.get("project_id") == expected_pid

        # 清理全局 app
        set_app(None)


class TestVectorRepoMetadataSchema:
    """验证 VectorRepository 对 project_id 的 schema 处理。"""

    async def test_vector_doc_converts_none_project_id_to_empty(self):
        """sqlite-vec 架构：MemoryVectorDoc project_id=None 表示 global memory
        （SQLite NULL），不再转 "" sentinel（chromadb 遗留语义已废弃）。"""
        from repositories.vector_repo import MemoryVectorDoc
        doc = MemoryVectorDoc(id="x", content="c", project_id=None)
        assert doc.model_dump(exclude={"id", "content"})["project_id"] is None

    async def test_vector_doc_preserves_project_id(self):
        """MemoryVectorDoc 序列化保留非空 project_id。"""
        from repositories.vector_repo import MemoryVectorDoc
        pid = os.path.normcase(r"D:\Projects\myapp")
        doc = MemoryVectorDoc(id="x", content="c", project_id=pid)
        assert doc.model_dump(exclude={"id", "content"})["project_id"] == pid
