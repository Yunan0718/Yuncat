"""tests/unit/test_audit_log.py —— Round 1 S6 防篡改审计日志测试

覆盖:
- audit_log 表建表迁移
- log_audit() 追加写入（fire-and-forget）
- list_audit_logs() 查询
- 防篡改：无 UPDATE/DELETE 接口
- args_summary 截断
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def db():
    """内存 SQLite + audit_log 表"""
    import aiosqlite
    from repositories.schema import SCHEMA_SQL
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


class TestAuditLogTable:
    """S6: audit_log 表结构"""

    async def test_table_exists_after_schema(self, db):
        """SCHEMA_SQL 包含 audit_log 建表"""
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "audit_log"

    async def test_indexes_exist(self, db):
        """审计表索引存在"""
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_audit%'"
        )
        rows = await cursor.fetchall()
        names = {r[0] for r in rows}
        assert "idx_audit_ts" in names
        assert "idx_audit_tool" in names


class TestLogAudit:
    """S6: log_audit() 写入"""

    async def test_basic_insert(self, db):
        """单条审计日志写入"""
        from repositories.audit_repo import log_audit
        await log_audit(
            db, agent_id="hermes", tool_name="write_file",
            arguments={"path": "test.txt", "content": "hello"},
            success=True, phase="execute",
            session_id="sess-1", task_id="task-1",
        )
        cursor = await db.execute("SELECT agent_id, tool_name, success, phase FROM audit_log")
        row = await cursor.fetchone()
        assert row[0] == "hermes"
        assert row[1] == "write_file"
        assert row[2] == 1  # success=True → 1
        assert row[3] == "execute"

    async def test_preview_phase_null_success(self, db):
        """preview 阶段 success=None"""
        from repositories.audit_repo import log_audit
        await log_audit(
            db, agent_id="openclaw", tool_name="run_command",
            arguments={"command": "dir"},
            success=None, phase="preview",
        )
        cursor = await db.execute("SELECT success FROM audit_log")
        row = await cursor.fetchone()
        assert row[0] is None  # None → NULL

    async def test_args_summary_truncated(self, db):
        """超长 args_summary 被截断——值级截断（>80 字符的值截断为 80+...）"""
        from repositories.audit_repo import log_audit
        long_args = {"data": "x" * 1000}
        await log_audit(
            db, agent_id="hermes", tool_name="write_file",
            arguments=long_args, success=True, phase="execute",
        )
        cursor = await db.execute("SELECT args_summary FROM audit_log")
        row = await cursor.fetchone()
        # 值级截断：1000 字符的 x 被截断为 80 字符 + "..."
        # 整体 str(summary) 远小于 500，不会触发全局截断
        assert "..." in row[0]
        assert len(row[0]) < 200  # 截断后远小于原始 1000+

    async def test_args_summary_global_truncation(self, db):
        """超多参数触发全局截断（>500 字符）"""
        from repositories.audit_repo import log_audit
        # 10 个长值，每个 80+3=83 字符，整体 > 500
        big_args = {f"key_{i}": "v" * 200 for i in range(10)}
        await log_audit(
            db, agent_id="hermes", tool_name="write_file",
            arguments=big_args, success=True, phase="execute",
        )
        cursor = await db.execute("SELECT args_summary FROM audit_log")
        row = await cursor.fetchone()
        # 全局截断到 500 + "..."
        assert len(row[0]) <= 505
        assert row[0].endswith("...")

    async def test_failed_write_does_not_raise(self, db):
        """审计日志写入失败不抛异常（fire-and-forget）"""
        from repositories.audit_repo import log_audit
        # 用一个已关闭的连接触发异常
        await db.close()
        # 应该静默失败，不抛
        await log_audit(
            db, agent_id="hermes", tool_name="write_file",
            arguments={}, success=True, phase="execute",
        )


class TestListAuditLogs:
    """S6: list_audit_logs() 查询"""

    async def test_list_returns_newest_first(self, db):
        """查询结果按 ts DESC 排序"""
        from repositories.audit_repo import log_audit, list_audit_logs
        await log_audit(db, "hermes", "write_file", {"p": "a"}, True, "execute")
        await log_audit(db, "hermes", "run_command", {"c": "dir"}, True, "execute")
        logs = await list_audit_logs(db, limit=10)
        assert len(logs) == 2
        # 最新优先——第二条应在前面
        assert logs[0]["tool_name"] == "run_command"
        assert logs[1]["tool_name"] == "write_file"

    async def test_filter_by_tool_name(self, db):
        """按 tool_name 过滤"""
        from repositories.audit_repo import log_audit, list_audit_logs
        await log_audit(db, "hermes", "write_file", {}, True, "execute")
        await log_audit(db, "hermes", "run_command", {}, True, "execute")
        await log_audit(db, "hermes", "write_file", {}, False, "execute")
        logs = await list_audit_logs(db, tool_name="write_file")
        assert len(logs) == 2
        assert all(l["tool_name"] == "write_file" for l in logs)

    async def test_limit_applied(self, db):
        """limit 参数生效"""
        from repositories.audit_repo import log_audit, list_audit_logs
        for i in range(5):
            await log_audit(db, "hermes", "write_file", {"i": i}, True, "execute")
        logs = await list_audit_logs(db, limit=3)
        assert len(logs) == 3


class TestTamperEvidence:
    """S6: 防篡改——无 UPDATE/DELETE 接口"""

    async def test_no_update_function_in_repo(self):
        """audit_repo 不暴露 UPDATE 接口"""
        import repositories.audit_repo as repo
        public_funcs = [name for name in dir(repo) if not name.startswith("_") and callable(getattr(repo, name))]
        # 不应有 update / delete 函数
        assert not any("update" in f.lower() or "delete" in f.lower() for f in public_funcs), \
            f"audit_repo 不应暴露 UPDATE/DELETE 接口: {public_funcs}"

    async def test_direct_sql_update_still_works_but_not_exposed(self, db):
        """SQL 层面可 UPDATE（SQLite 限制），但 repo 不暴露——防误用"""
        from repositories.audit_repo import log_audit
        await log_audit(db, "hermes", "write_file", {}, True, "execute")
        # 直接 SQL 可以改（SQLite 不防），但这不是 repo 接口
        await db.execute("UPDATE audit_log SET success=0")
        await db.commit()
        cursor = await db.execute("SELECT success FROM audit_log")
        row = await cursor.fetchone()
        assert row[0] == 0  # SQL 层可改，但 repo 不提供此能力
