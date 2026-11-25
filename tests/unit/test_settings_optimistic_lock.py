"""tests/unit/test_settings_optimistic_lock.py —— A2: settings 写入乐观锁

覆盖:
- 无 expected_version 时 upsert + version 自增（向后兼容）
- expected_version 匹配时更新成功 + version+1
- expected_version 不匹配时拒绝（竞态保护）
- 新 key 首次写入 version=1
- version 列存在性验证（migration 生效）
"""
import os
import sys
import time
import pytest
import aiosqlite

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
async def db(tmp_path):
    """创建带 schema + migrations 的 aiosqlite 连接"""
    db_path = os.path.join(str(tmp_path), "test_a2.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL, run_migrations
    await conn.executescript(SCHEMA_SQL)
    await run_migrations(conn)
    yield conn
    await conn.close()
    _safe_unlink(db_path)


async def _get_version(conn, key: str) -> int:
    cursor = await conn.execute("SELECT version FROM settings WHERE key=?", (key,))
    row = await cursor.fetchone()
    return row[0] if row else 0


class TestVersionColumnMigration:
    """version 列存在性验证"""

    async def test_version_column_exists(self, db):
        """settings 表应有 version 列"""
        cursor = await db.execute("PRAGMA table_info(settings)")
        columns = [r[1] for r in await cursor.fetchall()]
        assert "version" in columns, "settings 表应有 version 列（A2 migration）"

    async def test_version_defaults_to_1(self, db):
        """新插入的行 version 默认为 1"""
        await db.execute("INSERT INTO settings(key, value) VALUES('test-key', 'val')")
        await db.commit()
        assert await _get_version(db, "test-key") == 1


class TestSaveWithoutExpectedVersion:
    """无 expected_version 时 upsert + version 自增（向后兼容）"""

    async def test_first_save_sets_version_1(self, db):
        """首次写入 version=1"""
        from services.settings_service import SettingsService
        svc = SettingsService(db)
        await svc.save("theme", "dark")
        assert await _get_version(db, "theme") == 1

    async def test_second_save_increments_version(self, db):
        """第二次写入 version=2"""
        from services.settings_service import SettingsService
        svc = SettingsService(db)
        await svc.save("theme", "dark")
        await svc.save("theme", "light")
        assert await _get_version(db, "theme") == 2

    async def test_value_updated_correctly(self, db):
        """值正确更新"""
        from services.settings_service import SettingsService
        svc = SettingsService(db)
        await svc.save("lang", "en")
        await svc.save("lang", "zh")
        assert await svc.get_raw("lang") == "zh"


class TestSaveWithExpectedVersion:
    """expected_version 匹配时更新成功 + version+1"""

    async def test_update_with_matching_version(self, db):
        """version 匹配时更新成功"""
        from services.settings_service import SettingsService
        svc = SettingsService(db)
        await svc.save("workspace", "/old")
        # version 现在是 1

        result = await svc.save("workspace", "/new", expected_version=1)
        assert result is True
        assert await svc.get_raw("workspace") == "/new"
        assert await _get_version(db, "workspace") == 2

    async def test_update_with_version_2(self, db):
        """连续更新 version 递增"""
        from services.settings_service import SettingsService
        svc = SettingsService(db)
        await svc.save("k", "v1")  # version=1
        await svc.save("k", "v2", expected_version=1)  # version=2

        result = await svc.save("k", "v3", expected_version=2)  # version=3
        assert result is True
        assert await svc.get_raw("k") == "v3"
        assert await _get_version(db, "k") == 3


class TestSaveVersionMismatch:
    """expected_version 不匹配时拒绝——A2 核心竞态保护"""

    async def test_reject_when_version_mismatch(self, db):
        """version 不匹配时拒绝写入，返回 False"""
        from services.settings_service import SettingsService
        svc = SettingsService(db)
        await svc.save("api_key", "old-key")  # version=1

        # 模拟竞态：另一个进程已更新到 version=2
        await svc.save("api_key", "stolen-key")  # version=2

        # 用户用过期的 version=1 尝试写入——应拒绝
        result = await svc.save("api_key", "user-key", expected_version=1)
        assert result is False
        # 值应保持 stolen-key，不被 user-key 覆盖
        # 注意：api_key 是加密的，get_raw 返回密文，用 get_decrypted 验证
        assert await svc.get_decrypted("api_key") == "stolen-key"

    async def test_reject_preserves_version(self, db):
        """拒绝写入时 version 不变"""
        from services.settings_service import SettingsService
        svc = SettingsService(db)
        await svc.save("k", "v1")  # version=1
        await svc.save("k", "v2")  # version=2

        # 用错误的 version 尝试写入
        result = await svc.save("k", "v3", expected_version=1)
        assert result is False
        assert await _get_version(db, "k") == 2  # version 仍为 2

    async def test_reject_for_nonexistent_key_with_wrong_version(self, db):
        """不存在的 key + expected_version != 0 时拒绝"""
        from services.settings_service import SettingsService
        svc = SettingsService(db)
        # 不存在的 key version=0，传 expected_version=1 应拒绝
        result = await svc.save("newkey", "val", expected_version=1)
        assert result is False
        # key 不应被创建
        assert await svc.get_raw("newkey") is None

    async def test_allow_create_with_expected_version_zero(self, db):
        """不存在的 key + expected_version=0 时允许创建"""
        from services.settings_service import SettingsService
        svc = SettingsService(db)
        result = await svc.save("newkey", "val", expected_version=0)
        assert result is True
        assert await svc.get_raw("newkey") == "val"
        assert await _get_version(db, "newkey") == 1
