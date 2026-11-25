"""services/agent/model_selection.py —— 请求级模型选择（从 agent_service 拆出）

2026-10-08 拆分的两个原因：
  1. agent_service.py 已在尺寸基线之上（静态棘轮禁止继续变长）；
  2. 选择逻辑本身有明确的先后规则，独立成文件后能单测、也便于对照。

选择顺序（自上而下，先命中先返回）：
  1. **只有一个可用模型** → 所有模式都用它。
     分级/MAX 的前提是"有多个模型可选"；只有一个还去挑 tier 只会不断回退，
     用户看到的是"我开了 MAX 却没换模型"。
  2. **MAX** → 挑已配置可用里能力最强的那个（按模型自述 power/thinking/context 排序）。
     MAX 不只是把提示词写长，而是真的换更强的模型。
  3. **未开分级路由** → active。
  4. **开了分级路由** → 按 mode / model_tier / step_role / 复杂度选 tier。

所有分支都只依赖 ModelConfigService 的公开方法；本模块不碰 DB、不读环境变量。
"""
from __future__ import annotations

import logging

_log = logging.getLogger("yuncat.agent")

_VALID_TIERS = {"lite", "normal", "max", "vision"}


async def _sole_or_none(model_config) -> dict | None:
    """只有一个可用模型时返回它（fail-open：查询异常按"不是唯一"处理）。"""
    try:
        return await model_config.sole_usable_model()
    except Exception:
        _log.debug("sole_usable_model failed; fall back to tier routing", exc_info=True)
        return None


async def _best_or_none(model_config) -> dict | None:
    """MAX 用：挑能力最强的可用模型（fail-open：查询异常回退到后续分支）。"""
    try:
        return await model_config.best_available_model()
    except Exception:
        _log.debug("best_available_model failed; use active", exc_info=True)
        return None


async def _resolve_tier(model_config, *, mode, prompt, model_tier, max_mode_enabled,
                        long_task_step_role, conversation_context, capability_level) -> str:
    """按 mode / 显式 tier / step_role / 复杂度决定 tier 名。"""
    from services.model_router import route_model_tier, _resolve_step_tier_from_role

    if max_mode_enabled:
        return "max"
    if model_tier and model_tier in _VALID_TIERS:
        return model_tier
    if long_task_step_role:
        return _resolve_step_tier_from_role(long_task_step_role)
    if mode == "ask":
        return "lite"
    tier = await route_model_tier(
        prompt, conversation_context=conversation_context, capability_level=capability_level,
    )
    # 轻量档(L0): 非视觉任务一律压到 lite（用户主动选"轻量"，要更强请选 L2 强力）
    if capability_level == 0 and tier != "vision":
        tier = "lite"
    return tier


async def resolve_model_for_request(
    model_config,
    mode: str,
    prompt: str,
    *,
    max_mode_enabled: bool = False,
    long_task_step_role: str = "",
    model_tier: str = "",
    conversation_context: dict | None = None,
    capability_level: int = 1,
) -> dict:
    """请求级模型选择入口（规则见模块 docstring）。"""
    if not model_config:
        return {}

    sole = await _sole_or_none(model_config)
    if sole:
        return sole

    if max_mode_enabled:
        best = await _best_or_none(model_config)
        if best:
            return best

    if not await model_config.is_tiering_enabled():
        return await model_config.get_active_config()

    tier = await _resolve_tier(
        model_config, mode=mode, prompt=prompt, model_tier=model_tier,
        max_mode_enabled=max_mode_enabled, long_task_step_role=long_task_step_role,
        conversation_context=conversation_context, capability_level=capability_level,
    )
    cfg = await model_config.resolve_model_for_task(tier)
    if cfg:
        return cfg
    _log.warning("Tier '%s' has no models, falling back to active config", tier)
    return await model_config.get_active_config()
