"""tests/unit/test_ws_replay_messages.py —— Task 6.9: WS 重连后恢复流式上下文

覆盖 routes/ws.py::_handle_subscribe 的 replay_messages 行为:
1. 正常重连——推送 sync_state + replay_messages（含对话历史）
2. 无 session_id——只推 sync_state，不推 replay_messages
3. 无对话历史——只推 sync_state，不推 replay_messages
4. memory_service 不可用——只推 sync_state，不推 replay_messages
5. get_conversation 异常——只推 sync_state，异常被 catch

对标 Claude Code 重连后全量状态同步。
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


# ── Mock 工具 ──

class _MockWS:
    """模拟 FastAPI WebSocket——记录 send_json 调用"""
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, msg: dict) -> None:
        self.sent.append(msg)


class _MockAgentService:
    """模拟 AgentService——register_ws + get_task"""
    def __init__(self, task: dict | None = None):
        self._task = task
        self.registered: list[tuple[str, _MockWS]] = []

    def register_ws(self, task_id: str, ws) -> None:
        self.registered.append((task_id, ws))

    async def get_task(self, task_id: str) -> dict | None:
        return self._task


class _MockMemoryService:
    """模拟 MemoryService——get_conversation 返回预设消息"""
    def __init__(self, messages: list[dict] | None = None, raise_on_get: bool = False):
        self._messages = messages
        self._raise = raise_on_get

    async def get_conversation(self, session_id: str, limit: int = 50) -> list[dict]:
        if self._raise:
            raise RuntimeError("mock get_conversation failure")
        return self._messages or []


class _MockApp:
    """模拟 FastAPI app——state.agent_service + state.memory_service"""
    def __init__(self, agent_service, memory_service):
        self.state = types.SimpleNamespace(
            agent_service=agent_service,
            memory_service=memory_service,
        )


# ── 测试数据 ──

_SAMPLE_MESSAGES = [
    {"id": "msg-1", "role": "user", "content": "Hello", "reasoning_content": ""},
    {"id": "msg-2", "role": "assistant", "content": "Hi there!", "reasoning_content": "thinking..."},
    {"id": "msg-3", "role": "assistant", "content": "How can I help?", "reasoning_content": ""},
]


# ============================================================
# _handle_subscribe replay_messages 测试
# ============================================================

class TestHandleSubscribeReplayMessages:
    """Task 6.9: _handle_subscribe 重连时推送 replay_messages"""

    async def test_normal_reconnect_sends_sync_state_and_replay(self):
        """正常重连——推送 sync_state + replay_messages"""
        from routes.ws import _handle_subscribe

        task = {"id": "task-1", "status": "running", "session_id": "sess-1"}
        agent_svc = _MockAgentService(task=task)
        mem_svc = _MockMemoryService(messages=_SAMPLE_MESSAGES)
        app = _MockApp(agent_svc, mem_svc)
        ws = _MockWS()

        await _handle_subscribe(app, ws, {"task_id": "task-1"})

        # 应注册 WS
        assert len(agent_svc.registered) == 1
        assert agent_svc.registered[0][0] == "task-1"

        # 应推送两条消息: sync_state + replay_messages
        assert len(ws.sent) == 2
        assert ws.sent[0]["type"] == "sync_state"
        assert ws.sent[0]["task_id"] == "task-1"
        assert ws.sent[0]["status"] == "running"

        assert ws.sent[1]["type"] == "replay_messages"
        assert ws.sent[1]["task_id"] == "task-1"
        assert len(ws.sent[1]["messages"]) == 3
        # 验证消息格式
        assert ws.sent[1]["messages"][0]["role"] == "user"
        assert ws.sent[1]["messages"][0]["content"] == "Hello"
        assert ws.sent[1]["messages"][1]["role"] == "assistant"
        assert ws.sent[1]["messages"][1]["reasoning_content"] == "thinking..."

    async def test_no_session_id_skips_replay(self):
        """无 session_id——只推 sync_state，不推 replay_messages"""
        from routes.ws import _handle_subscribe

        task = {"id": "task-2", "status": "running", "session_id": None}
        agent_svc = _MockAgentService(task=task)
        mem_svc = _MockMemoryService(messages=_SAMPLE_MESSAGES)
        app = _MockApp(agent_svc, mem_svc)
        ws = _MockWS()

        await _handle_subscribe(app, ws, {"task_id": "task-2"})

        assert len(ws.sent) == 1
        assert ws.sent[0]["type"] == "sync_state"

    async def test_empty_conversation_skips_replay(self):
        """无对话历史——只推 sync_state，不推 replay_messages"""
        from routes.ws import _handle_subscribe

        task = {"id": "task-3", "status": "running", "session_id": "sess-3"}
        agent_svc = _MockAgentService(task=task)
        mem_svc = _MockMemoryService(messages=[])
        app = _MockApp(agent_svc, mem_svc)
        ws = _MockWS()

        await _handle_subscribe(app, ws, {"task_id": "task-3"})

        assert len(ws.sent) == 1
        assert ws.sent[0]["type"] == "sync_state"

    async def test_memory_service_unavailable_skips_replay(self):
        """memory_service 不可用——只推 sync_state"""
        from routes.ws import _handle_subscribe

        task = {"id": "task-4", "status": "running", "session_id": "sess-4"}
        agent_svc = _MockAgentService(task=task)
        app = _MockApp(agent_svc, None)  # memory_service = None
        ws = _MockWS()

        await _handle_subscribe(app, ws, {"task_id": "task-4"})

        assert len(ws.sent) == 1
        assert ws.sent[0]["type"] == "sync_state"

    async def test_get_conversation_exception_does_not_crash(self):
        """get_conversation 异常——catch 后只推 sync_state，不崩溃"""
        from routes.ws import _handle_subscribe

        task = {"id": "task-5", "status": "running", "session_id": "sess-5"}
        agent_svc = _MockAgentService(task=task)
        mem_svc = _MockMemoryService(raise_on_get=True)
        app = _MockApp(agent_svc, mem_svc)
        ws = _MockWS()

        # 不应抛异常
        await _handle_subscribe(app, ws, {"task_id": "task-5"})

        # 应只推 sync_state（replay 失败被 catch）
        assert len(ws.sent) == 1
        assert ws.sent[0]["type"] == "sync_state"

    async def test_task_not_found_sends_nothing(self):
        """task 不存在——不推送任何消息"""
        from routes.ws import _handle_subscribe

        agent_svc = _MockAgentService(task=None)
        mem_svc = _MockMemoryService(messages=_SAMPLE_MESSAGES)
        app = _MockApp(agent_svc, mem_svc)
        ws = _MockWS()

        await _handle_subscribe(app, ws, {"task_id": "nonexistent"})

        # task 不存在——不应推送 sync_state 或 replay_messages
        assert len(ws.sent) == 0
