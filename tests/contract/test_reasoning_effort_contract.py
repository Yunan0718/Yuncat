"""tests/contract/test_reasoning_effort_contract.py —— 思考档位必须由模型支持决定

2026-09-17：之前前端固定展示几档、靠后端静默 clamp；现在改成
模型选择器里只列该模型真实支持的档位（对标 Codex）。下面把
“后端给的支持集”与“前端只渲染这些档位”两头都钉住。
"""
import json
import os
import sys

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_SRC_PY = os.path.join(_ROOT, "src-python")
_SRC_FE = os.path.join(_ROOT, "src-frontend")
_LOCALES = ["zh", "en", "ja", "ko"]

sys.path.insert(0, _SRC_PY)


def _read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_backend_reports_supported_efforts_per_model():
    from services.reasoning import describe_model_reasoning

    # 2026-10-08：档位不再按厂商裁剪（用户要求「思考强度就是透传字段，别限制」），
    # 前端统一拿到完整档位全集；各厂商原生支持哪些由 vendor_efforts 单独给出。
    FULL = ["none", "minimal", "low", "medium", "high", "xhigh"]

    gpt51 = describe_model_reasoning("gpt-5.1")
    assert gpt51["efforts"] == FULL, gpt51
    assert gpt51["effort_supported"] is True
    assert gpt51["vendor_efforts"] == ["none", "low", "medium", "high"]

    codex_max = describe_model_reasoning("gpt-5.1-codex-max")
    assert "xhigh" in codex_max["efforts"], codex_max

    deepseek = describe_model_reasoning("deepseek-chat")   # 可用 thinking:disabled 关思考
    assert deepseek["efforts"][0] == "none", deepseek
    assert deepseek["off_supported"] is True

    claude = describe_model_reasoning("claude-sonnet-4-5")
    assert claude["efforts"] == FULL, claude
    assert claude["vendor_efforts"] == ["low", "medium", "high"]

    legacy = describe_model_reasoning("o3-mini")
    assert "minimal" in legacy["efforts"], legacy


def test_frontend_renders_only_what_the_model_supports():
    hook = _read(_SRC_FE, "src", "hooks", "useModelReasoning.ts")
    assert "/api/v1/models/reasoning" in hook, "前端应向后端要支持档位"
    assert "effort_supported" in hook, "厂商声明不接收该参数时要能识别"

    # 2026-09-17: 模型 + 思考强度 + 能力档合并到模型选择器，ModelEffortBar 已删
    selector = _read(_SRC_FE, "src", "components", "ModelSelector.tsx")
    assert "useModelReasoning" in selector, "模型选择器应用共用 hook 拿档位"
    assert "[\"low\", \"medium\", \"high\"]" not in selector, "不能再把档位写死"
    assert not os.path.exists(
        os.path.join(_SRC_FE, "src", "components", "chat", "ModelEffortBar.tsx")
    ), "旧的强度控件应已合并进模型选择器"

    selector = _read(_SRC_FE, "src", "components", "ModelSelector.tsx")
    assert "reasoning.efforts.map" in selector, "下拉里应按后端返回的档位渲染"


def test_effort_source_is_tracked_in_the_store():
    store = _read(_SRC_FE, "src", "store", "appStore.ts")
    assert "reasoningEffortSource" in store, "要区分“跟随档位”与“用户显式选”"
    assert "resetReasoningEffort" in store, "要能切回跟随"
    assert 'state.reasoningEffortSource === "auto"' in store, (
        "只有跟随档位时才能被能力档带着改"
    )


def test_four_locales_have_every_level_wording():
    levels = ["None", "Minimal", "Low", "Medium", "High", "Xhigh"]
    chat_keys = {f"effort{name}" for name in levels} | {f"effort{name}Desc" for name in levels}
    chat_keys |= {"effortFollowCap", "effortCustom", "effortCustomHint"}
    for lang in _LOCALES:
        path = os.path.join(_SRC_FE, "src", "i18n", "locales", f"{lang}.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        missing = chat_keys - set(data.get("chat", {}).keys())
        assert not missing, f"{lang}.json chat 缺少: {sorted(missing)}"
        for key in chat_keys:
            assert data["chat"][key].strip(), f"{lang}.json chat.{key} 不能为空"
        ms = data.get("modelSelector", {})
        assert ms.get("effortTitle") and ms.get("effortUnsupported"), (
            f"{lang}.json modelSelector 缺思考档位文案"
        )
