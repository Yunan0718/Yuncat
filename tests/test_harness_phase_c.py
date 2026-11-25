"""tests/test_harness_phase_c.py —— Supervisor–Worker Harness Phase C 验收

验收标准（docs/budget-reliability-design.md §8）：
  - C-01~C-07  BudgetManager 单元（contextvars 隔离、>= 严格语义、增量累加、墙钟、嵌套恢复）
  - C-08~C-11 埋点与超限强杀（tool_service / adapters/base.py + 回调注册表）
  - C-12~C-19 runtime（kill 统一接口 / 日志 10MB 截断 / 崩溃恢复扫描 + 通知）
  - C-20~C-23 状态机回归 + 重试超时翻倍数据驱动开关
  - C-24 熔断标记（token 超限被吞后工具调用直接拒绝）
"""
import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src-python"))

from harness import budget_manager as bm
from harness import runtime as rt
from harness import task_manager as tm
from harness.schemas import ExecutionBudget, TaskPackage
from repositories import init_db
from repositories.harness_schema import ensure_harness_schema


# ============================================================
# C-01 ~ C-07：BudgetManager 单元
# ============================================================

def _budget(**kw) -> ExecutionBudget:
    return ExecutionBudget(**kw)


def test_c01_no_limits_all_noop_within_attempt():
    """C-01: 预算全 None（不限制）——任意计数不抛，current() 可读。"""
    bm.enter_attempt("t1", "a1", _budget())
    try:
        st = bm.current()
        assert st is not None
        assert st.task_id == "t1" and st.attempt_id == "a1"
        # 三类计数各执行若干次均不抛
        for _ in range(10):
            bm.count_tool_invocation()
            bm.accumulate_tokens({"total_tokens": 5})
        assert st.tool_invocations == 10
        assert st.tokens == 50
    finally:
        bm.exit_attempt()


def test_c02_tool_invocations_ge_semantics():
    """C-02: max_tool_invocations=3——第 3 次计数（current>=limit）抛 BudgetExceeded。"""
    bm.enter_attempt("t2", "a2", _budget(max_tool_invocations=3))
    try:
        bm.count_tool_invocation()  # 1
        bm.count_tool_invocation()  # 2
        with pytest.raises(bm.BudgetExceeded) as ei:
            bm.count_tool_invocation()  # 3 >= 3
        assert ei.value.budget_type == bm.BUDGET_TYPE_TOOL
        assert ei.value.limit == 3 and ei.value.current == 3
        assert ei.value.task_id == "t2"
    finally:
        bm.exit_attempt()


def test_c03_tokens_incremental_accumulate():
    """C-03: max_tokens=100——增量累加（非覆盖），current>=limit 即抛（与 C-02 一致）。"""
    bm.enter_attempt("t3", "a3", _budget(max_tokens=100))
    try:
        bm.accumulate_tokens({"total_tokens": 40})
        bm.accumulate_tokens({"total_tokens": 40})
        assert bm.current().tokens == 80  # 增量累计，不是覆盖为 40
        with pytest.raises(bm.BudgetExceeded) as ei:
            bm.accumulate_tokens({"total_tokens": 20})  # 100 >= 100 → 超
        assert ei.value.budget_type == bm.BUDGET_TYPE_TOKENS
        assert ei.value.limit == 100 and ei.value.current == 100
    finally:
        bm.exit_attempt()

    # 回退路径（新 attempt 独立验证）：无 total_tokens → prompt + completion
    bm.enter_attempt("t3b", "a3b", _budget(max_tokens=100))
    try:
        bm.accumulate_tokens({"prompt_tokens": 60, "completion_tokens": 30})
        assert bm.current().tokens == 90
        with pytest.raises(bm.BudgetExceeded):
            bm.accumulate_tokens({"prompt_tokens": 11})  # 101 >= 100 → 超
    finally:
        bm.exit_attempt()


def test_c04_max_seconds_wall_clock(monkeypatch):
    """C-04: max_seconds=1——墙钟超限后任意计数抛（检查点驱动，无独立定时器）。"""
    class _FakeClock:
        def __init__(self, start: float):
            self.now = start

        def time(self) -> float:
            return self.now

    fake = _FakeClock(1000.0)
    monkeypatch.setattr(bm, "_time", SimpleNamespace(time=fake.time))
    bm.enter_attempt("t4", "a4", _budget(max_seconds=1))
    try:
        bm.count_tool_invocation()  # elapsed=0 不超
        fake.now = 1001.0  # 恰好 1s
        with pytest.raises(bm.BudgetExceeded) as ei:
            bm.count_tool_invocation()  # 1 >= 1 → 超
        assert ei.value.budget_type == bm.BUDGET_TYPE_SECONDS
        assert ei.value.current == 1
    finally:
        bm.exit_attempt()


