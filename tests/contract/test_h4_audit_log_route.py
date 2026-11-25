"""tests/contract/test_h4_audit_log_route.py —— H4: 审计日志查看路由合约测试

验证 GET /api/v1/audit-log 端点:
- 默认参数返回最近 100 条
- limit 参数限制返回条数（上限 1000）
- tool_name 过滤
- since_ts 过滤
- DB 未初始化时返回 error=db_not_initialized
- 返回结构包含 logs/count/query/server_ts

对标 test_long_task_api.py 的 TestClient + 真实 aiosqlite 模式。
"""
import os
import sys
import time as _t
import pytest
import aiosqlite

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.audit import router as audit_router
from repositories.schema import SCHEMA_SQL


@pytest.fixture
def app_with_db(tmp_path):
    """构造带真实 aiosqlite DB 的 FastAPI app——使用同步 TestClient 避免异步上下文管理器问题"""
    db_path = str(tmp_path / "test.db")

    app = FastAPI()
    app.include_router(audit_router)

    # 用同步 aiosqlite 连接（TestClient 同步调用路由，但路由内 await conn.execute 仍工作）
    # 注意: aiosqlite 连接需在同一线程使用——TestClient 用线程池执行，每次请求可能换线程
    # 因此改用持久连接 + 同一事件循环
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    conn = loop.run_until_complete(aiosqlite.connect(db_path))
    loop.run_until_complete(conn.executescript(SCHEMA_SQL))
    loop.run_until_complete(conn.commit())
    app.state.db = conn
    app.state._test_loop = loop  # 保留 loop 引用避免 GC

    yield app

    loop.run_until_complete(conn.close())
    loop.close()
    asyncio.set_event_loop(None)


@pytest.fixture
def app_no_db():
    """DB 未初始化的 app——模拟启动失败场景"""
    app = FastAPI()
    app.include_router(audit_router)
    # 故意不设置 app.state.db
    return app


@pytest.fixture
def client(app_with_db):
    return TestClient(app_with_db)


@pytest.fixture
def client_no_db(app_no_db):
    return TestClient(app_no_db)


