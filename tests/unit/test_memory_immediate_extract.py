"""tests/unit/test_memory_immediate_extract.py —— Task 6.6: 记忆即时提取（F-059）

覆盖:
- add_message(role="assistant") 发布 ASSISTANT_TURN_COMPLETE 事件
- add_message(role="user") 不发布该事件
- MemoryTree.trigger_conversation_index 创建 debounced task
- 连续调用取消旧 task 重建（去抖）
- debounced task 到期后调用 index_from_conversations

对标 Claude Code 每轮即时提取——事件驱动 + 15 分钟批量兜底。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


def _safe_unlink(path: str) -> None:
    if not os.path.exists(path):
        return
    for _ in range(5):
        try:
            os.unlink(path)
            return
        except PermissionError:
            import time
            time.sleep(0.05)


@pytest.fixture
async def memory_service(tmp_path):
    """创建 MemoryService 实例——使用真实 aiosqlite 连接"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test_immediate.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    from services.memory.memory_service import MemoryService
    svc = MemoryService(conn)
    yield svc
    await conn.close()
    _safe_unlink(db_path)


@pytest.fixture
async def event_bus_reset():
    """重置全局 event bus 单例——避免测试间污染"""
    import services.event_bus as eb_mod
    old = eb_mod._event_bus
    eb_mod._event_bus = None
    from services.event_bus import EventBus
    new_bus = EventBus()
    eb_mod._set_event_bus(new_bus)
    await new_bus.start()
    yield new_bus
    await new_bus.stop()
    eb_mod._event_bus = old


# ============================================================
# 事件发布测试
# ============================================================

class TestAssistantTurnEventPublish:
    """add_message 在 assistant 提交后发布 ASSISTANT_TURN_COMPLETE 事件"""

    async def test_assistant_message_publishes_event(self, memory_service, event_bus_reset):
        """assistant 消息提交后发布 ASSISTANT_TURN_COMPLETE 事件"""
        from services.event_bus import Events
        received: list[dict] = []

        async def _handler(event_type: str, data: dict):
            received.append({"event_type": event_type, "data": data})

        event_bus_reset.subscribe(Events.ASSISTANT_TURN_COMPLETE, _handler)

        await memory_service.add_message("sess-1", "assistant", "Hello world")
        # event bus consumer 是异步的——给一点时间处理
        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0]["event_type"] == Events.ASSISTANT_TURN_COMPLETE
        assert received[0]["data"]["session_id"] == "sess-1"

    async def test_user_message_does_not_publish_event(self, memory_service, event_bus_reset):
        """user 消息不发布 ASSISTANT_TURN_COMPLETE 事件"""
        from services.event_bus import Events
        received: list[dict] = []

        async def _handler(event_type: str, data: dict):
            received.append({"event_type": event_type, "data": data})

        event_bus_reset.subscribe(Events.ASSISTANT_TURN_COMPLETE, _handler)

        await memory_service.add_message("sess-2", "user", "Question")
        await asyncio.sleep(0.1)

        assert len(received) == 0, "user 消息不应触发 ASSISTANT_TURN_COMPLETE"

    async def test_system_message_does_not_publish_event(self, memory_service, event_bus_reset):
        """system 消息不发布 ASSISTANT_TURN_COMPLETE 事件"""
        from services.event_bus import Events
        received: list[dict] = []

        async def _handler(event_type: str, data: dict):
            received.append({"event_type": event_type, "data": data})

        event_bus_reset.subscribe(Events.ASSISTANT_TURN_COMPLETE, _handler)

        await memory_service.add_message("sess-3", "system", "system notice")
        await asyncio.sleep(0.1)

        assert len(received) == 0


# ============================================================
# Debounce 即时提取测试
# ============================================================

