"""contract: agent 元数据 drift 修复 —— 原生 agent 工具门与档案身份

背景（2026-08-31 发现）：DEFAULT_AGENT_ID 从 "hermes" 漂移为 "yuncat" 后——
1. tool_service 按 tool.agents 校验执行权限，而内置工具的 agents 列表里没有
   "yuncat" → 原生 agent 的全部内置工具调用 PermissionError，被 loop 的
   except 吞成 {"error": ...}，静默失败（实测 list_dir/read_file/run_command 全灭）。
2. registry 把 OC 网关工具注册成 agents=[DEFAULT_AGENT_ID]，注释写「仅 hermes
   可见」→ 实际误挂给 yuncat。
3. capability_router 的档案 agent_id=DEFAULT_AGENT_ID + name="Hermes"，身份错位。

本文件锁住修复后的语义：原生 agent 是受信宿主（可见/可执行全部工具），
外部 agent 仍按各自 agents 列表严格把关。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from config import DEFAULT_AGENT_ID, HERMES_AGENT_ID  # noqa: E402


class _FakeAdapter:
    """最小 adapter 桩——满足 build_profiles 的 duck typing。"""

    def capabilities(self):
        return None

    async def list_available_tools(self):
        return []


def _load_all_builtin_tools() -> None:
    """导入全部内置工具模块，触发 @tool 注册（与生产 lifespan 行为对齐）。"""
    import importlib
    import pkgutil

    import tools.builtin as _b
    for m in pkgutil.iter_modules(_b.__path__):
        try:
            importlib.import_module("tools.builtin." + m.name)
        except Exception:
            pass


# ---- 执行门（tool_service.py:215 的原生豁免）----

@pytest.mark.asyncio
async def test_native_agent_can_execute_builtin_tools():
    """P0 回归：原生 yuncat agent 必须能执行内置工具。

    修复前：PermissionError('Agent yuncat not allowed: list_dir')，
    被 agent_tool_loop 的 except 吞成 {"error": ...}——app 不崩但原生 agent
    什么都做不了。
    """
    _load_all_builtin_tools()
    from services.tools.tool_service import ToolService

    ts = ToolService()
    result = await ts.execute("list_dir", {"path": "."}, agent_id=DEFAULT_AGENT_ID)
    assert result.get("success") is True, (
        "原生 agent 执行内置工具被拒——drift 回归："
        f"{result.get('error', '')[:120]}"
    )


@pytest.mark.asyncio
async def test_native_agent_can_run_safe_command():
    """原生 agent 的 run_command 也必须可用（修前同样被拒）。"""
    _load_all_builtin_tools()
    from services.tools.tool_service import ToolService

    ts = ToolService()
    result = await ts.execute("run_command", {"command": "echo drift-check"},
                              agent_id=DEFAULT_AGENT_ID)
    assert result.get("success") is True


@pytest.mark.asyncio
async def test_external_agent_still_gated():
    """外部 agent 不在工具 agents 列表内 → 仍必须拒绝（豁免仅限原生）。"""
    _load_all_builtin_tools()
    from services.tools.tool_service import ToolService

    ts = ToolService()
    with pytest.raises(PermissionError):
        await ts.execute("list_dir", {"path": "."}, agent_id="codex")


@pytest.mark.asyncio
async def test_hermes_still_allowed_for_builtin_tools():
    """hermes 在内置工具 agents 列表内 → 不受影响。"""
    _load_all_builtin_tools()
    from services.tools.tool_service import ToolService

    ts = ToolService()
    result = await ts.execute("list_dir", {"path": "."}, agent_id=HERMES_AGENT_ID)
    assert result.get("success") is True


# ---- 可见性（registry.list_tools）----

def test_native_agent_sees_all_tools():
    """原生 agent 必须可见全部工具（修前只剩 6 个 OC 工具、0 个内置）。"""
    _load_all_builtin_tools()
    from tools.registry import get_all_tools, list_tools

    # 先调用 list_tools 触发 OC 工具懒加载，再取注册表总数，二者才可比
    visible = list_tools(agent_id=DEFAULT_AGENT_ID)
    total = len(get_all_tools())
    assert len(visible) == total, (
        f"原生 agent 工具可见性不全：{len(visible)}/{total}"
    )
    names = {t["name"] for t in visible}
    assert "search_files" in names and "list_dir" in names


def test_oc_tools_declared_for_hermes_not_default():
    """OC 网关工具必须登记给 hermes（注释本意），不得跟随 DEFAULT_AGENT_ID 漂移。"""
    _load_all_builtin_tools()
    from tools.registry import get_all_tools, list_tools

    list_tools(agent_id=DEFAULT_AGENT_ID)  # 触发 OC 工具懒加载
    oc_meta = [t for name, t in get_all_tools().items() if name.startswith("openclaw_")]
    assert oc_meta, "OC 工具未注册"
    for meta in oc_meta:
        assert meta.agents == [HERMES_AGENT_ID], (
            f"{meta.name} agents={meta.agents}，应为 [HERMES_AGENT_ID]"
        )


# ---- capability_router 档案身份 ----

@pytest.mark.asyncio
async def test_profiles_native_named_yuncat_not_hermes():
    """原生档案必须是 (yuncat, 'Yuncat')，不得再出现 (yuncat, 'Hermes') 错位体。"""
    from services.capability_router import build_profiles

    adapters = {
        DEFAULT_AGENT_ID: _FakeAdapter(),
        HERMES_AGENT_ID: _FakeAdapter(),
        "openclaw": _FakeAdapter(),
    }
    profiles = await build_profiles(adapters)

    native = [p for p in profiles if p.agent_id == DEFAULT_AGENT_ID]
    assert native, "缺少原生 agent 档案"
    assert all(p.name == "Yuncat" for p in native), (
        f"原生档案名错位：{[p.name for p in native]}"
    )

    # 2026-09-10: hermes 恢复为外部网关 agent 后**不进入**能力路由档案——
    # 外部 agent（openclaw 视觉/搜索档除外）禁自动路由，仅用户显式选择时调用。
    hermes = [p for p in profiles if p.agent_id == HERMES_AGENT_ID]
    assert not hermes, (
        "外部 agent hermes 不应出现在能力路由候选档案中（禁自动路由）"
    )

    drifted = [p for p in profiles if p.agent_id == DEFAULT_AGENT_ID and p.name == "Hermes"]
    assert not drifted, "drift 错位体回归：存在 (yuncat, 'Hermes') 档案"


@pytest.mark.asyncio
async def test_native_profile_lists_builtin_tools():
    """原生档案的工具列表必须包含内置工具（修前只有 6 个 OC 工具）。"""
    _load_all_builtin_tools()
    from services.capability_router import build_profiles

    profiles = await build_profiles({DEFAULT_AGENT_ID: _FakeAdapter()})
    native = [p for p in profiles if p.agent_id == DEFAULT_AGENT_ID]
    assert native
    tool_names = {t.get("name", "") for t in native[0].tools}
    assert "list_dir" in tool_names or "search_files" in tool_names, (
        f"原生档案缺内置工具，仅有：{sorted(tool_names)[:10]}"
    )
