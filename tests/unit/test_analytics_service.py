"""tests/unit/test_analytics_service.py —— Analytics 双通道 + 批量缓冲测试"""
import os
import sys
import tempfile
import time
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
            time.sleep(0.05)


@pytest.fixture
async def analytics_service(tmp_path):
    """创建 AnalyticsService（含完整 DB schema）"""
    import aiosqlite
    db_path = os.path.join(str(tmp_path), "test.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    from services.analytics_service import AnalyticsService
    svc = AnalyticsService(conn)
    yield svc
    await svc.flush_all()
    await conn.close()
    _safe_unlink(db_path)


class TestTrack:
    """事件追踪"""

    async def test_track_non_critical_buffered(self, analytics_service):
        """非关键事件进缓冲（不立即写 DB）"""
        await analytics_service.track("task_submitted", {"agent_id": "hermes"})
        assert len(analytics_service._buffer) == 1

    async def test_critical_event_writes_immediately(self, analytics_service):
        """关键事件直写 DB"""
        await analytics_service.task_failed("t1", "hermes", "test error")
        # 直写不走缓冲
        assert len(analytics_service._buffer) == 0

    async def test_flush_all_clears_buffer(self, analytics_service):
        """flush_all 清空缓冲"""
        await analytics_service.task_submitted("hermes")
        assert len(analytics_service._buffer) == 1
        await analytics_service.flush_all()
        assert len(analytics_service._buffer) == 0


class TestBatchFlush:
    """批量刷新（20 条阈值）"""

    async def test_auto_flush_at_threshold(self, analytics_service):
        """缓冲达到 20 条时自动 flush"""
        for i in range(20):
            await analytics_service.task_submitted("hermes", prompt_len=i)
        # 20 条应触发自动 flush，缓冲清零
        assert len(analytics_service._buffer) == 0

    async def test_buffer_below_threshold_not_flushed(self, analytics_service):
        """不足 20 条时不自动 flush"""
        for i in range(15):
            await analytics_service.task_submitted("hermes")
        assert len(analytics_service._buffer) == 15


class TestQueryMethods:
    """查询接口"""

    async def test_get_accept_rate_empty(self, analytics_service):
        """无数据时接受率返回 0"""
        rate = await analytics_service.get_accept_rate()
        assert rate["rate"] == 0
        assert rate["accepted"] == 0

    async def test_get_agent_usage_empty(self, analytics_service):
        """无数据时排行返回空列表"""
        stats = await analytics_service.get_agent_usage_stats()
        assert stats == []

    async def test_accept_rate_calculates(self, analytics_service):
        """接受率计算正确"""
        await analytics_service.human_accepted("t1")
        await analytics_service.human_accepted("t2")
        await analytics_service.human_rejected("t3", "wrong")
        await analytics_service.flush_all()  # human_accepted 走缓冲，需刷新
        rate = await analytics_service.get_accept_rate()
        assert rate["accepted"] == 2
        assert rate["rejected"] == 1
        assert rate["rate"] == 2 / 3


class TestStartupMetrics:
    """Phase 97: 后端启动各阶段耗时上报"""

    async def test_startup_metrics_writes_event(self, analytics_service):
        """startup_metrics 写入 analytics_events 表，event_type='startup_metrics'"""
        import json

        phases = {"init_ms": 12, "db_init_ms": 340, "adapters_ms": 1200}
        await analytics_service.startup_metrics(phases=phases, total_ms=1552)
        await analytics_service.flush_all()

        cursor = await analytics_service.db.execute(
            "SELECT event_data FROM analytics_events WHERE event_type='startup_metrics'"
        )
        row = await cursor.fetchone()
        assert row is not None
        data = json.loads(row[0])
        assert data["phases"] == phases
        assert data["total_ms"] == 1552

    async def test_startup_metrics_with_extra(self, analytics_service):
        """extra 上下文（version/platform/python_version）正确写入 event_data"""
        import json

        phases = {"init_ms": 5, "db_init_ms": 100, "adapters_ms": 500}
        extra = {
            "version": "0.4.0",
            "platform": "Windows-11-10.0.22631",
            "python_version": "3.12.1",
            "is_first_run": False,
        }
        await analytics_service.startup_metrics(phases=phases, total_ms=605, extra=extra)
        await analytics_service.flush_all()

        cursor = await analytics_service.db.execute(
            "SELECT event_data FROM analytics_events WHERE event_type='startup_metrics'"
        )
        row = await cursor.fetchone()
        data = json.loads(row[0])
        assert data["version"] == "0.4.0"
        assert data["platform"] == "Windows-11-10.0.22631"
        assert data["python_version"] == "3.12.1"
        assert data["is_first_run"] is False
        assert data["phases"] == phases
        assert data["total_ms"] == 605

    async def test_startup_metrics_without_extra(self, analytics_service):
        """extra=None 时只有 phases + total_ms，无额外字段"""
        import json

        await analytics_service.startup_metrics(
            phases={"init_ms": 1}, total_ms=1, extra=None
        )
        await analytics_service.flush_all()

        cursor = await analytics_service.db.execute(
            "SELECT event_data FROM analytics_events WHERE event_type='startup_metrics'"
        )
        row = await cursor.fetchone()
        data = json.loads(row[0])
        assert "version" not in data
        assert "platform" not in data
        assert data["phases"] == {"init_ms": 1}
        assert data["total_ms"] == 1

    async def test_startup_metrics_uses_batch_buffer(self, analytics_service):
        """startup_metrics 走批量缓冲通道——< 20 条不立即落库"""
        for i in range(5):
            await analytics_service.startup_metrics(
                phases={"init_ms": i}, total_ms=i * 10
            )
        # 批量缓冲 < 20 条，不立即落库
        cursor = await analytics_service.db.execute(
            "SELECT COUNT(*) FROM analytics_events WHERE event_type='startup_metrics'"
        )
        count_before = (await cursor.fetchone())[0]
        assert count_before == 0

        await analytics_service.flush_all()
        cursor = await analytics_service.db.execute(
            "SELECT COUNT(*) FROM analytics_events WHERE event_type='startup_metrics'"
        )
        count_after = (await cursor.fetchone())[0]
        assert count_after == 5


class TestFrontendStartup:
    """Phase 97: 前端启动耗时上报"""

    async def test_frontend_startup_writes_event(self, analytics_service):
        """frontend_startup 写入 analytics_events 表"""
        import json

        await analytics_service.frontend_startup(frontend_ms=3200)
        await analytics_service.flush_all()

        cursor = await analytics_service.db.execute(
            "SELECT event_data FROM analytics_events WHERE event_type='frontend_startup'"
        )
        row = await cursor.fetchone()
        assert row is not None
        data = json.loads(row[0])
        assert data["frontend_ms"] == 3200

    async def test_frontend_startup_with_stages(self, analytics_service):
        """stages 各阶段进入时间戳正确写入 event_data"""
        import json

        stages = {"init": 0, "db-init": 800, "adapters": 1500, "ready": 2100}
        await analytics_service.frontend_startup(frontend_ms=2100, stages=stages)
        await analytics_service.flush_all()

        cursor = await analytics_service.db.execute(
            "SELECT event_data FROM analytics_events WHERE event_type='frontend_startup'"
        )
        row = await cursor.fetchone()
        data = json.loads(row[0])
        assert data["frontend_ms"] == 2100
        assert data["stages"] == stages

    async def test_frontend_startup_with_extra(self, analytics_service):
        """extra 上下文（app_version/electron_version/platform）正确写入"""
        import json

        extra = {
            "app_version": "0.4.0",
            "electron_version": "42.0.0",
            "platform": "win32",
        }
        await analytics_service.frontend_startup(frontend_ms=1500, extra=extra)
        await analytics_service.flush_all()

        cursor = await analytics_service.db.execute(
            "SELECT event_data FROM analytics_events WHERE event_type='frontend_startup'"
        )
        row = await cursor.fetchone()
        data = json.loads(row[0])
        assert data["app_version"] == "0.4.0"
        assert data["electron_version"] == "42.0.0"
        assert data["platform"] == "win32"
        assert data["frontend_ms"] == 1500

    async def test_frontend_startup_minimal(self, analytics_service):
        """只传 frontend_ms，stages/extra 均省略——数据结构正确"""
        import json

        await analytics_service.frontend_startup(frontend_ms=100)
        await analytics_service.flush_all()

        cursor = await analytics_service.db.execute(
            "SELECT event_data FROM analytics_events WHERE event_type='frontend_startup'"
        )
        row = await cursor.fetchone()
        data = json.loads(row[0])
        assert data["frontend_ms"] == 100
        assert "stages" not in data
        assert "app_version" not in data
