"""会话标题：纯策略、持久化、置顶语义、接口与自动生成。

覆盖 DSH `session-title` 能力缝在 Yuncat 的落地：
  - 标题是持久化的"当前生效值"，不是每次查询重新派生；
  - 用户手动命名置顶后，自动生成不得覆盖；
  - 没有模型 / 模型失败时，确定性回退标题依然成立；
  - 列表接口用已存标题，不用首条消息重新算。
"""
import asyncio
import os
import sys
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from session_titles import (  # noqa: E402
    SOURCE_FALLBACK,
    SOURCE_PROVIDER,
    SOURCE_USER,
    derive_fallback_title,
    normalize_title,
    resolve_title,
)


def test_normalize_strips_code_fence_and_control_chars():
    assert normalize_title("```py\nprint(1)\n```\n修复登录", 100) == "修复登录"
    assert normalize_title("前缀 ```py\ncode\n``` 后缀", 100) == "前缀 后缀"
    assert normalize_title("a\x00b", 100) == "ab"


def test_normalize_truncates_and_keeps_visibility():
    assert normalize_title("abcdef", 3) == "abc"
    assert normalize_title("   ", 10) == ""
    assert normalize_title(None, 10) == ""


def test_derive_fallback_title_cjk_and_english():
    assert derive_fallback_title("修复登录流程的鉴权问题", 30) == "修复登录流程的鉴权问题"
    cjk = derive_fallback_title("修" * 40, 10)
    assert len(cjk) <= 10 and cjk.endswith("…")
    english = derive_fallback_title("please " * 10, 20)
    assert english.endswith("…") and "please" in english
    assert derive_fallback_title("", 10) == ""


def test_resolve_title_prefers_stored_over_derived():
    assert resolve_title("我的标题", "首条消息", 200, 30) == "我的标题"
    assert resolve_title("", "首条消息", 200, 30) == "首条消息"


@pytest.fixture
async def title_db(tmp_path):
    import aiosqlite
    from repositories import SCHEMA_SQL
    conn = await aiosqlite.connect(str(tmp_path / "titles.db"))
    await conn.executescript(SCHEMA_SQL)
    await conn.execute(
        "INSERT INTO conversations(id,session_id,role,content,created_at) VALUES(?,?,?,?,?)",
        ("m1", "s1", "user", "修复登录流程的鉴权问题", 1),
    )
    await conn.commit()
    yield conn
    await conn.close()


async def test_set_and_get_session_title(title_db):
    from repositories import get_session_title, set_session_title
    row = await set_session_title(title_db, "s1", "我的标题", SOURCE_USER, auto_attempted=1)
    assert row["title"] == "我的标题"
    assert row["source"] == SOURCE_USER
    stored = await get_session_title(title_db, "s1")
    assert stored["title"] == "我的标题"
    assert stored["auto_attempted"] is True


async def test_user_title_is_pinned_against_auto_generation(title_db):
    from repositories import get_session_title, set_session_title
    await set_session_title(title_db, "s1", "我的标题", SOURCE_USER, auto_attempted=1)
    row = await set_session_title(title_db, "s1", "自动标题", SOURCE_PROVIDER)
    assert row["title"] == "我的标题"
    assert row["source"] == SOURCE_USER
    stored = await get_session_title(title_db, "s1")
    assert stored["title"] == "我的标题"


async def test_force_overrides_pin(title_db):
    from repositories import set_session_title
    await set_session_title(title_db, "s1", "我的标题", SOURCE_USER, auto_attempted=1)
    row = await set_session_title(
        title_db, "s1", "自动标题", SOURCE_PROVIDER, force=True)
    assert row["title"] == "自动标题"
    assert row["source"] == SOURCE_PROVIDER


async def test_claim_auto_attempt_is_single_shot(title_db):
    from repositories import claim_auto_attempt, get_session_title
    assert await claim_auto_attempt(title_db, "s1", "回退标题") is True
    assert await claim_auto_attempt(title_db, "s1", "回退标题") is False
    row = await get_session_title(title_db, "s1")
    assert row["title"] == "回退标题"
    assert row["auto_attempted"] is True


async def test_claim_auto_attempt_skips_pinned_title(title_db):
    from repositories import claim_auto_attempt, set_session_title
    await set_session_title(title_db, "s1", "我的标题", SOURCE_USER, auto_attempted=1)
    assert await claim_auto_attempt(title_db, "s1", "回退标题") is False


async def test_claim_auto_attempt_never_downgrades_provider_title(title_db):
    """模型润色出来的标题不能被回退标题覆盖。"""
    from repositories import claim_auto_attempt, get_session_title, set_session_title
    await set_session_title(
        title_db, "s1", "模型标题", SOURCE_PROVIDER, auto_attempted=0)
    assert await claim_auto_attempt(title_db, "s1", "回退标题") is False
    row = await get_session_title(title_db, "s1")
    assert row["title"] == "模型标题"


async def test_delete_session_title(title_db):
    from repositories import delete_session_title, set_session_title
    await set_session_title(title_db, "s1", "我的标题", SOURCE_USER, auto_attempted=1)
    assert await delete_session_title(title_db, "s1") == 1
    assert await delete_session_title(title_db, "s1") == 0


