"""tests/unit/test_steer_inbox.py — 运行中追加指令（steer）

覆盖：
- 入队/claim/计数；
- apply 时写入 history、持久化、推送 steer_applied；
- prepare_round：首次返回原 prompt，后续轮应用 steer 并返回 None；
- 多轮不再重复追加原 prompt（配合 history_trim 修复）。
"""
import asyncio
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src-python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from services.agent import steer_inbox as S  # noqa: E402


class _Memory:
    def __init__(self):
        self.calls = []

    async def add_message(self, session_id, role, content, agent_id=None, **kwargs):
        self.calls.append({"session_id": session_id, "role": role, "content": content})
        return {"id": "m"}


class _Svc:
    def __init__(self):
        self._memory_service = _Memory()
        self.events = []

    async def _safe_send(self, task_id, payload):
        self.events.append(payload)


def setup_function():
    S._PENDING.clear()


def test_push_claim_and_count():
    assert S.push_steer("t1", "先跑测试", "s1", "yuncat") == 1
    assert S.push_steer("t1", "再改 UI") == 2
    assert S.pending_steer_count("t1") == 2
    items = S.claim_steers("t1")
    assert [i["prompt"] for i in items] == ["先跑测试", "再改 UI"]
    assert S.pending_steer_count("t1") == 0


def test_apply_appends_persists_and_notifies():
    svc = _Svc()
    S.push_steer("t1", "先跑测试", "s1", "yuncat")
    ctx = {"history": []}

    async def run():
        return await S.apply_pending_steers(svc, "t1", "s1", "yuncat", ctx)

    applied = asyncio.run(run())
    assert applied == 1
    assert ctx["history"] == [{"role": "user", "content": "先跑测试"}]
    assert svc._memory_service.calls[0]["role"] == "user"
    assert svc.events[0]["type"] == "steer_applied"


def test_prepare_round_first_returns_prompt_and_keeps_queue():
    svc = _Svc()
    S.push_steer("t1", "先跑测试", "s1", "yuncat")
    ctx = {"history": []}

    async def run():
        return await S.prepare_round(svc, "t1", "s1", "yuncat", ctx, "原始任务")

    assert asyncio.run(run()) == "原始任务"
    assert ctx["history"] == []
    assert S.pending_steer_count("t1") == 1


def test_prepare_round_later_applies_and_returns_none():
    svc = _Svc()
    S.push_steer("t1", "先跑测试", "s1", "yuncat")
    ctx = {"history": [{"role": "assistant", "content": "调用工具"}]}

    async def run():
        await S.prepare_round(svc, "t1", "s1", "yuncat", ctx, "原始任务")
        return await S.prepare_round(svc, "t1", "s1", "yuncat", ctx, "原始任务")

    assert asyncio.run(run()) is None
    assert ctx["history"][-1] == {"role": "user", "content": "先跑测试"}
    assert S.pending_steer_count("t1") == 0


def test_queue_is_bounded_per_task(monkeypatch):
    monkeypatch.setattr(S, "STEER_MAX_PENDING_PER_TASK", 1)
    S.push_steer("t1", "第一条")
    try:
        S.push_steer("t1", "第二条")
    except S.SteerQueueFull:
        pass
    else:
        raise AssertionError("超出任务队列上限时应拒绝")
    assert S.pending_steer_count("t1") == 1


def test_clear_returns_discarded_count():
    S.push_steer("t1", "第一条")
    S.push_steer("t1", "第二条")
    assert S.clear_steers("t1") == 2
    assert S.clear_steers("t1") == 0
