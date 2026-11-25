"""tests/contract/test_provider_protocol.py —— 厂商端点/鉴权必须「数据驱动」

2026-09-10（多厂商适配审查）：连接测试与模型列表刷新里的 `provider == "anthropic"` /
`provider == "ollama"` 分支被复制到多处后立刻漂移，实测踩出三个真 bug：
1. Anthropic 健康检查 → ``…/v1/v1/messages``（base_url 已含 /v1，又接了一次 /v1）必然 404；
2. Ollama 健康检查 → ``…/v1/api/tags``（tags 在 origin 下，不经 /v1）404；
3. Ollama 声明 ``auth: none`` 却仍在协议判断前因「未配置 API Key」被拦成 401。

修复后：知识进 YAML 的 ``protocol:`` 块，代码只做通用解析。
这些测试锁住「路径不漂移」「鉴权按声明」「无 key 厂商不被拦」三件事。
"""
import os
import sys
import glob

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from services.provider_protocol import (  # noqa: E402
    DEFAULT_PROTOCOL,
    auth_headers,
    chat_url,
    health_url,
    models_url,
    parse_model_list,
    requires_api_key,
    resolve_protocol,
)

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "config", "models")


def _cfg(pid: str) -> dict:
    with open(os.path.join(CONFIG_DIR, f"{pid}.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ---- 默认值：OpenAI 兼容 ----

def test_defaults_are_openai_compatible():
    p = resolve_protocol({})
    assert p["chat"]["path"] == "/chat/completions"
    assert p["auth"]["scheme"] == "bearer"
    assert p["models"]["list_key"] == "data"
    assert requires_api_key(p) is True


def test_malformed_protocol_falls_back_to_defaults():
    assert resolve_protocol({"protocol": "nope"}) == DEFAULT_PROTOCOL
    assert resolve_protocol({"protocol": {"auth": {"scheme": "huh"}}})["auth"]["scheme"] == "bearer"


def test_deep_merge_keeps_undeclared_defaults():
    p = resolve_protocol({"protocol": {"models": {"list_key": "result"}}})
    assert p["models"]["list_key"] == "result"
    assert p["models"]["id_field"] == "id"       # 未声明的键仍取默认
    assert p["chat"]["path"] == "/chat/completions"


# ---- 路径不漂移（三个真 bug 的回归守卫）----

def test_anthropic_health_does_not_double_v1():
    cfg = _cfg("anthropic")
    p = resolve_protocol(cfg)
    url = health_url(cfg, p)
    assert url == "https://api.anthropic.com/v1/models"
    assert "/v1/v1" not in url


def test_ollama_health_is_on_origin_not_under_v1():
    cfg = _cfg("ollama")
    url = health_url(cfg, resolve_protocol(cfg))
    assert url == "http://localhost:11434/api/tags"
    assert "/v1/api/" not in url


def test_chat_url_appends_declared_path():
    cfg = {"base_url": "https://api.x.com/v1/"}
    assert chat_url(cfg, resolve_protocol(cfg)) == "https://api.x.com/v1/chat/completions"
    assert chat_url(cfg, resolve_protocol(
        {"protocol": {"chat": {"path": "/custom"}}})) == "https://api.x.com/v1/custom"


def test_models_url_prefers_declared_endpoint():
    """声明了 models_url 就必须用它——厂商真实端点优先于拼接猜测。"""
    cfg = {"base_url": "https://a/v1", "models_url": "https://a/native/list"}
    assert models_url(cfg, resolve_protocol(cfg)) == "https://a/native/list"


# ---- 鉴权按声明，不假设 Bearer ----

def test_auth_bearer_and_x_api_key():
    assert auth_headers(resolve_protocol({}), "K")["Authorization"] == "Bearer K"
    a = auth_headers(resolve_protocol(_cfg("anthropic")), "K")
    assert a["x-api-key"] == "K" and "Authorization" not in a
    assert a["anthropic-version"] == "2023-06-01"


def test_no_key_vendor_is_not_blocked():
    """Ollama 本地无 key，不能被「未配置 API Key」拦下。"""
    p = resolve_protocol(_cfg("ollama"))
    assert requires_api_key(p) is False
    assert auth_headers(p, "") == {}


# ---- 列表解析按声明 ----

def test_parse_openai_style():
    p = resolve_protocol({})
    assert parse_model_list({"data": [{"id": "deepseek/a-1"}, {"id": "b"}]}, p) == [
        {"id": "deepseek/a-1", "name": "a-1"}, {"id": "b", "name": "b"}]


def test_parse_top_level_array():
    p = resolve_protocol({"protocol": {"models": {"list_key": ""}}})
    assert parse_model_list([{"id": "x"}], p) == [{"id": "x", "name": "x"}]


def test_parse_ollama_style():
    p = resolve_protocol(_cfg("ollama"))
    data = {"models": [{"name": "llama3:8b", "model": "llama3:8b"}]}
    assert parse_model_list(data, p) == [{"id": "llama3:8b", "name": "llama3:8b"}]


def test_parse_anthropic_style_filters_and_maps_name():
    p = resolve_protocol(_cfg("anthropic"))
    data = {"data": [
        {"id": "claude-opus-5", "display_name": "Claude Opus 5", "type": "model"},
        {"id": "not-a-model", "type": "other"},
    ]}
    assert parse_model_list(data, p) == [{"id": "claude-opus-5", "name": "Claude Opus 5"}]


def test_parse_tolerates_garbage():
    p = resolve_protocol({})
    assert parse_model_list(None, p) == []
    assert parse_model_list({"data": ["str", None, {}]}, p) == []


# ---- 配置层契约：每个 provider 都要能解析出可用端点 ----

def test_every_provider_resolves_a_usable_health_url():
    for f in glob.glob(os.path.join(CONFIG_DIR, "*.yaml")):
        if os.path.basename(f) == "_index.yaml":
            continue
        with open(f, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        if not cfg.get("base_url"):
            continue  # models: [] 的占位 provider（custom）允许留空
        url = health_url(cfg, resolve_protocol(cfg))
        assert url.startswith(("http://", "https://")), f"{f} → {url}"
        assert "/v1/v1" not in url, f"{f} 出现重复 /v1: {url}"


def test_declared_models_url_never_has_double_v1():
    for f in glob.glob(os.path.join(CONFIG_DIR, "*.yaml")):
        if os.path.basename(f) == "_index.yaml":
            continue
        with open(f, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        mu = cfg.get("models_url", "")
        assert "/v1/v1" not in mu, f"{f} 的 models_url 重复 /v1: {mu}"
