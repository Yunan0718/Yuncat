"""tests/unit/test_conversations_callers.py —— D2: insert_conversation 调用方 commit 失败保护

覆盖:
- memory_compact._persist_compaction_snapshot: 文件快照不再污染 conversations
- agent_service._build_and_inject_recovery: commit 失败时 rollback，不污染连接
- 验证 commit 失败后连接仍可正常工作
"""
import os
import sys
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
async def db_conn(tmp_path):
    """创建带 schema 的 aiosqlite 连接"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_d2.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()
    _safe_unlink(db_path)


class TestPersistCompactionSnapshotCommitFailure:
    """memory_compact._persist_compaction_snapshot 持久化边界"""

    async def test_snapshot_does_not_insert_conversation_row(self, db_conn):
        """压缩真源是 checkpoint 表，文件 dump 不应再插入 system 对话。"""
        from services.memory.memory_service import MemoryService
        from repositories.memory_repo import MemoryRepository

        svc = MemoryService(db_conn, memory_repo=MemoryRepository(db_conn))
        older = [{"id": "msg-1", "role": "user", "content": "old msg"}]
        await svc._persist_compaction_snapshot("sess-snap", older)
        cursor = await db_conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE session_id='sess-snap'",
        )
        assert (await cursor.fetchone())[0] == 0

    async def test_connection_still_works_after_snapshot_commit_failure(self, db_conn):
        """commit 失败 + rollback 后，连接仍能正常写入"""
        from services.memory.memory_service import MemoryService
        from repositories.memory_repo import MemoryRepository

        svc = MemoryService(db_conn, memory_repo=MemoryRepository(db_conn))

        fail_once = [True]
        original_commit = db_conn.commit
        original_rollback = db_conn.rollback

        async def flaky_commit():
            if fail_once[0]:
                fail_once[0] = False
                raise RuntimeError("transient failure")
            await original_commit()

        async def real_rollback():
            await original_rollback()

        db_conn.commit = flaky_commit
        db_conn.rollback = real_rollback

        older = [{"id": "msg-1", "role": "user", "content": "old msg"}]
        await svc._persist_compaction_snapshot("sess-recover-snap", older)

        # 恢复正常 commit
        db_conn.commit = original_commit
        db_conn.rollback = original_rollback

        # 连接应仍能正常工作——写入一条消息
        await svc.add_message("sess-recover-snap", "user", "after recovery")
        await svc.flush_pending()

        # 验证写入成功
        ctx = await svc.get_session_context("sess-recover-snap")
        contents = [m["content"] for m in ctx]
        assert "after recovery" in contents, "rollback 后连接应仍能正常写入"


class TestBuildAndInjectRecoveryCommitFailure:
    """agent_service._build_and_inject_recovery commit 失败保护"""

    async def test_commit_failure_triggers_rollback(self, db_conn, monkeypatch):
        """commit 失败时必须 rollback，避免后续 _reinject_pending_tokens 等操作失败"""
        from services.agent.agent_service import AgentService

        # 构造最小 AgentService——只需要 db 和必要属性
        class _MinimalAgentService:
            pass

        svc = _MinimalAgentService()
        svc.db = db_conn

        # 绑定真实方法
        svc._build_and_inject_recovery = AgentService._build_and_inject_recovery.__get__(svc)

        rollback_called = [0]
        original_rollback = db_conn.rollback

        async def failing_commit():
            raise RuntimeError("recovery commit failure")

        async def spy_rollback():
            rollback_called[0] += 1

        db_conn.commit = failing_commit
        db_conn.rollback = spy_rollback

        state = {
            "tool_round": 3,
            "plan_step": 2,
            "plan_total": 5,
            "last_tool": "read_file",
            "confirmed_goal": "test goal",
        }

        # 应抛异常（recovery 是关键路径，不应吞错），但 rollback 必须被调用
        with pytest.raises(RuntimeError, match="recovery commit failure"):
            await svc._build_and_inject_recovery(
                "task-1", "sess-recover", "hermes", "test input", state,
            )

        assert rollback_called[0] >= 1, "commit 失败后必须调用 rollback"

        # 恢复
        db_conn.commit = original_rollback
        db_conn.rollback = original_rollback
