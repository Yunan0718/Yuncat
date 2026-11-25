"""tests/unit/test_model_config_cache.py —— Batch 7.9: ModelConfigService TTL 缓存测试"""
import asyncio
import os
import tempfile

import pytest
import yaml


@pytest.mark.unit
class TestModelConfigServiceCache:
    """Batch 7.9: ModelConfigService 热点查询 TTL 缓存行为"""

    @pytest.fixture(autouse=True)
    async def setup(self):
        """每个测试使用独立的临时配置目录和内存 DB。"""
        from services.model_config_service import ModelConfigService
        from services.settings_service import SettingsService
        import aiosqlite
        from repositories import init_db, run_migrations

        self.tmpdir = tempfile.mkdtemp()
        config_dir = os.path.join(self.tmpdir, "config", "models")
        os.makedirs(config_dir, exist_ok=True)

        # 写入两个 provider
        for pid, data in [
            ("p1", {
                "provider": "p1", "name": "Provider1",
                "base_url": "https://p1.api", "model": "m1",
                "models": [{"id": "m1", "name": "Model1", "context": 32768, "supports": ["text"]}],
            }),
            ("p2", {
                "provider": "p2", "name": "Provider2",
                "base_url": "https://p2.api", "model": "m2",
                "models": [{"id": "m2", "name": "Model2", "context": 32768, "supports": ["text"]}],
            }),
        ]:
            with open(os.path.join(config_dir, f"{pid}.yaml"), "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        with open(os.path.join(config_dir, "_index.yaml"), "w", encoding="utf-8") as f:
            yaml.dump({"active": "p1", "providers": ["p1", "p2"]}, f)

        db_path = os.path.join(self.tmpdir, "test.db")
        self.db = await init_db(db_path)
        await run_migrations(self.db)
        self.settings = SettingsService(self.db, cache_ttl=0.0)
        # 短 TTL 方便测试
        self.svc = ModelConfigService(
            config_dir=config_dir,
            settings_service=self.settings,
            cache_ttl=0.05,
        )
        yield
        await self.db.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_list_providers_cached(self):
        """list_providers 在 TTL 内只查询一次 settings/解密。"""
        calls = []
        original = self.settings.get_raw

        async def tracked_get_raw(key: str):
            calls.append(key)
            return await original(key)

        self.settings.get_raw = tracked_get_raw
        r1 = await self.svc.list_providers()
        r2 = await self.svc.list_providers()
        assert r1 == r2
        # security.key_rotation_warn_days + 2 个 provider 的 api_key 查询
        assert len(calls) <= 3

    async def test_save_api_key_invalidates_providers_cache(self):
        """save_api_key 后 list_providers 重新加载。"""
        r1 = await self.svc.list_providers()
        p1_before = next(p for p in r1 if p["provider"] == "p1")
        assert p1_before["api_key_configured"] is False
        await self.svc.save_api_key("p1", "secret-key")
        r2 = await self.svc.list_providers()
        p1_after = next(p for p in r2 if p["provider"] == "p1")
        assert p1_after["api_key_configured"] is True

    async def test_get_provider_config_cached(self):
        """get_provider_config 缓存解密后的 api_key。"""
        await self.svc.save_api_key("p1", "secret-key")
        cfg1 = await self.svc.get_provider_config("p1")
        # 绕过 save，直接改 settings 模拟 key 轮换
        await self.settings.save("api_key:p1", "new-secret")
        cfg2 = await self.svc.get_provider_config("p1")
        assert cfg2["api_key"] == "secret-key"  # 缓存命中
        await asyncio.sleep(0.06)
        cfg3 = await self.svc.get_provider_config("p1")
        assert cfg3["api_key"] == "new-secret"  # TTL 过期后刷新

    async def test_set_model_invalidates_provider_config_cache(self):
        """set_model 后 get_provider_config 立即刷新。"""
        cfg1 = await self.svc.get_provider_config("p1")
        assert cfg1["model"] == "m1"
        await self.svc.set_model("p1", "m1-new")
        cfg2 = await self.svc.get_provider_config("p1")
        assert cfg2["model"] == "m1-new"

    async def test_get_active_config_cached_and_invalidated(self):
        """get_active_config 按 agent_id 缓存，set_active 后失效。"""
        cfg1 = await self.svc.get_active_config()
        assert cfg1["provider"] == "p1"
        # set_active 现要求目标 provider 已配置 API key（防切到不可用配置）
        await self.svc.save_api_key("p2", "p2-secret")
        await self.svc.set_active("p2")
        cfg2 = await self.svc.get_active_config()
        assert cfg2["provider"] == "p2"

    async def test_cache_ttl_zero_disables_cache(self):
        """cache_ttl=0 时不缓存。"""
        from services.model_config_service import ModelConfigService
        svc_no_cache = ModelConfigService(
            config_dir=str(self.svc._config_dir),
            settings_service=self.settings,
            cache_ttl=0.0,
        )
        calls = []
        original = self.settings.get_decrypted

        async def tracked_get_decrypted(key: str):
            calls.append(key)
            return await original(key)

        self.settings.get_decrypted = tracked_get_decrypted
        await svc_no_cache.get_active_config()
        await svc_no_cache.get_active_config()
        assert len([c for c in calls if c == "api_key:p1"]) == 2

    async def test_lazy_cache_initialization_with_new(self):
        """通过 __new__ 构造的对象也能惰性初始化缓存字段。"""
        from services.model_config_service import ModelConfigService
        svc = ModelConfigService.__new__(ModelConfigService)
        svc._settings = self.settings
        svc._providers = {
            "lazy": {"name": "Lazy", "base_url": "https://lazy.api", "model": "m", "models": []},
        }
        svc._active = "lazy"
        # 未调用 __init__，缓存字段不存在
        assert not hasattr(svc, "_cache_lock")
        result = await svc.list_providers()
        assert len(result) == 1
        assert hasattr(svc, "_cache_lock")
