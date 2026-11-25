"""tests/contract/test_reasoning_vendor_neutral.py —— 思考强度映射必须「厂商中立」

2026-09-10（多厂商适配审查的 P0 修复）：
1. 用户会接各种厂商的模型，映射**不得把任何单一厂商写死**：
   - 家族可由 config/models/<provider>.yaml 的 `reasoning:` 块显式声明；
   - 厂商可声明 `effort_field: ""` 表示「本网关不接收该参数」；
   - 未声明的厂商回落到标准 reasoning_effort，且**永不把不支持的档位值硬发**。
2. 家族识别以**模型名优先、provider 兜底**——同一 provider 可托管别家模型
   （火山托管 deepseek-*），且 provider=openai 不能抹平 gpt-5.1 与 o3 的档位差异。
3. 参数不被接受时必须能**摘掉重试**（对任意厂商通用），而不是整次请求失败。
4. 已知错误修正：Gemini 走 /v1beta/openai 兼容端点，不得再发原生字段 thinkingBudget。
"""
import os
import sys
import glob

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from services.reasoning import (  # noqa: E402
    describe_model_reasoning,
    detect_family,
    is_reasoning_param_rejection,
    reasoning_spec,
    strip_reasoning_params,
    to_payload,
    to_payload_with_disable,
)

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "config", "models")


# ---- 1. 厂商中立：不写死 ----

def test_openai_family_gets_standard_field():
    assert to_payload_with_disable("high", "gpt-5.2", "openai") == {"reasoning_effort": "high"}
    assert to_payload_with_disable("none", "gpt-5.2", "openai") == {"reasoning_effort": "none"}


def test_unknown_vendor_uses_declared_field_only():
    """自定义厂商声明什么字段就发什么字段——代码里没有任何厂商名参与。"""
    spec = {"effort_field": "reasoning.effort"}
    assert to_payload_with_disable("high", "foo-9", "custom", spec) == {"reasoning.effort": "high"}


def test_vendor_can_opt_out_entirely():
    """厂商声明 effort_field: "" → 完全不发该参数（严格网关场景）。"""
    spec = {"effort_field": ""}
    assert to_payload_with_disable("high", "foo-9", "custom", spec) == {}
    assert to_payload_with_disable("none", "foo-9", "custom", spec) == {}


def test_vendor_spec_disable_overrides_builtin():
    spec = {"effort_field": "reasoning.effort", "disable": {"reasoning": {"enabled": False}}}
    out = to_payload_with_disable("none", "foo-9", "custom", spec)
    assert out == {"reasoning": {"enabled": False}}


def test_declared_disable_removes_declared_effort_field():
    """关思考时必须摘掉「该厂商声明的」effort 字段，而不是硬编码的 reasoning_effort。"""
    spec = {"effort_field": "reasoning.effort", "disable": {"reasoning": {"enabled": False}}}
    out = to_payload_with_disable("minimal", "foo-9", "custom", spec)
    assert "reasoning.effort" not in out
    assert out["reasoning"]["enabled"] is False


def test_spec_family_shortcut():
    spec = {"family": "deepseek"}
    assert detect_family("mystery-model", "custom", spec) == "deepseek"


def test_reasoning_spec_ignores_malformed():
    assert reasoning_spec(None) == {}
    assert reasoning_spec({"reasoning": "not-a-dict"}) == {}
    assert reasoning_spec({"reasoning": {"effort_field": ""}}) == {"effort_field": ""}


# ---- 2. 家族识别：模型名优先，provider 兜底 ----

def test_model_name_beats_provider():
    """provider=openai 不得把 gpt-5.2 降级成 generic（会丢掉 none 档）。"""
    assert detect_family("gpt-5.2", "openai") == "openai_gpt51"
    assert "none" in describe_model_reasoning("gpt-5.2", "openai")["efforts"]


def test_provider_is_fallback_only():
    """模型名识别不出时用 provider 兜底。"""
    assert detect_family("mystery-1", "anthropic") == "claude"
    assert detect_family("mystery-1", "deepseek") == "deepseek"
    assert detect_family("mystery-1", "nope") == "generic"


def test_cross_hosted_model_keeps_its_own_semantics():
    """火山/OpenRouter 托管别家模型时，按模型语义走，不按 provider。"""
    assert detect_family("deepseek-v3", "doubao") == "deepseek"


# ---- 3. 档位 clamp：宁可收敛也不硬发 ----

def test_effort_is_passed_through_verbatim():
    """2026-10-08：档位**原样透传**，不再收敛成厂商档位表里的值。

    用户明确要求：思考强度就是发给厂商的字段，由厂商自己解释。
    我们此前会把 xhigh 压成 high——用户开了最高强度，实际发出去却是次高档，
    这正是「开了最高思考强度却不如网页版」的一部分原因。
    """
    assert to_payload_with_disable("xhigh", "deepseek-v4-pro", "deepseek")["reasoning_effort"] == "xhigh"
    assert to_payload_with_disable("xhigh", "gpt-5.2", "openai")["reasoning_effort"] == "xhigh"
    # 厂商档位表里没有、但属于"不思考"语义的档位，仍翻译成该厂商的关闭字段（语义翻译，非降档）
    assert to_payload_with_disable("minimal", "deepseek-v4-pro", "deepseek") == {"thinking": {"type": "disabled"}}


