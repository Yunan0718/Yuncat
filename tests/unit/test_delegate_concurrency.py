"""tests/unit/test_delegate_concurrency.py —— Task 3.8: delegate_to_agent 全局并发限流

对标:
- 连接池上限（如 DB pool max_connections）——防止过多并发请求耗尽后端连接
- 主流做法: asyncio.Semaphore 限制全局并发数

原问题: delegate_to_agent 无全局并发限流，主 Agent 一轮可发 50 个委托耗尽 OC 后端连接。
已有单任务限流保护，但缺全局信号量。

覆盖:
- 信号量上限 = DELEGATE_CONCURRENCY_MAX
- 并发数超过上限时第 11 个委托被阻塞（等待释放）
- 释放后阻塞的委托恢复执行
- 快速失败请求（防递归/参数过大/无 adapters）不占用信号量槽位
- async with 异常时信号量正确释放
- _reset_delegate_sem 重置信号量状态
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


class TestDelegateConcurrencyLimit:
    """delegate_to_agent 全局并发信号量"""

    async def test_semaphore_value_matches_config(self):
        """信号量初始值 = DELEGATE_CONCURRENCY_MAX"""
        from config import DELEGATE_CONCURRENCY_MAX
        from tools.builtin.system_tool_config import _delegate_semaphore
        # Semaphore._value 是剩余可用槽位（Python 3.10+ 内部属性，测试用）
        assert _delegate_semaphore()._value == DELEGATE_CONCURRENCY_MAX

    async def test_concurrent_delegates_below_max_all_proceed(self, monkeypatch):
        """并发数 <= 上限时所有委托立即执行（不被阻塞）"""
        from config import DELEGATE_CONCURRENCY_MAX
        from tools.builtin.system_tool_config import delegate_to_agent, _delegate_semaphore

        # mock adapters 返回有效 target，delegate_tool 短延迟完成
        class _FakeAdapter:
            async def execute_tool(self, tool_name, arguments):
                await asyncio.sleep(0.05)
                return {"ok": True, "result": "done"}

        class _FakeAdapters:
            def get(self, name):
                return _FakeAdapter() if name == "openclaw" else None

            def keys(self):
                return ["openclaw"]

        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: _FakeAdapters())
        # mock context.get_current_agent_id 避免依赖 contextvars 设置
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        # 发 DELEGATE_CONCURRENCY_MAX 个并发委托——应全部立即执行
        tasks = [
            delegate_to_agent("openclaw", "some_tool", {"x": i})
            for i in range(DELEGATE_CONCURRENCY_MAX)
        ]
        results = await asyncio.gather(*tasks)
        assert all(r["success"] for r in results)
        # 信号量已全部释放
        assert _delegate_semaphore()._value == DELEGATE_CONCURRENCY_MAX

    async def test_exceed_max_blocks_until_release(self, monkeypatch):
        """并发数 > 上限时第 11 个委托被阻塞，释放后恢复"""
        from config import DELEGATE_CONCURRENCY_MAX
        from tools.builtin.system_tool_config import delegate_to_agent, _delegate_semaphore

        # 用 Event 控制前 N 个委托何时完成——保持占用信号量
        hold_event = asyncio.Event()
        completed_order: list[int] = []

        class _FakeAdapter:
            async def execute_tool(self, tool_name, arguments):
                idx = arguments.get("x", -1)
                if idx < DELEGATE_CONCURRENCY_MAX:
                    # 前 N 个持有信号量直到 hold_event.set()
                    await hold_event.wait()
                completed_order.append(idx)
                return {"ok": True, "result": "done"}

        class _FakeAdapters:
            def get(self, name):
                return _FakeAdapter() if name == "openclaw" else None

            def keys(self):
                return ["openclaw"]

        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: _FakeAdapters())
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        # 发 N 个占满信号量 + 1 个应被阻塞
        tasks = [
            asyncio.create_task(delegate_to_agent("openclaw", "tool", {"x": i}))
            for i in range(DELEGATE_CONCURRENCY_MAX + 1)
        ]
        # 让前 N 个启动并 acquire 信号量
        await asyncio.sleep(0.05)
        # 此时信号量应为 0（前 N 个持有），第 N+1 个被阻塞
        assert _delegate_semaphore()._value == 0
        # 第 N+1 个任务未完成（被阻塞）
        assert not tasks[-1].done()

        # 释放前 N 个
        hold_event.set()
        results = await asyncio.gather(*tasks)
        # 全部完成
        assert all(r["success"] for r in results)
        # 第 N+1 个最后完成（它等待前 N 个释放信号量）
        assert completed_order[-1] == DELEGATE_CONCURRENCY_MAX
        # 信号量全部释放
        assert _delegate_semaphore()._value == DELEGATE_CONCURRENCY_MAX

    async def test_fast_fail_does_not_acquire_semaphore(self, monkeypatch):
        """快速失败请求（防递归/参数过大/无 adapters）不占用信号量槽位"""
        from tools.builtin.system_tool_config import delegate_to_agent, _delegate_semaphore

        # 防递归——委托 delegate_to_agent 自身
        result = await delegate_to_agent("openclaw", "delegate_to_agent", {})
        assert result["success"] is False
        # 信号量未减少
        from config import DELEGATE_CONCURRENCY_MAX
        assert _delegate_semaphore()._value == DELEGATE_CONCURRENCY_MAX

        # 参数过大
        result = await delegate_to_agent("openclaw", "tool", {"x": "a" * 100_001})
        assert result["success"] is False
        assert _delegate_semaphore()._value == DELEGATE_CONCURRENCY_MAX

        # 无 adapters
        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: None)
        result = await delegate_to_agent("openclaw", "tool", {})
        assert result["success"] is False
        assert _delegate_semaphore()._value == DELEGATE_CONCURRENCY_MAX

    async def test_exception_releases_semaphore(self, monkeypatch):
        """delegate_tool 抛异常时信号量正确释放（async with 保证）"""
        from tools.builtin.system_tool_config import delegate_to_agent, _delegate_semaphore
        from config import DELEGATE_CONCURRENCY_MAX

        class _FakeAdapter:
            async def execute_tool(self, tool_name, arguments):
                raise RuntimeError("simulated backend failure")

        class _FakeAdapters:
            def get(self, name):
                return _FakeAdapter() if name == "openclaw" else None

            def keys(self):
                return ["openclaw"]

        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: _FakeAdapters())
        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")
        # delegate_tool 内部会捕获 execute_tool 异常，但若 delegate_tool 自身抛异常也需释放
        # 这里 mock delegate_tool 抛异常
        async def _bomb_delegate(**kwargs):
            raise RuntimeError("delegate_tool itself failed")
        monkeypatch.setattr("services.capability_router.delegate_tool", _bomb_delegate)

        result = await delegate_to_agent("openclaw", "tool", {})
        # 异常被 except 捕获，返回失败 ToolResult
        assert result["success"] is False
        # 信号量已释放
        assert _delegate_semaphore()._value == DELEGATE_CONCURRENCY_MAX

    async def test_reset_delegate_sem_clears_state(self):
        """_reset_delegate_sem 重置信号量状态"""
        from config import DELEGATE_CONCURRENCY_MAX
        from tools.builtin.system_tool_config import _delegate_semaphore, _reset_delegate_sem

        # acquire 一个槽位
        await _delegate_semaphore().acquire()
        assert _delegate_semaphore()._value == DELEGATE_CONCURRENCY_MAX - 1

        # 重置
        _reset_delegate_sem()
        # 重新导入获取新实例
        from tools.builtin import system_tool_config as stc
        assert stc._delegate_semaphore()._value == DELEGATE_CONCURRENCY_MAX
