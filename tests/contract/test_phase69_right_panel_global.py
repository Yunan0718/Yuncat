"""tests/contract/test_phase69_right_panel_global.py —— Phase 69 RightContextPanel 全局化合约测试

验证：
1. appStore.ts 定义 rightCollapsed / toggleRightPanel / setRightCollapsed
2. App.tsx 全局渲染 RightContextPanel（从 store 读 rightCollapsed）
3. ChatView.tsx 不再渲染 RightContextPanel（已上提）
4. ChatView.tsx 从 store 读 rightCollapsed（非局部 state）
5. localStorage key 为 yuncat-right-collapsed
"""
import os
import re

_SRC_FE = os.path.join(os.path.dirname(__file__), "..", "..", "src-frontend")
_APP_STORE = os.path.join(_SRC_FE, "src", "store", "appStore.ts")
_APP_TSX = os.path.join(_SRC_FE, "src", "App.tsx")
_CHAT_VIEW = os.path.join(_SRC_FE, "src", "components", "chat", "ChatView.tsx")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_app_store_defines_right_collapsed():
    """appStore.ts 定义 rightCollapsed / toggleRightPanel / setRightCollapsed"""
    code = _read(_APP_STORE)
    assert "rightCollapsed:" in code, "appStore 应定义 rightCollapsed state"
    assert "toggleRightPanel:" in code or "toggleRightPanel " in code, "appStore 应定义 toggleRightPanel action"
    assert "setRightCollapsed:" in code or "setRightCollapsed " in code, "appStore 应定义 setRightCollapsed action"


def test_app_store_persists_to_local_storage():
    """appStore 将 rightCollapsed 持久化到 localStorage"""
    code = _read(_APP_STORE)
    assert "yuncat-right-collapsed" in code, "rightCollapsed 应持久化到 localStorage key 'yuncat-right-collapsed'"


def test_app_tsx_renders_right_context_panel():
    """App.tsx 全局渲染 RightContextPanel"""
    code = _read(_APP_TSX)
    assert "RightContextPanel" in code, "App.tsx 应导入并渲染 RightContextPanel"
    # 应从 appStore 读 rightCollapsed
    assert "rightCollapsed" in code, "App.tsx 应从 appStore 读 rightCollapsed"
    # 应条件渲染（rightCollapsed 为 false 时显示）
    assert re.search(r"!\s*rightCollapsed\s*&&", code) or re.search(r"rightCollapsed\s*===\s*false", code), (
        "App.tsx 应在 rightCollapsed 为 false 时渲染 RightContextPanel"
    )


def test_app_tsx_renders_panel_outside_routes():
    """App.tsx 中 RightContextPanel 应在 <Routes> 之外（全局可见）"""
    code = _read(_APP_TSX)
    # RightContextPanel 应出现在 </main> 之后（主列外，全局位置）
    main_close_pos = code.rfind("</main>")
    # 找渲染位置：<RightContextPanel（JSX 标签，非 import 非 注释）
    render_match = re.search(r"<RightContextPanel\b", code)
    assert render_match, "App.tsx 应渲染 <RightContextPanel> JSX 标签"
    render_pos = render_match.start()
    assert render_pos > main_close_pos, (
        f"RightContextPanel 应在 </main> 之后渲染（全局位置，非路由内），"
        f"实际位置 {render_pos} <= </main> 位置 {main_close_pos}"
    )


def test_chat_view_no_longer_renders_right_context_panel():
    """ChatView.tsx 不再渲染 RightContextPanel（已上提到 App.tsx）"""
    code = _read(_CHAT_VIEW)
    # 不应 import RightContextPanel
    assert "import { RightContextPanel }" not in code, (
        "ChatView.tsx 不应再 import RightContextPanel（已全局化到 App.tsx）"
    )
    # 不应渲染 <RightContextPanel
    assert "<RightContextPanel" not in code, (
        "ChatView.tsx 不应再渲染 <RightContextPanel>（已全局化到 App.tsx）"
    )


def test_chat_view_reads_right_collapsed_from_store():
    """ChatView.tsx 从 appStore 读 rightCollapsed（非局部 state）"""
    code = _read(_CHAT_VIEW)
    # 应使用 useAppStore 读取 rightCollapsed——匹配 useAppStore((s) => ... rightCollapsed ...)
    # 注意箭头函数中含 )，故用 [\s\S]*? 跨字符匹配
    assert re.search(r"useAppStore\([\s\S]*?rightCollapsed", code), (
        "ChatView.tsx 应从 useAppStore 读 rightCollapsed"
    )
    # 不应有局部 useState 的 rightCollapsed
    # 匹配 "const [rightCollapsed, setRightCollapsed] = useState" 模式
    local_state_pattern = r"const\s+\[rightCollapsed,\s*setRightCollapsed\]\s*=\s*useState"
    assert not re.search(local_state_pattern, code), (
        "ChatView.tsx 不应再用 useState 管理 rightCollapsed（应从 appStore 读）"
    )


def test_chat_view_uses_toggle_right_panel():
    """ChatView.tsx 使用 store 的 toggleRightPanel"""
    code = _read(_CHAT_VIEW)
    assert "toggleRightPanel" in code, (
        "ChatView.tsx 应使用 appStore 的 toggleRightPanel action"
    )
