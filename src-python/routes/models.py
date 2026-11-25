"""routes/models.py —— 模型配置（models.yaml SSOT）+ 连接测试 + 文件读写"""

import logging
import os
import tempfile

from fastapi import APIRouter, HTTPException, Request

from config import MAX_TOKENS_MODEL_VALIDATION, ADAPTER_HEALTH_CHECK_TIMEOUT
from services.provider_protocol import (
    auth_headers,
    health_url,
    models_url,
    parse_model_list,
    requires_api_key,
    resolve_protocol,
)

router = APIRouter(prefix="/api/v1", tags=["models"])

_log = logging.getLogger("yuncat.models")

# Batch 4b: /models/test 的 base_url 校验——轻量结构校验（不再硬编码域名白名单）。
# 信任边界说明：测试目标 URL 来自用户本地的模型配置（对标 Cherry Studio / LobeChat
# 支持任意 base_url），one-api/new-api 中转与自建网关是国内常见用法，白名单会直接 403。
# 该输入并非不可信来源，SSRF 风险极低；若未来出现 LLM 可控的测试目标（如从网页/
# 对话内容提取 URL），需在此接回 security.url_guard 校验。
def _validate_test_base_url(base_url: str) -> None:
    """校验 base_url 可解析 + scheme 为 http/https + 有 host，否则 400。"""
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(400, f"Invalid base_url: {base_url!r}")


@router.get("/models")
async def list_providers(request: Request):
    """列出所有预置供应商（已填 API Key 的标记 configured）"""
    return await request.app.state.model_config.list_providers()


@router.get("/models/reasoning")
async def model_reasoning_support(request: Request, model: str = "", provider: str = "") -> dict:
    """返回指定模型的思考档位支持矩阵（自描述）——供前端按模型渲染思考强度下拉。

    2026-09-10（对标 deepseek-harness 的 per-model reasoning 描述）：
    前端不再固定展示 5 档后依赖后端静默 clamp，而是只启用模型真实支持的档位。

    2026-09-10 二次修订：家族判定改为 **provider 优先**（provider 比模型名权威），
    并读取 provider 配置里的可选 `reasoning:` 声明块——厂商可自行声明字段名，
    代码不写死任何单一厂商。未传 provider 时取当前激活 provider。
    """
    from services.reasoning import describe_model_reasoning, reasoning_spec

    spec: dict = {}
    try:
        mc = getattr(request.app.state, "model_config", None)
        if mc is not None:
            provider = provider or mc.get_active_provider()
            cfg = await mc.get_provider_config(provider) or {}
            spec = reasoning_spec(cfg)
            model = model or cfg.get("model", "")
    except Exception:  # 配置不可用时退化为纯模型名嗅探，不因描述接口失败影响前端
        pass
    return describe_model_reasoning(model, provider, spec)


@router.get("/models/active")
async def get_active_config(request: Request):
    """获取当前激活的模型配置——H1 修复：api_key 脱敏返回（对标 Stripe Dashboard）"""
    cfg = await request.app.state.model_config.get_active_config()
    # H1 修复：脱敏 api_key，防止 XSS/渲染进程被攻破时泄露 LLM 提供商密钥
    # 后端 adapter 仍通过 get_active_config() 拿完整 key，此处仅渲染进程边界脱敏
    api_key = cfg.get("api_key", "")
    cfg["api_key"] = _mask_api_key(api_key)
    cfg["api_key_configured"] = bool(api_key)
    cfg.update(await _credential_diagnostics(
        getattr(request.app.state, "settings_service", None),
        str(cfg.get("provider") or ""),
    ))
    return cfg


async def _credential_diagnostics(settings, provider: str) -> dict:
    """凭据可用性诊断——区分「从没填过」与「填过但本机密钥失效」。

    2026-10-21 实测：cred.key 丢失或被替换后，库里的密文永久解不开，
    而界面只显示「未配置 API Key」，用户以为是自己没填，反复重试。
    这里把事实报给前端，让它能直接给出可执行的下一步。
    """
    from security.credentials import FERNET_TOKEN_PREFIX, credential_key_state

    state = credential_key_state()
    has_ciphertext = False
    if settings is not None and provider:
        try:
            raw = await settings.get_raw(f"api_key:{provider}")
        except Exception:
            _log.warning("读取 %s 原始值失败，无法判断是否存在密文", provider, exc_info=True)
            raw = None
        has_ciphertext = bool(raw) and str(raw).startswith(FERNET_TOKEN_PREFIX)
    return {"credential_state": state, "has_stored_ciphertext": has_ciphertext}


