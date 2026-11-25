"""tests/unit/test_repositories.py —— 数据库层单元测试"""
import os
import sys
import tempfile
import time
import pytest
import asyncio
import aiosqlite

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


def _safe_unlink(path: str) -> None:
    """Windows 安全删除——重试 5 次，每次等待 50ms"""
    if not os.path.exists(path):
        return
    for _ in range(5):
        try:
            os.unlink(path)
            return
        except PermissionError:
            time.sleep(0.05)
    # 最后一次尝试
    try:
        os.unlink(path)
    except PermissionError:
        pass  # Windows 偶尔延迟释放，测试结束后由 Temp 目录清理


@pytest.fixture
async def test_db():
    """创建临时数据库，测试结束后清理"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    _safe_unlink(path)
    for suffix in ("-wal", "-shm"):
        _safe_unlink(path + suffix)


class TestInitDb:
    """测试 init_db() 数据库连接初始化"""

    async def test_init_db_returns_connection(self, test_db):
        """init_db 返回 aiosqlite.Connection"""
        from repositories import init_db
        conn = await init_db(test_db)
        assert isinstance(conn, aiosqlite.Connection)
        await conn.close()

    async def test_init_db_sets_wal_mode(self, test_db):
        """init_db 设置 WAL 日志模式"""
        from repositories import init_db
        conn = await init_db(test_db)
        cursor = await conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        assert row[0].upper() == "WAL"
        await conn.close()

    async def test_init_db_sets_busy_timeout(self, test_db):
        """init_db 设置 busy_timeout >= 30000ms"""
        from repositories import init_db
        conn = await init_db(test_db)
        cursor = await conn.execute("PRAGMA busy_timeout")
        row = await cursor.fetchone()
        assert row[0] >= 30000
        await conn.close()

    async def test_init_db_sets_foreign_keys(self, test_db):
        """init_db 启用外键约束"""
        from repositories import init_db
        conn = await init_db(test_db)
        cursor = await conn.execute("PRAGMA foreign_keys")
        row = await cursor.fetchone()
        assert row[0] == 1
        await conn.close()


class TestRunMigrations:
    """测试 run_migrations() 迁移执行"""

    async def test_run_migrations_creates_tables(self, test_db):
        """迁移后所有核心表存在"""
        from repositories import init_db, run_migrations
        conn = await init_db(test_db)
        await run_migrations(conn)

        expected_tables = [
            "agents", "agent_heartbeats", "tasks", "task_runs",
            "task_logs", "workflows", "workflow_runs", "memories",
            "memory_tags", "conversations", "settings",
            "analytics_events", "rules", "runtime_states",
            "webhook_nonces", "migrations",
        ]
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]
        for expected in expected_tables:
            assert expected in tables, f"Table {expected} not found"
        await conn.close()

    async def test_run_migrations_records_versions(self, test_db):
        """迁移记录写入 migrations 表"""
        from repositories import init_db, run_migrations
        conn = await init_db(test_db)
        await run_migrations(conn)

        cursor = await conn.execute(
            "SELECT COUNT(*) FROM migrations"
        )
        row = await cursor.fetchone()
        assert row[0] > 0, "Expected at least 1 migration record"
        await conn.close()

    async def test_run_migrations_idempotent(self, test_db):
        """重复执行迁移不报错"""
        from repositories import init_db, run_migrations
        conn = await init_db(test_db)
        await run_migrations(conn)
        # 第二次执行
        await run_migrations(conn)
        # 不抛异常即为通过
        await conn.close()

    async def test_agents_table_empty_after_init(self, test_db):
        """新库迁移后 SELECT COUNT(*) FROM agents 返回 0（验收标准）"""
        from repositories import init_db, run_migrations
        conn = await init_db(test_db)
        await run_migrations(conn)

        cursor = await conn.execute("SELECT COUNT(*) FROM agents")
        row = await cursor.fetchone()
        assert row[0] == 0
        await conn.close()


class TestSchemaSql:
    """测试 SCHEMA_SQL 常量"""

    def test_schema_sql_is_nonempty_string(self):
        """SCHEMA_SQL 是非空字符串"""
        from repositories import SCHEMA_SQL
        assert isinstance(SCHEMA_SQL, str)
        assert len(SCHEMA_SQL) > 100

    def test_schema_sql_contains_agents_table(self):
        """SCHEMA_SQL 包含 agents 表"""
        from repositories import SCHEMA_SQL
        assert "CREATE TABLE IF NOT EXISTS agents" in SCHEMA_SQL


class TestSafeAddColumn:
    """测试 _safe_add_column 辅助函数"""

    async def test_safe_add_column_new(self, test_db):
        """添加新列成功"""
        from repositories import init_db, _safe_add_column
        conn = await init_db(test_db)
        await conn.execute("CREATE TABLE foo (id INTEGER)")
        await _safe_add_column(conn, "foo", "bar", "TEXT DEFAULT 'hello'")
        cursor = await conn.execute("PRAGMA table_info(foo)")
        cols = [row[1] for row in await cursor.fetchall()]
        assert "bar" in cols
        await conn.close()

    async def test_safe_add_column_idempotent(self, test_db):
        """重复添加同一列不报错"""
        from repositories import init_db, _safe_add_column
        conn = await init_db(test_db)
        await conn.execute("CREATE TABLE foo (id INTEGER)")
        await _safe_add_column(conn, "foo", "bar", "TEXT DEFAULT 'hello'")
        await _safe_add_column(conn, "foo", "bar", "TEXT DEFAULT 'hello'")
        # 不抛异常即为通过
        await conn.close()
