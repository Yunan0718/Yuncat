"""tests/unit/test_llm_tracing.py —— Task 7.26: LLM 全链路追踪

验证 per-LLM-call latency / cost / token 全链路追踪——轻量级方案
（不依赖 OpenTelemetry SDK，扩展现有 analytics_events 基础设施）。

对标:
  - OpenTelemetry tracing: per-span latency / attributes
  - LangSmith: per-LLM-call latency / cost / token
  - Helicone: LLM observability dashboard

测试策略:
  1. _track_token_usage 包含 duration_ms + round 字段
  2. _accumulate_round_tokens 传递 duration_ms + round_num
  3. AnalyticsService.get_llm_call_metrics(task_id) 查询接口
  4. 向后兼容——不传新参数时行为不变
"""
import asyncio
import json as _json
import os
import sys

import pytest
from services.llm_usage import TOKEN_USAGE_EVENT_SQL

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ============================================================
# Mock 工具（复用 test_token_cost.py 的模式）
# ============================================================

class _MockAdapter:
    def __init__(self, usage: dict):
        self.last_usage = usage


class _MockAnalytics:
    def __init__(self):
        self.tracked: list[dict] = []

    async def track(self, event_type: str, data: dict | None = None) -> None:
        self.tracked.append({"event_type": event_type, "data": data})


class _MockSvc:
    def __init__(self, db, with_analytics: bool = True):
        self.db = db
        if with_analytics:
            self._analytics_service = _MockAnalytics()


@pytest.fixture
async def tracing_db(tmp_path):
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_tracing.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


# ============================================================
# _track_token_usage 包含 duration_ms + round 字段测试
# ============================================================

