"""tests/unit/test_event_bus.py —— EventBus 发布/订阅测试"""
import os
import sys
import asyncio
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
async def event_bus():
    """创建并启动 EventBus"""
    from services.event_bus import EventBus
    eb = EventBus()
    await eb.start()
    yield eb
    await eb.stop()


class TestPublishSubscribe:
    """发布/订阅基础"""

    async def test_subscribe_and_publish(self, event_bus):
        """订阅后能收到事件"""
        received = []

        async def handler(event_type, data):
            received.append((event_type, data))

        event_bus.subscribe("test.event", handler)
        await event_bus.publish("test.event", {"key": "value"})

        # 等待消费
        await asyncio.sleep(0.1)
        assert len(received) == 1
        assert received[0][0] == "test.event"
        assert received[0][1]["key"] == "value"

    async def test_multiple_subscribers(self, event_bus):
        """多个订阅者都能收到事件"""
        results = []

        async def h1(event_type, data):
            results.append("h1")

        async def h2(event_type, data):
            results.append("h2")

        event_bus.subscribe("multi", h1)
        event_bus.subscribe("multi", h2)
        await event_bus.publish("multi", {})
        await asyncio.sleep(0.1)

        assert "h1" in results
        assert "h2" in results

    async def test_unsubscribe(self, event_bus):
        """取消订阅后不再收到事件"""
        received = []

        async def handler(event_type, data):
            received.append(1)

        event_bus.subscribe("temp", handler)
        event_bus.unsubscribe("temp", handler)
        await event_bus.publish("temp", {})
        await asyncio.sleep(0.1)

        assert len(received) == 0

    async def test_no_subscriber_no_error(self, event_bus):
        """无订阅者时发布不报错"""
        await event_bus.publish("no.subscribers", {})
        await asyncio.sleep(0.05)
        # 不抛异常即为通过

    async def test_handler_exception_isolated(self, event_bus):
        """一个 handler 异常不影响其他 handler"""
        results = []

        async def good_handler(event_type, data):
            results.append("good")

        async def bad_handler(event_type, data):
            raise RuntimeError("handler crash")

        event_bus.subscribe("mixed", good_handler)
        event_bus.subscribe("mixed", bad_handler)
        await event_bus.publish("mixed", {})
        await asyncio.sleep(0.1)

        assert "good" in results


class TestEventTypes:
    """预定义事件类型"""

    def test_events_enum(self):
        from services.event_bus import Events
        assert Events.TASK_COMPLETED == "task_completed"
        assert Events.TOOL_EXECUTED == "tool_executed"
        assert Events.DREAM_COMPLETED == "dream_completed"
        # Phase 43 E.1: 融合事件类型
        assert Events.BACKEND_HEALTH_CHECK == "backend_health_check"
        assert Events.CROSS_BACKEND_DELEGATION == "cross_backend_delegation"
        assert Events.MEMORY_SYNC_COMPLETED == "memory_sync_completed"


class TestGlobalSingleton:
    """全局单例"""

    def test_get_event_bus_returns_singleton(self):
        from services.event_bus import get_event_bus
        eb1 = get_event_bus()
        eb2 = get_event_bus()
        assert eb1 is eb2
