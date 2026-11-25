"""tests/unit/test_tool_confirm_batch4b.py —— Batch 4b 确认弹窗来源标识 + 过期联动

覆盖:
- create_pending_state_with_expiry 返回 (token, expires_at)；create_pending_state 保持 str 兼容
- WsManager.push_tool_confirm / push_tool_confirm_batch payload 携带 expires_at + task_prompt
- _expire_pending_states 标记过期 + 定向推送 tool_confirm_expired
- P4: /models/test 的 _validate_test_base_url 自定义网关 host 放行（无域名白名单）
"""
import os
import sys
import time
import json
import asyncio
import pytest
import aiosqlite

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
async def db(tmp_path):
    """创建带 schema 的 aiosqlite 连接"""
    db_path = os.path.join(str(tmp_path), "test_batch4b.db")
    conn = await aiosqlite.connect(db_path)
    from repositories import SCHEMA_SQL
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    yield conn
    await conn.close()
    _safe_unlink(db_path)


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


# ══════════════════════════════════════════════════════════════
# create_pending_state_with_expiry
# ══════════════════════════════════════════════════════════════

class TestCreatePendingStateWithExpiry:

    async def test_returns_token_and_expires_at(self, db):
        from tools.builtin.system_tool_confirm import create_pending_state_with_expiry

        before = time.time()
        token, expires_at = await create_pending_state_with_expiry(
            db, "pending_write", {"path": "a.txt"}, ttl=1800,
        )
        after = time.time()

        assert isinstance(token, str) and len(token) > 0
        assert before + 1800 <= expires_at <= after + 1800

        cursor = await db.execute(
            "SELECT expires_at FROM runtime_states WHERE id=?", (token,)
        )
        row = await cursor.fetchone()
        assert row[0] == pytest.approx(expires_at, abs=1e-3)

    async def test_create_pending_state_still_returns_str(self, db):
        """向后兼容：create_pending_state 仍只返回 token 字符串"""
        from tools.builtin.system_tool_confirm import create_pending_state

        token = await create_pending_state(db, "pending_write", {"path": "b.txt"})
        assert isinstance(token, str)


# ══════════════════════════════════════════════════════════════
# WsManager payload 字段
# ══════════════════════════════════════════════════════════════

class TestPushToolConfirmPayload:

    async def test_single_payload_contains_expires_at_and_task_prompt(self):
        from services.agent.ws_manager import WsManager

        mgr = WsManager()
        ws = _FakeWs()
        mgr.register_ws("task-1", ws)

        expires_at = time.time() + 1800
        await mgr.push_tool_confirm(
            "task-1", "write_file", {"path": "a.txt"}, "tok-1",
            expires_at, "帮我重构登录模块",
        )

        assert len(ws.sent) == 1
        payload = ws.sent[0]
        assert payload["type"] == "tool_confirm_required"
        assert payload["confirm_token"] == "tok-1"
        assert payload["expires_at"] == expires_at
        assert payload["task_prompt"] == "帮我重构登录模块"

    async def test_batch_payload_contains_task_prompt_and_item_expires(self):
        from services.agent.ws_manager import WsManager

        mgr = WsManager()
        ws = _FakeWs()
        mgr.register_ws("task-2", ws)

        e1, e2 = time.time() + 1700, time.time() + 1800
        items = [
            {"tool_name": "write_file", "preview": {}, "confirm_token": "t-a", "expires_at": e1},
            {"tool_name": "run_command", "preview": {}, "confirm_token": "t-b", "expires_at": e2},
        ]
        await mgr.push_tool_confirm_batch("task-2", items, "执行部署脚本")

        payload = ws.sent[0]
        assert payload["type"] == "tool_confirm_batch_required"
        assert payload["task_prompt"] == "执行部署脚本"
        assert payload["items"][0]["expires_at"] == e1
        assert payload["items"][1]["expires_at"] == e2


# ══════════════════════════════════════════════════════════════
# _expire_pending_states 过期推送
# ══════════════════════════════════════════════════════════════

class _FakeWsManager:
    def __init__(self):
        self.pushed = []

    async def safe_send(self, task_id, payload):
        self.pushed.append((task_id, payload))


class _FakeAgentService:
    def __init__(self):
        self.ws_manager = _FakeWsManager()


class _FakeState:
    pass


class _FakeApp:
    def __init__(self):
        self.state = _FakeState()
        self.state.agent_service = _FakeAgentService()


