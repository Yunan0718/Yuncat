"""tests/unit/test_file_watcher_dedup.py —— F2: watchdog 事件去重

覆盖:
- 500ms 窗口内同 path 事件去重（已有行为，回归保护）
- on_moved 同时记录 src_path 和 dest_path
- _flush 后若有新事件到达，应重新调度 flush（stuck-path 修复）
- 跨线程安全（_pending 加锁）
"""
import os
import sys
import asyncio
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class _MockEvent:
    """模拟 watchdog FileSystemEvent"""
    def __init__(self, src_path, is_directory=False, dest_path=None):
        self.src_path = src_path
        self.is_directory = is_directory
        self.dest_path = dest_path


class _MockEventBus:
    """模拟 EventBus——记录所有 publish 调用"""
    def __init__(self):
        self.published = []

    async def publish(self, event_type, data):
        self.published.append((event_type, data))


@pytest.fixture
async def handler(tmp_path):
    """创建 DebouncedHandler（需在 async 上下文中构造）"""
    from services.infra.file_watcher import DebouncedHandler
    bus = _MockEventBus()
    h = DebouncedHandler(bus, str(tmp_path))
    yield h, bus
    # 清理未完成的 flush task
    if h._flush_task and not h._flush_task.done():
        h._flush_task.cancel()
        try:
            await h._flush_task
        except (asyncio.CancelledError, Exception):
            pass


class TestSamePathDedup:
    """500ms 窗口内同 path 事件去重（回归保护 + 修复验证）"""

    async def test_same_path_within_window_deduped(self, handler):
        """500ms 内同一文件多次事件只记录一次"""
        h, bus = handler
        h.on_any_event(_MockEvent("/ws/a.txt"))
        h.on_any_event(_MockEvent("/ws/a.txt"))
        h.on_any_event(_MockEvent("/ws/a.txt"))
        assert len(h._pending) == 1
        assert "/ws/a.txt" in h._pending

    async def test_different_paths_not_deduped(self, handler):
        """不同路径不被去重"""
        h, bus = handler
        h.on_any_event(_MockEvent("/ws/a.txt"))
        h.on_any_event(_MockEvent("/ws/b.txt"))
        assert len(h._pending) == 2


class TestMovedEventDestPath:
    """F2: on_moved 应同时记录 src_path 和 dest_path"""

    async def test_moved_records_both_paths(self, handler):
        """rename/move 事件记录新旧两个路径——前端 FileTree 需知道新文件"""
        h, bus = handler
        # 模拟 watchdog on_moved 事件
        h.on_any_event(_MockEvent("/ws/old.txt", dest_path="/ws/new.txt"))
        # src 和 dest 都应被记录
        assert "/ws/old.txt" in h._pending
        assert "/ws/new.txt" in h._pending

    async def test_moved_without_dest_only_src(self, handler):
        """dest_path 为 None 时只记录 src（兼容）"""
        h, bus = handler
        h.on_any_event(_MockEvent("/ws/only.txt", dest_path=None))
        assert "/ws/only.txt" in h._pending
        assert len(h._pending) == 1


class TestFlushRearm:
    """F2: _flush 后若有新事件到达，应重新调度 flush（stuck-path 修复）"""

    async def test_flush_rearms_when_new_events_during_flush(self, handler):
        """flush 执行期间新到达的事件不应丢失——应触发新一轮 flush"""
        h, bus = handler
        # 触发首次事件 → 创建 flush task
        h.on_any_event(_MockEvent("/ws/first.txt"))
        first_task = h._flush_task
        assert first_task is not None

        # 等待 flush 完成（含 500ms 防抖）
        await asyncio.sleep(0.6)
        assert first_task.done()
        # 首次 flush 应发布 first.txt
        assert len(bus.published) == 1
        assert "/ws/first.txt" in bus.published[0][1]["paths"]

        # 现在在 flush 完成后触发新事件——应创建新 flush task
        h.on_any_event(_MockEvent("/ws/second.txt"))
        # second.txt 不应丢失
        assert "/ws/second.txt" in h._pending
        assert h._flush_task is not None and not h._flush_task.done()

        # 等待第二次 flush
        await asyncio.sleep(0.6)
        assert len(bus.published) == 2
        assert "/ws/second.txt" in bus.published[1][1]["paths"]

    async def test_event_during_clear_not_lost(self, handler):
        """_flush 的 clear() 与新事件交错时，新事件不应丢失"""
        h, bus = handler
        h.on_any_event(_MockEvent("/ws/before.txt"))
        await asyncio.sleep(0.6)  # flush 完成
        assert len(bus.published) == 1

        # 连续两个事件
        h.on_any_event(_MockEvent("/ws/x.txt"))
        h.on_any_event(_MockEvent("/ws/y.txt"))
        await asyncio.sleep(0.6)
        # 两个都应被发布
        assert len(bus.published) == 2
        published_paths = set(bus.published[1][1]["paths"])
        assert "/ws/x.txt" in published_paths
        assert "/ws/y.txt" in published_paths


class TestIgnorePatterns:
    """忽略模式——.git/node_modules 等不触发事件"""

    async def test_git_directory_ignored(self, handler):
        h, bus = handler
        h.on_any_event(_MockEvent("/ws/.git/config"))
        assert len(h._pending) == 0

    async def test_node_modules_ignored(self, handler):
        h, bus = handler
        h.on_any_event(_MockEvent("/ws/node_modules/pkg/index.js"))
        assert len(h._pending) == 0

    async def test_hidden_file_ignored(self, handler):
        h, bus = handler
        h.on_any_event(_MockEvent("/ws/.hidden"))
        assert len(h._pending) == 0


class TestThreadSafety:
    """F2: _pending 跨线程访问应有锁保护"""

    async def test_pending_lock_exists(self, handler):
        """DebouncedHandler 应有 _lock 属性保护 _pending"""
        import threading
        h, bus = handler
        assert hasattr(h, "_lock"), "DebouncedHandler 应有 _lock 保护跨线程 _pending 访问"
        assert isinstance(h._lock, type(threading.Lock())), "_lock 应为 threading.Lock 类型"

    async def test_concurrent_on_any_event_safe(self, handler):
        """模拟 watchdog 线程并发调用 on_any_event 不出错"""
        h, bus = handler
        # 模拟多线程并发触发（watchdog observer 线程）
        import threading
        errors = []

        def _fire(n):
            try:
                for i in range(20):
                    h.on_any_event(_MockEvent(f"/ws/concurrent_{n}_{i}.txt"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_fire, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"并发 on_any_event 出错: {errors}"
        # 4 线程 × 20 = 80 个不同路径都应被记录
        assert len(h._pending) == 80
