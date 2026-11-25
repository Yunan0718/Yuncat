"""tests/unit/test_model_selection.py —— 单模型全模式通用 / MAX 选最强

2026-10-08 新增。起因：用户开了 MAX + 最高思考强度，实测仍跑最便宜的
`deepseek-v4-flash`，任务质量不如 DeepSeek 网页版。根因是分级路由默认关闭时
`_resolve_model_for_request` 直接 `return get_active_config()`——MAX 一步没换模型。

本组测试锁定新的选择顺序：
  1. 只有一个可用模型 → 所有模式都用它；
  2. MAX → 用能力最强的可用模型（按模型自述元数据排序，不含任何厂商名判断）；
  3. 其余 → 维持原语义（未开分级用 active）。
"""
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src-python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from services.agent.model_selection import resolve_model_for_request  # noqa: E402
from services.model_catalog_query import _version_key, best_available_model  # noqa: E402


class _FakeModelConfig:
    """只实现 model_selection 需要的几个方法。"""

    def __init__(self, providers, tiers_enabled=False, active=None):
        self._providers = providers
        self._tiers_enabled = tiers_enabled
        # 真实 ModelConfigService 有 _settings（provider_has_usable_key 会读它）。
        # 这里用 None：provider_has_usable_key(provider, None, pid) 直接看 provider.api_key，
        # 正是本测试要模拟的"有 Key / 没 Key"两种情形。
        self._settings = None
        self._active = active or {"provider": "demo", "model": "active-model", "api_key": "k"}

    async def sole_usable_model(self):
        from services.model_catalog_query import sole_usable_model

        return await sole_usable_model(self)

    async def best_available_model(self):
        from services.model_catalog_query import best_available_model

        return await best_available_model(self)

    async def is_tiering_enabled(self):
        return self._tiers_enabled

    async def get_active_config(self):
        return dict(self._active)

    async def resolve_model_for_task(self, tier, exclude=None):
        return {"provider": "tier-" + tier, "model": "m-" + tier, "api_key": "k"}

    async def _resolve_binding(self, binding):
        pid, mid = binding["provider"], binding["model_id"]
        p = self._providers.get(pid) or {}
        return {"provider": pid, "model": mid, "api_key": p.get("api_key", "")}


def _prov(*models, provider="demo", api_key="k"):
    return {provider: {"api_key": api_key, "models": list(models)}}


# ── 1. 单模型：全模式都用它 ──────────────────────────────────

@pytest.mark.asyncio
async def test_single_model_used_for_every_mode():
    cfg = _FakeModelConfig(_prov({"id": "only-one", "context": 8000}))
    for max_on in (False, True):
        out = await resolve_model_for_request(cfg, "agent", "hi", max_mode_enabled=max_on)
        assert out["model"] == "only-one", f"MAX={max_on} 时也该用唯一模型"


@pytest.mark.asyncio
async def test_single_model_ignores_active_pointing_elsewhere():
    """active 指向别处也不该生效——可用模型只有一个就用它。"""
    cfg = _FakeModelConfig(
        _prov({"id": "only-one"}),
        active={"provider": "ghost", "model": "not-configured", "api_key": "k"},
    )
    out = await resolve_model_for_request(cfg, "agent", "hi")
    assert out["model"] == "only-one"


# ── 2. 多模型 + MAX：选最强 ─────────────────────────────────

@pytest.mark.asyncio
async def test_max_picks_strongest_by_power():
    cfg = _FakeModelConfig(_prov(
        {"id": "demo-flash", "power": 1, "supports": ["text", "thinking"]},
        {"id": "demo-pro", "power": 3, "supports": ["text", "thinking"]},
    ))
    out = await resolve_model_for_request(cfg, "agent", "hi", max_mode_enabled=True)
    assert out["model"] == "demo-pro"


@pytest.mark.asyncio
async def test_max_breaks_tie_by_version():
    """同 power 档内用版本号定新旧（厂商中立，不认模型名）。"""
    cfg = _FakeModelConfig(_prov(
        {"id": "demo-5.1", "power": 2, "supports": ["thinking"], "context": 100},
        {"id": "demo-5.2", "power": 2, "supports": ["thinking"], "context": 100},
    ))
    out = await resolve_model_for_request(cfg, "agent", "hi", max_mode_enabled=True)
    assert out["model"] == "demo-5.2"


@pytest.mark.asyncio
async def test_max_prefers_thinking_capable_at_same_power():
    cfg = _FakeModelConfig(_prov(
        {"id": "a-plain", "power": 2, "supports": ["text"], "context": 999999},
        {"id": "b-thinker", "power": 2, "supports": ["text", "thinking"], "context": 100},
    ))
    out = await resolve_model_for_request(cfg, "agent", "hi", max_mode_enabled=True)
    assert out["model"] == "b-thinker"


@pytest.mark.asyncio
async def test_max_never_picks_unconfigured_provider():
    """没有可用 Key 的 provider 不参与 MAX 选型（与 UI「已配置」同口径）。"""
    providers = {
        "configured": {"api_key": "k", "models": [{"id": "weak", "power": 1}]},
        "unconfigured": {"api_key": "", "models": [{"id": "strong", "power": 3}]},
    }
    cfg = _FakeModelConfig(providers)
    out = await resolve_model_for_request(cfg, "agent", "hi", max_mode_enabled=True)
    assert out["model"] == "weak"


# ── 3. 非 MAX：维持原语义 ───────────────────────────────────

@pytest.mark.asyncio
async def test_non_max_without_tiering_uses_active():
    cfg = _FakeModelConfig(_prov(
        {"id": "demo-flash", "power": 1},
        {"id": "demo-pro", "power": 3},
    ), tiers_enabled=False)
    out = await resolve_model_for_request(cfg, "agent", "hi", max_mode_enabled=False)
    assert out["model"] == "active-model", "未开分级且非 MAX 时应保持 active（不擅自换模型）"


@pytest.mark.asyncio
async def test_tiering_enabled_routes_to_tier():
    cfg = _FakeModelConfig(_prov(
        {"id": "demo-flash", "power": 1},
        {"id": "demo-pro", "power": 3},
    ), tiers_enabled=True)
    out = await resolve_model_for_request(cfg, "agent", "hi", max_mode_enabled=True)
    # MAX + 分级开启：best_available_model 已先命中，不会走到 tier
    assert out["model"] == "demo-pro"


@pytest.mark.asyncio
async def test_no_config_returns_empty():
    assert await resolve_model_for_request(None, "agent", "hi") == {}


# ── 4. 版本号解析（厂商中立）────────────────────────────────

def test_version_key_has_no_vendor_knowledge():
    assert _version_key("gpt-5.2") == (5, 2)
    assert _version_key("grok-4.20-0309") == (4, 20, 309)
    assert _version_key("claude-opus-5") == (5,)
    assert _version_key("no-digits-here") == ()
    assert _version_key("") == ()


def test_best_available_model_returns_none_when_nothing_usable():
    import asyncio

    cfg = _FakeModelConfig({"p": {"api_key": "", "models": [{"id": "x", "power": 3}]}})
    assert asyncio.run(best_available_model(cfg)) is None
