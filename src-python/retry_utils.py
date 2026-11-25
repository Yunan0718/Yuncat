"""src-python/retry_utils.py —— Task 6.16 共享重试策略

统一 long_task.executor（拓扑并行执行器）与 agent_lifecycle._execute_plan_steps_impl
（Plan 顺序执行器）两条 plan 执行路径的重试策略。

主流做法对标：
  - OpenAI / Anthropic SDK：网络错误 + 429 自动重试（指数退避 + jitter）
  - tenacity 库：retryIfExceptionType + ExponentialBackoff
  - 本模块：精简实现，按错误关键词判定可重试性，指数退避（2^n * base_delay）

设计决策：
  - 可重试错误 = 瞬时错误（网络超时/限流/文件锁/服务不可用）——重试有意义
  - 不可重试错误 = 永久错误（语法错误/参数错误/逻辑错误）——重试无意义，直接失败
  - 共享常量 + 谓词，让两条执行路径重试策略保持一致（避免漂移）
  - retry_async 通用包装器：供需要重试的异步调用使用（adapter.stream / spawn_subagent 等）

Task 7.4（重试退避加 jitter + Retry-After）：
  - 指数退避加 jitter：delay = min(backoff, MAX_BACKOFF) + uniform(0, backoff*0.5)
    防 thundering herd——多客户端同一瞬时错误后不会同步重试压垮服务
  - backoff 上限 MAX_BACKOFF=60s——避免长退避阻塞任务过久
  - Retry-After 支持：异常含 retry_after 属性时优先使用（服务器指定，不加 jitter）
    向前兼容——普通 Exception 无此属性，走指数退避路径；adapter 可逐步抛含 retry_after 的异常
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

_log = logging.getLogger("yuncat.retry")

T = TypeVar("T")

# 重试次数（不含首次尝试）——对标主流 SDK 默认 2-3 次
MAX_RETRIES = 2

# 指数退避基数（秒）——首次重试 2s，二次 4s，三次 8s
RETRY_BASE_DELAY = 2.0

# Task 7.4: 退避上限（秒）——避免长退避阻塞任务过久。对标 AWS SDK 默认 60s
MAX_BACKOFF = 60.0

# Task 7.4: jitter 系数——delay += uniform(0, backoff * RETRY_JITTER_FACTOR)
# 0.5 对标 tenacity randomize_exponential_backoff 的常见取值，防 thundering herd
RETRY_JITTER_FACTOR = 0.5

# 可重试错误关键词（网络超时/模型限流/文件锁/服务不可用）
RETRIABLE_KEYWORDS = (
    "timeout", "timed out", "rate limit", "rate_limit", "429",
    "file lock", "temporarily", "connection reset", "overloaded",
    "service unavailable", "503", "502", "eof occurred",
)


def is_retriable(error: str | None) -> bool:
    """判断错误是否可重试（网络超时/模型限流/文件锁/服务不可用）

    Args:
        error: 错误字符串（异常 str() 或 result.error）

    Returns:
        True 表示可重试（瞬时错误），False 表示不可重试（永久错误）
    """
    err_lower = (error or "").lower()
    return any(kw in err_lower for kw in RETRIABLE_KEYWORDS)


async def retry_async(
    factory: Callable[[], Awaitable[T]],
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY,
    label: str = "operation",
) -> T:
    """通用异步重试包装器——可重试异常重试，不可重试异常直接抛出

    Args:
        factory: 无参异步工厂，每次调用返回 Awaitable（重试时重新调用）
        max_retries: 最大重试次数（不含首次尝试）
        base_delay: 退避基数（秒），实际延迟 = min(base_delay * 2^attempt, MAX_BACKOFF)
                    + uniform(0, backoff * RETRY_JITTER_FACTOR)
        label: 操作标签（日志用）

    Returns:
        factory 的成功返回值

    Raises:
        可重试异常耗尽重试后抛出最后一个异常；
        不可重试异常首次即抛出。

    Task 7.4:
        - 异常含 retry_after 属性时优先使用（服务器指定，不加 jitter，受 MAX_BACKOFF 约束）
        - 普通异常走指数退避 + jitter（向前兼容）
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):  # 1 次初始 + max_retries 次重试
        try:
            return await factory()
        except Exception as exc:
            last_exc = exc
            err = str(exc)
            # 2026-09-10: 结构化错误自带 retriable 判据时优先（不猜文案关键词）
            explicit = getattr(exc, "retriable", None)
            if explicit is False:
                raise  # 明确的永久性错误（AUTH/QUOTA/上下文超限等），直接抛出
            if explicit is None and not is_retriable(err):
                raise  # 无显式判据时退回文案关键词分类
            if attempt < max_retries:
                # Task 7.4: Retry-After 优先（服务器指定），否则指数退避 + jitter
                retry_after = getattr(exc, "retry_after", None)
                if retry_after is not None:
                    # 服务器明确指定等待时间，直接使用，不加 jitter，但受 MAX_BACKOFF 约束
                    delay = min(float(retry_after), MAX_BACKOFF)
                else:
                    backoff = min(base_delay * (2 ** attempt), MAX_BACKOFF)
                    delay = backoff + random.uniform(0, backoff * RETRY_JITTER_FACTOR)
                _log.warning(
                    "%s attempt %d failed (retriable: %s), retrying in %.1fs",
                    label, attempt + 1, err[:120], delay,
                )
                await asyncio.sleep(delay)
    # 耗尽重试——抛出最后一个异常（mypy 需要显式断言）
    assert last_exc is not None
    raise last_exc
