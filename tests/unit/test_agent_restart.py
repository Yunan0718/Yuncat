"""tests/unit/test_agent_restart.py —— Task 6.15: Agent restart 端点 + start() 重置 _stopping

覆盖：
1. BaseProcessAdapter.start() 重置 _stopping=False（修复 stop 后 start watchdog 不重启的 bug）
2. POST /api/v1/agents/{agent_id}/restart 调用 stop_agent + start_agent
3. restart 顺序：stop 先于 start
4. restart 返回 {"status": "ok"}
5. restart 不存在的 agent——不报错（与 start/stop 端点行为一致）
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src-python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── 测试 1: BaseProcessAdapter.start() 重置 _stopping ──

async def _async_noop():
    pass


def _sync_noop():
    pass


class TestStartResetsStopping:
    """Task 6.15: start() 必须重置 _stopping=False

    Bug 场景：stop() 设 _stopping=True 阻止 watchdog 重启。如果 start() 不重置，
    stop 后 start 的新 watchdog task 在进程崩溃时检查 _stopping=True 会直接 return，
    不重启——子进程崩溃后永久不可用。
    """

    @pytest.mark.asyncio
    async def test_start_resets_stopping_flag(self):
        """start() 后 _stopping 必须为 False"""
        from adapters.base import BaseProcessAdapter

        # 创建 BaseProcessAdapter 实例，mock 掉子进程相关方法
        adapter = BaseProcessAdapter.__new__(BaseProcessAdapter)
        adapter.agent_id = "test"
        adapter._ready = asyncio.Event()
        adapter._stopping = True  # 模拟 stop() 后的状态
        adapter._watchdog_task = None
        adapter._read_stdout_task = None
        adapter._read_stderr_task = None
        # __new__ 绕过 __init__，需手动补齐 start() 依赖的属性
        adapter._start_lock = asyncio.Lock()
        adapter.process = None

        # mock 掉会启动真实子进程的方法
        async def _fake_spawn():
            adapter._ready.set()
        adapter._spawn_and_wait = _fake_spawn
        adapter._watchdog = _async_noop
        adapter._read_stdout = _async_noop
        adapter._read_stderr = _async_noop
        adapter._attach_task_callbacks = _sync_noop

        # 验证 start 前 _stopping=True
        assert adapter._stopping is True

        await adapter.start()

        # 核心断言：start() 重置 _stopping=False
        assert adapter._stopping is False
        assert adapter._ready.is_set()

    @pytest.mark.asyncio
    async def test_start_with_clean_state_keeps_stopping_false(self):
        """start() 在 _stopping=False 时保持 False（不误设为 True）"""
        from adapters.base import BaseProcessAdapter

        adapter = BaseProcessAdapter.__new__(BaseProcessAdapter)
        adapter.agent_id = "test"
        adapter._ready = asyncio.Event()
        adapter._stopping = False  # 初始干净状态
        adapter._watchdog_task = None
        adapter._read_stdout_task = None
        adapter._read_stderr_task = None
        # __new__ 绕过 __init__，需手动补齐 start() 依赖的属性
        adapter._start_lock = asyncio.Lock()
        adapter.process = None

        async def _fake_spawn():
            adapter._ready.set()
        adapter._spawn_and_wait = _fake_spawn
        adapter._watchdog = _async_noop
        adapter._read_stdout = _async_noop
        adapter._read_stderr = _async_noop
        adapter._attach_task_callbacks = _sync_noop

        await adapter.start()
        assert adapter._stopping is False


# ── 测试 2-5: POST /api/v1/agents/{agent_id}/restart 端点 ──

class _MockAgentService:
    """模拟 AgentService——记录 stop/start 调用顺序"""
    def __init__(self, adapters=None):
        self.adapters = adapters or {}
        self.calls: list[str] = []  # 记录调用顺序：["stop", "start"]

    async def stop_agent(self, agent_id: str):
        self.calls.append("stop")

    async def start_agent(self, agent_id: str):
        self.calls.append("start")


class _MockApp:
    def __init__(self, agent_service=None):
        self.state = types.SimpleNamespace(agent_service=agent_service)


class _MockRequest:
    def __init__(self, app):
        self.app = app


# 导入被测函数
from routes.agents import restart_agent  # noqa: E402


class TestRestartEndpoint:
    """Task 6.15: POST /api/v1/agents/{agent_id}/restart"""

    @pytest.mark.asyncio
    async def test_restart_calls_stop_then_start(self):
        """restart 端点调用 stop_agent + start_agent"""
        svc = _MockAgentService(adapters={"hermes": object()})
        app = _MockApp(agent_service=svc)
        request = _MockRequest(app=app)

        result = await restart_agent("hermes", request)

        # 验证调用顺序：stop 先于 start
        assert svc.calls == ["stop", "start"]
        assert result == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_restart_returns_ok(self):
        """restart 返回 {"status": "ok"}"""
        svc = _MockAgentService(adapters={"hermes": object()})
        app = _MockApp(agent_service=svc)
        request = _MockRequest(app=app)

        result = await restart_agent("hermes", request)
        assert result == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_restart_stop_before_start(self):
        """确保 stop 在 start 之前完成（restart = stop + start 原子序列）"""
        svc = _MockAgentService(adapters={"hermes": object()})
        app = _MockApp(agent_service=svc)
        request = _MockRequest(app=app)

        await restart_agent("hermes", request)

        # stop 必须在 start 之前
        stop_idx = svc.calls.index("stop")
        start_idx = svc.calls.index("start")
        assert stop_idx < start_idx

    @pytest.mark.asyncio
    async def test_restart_unknown_agent_no_error(self):
        """restart 不存在的 agent 不报错（与 start/stop 端点行为一致）"""
        svc = _MockAgentService(adapters={})  # 无 adapter
        app = _MockApp(agent_service=svc)
        request = _MockRequest(app=app)

        # 不应抛异常——start_agent/stop_agent 内部对 None adapter 静默跳过
        result = await restart_agent("ghost", request)
        assert result == {"status": "ok"}