def _mask_api_key(key: str) -> str:
    """脱敏 API Key——对标 Stripe Dashboard 格式：prefix***last4"""
    if not key or len(key) <= 8:
        return "***" if key else ""
    return f"{key[:6]}***{key[-4:]}"


@router.put("/models/{provider}")
async def save_provider_config(provider: str, payload: dict, request: Request):
    """保存供应商的 api_key、model 和 pricing"""
    api_key = payload.get("api_key")
    model = payload.get("model")
    pricing = payload.get("pricing")
    try:
        if api_key:
            await request.app.state.model_config.save_api_key(provider, api_key)
        if model:
            await request.app.state.model_config.set_model(provider, model)
        if pricing is not None:
            await request.app.state.model_config.set_pricing(provider, pricing)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/models/{provider}/activate")
async def activate_provider(provider: str, request: Request):
    """切换当前激活的供应商 — Phase 43 P1.F: 返回模型详细信息"""
    try:
        await request.app.state.model_config.set_active(provider)
        cfg = await request.app.state.model_config.get_provider_config(provider)
        if cfg:
            models_list = cfg.get("models", [])
            current_model = cfg.get("model", "")
            model_info = next((m for m in models_list if m.get("id") == current_model), {})
            return {
                "status": "ok",
                "active": provider,
                "provider_name": cfg.get("name", provider),
                "model": current_model,
                "model_name": model_info.get("name", current_model),
                "context_window": model_info.get("context", 131072),
                "supports": model_info.get("supports", []),
            }
        return {"status": "ok", "active": provider}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/models/reload")
async def reload_models(request: Request):
    """重新加载 models.yaml（手动编辑文件后调用）"""
    request.app.state.model_config.reload()
    return {"status": "ok"}


# Phase 43 M.2: per-agent 模型配置端点
@router.get("/models/agent/{agent_id}")
async def get_agent_provider(agent_id: str, request: Request):
    """获取指定 Agent 的独立 Provider 配置。"""
    mc = request.app.state.model_config
    provider = mc.get_agent_provider(agent_id)
    if not provider:
        provider = mc.get_active_provider()
    cfg = await mc.get_provider_config(provider)
    return {
        "agent_id": agent_id,
        "provider": provider,
        "provider_name": cfg.get("name", provider) if cfg else provider,
        "model": cfg.get("model", "") if cfg else "",
        "models": cfg.get("models", []) if cfg else [],
        "is_custom": mc.get_agent_provider(agent_id) is not None,
    }


@router.post("/models/agent/{agent_id}")
async def set_agent_provider(agent_id: str, payload: dict, request: Request):
    """为指定 Agent 设置独立 Provider。payload: {"provider": "deepseek"}"""
    provider = payload.get("provider")
    if not provider:
        raise HTTPException(400, "Missing 'provider'")
    try:
        request.app.state.model_config.set_agent_provider(agent_id, provider)
        return {"status": "ok", "agent_id": agent_id, "provider": provider}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/models/agent/{agent_id}")
async def reset_agent_provider(agent_id: str, request: Request):
    """重置 Agent 的 Provider 到全局默认。"""
    mc = request.app.state.model_config
    if agent_id in mc._agent_providers:
        del mc._agent_providers[agent_id]
    return {"status": "ok", "agent_id": agent_id}


@router.post("/models/test")
async def test_model_connection(payload: dict, request: Request):
    """测试供应商连接——从后端发起请求，绕过 CORS。

    优先使用 payload 中的 api_key；若未提供则从已保存配置中解密获取。
    端点/鉴权/解析全部来自 provider 的 ``protocol:`` 声明（见 services/provider_protocol.py），
    **不在这里判断任何具体厂商名**——新增厂商只需写 YAML。
    """
    base_url = (payload.get("base_url") or "").strip()
    provider = payload.get("provider", "")
    api_key = payload.get("api_key", "")

    saved: dict = {}
    if provider:
        saved = dict(await request.app.state.model_config.get_provider_config(provider) or {})
    if base_url:
        # 前端传了 base_url（用户正在编辑）→ 以它为准，且**不带入已保存的 models_url**：
        # 否则用户改了域名点「测试」，请求会打到旧域名的 models_url 上。
        cfg = {"base_url": base_url,
               "models_url": payload.get("models_url", ""),
               "model": payload.get("model", "")}
    else:
        cfg = saved
    if not cfg.get("base_url"):
        raise HTTPException(400, "Missing 'base_url'")
    # 协议声明（鉴权方案 / 列表解析）沿用已保存的：用户只是在改地址，不等于换厂商。
    declared = saved.get("protocol") or payload.get("protocol")
    protocol = resolve_protocol({"protocol": declared} if declared else {})
    # Batch 4b: 轻量结构校验（scheme/host）——自定义网关/中转域名放行，详见 _validate_test_base_url 注释
    _validate_test_base_url(cfg["base_url"])

    if not api_key:
        api_key = saved.get("api_key", "")
    if not api_key and requires_api_key(protocol):
        return {"status": 401, "ok": False, "error": "未配置 API Key"}

    import aiohttp

    test_url = health_url(cfg, protocol)
    headers = auth_headers(protocol, api_key)
    method = str((protocol.get("health") or {}).get("method") or "GET").upper()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=ADAPTER_HEALTH_CHECK_TIMEOUT),
        ) as session:
            if method == "POST":
                req = session.post(test_url, headers=headers, json={
                    "model": cfg.get("model", ""),
                    "max_tokens": MAX_TOKENS_MODEL_VALIDATION,
                    "messages": [{"role": "user", "content": "hi"}],
                })
            else:
                req = session.get(test_url, headers=headers)
            async with req as resp:
                ok = resp.ok
                if provider:
                    request.app.state.model_config.set_health_status(
                        provider, "configured" if ok else "failed")
                return {"status": resp.status, "ok": ok, "endpoint": test_url}
    except Exception as e:
        if provider:
            request.app.state.model_config.set_health_status(provider, "failed")
        return {"status": 0, "ok": False, "endpoint": test_url, "error": str(e)}


