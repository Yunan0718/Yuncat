"""tests/unit/test_rate_limit.py —— Batch 7.5: API 限流测试

使用最小 FastAPI app + TestClient 验证 Token Bucket 行为。
"""
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from services.rate_limit import RateLimiter, TokenBucket, set_rate_limiter


@pytest.fixture(autouse=True)
def reset_limiter():
    """每个测试前重置全局限流器。"""
    set_rate_limiter(None)
    yield
    set_rate_limiter(None)


def _build_app(rate: float = 1.0, capacity: int = 2, strict_paths=("POST:/strict",)) -> FastAPI:
    """构造带限流中间件的最小 app。limiter 在 app 生命周期内唯一，确保状态跨请求保持。"""
    app = FastAPI()
    limiter = RateLimiter()
    set_rate_limiter(limiter)

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        with mock.patch("services.rate_limit.RATE_LIMIT_ENABLED", True), \
             mock.patch("services.rate_limit.RATE_LIMIT_READ_RPS", rate), \
             mock.patch("services.rate_limit.RATE_LIMIT_READ_BURST", capacity), \
             mock.patch("services.rate_limit.RATE_LIMIT_WRITE_RPS", rate), \
             mock.patch("services.rate_limit.RATE_LIMIT_WRITE_BURST", capacity), \
             mock.patch("services.rate_limit.RATE_LIMIT_STRICT_RPS", rate), \
             mock.patch("services.rate_limit.RATE_LIMIT_STRICT_BURST", capacity), \
             mock.patch("services.rate_limit.RATE_LIMIT_STRICT_PATHS", strict_paths), \
             mock.patch("services.rate_limit.RATE_LIMIT_EXEMPT_PATHS", ("/health", "/auth")):
            client = request.client
            key = f"ip:{client.host}" if client else "ip:unknown"
            allowed, retry_after = await limiter.is_allowed(key, request.method, request.url.path)
            if allowed:
                return await call_next(request)
            from schemas.error_response import error_response
            response = error_response(
                status_code=429,
                message="too fast",
                code="RATE_LIMITED",
                details={"retry_after": round(retry_after, 2)},
            )
            response.headers["Retry-After"] = str(int(retry_after) + 1)
            return response

    @app.get("/read")
    async def read():
        return {"ok": True}

    @app.post("/write")
    async def write():
        return {"ok": True}

    @app.post("/strict")
    async def strict():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


class TestTokenBucket:
    @pytest.mark.asyncio
    async def test_burst_then_refill(self):
        """容量为 2 时前两次通过，第三次阻塞，时间推进后恢复。"""
        bucket = TokenBucket(rate=1.0, capacity=2)
        clock = 0.0

        allowed, _ = await bucket.is_allowed(lambda: clock)
        assert allowed is True
        allowed, _ = await bucket.is_allowed(lambda: clock)
        assert allowed is True
        allowed, wait = await bucket.is_allowed(lambda: clock)
        assert allowed is False
        assert wait > 0

        clock += 1.5
        allowed, _ = await bucket.is_allowed(lambda: clock)
        assert allowed is True


class TestRateLimiterClassification:
    @pytest.mark.asyncio
    async def test_read_path_classified_as_read(self):
        limiter = RateLimiter()
        with mock.patch("services.rate_limit.RATE_LIMIT_STRICT_PATHS", ("POST:/strict",)):
            level = limiter._classify("GET", "/read")
        assert level == "read"

    @pytest.mark.asyncio
    async def test_post_path_classified_as_write(self):
        limiter = RateLimiter()
        with mock.patch("services.rate_limit.RATE_LIMIT_STRICT_PATHS", ("POST:/strict",)):
            level = limiter._classify("POST", "/write")
        assert level == "write"

    @pytest.mark.asyncio
    async def test_strict_path_classified_as_strict(self):
        limiter = RateLimiter()
        with mock.patch("services.rate_limit.RATE_LIMIT_STRICT_PATHS", ("POST:/strict",)):
            level = limiter._classify("POST", "/strict")
        assert level == "strict"


class TestMiddleware:
    def test_burst_exceeded_returns_429(self):
        app = _build_app(rate=1.0, capacity=2)
        client = TestClient(app)

        assert client.get("/read").status_code == 200
        assert client.get("/read").status_code == 200
        resp = client.get("/read")
        assert resp.status_code == 429
        body = resp.json()
        assert body["code"] == "RATE_LIMITED"
        assert "retry_after" in body["details"]
        assert "Retry-After" in resp.headers

    def test_exempt_path_not_limited(self):
        app = _build_app(rate=1.0, capacity=1)
        client = TestClient(app)

        for _ in range(5):
            assert client.get("/health").status_code == 200

    def test_strict_path_limited(self):
        app = _build_app(rate=1.0, capacity=1, strict_paths=("POST:/strict",))
        client = TestClient(app)

        assert client.post("/strict").status_code == 200
        resp = client.post("/strict")
        assert resp.status_code == 429

    def test_different_levels_have_separate_buckets(self):
        app = _build_app(rate=1.0, capacity=1)
        client = TestClient(app)

        # 读层级先耗尽
        assert client.get("/read").status_code == 200
        assert client.get("/read").status_code == 429

        # 写层级仍可用
        assert client.post("/write").status_code == 200