def test_c05_context_isolation_concurrent():
    """C-05: contextvars 隔离——两个 attempt 并发交替计数互不污染。"""
    async def worker_a():
        bm.enter_attempt("ta", "aa", _budget(max_tool_invocations=5))
        try:
            bm.count_tool_invocation()
            bm.count_tool_invocation()
            await asyncio.sleep(0)  # 让出给 B
            # B 的计数不影响 A
            assert bm.current().tool_invocations == 2
            bm.count_tool_invocation()  # 3
            return bm.current().tool_invocations
        finally:
            bm.exit_attempt()

    async def worker_b():
        await asyncio.sleep(0)  # 等 A 先挂上下文
        bm.enter_attempt("tb", "ab", _budget(max_tool_invocations=3))
        try:
            bm.count_tool_invocation()
            bm.count_tool_invocation()
            with pytest.raises(bm.BudgetExceeded):
                bm.count_tool_invocation()  # B 超限——仅 B 触发
            return bm.current().tool_invocations
        finally:
            bm.exit_attempt()

    async def main():
        results = await asyncio.gather(worker_a(), worker_b())
        assert results[0] == 3  # A 不受 B 超限影响
        assert results[1] == 3

    asyncio.run(main())


def test_c06_no_context_noop():
    """C-06: 无 attempt 上下文——全部 API no-op 零异常（现有工具/对话路径不受影响）。"""
    assert bm.current() is None
    bm.count_tool_invocation()      # no-op
    bm.accumulate_tokens({"total_tokens": 999})  # no-op
    bm.exit_attempt()               # no-op（重复/空退出安全）
    bm.exit_attempt()
    assert bm.current() is None


def test_c07_nested_and_exception_recovery():
    """C-07: 嵌套配对 + 异常路径 finally 恢复。"""
    bm.enter_attempt("t_outer", "a_outer", _budget(max_tool_invocations=100))
    try:
        bm.enter_attempt("t_inner", "a_inner", _budget(max_tool_invocations=1))
        try:
            with pytest.raises(bm.BudgetExceeded):
                bm.count_tool_invocation()  # 内层超限
        finally:
            bm.exit_attempt()  # 异常路径恢复 → 回到外层
        assert bm.current().task_id == "t_outer"
        bm.count_tool_invocation()  # 外层计数正常
        assert bm.current().tool_invocations == 1
    finally:
        bm.exit_attempt()
    assert bm.current() is None


def test_c08_exceeded_handler_registry():
    """C-08: 超限回调注册表——handler 先于异常触发，且 handler 异常不阻断主路径。"""
    calls = []

    def handler(exc):
        calls.append(exc)

    bm.set_exceeded_handler(handler)
    try:
        bm.enter_attempt("t8", "a8", _budget(max_tool_invocations=1))
        try:
            with pytest.raises(bm.BudgetExceeded) as ei:
                bm.count_tool_invocation()
            assert calls and calls[-1] is ei.value  # handler 收到同一异常对象
        finally:
            bm.exit_attempt()
        # handler 自身抛异常 → 吞掉，不影响计数主路径
        def bad_handler(_exc):
            raise RuntimeError("handler boom")

        bm.set_exceeded_handler(bad_handler)
        bm.enter_attempt("t8b", "a8b", _budget(max_tool_invocations=1))
        try:
            with pytest.raises(bm.BudgetExceeded):
                bm.count_tool_invocation()  # 仍正常抛 BudgetExceeded
        finally:
            bm.exit_attempt()
    finally:
        bm.set_exceeded_handler(None)


# ============================================================
# C-09 ~ C-11：埋点与超限强杀接线
# ============================================================

