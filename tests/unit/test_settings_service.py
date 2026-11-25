"""tests/unit/test_settings_service.py —— 设置服务单元测试"""
import pytest
import os
import tempfile
import aiosqlite

from repositories import init_db, run_migrations


@pytest.mark.unit
class TestSettingsService:
    @pytest.fixture(autouse=True)
    async def setup_db(self):
        """每个测试使用独立的临时数据库"""
        from services.settings_service import SettingsService
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = await init_db(path)
        await run_migrations(self.db)
        self.svc = SettingsService(self.db)
        yield
        await self.db.close()
        os.unlink(path)

    async def test_save_and_get_raw(self):
        await self.svc.save("test_key", "test_value")
        assert await self.svc.get_raw("test_key") == "test_value"

    async def test_get_all(self):
        await self.svc.save("key1", "val1")
        await self.svc.save("key2", "val2")
        all_settings = await self.svc.get_all()
        assert all_settings["key1"] == "val1"
        assert all_settings["key2"] == "val2"

    async def test_api_key_masked_in_get_all(self):
        await self.svc.save("api_key", "sk-secret-123")
        all_settings = await self.svc.get_all()
        assert all_settings["api_key"] == "***encrypted***"

    async def test_save_overwrites_existing(self):
        await self.svc.save("key", "val1")
        await self.svc.save("key", "val2")
        assert await self.svc.get_raw("key") == "val2"

    async def test_get_decrypted_non_encrypted(self):
        await self.svc.save("theme", "dark")
        assert await self.svc.get_decrypted("theme") == "dark"


@pytest.mark.unit
class TestSettingsServiceCache:
    """Batch 7.9: SettingsService TTL 缓存行为"""

    @pytest.fixture(autouse=True)
    async def setup_db(self):
        from services.settings_service import SettingsService
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = await init_db(path)
        await run_migrations(self.db)
        # 短 TTL 方便测试，同时验证默认路径外可传入自定义 ttl
        self.svc = SettingsService(self.db, cache_ttl=0.05)
        yield
        await self.db.close()
        os.unlink(path)

    async def test_cache_hit_avoids_db_query(self):
        """缓存有效期内第二次 get_raw 不应再访问 DB。"""
        await self.svc.save("cached_key", "v1")
        calls = []
        original = self.db.execute

        async def tracked_execute(sql, params=()):
            calls.append((sql, params))
            return await original(sql, params)

        self.db.execute = tracked_execute
        first = await self.svc.get_raw("cached_key")
        second = await self.svc.get_raw("cached_key")
        assert first == second == "v1"
        assert len([c for c in calls if "SELECT value FROM settings" in c[0]]) == 1

    async def test_ttl_expiry_refreshes_from_db(self):
        """超过 TTL 后应重新从 DB 读取。"""
        await self.svc.save("ttl_key", "v1")
        assert await self.svc.get_raw("ttl_key") == "v1"
        # 绕过缓存直接修改 DB
        await self.db.execute(
            "UPDATE settings SET value=? WHERE key=?", ("v2", "ttl_key")
        )
        await self.db.commit()
        # 缓存仍有效
        assert await self.svc.get_raw("ttl_key") == "v1"
        # 等待 TTL 过期
        import asyncio
        await asyncio.sleep(0.06)
        assert await self.svc.get_raw("ttl_key") == "v2"

    async def test_save_invalidates_cache(self):
        """save 后缓存失效，下一次读取拿到新值。"""
        await self.svc.save("inv_key", "v1")
        assert await self.svc.get_raw("inv_key") == "v1"
        # 直接改 DB，再 save 另一个 key——不应影响 inv_key 缓存
        await self.db.execute(
            "UPDATE settings SET value=? WHERE key=?", ("v2", "inv_key")
        )
        await self.db.commit()
        # save 不相关 key
        await self.svc.save("other_key", "x")
        # inv_key 缓存仍在（只清了自己和 all_cache）
        assert await self.svc.get_raw("inv_key") == "v1"
        # save inv_key 自己
        await self.svc.save("inv_key", "v3")
        assert await self.svc.get_raw("inv_key") == "v3"

    async def test_get_all_cached_and_invalidated(self):
        """get_all 结果被缓存，save 后失效。"""
        await self.svc.save("ga1", "a")
        all1 = await self.svc.get_all()
        calls = []
        original = self.db.execute

        async def tracked_execute(sql, params=()):
            calls.append(sql)
            return await original(sql, params)

        self.db.execute = tracked_execute
        all2 = await self.svc.get_all()
        assert all1 == all2
        assert not any("SELECT key, value FROM settings" in c for c in calls)
        await self.svc.save("ga2", "b")
        all3 = await self.svc.get_all()
        assert "ga2" in all3

    async def test_cache_ttl_zero_disables_cache(self):
        """cache_ttl=0 时每次查询都走 DB。"""
        from services.settings_service import SettingsService
        svc_no_cache = SettingsService(self.db, cache_ttl=0.0)
        await svc_no_cache.save("no_cache_key", "v1")
        calls = []
        original = self.db.execute

        async def tracked_execute(sql, params=()):
            calls.append(sql)
            return await original(sql, params)

        self.db.execute = tracked_execute
        await svc_no_cache.get_raw("no_cache_key")
        await svc_no_cache.get_raw("no_cache_key")
        select_calls = [c for c in calls if "SELECT value FROM settings" in c]
        assert len(select_calls) == 2
