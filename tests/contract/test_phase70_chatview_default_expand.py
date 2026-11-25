"""tests/contract/test_phase70_chatview_default_expand.py —— Phase 70 ChatView 默认展开合约测试

验证：
1. ChatView.tsx 导出 loadLeftCollapsed() 纯函数，默认返回 false（展开）
2. ChatView.tsx useState 用 loadLeftCollapsed() 初始化 leftCollapsed
3. ChatView.tsx 持久化 leftCollapsed 到 localStorage（key: yuncat-left-collapsed）
4. ChatView.tsx 首次进入引导 toast（flag: yuncat-chat-view-visited，i18n key: phase70.welcomeToast）
5. appStore.ts rightCollapsed 默认 false（stored === null ? false : ...）
6. 4 语言 i18n locale 文件均含 phase70.welcomeToast key
"""
import os
import re

_SRC_FE = os.path.join(os.path.dirname(__file__), "..", "..", "src-frontend")
_APP_STORE = os.path.join(_SRC_FE, "src", "store", "appStore.ts")
_CHAT_VIEW = os.path.join(_SRC_FE, "src", "components", "chat", "ChatView.tsx")
_I18N_DIR = os.path.join(_SRC_FE, "src", "i18n", "locales")
_LOCALES = ["zh", "en", "ja", "ko"]


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_chat_view_exports_load_left_collapsed():
    """ChatView.tsx 导出 loadLeftCollapsed 纯函数"""
    code = _read(_CHAT_VIEW)
    assert re.search(r"export\s+function\s+loadLeftCollapsed\s*\(", code), (
        "ChatView.tsx 应导出 loadLeftCollapsed() 函数（供单元测试，对齐 Phase 66 TerminalPanel 模式）"
    )


def test_load_left_collapsed_default_false():
    """loadLeftCollapsed 在 localStorage 为空时返回 false（默认展开）"""
    code = _read(_CHAT_VIEW)
    # 核心逻辑：stored === null ? false : stored === "true"
    match = re.search(
        r"function\s+loadLeftCollapsed\([^)]*\)[^{]*\{[\s\S]*?return\s+stored\s*===\s*null\s*\?\s*false",
        code,
    )
    assert match, (
        "loadLeftCollapsed 应在 stored === null 时返回 false（默认展开，Phase 70 核心要求）"
    )


def test_chat_view_uses_load_left_collapsed_in_state():
    """ChatView.tsx 用 loadLeftCollapsed() 初始化 leftCollapsed useState"""
    code = _read(_CHAT_VIEW)
    # 匹配：useState<boolean>(() => loadLeftCollapsed())
    assert re.search(r"useState<boolean>\(\(\)\s*=>\s*loadLeftCollapsed\(\)\)", code), (
        "ChatView.tsx 应用 loadLeftCollapsed() 作为 leftCollapsed 的 useState 初始化器"
    )


def test_chat_view_persists_left_collapsed():
    """ChatView.tsx 持久化 leftCollapsed 到 localStorage"""
    code = _read(_CHAT_VIEW)
    assert "yuncat-left-collapsed" in code, (
        "ChatView.tsx 应将 leftCollapsed 持久化到 localStorage key 'yuncat-left-collapsed'"
    )
    # 应有 useEffect 在 leftCollapsed 变化时写入
    assert re.search(r"localStorage\.setItem\(\s*['\"]yuncat-left-collapsed['\"]", code), (
        "ChatView.tsx 应在 useEffect 中调用 localStorage.setItem('yuncat-left-collapsed', ...)"
    )


def test_chat_view_welcome_toast_first_visit():
    """ChatView.tsx 首次进入显示引导 toast"""
    code = _read(_CHAT_VIEW)
    # 首次访问 flag
    assert "yuncat-chat-view-visited" in code, (
        "ChatView.tsx 应使用 localStorage flag 'yuncat-chat-view-visited' 标记首次访问"
    )
    # 应调用 toast 显示 phase70.welcomeToast
    assert "phase70.welcomeToast" in code, (
        "ChatView.tsx 应调用 toast(t('phase70.welcomeToast'), ...) 显示首次进入引导"
    )


def test_app_store_right_collapsed_default_true():
    """appStore.ts rightCollapsed 在 localStorage 为空时默认 true（折叠）

    决策变更：Phase 70 初版为默认展开，后改为默认折叠——对标 ChatGPT/Claude，
    首屏以对话为主，右侧上下文面板作为可选辅助区由用户手动打开，
    避免初始界面繁杂（见 appStore.ts 中 rightCollapsed 的注释）。
    """
    code = _read(_APP_STORE)
    # 匹配 IIFE 中的默认 true 逻辑：stored === null ? true : stored === "true"
    # 容忍多空格
    pattern = r"stored\s*===\s*null\s*\?\s*true\s*:\s*stored\s*===\s*['\"]true['\"]"
    assert re.search(pattern, code), (
        "appStore.ts rightCollapsed 应在 stored === null 时返回 true（默认折叠）"
    )
    # localStorage 不可用（隐私模式等）时也应回退到折叠，与默认值一致
    fallback = re.search(r"\}\s*catch\s*\{?\s*return\s*true", code)
    assert fallback, "rightCollapsed 的 catch 分支应回退到 true（与默认值一致）"


def test_i18n_locales_have_welcome_toast():
    """4 语言 i18n locale 文件均含 phase70.welcomeToast key"""
    for lang in _LOCALES:
        path = os.path.join(_I18N_DIR, f"{lang}.json")
        assert os.path.exists(path), f"i18n locale 文件不存在：{path}"
        code = _read(path)
        assert "phase70" in code, f"{lang}.json 应含 phase70 命名空间"
        assert "welcomeToast" in code, (
            f"{lang}.json 应含 phase70.welcomeToast key"
        )


def test_i18n_locales_welcome_toast_non_empty():
    """4 语言 phase70.welcomeToast 均有非空翻译值"""
    import json

    for lang in _LOCALES:
        path = os.path.join(_I18N_DIR, f"{lang}.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        phase70 = data.get("phase70", {})
        assert isinstance(phase70, dict), f"{lang}.json phase70 应为对象"
        toast_msg = phase70.get("welcomeToast")
        assert isinstance(toast_msg, str) and len(toast_msg) > 5, (
            f"{lang}.json phase70.welcomeToast 应为非空字符串（长度 > 5）"
        )
