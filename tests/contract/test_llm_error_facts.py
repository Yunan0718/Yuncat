"""tests/contract/test_llm_error_facts.py —— 状态码优先分类 + 重试事实 + request-id

2026-09-10（借鉴 deepseek-harness 的错误层）：
- 先用 HTTP 状态定码（AUTH/RATE_LIMIT/SERVER/INVALID_REQUEST），文案分类只细化 400/429；
- 捕获 Retry-After（秒数或 HTTP-date）供上层退避；
- 捕获 provider request-id（x-request-id / x-deepseek-request-id）供排障对账。
"""
import os
import sys

import pytest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from services.llm_errors import (  # noqa: E402
    AUTH,
    CONTEXT_WINDOW_EXCEEDED,
    INVALID_REQUEST,
    QUOTA_EXCEEDED,
    RATE_LIMIT,
    SERVER,
    code_for_status,
    error_facts,
    parse_retry_after_ms,
    request_id_from_headers,
)


# ---- 状态码优先分类 ----

def test_auth_statuses():
    assert code_for_status(401) == AUTH
    assert code_for_status(403) == AUTH


def test_rate_limit_and_server():
    assert code_for_status(429) == RATE_LIMIT
    assert code_for_status(500) == SERVER
    assert code_for_status(503) == SERVER


def test_413_is_invalid_request():
    assert code_for_status(413) == INVALID_REQUEST


def test_400_refined_by_detail():
    assert code_for_status(400) == INVALID_REQUEST
    assert code_for_status(400, "maximum context length exceeded") == CONTEXT_WINDOW_EXCEEDED


def test_quota_wins_over_generic_status():
    # 402/400 携带配额文案时归 QUOTA_EXCEEDED（上层要提示充值而非重试）
    assert code_for_status(402, "insufficient quota") == QUOTA_EXCEEDED


def test_unknown_status_falls_back_to_http_code():
    assert code_for_status(418) == "HTTP_418"


# ---- Retry-After ----

def test_retry_after_seconds():
    assert parse_retry_after_ms("30") == 30_000
    assert parse_retry_after_ms(" 5 ") == 5_000


def test_retry_after_http_date():
    future = datetime.now(timezone.utc) + timedelta(seconds=20)
    ms = parse_retry_after_ms(future.strftime("%a, %d %b %Y %H:%M:%S GMT"))
    assert ms is not None and 15_000 <= ms <= 21_000


def test_retry_after_invalid_or_past():
    assert parse_retry_after_ms(None) is None
    assert parse_retry_after_ms("") is None
    assert parse_retry_after_ms("0") is None
    assert parse_retry_after_ms("not-a-date") is None
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert parse_retry_after_ms(past.strftime("%a, %d %b %Y %H:%M:%S GMT")) is None


# ---- request-id ----

def test_request_id_headers():
    assert request_id_from_headers({"x-request-id": "abc-123"}) == "abc-123"
    assert request_id_from_headers({"x-deepseek-request-id": "ds-9"}) == "ds-9"
    assert request_id_from_headers({"other": "x"}) is None
    assert request_id_from_headers(None) is None


# ---- 结构化错误事实 ----

def test_error_facts_from_provider_body():
    body = '{"error": {"code": "rate_limit_exceeded", "type": "rate", "message": "Too many requests"}}'
    facts = error_facts(429, {"retry-after": "12", "x-request-id": "rid-1"}, body)
    assert facts["error_code"] == RATE_LIMIT
    assert facts["status"] == 429
    assert facts["message"] == "Too many requests"
    assert facts["retry_after_ms"] == 12_000
    assert facts["request_id"] == "rid-1"


def test_error_facts_handles_malformed_body():
    facts = error_facts(500, {}, "<html>bad gateway</html>")
    assert facts["error_code"] == SERVER
    assert "HTTP 500" in facts["message"]
    assert facts["retry_after_ms"] is None


def test_error_facts_context_overflow():
    body = '{"error": {"message": "This model maximum context length is 128000 tokens"}}'
    facts = error_facts(400, {}, body)
    assert facts["error_code"] == CONTEXT_WINDOW_EXCEEDED


# ---- 结构化异常与重试闭环（2026-09-10）----

def test_retriable_for_code_is_code_driven():
    from services.llm_errors import retriable_for_code

    assert retriable_for_code(RATE_LIMIT) is True
    assert retriable_for_code(SERVER) is True
    assert retriable_for_code("HTTP_503") is True
    assert retriable_for_code(AUTH) is False
    assert retriable_for_code(QUOTA_EXCEEDED) is False
    assert retriable_for_code(CONTEXT_WINDOW_EXCEEDED) is False


def test_llm_call_error_from_stream_error_chunk():
    from services.llm_errors import LlmCallError

    err = LlmCallError.from_stream_error({
        "type": "stream_error", "data": "Too many requests", "error_code": RATE_LIMIT,
        "status": 429, "request_id": "rid-9", "retry_after_ms": 7000,
    })
    assert err.error_code == RATE_LIMIT
    assert err.retriable is True
    assert err.retry_after == 7.0          # retry_async 直接采用（秒）
    assert err.request_id == "rid-9"
    assert "Too many requests" in str(err)


def test_llm_call_error_permanent_is_not_retriable():
    from services.llm_errors import LlmCallError

    err = LlmCallError("bad key", error_code=AUTH, status=401)
    assert err.retriable is False
    assert err.retry_after is None


@pytest.mark.asyncio
async def test_retry_async_honors_explicit_retriable_flag():
    """显式 retriable=False 时首轮即抛出（不靠文案关键词）；True 时按 retry_after 重试。"""
    from services.llm_errors import AUTH, RATE_LIMIT, LlmCallError
    from retry_utils import retry_async

    calls = {"n": 0}

    async def _fatal():
        calls["n"] += 1
        raise LlmCallError("invalid api key", error_code=AUTH, status=401)

    with pytest.raises(LlmCallError):
        await retry_async(_fatal, max_retries=3, base_delay=0.01, label="t")
    assert calls["n"] == 1, "永久性错误不应重试"

    async def _flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise LlmCallError("rate limited", error_code=RATE_LIMIT, status=429, retry_after_ms=1)
        return "ok"

    calls["n"] = 0
    assert await retry_async(_flaky, max_retries=2, base_delay=0.01, label="t") == "ok"
    assert calls["n"] == 2
