"""tests/unit/test_analytics_startup_route.py —— Phase 97 路由契约测试

POST /api/v1/analytics/startup 的 6 个分支：
1. 200 —— 正常上报 frontend_ms
2. 200 —— 带 stages + extra 字段
3. 400 —— 缺 frontend_ms
4. 400 —— frontend_ms 非数字
5. 400 —— frontend_ms 负数
6. 400 —— stages 非对象

GET /api/v1/analytics/startup-stats 的 2 个分支：
7. 200 —— 返回后端 + 前端启动统计
8. 200 —— 无数据时返回空列表 + 0 均值

路由用 app.state.analytics_service，测试经 app.dependency_overrides 或
直接在 app.state 注入 mock。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.system import router as system_router


# ═══════════════════════════════════════════════════════════════
# Mock AnalyticsService
# ═══════════════════════════════════════════════════════════════

class _MockAnalyticsService:
    """模拟 AnalyticsService——记录 frontend_startup 调用参数。"""

    def __init__(self):
        self.frontend_calls: list[dict] = []
        self._db = _MockDB()

    async def frontend_startup(self, frontend_ms, stages=None, extra=None):
        self.frontend_calls.append({
            "frontend_ms": frontend_ms,
            "stages": stages,
            "extra": extra,
        })

    @property
    def db(self):
        return self._db


class _MockDB:
    """模拟 aiosqlite 连接——startup-stats 端点查询用。"""

    def __init__(self):
        self._rows: list[tuple] = []

    def set_rows(self, rows: list[tuple]):
        self._rows = rows

    async def execute(self, sql: str, *args, **kwargs):
        return _MockCursor(self._rows)


class _MockCursor:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def mock_analytics():
    return _MockAnalyticsService()


@pytest.fixture
def app(mock_analytics):
    app = FastAPI()
    app.include_router(system_router)
    app.state.analytics_service = mock_analytics
    app.state.db = mock_analytics.db
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


# ═══════════════════════════════════════════════════════════════
# POST /api/v1/analytics/startup 契约测试
# ═══════════════════════════════════════════════════════════════

class TestReportFrontendStartup:
    """POST /api/v1/analytics/startup"""

    def test_200_minimal(self, client, mock_analytics):
        """正常上报 frontend_ms——只传必填字段"""
        resp = client.post("/api/v1/analytics/startup", json={"frontend_ms": 3200})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert len(mock_analytics.frontend_calls) == 1
        call = mock_analytics.frontend_calls[0]
        assert call["frontend_ms"] == 3200
        assert call["stages"] is None
        assert call["extra"] is None

    def test_200_with_stages_and_extra(self, client, mock_analytics):
        """带 stages + extra 字段——完整上报"""
        resp = client.post("/api/v1/analytics/startup", json={
            "frontend_ms": 2100,
            "stages": {"init": 0, "db-init": 800, "adapters": 1500, "ready": 2100},
            "app_version": "0.4.0",
            "electron_version": "42.0.0",
            "platform": "win32",
        })
        assert resp.status_code == 200
        call = mock_analytics.frontend_calls[0]
        assert call["frontend_ms"] == 2100
        assert call["stages"] == {"init": 0, "db-init": 800, "adapters": 1500, "ready": 2100}
        assert call["extra"] == {
            "app_version": "0.4.0",
            "electron_version": "42.0.0",
            "platform": "win32",
        }

    def test_400_missing_frontend_ms(self, client):
        """缺 frontend_ms → 400"""
        resp = client.post("/api/v1/analytics/startup", json={"stages": {}})
        assert resp.status_code == 400
        assert "frontend_ms" in resp.json()["detail"]

    def test_400_frontend_ms_not_number(self, client):
        """frontend_ms 非数字 → 400"""
        resp = client.post("/api/v1/analytics/startup", json={"frontend_ms": "abc"})
        assert resp.status_code == 400
        assert "frontend_ms" in resp.json()["detail"]

    def test_400_frontend_ms_negative(self, client):
        """frontend_ms 负数 → 400"""
        resp = client.post("/api/v1/analytics/startup", json={"frontend_ms": -1})
        assert resp.status_code == 400
        assert "frontend_ms" in resp.json()["detail"]

    def test_400_stages_not_object(self, client):
        """stages 非对象 → 400"""
        resp = client.post("/api/v1/analytics/startup", json={
            "frontend_ms": 100,
            "stages": "invalid",
        })
        assert resp.status_code == 400
        assert "stages" in resp.json()["detail"]

    def test_200_float_frontend_ms_converted_to_int(self, client, mock_analytics):
        """frontend_ms 为浮点数——合法，转为 int 存储"""
        resp = client.post("/api/v1/analytics/startup", json={"frontend_ms": 2100.7})
        assert resp.status_code == 200
        call = mock_analytics.frontend_calls[0]
        assert call["frontend_ms"] == 2100  # int 转换


# ═══════════════════════════════════════════════════════════════
# GET /api/v1/analytics/startup-stats 契约测试
# ═══════════════════════════════════════════════════════════════

class TestStartupStats:
    """GET /api/v1/analytics/startup-stats"""

    def test_200_with_data(self, client, mock_analytics):
        """有数据时返回后端 + 前端启动统计 + 均值"""
        backend_data = json.dumps({"total_ms": 1500, "phases": {"init_ms": 10}})
        frontend_data = json.dumps({"frontend_ms": 3000, "stages": {"init": 0}})

        # mock DB 返回——第一次 execute 返回 backend_rows，第二次返回 frontend_rows
        call_count = [0]

        original_execute = mock_analytics.db.execute

        async def _execute(sql, *args, **kwargs):
            nonlocal call_count
            call_count[0] += 1
            if "startup_metrics" in sql:
                return _MockCursor([(backend_data, 1700000000)])
            elif "frontend_startup" in sql:
                return _MockCursor([(frontend_data, 1700000010)])
            return _MockCursor([])

        mock_analytics.db.execute = _execute

        resp = client.get("/api/v1/analytics/startup-stats?days=7")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["backend"]) == 1
        assert body["backend"][0]["total_ms"] == 1500
        assert body["backend"][0]["created_at"] == 1700000000
        assert len(body["frontend"]) == 1
        assert body["frontend"][0]["frontend_ms"] == 3000
        assert body["backend_avg_ms"] == 1500.0
        assert body["frontend_avg_ms"] == 3000.0

    def test_200_empty_data(self, client, mock_analytics):
        """无数据时返回空列表 + 0 均值"""

        async def _execute(sql, *args, **kwargs):
            return _MockCursor([])

        mock_analytics.db.execute = _execute

        resp = client.get("/api/v1/analytics/startup-stats?days=7")
        assert resp.status_code == 200
        body = resp.json()
        assert body["backend"] == []
        assert body["frontend"] == []
        assert body["backend_avg_ms"] == 0
        assert body["frontend_avg_ms"] == 0
