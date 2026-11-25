"""tests/contract/test_model_catalog_merge.py —— 目录同步必须「合并」而非「覆盖」

2026-09-10（多厂商适配审查）：`sync_models_from_catalog()` 原先把目录结果**整体覆盖**
到 `models` + `pricing` 上，会静默抹掉手工维护的元数据：

- `context`：目录常缺，覆盖后 1M 上下文退化成默认 128K；
- `supports`：目录只有 `supports_vision` / `supports_reasoning` 两位启发式，比人工标注粗；
- 目录未收录的既有模型会被整个删掉（用户正在用的模型突然消失、定价也丢）。

与之对照，`refresh_models()` 是刻意保留这些字段的——同一件事两处策略不一致。
本文件锁住「合并」语义，并守住 provider 映射的唯一性。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from services.model_config_service import (  # noqa: E402
    _LITELLM_PROVIDER_MAP,
    merge_catalog_into_provider,
    parse_model_catalog,
)


def _cat(mid, name=None, context=128_000, supports=None, inp=1.0, out=2.0, cached=None):
    return {"id": mid, "name": name or mid, "context": context,
            "supports": supports or ["text", "tools"],
            "input": inp, "output": out, "cached_input": cached}


# ---- 手工元数据优先 ----

def test_manual_metadata_wins_over_catalog():
    existing = [{"id": "m1", "name": "My M1", "context": 1_000_000,
                 "supports": ["text", "tools", "vision", "thinking"]}]
    merged, _ = merge_catalog_into_provider(existing, {}, [_cat("m1", context=128_000)])
    assert merged[0] == {"id": "m1", "name": "My M1", "context": 1_000_000,
                         "supports": ["text", "tools", "vision", "thinking"]}


def test_catalog_pricing_overrides_for_known_id():
    """同步的目的之一就是刷新定价 → 同 id 的价以目录为准。"""
    _, pricing = merge_catalog_into_provider([], {"m1": {"input": 9, "output": 9}}, [_cat("m1", inp=1.5, out=3.0)])
    assert pricing["m1"] == {"input": 1.5, "output": 3.0, "currency": "USD"}


def test_manual_pricing_kept_when_catalog_has_no_price():
    _, pricing = merge_catalog_into_provider([], {"old": {"input": 7, "output": 8}}, [_cat("new")])
    assert pricing["old"] == {"input": 7, "output": 8}
    assert pricing["new"]["input"] == 1.0


# ---- 不丢东西 ----

def test_existing_model_absent_from_catalog_is_kept():
    """目录未收录 ≠ 已下线。删掉会让在用该模型的用户突然失去选项。"""
    existing = [{"id": "legacy", "name": "Legacy", "context": 64000, "supports": ["text"]}]
    merged, _ = merge_catalog_into_provider(existing, {}, [_cat("new1")])
    assert [m["id"] for m in merged] == ["new1", "legacy"]


def test_catalog_only_id_is_added():
    merged, _ = merge_catalog_into_provider([], {}, [_cat("a"), _cat("b")])
    assert [m["id"] for m in merged] == ["a", "b"]


def test_no_duplicate_ids_after_merge():
    existing = [{"id": "a", "name": "A", "context": 1, "supports": ["text"]}]
    merged, _ = merge_catalog_into_provider(existing, {}, [_cat("a"), _cat("b")])
    ids = [m["id"] for m in merged]
    assert len(ids) == len(set(ids))


def test_empty_inputs_are_safe():
    assert merge_catalog_into_provider([], {}, []) == ([], {})
    assert merge_catalog_into_provider(None, None, []) == ([], {})


def test_partial_manual_entry_falls_back_to_catalog():
    """手工条目缺 context 时用目录的补，而不是留空。"""
    existing = [{"id": "a", "name": "Manual"}]
    merged, _ = merge_catalog_into_provider(existing, {}, [_cat("a", context=999_000)])
    assert merged[0]["context"] == 999_000
    assert merged[0]["name"] == "Manual"


# ---- 映射唯一性：一个 litellm key 不得属于两个 provider ----

def test_litellm_keys_are_never_shared_between_providers():
    """共享 key 意味着「同一批模型会被灌进两个 provider」——必然有一边是错的。

    豆包刻意不映射，正是因为它的 volcengine 目录里同时收录了 DeepSeek 等别家模型。
    """
    seen: dict[str, str] = {}
    for provider, keys in _LITELLM_PROVIDER_MAP.items():
        for k in keys:
            assert k not in seen, f"key {k!r} 同时属于 {seen[k]} 与 {provider}"
            seen[k] = provider


def test_every_mapped_provider_is_a_real_provider():
    from services.model_config_service import ModelConfigService
    import glob

    names = {os.path.basename(f)[:-5]
             for f in glob.glob(os.path.join(os.path.dirname(__file__), "..", "..",
                                             "config", "models", "*.yaml"))}
    names.discard("_index")
    unknown = set(_LITELLM_PROVIDER_MAP) - names
    assert unknown == set(), f"映射了不存在的 provider: {unknown}"


# ---- parse_model_catalog 仍按 id 去前缀 ----

def test_parse_strips_vendor_prefix():
    data = {
        "dashscope/qwen3-max": {
            "litellm_provider": "dashscope", "mode": "chat",
            "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6,
            "max_input_tokens": 262144, "supports_reasoning": True,
        }
    }
    out = parse_model_catalog(data, {"dashscope"})
    assert out[0]["id"] == "qwen3-max"
    assert out[0]["input"] == 1.0 and out[0]["output"] == 2.0
    assert "thinking" in out[0]["supports"]


def test_parse_skips_non_chat_and_priceless():
    data = {
        "openai/text-embedding-3-large": {"litellm_provider": "openai", "mode": "embedding",
                                          "input_cost_per_token": 1e-7, "output_cost_per_token": 0},
        "openai/gpt-x": {"litellm_provider": "openai", "mode": "chat"},  # 无定价
        "other/thing": {"litellm_provider": "other", "mode": "chat",
                        "input_cost_per_token": 1e-6, "output_cost_per_token": 1e-6},
    }
    assert parse_model_catalog(data, {"openai"}) == []


def test_catalog_refresh_keeps_manual_cached_input():
    """目录不返回 cached_input 时，整条替换会抹掉用户手填的缓存价。"""
    existing_pricing = {"m1": {"input": 9, "output": 9, "currency": "USD", "cached_input": 0.5}}
    _, pricing = merge_catalog_into_provider([], existing_pricing, [_cat("m1", inp=1.0, out=2.0)])
    assert pricing["m1"]["cached_input"] == 0.5      # 保留
    assert pricing["m1"]["input"] == 1.0            # 刷新


def test_catalog_cached_input_wins_when_present():
    _, pricing = merge_catalog_into_provider([], {"m1": {"input": 9, "cached_input": 9}},
                                             [_cat("m1", cached=0.02)])
    assert pricing["m1"]["cached_input"] == 0.02
