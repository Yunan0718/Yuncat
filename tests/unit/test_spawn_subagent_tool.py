"""tests/unit/test_spawn_subagent_tool.py —— Task 7.8 暴露 spawn_subagent 为工具

覆盖：
1. spawn_subagent 工具注册（name/parameters/agents/confirmable）
2. 工具调用 AgentService.spawn_subagent 并传递正确参数
3. 递归深度限制（max_depth=3，超过则拒绝）
4. 深度传播（子 Agent 继承递增的 depth，通过 ContextVar + asyncio.create_task）
5. 错误处理（AgentService 不可用 / spawn 失败）
6. 默认 agent_id（未指定时用 DEFAULT_AGENT_ID）

对标 Claude Code Task tool：
  - LLM 可主动 spawn 子 Agent 处理独立子任务
  - 子 Agent 有独立上下文窗口（不污染父对话）
  - 递归深度限制防止失控（Claude Code max_depth=3-4）
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from config import MAX_SUBAGENT_DEPTH, DEFAULT_AGENT_ID
from context import get_subagent_depth, set_subagent_depth, reset_subagent_depth


# ═══════════════════════════════════════════════════════════════
# 工具注册测试
# ═══════════════════════════════════════════════════════════════

def _ensure_spawn_subagent_registered() -> None:
    """确保 spawn_subagent 已注册——消除本文件的**导入顺序依赖**。

    `spawn_subagent` 由 `tools/builtin/system_tool_config.py` 的装饰器在**首次导入**时
    注册到 tools.registry。若别的测试文件先导入过该模块（或因故清空了注册表），
    本文件再 `import` 就是 no-op，工具不会补注册 —— 症状是
    **单独跑通过、全量跑失败**（实测 6 条用例在全簇中失败、单跑全绿）。
    这里显式 reload 一次，让结果不再取决于其他测试的导入顺序。
    """
    from tools.registry import get_tool

    import tools.builtin.system_tool_config as _tool_module

    if get_tool("spawn_subagent") is None:
        import importlib

        importlib.reload(_tool_module)


@pytest.fixture(autouse=True)
def _registered_spawn_subagent():
    """本文件所有用例执行前保证工具已注册。"""
    _ensure_spawn_subagent_registered()
    yield


class TestSpawnSubagentToolRegistration:
    """Task 7.8: spawn_subagent 应注册为工具。"""

    def test_tool_registered_in_registry(self):
        """spawn_subagent 应在 tools/registry 中注册。"""
        from tools.registry import get_tool
        # 触发模块加载
        import tools.builtin.system_tool_config  # noqa: F401
        tm = get_tool("spawn_subagent")
        assert tm is not None, "spawn_subagent tool must be registered"

    def test_tool_agents_includes_hermes_and_openclaw(self):
        """工具应同时注册给 hermes 和 openclaw。"""
        from tools.registry import get_tool
        import tools.builtin.system_tool_config  # noqa: F401
        tm = get_tool("spawn_subagent")
        assert tm is not None
        assert "hermes" in tm.agents
        assert "openclaw" in tm.agents

    def test_tool_has_prompt_parameter(self):
        """工具参数应包含 prompt（必填）。"""
        from tools.registry import get_tool
        import tools.builtin.system_tool_config  # noqa: F401
        tm = get_tool("spawn_subagent")
        assert tm is not None
        props = tm.parameters.get("properties", {})
        assert "prompt" in props
        required = tm.parameters.get("required", [])
        assert "prompt" in required

    def test_tool_has_optional_agent_id_parameter(self):
        """工具参数应包含 agent_id（可选）。"""
        from tools.registry import get_tool
        import tools.builtin.system_tool_config  # noqa: F401
        tm = get_tool("spawn_subagent")
        assert tm is not None
        props = tm.parameters.get("properties", {})
        assert "agent_id" in props
        required = tm.parameters.get("required", [])
        assert "agent_id" not in required

    def test_tool_has_optional_context_summary_parameter(self):
        """工具参数应包含 context_summary（可选）。"""
        from tools.registry import get_tool
        import tools.builtin.system_tool_config  # noqa: F401
        tm = get_tool("spawn_subagent")
        assert tm is not None
        props = tm.parameters.get("properties", {})
        assert "context_summary" in props

    def test_tool_is_confirmable(self):
        """spawn_subagent 应 confirmable=True（启动子 Agent 是有副作用的操作）。"""
        from tools.registry import get_tool
        import tools.builtin.system_tool_config  # noqa: F401
        tm = get_tool("spawn_subagent")
        assert tm is not None
        assert tm.confirmable is True


# ═══════════════════════════════════════════════════════════════
# 递归深度限制测试
# ═══════════════════════════════════════════════════════════════

class TestSubagentDepthTracking:
    """Task 7.8: ContextVar 追踪递归深度。"""

    def test_get_subagent_depth_defaults_to_zero(self):
        """默认深度为 0（主 Agent）。"""
        # 确保没有被前序测试污染
        token = set_subagent_depth(0)
        try:
            assert get_subagent_depth() == 0
        finally:
            reset_subagent_depth(token)

    def test_set_and_get_subagent_depth(self):
        """设置和读取深度。"""
        token = set_subagent_depth(2)
        try:
            assert get_subagent_depth() == 2
        finally:
            reset_subagent_depth(token)

    def test_reset_restores_previous_depth(self):
        """reset 恢复之前的深度值（ContextVar token 语义）。"""
        token = set_subagent_depth(1)
        assert get_subagent_depth() == 1
        token2 = set_subagent_depth(3)
        assert get_subagent_depth() == 3
        reset_subagent_depth(token2)  # 恢复到 token2 创建时的值（1）
        assert get_subagent_depth() == 1
        reset_subagent_depth(token)   # 恢复到 token 创建时的值（0）

    async def test_depth_propagates_to_child_task(self):
        """ContextVar 通过 asyncio.create_task 传播到子任务。"""
        token = set_subagent_depth(2)
        try:
            # 模拟 spawn_subagent 内部创建后台任务
            child_depth_holder = []

            async def child():
                child_depth_holder.append(get_subagent_depth())

            task = asyncio.create_task(child())
            await task
            assert child_depth_holder == [2], "child task should inherit parent's depth"
        finally:
            reset_subagent_depth(token)


# ═══════════════════════════════════════════════════════════════
# 工具执行 + 深度限制测试
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def clean_depth():
    """每个测试前后清理深度 ContextVar。"""
    token = set_subagent_depth(0)
    yield
    reset_subagent_depth(token)


@pytest.fixture
def mock_agent_service():
    """Mock AgentService.spawn_subagent。"""
    mock_svc = MagicMock()
    mock_svc.spawn_subagent = AsyncMock(return_value={
        "ok": True,
        "task_id": "fake-task-id",
        "output": "Sub-agent completed successfully",
    })
    with patch("context.get_agent_service", return_value=mock_svc):
        yield mock_svc


class TestSpawnSubagentToolExecution:
    """Task 7.8: spawn_subagent 工具执行逻辑。"""

    async def test_successful_spawn_calls_agent_service(self, clean_depth, mock_agent_service):
        """成功调用时，应调用 AgentService.spawn_subagent。"""
        from tools.builtin.system_tool_config import spawn_subagent_tool
        result = await spawn_subagent_tool(prompt="Analyze code quality")
        assert result["success"] is True
        mock_agent_service.spawn_subagent.assert_called_once()
        call_kwargs = mock_agent_service.spawn_subagent.call_args
        assert call_kwargs.kwargs.get("prompt") == "Analyze code quality"

    async def test_default_agent_id_when_not_specified(self, clean_depth, mock_agent_service):
        """未指定 agent_id 时，应用 DEFAULT_AGENT_ID。"""
        from tools.builtin.system_tool_config import spawn_subagent_tool
        await spawn_subagent_tool(prompt="Test task")
        call_kwargs = mock_agent_service.spawn_subagent.call_args
        assert call_kwargs.kwargs.get("agent_id") == DEFAULT_AGENT_ID

    async def test_explicit_agent_id_passed_through(self, clean_depth, mock_agent_service):
        """显式指定 agent_id 时，应传递给 AgentService。"""
        from tools.builtin.system_tool_config import spawn_subagent_tool
        await spawn_subagent_tool(prompt="Test task", agent_id="openclaw")
        call_kwargs = mock_agent_service.spawn_subagent.call_args
        assert call_kwargs.kwargs.get("agent_id") == "openclaw"

    async def test_context_summary_passed_through(self, clean_depth, mock_agent_service):
        """context_summary 应传递给 AgentService。"""
        from tools.builtin.system_tool_config import spawn_subagent_tool
        await spawn_subagent_tool(
            prompt="Test task",
            context_summary="Previous context: ...",
        )
        call_kwargs = mock_agent_service.spawn_subagent.call_args
        assert call_kwargs.kwargs.get("context_summary") == "Previous context: ..."

    async def test_returns_spawn_result(self, clean_depth, mock_agent_service):
        """应返回 AgentService.spawn_subagent 的结果（ToolResult 格式）。"""
        from tools.builtin.system_tool_config import spawn_subagent_tool
        result = await spawn_subagent_tool(prompt="Test")
        assert result["success"] is True
        assert result["data"]["task_id"] == "fake-task-id"
        assert "output" in result["data"]


# ═══════════════════════════════════════════════════════════════
# 深度限制执行测试
# ═══════════════════════════════════════════════════════════════

class TestSpawnSubagentDepthLimit:
    """Task 7.8: 超过 MAX_SUBAGENT_DEPTH 时拒绝。"""

    async def test_depth_zero_allowed(self, clean_depth, mock_agent_service):
        """深度 0（主 Agent）允许 spawn。"""
        assert get_subagent_depth() == 0
        from tools.builtin.system_tool_config import spawn_subagent_tool
        result = await spawn_subagent_tool(prompt="Test")
        assert result["success"] is True
        mock_agent_service.spawn_subagent.assert_called_once()

    async def test_depth_below_max_allowed(self, clean_depth, mock_agent_service):
        """深度 < MAX_SUBAGENT_DEPTH 时允许。"""
        token = set_subagent_depth(MAX_SUBAGENT_DEPTH - 1)
        try:
            from tools.builtin.system_tool_config import spawn_subagent_tool
            result = await spawn_subagent_tool(prompt="Test")
            assert result["success"] is True
            mock_agent_service.spawn_subagent.assert_called_once()
        finally:
            reset_subagent_depth(token)

    async def test_depth_at_max_refused(self, clean_depth, mock_agent_service):
        """深度 = MAX_SUBAGENT_DEPTH 时拒绝（已达上限）。"""
        token = set_subagent_depth(MAX_SUBAGENT_DEPTH)
        try:
            from tools.builtin.system_tool_config import spawn_subagent_tool
            result = await spawn_subagent_tool(prompt="Test")
            assert result["success"] is False
            assert "depth" in result.get("error", "").lower() or "递归" in result.get("error", "")
            mock_agent_service.spawn_subagent.assert_not_called()
        finally:
            reset_subagent_depth(token)

    async def test_depth_above_max_refused(self, clean_depth, mock_agent_service):
        """深度 > MAX_SUBAGENT_DEPTH 时拒绝。"""
        token = set_subagent_depth(MAX_SUBAGENT_DEPTH + 1)
        try:
            from tools.builtin.system_tool_config import spawn_subagent_tool
            result = await spawn_subagent_tool(prompt="Test")
            assert result["success"] is False
            mock_agent_service.spawn_subagent.assert_not_called()
        finally:
            reset_subagent_depth(token)

    async def test_spawn_increments_depth_for_child(self, clean_depth, mock_agent_service):
        """spawn 后子 Agent 应看到递增的深度（通过 ContextVar 传播）。"""
        # 模拟 AgentService.spawn_subagent 内部创建后台任务
        child_depths = []

        async def mock_spawn(**kwargs):
            # 模拟 start_task → asyncio.create_task
            async def child_task():
                child_depths.append(get_subagent_depth())
            child = asyncio.create_task(child_task())
            await child
            return {"ok": True, "task_id": "test", "output": "done"}

        mock_agent_service.spawn_subagent = AsyncMock(side_effect=mock_spawn)

        from tools.builtin.system_tool_config import spawn_subagent_tool
        # 主 Agent 深度 0 → 子 Agent 应看到深度 1
        await spawn_subagent_tool(prompt="Test")
        assert child_depths == [1], f"child should see depth=1, got {child_depths}"


# ═══════════════════════════════════════════════════════════════
# 错误处理测试
# ═══════════════════════════════════════════════════════════════

class TestSpawnSubagentErrorHandling:
    """Task 7.8: 错误处理。"""

    async def test_agent_service_unavailable(self, clean_depth):
        """AgentService 不可用时返回错误。"""
        with patch("context.get_agent_service", return_value=None):
            from tools.builtin.system_tool_config import spawn_subagent_tool
            result = await spawn_subagent_tool(prompt="Test")
            assert result["success"] is False
            assert "error" in result

    async def test_spawn_failure_propagated(self, clean_depth):
        """AgentService.spawn_subagent 失败时传播错误。"""
        mock_svc = MagicMock()
        mock_svc.spawn_subagent = AsyncMock(return_value={
            "ok": False, "error": "Sub-agent timeout (120s)",
        })
        with patch("context.get_agent_service", return_value=mock_svc):
            from tools.builtin.system_tool_config import spawn_subagent_tool
            result = await spawn_subagent_tool(prompt="Test")
            assert result["success"] is False
            assert "timeout" in result["error"].lower()

    async def test_spawn_exception_returns_error(self, clean_depth):
        """AgentService.spawn_subagent 抛异常时返回错误而非崩溃。"""
        mock_svc = MagicMock()
        mock_svc.spawn_subagent = AsyncMock(side_effect=RuntimeError("DB error"))
        with patch("context.get_agent_service", return_value=mock_svc):
            from tools.builtin.system_tool_config import spawn_subagent_tool
            result = await spawn_subagent_tool(prompt="Test")
            assert result["success"] is False
            assert "error" in result
