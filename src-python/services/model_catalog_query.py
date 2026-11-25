"""services/model_catalog_query.py —— 模型目录的可用性/能力查询（从 model_config_service 拆出）

2026-10-08 拆分原因：model_config_service.py 已在尺寸基线之上（静态棘轮禁止继续变长），
新逻辑按仓内既有做法外置成模块。这些函数只依赖传入的 model_config 对象
（读 `_providers` / `_settings` 与 `_resolve_binding`），不碰 DB、不读环境变量。

「可用」的判定与 UI / active 配置同一把尺子：**能解密出 Key 才算**
（见 services/provider_key_state.py）——否则会出现"界面说配好了、这里当没有"的分歧。
"""
from __future__ import annotations

import logging

_log = logging.getLogger("yuncat.model_config")


async def _provider_usable(model_config, pid: str, provider: dict) -> bool:
    """该 provider 是否有可用 Key——与 provider_key_state 同口径（能解密才算）。"""
    from services.provider_key_state import provider_has_usable_key

    try:
        return await provider_has_usable_key(provider, model_config._settings, pid)
    except Exception:
        _log.debug("provider_has_usable_key failed for %s", pid, exc_info=True)
        return False


def _model_power(model: dict) -> int:
    """模型自述能力档（`power`）。缺省 0——不影响可用性，只是排序靠后。"""
    try:
        return int(model.get("power") or 0)
    except (TypeError, ValueError):
        return 0


_VERSION_RE = None


def _version_key(model_id: str) -> tuple[int, ...]:
    """从模型 id 里抽出数字版本号，用于同一 power 档内比较新旧（厂商中立）。

    为什么需要：同一档位下仍有代际强弱（gpt-5.2 vs gpt-5.1、grok-4.6 vs grok-4.5），
    单靠 power 分不出来。这里只取纯粹的数字序列，不含任何厂商/模型名判断：
    `gpt-5.2` → (5, 2)，`grok-4.20-0309` → (4, 20, 309)，`claude-opus-5` → (5,)。
    日期型后缀（如 -20251101）数值很大，会让"更晚的日期版"排前——这符合直觉。
    """
    global _VERSION_RE
    if _VERSION_RE is None:
        import re as _re

        _VERSION_RE = _re.compile(r"\d+")
    return tuple(int(x) for x in _VERSION_RE.findall(model_id or ""))


async def best_available_model(model_config) -> dict | None:
    """在**已配置可用**的模型里挑能力最强的一个（厂商中立）。

    2026-10-08：MAX 模式此前只放大提示词与上限，模型一步不换——用户开了 MAX
    却仍跑最便宜的那个模型，体感就是「MAX 没用」。

    排序只取**模型自身声明的元数据**，与厂商名/厂商专有命名无关，也不做金额计算：
      1. `power`：模型目录里声明的能力档（可选整数，越大越强）。厂商按自己的
         产品线填写（pro=3 / standard=2 / flash=1 / mini=0）；未声明按 0，不淘汰。
      2. 声明支持思考（supports 含 thinking / thinking: true）优先。
      3. 上下文更大优先。
      4. 仍相同按 (provider, model_id) 稳定排序，保证结果可复现。
    没有可用模型时返回 None，由调用方回退到 active。
    """
    candidates: list[tuple[tuple, str, str]] = []
    for pid, p in model_config._providers.items():
        if not await _provider_usable(model_config, pid, p):
            continue
        for m in (p.get("models") or []):
            mid = m.get("id")
            if not mid:
                continue
            supports = m.get("supports") or []
            power = _model_power(m)
            thinking = 1 if (m.get("thinking") or "thinking" in supports) else 0
            ctx = int(m.get("context") or 0)
            ver = _version_key(mid)
            candidates.append(((power, thinking, ctx, ver, pid, mid), pid, mid))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    _, pid, mid = candidates[0]
    return await model_config._resolve_binding({"provider": pid, "model_id": mid})


async def sole_usable_model(model_config) -> dict | None:
    """当且仅当「只有一个可用模型」时返回它，否则返回 None。

    用途：只有一个可用模型时全模式都用它——分级路由在多档之间挑来挑去没有意义，
    反而会因为 tier 未配置而回退，让用户以为 MAX 没生效。
    """
    found: list[tuple[str, str]] = []
    for pid, p in model_config._providers.items():
        if not await _provider_usable(model_config, pid, p):
            continue
        for m in (p.get("models") or []):
            mid = m.get("id")
            if mid:
                found.append((pid, mid))
                if len(found) > 1:
                    return None
    if len(found) != 1:
        return None
    pid, mid = found[0]
    return await model_config._resolve_binding({"provider": pid, "model_id": mid})


def build_model_entry(provider_id: str, model: dict, pricing: dict, recommend_tier) -> dict:
    """把某个模型条目统一成前端/目录使用的形状（含 recommended_tier 与 power）。

    抽到本模块的原因：model_config_service 已在尺寸基线之上，
    且这几个字段的语义（能力档、推荐档）与「目录查询」是同一族关注点。
    """
    mid = model.get("id", "")
    pricing_for_model = pricing.get(mid, {}) if isinstance(pricing, dict) else {}
    supports = model.get("supports", [])
    return {
        "provider": provider_id,
        "model_id": mid,
        "name": model.get("name", mid),
        "context": model.get("context", 0),
        "supports": supports,
        "supports_vision": "vision" in supports,
        "thinking": bool(model.get("thinking") or "thinking" in supports),
        "pricing": pricing_for_model,
        "recommended_tier": recommend_tier(model, pricing),
        # 能力档（模型自述，厂商中立）：供 MAX 选型与前端展示
        "power": _model_power(model),
    }
