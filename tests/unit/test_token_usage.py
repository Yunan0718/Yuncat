"""tests/unit/test_token_usage.py —— token 用量统计（去价格化后的口径）

2026-09-10 用户决策：**只统计 token 消耗，不再统计价格**。
背景（调研结论，详见 services/llm_usage.py 模块注释）：
- 厂商没有"逐次用量查询"API（只有余额/配额窗口），token 统计的主流做法是
  **本地账本**——累计每次响应自带的 usage；
- 旧 `calc_cost` 读 `pricing.input_per_1k`，而 YAML 存的是每百万价 → 恒为 0，
  价格统计从未真正工作过，正好随本次一起移除；
- 流式必须显式发 `stream_options: {"include_usage": true}` 才有 usage。

覆盖：
1. `normalize_usage` 纯函数——OpenAI 嵌套明细 / DeepSeek 平铺 / 缺失字段；
2. `_accumulate_round_tokens` 集成——持久化 used_tokens + token_usage 事件（无 cost_usd）；
3. `get_llm_call_summary`——去 total_cost_usd、兼容旧 token_cost 事件、聚合 cached/reasoning。
"""
import asyncio
import json as _json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from services.llm_usage import normalize_usage, stream_usage_options  # noqa: E402


# ═══════════════════════════════════════════════════════════════
# normalize_usage 纯函数
# ═══════════════════════════════════════════════════════════════

