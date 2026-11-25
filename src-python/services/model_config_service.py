"""services/model_config_service.py —— 模型配置服务（ccswitch 风格）

config/models/ 目录中每个提供商一个 YAML 文件，_index.yaml 管理激活的提供商。
用户填写的 api_key 和自选 model 加密存储到 SQLite settings 表。
v78: Batch 7.9 增加 TTL 缓存，减少热点查询的 YAML/DB/解密开销。
"""
import asyncio
import json
import logging
import os
import time as _time
from pathlib import Path
from copy import deepcopy

import yaml

from services.provider_key_state import provider_has_usable_key

_log = logging.getLogger("yuncat.models")

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "config", "models")
LEGACY_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "config", "models.yaml")

# 模型目录 GitHub 数据源（手动触发，非自动拉取）。
# 采用 LiteLLM 的 model_prices_and_context_window.json —— 事实行业标准，
# 含精确 model id + per-token 定价 + 上下文窗口 + 能力标记，社区每日维护。
MODEL_CATALOG_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
MODEL_CATALOG_TIMEOUT_SECONDS = 30.0
MODEL_CATALOG_MAX_MODELS = 60

# Yuncat provider → LiteLLM litellm_provider 值集合。
# 仅映射 LiteLLM 有一等公民支持的厂商；未映射的（qwen/glm/siliconflow/doubao/
# hunyuan/baidu/ollama/openrouter/custom）走 openrouter 聚合或无稳定 ID，返回提示让用户手动填写。
_LITELLM_PROVIDER_MAP: dict[str, set[str]] = {
    "openai": {"openai"},
    "anthropic": {"anthropic"},
    "deepseek": {"deepseek"},
    "gemini": {"gemini", "vertex_ai"},
    "grok": {"xai"},
    "mistral": {"mistral"},
    "kimi": {"moonshot"},
    # 2026-09-10 扩充：解析是「litellm_provider ∈ set」的集合匹配，**多写一个同厂商的别名
    # 是安全的**（匹配不到就是空集），真正的风险只在于「写成了别家厂商的 key」——
    # 那会把别家模型灌进该 provider。故这里只放**厂商独占**的 key。
    "qwen": {"dashscope"},              # 阿里独占；QwenCloud/Qwen AI Platform 也走该实现
    "glm": {"zhipuai", "zai"},          # 智谱新旧两个 key 都是它自己
    "hunyuan": {"hunyuan"},             # 腾讯独占
    "baidu": {"qianfan"},               # 百度千帆独占
    "siliconflow": {"siliconflow"},     # 平台独占（其托管的三方开源模型本就可用）
    "ollama": {"ollama"},
    "openrouter": {"openrouter"},       # 聚合平台，本就托管各家模型
    # 刻意不映射：
    # - doubao：豆包走 volcengine，但 volcengine 目录里**同时收录 DeepSeek 等别家模型**，
    #   映射后会把别家模型灌进「豆包」列表 → 宁可让用户手工填。
    # - custom：无目录数据源。
}

# Phase 109: 厂商展示顺序——设置页 / 模型选择器里的厂商按此排列（未知厂商置后）。
# 把默认激活的 deepseek 放最前，其余按国内/国际主流认知排序。
_PROVIDER_DISPLAY_ORDER: list[str] = [
    "deepseek",
    "openai",
    "anthropic",
    "gemini",
    "qwen",
    "kimi",
    "doubao",
    "grok",
    "mistral",
    "glm",
    "hunyuan",
    "baidu",
    "siliconflow",
    "ollama",
    "openrouter",
    "custom",
]


def parse_model_catalog(data: dict, litellm_providers: set[str]) -> list[dict]:
    """解析 LiteLLM 目录 JSON，返回 [{id, name, context, supports, input, output, cached_input}]。

    仅取 chat 模型（排除 embedding / image / audio 等），定价从 per-token 美元
    换算为 per-million 美元。纯函数，便于单元测试。
    """
    result: list[dict] = []
    for key, info in data.items():
        if not isinstance(info, dict):
            continue
        if info.get("litellm_provider") not in litellm_providers:
            continue
        if info.get("mode") != "chat":
            continue
        # model id 取 key 最后一段（openai/gpt-5 → gpt-5）
        mid = str(key).rsplit("/", 1)[-1]
        in_cost = info.get("input_cost_per_token")
        out_cost = info.get("output_cost_per_token")
        if not isinstance(in_cost, (int, float)) or not isinstance(out_cost, (int, float)):
            continue
        supports = ["text", "tools"]
        if info.get("supports_vision"):
            supports.append("vision")
        if info.get("supports_reasoning"):
            supports.append("thinking")
        result.append({
            "id": mid,
            "name": mid,
            "context": int(info.get("max_input_tokens") or info.get("max_tokens") or 128_000),
            "supports": supports,
            "input": round(in_cost * 1_000_000, 4),
            "output": round(out_cost * 1_000_000, 4),
            "cached_input": round(info["cache_read_input_token_cost"] * 1_000_000, 4)
            if isinstance(info.get("cache_read_input_token_cost"), (int, float)) else None,
        })
    return result


