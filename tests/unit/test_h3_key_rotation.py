"""tests/unit/test_h3_key_rotation.py —— H3: API key 轮换年龄追踪测试

覆盖:
- Migration #23: settings.updated_at 列存在
- SettingsService.save() 三条路径均更新 updated_at
- SettingsService.get_key_age_days() / get_key_updated_at() 计算正确
- ModelConfigService.list_providers() 返回 rotation_status / key_age_days 字段
- rotation_status 计算逻辑（ok/warn/expired/disabled）

H3 范围: 最小提醒方案——settings 表加 updated_at 列，list_providers 附带年龄信息，
前端显示徽章+横幅。不强制轮换，纯软提示。
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
    db_path = os.path.join(str(tmp_path), "test_h3.db")
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    from repositories import SCHEMA_SQL, run_migrations
    await conn.executescript(SCHEMA_SQL)
    await run_migrations(conn)
    yield conn
    await conn.close()
    _safe_unlink(db_path)


@pytest.fixture
async def settings_svc(db):
    """SettingsService 实例"""
    from services.settings_service import SettingsService
    return SettingsService(db)


async def _get_updated_at(conn, key: str) -> int | None:
    cursor = await conn.execute("SELECT updated_at FROM settings WHERE key=?", (key,))
    row = await cursor.fetchone()
    return row[0] if row else None


# ─── Migration #23: settings.updated_at 列 ───


class TestUpdatedAtMigration:
    """H3 migration #23: settings 表 updated_at 列存在性"""

    async def test_updated_at_column_exists(self, db):
        """settings 表应有 updated_at 列"""
        cursor = await db.execute("PRAGMA table_info(settings)")
        columns = [r[1] for r in await cursor.fetchall()]
        assert "updated_at" in columns, "settings 表应有 updated_at 列（H3 migration #23）"

    async def test_updated_at_defaults_to_now(self, db):
        """新插入的行 updated_at 默认为当前时间戳"""
        before = int(time.time())
        await db.execute("INSERT INTO settings(key, value) VALUES('test-key', 'val')")
        await db.commit()
        ts = await _get_updated_at(db, "test-key")
        assert ts is not None
        # 允许 2 秒误差
        assert before - 2 <= ts <= int(time.time()) + 2

    async def test_migration_version_23_recorded(self, db):
        """migrations 表应记录 v23"""
        cursor = await db.execute("SELECT version FROM migrations WHERE version=23")
        row = await cursor.fetchone()
        assert row is not None, "migrations 表应记录 v23（H3）"


# ─── SettingsService.save() 更新 updated_at ───


class TestSaveUpdatesTimestamp:
    """H3: SettingsService.save() 三条路径均更新 updated_at"""

    async def test_default_upsert_sets_updated_at(self, settings_svc, db):
        """默认 upsert 路径设置 updated_at"""
        await settings_svc.save("theme", "dark")
        ts = await _get_updated_at(db, "theme")
        assert ts is not None
        assert ts > 0

    async def test_default_upsert_refreshes_updated_at_on_update(self, settings_svc, db):
        """再次 save 时 updated_at 刷新"""
        await settings_svc.save("theme", "dark")
        ts1 = await _get_updated_at(db, "theme")
        # 等待 1.1 秒确保时间戳不同
        time.sleep(1.1)
        await settings_svc.save("theme", "light")
        ts2 = await _get_updated_at(db, "theme")
        assert ts2 > ts1, "再次 save 后 updated_at 应刷新"

    async def test_optimistic_lock_create_sets_updated_at(self, settings_svc, db):
        """expected_version=0 创建路径设置 updated_at"""
        result = await settings_svc.save("newkey", "val", expected_version=0)
        assert result is True
        ts = await _get_updated_at(db, "newkey")
        assert ts is not None
        assert ts > 0

    async def test_optimistic_lock_update_sets_updated_at(self, settings_svc, db):
        """expected_version 匹配更新路径刷新 updated_at"""
        await settings_svc.save("k", "v1")  # version=1
        ts1 = await _get_updated_at(db, "k")
        time.sleep(1.1)
        result = await settings_svc.save("k", "v2", expected_version=1)
        assert result is True
        ts2 = await _get_updated_at(db, "k")
        assert ts2 > ts1, "乐观锁更新路径应刷新 updated_at"