class TestExpirePendingStatesPush:

    async def test_expired_pending_pushes_tool_confirm_expired(self, db, monkeypatch):
        from tools.builtin.system_tool_confirm import _expire_pending_states

        now = time.time()
        await db.execute(
            "INSERT INTO runtime_states(id, state_type, payload, created_at, expires_at, task_id, status) "
            "VALUES(?,?,?,?,?,?,?)",
            ("tok-exp-1", "pending_write", json.dumps({}), now, now - 5, "task-exp", "pending"),
        )
        await db.execute(
            "INSERT INTO runtime_states(id, state_type, payload, created_at, expires_at, task_id, status) "
            "VALUES(?,?,?,?,?,?,?)",
            ("tok-exp-2", "pending_write", json.dumps({}), now, now + 1800, "task-ok", "pending"),
        )
        await db.commit()

        fake_app = _FakeApp()
        import context as yuncat_context
        monkeypatch.setattr(yuncat_context, "get_app", lambda: fake_app)

        count = await _expire_pending_states(db)

        assert count == 1
        # 仅过期且带 task_id 的记录被推送
        assert len(fake_app.state.agent_service.ws_manager.pushed) == 1
        task_id, payload = fake_app.state.agent_service.ws_manager.pushed[0]
        assert task_id == "task-exp"
        assert payload["type"] == "tool_confirm_expired"
        assert payload["confirm_token"] == "tok-exp-1"

        cursor = await db.execute("SELECT status FROM runtime_states WHERE id='tok-exp-1'")
        assert (await cursor.fetchone())[0] == "expired"
        cursor = await db.execute("SELECT status FROM runtime_states WHERE id='tok-exp-2'")
        assert (await cursor.fetchone())[0] == "pending"

    async def test_push_failure_does_not_break_marking(self, db, monkeypatch):
        """WS 推送异常不应影响过期标记流程（best-effort）"""
        from tools.builtin.system_tool_confirm import _expire_pending_states

        now = time.time()
        await db.execute(
            "INSERT INTO runtime_states(id, state_type, payload, created_at, expires_at, task_id, status) "
            "VALUES(?,?,?,?,?,?,?)",
            ("tok-exp-3", "pending_write", json.dumps({}), now, now - 1, "task-x", "pending"),
        )
        await db.commit()

        def _boom():
            raise RuntimeError("no app")

        import context as yuncat_context
        monkeypatch.setattr(yuncat_context, "get_app", _boom)

        count = await _expire_pending_states(db)
        assert count == 1
        cursor = await db.execute("SELECT status FROM runtime_states WHERE id='tok-exp-3'")
        assert (await cursor.fetchone())[0] == "expired"


# ══════════════════════════════════════════════════════════════
# P4: /models/test base_url 轻量校验（自定义网关放行）
# ══════════════════════════════════════════════════════════════

class TestValidateTestBaseUrl:

    def test_custom_gateway_host_passes(self):
        from routes.models import _validate_test_base_url
        # one-api/new-api 中转 / 自建网关——原白名单会 403，现在放行
        _validate_test_base_url("https://my-one-api.example.com/v1")
        _validate_test_base_url("http://10.0.0.5:3000")
        _validate_test_base_url("https://gateway.internal.cn")

    def test_bad_scheme_rejected_400(self):
        from fastapi import HTTPException
        from routes.models import _validate_test_base_url
        with pytest.raises(HTTPException) as exc:
            _validate_test_base_url("ftp://example.com")
        assert exc.value.status_code == 400

    def test_no_host_rejected_400(self):
        from fastapi import HTTPException
        from routes.models import _validate_test_base_url
        with pytest.raises(HTTPException) as exc:
            _validate_test_base_url("not-a-url")
        assert exc.value.status_code == 400

    def test_no_allowlist_attribute_left(self):
        """确认硬编码白名单表已删除"""
        import routes.models as models_mod
        assert not hasattr(models_mod, "_ALLOWED_PROVIDER_HOSTS")


class _StubModelConfig:
    def __init__(self):
        self.health = {}

    async def get_provider_config(self, provider):
        return {}

    def set_health_status(self, provider, status):
        self.health[provider] = status


class TestModelsTestRouteCustomHost:
    """/models/test 路由级：自定义 host 不再 403（连接失败返回 ok:False 而非 403）"""

    def test_custom_host_not_403(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes.models import router as models_router

        app = FastAPI()
        app.include_router(models_router)
        app.state.model_config = _StubModelConfig()
        client = TestClient(app)

        resp = client.post("/api/v1/models/test", json={
            "base_url": "http://127.0.0.1:59999",  # 未监听端口——自定义 host 放行后走到实际连接
            "provider": "custom-gateway",
            "api_key": "sk-test",
        })
        assert resp.status_code == 200, "自定义网关 host 不应被域名白名单 403"
        body = resp.json()
        assert body["ok"] is False  # 连接失败——但不是 403 拦截

    def test_missing_base_url_400(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes.models import router as models_router

        app = FastAPI()
        app.include_router(models_router)
        app.state.model_config = _StubModelConfig()
        client = TestClient(app)

        resp = client.post("/api/v1/models/test", json={"provider": "x", "api_key": "k"})
        assert resp.status_code == 400

    def test_invalid_scheme_400(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes.models import router as models_router

        app = FastAPI()
        app.include_router(models_router)
        app.state.model_config = _StubModelConfig()
        client = TestClient(app)

        resp = client.post("/api/v1/models/test", json={
            "base_url": "file:///etc/passwd", "provider": "x", "api_key": "k",
        })
        assert resp.status_code == 400
