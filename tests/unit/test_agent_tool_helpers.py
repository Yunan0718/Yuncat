"""tests/unit/test_agent_tool_helpers.py —— Task 7.4 stream_with_fallback 测试

覆盖 Task 7.4（重试退避加 jitter + Retry-After）的核心行为：
  - 主 provider 成功 → 不降级
  - 同一 provider 重试 1 次后再切换（仅在 stream 未产出 chunk 时重试）
  - stream 已产出 chunk 后不重试（避免重复 chunk，对标 OpenAI/Anthropic SDK）
  - 401/403 认证错误 → 直接 return，不降级
  - 异常含 retry_after 属性 → 内层退避用 retry_after（不加 jitter）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from retry_utils import MAX_BACKOFF, RETRY_JITTER_FACTOR
from services.agent.agent_tool_helpers import stream_with_fallback


class _MockAdapter:
    """模拟 Adapter——按预设 scenarios 依次产出 chunk 或抛异常。

    每次 stream() 调用消费一个 scenario：
      scenario = [("yield", chunk), ..., ("raise", exc)]
    先 yield 所有 chunk，再 raise（若有）。
    """

    def __init__(self, scenarios: list[list[tuple]]):
        self.scenarios = scenarios
        self.call_count = 0

    async def stream(self, prompt, context=None):
        idx = self.call_count
        self.call_count += 1
        scenario = self.scenarios[idx] if idx < len(self.scenarios) else []
        for action, payload in scenario:
            if action == "yield":
                yield payload
            elif action == "raise":
                raise payload


class _MockModelConfig:
    """模拟 ModelConfigService——list_providers / get_provider_config。"""

    def __init__(self, providers: list[dict]):
        self.providers = providers

    async def list_providers(self):
        return self.providers

    async def get_provider_config(self, provider_id: str):
        for p in self.providers:
            if p["provider"] == provider_id:
                return p
        return None


class _MockSelf:
    """模拟 AgentService 实例——提供 stream_with_fallback 需要的依赖。"""

    def __init__(self, adapter: _MockAdapter, providers: list[dict], ws_connected: bool = True):
        self.adapters = {"hermes": adapter}
        self._model_config = _MockModelConfig(providers)
        self._ws_connections = set() if ws_connected else None
        self.sent_messages: list[dict] = []

    async def _safe_send(self, task_id: str, msg: dict):
        self.sent_messages.append(msg)


class _RateLimitError(Exception):
    """带 retry_after 属性的限流异常——模拟 adapter 解析 Retry-After header 后抛出。"""

    def __init__(self, msg: str, retry_after: float):
        super().__init__(msg)
        self.retry_after = retry_after


class TestStreamWithFallbackTask74:
    """Task 7.4: stream_with_fallback 重试/降级行为"""

    async def test_primary_provider_success(self, monkeypatch):
        """主 provider 首次成功 → 直接 yield chunk，不降级、不重试"""
        async def _no_sleep(_):
            pass
        monkeypatch.setattr("asyncio.sleep", _no_sleep)

        adapter = _MockAdapter([
            [("yield", {"text": "hello"}), ("yield", {"text": " world"})],
        ])
        svc = _MockSelf(adapter, providers=[
            {"provider": "deepseek", "api_key_configured": True, "api_key": "k1"},
        ])

        chunks = []
        async for chunk in stream_with_fallback(
            svc, agent_id="hermes", prompt="hi", context={"model_config": {}},
            primary_provider="deepseek", task_id="t1",
        ):
            chunks.append(chunk)

        assert chunks == [{"text": "hello"}, {"text": " world"}]
        assert adapter.call_count == 1
        # 不应有降级通知
        assert not any(m["type"] == "model_degraded" for m in svc.sent_messages)

    async def test_retry_same_provider_before_fallback(self, monkeypatch):
        """主 provider 失败（未 yield chunk）→ 重试 1 次 → 仍失败 → 切换到备选"""
        sleep_calls: list[float] = []

        async def _capture_sleep(delay):
            sleep_calls.append(delay)
        monkeypatch.setattr("asyncio.sleep", _capture_sleep)

        # call 1: 主 provider inner_attempt=0 失败
        # call 2: 主 provider inner_attempt=1 失败（重试）
        # call 3: 备选 provider 成功
        adapter = _MockAdapter([
            [("raise", ConnectionError("timeout"))],
            [("raise", ConnectionError("timeout"))],
            [("yield", {"text": "recovered"})],
        ])
        svc = _MockSelf(adapter, providers=[
            {"provider": "deepseek", "api_key_configured": True, "api_key": "k1"},
            {"provider": "openai", "api_key_configured": True, "api_key": "k2"},
        ])

        chunks = []
        async for chunk in stream_with_fallback(
            svc, agent_id="hermes", prompt="hi", context={"model_config": {}},
            primary_provider="deepseek", task_id="t1",
        ):
            chunks.append(chunk)

        # 备选 provider 产出 chunk
        assert chunks == [{"text": "recovered"}]
        # 主 provider 调用 2 次（初始 + 重试）+ 备选 1 次
        assert adapter.call_count == 3
        # 应有降级通知
        assert any(m["type"] == "model_degraded" for m in svc.sent_messages)
        # 1 次内层退避（inner_attempt=0, backoff=2 → delay ∈ [2,3]）
        # 1 次切换退避（switch_backoff=2 → delay ∈ [2,3]）
        assert len(sleep_calls) == 2
        assert all(2.0 <= d <= 3.0 for d in sleep_calls)

    async def test_no_retry_after_chunk_yielded(self, monkeypatch):
        """主 provider stream 中途失败（已 yield chunk）→ 不重试，直接切换"""
        sleep_calls: list[float] = []

        async def _capture_sleep(delay):
            sleep_calls.append(delay)
        monkeypatch.setattr("asyncio.sleep", _capture_sleep)

        # call 1: 主 provider yield 1 chunk 后失败
        # call 2: 备选 provider 成功
        adapter = _MockAdapter([
            [("yield", {"text": "partial"}), ("raise", ConnectionError("connection reset"))],
            [("yield", {"text": " ok"})],
        ])
        svc = _MockSelf(adapter, providers=[
            {"provider": "deepseek", "api_key_configured": True, "api_key": "k1"},
            {"provider": "openai", "api_key_configured": True, "api_key": "k2"},
        ])

        chunks = []
        async for chunk in stream_with_fallback(
            svc, agent_id="hermes", prompt="hi", context={"model_config": {}},
            primary_provider="deepseek", task_id="t1",
        ):
            chunks.append(chunk)

        # 主 provider 的 partial chunk + 备选的 ok chunk
        assert chunks == [{"text": "partial"}, {"text": " ok"}]
        # 主 provider 只调用 1 次（已 yield chunk，不重试）+ 备选 1 次
        assert adapter.call_count == 2
        # 只有 1 次切换退避（无内层重试）
        assert len(sleep_calls) == 1
        assert 2.0 <= sleep_calls[0] <= 3.0

    async def test_auth_error_no_fallback(self, monkeypatch):
        """401 错误 → 直接 return，不降级"""
        async def _no_sleep(_):
            pass
        monkeypatch.setattr("asyncio.sleep", _no_sleep)

        adapter = _MockAdapter([
            [("raise", Exception("401 Unauthorized"))],
        ])
        svc = _MockSelf(adapter, providers=[
            {"provider": "deepseek", "api_key_configured": True, "api_key": "k1"},
            {"provider": "openai", "api_key_configured": True, "api_key": "k2"},
        ])

        chunks = []
        async for chunk in stream_with_fallback(
            svc, agent_id="hermes", prompt="hi", context={"model_config": {}},
            primary_provider="deepseek", task_id="t1",
        ):
            chunks.append(chunk)

        # 无 chunk 产出
        assert chunks == []
        # 只调用主 provider 1 次
        assert adapter.call_count == 1
        # 应有 error 通知（不是 model_degraded）
        assert any(m["type"] == "error" for m in svc.sent_messages)
        assert not any(m["type"] == "model_degraded" for m in svc.sent_messages)

    async def test_retry_after_used_as_inner_delay(self, monkeypatch):
        """异常含 retry_after 属性 → 内层退避用 retry_after（不加 jitter）"""
        sleep_calls: list[float] = []

        async def _capture_sleep(delay):
            sleep_calls.append(delay)
        monkeypatch.setattr("asyncio.sleep", _capture_sleep)

        # call 1: 主 provider 失败 (retry_after=7)
        # call 2: 主 provider 重试成功
        adapter = _MockAdapter([
            [("raise", _RateLimitError("rate limit exceeded", retry_after=7.0))],
            [("yield", {"text": "ok"})],
        ])
        svc = _MockSelf(adapter, providers=[
            {"provider": "deepseek", "api_key_configured": True, "api_key": "k1"},
        ])

        chunks = []
        async for chunk in stream_with_fallback(
            svc, agent_id="hermes", prompt="hi", context={"model_config": {}},
            primary_provider="deepseek", task_id="t1",
        ):
            chunks.append(chunk)

        assert chunks == [{"text": "ok"}]
        # 只有 1 次内层退避，delay = retry_after = 7.0（无 jitter）
        assert sleep_calls == [7.0]

    async def test_retry_after_capped_at_max(self, monkeypatch):
        """retry_after > MAX_BACKOFF 时内层退避被封顶"""
        sleep_calls: list[float] = []

        async def _capture_sleep(delay):
            sleep_calls.append(delay)
        monkeypatch.setattr("asyncio.sleep", _capture_sleep)

        # call 1: 主 provider 失败 (retry_after=300，超过 MAX_BACKOFF=60)
        # call 2: 主 provider 重试成功
        adapter = _MockAdapter([
            [("raise", _RateLimitError("rate limit exceeded", retry_after=300.0))],
            [("yield", {"text": "ok"})],
        ])
        svc = _MockSelf(adapter, providers=[
            {"provider": "deepseek", "api_key_configured": True, "api_key": "k1"},
        ])

        chunks = []
        async for chunk in stream_with_fallback(
            svc, agent_id="hermes", prompt="hi", context={"model_config": {}},
            primary_provider="deepseek", task_id="t1",
        ):
            chunks.append(chunk)

        assert chunks == [{"text": "ok"}]
        # delay = min(retry_after=300, MAX_BACKOFF=60) = 60.0
        assert sleep_calls == [60.0]

    async def test_switch_backoff_has_jitter(self, monkeypatch):
        """切换退避含 jitter——多次执行 delay 有变化"""
        import random as _random

        async def _capture_sleep(delay):
            captured.append(delay)

        captured: list[float] = []
        monkeypatch.setattr("asyncio.sleep", _capture_sleep)

        # 主 provider 两次都失败 → 切换到备选成功
        # call 1: 主 provider inner_attempt=0 失败
        # call 2: 主 provider inner_attempt=1 失败（重试）
        # call 3: 备选成功
        seen_delays = set()
        for seed in range(10):
            _random.seed(seed)
            captured.clear()
            adapter = _MockAdapter([
                [("raise", ConnectionError("timeout"))],
                [("raise", ConnectionError("timeout"))],
                [("yield", {"text": "ok"})],
            ])
            svc = _MockSelf(adapter, providers=[
                {"provider": "deepseek", "api_key_configured": True, "api_key": "k1"},
                {"provider": "openai", "api_key_configured": True, "api_key": "k2"},
            ])

            chunks = []
            async for chunk in stream_with_fallback(
                svc, agent_id="hermes", prompt="hi", context={"model_config": {}},
                primary_provider="deepseek", task_id="t1",
            ):
                chunks.append(chunk)

            # 1 次内层退避（inner_attempt=0）+ 1 次切换退避
            # 切换退避是最后一次 sleep
            assert chunks == [{"text": "ok"}]
            assert len(captured) == 2
            seen_delays.add(round(captured[-1], 4))

        # 切换退避 jitter ∈ [0, 1.0]，不同 seed 应产生不同 delay
        assert len(seen_delays) >= 2
