"""tests/contract/test_phase73a_prompt_optimize.py —— Phase 73-A 输入框 ✨ 优化合约测试

验证：
1. 前端组件存在 + 关键结构
   - PromptOptimizeButton.tsx 含 Sparkles icon + aria-label + data-testid
   - OptimizedPromptPreview.tsx 含 apply/discard 按钮 + data-testid
   - usePromptOptimize.ts 导出 buildOptimizeRequest + OptimizeResult
2. InputArea.tsx 集成 ✨ 按钮 + 预览面板
   - import PromptOptimizeButton / OptimizedPromptPreview / usePromptOptimize
   - 调用 optimize(input)
   - OptimizedPromptPreview 条件渲染 + onApply 调 setInput
3. 4 语言 i18n 含 chat.optimizePrompt 块
4. 后端 PromptAgent.optimize 方法存在
5. 后端 routes POST /optimize 端点存在
6. prompts/prompt_optimize.txt 模板存在
7. Vitest 文件存在
"""
import json
import os
import re

_SRC_FE = os.path.join(os.path.dirname(__file__), "..", "..", "src-frontend")
_SRC_PY = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")

_BUTTON = os.path.join(_SRC_FE, "src", "components", "chat", "PromptOptimizeButton.tsx")
_PREVIEW = os.path.join(_SRC_FE, "src", "components", "chat", "OptimizedPromptPreview.tsx")
_HOOK = os.path.join(_SRC_FE, "src", "hooks", "usePromptOptimize.ts")
_INPUT_AREA = os.path.join(_SRC_FE, "src", "components", "chat", "InputArea.tsx")
_VITEST = os.path.join(_SRC_FE, "src", "hooks", "__tests__", "usePromptOptimize.test.ts")
_I18N_DIR = os.path.join(_SRC_FE, "src", "i18n", "locales")
_LOCALES = ["zh", "en", "ja", "ko"]

_AGENT_PY = os.path.join(_SRC_PY, "services", "prompt_agent.py")
_AGENT_ROUTE = os.path.join(_SRC_PY, "routes", "prompt_agent.py")
_OPTIMIZE_TXT = os.path.join(_SRC_PY, "prompts", "prompt_optimize.txt")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── 前端组件 ──

def test_prompt_optimize_button_exists():
    """PromptOptimizeButton.tsx 存在"""
    assert os.path.isfile(_BUTTON), "PromptOptimizeButton.tsx 应存在"


def test_button_has_sparkles_icon():
    """按钮含 Sparkles icon（✨）"""
    code = _read(_BUTTON)
    assert "Sparkles" in code, "PromptOptimizeButton 应 import Sparkles from lucide-react"


def test_button_has_loading_state():
    """按钮含 loading 状态（Loader2 spinner）"""
    code = _read(_BUTTON)
    assert "Loader2" in code, "PromptOptimizeButton 应含 Loader2 loading spinner"
    assert "loading" in code, "PromptOptimizeButton 应接收 loading prop"


def test_button_has_aria_label():
    """按钮含 aria-label（无障碍）"""
    code = _read(_BUTTON)
    assert "aria-label" in code, "PromptOptimizeButton 应含 aria-label"


def test_button_has_testid():
    """按钮含 data-testid 便于测试"""
    code = _read(_BUTTON)
    assert "data-testid" in code, "PromptOptimizeButton 应含 data-testid"


def test_optimized_prompt_preview_exists():
    """OptimizedPromptPreview.tsx 存在"""
    assert os.path.isfile(_PREVIEW), "OptimizedPromptPreview.tsx 应存在"


def test_preview_has_apply_and_discard():
    """预览面板含 apply + discard 按钮"""
    code = _read(_PREVIEW)
    assert "onApply" in code, "OptimizedPromptPreview 应接收 onApply prop"
    assert "onDiscard" in code, "OptimizedPromptPreview 应接收 onDiscard prop"


def test_preview_renders_optimized_text():
    """预览面板渲染当前候选的 optimized 文本

    Batch 5.2 起支持多候选：面板按 selectedIndex 取 `selected`
    （= result.candidates[i] || result），渲染的是 selected.optimized。
    """
    code = _read(_PREVIEW)
    assert "result.candidates[selectedIndex]" in code, (
        "OptimizedPromptPreview 应支持多候选切换（selectedIndex）"
    )
    assert "selected.optimized" in code, "OptimizedPromptPreview 应渲染 selected.optimized"


def test_preview_renders_changes_list():
    """预览面板渲染当前候选的 changes 列表（Batch 5.2 后为 selected.changes）"""
    code = _read(_PREVIEW)
    assert "selected.changes" in code, "OptimizedPromptPreview 应渲染 selected.changes"


# ── usePromptOptimize hook ──

def test_hook_exports_build_optimize_request():
    """usePromptOptimize.ts 导出 buildOptimizeRequest 纯函数"""
    code = _read(_HOOK)
    assert re.search(r"export\s+function\s+buildOptimizeRequest\s*\(", code), (
        "usePromptOptimize 应导出 buildOptimizeRequest 函数（供 Vitest）"
    )


