"""tests/unit/test_token_budget.py —— AUDIT #13 Token 预算测试

覆盖：
1. execute_tool_loop 启动时读取 max_budget_tokens / used_tokens
2. 预算已耗尽 → 立即优雅暂停 (status='paused_budget')，不调 LLM
3. 每轮累加 adapter.last_usage["total_tokens"] 并持久化到 tasks 表
4. resume_task 支持恢复 paused_budget 状态的任务

设计要点：
- _MockAgentSvcFull 提供 execute_tool_loop 需要的全部依赖
- _MockAdapter.last_usage 模拟 LLM 返回的 token 用量
- mock _stream_with_fallback 产出可控的 chunk 序列
"""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ═══════════════════════════════════════════════════════════════
# Mocks
# ═══════════════════════════════════════════════════════════════

class _MockAdapter:
    """模拟 adapter——last_usage 可控"""

    def __init__(self, total_tokens_per_call: int = 0):
        self._total = total_tokens_per_call
        self.last_usage: dict[str, int] = {}
        if total_tokens_per_call > 0:
            self.last_usage = {
                "prompt_tokens": total_tokens_per_call // 2,
                "completion_tokens": total_tokens_per_call // 2,
                "total_tokens": total_tokens_per_call,
            }


class _MockCompactor:
    """模拟 compactor——空操作"""

    def emergency_trim_history(self, history):
        return history

    def micro_compact_history(self, history, model_cfg=None):
        return 0

    def reattach_context_after_compact(self, session_id, context, model_cfg):
        pass


class _MockMemoryService:
    """模拟 memory_service"""

    async def add_message(self, *args, **kwargs):
        pass

    async def maybe_compact_context(self, *args, **kwargs):
        pass


class _MockAgentSvcFull:
    """完整 mock self——提供 execute_tool_loop 需要的全部依赖

    stream_chunks: 每轮产出的 chunk 列表（None=产出文本+工具调用，空=无工具调用退出）
    """

    def __init__(self, db, stream_chunks_per_round=None, total_tokens_per_call=0):
        self.db = db
        self.adapters = {}
        self._pending_failure_analysis = None
        self.sent: list[dict] = []
        self._tool_service = None
        self._memory_service = _MockMemoryService()
        self.compactor = _MockCompactor()
        self._micro_compact_last_applied: dict = {}
        self._needs_full_compact: set = set()
        self._stream_chunks = stream_chunks_per_round or []
        self._total_tokens_per_call = total_tokens_per_call
        self._mark_calls: list[tuple] = []
        self._snapshots: list[dict] = []

    async def _safe_send(self, task_id: str, payload: dict) -> None:
        self.sent.append({"task_id": task_id, **payload})

    async def _mark_task(self, task_id: str, status: str,
                         error: str | None = None, output: str | None = None,
                         expected_from: str | None = None):
        self._mark_calls.append((task_id, status, error, output))
        await self.db.execute(
            "UPDATE tasks SET status=? WHERE id=?", (status, task_id)
        )
        await self.db.commit()

    async def push_stream_chunk(self, task_id: str, chunk: str, index: int):
        pass

    async def _streaming_execute_tool(self, *args, **kwargs):
        return None

    async def _write_task_snapshot(self, task_id: str, data: dict):
        self._snapshots.append(data)

    def _create_bg_task(self, coro, label=""):
        task = asyncio.create_task(coro)
        return task

    async def _stream_with_fallback(
        self, agent_id, prompt, context, primary_provider="deepseek", task_id="",
    ):
        """模拟 LLM 流——产出预设 chunks"""
        if self._stream_chunks:
            chunks = self._stream_chunks.pop(0)
        else:
            chunks = []
        for chunk in chunks:
            yield chunk


# ═══════════════════════════════════════════════════════════════
# DB fixture
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
async def db(tmp_path):
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_budget.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


def _make_model_cfg():
    return {
        "provider": "deepseek",
        "model": "test-model",
        "context_window": 32000,
    }


# ═══════════════════════════════════════════════════════════════
# AUDIT #13: Token 预算测试
# ═══════════════════════════════════════════════════════════════

