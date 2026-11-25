"""ask_user 工具、WS 回答和任务取消清理。"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src-python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tools.builtin import system_tool_ask_user as ask  # noqa: E402


class _Service:
    def __init__(self, on_send):
        self.on_send = on_send
        self.events = []

    async def _safe_send(self, task_id, payload):
        self.events.append(payload)
        self.on_send(task_id, payload)


def _patch_context(monkeypatch, service, task_id="t1"):
    monkeypatch.setattr("context.get_current_task_id", lambda: task_id)
    monkeypatch.setattr("context.get_current_agent_id", lambda: "yuncat")
    monkeypatch.setattr("context.get_app", lambda: types.SimpleNamespace(
        state=types.SimpleNamespace(agent_service=service),
    ))


def test_ask_user_waits_for_ws_answer(monkeypatch):
    service = _Service(lambda _task, payload: ask.resolve_user_question(payload["token"], "B"))
    _patch_context(monkeypatch, service)

    result = asyncio.run(ask.ask_user("Pick one", ["A", "B"], allow_free_text=False))

    assert result["success"] is True
    assert result["data"]["answer"] == "B"
    assert service.events[0]["type"] == "user_question_request"
    assert ask._WAITERS == {}


def test_task_end_wakes_waiter_and_closes_dialog(monkeypatch):
    from services.agent.task_interactions import discard_task_interactions

    async def run():
        service = _Service(lambda *_args: None)
        _patch_context(monkeypatch, service)
        waiting = asyncio.create_task(ask.ask_user("Need input", timeout_sec=30))
        while not service.events:
            await asyncio.sleep(0)
        await discard_task_interactions(service, "t1")
        return await waiting, service.events

    result, events = asyncio.run(run())

    assert result["meta"]["cancelled"] is True
    assert events[-1]["type"] == "user_question_closed"
    assert events[-1]["reason"] == "task_ended"
    assert ask._WAITERS == {}


def test_timeout_closes_dialog(monkeypatch):
    service = _Service(lambda *_args: None)
    _patch_context(monkeypatch, service)

    async def _timeout(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(ask.asyncio, "wait_for", _timeout)
    result = asyncio.run(ask.ask_user("Question", timeout_sec=1))

    assert result["meta"]["timeout"] is True
    assert service.events[-1]["type"] == "user_question_closed"
    assert service.events[-1]["reason"] == "timeout"


def test_disabled_free_text_requires_options(monkeypatch):
    service = _Service(lambda *_args: None)
    _patch_context(monkeypatch, service)

    result = asyncio.run(ask.ask_user("Question", allow_free_text=False))

    assert result["success"] is False
    assert "options are required" in result["error"]
    assert service.events == []


def test_option_bounds_are_rejected_explicitly(monkeypatch):
    service = _Service(lambda *_args: None)
    _patch_context(monkeypatch, service)

    result = asyncio.run(ask.ask_user("Question", ["x" * (ask.MAX_OPTION_CHARS + 1)]))

    assert result["success"] is False
    assert "at most" in result["error"]
    assert service.events == []


def test_task_cancel_wakes_waiter(monkeypatch):
    service = _Service(lambda task, _payload: ask.cancel_questions_for_task(task))
    _patch_context(monkeypatch, service)

    result = asyncio.run(ask.ask_user("Continue?", timeout_sec=30))

    assert result["success"] is False
    assert result["meta"]["cancelled"] is True


def test_ask_user_requires_running_task(monkeypatch):
    service = _Service(lambda *_args: None)
    _patch_context(monkeypatch, service, task_id="")

    result = asyncio.run(ask.ask_user("Question"))

    assert result["success"] is False
    assert "task context" in result["error"]


def test_ws_response_resolves_waiter(monkeypatch):
    from routes.ws_user_question import handle_user_question_response

    async def run():
        future = asyncio.get_running_loop().create_future()
        ask._WAITERS["token-1"] = ("t1", future)
        sent = []

        class _WS:
            async def send_json(self, payload):
                sent.append(payload)

        try:
            await handle_user_question_response(_WS(), {
                "token": "token-1", "answer": "do it",
            })
            assert future.result()["answer"] == "do it"
            assert sent[0]["accepted"] is True
        finally:
            ask._WAITERS.clear()

    asyncio.run(run())


def test_ws_rejects_oversized_answer_without_consuming_waiter():
    from routes.ws_user_question import handle_user_question_response

    async def run():
        future = asyncio.get_running_loop().create_future()
        ask._WAITERS["token-long"] = ("t1", future)
        sent = []

        class _WS:
            async def send_json(self, payload):
                sent.append(payload)

        try:
            await handle_user_question_response(_WS(), {
                "token": "token-long", "answer": "x" * (ask.MAX_ANSWER_CHARS + 1),
            })
            assert future.done() is False
            assert sent[0]["type"] == "user_question_invalid"
        finally:
            ask._WAITERS.clear()

    asyncio.run(run())
