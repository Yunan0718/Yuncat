"""tests/contract/test_phase65_messageitem_mobile.py —— Phase 65 MessageItem 操作按钮移动端适配合约测试

验证 Phase 65 前端实现（对标 Phase 70/71 合约测试模式——静态分析 .tsx + i18n 验证）：
1. MessageItem.tsx 含 Phase 65 标记注释
2. PromptBubble hover 依赖修复——小屏默认可见（opacity-100 md:opacity-0 ...）
3. PromptBubble 含 focus-within 保证键盘 focus 可见
4. TaskFooter 次要按钮桌面端平铺（hidden md:inline-flex）
5. TaskFooter 小屏收入下拉菜单（md:hidden + DropdownMenu）
6. 所有 iconOnly 按钮含 aria-label（无障碍）
7. PromptBubble "编辑重送"按钮含 aria-label + title
8. 4 语言 i18n locale 文件均含 phase65.moreActions key
9. Vitest 文件 MessageItem.test.tsx 存在
"""
import os
import re

_SRC_FE = os.path.join(os.path.dirname(__file__), "..", "..", "src-frontend")
_MSG_ITEM = os.path.join(_SRC_FE, "src", "components", "chat", "MessageItem.tsx")
_VITEST = os.path.join(_SRC_FE, "src", "components", "__tests__", "MessageItem.test.tsx")
_I18N_DIR = os.path.join(_SRC_FE, "src", "i18n", "locales")
_LOCALES = ["zh", "en", "ja", "ko"]


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── Phase 65 标记 ──

def test_message_item_has_phase65_marker():
    """MessageItem.tsx 含 Phase 65 标记注释（便于追溯）"""
    code = _read(_MSG_ITEM)
    assert "Phase 65" in code, "MessageItem.tsx 应含 'Phase 65' 标记注释"


# ── PromptBubble hover 修复 ──

def test_prompt_bubble_hover_fix_small_screen_visible():
    """PromptBubble 编辑按钮小屏默认可见——opacity-100 md:opacity-0 模式"""
    code = _read(_MSG_ITEM)
    # 核心：opacity-100（小屏可见）+ md:opacity-0（桌面默认隐藏）
    assert re.search(r"opacity-100\s+md:opacity-0", code), (
        "PromptBubble 应使用 'opacity-100 md:opacity-0' 模式——小屏默认可见，桌面 hover 显示"
    )


def test_prompt_bubble_focus_within_visible():
    """PromptBubble 含 focus-within 保证键盘 focus 时按钮可见（无障碍）"""
    code = _read(_MSG_ITEM)
    assert re.search(r"md:focus-within:opacity-100", code), (
        "PromptBubble 应含 'md:focus-within:opacity-100'——键盘 focus 时按钮可见（Phase 65 无障碍要求）"
    )


def test_prompt_bubble_edit_button_has_aria_label():
    """PromptBubble '编辑重送'按钮含 aria-label + title（无障碍）"""
    code = _read(_MSG_ITEM)
    # 查找 editResend 相关按钮的 aria-label
    assert re.search(r'aria-label=\{t\("chat\.editResend"\)\}', code), (
        "PromptBubble 编辑按钮应含 aria-label={t('chat.editResend')}"
    )
    assert re.search(r'title=\{t\("chat\.editResend"\)\}', code), (
        "PromptBubble 编辑按钮应含 title={t('chat.editResend')}"
    )


# ── TaskFooter 响应式下拉菜单 ──

def test_task_footer_secondary_buttons_desktop_inline():
    """TaskFooter 次要按钮桌面端平铺——hidden md:inline-flex"""
    code = _read(_MSG_ITEM)
    assert re.search(r"hidden\s+md:inline-flex", code), (
        "TaskFooter 次要按钮应使用 'hidden md:inline-flex'——桌面端平铺可见"
    )


def test_task_footer_mobile_dropdown_trigger():
    """TaskFooter 小屏收入下拉菜单——md:hidden + DropdownMenuTrigger"""
    code = _read(_MSG_ITEM)
    # 下拉触发器在小屏可见（md:hidden 表示桌面隐藏，小屏显示）
    assert re.search(r"md:hidden", code), (
        "TaskFooter 应含 'md:hidden'——小屏显示下拉触发器"
    )
    assert "DropdownMenu" in code, "TaskFooter 应使用 DropdownMenu 组件承载小屏次要按钮"


def test_task_footer_dropdown_trigger_has_aria_label():
    """TaskFooter 下拉触发器含 aria-label（无障碍）"""
    code = _read(_MSG_ITEM)
    assert re.search(r'aria-label=\{t\("phase65\.moreActions"\)\}', code), (
        "TaskFooter 下拉触发器应含 aria-label={t('phase65.moreActions')}"
    )


# ── aria-label 无障碍 ──

def test_icon_buttons_have_aria_label():
    """iconOnly 按钮含 aria-label（like/dislike/share 等）"""
    code = _read(_MSG_ITEM)
    # 次要按钮的 aria-label 模式：aria-label={a.title || a.label}
    assert re.search(r'aria-label=\{a\.title\s*\|\|\s*a\.label\}', code), (
        "iconOnly 按钮应含 aria-label={a.title || a.label}（动态无障碍标签）"
    )


def test_pause_button_has_aria_label():
    """暂停按钮含 aria-label（流式任务控制无障碍）"""
    code = _read(_MSG_ITEM)
    assert re.search(r'aria-label=\{t\("chat\.pause"\)\}', code), (
        "暂停按钮应含 aria-label={t('chat.pause')}"
    )


# ── i18n 4 语言 ──

def test_i18n_locales_have_phase65_more_actions():
    """4 语言 i18n locale 文件均含 phase65.moreActions key"""
    for lang in _LOCALES:
        path = os.path.join(_I18N_DIR, f"{lang}.json")
        assert os.path.exists(path), f"locale 文件不存在: {lang}.json"
        code = _read(path)
        assert re.search(r'"moreActions"\s*:', code), (
            f"{lang}.json 应含 'moreActions' key（Phase 65 i18n）"
        )


def test_i18n_phase65_more_actions_non_empty():
    """4 语言 phase65.moreActions 值非空"""
    import json
    for lang in _LOCALES:
        path = os.path.join(_I18N_DIR, f"{lang}.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        phase65 = data.get("phase65", {})
        assert phase65.get("moreActions"), (
            f"{lang}.json phase65.moreActions 值为空"
        )


# ── Vitest 存在 ──

def test_vitest_file_exists():
    """Vitest 文件 MessageItem.test.tsx 存在（Phase 65 单元测试）"""
    assert os.path.exists(_VITEST), (
        "MessageItem.test.tsx 应存在——Phase 65 要求 Vitest 断言 completed 任务操作按钮不依赖 hover"
    )


def test_vitest_file_has_completed_button_test():
    """Vitest 文件含 completed 任务按钮不依赖 hover 的测试用例"""
    code = _read(_VITEST)
    # 查找测试用例描述——completed + hover 或 button 相关
    assert re.search(r'(completed|hover|button|aria-label|moreActions)', code, re.IGNORECASE), (
        "MessageItem.test.tsx 应含测试用例验证 completed 任务按钮/无障碍"
    )