def test_unknown_effort_falls_back_without_vendor_knowledge():
    """未知取值只做合法性归一（→ medium），不掺任何厂商档位表判断。"""
    assert to_payload_with_disable("bogus", "deepseek-v4-pro", "deepseek")["reasoning_effort"] == "medium"
    assert to_payload_with_disable("", "gpt-5.2", "openai")["reasoning_effort"] == "medium"


def test_no_disable_field_means_omit_not_downgrade():
    """kimi 无关闭字段 → 省略全部参数，而不是把「关闭」降级成 low 静默硬发。"""
    assert to_payload_with_disable("none", "kimi-k2.6", "kimi") == {}
    assert to_payload_with_disable("none", "claude-opus-5", "anthropic") == {}


def test_family_disable_fields_are_used():
    assert to_payload_with_disable("none", "deepseek-v4-pro", "deepseek") == {"thinking": {"type": "disabled"}}
    assert to_payload_with_disable("none", "qwen3-max", "qwen") == {"enable_thinking": False}
    assert to_payload_with_disable("none", "glm-5", "glm") == {"thinking": {"type": "disabled"}}


# ---- 4. 已知错误修正：Gemini 兼容端点不再发原生字段 ----

def test_gemini_uses_openai_shim_field_not_native_field():
    out = to_payload_with_disable("none", "gemini-3-pro", "gemini")
    assert out == {"reasoning_effort": "none"}
    assert "thinkingBudget" not in out


def test_gemini_none_is_advertised_and_never_clamped():
    info = describe_model_reasoning("gemini-3-pro", "gemini")
    assert "none" in info["efforts"] and info["off_supported"] is True


# ---- 5. 摘参数重试链路 ----

def test_strip_removes_every_managed_key():
    payload = {
        "model": "x", "messages": [],
        "reasoning_effort": "high",
        "thinking": {"type": "disabled"},
        "enable_thinking": False,
    }
    removed = strip_reasoning_params(payload)
    assert sorted(removed) == ["enable_thinking", "reasoning_effort", "thinking"]
    assert payload == {"model": "x", "messages": []}


def test_strip_returns_empty_when_nothing_to_strip():
    assert strip_reasoning_params({"model": "x"}) == []


def test_rejection_detected_for_param_errors_only():
    assert is_reasoning_param_rejection(400, '{"error":"Unrecognized request argument: reasoning_effort"}')
    assert is_reasoning_param_rejection(422, "unknown parameter: thinking")
    # 与参数无关的 400 不能误判，否则会白白重试并掩盖真实错误
    assert not is_reasoning_param_rejection(400, "model not found")
    assert not is_reasoning_param_rejection(400, "context length exceeded")
    # 鉴权/限流/服务端错误重试也不会成功
    assert not is_reasoning_param_rejection(401, "unknown parameter reasoning_effort")
    assert not is_reasoning_param_rejection(429, "unknown parameter reasoning_effort")
    assert not is_reasoning_param_rejection(503, "unknown parameter reasoning_effort")


# ---- 6. 配置层契约：内置 provider 的 YAML 必须自洽 ----

def _load_all_providers() -> dict[str, dict]:
    out = {}
    for f in glob.glob(os.path.join(CONFIG_DIR, "*.yaml")):
        name = os.path.basename(f)
        if name == "_index.yaml":
            continue
        with open(f, encoding="utf-8") as fh:
            out[name] = yaml.safe_load(fh) or {}
    return out


def test_default_model_must_exist_in_its_own_list():
    """默认模型不在 models 列表里 = 用户点开就是 404（openai.yaml 曾踩过）。"""
    offenders = []
    for name, d in _load_all_providers().items():
        ids = [m["id"] for m in (d.get("models") or [])]
        if not ids:
            continue  # 聚合型 provider（openrouter）列表由运行时拉取
        if d.get("model") and d["model"] not in ids:
            offenders.append(f"{name}: {d['model']}")
    assert offenders == [], f"默认模型不在自述模型列表内: {offenders}"


def test_no_duplicate_model_ids():
    for name, d in _load_all_providers().items():
        ids = [m["id"] for m in (d.get("models") or [])]
        assert len(ids) == len(set(ids)), f"{name} 存在重复 model id"


def test_needs_proxy_providers_declare_a_hint():
    """声明 needs_proxy 却不说为什么，用户只会看到裸 404/401。"""
    for name, d in _load_all_providers().items():
        if d.get("needs_proxy"):
            assert d.get("proxy_hint"), f"{name} 声明 needs_proxy 但缺 proxy_hint"


def test_pricing_does_not_leak_across_vendors():
    """定价表里出现别家厂商的模型 = 目录同步串味（gemini.yaml 曾混入 grok-*）。"""
    leaks = []
    for name, d in _load_all_providers().items():
        vendor = name.replace(".yaml", "")
        if vendor == "openrouter":
            continue  # 聚合平台，本来就托管各家模型
        for mid in (d.get("pricing") or {}):
            head = mid.split("-")[0].lower()
            if head.startswith("grok") and vendor != "grok":
                leaks.append(f"{name}: {mid}")
    assert leaks == [], f"定价表存在跨厂商污染: {leaks}"
