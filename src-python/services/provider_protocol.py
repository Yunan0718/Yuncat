"""services/provider_protocol.py —— 厂商协议声明（把「写死厂商名」收敛为数据）

背景（2026-09-10 多厂商适配审查）：
模型列表探测 / 连接测试 / 鉴权头的写法原本用 `provider == "anthropic"`、
`provider == "ollama"` 这类硬编码分支散落在 routes/models.py 里。同一份知识被复制到
多处后立刻不一致，实测踩出三个真 bug：

1. Anthropic 连接测试把 base_url(``…/v1``) 与 ``/v1/messages`` 直接相接 →
   ``…/v1/v1/messages``，**必然 404**；
2. Ollama 连接测试拼出 ``…/v1/api/tags``（Ollama 的 tags 在 origin 下，不经 /v1）→ 404；
3. Ollama 声明 ``auth: none``（本地无 key），但连接测试在协议判断之前就因「未配置 API Key」
   直接返回 401，**本地模型永远测不通**。

修复思路：**知识进 YAML，代码只做通用解析**。
每个 provider 可在 ``config/models/<id>.yaml`` 里写可选的 ``protocol:`` 块；
未声明的走 OpenAI 兼容默认值。新增厂商无需改任何 Python。

    protocol:
      auth:
        scheme: bearer            # bearer | x-api-key | none
        headers: {}               # 额外固定头，如 anthropic-version
      chat:
        path: /chat/completions
      models:
        path: /models             # 仅在未声明 models_url 时作为回退
        list_key: data            # "" 表示顶层就是数组（Ollama 是 models）
        id_field: id
        name_field: ''            # '' → 取 id 最后一段
        required: {type: model}   # 列表项必须匹配的字段
      health:
        method: GET
        path: ''                  # '' → 复用模型列表 URL
"""
from __future__ import annotations

import copy

# OpenAI 兼容默认值——覆盖绝大多数厂商，未声明 protocol 的 provider 走这里
DEFAULT_PROTOCOL: dict = {
    "auth": {"scheme": "bearer", "headers": {}},
    "chat": {"path": "/chat/completions"},
    "models": {
        "path": "/models",
        "list_key": "data",
        "id_field": "id",
        "name_field": "",
        "required": {},
    },
    "health": {"method": "GET", "path": ""},
}

_AUTH_SCHEMES = ("bearer", "x-api-key", "none")


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def resolve_protocol(cfg: dict | None) -> dict:
    """把 provider 配置里的 ``protocol:`` 块合并到默认值上（不认识的值按默认处理）。"""
    cfg = cfg or {}
    declared = cfg.get("protocol")
    proto = _deep_merge(DEFAULT_PROTOCOL, declared if isinstance(declared, dict) else {})
    if proto["auth"].get("scheme") not in _AUTH_SCHEMES:
        proto["auth"]["scheme"] = DEFAULT_PROTOCOL["auth"]["scheme"]
    return proto


def requires_api_key(protocol: dict) -> bool:
    """``auth.scheme: none`` 的厂商（本地 Ollama 等）不该被「未配置 API Key」拦下。"""
    return (protocol.get("auth") or {}).get("scheme", "bearer") != "none"


def auth_headers(protocol: dict, api_key: str) -> dict:
    """按声明的鉴权方案生成请求头（不再假设一定是 Bearer）。"""
    auth = protocol.get("auth") or {}
    scheme = auth.get("scheme", "bearer")
    headers: dict = {}
    if scheme == "bearer" and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif scheme == "x-api-key" and api_key:
        headers["x-api-key"] = api_key
    headers.update(auth.get("headers") or {})
    return headers


def models_url(cfg: dict, protocol: dict) -> str:
    """模型列表 URL：显式 ``models_url`` 优先（厂商真实端点），否则 base_url + 声明路径。"""
    declared = str((cfg or {}).get("models_url") or "").strip()
    if declared:
        return declared
    base = str((cfg or {}).get("base_url") or "").rstrip("/")
    return f"{base}{protocol['models']['path']}"


def health_url(cfg: dict, protocol: dict) -> str:
    """连接测试 URL：未声明 ``health.path`` 时复用模型列表 URL。

    复用而非另拼路径是有意为之——模型列表端点是每个 provider 都维护过的真实地址，
    另拼一份必然与它漂移（Anthropic 的 ``/v1/v1/messages`` 就是这么来的）。
    """
    path = str((protocol.get("health") or {}).get("path") or "").strip()
    if not path:
        return models_url(cfg, protocol)
    base = str((cfg or {}).get("base_url") or "").rstrip("/")
    return f"{base}{path}"


def chat_url(cfg: dict, protocol: dict) -> str:
    base = str((cfg or {}).get("base_url") or "").rstrip("/")
    return f"{base}{protocol['chat']['path']}"


def parse_model_list(data, protocol: dict) -> list[dict]:
    """按声明解析模型列表，返回 ``[{"id", "name"}]``。

    兼容三种常见形态：顶层数组（``list_key: ""``）、``{"data": [...]}``、
    ``{"models": [...]}``；并可按 ``required`` 过滤（如 Anthropic 的 ``type: model``）。
    """
    spec = protocol.get("models") or {}
    list_key = spec.get("list_key", "data")
    if isinstance(data, list):
        items = data
    elif list_key:
        items = (data or {}).get(list_key) or []
    else:
        items = []
    id_field = spec.get("id_field", "id")
    name_field = spec.get("name_field") or ""
    required = spec.get("required") or {}
    out: list[dict] = []
    for m in items:
        if not isinstance(m, dict):
            continue
        if any(m.get(k) != v for k, v in required.items()):
            continue
        mid = str(m.get(id_field) or "")
        if not mid:
            continue
        name = str(m.get(name_field) or "") if name_field else ""
        out.append({"id": mid, "name": name or mid.rsplit("/", 1)[-1]})
    return out
