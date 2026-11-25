"""tests/contract/test_phase73_prompt_library_ui.py —— Phase 73 PromptManager UI 增强合约测试

验证 Phase 73 前端实现（对标 Phase 65/70/71 合约测试模式——静态分析 .tsx + i18n 验证）：
1. PromptManager.tsx 引入 Tabs 布局（Tabs/TabsList/TabsTrigger/TabsContent）
2. PromptManager.tsx 引入 PromptLibraryList 组件
3. PromptManager.tsx 含两个 tab（library + variables）
4. PromptLibraryList.tsx 导出 filterByQuery 纯函数
5. PromptLibraryList.tsx 含搜索框 + 分类树 + 内容区三栏布局
6. PromptLibraryList.tsx 引入 BehaviorSelector
7. BehaviorSelector.tsx 导出 groupBehaviorsByLanguage 纯函数
8. BehaviorSelector.tsx 含复选框 + 全选/清空 + 已选数量
9. 4 语言 i18n 含 promptLibrary.* keys
10. Vitest 文件存在（BehaviorSelector.test.ts + PromptLibraryList.test.ts）
"""
import os
import re

_SRC_FE = os.path.join(os.path.dirname(__file__), "..", "..", "src-frontend")
_PROMPT_MANAGER = os.path.join(_SRC_FE, "src", "pages", "PromptManager.tsx")
_LIBRARY_LIST = os.path.join(_SRC_FE, "src", "components", "prompts", "PromptLibraryList.tsx")
_BEHAVIOR_SELECTOR = os.path.join(_SRC_FE, "src", "components", "prompts", "BehaviorSelector.tsx")
_VITEST_BEHAVIOR = os.path.join(_SRC_FE, "src", "components", "__tests__", "BehaviorSelector.test.ts")
_VITEST_LIBRARY = os.path.join(_SRC_FE, "src", "components", "__tests__", "PromptLibraryList.test.ts")
_I18N_DIR = os.path.join(_SRC_FE, "src", "i18n", "locales")
_LOCALES = ["zh", "en", "ja", "ko"]


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── PromptManager.tsx Tabs 布局 ──

def test_prompt_manager_imports_tabs():
    """PromptManager.tsx 引入 Tabs 组件"""
    code = _read(_PROMPT_MANAGER)
    assert "Tabs" in code and "TabsList" in code and "TabsTrigger" in code and "TabsContent" in code, (
        "PromptManager.tsx 应引入 Tabs/TabsList/TabsTrigger/TabsContent"
    )


def test_prompt_manager_imports_prompt_library_list():
    """PromptManager.tsx 引入 PromptLibraryList 组件"""
    code = _read(_PROMPT_MANAGER)
    assert re.search(r"import\s+\{[^}]*PromptLibraryList[^}]*\}", code), (
        "PromptManager.tsx 应引入 PromptLibraryList 组件"
    )


def test_prompt_manager_has_two_tabs():
    """PromptManager.tsx 含两个 tab——library + variables"""
    code = _read(_PROMPT_MANAGER)
    assert 'value="library"' in code, "应含 TabsTrigger value='library'"
    assert 'value="variables"' in code, "应含 TabsTrigger value='variables'"


def test_prompt_manager_renders_prompt_library_list_in_library_tab():
    """PromptManager.tsx 在 library tab 中渲染 PromptLibraryList"""
    code = _read(_PROMPT_MANAGER)
    # 查找 TabsContent value="library" 内含 <PromptLibraryList
    assert re.search(r'TabsContent\s+value="library"[\s\S]*?<PromptLibraryList', code), (
        "library tab 应渲染 <PromptLibraryList />"
    )


# ── PromptLibraryList.tsx ──

def test_library_list_exports_filter_by_query():
    """PromptLibraryList.tsx 导出 filterByQuery 纯函数"""
    code = _read(_LIBRARY_LIST)
    assert re.search(r"export\s+function\s+filterByQuery", code), (
        "PromptLibraryList.tsx 应导出 filterByQuery() 函数供单元测试"
    )


def test_library_list_has_search_input():
    """PromptLibraryList.tsx 含搜索框"""
    code = _read(_LIBRARY_LIST)
    assert re.search(r'type="search"|placeholder=\{t\("promptLibrary\.searchPlaceholder"\)\}', code), (
        "应含搜索框（type=search 或 promptLibrary.searchPlaceholder placeholder）"
    )


def test_library_list_has_category_tree():
    """PromptLibraryList.tsx 含分类树（roles/behaviors/workflows）"""
    code = _read(_LIBRARY_LIST)
    assert "roles" in code and "behaviors" in code and "workflows" in code, (
        "应含三个分类：roles/behaviors/workflows"
    )