# ─── SettingsService.get_key_age_days / get_key_updated_at ───


class TestKeyAgeCalculation:
    """H3: key 年龄计算"""

    async def test_get_key_updated_at_returns_timestamp(self, settings_svc):
        """get_key_updated_at 返回时间戳"""
        await settings_svc.save("api_key:test", "secret")
        ts = await settings_svc.get_key_updated_at("api_key:test")
        assert ts is not None
        assert ts > 0

    async def test_get_key_updated_at_none_for_missing_key(self, settings_svc):
        """不存在的 key 返回 None"""
        ts = await settings_svc.get_key_updated_at("api_key:nonexistent")
        assert ts is None

    async def test_get_key_age_days_zero_for_fresh_key(self, settings_svc):
        """新保存的 key 年龄为 0 天"""
        await settings_svc.save("api_key:fresh", "secret")
        age = await settings_svc.get_key_age_days("api_key:fresh")
        assert age == 0

    async def test_get_key_age_days_correct_for_old_key(self, settings_svc, db):
        """手动设置旧 updated_at 后年龄计算正确"""
        await settings_svc.save("api_key:old", "secret")
        # 模拟 100 天前写入
        old_ts = int(time.time()) - 100 * 86400
        await db.execute(
            "UPDATE settings SET updated_at=? WHERE key=?", (old_ts, "api_key:old")
        )
        await db.commit()
        age = await settings_svc.get_key_age_days("api_key:old")
        assert age == 100

    async def test_get_key_age_days_none_for_missing_key(self, settings_svc):
        """不存在的 key 年龄返回 None"""
        age = await settings_svc.get_key_age_days("api_key:nonexistent")
        assert age is None


# ─── ModelConfigService.list_providers() rotation_status ───


@pytest.fixture
async def model_config_with_keys(db, settings_svc):
    """构造带 3 个 provider 的 ModelConfigService（绕过 YAML 加载）"""
    from services.model_config_service import ModelConfigService
    svc = ModelConfigService.__new__(ModelConfigService)
    svc._settings = settings_svc
    svc._providers = {
        "fresh": {"name": "Fresh", "base_url": "https://fresh.api", "model": "m1", "models": []},
        "aging": {"name": "Aging", "base_url": "https://aging.api", "model": "m2", "models": []},
        "stale": {"name": "Stale", "base_url": "https://stale.api", "model": "m3", "models": []},
        "nokey": {"name": "NoKey", "base_url": "https://nokey.api", "model": "m4", "models": []},
    }
    svc._active = "fresh"
    svc._health_cache = {}

    # 保存 3 个 key（fresh=0天, aging=70天, stale=100天）
    await settings_svc.save("api_key:fresh", "key-fresh")
    await settings_svc.save("api_key:aging", "key-aging")
    await settings_svc.save("api_key:stale", "key-stale")

    # 手动调整 aging 和 stale 的 updated_at
    now = int(time.time())
    await db.execute(
        "UPDATE settings SET updated_at=? WHERE key=?",
        (now - 70 * 86400, "api_key:aging"),
    )
    await db.execute(
        "UPDATE settings SET updated_at=? WHERE key=?",
        (now - 100 * 86400, "api_key:stale"),
    )
    await db.commit()
    return svc


