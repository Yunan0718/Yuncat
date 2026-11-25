"""tests/contract/test_token_usage_contract.py —— token 用量字段名/事件类型的「唯一真源」守卫

2026-09-14 起因（一次真实回归）：
适配器层把 usage 归一化成 ``cached_tokens`` 后，``agent_finalize.py`` 仍在读
DeepSeek 专有的 ``prompt_cache_hit_tokens`` → **缓存命中恒为 0**，前端缓存命中率静默归零。
根因不是某一行写错，而是**同一份字段名在写入端与读取端各写了一份**（还有第三种写法
``cache_hit_tokens`` 散在事件体里）。

修复方式：字段名与事件类型集中到 ``services/llm_usage.py`` 的常量，
本文件守住三件事——
1. 归一化输出恰好等于 ``CANONICAL_USAGE_FIELDS``；
2. **生产者不得再写旧键名**（旧名只允许出现在读取兜底处，且需在允许名单内）；
3. **消费者不得再硬编码事件类型字面量**（一律走 TOKEN_USAGE_EVENTS/SQL），
   否则改名时又会出现"写入端换了名字、读取端还在查老名字"的静默空集。
"""
import os
import re
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
PY_SRC = os.path.join(ROOT, "src-python")

sys.path.insert(0, PY_SRC)

from services.llm_usage import (  # noqa: E402
    CANONICAL_USAGE_FIELDS,
    LEGACY_USAGE_FIELDS,
    TOKEN_USAGE_EVENTS,
    TOKEN_USAGE_EVENT_SQL,
    normalize_usage,
)

# 允许出现旧字段名的文件——**必须都是读取兜底**（为了兼容本地历史事件），
# 不允许新增生产者。名单变长就意味着又有人在写旧名。
LEGACY_NAMES_ALLOWED_IN = {
    "services/llm_usage.py",      # 定义 + 读取兜底
    "routes/system.py",           # 缓存命中率聚合：兼容历史事件体
}

# 允许出现事件类型字面量的文件（定义处 + 测试）
EVENT_LITERAL_ALLOWED_IN = {"services/llm_usage.py"}

# 事件写入端——往 analytics_events 写 token 用量事件的文件。
# 新增写入端必须登记到这里，否则"旧字段名复活"不会被发现。
EVENT_WRITER_FILES = (
    "services/agent/budget.py",
    "services/agent/agent_finalize.py",
)

_SKIP_DIRS = {"__pycache__", "python-embed", "node-embed", ".venv"}


def _iter_python_sources():
    for dirpath, dirnames, filenames in os.walk(PY_SRC):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, PY_SRC).replace("\\", "/")
            yield rel, full


def _strip_comments(text: str) -> str:
    """去掉整行注释与行尾注释。

    只用于扫描——注释里出现旧字段名是有价值的（说明"为什么改名"），
    不应被当成违规。字符串里含 # 的极端情况对本扫描无影响。
    """
    out = []
    for line in text.split("\n"):
        idx = line.find("#")
        out.append(line if idx < 0 else line[:idx])
    return "\n".join(out)


# ═══════════════════════════════════════════════════════════════
# 1. 归一化输出 = 规范字段集
# ═══════════════════════════════════════════════════════════════

def test_normalize_returns_exactly_canonical_fields():
    out = normalize_usage({"prompt_tokens": 1, "completion_tokens": 2})
    assert tuple(out.keys()) == CANONICAL_USAGE_FIELDS


def test_normalize_output_is_stable_under_missing_input():
    """字段集不能因输入缺失而变——消费者按固定键取值，缺键会静默变 0。"""
    assert tuple(normalize_usage(None).keys()) == CANONICAL_USAGE_FIELDS


def test_cache_miss_derived_when_provider_omits_it():
    """厂商只给命中不给未命中时按 prompt - cached 推导（DeepSeek 的 hit/miss 互补）。"""
    out = normalize_usage({"prompt_tokens": 100, "cached_tokens": 80})
    assert out["cached_tokens"] == 80
    assert out["cache_miss_tokens"] == 20


def test_cache_miss_explicit_value_wins_over_derivation():
    out = normalize_usage({"prompt_tokens": 100, "cached_tokens": 80, "cache_miss_tokens": 999})
    assert out["cache_miss_tokens"] == 999


def test_legacy_deepseek_keys_still_readable():
    """历史适配器/历史事件写的是 DeepSeek 旧键名——读取端必须仍能解析。"""
    out = normalize_usage({"prompt_tokens": 100, "prompt_cache_hit_tokens": 70,
                           "prompt_cache_miss_tokens": 30})
    assert out["cached_tokens"] == 70 and out["cache_miss_tokens"] == 30


# ═══════════════════════════════════════════════════════════════
# 2. 生产者不得再写旧键名
# ═══════════════════════════════════════════════════════════════

def test_no_producer_writes_legacy_field_names():
    """**事件写入端**不得出现任何旧字段名。

    只扫写入端而不是全仓：像 ``routes/system.py`` 的 API 响应体为了兼容前端
    仍返回 ``cache_hit_tokens`` 这个键名，那是**接口形状**而不是事件字段，
    在这里一刀切会让扫描失去意义（真正的风险是"写进事件后没人读得到"）。
    """
    offenders = []
    for rel in EVENT_WRITER_FILES:
        full = os.path.join(PY_SRC, rel)
        if not os.path.exists(full):
            continue
        text = _strip_comments(open(full, encoding="utf-8", errors="ignore").read())
        for legacy in LEGACY_USAGE_FIELDS:
            if legacy in text:
                offenders.append(f"{rel}: {legacy}")
    assert offenders == [], (
        "事件写入端出现旧字段名 —— 请使用 services/llm_usage.py 的 "
        f"FIELD_* 常量（否则读取端会静默读到 0）: {offenders}"
    )