async def test_c09_tool_execution_budget_exceeded_returns_failure():
    """C-09: _run_tool_execution 内预算超限——返回失败 ToolResult（meta.budget_exceeded），
    与超时同构（ok=False 跳过 post-hooks）；非 harness 上下文执行不受影响。"""
    from services.tools.tool_service import ToolService, _run_tool_execution

    async def _fake_tool(**kw):
        return {"success": True, "data": "ok"}

    tool = SimpleNamespace(func=_fake_tool, tool_timeout=None, is_concurrency_safe=True)
    svc = ToolService()

    # harness attempt 上下文：max_tool_invocations=1 → 首次执行即超限
    bm.enter_attempt("t9", "a9", _budget(max_tool_invocations=1))
    try:
        result, ok = await _run_tool_execution(
            svc, tool, "read_file", {"path": "x"}, agent_id="a", task_id=None, _t0=0.0,
        )
        assert ok is False
        assert result["success"] is False
        assert result["meta"]["budget_exceeded"] is True
        assert result["meta"]["budget_type"] == bm.BUDGET_TYPE_TOOL
    finally:
        bm.exit_attempt()

    # 无上下文（非 harness）：正常执行，零侵入
    result, ok = await _run_tool_execution(
        svc, tool, "read_file", {"path": "x"}, agent_id="a", task_id=None, _t0=0.0,
    )
    assert ok is True
    assert result["success"] is True


async def test_c10_invoke_captures_usage(monkeypatch):
    """C-10: 非流式 invoke 响应含 usage——last_usage 更新 + accumulate_tokens（缺口修复回归）。"""
    from adapters.base import OpenAiCompatibleAdapter
    from services import llm_cache

    class _FakeCache:
        async def get(self, *a, **kw):
            return None

        async def put(self, *a, **kw):
            return None

    class _FakeResp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return {
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"total_tokens": 42, "prompt_tokens": 10, "completion_tokens": 32},
            }

    class _FakeSession:
        # aiohttp 的 session.post() 是同步方法返回 async context manager——
        # async with self._session.post(...) 不会 await coroutine，故 fake 必须同步。
        def post(self, *a, **kw):
            return _FakeResp()

    async def _noop():
        return None

    monkeypatch.setattr(llm_cache, "get_global_cache", lambda: _FakeCache())
    adapter = OpenAiCompatibleAdapter(cmd=["python", "-c", "pass"], agent_id="t", port_hint=8000)
    adapter._session = _FakeSession()
    adapter.wait_ready = _noop
    adapter._ensure_session = _noop

    # 无 budget 上下文：响应正常返回（零侵入）
    msg = await adapter.invoke("hello", context={"tools": [{"type": "function"}]})
    assert msg.get("content") == "hi"
    assert adapter.last_usage["total_tokens"] == 42  # 缺口修复：invoke 也捕获 usage

    # 有 budget 上下文：accumulate 生效
    bm.enter_attempt("t10", "a10", _budget(max_tokens=100))
    try:
        await adapter.invoke("hello", context={"tools": [{"type": "function"}]})
        assert bm.current().tokens == 42
    finally:
        bm.exit_attempt()