class TestListProvidersRotationStatus:
    """H3: list_providers() 返回 rotation_status / key_age_days"""

    async def test_fresh_key_returns_ok_status(self, model_config_with_keys):
        """0 天 key → rotation_status='ok'"""
        result = await model_config_with_keys.list_providers()
        fresh = next(p for p in result if p["provider"] == "fresh")
        assert fresh["api_key_configured"] is True
        assert fresh["key_age_days"] == 0
        assert fresh["rotation_status"] == "ok"

    async def test_aging_key_returns_warn_status(self, model_config_with_keys):
        """70 天 key（>= 60 = 90*2/3）→ rotation_status='warn'"""
        result = await model_config_with_keys.list_providers()
        aging = next(p for p in result if p["provider"] == "aging")
        assert aging["api_key_configured"] is True
        assert aging["key_age_days"] == 70
        assert aging["rotation_status"] == "warn"

    async def test_stale_key_returns_expired_status(self, model_config_with_keys):
        """100 天 key（>= 90）→ rotation_status='expired'"""
        result = await model_config_with_keys.list_providers()
        stale = next(p for p in result if p["provider"] == "stale")
        assert stale["api_key_configured"] is True
        assert stale["key_age_days"] == 100
        assert stale["rotation_status"] == "expired"

    async def test_unconfigured_provider_no_rotation_fields(self, model_config_with_keys):
        """未配置 key 的 provider 不返回 rotation_status / key_age_days"""
        result = await model_config_with_keys.list_providers()
        nokey = next(p for p in result if p["provider"] == "nokey")
        assert nokey["api_key_configured"] is False
        assert "rotation_status" not in nokey
        assert "key_age_days" not in nokey

    async def test_warn_days_zero_disables_rotation(self, model_config_with_keys, settings_svc, db):
        """security.key_rotation_warn_days=0 → rotation_status='disabled'"""
        await settings_svc.save("security.key_rotation_warn_days", "0")
        result = await model_config_with_keys.list_providers()
        stale = next(p for p in result if p["provider"] == "stale")
        assert stale["rotation_status"] == "disabled"
        # key_age_days 仍返回
        assert stale["key_age_days"] == 100

    async def test_warn_days_custom_threshold(self, model_config_with_keys, settings_svc, db):
        """security.key_rotation_warn_days=200 → 100 天 key 状态='ok'（< 200*2/3=133）"""
        await settings_svc.save("security.key_rotation_warn_days", "200")
        result = await model_config_with_keys.list_providers()
        stale = next(p for p in result if p["provider"] == "stale")
        assert stale["rotation_status"] == "ok"
        assert stale["key_age_days"] == 100

    async def test_warn_boundary_exactly_at_threshold(self, model_config_with_keys, settings_svc, db):
        """边界: age 恰好 == warn_days*2/3 → 'warn'（>= 判断）"""
        # warn_days=90, 2/3=60, age=60 → warn
        await settings_svc.save("api_key:boundary", "key-b")
        now = int(time.time())
        await db.execute(
            "UPDATE settings SET updated_at=? WHERE key=?",
            (now - 60 * 86400, "api_key:boundary"),
        )
        await db.commit()
        # 临时加一个 provider
        model_config_with_keys._providers["boundary"] = {
            "name": "Boundary", "base_url": "https://b.api", "model": "m", "models": []
        }
        result = await model_config_with_keys.list_providers()
        boundary = next(p for p in result if p["provider"] == "boundary")
        assert boundary["key_age_days"] == 60
        assert boundary["rotation_status"] == "warn"

    async def test_expired_boundary_exactly_at_warn_days(self, model_config_with_keys, settings_svc, db):
        """边界: age 恰好 == warn_days → 'expired'（>= 判断）"""
        await settings_svc.save("api_key:expbd", "key-e")
        now = int(time.time())
        await db.execute(
            "UPDATE settings SET updated_at=? WHERE key=?",
            (now - 90 * 86400, "api_key:expbd"),
        )
        await db.commit()
        model_config_with_keys._providers["expbd"] = {
            "name": "ExpBd", "base_url": "https://e.api", "model": "m", "models": []
        }
        result = await model_config_with_keys.list_providers()
        expbd = next(p for p in result if p["provider"] == "expbd")
        assert expbd["key_age_days"] == 90
        assert expbd["rotation_status"] == "expired"
