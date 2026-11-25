"""tests/unit/test_force_pause.py —— Task 6.13: 强制暂停中断当前子任务

覆盖 routes/tasks.py::pause_task 的 force 参数：
1. force=false（默认）——不 terminate adapter 子进程
2. force=true——terminate adapter 子进程
3. force=true adapter 进程已退出——不报错
4. force=true task 不存在——不报错
5. force=true 无 adapter——不报错
6. WS 消息含 force 标志
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


# ── Mock ──

class _MockProcess:
    """模拟 asyncio.subprocess.Process"""
    def __init__(self, returncode=None, pid=12345):
        self.returncode = returncode  # None = running
        self.pid = pid
        self.terminated = False

    def terminate(self):
        if self.returncode is None:
            self.terminated = True
            self.returncode = -15  # SIGTERM


class _MockAdapter:
    """模拟 BaseProcessAdapter"""
    def __init__(self, process=None):
        self.process = process
        self.agent_id = "hermes"


class _MockInternalTask:
    """模拟 asyncio.Task"""
    def __init__(self, done=False):
        self._done = done
        self.cancelled = False

    def done(self):
        return self._done

    def cancel(self):
        self.cancelled = True


class _MockDB:
    """模拟 aiosqlite 连接"""
    def __init__(self):
        self.queries: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, params: tuple = ()):
        self.queries.append((sql, params))

    async def commit(self):
        pass


class _MockAgentService:
    """模拟 AgentService"""
    def __init__(self, adapters=None, task=None, internal_task=None):
        self.adapters = adapters or {}
        self._task_handles: dict = {}
        if internal_task is not None:
            self._task_handles["task-1"] = internal_task
        self._task = task
        self.sent: list[tuple[str, dict]] = []

    async def get_task(self, task_id: str):
        return self._task

    async def _safe_send(self, task_id: str, msg: dict) -> None:
        self.sent.append((task_id, msg))


class _MockApp:
    """模拟 FastAPI app.state"""
    def __init__(self, db=None, agent_service=None):
        self.state = types.SimpleNamespace(
            db=db,
            agent_service=agent_service,
        )


class _MockRequest:
    """模拟 FastAPI Request"""
    def __init__(self, app):
        self.app = app


# ── 导入被测函数 ──

from routes.tasks import pause_task  # noqa: E402


# ── 测试 ──

class TestForcePause:
    """Task 6.13: 强制暂停——中断当前子任务"""

    @pytest.mark.asyncio
    async def test_normal_pause_does_not_terminate_adapter(self):
        """force=false（默认）不 terminate adapter 子进程"""
        proc = _MockProcess(returncode=None)
        adapter = _MockAdapter(process=proc)
        agent_svc = _MockAgentService(
            adapters={"hermes": adapter},
            task={"agent_id": "hermes"},
            internal_task=_MockInternalTask(done=False),
        )
        app = _MockApp(db=_MockDB(), agent_service=agent_svc)
        request = _MockRequest(app=app)

        await pause_task("task-1", request, force=False)

        assert proc.terminated is False
        assert agent_svc._task_handles == {}  # handle 被 pop
        # WS 消息含 force=false
        assert len(agent_svc.sent) == 1
        _, msg = agent_svc.sent[0]
        assert msg["type"] == "task_paused"
        assert msg["force"] is False

    @pytest.mark.asyncio
    async def test_force_pause_terminates_adapter_process(self):
        """force=true terminate adapter 子进程"""
        proc = _MockProcess(returncode=None)
        adapter = _MockAdapter(process=proc)
        internal_task = _MockInternalTask(done=False)
        agent_svc = _MockAgentService(
            adapters={"hermes": adapter},
            task={"agent_id": "hermes"},
            internal_task=internal_task,
        )
        app = _MockApp(db=_MockDB(), agent_service=agent_svc)
        request = _MockRequest(app=app)

        await pause_task("task-1", request, force=True)

        assert proc.terminated is True
        assert internal_task.cancelled is True
        assert len(agent_svc.sent) == 1
        _, msg = agent_svc.sent[0]
        assert msg["force"] is True

    @pytest.mark.asyncio
    async def test_force_pause_adapter_already_exited(self):
        """force=true adapter 进程已退出（returncode != None）时不报错"""
        proc = _MockProcess(returncode=0)  # 已退出
        adapter = _MockAdapter(process=proc)
        agent_svc = _MockAgentService(
            adapters={"hermes": adapter},
            task={"agent_id": "hermes"},
        )
        app = _MockApp(db=_MockDB(), agent_service=agent_svc)
        request = _MockRequest(app=app)

        # 不应抛异常
        await pause_task("task-1", request, force=True)
        assert proc.terminated is False  # 已退出，不再 terminate

    @pytest.mark.asyncio
    async def test_force_pause_no_adapter(self):
        """force=true task 的 agent_id 无对应 adapter 时不报错"""
        agent_svc = _MockAgentService(
            adapters={},  # 无 adapter
            task={"agent_id": "unknown"},
        )
        app = _MockApp(db=_MockDB(), agent_service=agent_svc)
        request = _MockRequest(app=app)

        await pause_task("task-1", request, force=True)
        assert len(agent_svc.sent) == 1  # 仍推 WS

    @pytest.mark.asyncio
    async def test_force_pause_task_not_found(self):
        """task 不存在 → 404（不再假装暂停成功、也不再推 WS）"""
        from fastapi import HTTPException
        import pytest as _pytest

        agent_svc = _MockAgentService(
            adapters={"hermes": _MockAdapter(process=_MockProcess())},
            task=None,  # task 不存在
        )
        app = _MockApp(db=_MockDB(), agent_service=agent_svc)
        request = _MockRequest(app=app)

        with _pytest.raises(HTTPException) as exc:
            await pause_task("task-1", request, force=True)
        assert exc.value.status_code == 404
        assert agent_svc.sent == []

    @pytest.mark.asyncio
    async def test_force_pause_terminate_exception_swallowed(self):
        """force=true terminate 抛异常时不影响 WS 推送"""
        class _BrokenProcess:
            returncode = None
            pid = 999
            def terminate(self):
                raise OSError("permission denied")

        adapter = _MockAdapter(process=_BrokenProcess())
        agent_svc = _MockAgentService(
            adapters={"hermes": adapter},
            task={"agent_id": "hermes"},
        )
        app = _MockApp(db=_MockDB(), agent_service=agent_svc)
        request = _MockRequest(app=app)

        # 不应抛异常
        await pause_task("task-1", request, force=True)
        assert len(agent_svc.sent) == 1  # 仍推 WS