def test_hook_exports_optimize_result_type():
    """usePromptOptimize.ts 导出 OptimizeResult 类型"""
    code = _read(_HOOK)
    assert "OptimizeResult" in code, "usePromptOptimize 应导出 OptimizeResult 类型"


def test_hook_calls_optimize_api():
    """hook 调用 POST /api/v1/prompt-agent/optimize"""
    code = _read(_HOOK)
    assert "/api/v1/prompt-agent/optimize" in code, "hook 应调用 /api/v1/prompt-agent/optimize"


# ── InputArea 集成 ──

def test_input_area_imports_optimize_components():
    """InputArea.tsx import PromptOptimizeButton + OptimizedPromptPreview + usePromptOptimize"""
    code = _read(_INPUT_AREA)
    assert "PromptOptimizeButton" in code, "InputArea 应 import PromptOptimizeButton"
    assert "OptimizedPromptPreview" in code, "InputArea 应 import OptimizedPromptPreview"
    assert "usePromptOptimize" in code, "InputArea 应 import usePromptOptimize"


def test_input_area_renders_optimize_button():
    """InputArea.tsx 渲染 PromptOptimizeButton + 调用 optimize(input)"""
    code = _read(_INPUT_AREA)
    assert "<PromptOptimizeButton" in code, "InputArea 应渲染 <PromptOptimizeButton>"
    assert "optimize(input)" in code, "InputArea 应调用 optimize(input)"


def test_input_area_renders_preview_conditionally():
    """InputArea.tsx 条件渲染 OptimizedPromptPreview + onApply 调 setInput

    Batch 5.2 多候选后，onApply 改为回调形式 `onApply={(optimized) => setInput(optimized)}`
    ——由预览面板把当前选中候选的文本回传，InputArea 不再直接读
    optimizeResult.optimized（那样会忽略用户选择的候选）。
    """
    code = _read(_INPUT_AREA)
    assert "optimizeResult &&" in code or "optimizeResult && (" in code, (
        "InputArea 应条件渲染 OptimizedPromptPreview（optimizeResult 非空时）"
    )
    assert re.search(r"onApply=\{\(\s*optimized\s*\)\s*=>", code), (
        "onApply 应为回调形式，接收预览面板回传的候选文本"
    )
    assert re.search(r"setInput\(\s*optimized\s*\)", code), (
        "onApply 应调 setInput(optimized) 替换输入框内容"
    )


# ── i18n ──

def test_i18n_locales_have_optimize_prompt_keys():
    """4 语言 i18n locale 文件均含 chat.optimizePrompt 块"""
    for lang in _LOCALES:
        path = os.path.join(_I18N_DIR, f"{lang}.json")
        data = json.loads(_read(path))
        assert "chat" in data, f"{lang}.json 应含 chat 块"
        assert "optimizePrompt" in data["chat"], f"{lang}.json chat.optimizePrompt 应存在"
        block = data["chat"]["optimizePrompt"]
        for key in ["button", "loading", "title", "apply", "discard", "emptyError"]:
            assert key in block, f"{lang}.json chat.optimizePrompt.{key} 应存在"


def test_i18n_optimize_prompt_has_changes_label():
    """4 语言 i18n 含 chat.optimizePrompt.changesLabel"""
    for lang in _LOCALES:
        path = os.path.join(_I18N_DIR, f"{lang}.json")
        data = json.loads(_read(path))
        assert "changesLabel" in data["chat"]["optimizePrompt"], (
            f"{lang}.json chat.optimizePrompt.changesLabel 应存在"
        )


# ── 后端 ──

def test_backend_prompt_agent_has_optimize_method():
    """PromptAgent 类含 optimize 方法"""
    code = _read(_AGENT_PY)
    assert re.search(r"async\s+def\s+optimize\s*\(\s*self,\s*raw_prompt", code), (
        "PromptAgent 应含 async def optimize(self, raw_prompt, ...) 方法"
    )


def test_backend_route_has_optimize_endpoint():
    """routes/prompt_agent.py 含 POST /optimize 端点"""
    code = _read(_AGENT_ROUTE)
    assert '@router.post("/optimize")' in code, "routes/prompt_agent.py 应含 @router.post('/optimize')"


def test_backend_optimize_template_exists():
    """prompts/prompt_optimize.txt 模板存在"""
    assert os.path.isfile(_OPTIMIZE_TXT), "prompts/prompt_optimize.txt 应存在"


def test_backend_optimize_template_has_raw_prompt_placeholder():
    """优化模板含 {raw_prompt} 占位符"""
    code = _read(_OPTIMIZE_TXT)
    assert "{raw_prompt}" in code, "prompt_optimize.txt 应含 {raw_prompt} 占位符"


# ── Vitest 存在 ──

def test_vitest_use_prompt_optimize_exists():
    """Vitest 测试文件 usePromptOptimize.test.ts 存在"""
    assert os.path.isfile(_VITEST), "hooks/__tests__/usePromptOptimize.test.ts 应存在"


def test_vitest_tests_build_optimize_request():
    """Vitest 测试覆盖 buildOptimizeRequest 纯函数"""
    code = _read(_VITEST)
    assert "buildOptimizeRequest" in code, "Vitest 应测试 buildOptimizeRequest"
    assert "describe" in code, "Vitest 应含 describe 测试块"