class TestTriggerConversationIndex:
    """MemoryTree.trigger_conversation_index debounce 行为"""

    async def test_trigger_creates_debounce_task(self, tmp_path):
        """trigger_conversation_index 创建一个 asyncio.Task（debounced）"""
        import aiosqlite
        from services.memory.memory_tree import MemoryTree
        db_path = os.path.join(str(tmp_path), "test_trigger.db")
        conn = await aiosqlite.connect(db_path)
        from repositories import SCHEMA_SQL
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        try:
            tree = MemoryTree(db=conn)
            assert tree._index_debounce_task is None

            tree.trigger_conversation_index()
            await asyncio.sleep(0.01)  # 让 create_task 生效

            assert tree._index_debounce_task is not None
            assert not tree._index_debounce_task.done()

            # 清理——取消未完成的 task
            tree._cancel_debounce_task()
        finally:
            await conn.close()
            _safe_unlink(db_path)

    async def test_rapid_triggers_cancel_previous(self, tmp_path):
        """连续调用取消旧 task 重建——去抖"""
        import aiosqlite
        from services.memory.memory_tree import MemoryTree
        db_path = os.path.join(str(tmp_path), "test_debounce.db")
        conn = await aiosqlite.connect(db_path)
        from repositories import SCHEMA_SQL
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        try:
            tree = MemoryTree(db=conn)

            tree.trigger_conversation_index()
            await asyncio.sleep(0.01)
            first_task = tree._index_debounce_task
            assert first_task is not None

            tree.trigger_conversation_index()
            await asyncio.sleep(0.01)
            second_task = tree._index_debounce_task
            assert second_task is not None
            assert second_task is not first_task, "第二次调用应创建新 task"
            assert first_task.cancelled() or first_task.done(), "旧 task 应被取消"

            tree._cancel_debounce_task()
        finally:
            await conn.close()
            _safe_unlink(db_path)

    async def test_debounced_task_calls_index_from_conversations(self, tmp_path):
        """debounce 到期后调用 index_from_conversations"""
        import aiosqlite
        from services.memory.memory_tree import MemoryTree
        db_path = os.path.join(str(tmp_path), "test_call.db")
        conn = await aiosqlite.connect(db_path)
        from repositories import SCHEMA_SQL
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        try:
            tree = MemoryTree(db=conn)

            # mock index_from_conversations 避免真实 LLM 调用
            call_count = {"n": 0}

            async def _mock_index():
                call_count["n"] += 1
                return 0

            tree.index_from_conversations = _mock_index

            # 用极短 debounce 避免测试慢
            import config
            original = config.MEMORY_INDEX_DEBOUNCE_SEC
            config.MEMORY_INDEX_DEBOUNCE_SEC = 0.05
            try:
                tree.trigger_conversation_index()
                # 等 debounce + 执行
                await asyncio.sleep(0.2)

                assert call_count["n"] == 1, "debounce 到期后应调用 index_from_conversations"
            finally:
                config.MEMORY_INDEX_DEBOUNCE_SEC = original
        finally:
            await conn.close()
            _safe_unlink(db_path)

    async def test_debounce_cancels_before_index_runs(self, tmp_path):
        """连续 trigger 时，只有最后一次会执行 index_from_conversations"""
        import aiosqlite
        from services.memory.memory_tree import MemoryTree
        db_path = os.path.join(str(tmp_path), "test_cancel.db")
        conn = await aiosqlite.connect(db_path)
        from repositories import SCHEMA_SQL
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        try:
            tree = MemoryTree(db=conn)

            call_count = {"n": 0}

            async def _mock_index():
                call_count["n"] += 1
                return 0

            tree.index_from_conversations = _mock_index

            import config
            original = config.MEMORY_INDEX_DEBOUNCE_SEC
            config.MEMORY_INDEX_DEBOUNCE_SEC = 0.1
            try:
                # 连续触发 3 次——应只执行 1 次（最后一次）
                tree.trigger_conversation_index()
                await asyncio.sleep(0.03)
                tree.trigger_conversation_index()
                await asyncio.sleep(0.03)
                tree.trigger_conversation_index()

                # 等 debounce 到期 + 执行
                await asyncio.sleep(0.3)

                assert call_count["n"] == 1, "连续 trigger 只应执行 1 次（去抖）"
            finally:
                config.MEMORY_INDEX_DEBOUNCE_SEC = original
        finally:
            await conn.close()
            _safe_unlink(db_path)


class TestEventSubscription:
    """MemoryTree 订阅 ASSISTANT_TURN_COMPLETE 事件"""

    async def test_start_event_subscription_registers_handler(self, tmp_path):
        """start_event_subscription 注册 handler 到 event bus"""
        import aiosqlite
        import services.event_bus as eb_mod
        from services.event_bus import EventBus, Events
        from services.memory.memory_tree import MemoryTree

        old = eb_mod._event_bus
        eb_mod._event_bus = None
        bus = EventBus()
        eb_mod._set_event_bus(bus)
        await bus.start()

        db_path = os.path.join(str(tmp_path), "test_sub.db")
        conn = await aiosqlite.connect(db_path)
        from repositories import SCHEMA_SQL
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        try:
            tree = MemoryTree(db=conn)
            tree.start_event_subscription()

            # 验证 handler 已注册
            assert Events.ASSISTANT_TURN_COMPLETE in bus._subscribers
            assert len(bus._subscribers[Events.ASSISTANT_TURN_COMPLETE]) == 1

            tree._cancel_debounce_task()
        finally:
            await conn.close()
            _safe_unlink(db_path)
            await bus.stop()
            eb_mod._event_bus = old

    async def test_event_triggers_debounced_index(self, tmp_path):
        """ASSISTANT_TURN_COMPLETE 事件触发 debounced index"""
        import aiosqlite
        import services.event_bus as eb_mod
        from services.event_bus import EventBus, Events, get_event_bus
        from services.memory.memory_tree import MemoryTree

        old = eb_mod._event_bus
        eb_mod._event_bus = None
        bus = EventBus()
        eb_mod._set_event_bus(bus)
        await bus.start()

        db_path = os.path.join(str(tmp_path), "test_evt_trigger.db")
        conn = await aiosqlite.connect(db_path)
        from repositories import SCHEMA_SQL
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        try:
            tree = MemoryTree(db=conn)

            call_count = {"n": 0}

            async def _mock_index():
                call_count["n"] += 1
                return 0

            tree.index_from_conversations = _mock_index

            import config
            original = config.MEMORY_INDEX_DEBOUNCE_SEC
            config.MEMORY_INDEX_DEBOUNCE_SEC = 0.05
            try:
                tree.start_event_subscription()

                # 发布事件
                await get_event_bus().publish(
                    Events.ASSISTANT_TURN_COMPLETE,
                    {"session_id": "sess-test"},
                )
                # 等 event bus consumer + debounce + 执行
                await asyncio.sleep(0.3)

                assert call_count["n"] == 1, "事件应触发 debounced index"
            finally:
                config.MEMORY_INDEX_DEBOUNCE_SEC = original
                tree._cancel_debounce_task()
        finally:
            await conn.close()
            _safe_unlink(db_path)
            await bus.stop()
            eb_mod._event_bus = old
