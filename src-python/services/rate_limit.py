"""services/rate_limit.py —— Batch 7.5: 内存 Token Bucket API 限流

单用户桌面应用场景：无需 Redis，按 client host 分桶。
严格 / 写 / 读 三级阈值，429 返回 Retry-After。
"""
from __future__ import annotations

import asyncio
import time as _time
from typing import Callable

from config import (
    RATE_LIMIT_ENABLED,
    RATE_LIMIT_READ_RPS,
    RATE_LIMIT_READ_BURST,
    RATE_LIMIT_WRITE_RPS,
    RATE_LIMIT_WRITE_BURST,
    RATE_LIMIT_STRICT_RPS,
    RATE_LIMIT_STRICT_BURST,
    RATE_LIMIT_STRICT_PATHS,
    RATE_LIMIT_EXEMPT_PATHS,
)


class TokenBucket:
    """异步 Token Bucket。"""

    def __init__(self, rate: float, capacity: int):
        if rate <= 0:
            raise ValueError("rate must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._rate = float(rate)
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._last_update = _time.monotonic()
        self._lock = asyncio.Lock()

    async def is_allowed(self, clock: Callable[[], float] | None = None) -> tuple[bool, float]:
        """尝试消费 1 token，返回 (allowed, retry_after)。"""
        now = clock() if clock else _time.monotonic()
        async with self._lock:
            elapsed = now - self._last_update
            # 时钟回拨保护（测试注入 clock 可能小于 last_update）
            if elapsed > 0:
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            elif elapsed < 0:
                self._last_update = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True, 0.0
            retry_after = max(0.0, (1.0 - self._tokens) / self._rate)
            return False, retry_after


class RateLimiter:
    """按 key 维护一组 TokenBucket，按 method/path 分级。"""

    def __init__(self):
        self._buckets: dict[str, TokenBucket] = {}
        self._buckets_lock = asyncio.Lock()

    def _classify(self, method: str, path: str) -> str:
        """返回 strict / write / read。"""
        upper_method = method.upper()
        for entry in RATE_LIMIT_STRICT_PATHS:
            if ":" in entry:
                emethod, epath = entry.split(":", 1)
                if upper_method == emethod and path.startswith(epath):
                    return "strict"
            elif path.startswith(entry):
                return "strict"
        if upper_method in ("POST", "PUT", "DELETE", "PATCH"):
            return "write"
        return "read"

    def _bucket_params(self, level: str) -> tuple[float, int]:
        if level == "strict":
            return RATE_LIMIT_STRICT_RPS, RATE_LIMIT_STRICT_BURST
        if level == "write":
            return RATE_LIMIT_WRITE_RPS, RATE_LIMIT_WRITE_BURST
        return RATE_LIMIT_READ_RPS, RATE_LIMIT_READ_BURST

    def _is_exempt(self, path: str) -> bool:
        for prefix in RATE_LIMIT_EXEMPT_PATHS:
            if path.startswith(prefix):
                return True
        return False

    async def is_allowed(
        self,
        key: str,
        method: str,
        path: str,
        clock: Callable[[], float] | None = None,
    ) -> tuple[bool, float]:
        """判断请求是否允许通过。"""
        if not RATE_LIMIT_ENABLED or self._is_exempt(path):
            return True, 0.0
        level = self._classify(method, path)
        bucket_key = f"{key}:{level}"
        async with self._buckets_lock:
            bucket = self._buckets.get(bucket_key)
            if bucket is None:
                rate, capacity = self._bucket_params(level)
                bucket = TokenBucket(rate, capacity)
                self._buckets[bucket_key] = bucket
        return await bucket.is_allowed(clock)

    async def reset(self) -> None:
        """清空所有 bucket——主要用于测试。"""
        async with self._buckets_lock:
            self._buckets.clear()


# 模块级单例
_limiter: RateLimiter | None = None
_limiter_lock = asyncio.Lock()


async def get_rate_limiter() -> RateLimiter:
    """获取全局 RateLimiter 单例。"""
    global _limiter
    if _limiter is None:
        async with _limiter_lock:
            if _limiter is None:
                _limiter = RateLimiter()
    return _limiter


def set_rate_limiter(limiter: RateLimiter | None) -> None:
    """设置/替换全局限流器——供测试注入 mock。"""
    global _limiter
    _limiter = limiter
