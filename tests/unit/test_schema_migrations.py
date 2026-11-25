"""tests/unit/test_schema_migrations.py —— Task 7.13 迁移事务包装测试

覆盖 F-128：多语句迁移中间失败时前面的语句回滚，避免半迁移状态。
对标 Django/Prisma 自动事务迁移——每个迁移是一个原子事务。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


async def _async_noop(*args, **kwargs):
    """异步 no-op，用于 mock _seed_roles / _check_schema_drift。"""
    pass


def _setup_migration_test(monkeypatch, migrations):
    """配置 schema 模块使用测试专用迁移列表，隔离 _seed_roles / _check_schema_drift。"""
    import repositories.schema as schema_mod
    import repositories.schema_checks as schema_checks_mod

    monkeypatch.setattr(schema_mod, "MIGRATIONS", migrations)
    monkeypatch.setattr(schema_mod, "COLUMN_MIGRATIONS", [])
    monkeypatch.setattr(schema_mod, "_seed_roles", _async_noop)
    monkeypatch.setattr(schema_checks_mod, "_check_schema_drift", _async_noop)


class TestMigrationTransactionWrapping:
    """Task 7.13: 迁移事务包装——多语句迁移原子性"""

    async def test_migration_atomic_rollback_on_failure(self, monkeypatch, tmp_path):
        """迁移 sql 中间失败时，前面的语句回滚（F-128 核心修复）

        修复前：executescript 不回滚，test_t1/test_t2 保留（半迁移状态）
        修复后：BEGIN...COMMIT 包裹，整体回滚，test_t1/test_t2 不存在
        """
        import aiosqlite
        import repositories.schema as schema_mod

        # 3 条语句，第 3 条重复建表失败
        failing_sql = """
            CREATE TABLE test_t1 (id INTEGER);
            CREATE TABLE test_t2 (id INTEGER);
            CREATE TABLE test_t1 (id INTEGER);
        """
        _setup_migration_test(monkeypatch, [
            (100, "test failing migration", failing_sql),
        ])

        db_path = str(tmp_path / "test.db")
        conn = await aiosqlite.connect(db_path)
        try:
            # 迁移失败（第 3 条重复建表）
            with pytest.raises(Exception):
                await schema_mod.run_migrations(conn)

            # 验证：前面的语句已回滚（test_t1/test_t2 不应存在）
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'test_%'"
            )
            tables = [r[0] for r in await cursor.fetchall()]
            assert tables == [], f"迁移失败后应回滚，但发现表: {tables}"
        finally:
            await conn.close()

    async def test_migration_version_not_recorded_on_failure(self, monkeypatch, tmp_path):
        """迁移失败时版本号不记录（避免重启后跳过失败迁移）"""
        import aiosqlite
        import repositories.schema as schema_mod

        failing_sql = """
            CREATE TABLE test_ok (id INTEGER);
            CREATE TABLE test_ok (id INTEGER);
        """
        _setup_migration_test(monkeypatch, [
            (200, "test failing migration", failing_sql),
        ])

        db_path = str(tmp_path / "test.db")
        conn = await aiosqlite.connect(db_path)
        try:
            with pytest.raises(Exception):
                await schema_mod.run_migrations(conn)

            cursor = await conn.execute("SELECT version FROM migrations WHERE version=200")
            row = await cursor.fetchone()
            assert row is None, "迁移失败时版本号不应记录"
        finally:
            await conn.close()

    async def test_migration_success_records_version(self, monkeypatch, tmp_path):
        """迁移成功时版本号记录 + 表和索引创建"""
        import aiosqlite
        import repositories.schema as schema_mod

        success_sql = """
            CREATE TABLE test_success (id INTEGER, name TEXT);
            CREATE INDEX idx_test_success ON test_success(name);
        """
        _setup_migration_test(monkeypatch, [
            (300, "test success migration", success_sql),
        ])

        db_path = str(tmp_path / "test.db")
        conn = await aiosqlite.connect(db_path)
        try:
            await schema_mod.run_migrations(conn)

            # 版本号记录
            cursor = await conn.execute("SELECT version, description FROM migrations WHERE version=300")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 300

            # 表和索引创建
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('test_success', 'idx_test_success')"
            )
            names = [r[0] for r in await cursor.fetchall()]
            assert "test_success" in names
            assert "idx_test_success" in names
        finally:
            await conn.close()

    async def test_multiple_migrations_partial_failure(self, monkeypatch, tmp_path):
        """多条迁移：第 1 条成功（已提交），第 2 条失败（sql 回滚，版本号不记录）

        验证：失败的迁移不影响已成功的迁移；失败的迁移的 sql 效果回滚。
        """
        import aiosqlite
        import repositories.schema as schema_mod

        success_sql = "CREATE TABLE test_m1 (id INTEGER);"
        failing_sql = """
            CREATE TABLE test_m2 (id INTEGER);
            CREATE TABLE test_m2 (id INTEGER);
        """
        _setup_migration_test(monkeypatch, [
            (400, "first success", success_sql),
            (401, "second failing", failing_sql),
        ])

        db_path = str(tmp_path / "test.db")
        conn = await aiosqlite.connect(db_path)
        try:
            with pytest.raises(Exception):
                await schema_mod.run_migrations(conn)

            # migration 1 成功：表存在 + 版本号记录
            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE name='test_m1'")
            assert (await cursor.fetchone()) is not None
            cursor = await conn.execute("SELECT version FROM migrations WHERE version=400")
            assert (await cursor.fetchone()) is not None

            # migration 2 失败：表回滚（不存在）+ 版本号不记录
            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE name='test_m2'")
            assert (await cursor.fetchone()) is None, "失败的迁移的表应回滚"
            cursor = await conn.execute("SELECT version FROM migrations WHERE version=401")
            assert (await cursor.fetchone()) is None, "失败的迁移版本号不应记录"
        finally:
            await conn.close()