# ════════════════════════════════════════════════════════════
# Phase 108: 模型分级路由（Tiering）配置端点
# ════════════════════════════════════════════════════════════

@router.get("/models/all")
async def list_all_models(request: Request):
    """聚合所有厂商的可用模型，供分级面板渲染胶囊。"""
    return await request.app.state.model_config.list_all_models()


@router.get("/models/tiers")
async def get_tier_config(request: Request):
    """获取分级路由开关状态 + 4 个 tier 的模型绑定。"""
    mc = request.app.state.model_config
    enabled = await mc.is_tiering_enabled()
    tiers = {}
    for tier in mc._VALID_TIERS:
        tiers[tier] = await mc.get_tier_config(tier)
    return {"enabled": enabled, "tiers": tiers}


@router.post("/models/tiering")
async def set_tiering_enabled(payload: dict, request: Request):
    """开启/关闭分级路由。开启时 tier 保持为空，由用户自行配置（可通过前端的"恢复推荐"按钮主动填充）。"""
    enabled = bool(payload.get("enabled", False))
    mc = request.app.state.model_config
    await mc.set_tiering_enabled(enabled)
    return {"enabled": enabled}


@router.put("/models/tiers")
async def set_tier_binding(payload: dict, request: Request):
    """保存某个 tier 的模型绑定顺序。payload: {"tier": "lite", "bindings": [...]}"""
    tier = payload.get("tier", "")
    bindings = payload.get("bindings", [])
    if not isinstance(bindings, list):
        raise HTTPException(400, "bindings must be a list")
    try:
        await request.app.state.model_config.set_tier_binding(
            tier,
            [(b.get("provider"), b.get("model_id")) for b in bindings],
        )
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/models/tiers/recommend")
async def recommend_tiers(request: Request):
    """返回系统推荐的 tier 分配（用户覆盖恢复用）。"""
    return await request.app.state.model_config.recommend_initial_tiers()