def test_library_list_imports_behavior_selector():
    """PromptLibraryList.tsx 引入 BehaviorSelector 组件"""
    code = _read(_LIBRARY_LIST)
    assert re.search(r"import\s+\{[^}]*BehaviorSelector[^}]*\}", code), (
        "应引入 BehaviorSelector 组件"
    )


def test_library_list_has_dag_preview():
    """PromptLibraryList.tsx 含 DAG 预览组件"""
    code = _read(_LIBRARY_LIST)
    assert "DagPreview" in code, "应含 DagPreview 组件用于工作流 DAG 预览"
    assert "nodes" in code and "edges" in code, "DAG 预览应含 nodes 和 edges"


# ── BehaviorSelector.tsx ──

def test_behavior_selector_exports_group_function():
    """BehaviorSelector.tsx 导出 groupBehaviorsByLanguage 纯函数"""
    code = _read(_BEHAVIOR_SELECTOR)
    assert re.search(r"export\s+function\s+groupBehaviorsByLanguage", code), (
        "BehaviorSelector.tsx 应导出 groupBehaviorsByLanguage() 函数供单元测试"
    )


def test_behavior_selector_has_checkboxes():
    """BehaviorSelector.tsx 含复选框（input type=checkbox）"""
    code = _read(_BEHAVIOR_SELECTOR)
    assert 'type="checkbox"' in code, "应含 <input type='checkbox'> 复选框"


def test_behavior_selector_has_select_all_and_clear():
    """BehaviorSelector.tsx 含全选/清空按钮"""
    code = _read(_BEHAVIOR_SELECTOR)
    assert "selectAll" in code, "应含 selectAll（全选）功能"
    assert "clearGroup" in code or "clear" in code.lower(), "应含 clear（清空）功能"


def test_behavior_selector_shows_selected_count():
    """BehaviorSelector.tsx 显示已选数量"""
    code = _read(_BEHAVIOR_SELECTOR)
    assert "selected" in code and "count" in code, "应显示已选数量（selected + count）"


# ── i18n 4 语言 ──

def test_i18n_locales_have_prompt_library_keys():
    """4 语言 i18n 含 promptLibrary.* keys"""
    required_keys = ["title", "searchPlaceholder", "tabLibrary", "tabVariables", "noResults"]
    for lang in _LOCALES:
        path = os.path.join(_I18N_DIR, f"{lang}.json")
        assert os.path.exists(path), f"locale 文件不存在: {lang}.json"
        code = _read(path)
        assert '"promptLibrary"' in code, f"{lang}.json 应含 promptLibrary 块"
        for key in required_keys:
            assert f'"{key}"' in code, f"{lang}.json promptLibrary 应含 '{key}' key"


def test_i18n_locales_have_categories_keys():
    """4 语言 i18n 含 promptLibrary.categories.{roles,behaviors,workflows}"""
    for lang in _LOCALES:
        path = os.path.join(_I18N_DIR, f"{lang}.json")
        code = _read(path)
        # 在 promptLibrary 块内查找 categories
        assert '"categories"' in code, f"{lang}.json 应含 categories 子块"
        assert '"roles"' in code, f"{lang}.json 应含 categories.roles"
        assert '"behaviors"' in code, f"{lang}.json 应含 categories.behaviors"
        assert '"workflows"' in code, f"{lang}.json 应含 categories.workflows"


def test_i18n_behaviors_have_select_all_and_clear():
    """4 语言 i18n 含 promptLibrary.behaviors.selectAll + clear"""
    for lang in _LOCALES:
        path = os.path.join(_I18N_DIR, f"{lang}.json")
        code = _read(path)
        assert '"selectAll"' in code, f"{lang}.json 应含 behaviors.selectAll"
        assert '"clear"' in code, f"{lang}.json 应含 behaviors.clear"


# ── Vitest 文件存在 ──

def test_vitest_behavior_selector_exists():
    """Vitest 文件 BehaviorSelector.test.ts 存在"""
    assert os.path.exists(_VITEST_BEHAVIOR), (
        "BehaviorSelector.test.ts 应存在——Phase 73 纯函数单元测试"
    )


def test_vitest_prompt_library_list_exists():
    """Vitest 文件 PromptLibraryList.test.ts 存在"""
    assert os.path.exists(_VITEST_LIBRARY), (
        "PromptLibraryList.test.ts 应存在——Phase 73 纯函数单元测试"
    )


def test_vitest_files_test_exported_functions():
    """Vitest 文件测试导出的纯函数"""
    behavior_code = _read(_VITEST_BEHAVIOR)
    assert "groupBehaviorsByLanguage" in behavior_code, (
        "BehaviorSelector.test.ts 应测试 groupBehaviorsByLanguage 函数"
    )
    library_code = _read(_VITEST_LIBRARY)
    assert "filterByQuery" in library_code, (
        "PromptLibraryList.test.ts 应测试 filterByQuery 函数"
    )
