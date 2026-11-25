"""tests/unit/test_hooks.py —— Hook 拦截链测试"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestHookRegistration:
    """Hook 注册"""

    def test_register_pre_tool_hook(self):
        """注册 PreToolUse Hook"""
        from hooks.registry import register_hook, HookType, _hooks
        _hooks[HookType.PRE_TOOL_USE].clear()

        @register_hook(HookType.PRE_TOOL_USE, r"write_file", "test hook")
        async def my_hook(tool_name, arguments, agent_id):
            return None

        assert len(_hooks[HookType.PRE_TOOL_USE]) == 1
        assert _hooks[HookType.PRE_TOOL_USE][0].description == "test hook"

    def test_register_multiple_hooks(self):
        """注册多个 Hook"""
        from hooks.registry import register_hook, HookType, _hooks
        _hooks[HookType.POST_TOOL_USE].clear()

        @register_hook(HookType.POST_TOOL_USE, r".*", "audit all")
        async def h1(tool_name, args, result, agent_id):
            pass

        @register_hook(HookType.POST_TOOL_USE, r"write_file", "audit write")
        async def h2(tool_name, args, result, agent_id):
            pass

        assert len(_hooks[HookType.POST_TOOL_USE]) == 2


class TestPreToolHooks:
    """PreToolUse Hook 执行"""

    async def test_hook_allows(self):
        """Hook 返回 None 时放行"""
        from hooks.registry import register_hook, run_pre_tool_hooks, HookType, _hooks
        _hooks[HookType.PRE_TOOL_USE].clear()

        @register_hook(HookType.PRE_TOOL_USE, r"test_tool", "allow")
        async def allow_hook(tool_name, arguments, agent_id):
            return None

        result = await run_pre_tool_hooks("test_tool", {}, "hermes")
        assert result is None

    async def test_hook_blocks(self):
        """Hook 返回 block=True 时拦截"""
        from hooks.registry import register_hook, run_pre_tool_hooks, HookType, _hooks
        _hooks[HookType.PRE_TOOL_USE].clear()

        @register_hook(HookType.PRE_TOOL_USE, r"bad_tool", "block")
        async def block_hook(tool_name, arguments, agent_id):
            return {"block": True, "reason": "测试拦截"}

        result = await run_pre_tool_hooks("bad_tool", {}, "hermes")
        assert result is not None
        assert result["block"] is True
        assert "拦截" in result["reason"]

    async def test_hook_pattern_mismatch_skips(self):
        """不匹配的 Hook 被跳过"""
        from hooks.registry import register_hook, run_pre_tool_hooks, HookType, _hooks
        _hooks[HookType.PRE_TOOL_USE].clear()

        @register_hook(HookType.PRE_TOOL_USE, r"write_file", "write only")
        async def write_hook(tool_name, arguments, agent_id):
            return {"block": True, "reason": "no"}

        result = await run_pre_tool_hooks("read_file", {}, "hermes")
        assert result is None

    async def test_hook_exception_allows(self):
        """Hook 抛异常时放行（不阻断工具执行）"""
        from hooks.registry import register_hook, run_pre_tool_hooks, HookType, _hooks
        _hooks[HookType.PRE_TOOL_USE].clear()

        @register_hook(HookType.PRE_TOOL_USE, r".*", "broken")
        async def broken_hook(tool_name, arguments, agent_id):
            raise RuntimeError("hook crash")

        result = await run_pre_tool_hooks("any_tool", {}, "hermes")
        assert result is None  # 不阻断

    async def test_system_path_blocked(self):
        """内置 hook 阻止系统目录写入（注册后验证）"""
        from hooks.registry import register_hook, run_pre_tool_hooks, HookType, _hooks
        # 先清理，再注册系统路径防护 hook
        _hooks[HookType.PRE_TOOL_USE].clear()

        @register_hook(HookType.PRE_TOOL_USE, r"write_file", "敏感路径防护")
        async def block_sys(tool_name, arguments, agent_id):
            path = arguments.get("path", "")
            if path.startswith("C:\\Windows"):
                return {"block": True, "reason": f"禁止写入系统目录: {path}"}
            return None

        result = await run_pre_tool_hooks("write_file", {"path": "C:\\Windows\\exploit.exe"}, "hermes")
        assert result is not None
        assert result["block"] is True
        assert "系统目录" in result["reason"]


class TestPostToolHooks:
    """PostToolUse Hook 执行"""

    async def test_post_hook_runs(self):
        """PostToolUse Hook 被调用"""
        from hooks.registry import register_hook, run_post_tool_hooks, HookType, _hooks
        _hooks[HookType.POST_TOOL_USE].clear()

        called = []

        @register_hook(HookType.POST_TOOL_USE, r".*", "counter")
        async def counter(tool_name, args, result, agent_id):
            called.append(tool_name)

        await run_post_tool_hooks("read_file", {"path": "x"}, {"action": "read"}, "hermes")
        assert len(called) == 1
        assert called[0] == "read_file"


class TestListHooks:
    """list_hooks 调试工具"""

    def test_list_returns_entries(self):
        from hooks.registry import register_hook, list_hooks, HookType, _hooks
        _hooks[HookType.PRE_TOOL_USE].clear()
        _hooks[HookType.POST_TOOL_USE].clear()

        @register_hook(HookType.PRE_TOOL_USE, r"a", "hook a")
        async def ha(tool_name, args, agent_id):
            pass

        @register_hook(HookType.POST_TOOL_USE, r"b", "hook b")
        async def hb(tool_name, args, result, agent_id):
            pass

        entries = list_hooks()
        assert len(entries) >= 2
        types = {e["type"] for e in entries}
        assert "pre_tool_use" in types
        assert "post_tool_use" in types