async def test_c11_stream_captures_usage():
    """C-11: 流式 usage chunk——last_usage 更新 + accumulate 汇报（既有行为回归）。"""
    from adapters.base import OpenAiCompatibleAdapter

    chunks = [
        b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n',
        b'data: {"choices": [{"delta": {"content": ""}}], '
        b'"usage": {"total_tokens": 42, "prompt_tokens": 10, "completion_tokens": 32}}\n\n',
        b"data: [DONE]\n\n",
    ]

    class _FakeContent:
        def __init__(self, cs):
            self._cs = cs

        async def iter_any(self):
            for c in self._cs:
                yield c

    class _FakeStreamResp:
        def __init__(self, cs):
            self.content = _FakeContent(cs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeSession:
        def post(self, *a, **kw):  # 同步——async with 需要 async context manager
            return _FakeStreamResp(chunks)

    async def _noop():
        return None

    adapter = OpenAiCompatibleAdapter(cmd=["python", "-c", "pass"], agent_id="t", port_hint=8000)
    adapter._session = _FakeSession()
    adapter.wait_ready = _noop
    adapter._ensure_session = _noop

    bm.enter_attempt("t11", "a11", _budget(max_tokens=100))
    try:
        out = []
        async for chunk in adapter.stream("hello"):
            out.append(chunk)
        assert adapter.last_usage["total_tokens"] == 42
        assert bm.current().tokens == 42
        assert any(c["type"] == "text" for c in out)
    finally:
        bm.exit_attempt()


# ============================================================
# C-12 ~ C-15：runtime——kill 统一接口 + 日志截断
# ============================================================

async def test_c12_kill_process_tree_posix(monkeypatch):
    """C-12: POSIX 分支——killpg(SIGTERM) → grace → killpg(SIGKILL)；进程已退出时 no-op。"""
    monkeypatch.setattr(rt.sys, "platform", "linux")
    calls: list[int] = []

    async def _fast_sleep(_s):
        return None

    def _fake_killpg(pgid, sig):
        calls.append(sig)
        if sig == signal.SIGTERM and pgid == 999:
            raise ProcessLookupError(3, "no such process")  # 幂等：已退出

    # 替换 rt.os 模块引用（Windows 的 os 无 killpg 属性，且 frozen 模块不可 patch）
    fake_os = SimpleNamespace(killpg=_fake_killpg, kill=lambda _p, _s: None)
    monkeypatch.setattr(rt, "os", fake_os)
    monkeypatch.setattr(rt.asyncio, "sleep", _fast_sleep)

    await rt.kill_process_tree(pid=42, reason="test", grace_sec=0.1)
    assert calls == [signal.SIGTERM, rt._SIGKILL]

    # 幂等：进程已退出 → 不抛、不补 SIGKILL
    calls.clear()
    await rt.kill_process_tree(pid=999, reason="gone", grace_sec=0.1)
    assert calls == [signal.SIGTERM]

    # pid 非法 → no-op
    calls.clear()
    await rt.kill_process_tree(pid=0, reason="bad")
    assert calls == []


async def test_c13_kill_process_tree_windows(monkeypatch):
    """C-13: Windows 分支——有 hjob 优先 TerminateJobObject（整组杀），失败降级 os.kill。"""
    monkeypatch.setattr(rt.sys, "platform", "win32")
    terminated: list[int] = []
    kills: list[tuple[int, int]] = []

    async def _fast_sleep(_s):
        return None

    monkeypatch.setattr(rt.asyncio, "sleep", _fast_sleep)

    # 优先路径：terminate_job 成功 → 不降级
    monkeypatch.setattr(
        "security.job_object.terminate_job",
        lambda hjob, exit_code=1: terminated.append(hjob) or True,
    )
    fake_os = SimpleNamespace(kill=lambda pid, sig: kills.append((pid, sig)), killpg=lambda _p, _s: None)
    monkeypatch.setattr(rt, "os", fake_os)
    await rt.kill_process_tree(pid=7, reason="budget", hjob=0xABC)
    assert terminated == [0xABC]
    assert kills == []

    # 降级路径：terminate_job 失败 → os.kill 主进程（Windows 单次强杀，无二段）
    monkeypatch.setattr(
        "security.job_object.terminate_job",
        lambda hjob, exit_code=1: False,
    )
    await rt.kill_process_tree(pid=7, reason="fallback", hjob=None)
    assert kills == [(7, signal.SIGTERM)]


def test_c14_truncate_log_preserves_tail(tmp_path):
    """C-14: 日志超限（11MB）→ 保尾截断 ≤10MB，末行保留。"""
    p = tmp_path / "worker.log"
    p.write_bytes(b"HEAD|" * 1024 + b"|" * (11 * 1024 * 1024) + b"TAIL_MARKER")
    assert rt.truncate_log(p, max_bytes=10 * 1024 * 1024) is True
    data = p.read_bytes()
    assert len(data) <= 10 * 1024 * 1024
    assert data.endswith(b"TAIL_MARKER")  # 尾部（最新状态）保留
    assert b"HEAD|" not in data  # 头部被截掉


def test_c15_truncate_log_noop_under_limit(tmp_path):
    """C-15: 未超限（<10MB）→ 不触碰；不存在 → no-op。"""
    p = tmp_path / "small.log"
    payload = b"x" * 1024
    p.write_bytes(payload)
    assert rt.truncate_log(p) is False
    assert p.read_bytes() == payload
    assert rt.truncate_log(tmp_path / "missing.log") is False


# ============================================================
# C-16 ~ C-19：崩溃恢复扫描
# ============================================================

@pytest.fixture
async def db(tmp_path):
    conn = await init_db(str(tmp_path / "harness_c.db"))
    await ensure_harness_schema(conn)
    yield conn
    await conn.close()


async def _seed_running(db, idem: str, status: str, age_sec: int) -> dict:
    pkg = TaskPackage(idempotency_key=idem, mission="crash test")
    task = await tm.create_task(db, pkg)
    await tm.transition(db, task["id"], tm.TaskStatus.QUEUED, "queue")   # PENDING → QUEUED
    await tm.transition(db, task["id"], tm.TaskStatus.RUNNING, "start")  # QUEUED → RUNNING
    if status == tm.TaskStatus.WAITING_PERMISSION:
        await tm.transition(db, task["id"], tm.TaskStatus.WAITING_PERMISSION, "l1")
    await db.execute(
        "UPDATE harness_tasks SET updated_at=? WHERE id=?",
        (int(time.time()) - age_sec, task["id"]),
    )
    await db.commit()
    return await tm.get_task(db, task["id"])


async def test_c16_scan_resets_stale_running(db):
    """C-16: RUNNING 且超时（600s 无心跳）→ 重置 TIMEOUT + 审计。"""
    task = await _seed_running(db, "k16", tm.TaskStatus.RUNNING, 600)
    reset = await rt.scan_stale_running_tasks(db)
    assert len(reset) == 1
    assert reset[0]["task_id"] == task["id"]
    assert reset[0]["to_status"] == tm.TaskStatus.TIMEOUT
    assert (await tm.get_task(db, task["id"]))["status"] == tm.TaskStatus.TIMEOUT
    transitions = await tm.get_state_transitions(db, task["id"])
    assert transitions[-1]["to_status"] == tm.TaskStatus.TIMEOUT


async def test_c17_scan_keeps_fresh_running(db):
    """C-17: 未超时（10s）→ 不重置。"""
    task = await _seed_running(db, "k17", tm.TaskStatus.RUNNING, 10)
    reset = await rt.scan_stale_running_tasks(db)
    assert reset == []
    assert (await tm.get_task(db, task["id"]))["status"] == tm.TaskStatus.RUNNING


async def test_c18_scan_publishes_task_reset(db):
    """C-18: 重置时发布 task_reset 通知（notifier 注入）。"""
    class _FakeNotifier:
        def __init__(self):
            self.events = []

        async def publish(self, evt, payload):
            self.events.append((evt, payload))

    task = await _seed_running(db, "k18", tm.TaskStatus.RUNNING, 600)
    notifier = _FakeNotifier()
    await rt.scan_stale_running_tasks(db, notifier=notifier)
    assert len(notifier.events) == 1
    evt, payload = notifier.events[0]
    assert evt == "task_reset"
    assert payload["task_id"] == task["id"]
    assert payload["from_status"] == tm.TaskStatus.RUNNING
    assert payload["to_status"] == tm.TaskStatus.TIMEOUT


async def test_c19_scan_resets_waiting_permission(db):
    """C-19: WAITING_PERMISSION 残留（崩溃时 L1 挂起）同样重置为 TIMEOUT。"""
    task = await _seed_running(db, "k19", tm.TaskStatus.WAITING_PERMISSION, 600)
    reset = await rt.scan_stale_running_tasks(db)
    assert len(reset) == 1
    assert reset[0]["from_status"] == tm.TaskStatus.WAITING_PERMISSION
    assert (await tm.get_task(db, task["id"]))["status"] == tm.TaskStatus.TIMEOUT


# ============================================================
# C-20 ~ C-23：状态机回归 + 重试熔断 + 超时翻倍数据驱动开关
# ============================================================

def test_c20_state_transition_regression():
    """C-20: 预算/恢复相关迁移边放行；非法迁移仍拒绝（状态机表回归保护）。"""
    # 预算路径：RUNNING → BUDGET_EXCEEDED → BLOCKED/REJECTED
    tm.validate_transition(tm.TaskStatus.RUNNING, tm.TaskStatus.BUDGET_EXCEEDED)
    tm.validate_transition(tm.TaskStatus.BUDGET_EXCEEDED, tm.TaskStatus.BLOCKED)
    tm.validate_transition(tm.TaskStatus.BUDGET_EXCEEDED, tm.TaskStatus.REJECTED)
    # 恢复路径：RUNNING → TIMEOUT；Phase C 新增 WAITING_PERMISSION → TIMEOUT
    tm.validate_transition(tm.TaskStatus.RUNNING, tm.TaskStatus.TIMEOUT)
    tm.validate_transition(tm.TaskStatus.WAITING_PERMISSION, tm.TaskStatus.TIMEOUT)
    # 非法迁移仍拒绝（终态不可再走）
    with pytest.raises(ValueError):
        tm.validate_transition(tm.TaskStatus.BLOCKED, tm.TaskStatus.RUNNING)
    with pytest.raises(ValueError):
        tm.validate_transition(tm.TaskStatus.COMPLETED, tm.TaskStatus.QUEUED)


@pytest.fixture
def app_ctx(db):
    app = SimpleNamespace(state=SimpleNamespace(db=db))
    from context import set_app

    set_app(app)
    yield app
    set_app(None)


async def _retry_to_timeout(db, task_id: str) -> None:
    """辅助：PENDING → QUEUED → RUNNING → TIMEOUT（一轮失败）。"""
    await tm.transition(db, task_id, tm.TaskStatus.QUEUED, "queue")
    await tm.transition(db, task_id, tm.TaskStatus.RUNNING, "start")
    await tm.transition(db, task_id, tm.TaskStatus.TIMEOUT, "timeout")


async def test_c21_retry_circuit_breaker(app_ctx, db):
    """C-21: max_attempts=3——初始 + 2 次重试，第 3 次失败熔断 BLOCKED（转 HITL）。"""
    from harness.supervisor_tools import reject_and_reassign

    pkg = TaskPackage(idempotency_key="k21", mission="x", max_attempts=3)
    task = await tm.create_task(db, pkg)
    for i in range(3):
        await _retry_to_timeout(db, task["id"])
        res = await reject_and_reassign(task["id"], f"fail #{i + 1}")
        assert res["success"] is True
        if i < 2:
            assert res["data"]["status"] == tm.TaskStatus.QUEUED
        else:
            assert res["data"]["status"] == tm.TaskStatus.BLOCKED  # 熔断
    updated = await tm.get_task(db, task["id"])
    assert updated["status"] == tm.TaskStatus.BLOCKED
    assert updated["attempt"] == 3


async def test_c22_relax_timeout_doubles_budget(app_ctx, db):
    """C-22: relax_timeout=True 且上次 TIMEOUT → 新 attempt max_seconds ×2（封顶内）。"""
    from harness.supervisor_tools import reject_and_reassign

    pkg = TaskPackage(
        idempotency_key="k22", mission="x", max_attempts=5,
        execution_budget=ExecutionBudget(max_seconds=100),
    )
    task = await tm.create_task(db, pkg)
    await _retry_to_timeout(db, task["id"])
    res = await reject_and_reassign(task["id"], "timeout too short", relax_timeout=True)
    assert res["success"] is True
    assert res["data"]["max_seconds_relaxed"] is True
    updated = await tm.get_task(db, task["id"])
    assert updated["status"] == tm.TaskStatus.QUEUED
    assert updated["execution_budget"]["max_seconds"] == 200


async def test_c23_relax_timeout_off_by_default(app_ctx, db):
    """C-23: relax_timeout 默认 False——预算不变（数据驱动开关默认关闭）。"""
    from harness.supervisor_tools import reject_and_reassign

    pkg = TaskPackage(
        idempotency_key="k23", mission="x", max_attempts=5,
        execution_budget=ExecutionBudget(max_seconds=100),
    )
    task = await tm.create_task(db, pkg)
    await _retry_to_timeout(db, task["id"])
    res = await reject_and_reassign(task["id"], "other reason")
    assert res["success"] is True
    assert res["data"]["max_seconds_relaxed"] is False
    updated = await tm.get_task(db, task["id"])
    assert updated["execution_budget"]["max_seconds"] == 100


def test_c24_budget_breaker_blocks_further_tools():
    """C-24: 熔断标记——token 超限异常被 adapter 热路径吞掉后，后续工具调用直接拒绝。

    覆盖"token 超限但工具继续执行"缺口：exceeded 置位后 count_tool_invocation
    不再计数，直接抛原始超限类型——agent 循环据此感知并停止。
    """
    calls = []
    bm.set_exceeded_handler(lambda exc: calls.append(exc.budget_type))
    bm.enter_attempt("t24", "a24", _budget(max_tokens=100, max_tool_invocations=10))
    try:
        try:
            bm.accumulate_tokens({"total_tokens": 100})  # 100 >= 100 → 超限
        except bm.BudgetExceeded:
            pass  # 模拟 adapter 热路径吞异常（不中断响应）
        assert bm.current().exceeded is True
        assert bm.current().exceeded_type == bm.BUDGET_TYPE_TOKENS
        # 熔断已置位——即使工具计数远未达限，后续调用也直接拒绝，且保留原始超限类型
        with pytest.raises(bm.BudgetExceeded) as ei:
            bm.count_tool_invocation()
        assert ei.value.budget_type == bm.BUDGET_TYPE_TOKENS
        # handler 触发两次：超限时 + 熔断拒绝时
        assert calls == [bm.BUDGET_TYPE_TOKENS, bm.BUDGET_TYPE_TOKENS]
    finally:
        bm.exit_attempt()
        bm.set_exceeded_handler(None)
