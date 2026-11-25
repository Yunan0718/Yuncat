"""tests/unit/test_retry_utils.py —— Task 6.16 共享重试策略测试

统一 long_task.executor 与 agent_lifecycle 两条 plan 执行路径的重试策略：
  - is_retriable 判断错误是否可重试（网络/限流/文件锁）
  - 共享常量 MAX_RETRIES / RETRY_BASE_DELAY / RETRIABLE_KEYWORDS
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from retry_utils import (
    MAX_BACKOFF,
    MAX_RETRIES,
    RETRIABLE_KEYWORDS,
    RETRY_BASE_DELAY,
    is_retriable,
    retry_async,
)


class TestIsRetriable:
    """is_retriable 识别可重试错误"""

    def test_timeout_is_retriable(self):
        assert is_retriable("Connection timeout")

    def test_rate_limit_is_retriable(self):
        assert is_retriable("rate limit exceeded")
        assert is_retriable("429 Too Many Requests")

    def test_file_lock_is_retriable(self):
        assert is_retriable("file lock acquired by another process")

    def test_connection_reset_is_retriable(self):
        assert is_retriable("connection reset by peer")

    def test_service_unavailable_is_retriable(self):
        assert is_retriable("503 service unavailable")
        assert is_retriable("502 bad gateway")

    def test_syntax_error_not_retriable(self):
        """编译/语法错误不可重试"""
        assert not is_retriable("SyntaxError: invalid syntax")
        assert not is_retriable("NameError: name 'x' is not defined")

    def test_value_error_not_retriable(self):
        assert not is_retriable("ValueError: invalid argument")

    def test_empty_error_not_retriable(self):
        assert not is_retriable("")
        assert not is_retriable(None)

    def test_case_insensitive(self):
        assert is_retriable("TIMEOUT OCCURRED")
        assert is_retriable("Rate Limit")


class TestSharedConstants:
    """共享常量存在且合理"""

    def test_max_retries_is_positive(self):
        assert MAX_RETRIES >= 1

    def test_retry_base_delay_positive(self):
        assert RETRY_BASE_DELAY > 0

    def test_retriable_keywords_non_empty(self):
        assert len(RETRIABLE_KEYWORDS) > 0
        assert "timeout" in RETRIABLE_KEYWORDS


class TestRetryAsync:
    """retry_async 通用重试包装器"""

    async def test_returns_result_on_first_success(self):
        call_count = 0

        async def factory():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await retry_async(factory, max_retries=3, base_delay=0.01, label="test")
        assert result == "ok"
        assert call_count == 1

    async def test_retries_on_retriable_exception(self, monkeypatch):
        """可重试异常 → 重试到成功"""
        # 禁用真实 sleep
        async def _fake_sleep(_delay):
            return None
        monkeypatch.setattr("asyncio.sleep", _fake_sleep)

        attempts = []

        async def factory():
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionError("timeout connecting to upstream")
            return "success-on-3rd"

        result = await retry_async(factory, max_retries=3, base_delay=0.01, label="test")
        assert result == "success-on-3rd"
        assert len(attempts) == 3

    async def test_no_retry_on_non_retriable(self, monkeypatch):
        async def _fake_sleep(_delay):
            return None
        monkeypatch.setattr("asyncio.sleep", _fake_sleep)

        attempts = []

        async def factory():
            attempts.append(1)
            raise ValueError("invalid argument")

        # 不可重试异常直接抛出，不重试
        try:
            await retry_async(factory, max_retries=3, base_delay=0.01, label="test")
            assert False, "should have raised"
        except ValueError:
            pass
        assert len(attempts) == 1

    async def test_exhausts_retries_then_raises(self, monkeypatch):
        async def _fake_sleep(_delay):
            return None
        monkeypatch.setattr("asyncio.sleep", _fake_sleep)

        attempts = []

        async def factory():
            attempts.append(1)
            raise ConnectionError("timeout")

        try:
            await retry_async(factory, max_retries=2, base_delay=0.01, label="test")
            assert False, "should have raised"
        except ConnectionError:
            pass
        # 1 次初始 + 2 次重试 = 3 次
        assert len(attempts) == 3

    async def test_exponential_backoff_delay(self, monkeypatch):
        """重试延迟按指数退避 + jitter（Task 7.4）

        backoff = base_delay * 2^attempt，delay = backoff + uniform(0, backoff*0.5)
        3 次重试：backoff=2/4/8 → delay ∈ [2,3] / [4,6] / [8,12]
        """
        delays = []

        async def _capture_sleep(delay):
            delays.append(delay)

        monkeypatch.setattr("asyncio.sleep", _capture_sleep)

        async def factory():
            raise ConnectionError("timeout")

        try:
            await retry_async(factory, max_retries=3, base_delay=2.0, label="test")
        except ConnectionError:
            pass
        # 3 次重试：backoff = 2^0*2=2, 2^1*2=4, 2^2*2=8
        # jitter ∈ [0, backoff * 0.5] → delay ∈ [backoff, backoff * 1.5]
        assert len(delays) == 3
        assert 2.0 <= delays[0] <= 3.0   # 2.0 + [0, 1.0]
        assert 4.0 <= delays[1] <= 6.0   # 4.0 + [0, 2.0]
        assert 8.0 <= delays[2] <= 12.0  # 8.0 + [0, 4.0]

    async def test_backoff_has_jitter(self, monkeypatch):
        """Task 7.4: 退避含 jitter——同 attempt 多次执行的 delay 有变化"""
        import random as _random

        captured_per_run: list[float] = []

        async def _capture_sleep(delay):
            captured_per_run.append(delay)

        monkeypatch.setattr("asyncio.sleep", _capture_sleep)

        for seed in range(10):
            _random.seed(seed)

            async def factory():
                raise ConnectionError("timeout")

            try:
                await retry_async(factory, max_retries=1, base_delay=2.0, label="test")
            except ConnectionError:
                pass

        # 10 次执行，每次 1 个 delay
        assert len(captured_per_run) == 10
        # jitter ∈ [0, 1.0]，不同 seed 应产生不同 delay
        assert len(set(round(d, 4) for d in captured_per_run)) >= 2

    async def test_backoff_capped_at_max(self, monkeypatch):
        """Task 7.4: backoff 被 MAX_BACKOFF 封顶"""
        delays = []

        async def _capture_sleep(delay):
            delays.append(delay)

        monkeypatch.setattr("asyncio.sleep", _capture_sleep)

        async def factory():
            raise ConnectionError("timeout")

        # base_delay=1000, max_retries=10 → backoff 应被 cap 到 MAX_BACKOFF（60）
        try:
            await retry_async(factory, max_retries=10, base_delay=1000.0, label="test")
        except ConnectionError:
            pass
        # 所有 delay ≤ MAX_BACKOFF * 1.5（jitter 上限）+ 容差
        for d in delays:
            assert d <= MAX_BACKOFF * 1.5 + 0.01

    async def test_retry_after_used_when_present(self, monkeypatch):
        """Task 7.4: 异常含 retry_after 属性时，delay = retry_after（无 jitter）

        Retry-After 是服务器明确指定的等待时间，应直接使用，不加 jitter。
        向前兼容：普通 Exception 无 retry_after 属性 → 走指数退避路径。
        """
        delays = []

        async def _capture_sleep(delay):
            delays.append(delay)

        monkeypatch.setattr("asyncio.sleep", _capture_sleep)

        class RateLimitError(Exception):
            def __init__(self, msg, retry_after):
                super().__init__(msg)
                self.retry_after = retry_after

        async def factory():
            raise RateLimitError("rate limit exceeded", retry_after=5.0)

        try:
            await retry_async(factory, max_retries=1, base_delay=2.0, label="test")
        except RateLimitError:
            pass

        # delay = retry_after = 5.0（无 jitter）
        assert delays == [5.0]

    async def test_retry_after_capped_at_max(self, monkeypatch):
        """Task 7.4: retry_after > MAX_BACKOFF 时被封顶"""
        delays = []

        async def _capture_sleep(delay):
            delays.append(delay)

        monkeypatch.setattr("asyncio.sleep", _capture_sleep)

        class RateLimitError(Exception):
            def __init__(self, msg, retry_after):
                super().__init__(msg)
                self.retry_after = retry_after

        async def factory():
            raise RateLimitError("rate limit exceeded", retry_after=300.0)

        try:
            await retry_async(factory, max_retries=1, base_delay=2.0, label="test")
        except RateLimitError:
            pass

        # delay = min(retry_after=300, MAX_BACKOFF=60) = 60.0
        assert delays == [60.0]

    async def test_retry_after_forward_compatible(self, monkeypatch):
        """Task 7.4: 普通 Exception 无 retry_after → 走指数退避（向前兼容）"""
        delays = []

        async def _capture_sleep(delay):
            delays.append(delay)

        monkeypatch.setattr("asyncio.sleep", _capture_sleep)

        async def factory():
            raise ConnectionError("timeout")  # 无 retry_after 属性

        try:
            await retry_async(factory, max_retries=1, base_delay=2.0, label="test")
        except ConnectionError:
            pass

        # 走指数退避：backoff=2.0, delay ∈ [2.0, 3.0]
        assert len(delays) == 1
        assert 2.0 <= delays[0] <= 3.0