@pytest.fixture
async def title_client(tmp_path):
    import aiosqlite
    from repositories import SCHEMA_SQL
    from routes.sessions import router
    conn = await aiosqlite.connect(str(tmp_path / "routes.db"))
    await conn.executescript(SCHEMA_SQL)
    await conn.execute(
        "INSERT INTO conversations(id,session_id,role,content,created_at) VALUES(?,?,?,?,?)",
        ("m1", "s1", "user", "修复登录流程的鉴权问题", 1),
    )
    await conn.commit()
    app = FastAPI()
    app.state.db = conn
    app.include_router(router)
    yield TestClient(app), conn
    await conn.close()


def test_rename_persists_and_shows_in_list(title_client):
    client, _ = title_client
    resp = client.patch("/api/v1/sessions/s1", json={"title": "我的标题"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "我的标题"
    assert client.get("/api/v1/sessions/s1/title").json()["title"] == "我的标题"
    listed = client.get("/api/v1/sessions").json()
    assert listed[0]["title"] == "我的标题"


def test_rename_rejects_empty_title(title_client):
    client, _ = title_client
    assert client.patch("/api/v1/sessions/s1", json={"title": "   "}).status_code == 400


def test_rename_is_persisted_in_db(title_client):
    """重命名不能只活在前端内存里——换一条连接也要读得到。"""
    client, conn = title_client
    client.patch("/api/v1/sessions/s1", json={"title": "我的标题"})
    from repositories import get_session_title
    row = asyncio.get_event_loop().run_until_complete(get_session_title(conn, "s1"))
    assert row["title"] == "我的标题"


def test_generate_title_derives_from_first_message(title_client):
    client, _ = title_client
    body = client.post("/api/v1/sessions/s1/title").json()
    assert body["title"] == "修复登录流程的鉴权问题"
    assert body["regenerated"] is True
    assert client.get("/api/v1/sessions/s1/title").json()["source"] == SOURCE_FALLBACK


def test_generate_title_respects_user_pin(title_client):
    client, _ = title_client
    client.patch("/api/v1/sessions/s1", json={"title": "我的标题"})
    body = client.post("/api/v1/sessions/s1/title").json()
    assert body["title"] == "我的标题"
    assert body["regenerated"] is False
    forced = client.post("/api/v1/sessions/s1/title", json={"force": True}).json()
    assert forced["title"] == "修复登录流程的鉴权问题"


def test_delete_session_removes_title(title_client):
    client, _ = title_client
    client.patch("/api/v1/sessions/s1", json={"title": "我的标题"})
    assert client.delete("/api/v1/sessions/s1").status_code == 200
    assert client.get("/api/v1/sessions/s1/title").json()["title"] == ""


class _FakeModelConfig:
    async def get_active_config(self):
        return {"model": "test-model", "api_key": "k", "provider": "test", "base_url": "http://x"}


class _FakeAdapter:
    def __init__(self, content="模型标题", error=None):
        self.content = content
        self.error = error
        self.stopped = False
        self.calls = 0

    async def invoke(self, prompt, context=None):
        self.calls += 1
        if self.error:
            return {"error": self.error}
        return {"content": self.content}

    async def stop(self):
        self.stopped = True


class _FakeWsManager:
    def __init__(self):
        self.payloads = []

    async def broadcast(self, payload):
        self.payloads.append(payload)


def test_ensure_title_falls_back_then_refines_with_model(title_db, monkeypatch):
    from services.session_title_service import SessionTitleService
    adapter = _FakeAdapter()
    ws = _FakeWsManager()
    svc = SessionTitleService(title_db, model_config=_FakeModelConfig())
    svc.set_ws_manager(ws)
    monkeypatch.setattr(svc, "_create_title_adapter", lambda: adapter)
    row = asyncio.run(svc.ensure_title("s1"))
    assert row["title"] == "模型标题"
    assert row["source"] == SOURCE_PROVIDER
    assert adapter.calls == 1
    assert adapter.stopped is True
    assert ws.payloads[0]["type"] == "session_title_updated"


def test_ensure_title_keeps_fallback_when_model_fails(title_db, monkeypatch):
    from services.session_title_service import SessionTitleService
    adapter = _FakeAdapter(error="boom")
    svc = SessionTitleService(title_db, model_config=_FakeModelConfig())
    monkeypatch.setattr(svc, "_create_title_adapter", lambda: adapter)
    row = asyncio.run(svc.ensure_title("s1"))
    assert row["title"] == "修复登录流程的鉴权问题"
    assert row["source"] == SOURCE_FALLBACK


def test_ensure_title_does_not_touch_pinned_title(title_db, monkeypatch):
    from repositories import set_session_title
    from services.session_title_service import SessionTitleService
    asyncio.run(set_session_title(title_db, "s1", "我的标题", SOURCE_USER, auto_attempted=1))
    adapter = _FakeAdapter()
    svc = SessionTitleService(title_db, model_config=_FakeModelConfig())
    monkeypatch.setattr(svc, "_create_title_adapter", lambda: adapter)
    row = asyncio.run(svc.ensure_title("s1"))
    assert row["title"] == "我的标题"
    assert adapter.calls == 0


def test_ensure_title_is_idempotent(title_db, monkeypatch):
    from services.session_title_service import SessionTitleService
    adapter = _FakeAdapter()
    svc = SessionTitleService(title_db, model_config=_FakeModelConfig())
    monkeypatch.setattr(svc, "_create_title_adapter", lambda: adapter)
    asyncio.run(svc.ensure_title("s1"))
    row = asyncio.run(svc.ensure_title("s1"))
    assert row["title"] == "模型标题"
    assert adapter.calls == 1