class TestH4AuditLogRoute:
    """H4: /api/v1/audit-log 路由合约测试"""

    def test_empty_audit_log_returns_empty_list(self, client):
        """空 audit_log 表应返回空列表 + count=0"""
        r = client.get("/api/v1/audit-log")
        assert r.status_code == 200
        data = r.json()
        assert data["logs"] == []
        assert data["count"] == 0
        assert "server_ts" in data
        assert data["query"]["limit"] == 100
        assert data["query"]["tool_name"] == ""
        assert data["query"]["since_ts"] == 0

    def test_default_limit_is_100(self, app_with_db, client):
        """默认 limit=100"""
        import asyncio
        loop = app_with_db.state._test_loop
        conn = app_with_db.state.db
        now = int(_t.time())
        for i in range(150):
            loop.run_until_complete(
                conn.execute(
                    "INSERT INTO audit_log(ts, agent_id, tool_name, args_summary, success, phase) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (now - i, "hermes", "run_command", f"{{'cmd': 'echo {i}'}}", 1, "execute"),
                )
            )
        loop.run_until_complete(conn.commit())

        r = client.get("/api/v1/audit-log")
        data = r.json()
        assert data["count"] == 100
        # 最新优先——第一条 ts 应最接近 now
        assert data["logs"][0]["ts"] == now
        assert data["logs"][0]["tool_name"] == "run_command"

    def test_limit_clamped_to_max_1000(self, client):
        """limit > 1000 应被钳制为 1000"""
        r = client.get("/api/v1/audit-log?limit=5000")
        data = r.json()
        assert data["query"]["limit"] == 1000

    def test_limit_clamped_to_min_1(self, client):
        """limit < 1 应被钳制为 1"""
        r = client.get("/api/v1/audit-log?limit=0")
        data = r.json()
        assert data["query"]["limit"] == 1

    def test_tool_name_filter(self, app_with_db, client):
        """tool_name 过滤——只返回匹配工具的记录"""
        import asyncio
        loop = app_with_db.state._test_loop
        conn = app_with_db.state.db
        now = int(_t.time())
        loop.run_until_complete(conn.execute(
            "INSERT INTO audit_log(ts, agent_id, tool_name, phase) VALUES(?, ?, ?, ?)",
            (now, "hermes", "write_file", "execute"),
        ))
        loop.run_until_complete(conn.execute(
            "INSERT INTO audit_log(ts, agent_id, tool_name, phase) VALUES(?, ?, ?, ?)",
            (now, "hermes", "run_command", "execute"),
        ))
        loop.run_until_complete(conn.execute(
            "INSERT INTO audit_log(ts, agent_id, tool_name, phase) VALUES(?, ?, ?, ?)",
            (now, "openclaw", "write_file", "execute"),
        ))
        loop.run_until_complete(conn.commit())

        r = client.get("/api/v1/audit-log?tool_name=write_file")
        data = r.json()
        assert data["count"] == 2
        assert all(log["tool_name"] == "write_file" for log in data["logs"])

    def test_since_ts_filter(self, app_with_db, client):
        """since_ts 过滤——只返回 ts >= since_ts 的记录"""
        import asyncio
        loop = app_with_db.state._test_loop
        conn = app_with_db.state.db
        loop.run_until_complete(conn.execute(
            "INSERT INTO audit_log(ts, agent_id, tool_name, phase) VALUES(?, ?, ?, ?)",
            (1000, "hermes", "write_file", "execute"),
        ))
        loop.run_until_complete(conn.execute(
            "INSERT INTO audit_log(ts, agent_id, tool_name, phase) VALUES(?, ?, ?, ?)",
            (2000, "hermes", "run_command", "execute"),
        ))
        loop.run_until_complete(conn.commit())

        r = client.get("/api/v1/audit-log?since_ts=1500")
        data = r.json()
        assert data["count"] == 1
        assert data["logs"][0]["ts"] == 2000
        assert data["logs"][0]["tool_name"] == "run_command"

    def test_returns_newest_first(self, app_with_db, client):
        """返回顺序应为最新优先（ORDER BY ts DESC）"""
        import asyncio
        loop = app_with_db.state._test_loop
        conn = app_with_db.state.db
        for ts in [100, 300, 200]:
            loop.run_until_complete(conn.execute(
                "INSERT INTO audit_log(ts, agent_id, tool_name, phase) VALUES(?, ?, ?, ?)",
                (ts, "hermes", "run_command", "execute"),
            ))
        loop.run_until_complete(conn.commit())

        r = client.get("/api/v1/audit-log")
        data = r.json()
        tss = [log["ts"] for log in data["logs"]]
        assert tss == [300, 200, 100]

    def test_log_entry_fields(self, app_with_db, client):
        """每条 log 应包含完整字段: id/ts/agent_id/tool_name/args_summary/success/phase/session_id/task_id"""
        import asyncio
        loop = app_with_db.state._test_loop
        conn = app_with_db.state.db
        loop.run_until_complete(conn.execute(
            "INSERT INTO audit_log(ts, agent_id, tool_name, args_summary, success, phase, "
            "session_id, task_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (1234567890, "hermes", "write_file", "{'path': 'foo.py'}", 1, "execute", "sess1", "task1"),
        ))
        loop.run_until_complete(conn.commit())

        r = client.get("/api/v1/audit-log")
        data = r.json()
        log = data["logs"][0]
        assert log["agent_id"] == "hermes"
        assert log["tool_name"] == "write_file"
        assert log["args_summary"] == "{'path': 'foo.py'}"
        assert log["success"] == 1
        assert log["phase"] == "execute"
        assert log["session_id"] == "sess1"
        assert log["task_id"] == "task1"
        assert log["ts"] == 1234567890
        assert "id" in log

    def test_db_not_initialized_returns_error(self, client_no_db):
        """app.state.db 未设置时应返回 error=db_not_initialized（不崩溃）"""
        r = client_no_db.get("/api/v1/audit-log")
        assert r.status_code == 200
        data = r.json()
        assert data["logs"] == []
        assert data["count"] == 0
        assert data["error"] == "db_not_initialized"

    def test_combined_filter_tool_name_and_since_ts(self, app_with_db, client):
        """tool_name + since_ts 组合过滤"""
        import asyncio
        loop = app_with_db.state._test_loop
        conn = app_with_db.state.db
        loop.run_until_complete(conn.execute(
            "INSERT INTO audit_log(ts, agent_id, tool_name, phase) VALUES(?, ?, ?, ?)",
            (100, "hermes", "write_file", "execute"),
        ))
        loop.run_until_complete(conn.execute(
            "INSERT INTO audit_log(ts, agent_id, tool_name, phase) VALUES(?, ?, ?, ?)",
            (200, "hermes", "run_command", "execute"),
        ))
        loop.run_until_complete(conn.execute(
            "INSERT INTO audit_log(ts, agent_id, tool_name, phase) VALUES(?, ?, ?, ?)",
            (300, "hermes", "run_command", "execute"),
        ))
        loop.run_until_complete(conn.commit())

        r = client.get("/api/v1/audit-log?tool_name=run_command&since_ts=150")
        data = r.json()
        assert data["count"] == 2
        assert all(log["tool_name"] == "run_command" for log in data["logs"])
        assert all(log["ts"] >= 150 for log in data["logs"])
