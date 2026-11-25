"""tests/unit/test_error_events.py —— Batch 8.6: 本地错误事件持久化测试

覆盖：
1. _persist_error_event 持久化路径——4xx 不带 stack_trace，5xx 带 stack_trace
2. 启动早期 db 不可用时不崩溃
3. routes/errors.py 查询/分页/过滤/清空

对标 test_error_response.py 的 TestClient 模式。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ============================================================
# _persist_error_event 单元测试
# ============================================================

class _FakeConn:
    """最小化 aiosqlite 连接 mock——记录 execute 调用。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.committed = 0

    async def execute(self, sql, params=()):
        self.calls.append((sql, params))

        class _Cur:
            rowcount = 0

            async def fetchone(self):
                return (0,)

            async def fetchall(self):
                return []

            async def close(self):
                pass

        return _Cur()

    async def commit(self):
        self.committed += 1


class _FakeApp:
    def __init__(self, db):
        self.state = type("S", (), {"db": db})()


class _FakeRequest:
    def __init__(self, db, path="/test", method="GET", headers=None, state_extras=None):
        self.app = _FakeApp(db) if db is not None else _FakeApp(None)
        self.url = type("U", (), {"path": path})()
        self.method = method
        self.headers = headers or {}
        self.state = type("State", (), {})()
        for k, v in (state_extras or {}).items():
            setattr(self.state, k, v)


class TestPersistErrorEvent:
    """Batch 8.6: _persist_error_event 持久化路径测试。"""

    @pytest.mark.asyncio
    async def test_4xx_persists_without_stack(self):
        from schemas.error_response import _persist_error_event

        conn = _FakeConn()
        req = _FakeRequest(conn, path="/api/v1/foo", method="GET")
        await _persist_error_event(req, 404, "NOT_FOUND", "not found", stack_trace=None)
        assert len(conn.calls) == 1
        sql, params = conn.calls[0]
        assert "INSERT INTO error_events" in sql
        assert params[0] == "NOT_FOUND"
        assert params[1] == 404
        assert params[2] == "not found"
        assert params[3] == "/api/v1/foo"
        assert params[4] == "GET"
        # 第 9 个参数是 stack_trace
        assert params[8] is None
        assert conn.committed == 1

    @pytest.mark.asyncio
    async def test_5xx_persists_with_stack(self):
        from schemas.error_response import _persist_error_event

        conn = _FakeConn()
        req = _FakeRequest(conn, path="/api/v1/bar", method="POST")
        stack = "Traceback (most recent call last):\n  File ..."
        await _persist_error_event(req, 500, "INTERNAL_ERROR", "boom", stack_trace=stack)
        assert len(conn.calls) == 1
        _, params = conn.calls[0]
        assert params[1] == 500
        assert params[8] == stack

    @pytest.mark.asyncio
    async def test_db_none_silently_skips(self):
        """启动早期 db 不可用时不崩溃。"""
        from schemas.error_response import _persist_error_event

        req = _FakeRequest(None)
        # 不应抛异常
        await _persist_error_event(req, 500, "INTERNAL_ERROR", "x", stack_trace="tb")
        # 无 db 时不会执行任何 SQL

    @pytest.mark.asyncio
    async def test_db_execute_failure_swallowed(self):
        """db.execute 抛异常时不传播。"""
        from schemas.error_response import _persist_error_event

        class _BadConn:
            async def execute(self, *a, **kw):
                raise RuntimeError("db locked")

            async def commit(self):
                pass

        req = _FakeRequest(_BadConn())
        # 不应抛异常
        await _persist_error_event(req, 500, "INTERNAL_ERROR", "x", stack_trace="tb")

    @pytest.mark.asyncio
    async def test_extracts_context_from_state_and_headers(self):
        """从 request.state 和 headers 提取 trace_id/task_id/agent_id。"""
        from schemas.error_response import _persist_error_event

        conn = _FakeConn()
        req = _FakeRequest(
            conn,
            headers={"x-trace-id": "trace-abc"},
            state_extras={"task_id": "task-xyz", "agent_id": "hermes"},
        )
        await _persist_error_event(req, 500, "INTERNAL_ERROR", "err")
        _, params = conn.calls[0]
        # trace_id, task_id, agent_id 是参数 5,6,7
        assert params[5] == "trace-abc"
        assert params[6] == "task-xyz"
        assert params[7] == "hermes"

    @pytest.mark.asyncio
    async def test_message_truncated_to_2000(self):
        """超长 message 截断到 2000 字符防爆 DB。"""
        from schemas.error_response import _persist_error_event

        conn = _FakeConn()
        req = _FakeRequest(conn)
        long_msg = "x" * 5000
        await _persist_error_event(req, 500, "INTERNAL_ERROR", long_msg)
        _, params = conn.calls[0]
        assert len(params[2]) == 2000


# ============================================================
# routes/errors.py 端点测试
# ============================================================

class _MockCursor:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return self._rows

    async def close(self):
        pass


