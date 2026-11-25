"""services/provider_key_state.py —— 「该 provider 是否有可用 API Key」的单一判定

2026-10-07：原先 model_config_service 里两处（list_providers 显示、set_active 校验）
各写一版「查 YAML 明文 + 查 settings 有没有那一行」，但**从不解密**。后果是
解不开的孤儿密文（cred.key 被换过）让界面显示 api_key_configured=True、health=active，
而真正调用时 get_active_config 解密失败返回空，Agent 只能报「未配置 API Key」——
界面与实际能力不一致，用户反复重试也不知道错在哪（本项目 2026-10-07 实测复现）。

判定与 get_active_config 必须同一把尺子：能解密才算已配置。get_decrypted 已包含
「历史明文按原值用」与「密文解不开返 None」两种语义，直接复用它即可。

单独成文件的原因：model_config_service.py 已在尺寸基线之上（912 行，静态棘轮禁止
继续变长），新逻辑按仓内既有做法（runtime-bootstrap.js / auth-recovery.js）外置。
"""
from __future__ import annotations


async def provider_has_usable_key(provider: dict, settings, pid: str) -> bool:
    """YAML 里有明文 key，或 settings 里的密文**能解开**，才算已配置。

    Args:
        provider: 该 provider 的 YAML 配置（可能含明文 api_key）
        settings: SettingsService（可为 None）
        pid: provider id，用于拼 "api_key:<pid>"
    """
    if provider.get("api_key"):
        return True
    if settings is None:
        return False
    return bool(await settings.get_decrypted(f"api_key:{pid}"))