def test_no_legacy_field_names_outside_read_fallback_allowlist():
    """旧字段名只允许出现在「读取兜底」的两个文件里。"""
    offenders = []
    for rel, full in _iter_python_sources():
        if rel in LEGACY_NAMES_ALLOWED_IN:
            continue
        text = _strip_comments(open(full, encoding="utf-8", errors="ignore").read())
        for legacy in LEGACY_USAGE_FIELDS:
            if legacy in text:
                offenders.append(f"{rel}: {legacy}")
    assert offenders == [], (
        "以下文件出现旧字段名 —— 生产者应改走 services/llm_usage.py 的常量，"
        f"读取兜底需显式加入允许名单: {offenders}"
    )


# ═══════════════════════════════════════════════════════════════
# 3. 消费者不得硬编码事件类型
# ═══════════════════════════════════════════════════════════════

def test_no_consumer_hardcodes_token_event_type():
    pattern = re.compile(r"event_type\s*(?:=|IN)\s*['\"]?(token_usage|token_cost)")
    offenders = []
    for rel, full in _iter_python_sources():
        if rel in EVENT_LITERAL_ALLOWED_IN:
            continue
        text = _strip_comments(open(full, encoding="utf-8", errors="ignore").read())
        for m in pattern.finditer(text):
            offenders.append(f"{rel}: event_type {m.group(1)}")
    assert offenders == [], (
        "以下位置硬编码了 token 事件类型 —— 请改用 "
        f"services.llm_usage.TOKEN_USAGE_EVENT_SQL（改名时会静默查空）: {offenders}"
    )


def test_event_constants_are_consistent():
    assert "token_usage" in TOKEN_USAGE_EVENTS
    # 改名前的历史类型必须保留在可查集合内，否则本地历史数据直接作废
    assert "token_cost" in TOKEN_USAGE_EVENTS
    for ev in TOKEN_USAGE_EVENTS:
        assert f"'{ev}'" in TOKEN_USAGE_EVENT_SQL


# ═══════════════════════════════════════════════════════════════
# 4. 消费端行为：缓存命中率 / 每日用量 / 上下文占用
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
async def db(tmp_path):
    import aiosqlite
    conn = await aiosqlite.connect(os.path.join(str(tmp_path), "token_contract.db"))
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()


async def _seed(db, event_type: str, data: dict):
    import json as _json
    await db.execute(
        "INSERT INTO analytics_events(event_type, event_data) VALUES(?,?)",
        (event_type, _json.dumps(data, ensure_ascii=False)),
    )
    await db.commit()


class _FakeRequest:
    def __init__(self, db):
        class _State:
            pass
        self.app = type("_App", (), {"state": _State()})()
        self.app.state.db = db


class TestCacheStatsReadsCanonicalFields:

    async def test_canonical_keys_are_aggregated(self, db):
        """生产者写的是 cached_tokens —— 聚合必须读到（回归守卫）。"""
        from routes.system import cache_stats
        await _seed(db, "token_usage", {"cached_tokens": 800, "cache_miss_tokens": 200})
        out = await cache_stats(days=7, request=_FakeRequest(db))
        assert out["cache_hit_tokens"] == 800
        assert out["cache_miss_tokens"] == 200
        assert out["hit_rate_pct"] == 80.0

    async def test_legacy_events_still_counted(self, db):
        from routes.system import cache_stats
        await _seed(db, "token_cost", {"cache_hit_tokens": 50, "cache_miss_tokens": 50})
        out = await cache_stats(days=7, request=_FakeRequest(db))
        assert out["cache_hit_tokens"] == 50
        assert out["hit_rate_pct"] == 50.0


class TestDailyUsageCoversBothEventTypes:

    async def test_both_event_types_aggregate(self, db):
        """改名前后的事件都要能聚合——否则历史用量凭空消失。"""
        from services.token_tracker import get_daily_usage
        await _seed(db, "token_usage", {"prompt_tokens": 100, "completion_tokens": 50})
        await _seed(db, "token_cost", {"prompt_tokens": 10, "completion_tokens": 5})
        daily = await get_daily_usage(db, days=30)
        total = sum(d["total_tokens"] for d in daily)
        assert total == 165

    async def test_empty_db_returns_empty_list(self, db):
        from services.token_tracker import get_daily_usage
        assert await get_daily_usage(db, days=7) == []


class TestContextUsagePrefersRealUsage:

    async def test_db_branch_resolves_real_prompt_tokens(self, db):
        """优先级 2（DB 真实用量）必须真的生效——否则上下文占用永远只是估算值。"""
        from services.token_tracker import estimate_context_usage
        await _seed(db, "token_usage", {"prompt_tokens": 5000, "session_id": "s1"})
        out = await estimate_context_usage(db, "s1", [{"role": "user", "content": "hi"}])
        assert out["source"] == "db_incremental"
        assert out["last_real_prompt_tokens"] == 5000

    async def test_falls_back_to_estimation_for_unknown_session(self, db):
        from services.token_tracker import estimate_context_usage
        out = await estimate_context_usage(db, "unknown", [{"role": "user", "content": "hi"}])
        assert out["source"] != "db_incremental"
        assert out["estimated_tokens"] >= 0
