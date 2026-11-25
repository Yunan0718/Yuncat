"""tests/unit/test_pending_tool_calls.py — 取消时为未覆盖的工具调用补合成结果

借鉴 DSH：取消时未启动的 tool_call 必须有一条合成 tool/result，保证历史成对。
"""
import asyncio
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src-python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from services.agent import pending_tool_calls as P  # noqa: E402


class _RecordingMemory:
    def __init__(self):
        self.calls = []

    async def add_message(self, session_id, role, content, agent_id=None, tool_call_id=None, **kwargs):
        self.calls.append({
            "session_id": session_id, "role": role, "content": content,
            "agent_id": agent_id, "tool_call_id": tool_call_id,
        })
        return {"id": "x"}


class _Svc:
    def __init__(self, memory=None):
        self._memory_service = memory or _RecordingMemory()


CALLS = [
    {"id": "call_1", "type": "function",
     "function": {"name": "read_file", "arguments": "{}"}},
    {"id": "call_2", "type": "function",
     "function": {"name": "run_command", "arguments": "{}"}},
]


def setup_function():
    P._PENDING.clear()


def test_register_drain_and_count():
    P.register_pending("t1", "s1", "yuncat", CALLS)
    assert P.pending_count("t1") == 2
    entry = P.drain_pending("t1")
    assert set(entry["calls"]) == {"call_1", "call_2"}
    assert P.pending_count("t1") == 0


def test_cancel_persists_synthetic_results():
    mem = _RecordingMemory()
    svc = _Svc(mem)
    P.register_pending("t1", "s1", "yuncat", CALLS)

    async def run():
        return await P.persist_cancelled_results(svc, "t1", {"session_id": "s1", "agent_id": "yuncat"})

    written = asyncio.run(run())
    assert written == 2
    assert [c["role"] for c in mem.calls] == ["tool", "tool"]
    assert [c["tool_call_id"] for c in mem.calls] == ["call_1", "call_2"]
    payload = json.loads(mem.calls[0]["content"])
    assert payload["cancelled"] is True
    assert P.pending_count("t1") == 0


def test_persist_tool_results_clears_pending_on_success():
    from services.agent.tool_result_persist import persist_tool_results

    mem = _RecordingMemory()
    P.register_pending("t2", "s2", "yuncat", CALLS)

    async def run():
        return await persist_tool_results(
            mem, "s2", "yuncat",
            [{"role": "tool", "tool_call_id": "call_1", "content": "ok"}],
            task_id="t2",
        )

    written = asyncio.run(run())
    assert written == 1
    assert P.pending_count("t2") == 0


def test_pending_registry_is_bounded():
    """内存护栏：异常路径下登记表不能无限增长。"""
    P._PENDING.clear()
    try:
        for i in range(P._PENDING_MAX_TASKS + 5):
            P.register_pending(f"t{i}", "s1", "yuncat", CALLS)
        assert len(P._PENDING) <= P._PENDING_MAX_TASKS
    finally:
        P._PENDING.clear()


def test_expired_registration_is_evicted():
    P._PENDING.clear()
    try:
        P.register_pending("old", "s1", "yuncat", CALLS)
        P._PENDING["old"]["ts"] = 0  # 伪装成很久以前登记的
        P.register_pending("fresh", "s1", "yuncat", CALLS)
        assert "old" not in P._PENDING
        assert "fresh" in P._PENDING
    finally:
        P._PENDING.clear()


def test_task_end_clears_pending_registration():
    """任务结束时清掉登记——否则异常退出会一直留着（内存泄漏）。"""
    from services.agent.task_interactions import discard_task_interactions

    P._PENDING.clear()
    try:
        P.register_pending("t1", "s1", "yuncat", CALLS)
        asyncio.run(discard_task_interactions(_Svc(), "t1"))
        assert P.pending_count("t1") == 0
    finally:
        P._PENDING.clear()