class TestTokenBudgetEnforcement:
    """AUDIT #13: execute_tool_loop 的 Token 预算强制"""

    async def test_budget_already_exceeded_immediately_pauses(self, db):
        """预算已耗尽 → 立即暂停，不调 LLM"""
        # 创建一个 used_tokens >= max_budget_tokens 的 task
        task_id = "test-task-001"
        await db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, session_id, "
            "max_budget_tokens, used_tokens, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (task_id, "test", "running", "hermes", "sess1",
             1000, 1500, int(time.time())),
        )
        await db.commit()

        svc = _MockAgentSvcFull(
            db,
            stream_chunks_per_round=[[{"type": "text", "data": "should not appear"}]],
            total_tokens_per_call=100,
        )
        adapter = _MockAdapter(total_tokens_per_call=100)
        context = {"history": []}

        from services.agent.agent_tool_loop import execute_tool_loop
        result = await execute_tool_loop(
            svc, adapter, context, task_id, "hermes", "sess1",
            _make_model_cfg(), "test prompt",
        )

        # 应立即暂停——没有调用 LLM（_stream_chunks 未被消费）
        assert len(svc._mark_calls) > 0
        assert svc._mark_calls[0][1] == "paused_budget"
        # 验证 DB 状态
        cursor = await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
        row = await cursor.fetchone()
        assert row[0] == "paused_budget"
        # 应推送 budget_exceeded WS 消息
        budget_msgs = [s for s in svc.sent if s.get("type") == "budget_exceeded"]
        assert len(budget_msgs) == 1
        assert budget_msgs[0]["used_tokens"] == 1500
        assert budget_msgs[0]["max_budget_tokens"] == 1000

    async def test_budget_accumulated_across_rounds(self, db):
        """多轮累加 used_tokens 并持久化"""
        task_id = "test-task-002"
        await db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, session_id, "
            "max_budget_tokens, used_tokens, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (task_id, "test", "running", "hermes", "sess2",
             10000, 0, int(time.time())),
        )
        await db.commit()

        # 第一轮：产出文本（无工具调用）→ break
        svc = _MockAgentSvcFull(
            db,
            stream_chunks_per_round=[[{"type": "text", "data": "Hello world"}]],
            total_tokens_per_call=600,
        )
        adapter = _MockAdapter(total_tokens_per_call=600)
        context = {"history": []}

        from services.agent.agent_tool_loop import execute_tool_loop
        await execute_tool_loop(
            svc, adapter, context, task_id, "hermes", "sess2",
            _make_model_cfg(), "test prompt",
        )

        # 验证 used_tokens 已累加并持久化
        cursor = await db.execute("SELECT used_tokens FROM tasks WHERE id=?", (task_id,))
        row = await cursor.fetchone()
        assert row[0] == 600  # 一轮 600 tokens

    async def test_no_budget_still_records_usage(self, db):
        """无预算限制 (max_budget_tokens=NULL) → **仍要记账**。

        2026-10-07 契约变更：预算上限只决定「何时暂停」，不该决定「是否记账」。
        原实现套了 `if _budget_limit > 0`，导致无预算任务 used_tokens 恒为 0，
        与 analytics_events 的真实用量打架（实测漏记 11.5 万 token）。
        """
        task_id = "test-task-003"
        await db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (task_id, "test", "running", "hermes", "sess3", int(time.time())),
        )
        await db.commit()

        svc = _MockAgentSvcFull(
            db,
            stream_chunks_per_round=[[{"type": "text", "data": "Hello"}]],
            total_tokens_per_call=500,
        )
        adapter = _MockAdapter(total_tokens_per_call=500)
        context = {"history": []}

        from services.agent.agent_tool_loop import execute_tool_loop
        await execute_tool_loop(
            svc, adapter, context, task_id, "hermes", "sess3",
            _make_model_cfg(), "test prompt",
        )

        # 无预算 → 仍记录真实用量（不再静默归零）
        cursor = await db.execute("SELECT used_tokens FROM tasks WHERE id=?", (task_id,))
        row = await cursor.fetchone()
        assert row[0] == 500   # 一轮 500 tokens，无论有无预算都要记账


# ═══════════════════════════════════════════════════════════════
# AUDIT #13: resume_task 支持 paused_budget
# ═══════════════════════════════════════════════════════════════

class TestResumePausedBudget:
    """AUDIT #13: resume_task 支持恢复 paused_budget 状态"""

    async def test_resume_paused_budget_returns_true(self, db):
        """paused_budget 状态的 task 可以被 resume"""
        task_id = "test-resume-001"
        await db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, session_id, "
            "max_budget_tokens, used_tokens, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (task_id, "test", "paused_budget", "hermes", "sess-r",
             1000, 1200, int(time.time())),
        )
        await db.commit()

        from services.agent.agent_service import AgentService
        svc = AgentService.__new__(AgentService)
        svc.db = db
        svc._task_handles = {}
        svc._task_completion_events = {}  # _mark_task 需要

        # mock _create_bg_task 避免真正启动 agent
        bg_started = []
        async def _fake_run_agent(*args, **kwargs):
            bg_started.append(args)
        def _fake_create_bg_task(coro, label=""):
            coro.close()
            return asyncio.ensure_future(_fake_run_agent())

        svc._create_bg_task = _fake_create_bg_task

        ok = await svc.resume_task(task_id)
        assert ok is True
        # 状态应改为 running
        cursor = await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
        row = await cursor.fetchone()
        assert row[0] == "running"

    async def test_resume_completed_returns_false(self, db):
        """completed 状态的 task 不能被 resume"""
        task_id = "test-resume-002"
        await db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (task_id, "test", "completed", "hermes", "sess-c", int(time.time())),
        )
        await db.commit()

        from services.agent.agent_service import AgentService
        svc = AgentService.__new__(AgentService)
        svc.db = db
        svc._task_handles = {}
        svc._task_completion_events = {}

        ok = await svc.resume_task(task_id)
        assert ok is False


# ═══════════════════════════════════════════════════════════════
# Batch 3.1: resolve_context_window 支持 context_window 字段
# ═══════════════════════════════════════════════════════════════

class TestResolveContextWindow:
    """Batch 3.1: resolve_context_window 应识别 context_window 字段。"""

    def test_context_key_takes_priority(self):
        """context 字段优先于 context_window。"""
        from config import resolve_context_window
        cfg = {"context": 64000, "context_window": 128000}
        assert resolve_context_window(cfg) == 64000

    def test_context_window_fallback(self):
        """无 context 时，context_window 字段生效。"""
        from config import resolve_context_window
        cfg = {"context_window": 256000}
        assert resolve_context_window(cfg) == 256000

    def test_model_id_fallback_when_no_context_keys(self):
        """无 context/context_window 时回退到 model_id 匹配。"""
        from config import resolve_context_window
        cfg = {"model": "gpt-4o"}
        assert resolve_context_window(cfg) == 131_072