class TestTrackTokenCostTracingFields:
    """Task 7.26: _track_token_usage 包含 duration_ms + round 字段"""

    async def test_duration_ms_in_event_data(self, tracing_db):
        """duration_ms 传入时写入 event_data"""
        from services.agent.agent_tool_loop import _accumulate_round_tokens
        usage = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
        adapter = _MockAdapter(usage)
        cfg = {
            "provider": "deepseek", "model": "deepseek-chat",
            "pricing": {"input_per_1k": 0.001, "output_per_1k": 0.002},
        }
        svc = _MockSvc(tracing_db, with_analytics=True)

        await _accumulate_round_tokens(
            svc, adapter, "task-trace-1", 0, "hermes", cfg,
            duration_ms=2500, round_num=3,
        )

        assert len(svc._analytics_service.tracked) == 1
        event = svc._analytics_service.tracked[0]
        assert event["data"]["duration_ms"] == 2500
        assert event["data"]["round"] == 3

    async def test_round_field_in_event_data(self, tracing_db):
        """round 字段表示工具循环第几轮 LLM 调用"""
        from services.agent.agent_tool_loop import _accumulate_round_tokens
        usage = {"prompt_tokens": 500, "completion_tokens": 200, "total_tokens": 700}
        adapter = _MockAdapter(usage)
        cfg = {
            "provider": "openai", "model": "gpt-4o",
            "pricing": {"input_per_1k": 0.005, "output_per_1k": 0.015},
        }
        svc = _MockSvc(tracing_db, with_analytics=True)

        await _accumulate_round_tokens(
            svc, adapter, "task-trace-2", 0, "openclaw", cfg,
            duration_ms=800, round_num=1,
        )

        event = svc._analytics_service.tracked[0]
        assert event["data"]["round"] == 1
        assert event["data"]["duration_ms"] == 800
        assert event["data"]["agent_id"] == "openclaw"

    async def test_backward_compat_no_duration_no_round(self, tracing_db):
        """不传 duration_ms / round_num 时向后兼容（默认 0）"""
        from services.agent.agent_tool_loop import _accumulate_round_tokens
        usage = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
        adapter = _MockAdapter(usage)
        cfg = {
            "provider": "deepseek", "model": "deepseek-chat",
            "pricing": {"input_per_1k": 0.001, "output_per_1k": 0.002},
        }
        svc = _MockSvc(tracing_db, with_analytics=True)

        # 不传 duration_ms / round_num（向后兼容原签名）
        await _accumulate_round_tokens(
            svc, adapter, "task-trace-3", 0, "hermes", cfg,
        )

        event = svc._analytics_service.tracked[0]
        assert event["data"]["duration_ms"] == 0
        assert event["data"]["round"] == 0

    async def test_direct_insert_includes_tracing_fields(self, tracing_db, monkeypatch):
        """analytics 不可用时直接 INSERT 也包含 duration_ms + round"""
        import context as _ctx
        monkeypatch.setattr(_ctx, "_app", None, raising=False)

        from services.agent.agent_tool_loop import _accumulate_round_tokens
        usage = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
        adapter = _MockAdapter(usage)
        cfg = {
            "provider": "deepseek", "model": "deepseek-chat",
            "pricing": {"input_per_1k": 0.001, "output_per_1k": 0.002},
        }
        svc = _MockSvc(tracing_db, with_analytics=False)

        await _accumulate_round_tokens(
            svc, adapter, "task-trace-4", 0, "hermes", cfg,
            duration_ms=1500, round_num=2,
        )

        cursor = await tracing_db.execute(
            "SELECT event_data FROM analytics_events "
            f"WHERE event_type IN {TOKEN_USAGE_EVENT_SQL}"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        data = _json.loads(rows[0][0])
        assert data["duration_ms"] == 1500
        assert data["round"] == 2


# ============================================================
# AnalyticsService.get_llm_call_metrics 查询接口测试
# ============================================================

class TestGetLlmCallMetrics:
    """Task 7.26: AnalyticsService.get_llm_call_metrics(task_id) 查询 per-LLM-call 指标"""

    async def test_returns_empty_for_no_data(self, tracing_db):
        """无 token 用量事件时返回空列表"""
        from services.analytics_service import AnalyticsService
        analytics = AnalyticsService(tracing_db)
        result = await analytics.get_llm_call_metrics("nonexistent-task")
        assert result == []

    async def test_returns_metrics_for_task(self, tracing_db):
        """返回指定 task_id 的所有 LLM 调用指标"""
        from services.analytics_service import AnalyticsService
        analytics = AnalyticsService(tracing_db)

        # 模拟 3 轮 LLM 调用
        for i in range(1, 4):
            await analytics.track("token_usage", {
                "task_id": "task-metrics-1",
                "agent_id": "hermes",
                "provider": "deepseek",
                "model": "deepseek-chat",
                "prompt_tokens": 1000 * i,
                "completion_tokens": 500 * i,
                "total_tokens": 1500 * i,
                "cost_usd": 0.002 * i,
                "duration_ms": 1000 * i,
                "round": i,
            })
        await analytics.flush_all()

        result = await analytics.get_llm_call_metrics("task-metrics-1")
        assert len(result) == 3
        # 按 round 排序
        rounds = [r["round"] for r in result]
        assert rounds == [1, 2, 3]

    async def test_metrics_contain_all_fields(self, tracing_db):
        """返回的指标包含所有字段：round, duration_ms, tokens"""
        from services.analytics_service import AnalyticsService
        analytics = AnalyticsService(tracing_db)
        await analytics.track("token_usage", {
            "task_id": "task-metrics-2",
            "agent_id": "hermes",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "prompt_tokens": 2000,
            "completion_tokens": 800,
            "total_tokens": 2800,
            "duration_ms": 3200,
            "round": 5,
        })
        await analytics.flush_all()

        result = await analytics.get_llm_call_metrics("task-metrics-2")
        assert len(result) == 1
        m = result[0]
        assert m["round"] == 5
        assert m["duration_ms"] == 3200
        assert m["prompt_tokens"] == 2000
        assert m["completion_tokens"] == 800
        assert m["total_tokens"] == 2800
        assert m["model"] == "deepseek-chat"
        assert m["agent_id"] == "hermes"

    async def test_filters_by_task_id(self, tracing_db):
        """只返回指定 task_id 的指标（不混入其他 task）"""
        from services.analytics_service import AnalyticsService
        analytics = AnalyticsService(tracing_db)
        await analytics.track("token_usage", {
            "task_id": "task-A", "agent_id": "hermes",
            "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "duration_ms": 500, "round": 1,
        })
        await analytics.track("token_usage", {
            "task_id": "task-B", "agent_id": "hermes",
            "prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300, "duration_ms": 800, "round": 1,
        })
        await analytics.flush_all()

        result_a = await analytics.get_llm_call_metrics("task-A")
        result_b = await analytics.get_llm_call_metrics("task-B")
        assert len(result_a) == 1
        assert len(result_b) == 1
        assert result_a[0]["task_id"] == "task-A"
        assert result_b[0]["task_id"] == "task-B"

    async def test_sorted_by_round_ascending(self, tracing_db):
        """结果按 round 升序排列（便于查看调用序列）"""
        from services.analytics_service import AnalyticsService
        analytics = AnalyticsService(tracing_db)
        # 故意乱序插入
        for r in [3, 1, 2]:
            await analytics.track("token_usage", {
                "task_id": "task-sort", "agent_id": "hermes",
                "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "duration_ms": 500, "round": r,
            })
        await analytics.flush_all()

        result = await analytics.get_llm_call_metrics("task-sort")
        rounds = [r["round"] for r in result]
        assert rounds == [1, 2, 3]

    async def test_summary_aggregation(self, tracing_db):
        """get_llm_call_summary 返回聚合统计：总延迟、总成本、总 token、调用次数"""
        from services.analytics_service import AnalyticsService
        analytics = AnalyticsService(tracing_db)
        for i in range(1, 4):
            await analytics.track("token_usage", {
                "task_id": "task-summary",
                "agent_id": "hermes",
                "provider": "deepseek",
                "model": "deepseek-chat",
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "total_tokens": 1500,
                "duration_ms": 1000,
                "round": i,
            })
        await analytics.flush_all()

        summary = await analytics.get_llm_call_summary("task-summary")
        assert summary["call_count"] == 3
        assert summary["total_duration_ms"] == 3000
        assert summary["total_tokens"] == 4500
        assert summary["total_prompt_tokens"] == 3000
        assert summary["total_completion_tokens"] == 1500