@router.post("/models/{provider}/refresh")
async def refresh_models(provider: str, request: Request):
    """从供应商 API 动态拉取模型列表（cc-switch 风格 modelsUrl）。
    覆盖 YAML 中的 models 字段并持久化。
    """
    import aiohttp, yaml

    cfg = await request.app.state.model_config.get_provider_config(provider)
    if not cfg:
        raise HTTPException(404, f"Provider {provider} not found")

    api_key = cfg.get("api_key", "")
    protocol = resolve_protocol(cfg)
    if not api_key and requires_api_key(protocol):
        raise HTTPException(400, "API Key not configured")

    models_endpoint = models_url(cfg, protocol)

    from config import MODEL_REFRESH_TIMEOUT_SECONDS, MODEL_REFRESH_MAX_MODELS

    headers = auth_headers(protocol, api_key)

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=MODEL_REFRESH_TIMEOUT_SECONDS)) as session:
            async with session.get(models_endpoint, headers=headers) as resp:
                if resp.status != 200:
                    raise HTTPException(400, f"API returned {resp.status}")
                data = await resp.json()

        # 解析格式由 provider 的 protocol.models 声明决定（默认 OpenAI 兼容 data[].id）
        raw = parse_model_list(data, protocol)

        # 写入 YAML
        # 保留现有 YAML 中已存在模型的精确 context/supports/name——厂商 /models 端点
        # 通常不返回这些元数据（DeepSeek/OpenAI 只返回 id），fallback 推断不准确，
        # 会覆盖用户手工维护的精确值（如 1M context、thinking 标记）。
        existing_models = {m.get("id"): m for m in (cfg.get("models") or []) if isinstance(m, dict)}
        models_list = []
        for m in raw[:MODEL_REFRESH_MAX_MODELS]:  # 最多 MODEL_REFRESH_MAX_MODELS 个
            mid = m["id"]
            existing = existing_models.get(mid)
            if existing and existing.get("context"):
                ctx = existing["context"]
                supports = existing.get("supports") or ["text", "tools"]
                name = existing.get("name") or m["name"]
            else:
                # 推断上下文长度（从模型名或 API 返回的 context_window 字段）
                from config import MODEL_REFRESH_LARGE_CONTEXT_FALLBACK, MAX_CONTEXT_FALLBACK_CHARS
                ctx = m.get("context_length") or m.get("context_window") or m.get("max_tokens") or (MODEL_REFRESH_LARGE_CONTEXT_FALLBACK if "pro" in mid.lower() else MAX_CONTEXT_FALLBACK_CHARS)
                supports = ["text", "tools"]
                if any(kw in mid.lower() for kw in ["vision", "vl", "omni", "video"]):
                    supports.append("vision")
                if any(kw in mid.lower() for kw in ["reason", "think", "r1", "o1", "o3"]):
                    supports.append("thinking")
                name = m["name"]
            models_list.append({"id": mid, "name": name, "context": ctx, "supports": supports})

        cfg["models"] = models_list
        request.app.state.model_config._providers[provider]["models"] = models_list
        request.app.state.model_config._save_provider(provider)

        return {"ok": True, "count": len(models_list), "models": models_list}
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/models/{provider}/sync-catalog")
async def sync_models_from_catalog(provider: str, request: Request):
    """从 GitHub 模型目录（LiteLLM）手动拉取并覆盖该 provider 的模型列表 + 定价。

    手动触发——网络请求是显式副作用，仅在用户点击时发生。失败不破坏现有配置。
    """
    result = await request.app.state.model_config.sync_models_from_catalog(provider)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "sync failed")
    return result


@router.get("/models/file")
async def read_models_yaml(request: Request, provider: str = ""):
    """返回指定供应商的 YAML 配置原内容。

    不带 provider → 返回 _index.yaml（激活列表）
    带 provider → 返回 config/models/{provider}.yaml（完整模型列表）
    """
    svc = request.app.state.model_config
    if provider and provider in svc._providers:
        path = svc._get_provider_path(provider)
    else:
        path = svc._config_dir / "_index.yaml" if not provider else svc._get_index_path()
    if not os.path.exists(path):
        raise HTTPException(404, f"Config not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    return {"content": content, "path": str(path), "provider": provider}


@router.put("/models/file")
async def write_models_yaml(payload: dict, request: Request):
    """覆写供应商配置文件（前端编辑器保存时调用）——原子写入 + 备份"""
    content = payload.get("content")
    provider = payload.get("provider", "")
    if content is None:
        raise HTTPException(400, "Missing 'content' in payload")

    svc = request.app.state.model_config
    if provider and provider in svc._providers:
        path = svc._get_provider_path(provider)
    else:
        path = svc._config_dir / "_index.yaml"

    bak = str(path) + ".bak"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            old = f.read()
        with open(bak, "w", encoding="utf-8") as f:
            f.write(old)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise
    svc.reload()
    return {"status": "ok", "backup": bak}


# ════════════════════════════════════════════════════════════
# 模型配置文件编辑器（cc-switch 风格，每个模型一个 YAML）
# ════════════════════════════════════════════════════════════

@router.get("/models/config/files")
async def list_model_config_files(request: Request):
    """列出所有模型配置文件"""
    svc = request.app.state.model_config
    return svc.list_config_files()


@router.get("/models/config/files/{filename:path}")
async def get_model_config_file(filename: str, request: Request):
    """读取单个模型配置文件内容"""
    svc = request.app.state.model_config
    data = svc.get_config_file(filename)
    if not data:
        raise HTTPException(404, f"Config file '{filename}' not found")
    return data


@router.put("/models/config/files/{filename:path}")
async def save_model_config_file(filename: str, payload: dict, request: Request):
    """保存模型配置文件（即时生效）"""
    content = payload.get("content")
    if content is None:
        raise HTTPException(400, "Missing 'content'")
    svc = request.app.state.model_config
    ok = svc.save_config_file(filename, content)
    if not ok:
        raise HTTPException(400, f"Cannot save '{filename}'")
    return {"status": "ok", "filename": filename}