class TestNormalizeUsage:

    def test_openai_nested_details(self):
        usage = {
            "prompt_tokens": 1250, "completion_tokens": 500, "total_tokens": 1750,
            "prompt_tokens_details": {"cached_tokens": 1000},
            "completion_tokens_details": {"reasoning_tokens": 200},
        }
        out = normalize_usage(usage)
        assert out == {"prompt_tokens": 1250, "completion_tokens": 500, "total_tokens": 1750,
                       "cached_tokens": 1000, "cache_miss_tokens": 250,
                       "reasoning_tokens": 200}

    def test_deepseek_flat_cache_keys(self):
        out = normalize_usage({"prompt_tokens": 100, "completion_tokens": 50,
                               "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20})
        assert out["total_tokens"] == 150          # 缺 total 时回退相加
        assert out["cached_tokens"] == 80

    def test_total_falls_back_to_sum(self):
        assert normalize_usage({"prompt_tokens": 10, "completion_tokens": 5})["total_tokens"] == 15

    def test_missing_everything_is_zero_not_crash(self):
        assert normalize_usage(None) == {"prompt_tokens": 0, "completion_tokens": 0,
                                         "total_tokens": 0, "cached_tokens": 0,
                                         "cache_miss_tokens": 0, "reasoning_tokens": 0}

    def test_malformed_details_do_not_crash(self):
        out = normalize_usage({"prompt_tokens": 1, "prompt_tokens_details": "x",
                               "completion_tokens_details": 7})
        assert out["prompt_tokens"] == 1 and out["cached_tokens"] == 0

    def test_string_numbers_are_coerced(self):
        """DeepSeek 官方余额接口的数字是字符串——usage 字段同样做防御性转换。"""
        out = normalize_usage({"prompt_tokens": "12", "completion_tokens": "3", "total_tokens": "15"})
        assert out == {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15,
                       "cached_tokens": 0, "cache_miss_tokens": 12,
                       "reasoning_tokens": 0}


def test_stream_usage_options_is_the_openai_switch():
    assert stream_usage_options() == {"stream_options": {"include_usage": True}}


# ═══════════════════════════════════════════════════════════════
# _accumulate_round_tokens 集成
# ═══════════════════════════════════════════════════════════════

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
async def db(tmp_path):
    import aiosqlite
    conn = await aiosqlite.connect(os.path.join(str(tmp_path), "test_usage.db"))
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


class TestAccumulateRoundTokens:

    async def test_usage_tracked_without_any_pricing(self, db):
        """统计不再依赖 pricing——只有 usage 就记账，事件里没有 cost_usd"""
        from services.agent.agent_tool_loop import _accumulate_round_tokens
        usage = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500,
                 "cached_tokens": 800, "reasoning_tokens": 120}
        svc = _MockSvc(db, with_analytics=True)

        result = await _accumulate_round_tokens(
            svc, _MockAdapter(usage), "task-1", 0, "yuncat",
            {"provider": "deepseek", "model": "deepseek-v4-flash"},
        )

        assert result == 1500
        assert len(svc._analytics_service.tracked) == 1
        event = svc._analytics_service.tracked[0]
        assert event["event_type"] == "token_usage"
        d = event["data"]
        assert d["task_id"] == "task-1" and d["agent_id"] == "yuncat"
        assert d["provider"] == "deepseek" and d["model"] == "deepseek-v4-flash"
        assert d["prompt_tokens"] == 1000 and d["completion_tokens"] == 500
        assert d["total_tokens"] == 1500
        assert d["cached_tokens"] == 800 and d["reasoning_tokens"] == 120
        assert "cost_usd" not in d  # 去价格化

    async def test_track_even_without_model_cfg(self, db):
        """没有模型配置也照记 token（provider/model 留空）——用量本身就是有价值的"""
        from services.agent.agent_tool_loop import _accumulate_round_tokens
        svc = _MockSvc(db, with_analytics=True)

        await _accumulate_round_tokens(
            svc, _MockAdapter({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
            "task-2", 0,
        )

        (event,) = svc._analytics_service.tracked
        assert event["event_type"] == "token_usage"
        assert event["data"]["total_tokens"] == 15
        assert event["data"]["provider"] == "" and event["data"]["model"] == ""

    async def test_zero_usage_records_nothing(self, db):
        from services.agent.agent_tool_loop import _accumulate_round_tokens
        svc = _MockSvc(db, with_analytics=True)
        await _accumulate_round_tokens(svc, _MockAdapter({}), "task-3", 0, "yuncat")
        assert svc._analytics_service.tracked == []

    async def test_fallback_direct_insert(self, db, monkeypatch):
        """analytics_service 不可用时直接 INSERT，事件不丢"""
        import context as _ctx
        monkeypatch.setattr(_ctx, "_app", None, raising=False)

        from services.agent.agent_tool_loop import _accumulate_round_tokens
        svc = _MockSvc(db, with_analytics=False)

        await _accumulate_round_tokens(
            svc, _MockAdapter({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
            "task-4", 0, "yuncat", {"provider": "deepseek", "model": "m"},
        )

        cur = await db.execute(
            "SELECT event_type, event_data FROM analytics_events")
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "token_usage"
        assert _json.loads(rows[0][1])["total_tokens"] == 15


# ═══════════════════════════════════════════════════════════════
# get_llm_call_summary：去价格化 + 兼容旧事件
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
async def analytics_db(tmp_path):
    import aiosqlite
    from services.analytics_service import AnalyticsService
    conn = await aiosqlite.connect(os.path.join(str(tmp_path), "test_summary.db"))
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    svc = AnalyticsService(conn)
    yield svc, conn
    await conn.close()


async def _seed(analytics_db, event_type: str, task_id: str, data: dict):
    svc, conn = analytics_db
    await svc.db.execute(
        "INSERT INTO analytics_events(event_type, event_data) VALUES(?,?)",
        (event_type, _json.dumps({"task_id": task_id, **data}, ensure_ascii=False)),
    )
    await svc.db.commit()


class TestLlmCallSummary:

    async def test_summary_has_no_cost_but_has_cached_reasoning(self, analytics_db):
        await _seed(analytics_db, "token_usage", "t1", {
            "prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500,
            "cached_tokens": 800, "reasoning_tokens": 200, "duration_ms": 100, "round": 1,
        })
        svc, _ = analytics_db
        summary = await svc.get_llm_call_summary("t1")
        assert summary["call_count"] == 1
        assert summary["total_tokens"] == 1500
        assert summary["total_cached_tokens"] == 800
        assert summary["total_reasoning_tokens"] == 200
        assert "total_cost_usd" not in summary  # 去价格化

    async def test_legacy_token_cost_events_still_aggregate(self, analytics_db):
        """改名前的历史本地数据无需迁移即可继续聚合"""
        await _seed(analytics_db, "token_cost", "t2", {
            "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
            "cost_usd": 0.002, "duration_ms": 10, "round": 1,
        })
        svc, _ = analytics_db
        summary = await svc.get_llm_call_summary("t2")
        assert summary["call_count"] == 1
        assert summary["total_tokens"] == 150
        assert summary["total_cached_tokens"] == 0  # 旧事件没有该字段，补 0

    async def test_empty_task_returns_zeroed_shape(self, analytics_db):
        svc, _ = analytics_db
        summary = await svc.get_llm_call_summary("nope")
        assert summary["call_count"] == 0
        assert summary["total_reasoning_tokens"] == 0
        assert "total_cost_usd" not in summary