def merge_catalog_into_provider(
    existing_models: list[dict],
    existing_pricing: dict,
    catalog: list[dict],
) -> tuple[list[dict], dict]:
    """把 LiteLLM 目录结果与手工维护的 YAML **合并**，而不是整体覆盖。

    目录的价值是「批量补全 id 与最新定价」，不是「替换用户的模型表」：

    - **已存在的 id**：保留手工维护的 `name` / `context` / `supports`。目录的 supports 只有
      `supports_vision` / `supports_reasoning` 两位启发式，比人工标注粗；context 也常缺，
      覆盖会把 1M 上下文退化成默认 128K。定价则以目录为准（刷新定价正是同步的目的）。
    - **目录独有的 id**：新增。
    - **已存在但目录里没有的 id**：保留——可能是目录未收录或已下线，删掉既是信息损失，
      也会让正在使用该模型的用户突然失去选项。
    """
    existing_by_id = {m.get("id"): m for m in (existing_models or []) if isinstance(m, dict)}
    merged: list[dict] = []
    seen: set[str] = set()

    for m in catalog:
        mid = m["id"]
        seen.add(mid)
        old = existing_by_id.get(mid)
        if old:
            merged.append({
                "id": mid,
                "name": old.get("name") or m["name"],
                "context": old.get("context") or m["context"],
                "supports": old.get("supports") or m["supports"],
            })
        else:
            merged.append({"id": mid, "name": m["name"],
                           "context": m["context"], "supports": m["supports"]})

    # 目录未收录的既有模型：原样保留在末尾，顺序稳定
    for m in (existing_models or []):
        if isinstance(m, dict) and m.get("id") and m["id"] not in seen:
            merged.append(dict(m))

    # 定价：目录覆盖同 id 的 input/output/currency/缓存价，其余既有定价与自定义字段保留。
    # 注意是"就地更新"而不是整条替换——目录不返回 cached_input 时，
    # 整条替换会把用户手填的缓存价抹掉。
    pricing: dict = dict(existing_pricing or {})
    for m in catalog:
        entry: dict = dict(pricing.get(m["id"]) or {})
        entry.update({"input": m["input"], "output": m["output"], "currency": "USD"})
        if m["cached_input"] is not None:
            entry["cached_input"] = m["cached_input"]
        pricing[m["id"]] = entry
    return merged, pricing


