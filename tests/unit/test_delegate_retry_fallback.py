"""tests/unit/test_delegate_retry_fallback.py —— Task 6.7: delegate_to_agent 重试 + fallback

对标:
- Temporal Activity 自动重试（首次失败后重试 1 次，共 2 次尝试）
- Temporal Fallback Worker（目标 Worker 不可用时切换到备用 Worker）

原问题: 目标 Agent 返回失败或异常时直接返回错误，不重试、不备选 Agent、不降级。
修复: delegate_to_agent 加 1 次重试 + 失败后尝试备选 Agent + 仍失败则降级返回错误信息。

覆盖:
- 首次成功（无重试无 fallback）
- 首次失败 → 重试成功（retried=True）
- 重试失败 → fallback 成功（fallback="hermes"/"openclaw"）
- 全部失败 → 降级返回错误（success=False, retried=True, fallback=None 或 attempted）
- 防递归仍生效（不触发重试/fallback）
- 备选 Agent 不存在时不尝试 fallback
- 重试间隔退避（DELEGATE_RETRY_BACKOFF_SEC）
- delegate_tool 抛异常时正确处理
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture(autouse=True)
def _reset_sem():
    """每个测试前重置信号量，避免上一个测试的 acquire 计数泄漏。"""
    from tools.builtin.system_tool_config import _reset_delegate_sem
    _reset_delegate_sem()
    yield
    _reset_delegate_sem()


def _build_adapters(oc_supports=True, ha_supports=True):
    """构造 mock adapters dict——OC 和 HA 各自是否支持 execute_tool。"""
    class _FakeAdapter:
        def __init__(self, supports=True):
            self._supports = supports

        async def execute_tool(self, tool_name, arguments):
            return {"ok": True, "result": "fake"}

    class _FakeAdapters:
        def __init__(self):
            self._map = {}
            if oc_supports:
                self._map["openclaw"] = _FakeAdapter(oc_supports)
            if ha_supports:
                self._map["hermes"] = _FakeAdapter(ha_supports)

        def get(self, name):
            return self._map.get(name)

        def keys(self):
            return list(self._map.keys())

        def __contains__(self, name):
            return name in self._map

    return _FakeAdapters()


class TestDelegateRetryFallback:
    """Task 6.7: delegate_to_agent 重试 + fallback 行为"""

    async def test_success_first_try_no_retry_no_fallback(self, monkeypatch):
        """首次成功时不重试、不 fallback"""
        from tools.builtin.system_tool_config import delegate_to_agent

        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: _build_adapters())
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        call_count = 0
        async def _mock_delegate(**kwargs):
            nonlocal call_count
            call_count += 1
            return {"ok": True, "result": "success"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        result = await delegate_to_agent("openclaw", "some_tool", {"x": 1})

        assert result["success"] is True
        assert call_count == 1  # 只调用 1 次（无重试）
        assert result["meta"]["retried"] is False
        assert result["meta"]["fallback"] is None
        assert result["data"]["delegated_to"] == "openclaw"

    async def test_first_fail_retry_success(self, monkeypatch):
        """首次失败 → 重试成功（retried=True, fallback=None）"""
        from tools.builtin.system_tool_config import delegate_to_agent
        from config import DELEGATE_RETRY_BACKOFF_SEC

        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: _build_adapters())
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        # 记录 sleep 调用以验证退避
        sleep_calls = []
        async def _mock_sleep(sec):
            sleep_calls.append(sec)
        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        call_count = 0
        async def _mock_delegate(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"ok": False, "error": "transient failure"}
            return {"ok": True, "result": "recovered"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        result = await delegate_to_agent("openclaw", "tool", {})

        assert result["success"] is True
        assert call_count == 2  # 首次 + 重试 1 次
        assert result["meta"]["retried"] is True
        assert result["meta"]["fallback"] is None
        # 退避 sleep 被调用 1 次，间隔 = DELEGATE_RETRY_BACKOFF_SEC
        assert sleep_calls == [DELEGATE_RETRY_BACKOFF_SEC]

    async def test_retry_exhausted_fails_without_silent_switch(self, monkeypatch):
        """重试耗尽**不会**静默切到别的外部 Agent——直接失败。

        2026-09-10 决策：`DELEGATE_FALLBACK_MAP` 刻意留空。静默回退会让用户以为自己
        点名的后端生效了、实际执行者是另一个（甚至可能没启动），比明确失败更糟。
        """
        from config import DELEGATE_FALLBACK_MAP
        from tools.builtin.system_tool_config import delegate_to_agent

        assert DELEGATE_FALLBACK_MAP == {}, "回退映射应为空——外部 Agent 之间不静默互替"

        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: _build_adapters())
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        async def _mock_sleep(sec):
            pass
        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        call_targets = []
        async def _mock_delegate(from_agent_id, to_agent_id, tool_name, arguments, adapters):
            call_targets.append(to_agent_id)
            return {"ok": False, "error": "OC down"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        result = await delegate_to_agent("openclaw", "tool", {})

        assert result["success"] is False
        # 只在 openclaw 上重试，绝不出现第三个目标
        assert call_targets == ["openclaw", "openclaw"]
        assert result["meta"]["fallback"] is None
        assert result["meta"]["retried"] is True
        # 无 fallback → 最终目标仍是原点名的 agent（不会变成 hermes）
        assert result.get("data", {}).get("delegated_to", "openclaw") != "hermes"

    async def test_hermes_to_openclaw_fallback(self, monkeypatch):
        """点名 hermes 失败时**不会**换到 openclaw——外部 Agent 之间不静默互替"""
        from tools.builtin.system_tool_config import delegate_to_agent

        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: _build_adapters())
        monkeypatch.setattr("context.get_current_agent_id", lambda: "openclaw")

        async def _mock_sleep(sec):
            pass
        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        call_targets = []
        async def _mock_delegate(from_agent_id, to_agent_id, tool_name, arguments, adapters):
            call_targets.append(to_agent_id)
            if to_agent_id == "hermes":
                return {"ok": False, "error": "HA down"}
            return {"ok": True, "result": "OC handled it"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        result = await delegate_to_agent("hermes", "tool", {})

        # 点名 hermes 就只跑 hermes——不因为失败就换 openclaw（见上一条的决策说明）
        assert result["success"] is False
        assert call_targets == ["hermes", "hermes"]
        assert result["meta"]["fallback"] is None

    async def test_all_fail_degraded_return(self, monkeypatch):
        """全部失败（target 重试 + fallback 都失败）→ 降级返回错误"""
        from tools.builtin.system_tool_config import delegate_to_agent

        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: _build_adapters())
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        async def _mock_sleep(sec):
            pass
        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        call_count = 0
        async def _mock_delegate(**kwargs):
            nonlocal call_count
            call_count += 1
            return {"ok": False, "error": f"attempt {call_count} failed"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        result = await delegate_to_agent("openclaw", "tool", {})

        assert result["success"] is False
        # 1 次首发 + 1 次重试 = 2 次尝试（无 fallback 目标）
        assert call_count == 2
        assert result["meta"]["retried"] is True
        assert result["meta"]["fallback"] is None
        # observation 包含失败信息
        assert "失败" in result["observation"]

    async def test_anti_recursion_still_works(self, monkeypatch):
        """防递归检查仍生效——委托 delegate_to_agent 自身立即失败，不触发重试/fallback"""
        from tools.builtin.system_tool_config import delegate_to_agent

        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: _build_adapters())
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        call_count = 0
        async def _mock_delegate(**kwargs):
            nonlocal call_count
            call_count += 1
            return {"ok": True}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        result = await delegate_to_agent("openclaw", "delegate_to_agent", {})

        assert result["success"] is False
        assert "不能委托 delegate_to_agent 自身" in result["error"]
        # 防递归在信号量前快速失败，delegate_tool 从未被调用
        assert call_count == 0

    async def test_fallback_agent_not_available(self, monkeypatch):
        """备选 Agent 不存在（不在 adapters）时不尝试 fallback"""
        from tools.builtin.system_tool_config import delegate_to_agent

        # 只有 OC，没有 HA——fallback 到 hermes 会失败
        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: _build_adapters(oc_supports=True, ha_supports=False))
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        async def _mock_sleep(sec):
            pass
        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        call_count = 0
        async def _mock_delegate(**kwargs):
            nonlocal call_count
            call_count += 1
            return {"ok": False, "error": "OC always fails"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        result = await delegate_to_agent("openclaw", "tool", {})

        assert result["success"] is False
        # 只调 target 2 次（重试），无 fallback（hermes 不在 adapters）
        assert call_count == 2
        assert result["meta"]["fallback"] is None

    async def test_fallback_agent_no_execute_tool(self, monkeypatch):
        """备选 Agent 不支持 execute_tool 时不尝试 fallback（跳过 fallback）"""
        from tools.builtin.system_tool_config import delegate_to_agent

        # target=openclaw 支持 execute_tool；fallback=hermes 不支持
        class _NoExecAdapter:
            pass

        class _OCAdapter:
            async def execute_tool(self, tool_name, arguments):
                return {"ok": False, "error": "fail"}

        class _MixedAdapters:
            def get(self, name):
                if name == "openclaw":
                    return _OCAdapter()
                if name == "hermes":
                    return _NoExecAdapter()  # 无 execute_tool
                return None

            def keys(self):
                return ["openclaw", "hermes"]

            def __contains__(self, name):
                return name in ["openclaw", "hermes"]

        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: _MixedAdapters())
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        async def _mock_sleep(sec):
            pass
        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        call_count = 0
        async def _mock_delegate(**kwargs):
            nonlocal call_count
            call_count += 1
            return {"ok": False, "error": "OC fail"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        result = await delegate_to_agent("openclaw", "tool", {})

        assert result["success"] is False
        # 只调 openclaw 2 次（重试），hermes fallback 因无 execute_tool 被跳过
        assert call_count == 2
        assert result["meta"]["fallback"] is None

    async def test_delegate_tool_exception_handled(self, monkeypatch):
        """delegate_tool 抛异常时 _call_delegate 捕获并返回失败"""
        from tools.builtin.system_tool_config import delegate_to_agent

        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: _build_adapters())
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        async def _mock_sleep(sec):
            pass
        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        call_count = 0
        async def _mock_delegate(**kwargs):
            nonlocal call_count
            call_count += 1
            raise RuntimeError(f"boom #{call_count}")
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        result = await delegate_to_agent("openclaw", "tool", {})

        assert result["success"] is False
        # 异常被捕获并重试；无 fallback 目标 → 2 次尝试
        assert call_count == 2  # 首发 + 重试

    async def test_observation_contains_retry_info(self, monkeypatch):
        """observation 包含重试次数信息"""
        from tools.builtin.system_tool_config import delegate_to_agent

        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: _build_adapters())
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        async def _mock_sleep(sec):
            pass
        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        call_count = 0
        async def _mock_delegate(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"ok": False, "error": "fail"}
            return {"ok": True, "result": "ok"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        result = await delegate_to_agent("openclaw", "tool", {})

        assert result["success"] is True
        # observation 应包含 "重试 1 次后成功"
        assert "重试" in result["observation"]
        assert "成功" in result["observation"]

    async def test_event_published_with_retry_metadata(self, monkeypatch):
        """事件发布包含 fallback/attempts 元信息"""
        from tools.builtin.system_tool_config import delegate_to_agent

        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: _build_adapters())
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        async def _mock_sleep(sec):
            pass
        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        async def _mock_delegate(from_agent_id, to_agent_id, tool_name, arguments, adapters):
            if to_agent_id == "openclaw":
                return {"ok": False, "error": "OC down"}
            return {"ok": True, "result": "HA handled"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        # 捕获发布的事件
        published_events = []
        class _FakeEventBus:
            async def publish(self, event_type, payload):
                published_events.append((event_type, payload))

        monkeypatch.setattr("services.event_bus.get_event_bus", lambda: _FakeEventBus())
        monkeypatch.setattr("services.event_bus.Events", type("E", (), {"CROSS_BACKEND_DELEGATION": "cross_backend_delegation"}))

        result = await delegate_to_agent("openclaw", "tool", {})

        assert result["success"] is False
        # 验证事件包含重试元信息与「未发生 fallback」的事实
        delegation_events = [(t, p) for t, p in published_events if t == "cross_backend_delegation"]
        assert len(delegation_events) == 1
        payload = delegation_events[0][1]
        assert payload["fallback"] is None
        assert payload["attempts"] == 2
        # 目标始终是 openclaw 且它一直失败 → 明确失败，而不是悄悄换 hermes 成功
        assert payload["success"] is False
        assert payload["target_agent"] == "openclaw"


class TestCallDelegateHelper:
    """Task 6.7: _call_delegate 辅助函数单元测试"""

    async def test_returns_success_when_ok_true(self, monkeypatch):
        """result.ok=True → (True, result)"""
        from tools.builtin.system_tool_config import _call_delegate

        async def _mock_delegate(**kwargs):
            return {"ok": True, "result": "done"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        success, result = await _call_delegate("openclaw", "tool", {}, {}, "hermes")
        assert success is True
        assert result["ok"] is True

    async def test_returns_fail_when_ok_false(self, monkeypatch):
        """result.ok=False → (False, result)"""
        from tools.builtin.system_tool_config import _call_delegate

        async def _mock_delegate(**kwargs):
            return {"ok": False, "error": "failed"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        success, result = await _call_delegate("openclaw", "tool", {}, {}, "hermes")
        assert success is False
        assert result["error"] == "failed"

    async def test_returns_success_when_success_key_true(self, monkeypatch):
        """result.success=True（无 ok 键）→ (True, result)——对齐 delegate_tool 判定逻辑"""
        from tools.builtin.system_tool_config import _call_delegate

        async def _mock_delegate(**kwargs):
            return {"success": True, "data": "done"}  # 无 ok 键
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        success, result = await _call_delegate("openclaw", "tool", {}, {}, "hermes")
        assert success is True

    async def test_exception_returns_false(self, monkeypatch):
        """delegate_tool 抛异常 → (False, {"ok": False, "error": ...})"""
        from tools.builtin.system_tool_config import _call_delegate

        async def _mock_delegate(**kwargs):
            raise RuntimeError("network error")
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        success, result = await _call_delegate("openclaw", "tool", {}, {}, "hermes")
        assert success is False
        assert result["ok"] is False
        assert "network error" in result["error"]
