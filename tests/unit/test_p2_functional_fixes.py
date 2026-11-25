"""P2 边缘功能 bug 修复回归测试

覆盖 5 个 P2 修复（Bug 1 误报不存在）：
2. cron DST（文档化已知限制）
3. workspace index mtime（改为 _time.time 扫描完成时刻）
4. WS send 无超时（5 个 send 路径加 asyncio.wait_for）
5. workflow pause 非立即（文档化 batch-boundary 设计）
6. file_watcher stop 不取消 flush（修复 stop + lifespan 调用）

混合测试策略：
- 源码检查：验证修复代码存在
- 行为测试：对可独立测试的函数用 mock 验证行为正确性
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src-python"


def _src(rel: str) -> str:
    """读取源文件全文"""
    return (_SRC / rel).read_text(encoding="utf-8")


# ════════════════════════════════════════════════════════════
# Bug 2: cron DST（文档化）
# ════════════════════════════════════════════════════════════

class TestP22CronDST:
    def test_next_run_at_docstring_documents_dst_limitation(self):
        """P2-2: next_run_at docstring 应说明 DST 限制"""
        source = _src("services/cron_parser.py")
        assert "DST" in source, "docstring 应说明 DST 限制"
        assert "spring forward" in source, "应说明 spring forward 行为"
        assert "fall back" in source, "应说明 fall back 行为"


# ════════════════════════════════════════════════════════════
# Bug 3: workspace index mtime
# ════════════════════════════════════════════════════════════

class TestP23WorkspaceIndexMtime:
    def test_index_from_workspace_uses_time_not_dir_mtime(self):
        """P2-3: 应使用 _time.time() 而非 _os.path.getmtime(shared) 作为 cutoff"""
        source = _src("services/memory/memory_tree.py")
        # 应改为 _time.time()
        assert "_last_workspace_index_time = _time.time()" in source, \
            "应用 _time.time()（扫描完成时刻）替代目录 mtime"
        # 不应再有赋值语句用 shared 目录 mtime（注释中提及不算）
        # 检查实际赋值：_last_workspace_index_time = _os.path.getmtime(shared)
        assert "_last_workspace_index_time = _os.path.getmtime(shared)" not in source, \
            "不应再用 shared 目录 mtime 赋值（不反映文件内容变更）"


# ════════════════════════════════════════════════════════════
# Bug 4: WS send 无超时
# ════════════════════════════════════════════════════════════

class TestP24WSSendTimeout:
    def test_config_has_ws_send_timeout(self):
        """P2-4: config.py 应有 WS_SEND_TIMEOUT_SEC 常量"""
        source = _src("config.py")
        assert "WS_SEND_TIMEOUT_SEC" in source, "config 应有 WS_SEND_TIMEOUT_SEC"

    def test_safe_send_has_wait_for(self):
        """P2-4: safe_send 应使用 asyncio.wait_for"""
        source = _src("services/agent/ws_manager.py")
        assert "asyncio.wait_for" in source, "safe_send 应使用 asyncio.wait_for"
        assert "WS_SEND_TIMEOUT_SEC" in source, "应引用 WS_SEND_TIMEOUT_SEC"

    def test_all_send_paths_have_timeout(self):
        """P2-4: 所有 send 路径都应有 wait_for"""
        source = _src("services/agent/ws_manager.py")
        # 统计 send_json 调用数
        send_count = source.count("send_json")
        # 统计 wait_for 包裹数（至少应覆盖主要 send 路径）
        wait_for_count = source.count("asyncio.wait_for")
        # 至少 5 个 send 路径有 wait_for（safe_send/heartbeat/push_hitl/broadcast/push_agent_status）
        assert wait_for_count >= 5, \
            f"应有至少 5 个 wait_for（实际 {wait_for_count}），覆盖所有 send 路径"

    @pytest.mark.asyncio
    async def test_safe_send_timeout_cleans_dead_connection(self):
        """P2-4 行为测试: send 超时时应清理死连接"""
        from services.agent.ws_manager import WsManager

        mgr = WsManager()

        # mock 慢客户端：send_json 永远阻塞
        slow_ws = MagicMock()
        slow_ws.send_json = AsyncMock(side_effect=asyncio.TimeoutError())
        mgr._connections["task-1"] = [slow_ws]
        mgr._last_active[id(slow_ws)] = 0

        await mgr.safe_send("task-1", {"type": "test"})

        # 慢客户端应被清理
        assert "task-1" not in mgr._connections or len(mgr._connections.get("task-1", [])) == 0
        assert id(slow_ws) not in mgr._last_active


# ════════════════════════════════════════════════════════════
# Bug 5: workflow pause 非立即（文档化）
# ════════════════════════════════════════════════════════════

class TestP25WorkflowPauseNotImmediate:
    def test_pause_docstring_documents_batch_boundary(self):
        """P2-5: pause docstring 应说明 batch-boundary 限制"""
        source = _src("workflow/engine.py")
        assert "batch-boundary" in source or "batch 边界" in source, \
            "pause docstring 应说明 batch-boundary 限制"
        assert "Airflow" in source or "Temporal" in source or "Prefect" in source, \
            "应对标行业主流 DAG 引擎做法"


# ════════════════════════════════════════════════════════════
# Bug 6: file_watcher stop 不取消 flush
# ════════════════════════════════════════════════════════════

class TestP26FileWatcherStopFlush:
    def test_stop_cancels_flush_task(self):
        """P2-6: stop() 应取消 _flush_task"""
        source = _src("services/infra/file_watcher.py")
        assert "_flush_task" in source, "stop 应处理 _flush_task"
        assert ".cancel()" in source, "stop 应取消 _flush_task"

    def test_stop_clears_pending(self):
        """P2-6: stop() 应清空 _pending"""
        source = _src("services/infra/file_watcher.py")
        assert "_pending.clear()" in source, "stop 应清空 _pending"

    def test_lifespan_calls_file_watcher_stop(self):
        """P2-6: lifespan shutdown 应调用 file_watcher.stop()"""
        source = _src("lifespan.py")
        assert "file_watcher.stop()" in source, \
            "lifespan shutdown 应调用 file_watcher.stop()"
        # 应在 event_bus.stop() 之前
        fw_pos = source.index("file_watcher.stop()")
        eb_pos = source.index("event_bus.stop()")
        assert fw_pos < eb_pos, "file_watcher.stop() 应在 event_bus.stop() 之前"

    @pytest.mark.asyncio
    async def test_stop_cancels_inflight_flush(self):
        """P2-6 行为测试: stop() 应取消正在进行的 flush"""
        import threading
        from services.infra.file_watcher import FileWatcherService

        svc = FileWatcherService()

        # mock observer（不需要 join）
        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = False
        svc._observer = mock_observer

        # 创建一个真实的 task（会一直 sleep，模拟 in-flight flush）
        async def long_running():
            await asyncio.sleep(100)

        real_task = asyncio.create_task(long_running())

        # mock handler with real flush task
        mock_handler = MagicMock()
        mock_handler._flush_task = real_task
        mock_handler._pending = {"path1": 1.0}
        mock_handler._flushing = True
        mock_handler._lock = threading.Lock()
        svc._handler = mock_handler

        await svc.stop()

        # 应取消 flush task（cancelled 或 done 都算已处理）
        assert real_task.cancelled() or real_task.done(), "应取消 _flush_task"
        # 应清空 pending
        assert len(mock_handler._pending) == 0
        # 应重置标志
        assert mock_handler._flushing is False
        # 应清空引用
        assert svc._observer is None
        assert svc._handler is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