class ModelConfigService:
    """ccswitch 风格模型配置——每厂商一个配置文件。"""

    def __init__(
        self,
        config_dir: str = CONFIG_DIR,
        settings_service=None,
        cache_ttl: float | None = None,
    ):
        self._config_dir = Path(config_dir).resolve()
        self._settings = settings_service
        self._providers: dict[str, dict] = {}
        self._active: str = "deepseek"
        # Batch 7.9: TTL 缓存
        from config import MODEL_CONFIG_CACHE_TTL_SEC
        self._cache_ttl = (
            MODEL_CONFIG_CACHE_TTL_SEC if cache_ttl is None else float(cache_ttl)
        )
        self._providers_cache: tuple[float, list[dict]] | None = None
        self._provider_config_cache: dict[str, tuple[float, dict]] = {}
        self._active_config_cache: dict[str, tuple[float, dict]] = {}
        self._cache_lock = asyncio.Lock()
        self._load()

    # ============================================================
    # 加载
    # ============================================================

    def _load(self) -> None:
        """从 config/models/ 目录加载所有提供商配置。回退到旧版单文件。"""
        if not self._config_dir.exists():
            self._migrate_from_legacy()
        self._providers.clear()

        # 加载索引
        index_path = self._config_dir / "_index.yaml"
        if index_path.exists():
            index = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
            self._active = index.get("active", "deepseek")

        # 加载每个提供商文件
        for f in sorted(self._config_dir.glob("*.yaml")):
            if f.name.startswith("_"):
                continue
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                pid = data.get("provider") or f.stem
                self._providers[pid] = data
            except Exception as e:
                _log.warning("Failed to load %s: %s", f.name, e)

        if not self._providers:
            _log.warning("No provider configs found in %s, falling back to legacy", self._config_dir)
            self._migrate_from_legacy()

    def _migrate_from_legacy(self) -> None:
        """从旧版 models.yaml 迁移到目录格式"""
        legacy = Path(LEGACY_FILE)
        if not legacy.exists():
            return
        try:
            data = yaml.safe_load(legacy.read_text(encoding="utf-8")) or {}
            providers = data.get("providers", {})
            if not providers:
                return
            self._config_dir.mkdir(parents=True, exist_ok=True)

            self._active = data.get("active", "deepseek")
            index = {"active": self._active, "providers": list(providers.keys())}
            (self._config_dir / "_index.yaml").write_text(
                yaml.dump(index, allow_unicode=True, default_flow_style=False), encoding="utf-8")

            new_providers: dict[str, dict] = {}  # Phase 48 T3-13: 收集新格式（原赋 legacy 字典格式不一致）
            for pid, p in providers.items():
                out = {
                    "provider": pid,
                    "name": p.get("name", pid),
                    "base_url": p.get("base_url", ""),
                    "model": p.get("model", ""),
                    "auth": "api_key",
                    "pricing": {},
                    "models": [],
                }
                for m in p.get("models", []):
                    ctx = m.get("context", "128K")
                    if isinstance(ctx, str):
                        ctx_num = 32768
                        try:
                            if ctx.endswith('K'): ctx_num = int(ctx[:-1]) * 1024
                            elif ctx.endswith('M'): ctx_num = int(float(ctx[:-1]) * 1048576)
                        except (ValueError, TypeError, IndexError): pass
                    else:
                        ctx_num = int(ctx) if isinstance(ctx, (int, float)) else 32768
                    out["models"].append({
                        "id": m["id"], "name": m.get("name", m["id"]),
                        "context": ctx_num, "supports": ["text", "tools"],
                    })
                (self._config_dir / f"{pid}.yaml").write_text(
                    yaml.dump(out, allow_unicode=True, default_flow_style=False, sort_keys=False),
                    encoding="utf-8")
                new_providers[pid] = out

            # Phase 48 T3-13: 赋新格式字典（原 self._providers = providers 赋的是 legacy 格式，
            # 与 L46-54 从 yaml 文件加载的新格式结构不一致，导致 list_providers 等方法读取字段错位）
            self._providers = new_providers
            _log.info("Migrated legacy models.yaml to %d files in config/models/", len(providers))
        except Exception as e:
            _log.error("Legacy migration failed: %s", e)

    def _get_provider_path(self, pid: str) -> Path:
        return self._config_dir / f"{pid}.yaml"

    def _get_index_path(self) -> Path:
        return self._config_dir / "_index.yaml"

    # ============================================================
    # 列表 + 查询
    # ============================================================

    async def list_providers(self) -> list[dict]:
        """列出所有预置供应商（含模型列表、不含 api_key 明文）

        Phase 43 P1.E: 增加 health_status、configured 字段供前端展示。
        health_status: "active"(🟢当前) | "configured"(✅已配) | "unconfigured"(⚪未配) | "failed"(🔴失败)
        H3: 增加 api_key_updated_at / key_age_days / rotation_status 字段供前端轮换提醒。
        rotation_status: "ok"(绿) | "warn"(黄临近) | "expired"(红建议轮换) | "disabled"(关闭提醒)
        Batch 7.9: 结果 TTL 缓存，减少 DB/解密开销。
        """
        self._ensure_cache()
        if self._cache_ttl > 0:
            now = self._cache_now()
            async with self._cache_lock:
                if self._providers_cache is not None and now - self._providers_cache[0] < self._cache_ttl:
                    return self._providers_cache[1]

        # H3: 批量查询所有 api_key:* 的 updated_at，避免 N+1 查询
        key_ages: dict[str, int | None] = {}  # provider -> age_days (None=未配置或无时间戳)
        warn_days = 90  # 默认 90 天
        if self._settings:
            try:
                raw_warn = await self._settings.get_raw("security.key_rotation_warn_days")
                if raw_warn is not None:
                    warn_days = int(raw_warn)
            except (ValueError, TypeError):
                _log.debug("Invalid security.key_rotation_warn_days value, using default 90")
            try:
                cursor = await self._settings.db.execute(
                    "SELECT key, updated_at FROM settings WHERE key LIKE 'api_key:%'"
                )
                rows = await cursor.fetchall()
                import time as _t
                now = _t.time()
                for r in rows:
                    prov = r[0][len("api_key:"):]  # 去掉前缀
                    if r[1] is not None:
                        key_ages[prov] = max(0, int((now - int(r[1])) // 86400))
                    else:
                        key_ages[prov] = None
            except Exception:
                _log.debug("Failed to batch query key ages", exc_info=True)

        result = []
        for pid, p in self._providers.items():
            has_key = await provider_has_usable_key(p, self._settings, pid)
            # Phase 43 P1.E: 健康状态
            health = self._get_cached_health(pid)
            if health is None and has_key:
                health = "configured"
            elif health is None:
                health = "unconfigured"
            # 2026-10-07：active 不得掩盖「没有可用 key」（原来无条件覆盖，会让解不开的孤儿密文显示为健康）
            health = "active" if (pid == self._active and has_key) else health
            item = {
                "provider": pid,
                "name": p.get("name", pid),
                "base_url": p.get("base_url", ""),
                "website_url": p.get("website_url", ""),
                "api_key_url": p.get("api_key_url", p.get("website_url", "")),
                "needs_proxy": p.get("needs_proxy", False),
                "proxy_hint": p.get("proxy_hint", ""),
                "model": p.get("model", ""),
                "models": p.get("models", []),
                "api_key_configured": has_key,
                "configured": has_key,
                "health_status": health,
                "pricing": p.get("pricing", {}),
            }
            # H3: 附带 key 轮换年龄信息（仅当已配置 key 时）
            if has_key and pid in key_ages and key_ages[pid] is not None:
                age = key_ages[pid]
                item["key_age_days"] = age
                if warn_days <= 0:
                    item["rotation_status"] = "disabled"
                elif age >= warn_days:
                    item["rotation_status"] = "expired"
                elif age >= warn_days * 2 // 3:
                    item["rotation_status"] = "warn"
                else:
                    item["rotation_status"] = "ok"
            elif has_key:
                # key 已配置但 updated_at 缺失（旧库未迁移）——不显示状态
                item["key_age_days"] = None
                item["rotation_status"] = None
            result.append(item)

        # Phase 109: 厂商展示排序——按推荐顺序排列（未知厂商置后），
        # 让模型设置页 / 选择器里的厂商顺序稳定且符合直觉。
        result.sort(
            key=lambda p: _PROVIDER_DISPLAY_ORDER.index(p["provider"])
            if p["provider"] in _PROVIDER_DISPLAY_ORDER
            else len(_PROVIDER_DISPLAY_ORDER)
        )

        if self._cache_ttl > 0:
            async with self._cache_lock:
                self._providers_cache = (self._cache_now(), result)
        return result

    def _ensure_cache(self) -> None:
        """惰性初始化缓存字段——兼容通过 __new__ 构造的测试对象。"""
        if not hasattr(self, "_cache_lock"):
            self._cache_lock = asyncio.Lock()
        if not hasattr(self, "_providers_cache"):
            self._providers_cache = None
        if not hasattr(self, "_provider_config_cache"):
            self._provider_config_cache = {}
        if not hasattr(self, "_active_config_cache"):
            self._active_config_cache = {}
        if not hasattr(self, "_cache_ttl"):
            from config import MODEL_CONFIG_CACHE_TTL_SEC
            self._cache_ttl = MODEL_CONFIG_CACHE_TTL_SEC

    def _cache_now(self) -> float:
        """使用 monotonic 时钟，避免系统时间回拨导致缓存不刷新。"""
        return _time.monotonic()

    def _invalidate_all_caches_sync(self) -> None:
        """同步缓存失效——无需 event loop，供 sync 方法调用。

        CPython GIL 保证 dict/list 的原子操作，因此直接清空是安全的。
        """
        self._ensure_cache()
        self._providers_cache = None
        self._provider_config_cache.clear()
        self._active_config_cache.clear()

    async def _invalidate_all_caches(self) -> None:
        """异步缓存失效——带锁，供 async 方法调用。"""
        self._ensure_cache()
        async with self._cache_lock:
            self._providers_cache = None
            self._provider_config_cache.clear()
            self._active_config_cache.clear()

    def _get_cached_health(self, provider: str) -> str | None:
        """获取缓存的 Provider 健康状态。缓存 60s。"""
        import time as _t
        now = _t.time()
        cache = getattr(self, '_health_cache', None)
        if cache is None:
            self._health_cache: dict[str, tuple[float, str]] = {}
            cache = self._health_cache
        entry = cache.get(provider)
        if entry and now - entry[0] < 60:
            return entry[1]
        return None

    def set_health_status(self, provider: str, status: str):
        """记录 Provider 健康检查结果。"""
        import time as _t
        if not hasattr(self, '_health_cache'):
            self._health_cache = {}
        self._health_cache[provider] = (_t.time(), status)

    def get_active_provider(self) -> str:
        return self._active

    async def get_provider_config(self, provider: str) -> dict | None:
        """获取指定供应商的完整配置（含解密后的 api_key）"""
        self._ensure_cache()
        if self._cache_ttl > 0:
            now = self._cache_now()
            async with self._cache_lock:
                entry = self._provider_config_cache.get(provider)
                if entry is not None and now - entry[0] < self._cache_ttl:
                    return entry[1]

        p = self._providers.get(provider)
        if not p:
            return None
        cfg = deepcopy(p)
        if self._settings:
            decrypted_key = await self._settings.get_decrypted(f"api_key:{provider}")
            if decrypted_key:
                cfg["api_key"] = decrypted_key

        if self._cache_ttl > 0:
            async with self._cache_lock:
                self._provider_config_cache[provider] = (self._cache_now(), cfg)
        return cfg

    # ============================================================
    # 用户修改
    # ============================================================

    async def save_api_key(self, provider: str, api_key: str) -> None:
        """保存 API Key（加密写入 SQLite）"""
        if provider not in self._providers:
            raise ValueError(f"Unknown provider: {provider}")
        if self._settings:
            await self._settings.save(f"api_key:{provider}", api_key)
        else:
            raise RuntimeError("settings_service not available for API key encryption")
        await self._invalidate_all_caches()
        _log.info("API Key saved for provider %s", provider)

    async def set_model(self, provider: str, model: str) -> None:
        """修改供应商的当前模型"""
        if provider not in self._providers:
            raise ValueError(f"Unknown provider: {provider}")
        self._providers[provider]["model"] = model
        self._save_provider(provider)
        await self._invalidate_all_caches()

    async def set_pricing(self, provider: str, pricing: dict) -> None:
        """覆盖供应商的模型定价（用户手动更新，避免 YAML 快照过时）。

        pricing 结构: {model_id: {"input": float, "output": float, "currency": str, ...}}
        仅做结构校验，保留厂商自定义字段（如 cached_input）。未知 model_id 也保留——
        用户可能为尚未加入 models 列表的新模型手填定价。
        """
        if provider not in self._providers:
            raise ValueError(f"Unknown provider: {provider}")
        if not isinstance(pricing, dict):
            raise ValueError("pricing must be a dict")
        # 结构校验：每个条目必须是 dict，且 input/output 若存在须为数值
        for mid, p in pricing.items():
            if not isinstance(p, dict):
                raise ValueError(f"pricing[{mid}] must be a dict")
            for key in ("input", "output", "cached_input"):
                if key in p and p[key] is not None and not isinstance(p[key], (int, float)):
                    raise ValueError(f"pricing[{mid}].{key} must be a number")
        self._providers[provider]["pricing"] = deepcopy(pricing)
        self._save_provider(provider)
        await self._invalidate_all_caches()
        _log.info("Pricing updated for provider %s (%d models)", provider, len(pricing))

    async def sync_models_from_catalog(self, provider: str) -> dict:
        """从 GitHub 模型目录（LiteLLM）手动拉取并覆盖该 provider 的模型列表 + 定价。

        手动触发（前端按钮），不做后台定时任务——网络请求是显式副作用，由用户决定何时发生。
        拉取失败不破坏现有配置，返回错误信息供前端提示。

        返回: {"ok": bool, "count": int, "source": str, "error": str|None}
        """
        litellm_providers = _LITELLM_PROVIDER_MAP.get(provider)
        if not litellm_providers:
            return {
                "ok": False, "count": 0, "source": MODEL_CATALOG_URL,
                "error": f"Provider '{provider}' 暂无 GitHub 目录数据源，请手动填写模型与定价",
            }

        import aiohttp
        try:
            timeout = aiohttp.ClientTimeout(total=MODEL_CATALOG_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(MODEL_CATALOG_URL) as resp:
                    if resp.status != 200:
                        return {"ok": False, "count": 0, "source": MODEL_CATALOG_URL,
                                "error": f"目录返回 {resp.status}"}
                    data = await resp.json(content_type=None)
        except Exception as e:
            _log.warning("Model catalog sync failed for %s: %s", provider, e)
            return {"ok": False, "count": 0, "source": MODEL_CATALOG_URL, "error": str(e)}

        models = parse_model_catalog(data, litellm_providers)
        if not models:
            return {"ok": False, "count": 0, "source": MODEL_CATALOG_URL,
                    "error": "目录中未找到该 provider 的 chat 模型"}

        models = models[:MODEL_CATALOG_MAX_MODELS]

        # 合并 YAML 与目录结果，保留 base_url / website_url / fim 等其余字段
        p = self._providers.get(provider)
        if not p:
            return {"ok": False, "count": 0, "source": MODEL_CATALOG_URL,
                    "error": f"Unknown provider: {provider}"}

        # 合并而非覆盖：目录只补全 id 与定价，手工维护的 context / supports / name 优先。
        # 整体覆盖会静默抹掉人工精度的元数据（详见 merge_catalog_into_provider 注释）。
        merged_models, merged_pricing = merge_catalog_into_provider(
            p.get("models") or [], p.get("pricing") or {}, models)

        p["models"] = merged_models
        p["pricing"] = merged_pricing

        self._save_provider(provider)
        await self._invalidate_all_caches()
        _log.info("Model catalog synced for provider %s (%d models, %d from catalog)",
                  provider, len(merged_models), len(models))
        return {"ok": True, "count": len(merged_models), "source": MODEL_CATALOG_URL, "error": None}

    async def set_active(self, provider: str) -> None:
        """切换当前激活的供应商

        P2-1: 切换前校验 API key 是否已配置——防止用户切到未配置 key 的 provider
        导致下一个任务直接 401 失败。校验逻辑与 list_providers 一致：
        先查 YAML 明文 api_key，再查加密 settings 表。
        """
        if provider not in self._providers:
            raise ValueError(f"Unknown provider: {provider}")
        # P2-1: 校验 API key 已配置
        p = self._providers[provider]
        has_key = await provider_has_usable_key(p, self._settings, provider)
        if not has_key:
            raise ValueError(
                f"Provider '{provider}' 尚未配置 API key，无法切换为激活。"
                f"请先在设置中配置 API key。"
            )
        self._active = provider
        index = yaml.safe_load(self._get_index_path().read_text(encoding="utf-8")) or {}
        index["active"] = provider
        self._get_index_path().write_text(
            yaml.dump(index, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8")
        await self._invalidate_all_caches()
        _log.info("Active provider switched to %s", provider)

    def _save_provider(self, pid: str) -> None:
        """保存单个提供商配置——H3 修复：剥离 api_key 防止明文写入 YAML

        api_key 统一走加密存储（save_api_key → settings 表）。
        YAML 文件中已有的旧 api_key 在下次保存时自动被剥离（渐进迁移）。
        """
        from copy import deepcopy as _deepcopy
        p = self._providers.get(pid)
        if p:
            # H3 修复：YAML 文件不存储 api_key 明文
            p_safe = _deepcopy(p)
            p_safe.pop("api_key", None)
            self._get_provider_path(pid).write_text(
                yaml.dump(p_safe, allow_unicode=True, default_flow_style=False, sort_keys=False),
                encoding="utf-8")

    # ============================================================
    # AgentService 调用接口
    # ============================================================

    async def get_active_config(self, agent_id: str = "") -> dict:
        """返回当前激活模型的完整配置（含解密后的 api_key）。

        Phase 43 M.2: agent_id 参数可选 — 支持 per-agent 模型配置。
        如果 agent_id 在 _agent_providers 中有指定，优先使用该 provider。
        Batch 7.9: 结果 TTL 缓存，避免每次 Agent 调用都重复解密。
        """
        self._ensure_cache()
        cache_key = agent_id or "__default__"
        if self._cache_ttl > 0:
            now = self._cache_now()
            async with self._cache_lock:
                entry = self._active_config_cache.get(cache_key)
                if entry is not None and now - entry[0] < self._cache_ttl:
                    return entry[1]

        active = self._agent_providers.get(agent_id, self._active) if agent_id else self._active
        provider = self._providers.get(active, {})
        cfg = deepcopy(provider)

        api_key = ""
        if self._settings:
            decrypted = await self._settings.get_decrypted(f"api_key:{active}")
            if decrypted:
                api_key = decrypted
        if not api_key:
            api_key = provider.get("api_key", "")
        cfg["api_key"] = api_key
        cfg["provider"] = active

        if self._cache_ttl > 0:
            async with self._cache_lock:
                self._active_config_cache[cache_key] = (self._cache_now(), cfg)
        return cfg

    # Phase 43 M.2: Per-agent Provider 映射
    _agent_providers: dict[str, str] = {}

    def set_agent_provider(self, agent_id: str, provider: str) -> None:
        """设置某个 Agent 后端的独立 Provider。OC 用 Claude·Hermes 用 DeepSeek。"""
        self._agent_providers[agent_id] = provider
        # Batch 7.9: per-agent provider 变化会影响 get_active_config 缓存
        self._invalidate_all_caches_sync()
        _log.info("Agent '%s' provider set to '%s'", agent_id, provider)

    def get_agent_provider(self, agent_id: str) -> str | None:
        """获取某个 Agent 后端的独立 Provider。"""
        return self._agent_providers.get(agent_id)

    # ============================================================
    # Phase 108: 模型分级路由（Tiering）数据层
    # ============================================================

    _VALID_TIERS = {"lite", "normal", "max", "vision"}
    # 系统默认 fallback 链；用户可覆盖，但 v4 第一阶段先按默认值实现
    _DEFAULT_FALLBACK_CHAIN = {
        "lite": [],
        "normal": ["lite"],
        "max": ["normal", "lite"],
        "vision": ["max", "normal"],
    }

    def _tier_settings_key(self, tier: str) -> str:
        if tier not in self._VALID_TIERS:
            raise ValueError(f"Unknown tier: {tier}")
        return f"model_tier_{tier}"

    async def is_tiering_enabled(self) -> bool:
        """用户是否开启了分级路由。"""
        if not self._settings:
            return False
        raw = await self._settings.get_raw("model_tiering_enabled")
        return raw == "true"

    async def set_tiering_enabled(self, enabled: bool) -> None:
        """开启/关闭分级路由。"""
        if not self._settings:
            raise RuntimeError("settings_service not available")
        await self._settings.save("model_tiering_enabled", "true" if enabled else "false")
        await self._invalidate_all_caches()

    async def get_tier_config(self, tier: str) -> list[dict]:
        """返回指定 tier 的模型绑定列表（按用户拖动顺序）。

        每个条目为 {"provider": str, "model_id": str}。未配置时返回空列表。
        """
        if tier not in self._VALID_TIERS:
            raise ValueError(f"Unknown tier: {tier}")
        if not self._settings:
            return []
        raw = await self._settings.get_raw(self._tier_settings_key(tier))
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            _log.warning("Corrupt tier config for %s, resetting", tier)
            return []
        if not isinstance(data, list):
            return []
        result = []
        for item in data:
            if isinstance(item, dict) and "provider" in item and "model_id" in item:
                result.append({"provider": item["provider"], "model_id": item["model_id"]})
        return result

    async def set_tier_binding(
        self, tier: str, bindings: list[tuple[str, str] | dict[str, str]],
    ) -> None:
        """用户拖动/排序/删除后调用——写入 settings 表。

        bindings: [(provider, model_id), ...]
        """
        if tier not in self._VALID_TIERS:
            raise ValueError(f"Unknown tier: {tier}")
        normalized: list[dict] = []
        for b in bindings:
            if isinstance(b, dict):
                pid, mid = b.get("provider"), b.get("model_id")
            else:
                pid, mid = b[0], b[1]
            if not pid or not mid:
                continue
            if pid not in self._providers:
                _log.warning("Ignoring unknown provider in tier %s: %s", tier, pid)
                continue
            normalized.append({"provider": pid, "model_id": mid})
        if not self._settings:
            raise RuntimeError("settings_service not available")
        await self._settings.save(
            self._tier_settings_key(tier), json.dumps(normalized, ensure_ascii=False),
        )
        await self._invalidate_all_caches()

    async def resolve_model_for_task(
        self,
        tier: str,
        *,
        exclude: list[tuple[str, str]] | None = None,
    ) -> dict | None:
        """为本次请求选择 tier 内第一个未被排除的可用模型。

        返回完整配置（含 api_key），供 adapter 使用。
        若目标 tier 为空，按 fallback 链尝试；全部为空返回 None。
        """
        if tier not in self._VALID_TIERS:
            raise ValueError(f"Unknown tier: {tier}")
        excluded = set(exclude or [])
        order = [tier] + self._DEFAULT_FALLBACK_CHAIN.get(tier, [])
        for current_tier in order:
            bindings = await self.get_tier_config(current_tier)
            for binding in bindings:
                key = (binding["provider"], binding["model_id"])
                if key in excluded:
                    continue
                cfg = await self._resolve_binding(binding)
                if cfg:
                    return cfg
        return None

    async def _resolve_binding(self, binding: dict) -> dict | None:
        """把 provider + model_id 合并成 adapter 可用的完整配置。"""
        pid = binding["provider"]
        mid = binding["model_id"]
        p = self._providers.get(pid)
        if not p:
            return None
        model_info = next((m for m in p.get("models", []) if m.get("id") == mid), None)
        if not model_info:
            return None
        cfg = deepcopy(p)
        cfg["provider"] = pid
        cfg["model"] = mid
        cfg["context"] = model_info.get("context", cfg.get("context", 0))
        cfg["supports"] = model_info.get("supports", cfg.get("supports", []))
        # 模型级定价优先；当前 YAML 中 pricing 按 model_id 分组
        pricing = cfg.get("pricing", {})
        if isinstance(pricing, dict) and mid in pricing:
            cfg["model_pricing"] = pricing[mid]
        # api_key 从加密存储读取；YAML 明文 key 作为 fallback
        api_key = cfg.pop("api_key", "") or ""
        if self._settings:
            decrypted = await self._settings.get_decrypted(f"api_key:{pid}")
            if decrypted:
                api_key = decrypted
        cfg["api_key"] = api_key
        return cfg

    async def list_all_models(self) -> list[dict]:
        """聚合所有厂商 YAML 的 models 字段，返回统一列表。

        每个条目含 provider / model_id / name / context / pricing / thinking /
        supports_vision / recommended_tier。前端据此渲染胶囊。
        """
        from services.model_catalog_query import build_model_entry
        result = []
        for pid, p in self._providers.items():
            pricing = p.get("pricing", {}) if isinstance(p.get("pricing"), dict) else {}
            for m in p.get("models", []):
                result.append(build_model_entry(pid, m, pricing, self._recommend_tier))
        return result

    @classmethod
    def _recommend_tier(cls, model: dict, provider_pricing: dict) -> str:
        """根据模型元数据推荐默认 tier。用户可覆盖。"""
        supports = model.get("supports", [])
        if "vision" in supports:
            return "vision"
        if model.get("thinking") or "thinking" in supports:
            return "max"
        pricing = provider_pricing.get(model.get("id", ""), {}) if isinstance(provider_pricing, dict) else {}
        price = pricing.get("input", 0) + pricing.get("output", 0)
        ctx = model.get("context", 0)
        if price <= 0.5 and ctx <= 131072:
            return "lite"
        if price >= 3.0 or ctx >= 524288:
            return "max"
        return "normal"

    async def best_available_model(self) -> dict | None:
        """MAX 用：挑已配置可用里能力最强的模型——委托到 model_catalog_query。"""
        from services.model_catalog_query import best_available_model

        return await best_available_model(self)

    async def sole_usable_model(self) -> dict | None:
        """只有一个可用模型时返回它，否则 None——委托到 model_catalog_query。"""
        from services.model_catalog_query import sole_usable_model

        return await sole_usable_model(self)

    async def recommend_initial_tiers(self) -> dict[str, list[dict]]:
        """根据系统推荐规则，为每个 tier 生成分配建议。

        用于用户开启分级路由时自动填充。返回 {tier: [{provider, model_id}, ...]}。
        """
        all_models = await self.list_all_models()
        tiers: dict[str, list[dict]] = {t: [] for t in self._VALID_TIERS}
        seen: set[tuple[str, str]] = set()
        for m in all_models:
            key = (m["provider"], m["model_id"])
            if key in seen:
                continue
            seen.add(key)
            tier = m.get("recommended_tier", "normal")
            if tier in tiers:
                tiers[tier].append({"provider": m["provider"], "model_id": m["model_id"]})
        return tiers

    def reload(self) -> None:
        """重新加载所有配置文件"""
        self._load()
        self._invalidate_all_caches_sync()
        _log.info("Model configs reloaded from %s", self._config_dir)

    # ---- 配置文件编辑器（cc-switch 风格） ----

    def list_config_files(self) -> list[dict]:
        """列出所有模型配置文件（供设置页编辑器使用）"""
        files = []
        for f in sorted(self._config_dir.glob("*.yaml")):
            try:
                content = f.read_text(encoding="utf-8")
                files.append({
                    "filename": f.name,
                    "provider": f.stem if not f.name.startswith("_") else None,
                    "content": content,
                    "is_index": f.name.startswith("_"),
                })
            except Exception:
                _log.debug("读取模型配置文件失败", exc_info=True)
        return files

    def get_config_file(self, filename: str) -> dict | None:
        """读取单个配置文件内容"""
        f = self._config_dir / filename
        if not f.exists() or not f.name.endswith(".yaml"):
            return None
        return {"filename": f.name, "content": f.read_text(encoding="utf-8")}

    def save_config_file(self, filename: str, content: str) -> bool:
        """保存配置文件并即时重载"""
        f = self._config_dir / filename
        if not f.name.endswith(".yaml"):
            return False
        f.write_text(content, encoding="utf-8")
        self.reload()
        # reload 内部已调用 _invalidate_all_caches_sync
        return True