class _RouteFakeConn:
    """routes 测试用 mock conn——支持 SQL 模式匹配返回不同结果。"""

    def __init__(self, count=0, rows=None, deleted=0):
        self._count = count
        self._rows = rows or []
        self._deleted = deleted
        self.last_sql = ""

    async def execute(self, sql, params=()):
        self.last_sql = sql
        s = sql.strip().upper()
        if s.startswith("SELECT COUNT"):
            return _MockCursor(rows=[(self._count,)])
        if s.startswith("DELETE"):
            return _MockCursor(rowcount=self._deleted)
        # SELECT 列表
        return _MockCursor(rows=self._rows)

    async def commit(self):
        pass


class TestErrorsRoutes:
    """Batch 8.6: routes/errors.py GET/DELETE 端点。"""

    @pytest.fixture
    def app_with_errors(self):
        from fastapi import FastAPI
        from routes.errors import router

        app = FastAPI()
        app.include_router(router)
        return app

    @pytest.fixture
    def client(self, app_with_errors):
        from fastapi.testclient import TestClient
        with TestClient(app_with_errors) as c:
            yield c

    def _set_db(self, client, conn):
        client.app.state.db = conn

    def test_list_empty(self, client):
        """空数据返回 [] 不报错。"""
        self._set_db(client, _RouteFakeConn(count=0, rows=[]))
        resp = client.get("/api/v1/errors")
        assert resp.status_code == 200
        data = resp.json()
        assert data["errors"] == []
        assert data["count"] == 0
        assert data["total"] == 0

    def test_list_with_data(self, client):
        """有数据时正确序列化。"""
        rows = [(
            1, "NOT_FOUND", 404, "missing", "/api/v1/foo", "GET",
            "trace-1", "task-1", "hermes", "", 1700000000,
        )]
        self._set_db(client, _RouteFakeConn(count=1, rows=rows))
        resp = client.get("/api/v1/errors")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        e = data["errors"][0]
        assert e["error_code"] == "NOT_FOUND"
        assert e["status_code"] == 404
        assert e["path"] == "/api/v1/foo"
        assert e["method"] == "GET"
        assert e["trace_id"] == "trace-1"
        assert e["task_id"] == "task-1"
        assert e["agent_id"] == "hermes"
        assert e["created_at"] == 1700000000

    def test_list_level_4xx_filter(self, client):
        """level=4xx 过滤。"""
        conn = _RouteFakeConn(count=0, rows=[])
        self._set_db(client, conn)
        client.get("/api/v1/errors?level=4xx")
        assert "status_code >= 400 AND status_code < 500" in conn.last_sql

    def test_list_level_5xx_filter(self, client):
        """level=5xx 过滤。"""
        conn = _RouteFakeConn(count=0, rows=[])
        self._set_db(client, conn)
        client.get("/api/v1/errors?level=5xx")
        assert "status_code >= 500" in conn.last_sql

    def test_list_since_ts_filter(self, client):
        """since_ts 过滤。"""
        conn = _RouteFakeConn(count=0, rows=[])
        self._set_db(client, conn)
        client.get("/api/v1/errors?since_ts=1700000000")
        assert "created_at >= ?" in conn.last_sql

    def test_list_pagination(self, client):
        """分页参数传递。"""
        conn = _RouteFakeConn(count=100, rows=[])
        self._set_db(client, conn)
        client.get("/api/v1/errors?limit=10&offset=20")
        assert "LIMIT ? OFFSET ?" in conn.last_sql

    def test_list_limit_capped(self, client):
        """limit 超过上限被截断到 500。"""
        conn = _RouteFakeConn(count=0, rows=[])
        self._set_db(client, conn)
        resp = client.get("/api/v1/errors?limit=10000")
        data = resp.json()
        assert data["query"]["limit"] == 500

    def test_list_db_none_returns_error(self, client):
        """db 未初始化时返回 db_not_initialized。"""
        # 不设置 db
        if hasattr(client.app.state, "db"):
            del client.app.state.db
        resp = client.get("/api/v1/errors")
        assert resp.status_code == 200
        data = resp.json()
        assert data["error"] == "db_not_initialized"
        assert data["errors"] == []

    def test_clear_without_confirm_returns_400(self, client):
        """无 confirm=true 返回 400。"""
        self._set_db(client, _RouteFakeConn())
        resp = client.delete("/api/v1/errors")
        assert resp.status_code == 400

    def test_clear_with_confirm(self, client):
        """confirm=true 执行清空。"""
        conn = _RouteFakeConn(deleted=5)
        self._set_db(client, conn)
        resp = client.delete("/api/v1/errors?confirm=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == 5
        assert conn.last_sql.strip().upper().startswith("DELETE")

    def test_clear_with_older_than_ts(self, client):
        """older_than_ts 走条件删除。"""
        conn = _RouteFakeConn(deleted=3)
        self._set_db(client, conn)
        resp = client.delete("/api/v1/errors?confirm=true&older_than_ts=1700000000")
        assert resp.status_code == 200
        assert "WHERE created_at < ?" in conn.last_sql
