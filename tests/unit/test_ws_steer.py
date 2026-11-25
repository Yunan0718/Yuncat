"""routes/ws_steer.py —— steer 入队前的校验与回执。"""
import asyncio
import sys
import types
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src-python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from routes import ws_steer  # noqa: E402
from services.agent import steer_inbox as inbox  # noqa: E402


class _WS:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


class _Service:
    def __init__(self, task):
        self.task = task

    async def get_task(self, _task_id):
        return self.task


def setup_function():
    inbox._PENDING.clear()


def _run(task, message):
    ws = _WS()
    app = types.SimpleNamespace(state=types.SimpleNamespace(agent_service=_Service(task)))
    asyncio.run(ws_steer.handle_steer(app, ws, message))
    return ws.sent


def test_running_task_is_queued():
    sent = _run(
        {"status": "running", "session_id": "s1", "agent_id": "yuncat"},
        {"task_id": "t1", "prompt": "先跑测试"},
    )
    assert sent[0]["type"] == "steer_queued"
    assert sent[0]["queued"] == 1
    assert inbox.pending_steer_count("t1") == 1


def test_non_running_or_missing_task_is_rejected():
    for task in (None, {"status": "completed"}):
        sent = _run(task, {"task_id": "t1", "prompt": "继续"})
        assert sent[0]["type"] == "steer_error"


def test_empty_and_oversized_prompts_are_rejected(monkeypatch):
    empty = _run({"status": "running"}, {"task_id": "t1", "prompt": "  "})
    assert empty[0]["type"] == "steer_error"
    monkeypatch.setattr(ws_steer, "WS_PROMPT_MAX_CHARS", 3)
    oversized = _run({"status": "running"}, {"task_id": "t1", "prompt": "1234"})
    assert oversized[0]["type"] == "steer_error"
