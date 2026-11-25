"""tests/unit/test_auth_routes.py —— Batch 7.6: /auth/token 与 /auth/refresh 端点测试"""
import os
import sys
from unittest import mock

import pytest
import aiosqlite
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from routes.auth import router as auth_router
from config import TOKEN


AUTH_TOKENS_DDL = """
CREATE TABLE auth_tokens (
    id TEXT PRIMARY KEY,
    token_type TEXT NOT NULL CHECK(token_type IN ('refresh')),
    token_hash TEXT NOT NULL UNIQUE,
    session_id TEXT,
    expires_at INTEGER,
    revoked_at INTEGER,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    last_used_at INTEGER
);
"""


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(AUTH_TOKENS_DDL)
    yield conn
    await conn.close()


@pytest.fixture
def client(db):
    app = FastAPI()
    app.state.db = db
    app.include_router(auth_router)
    return TestClient(app)


class TestTokenExchange:
    def test_exchange_with_static_token(self, client):
        with mock.patch("routes.auth.TOKEN", "static-test-token"):
            resp = client.post(
                "/auth/token",
                headers={"Authorization": "Bearer static-test-token"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0

    def test_exchange_with_wrong_token_returns_403(self, client):
        with mock.patch("routes.auth.TOKEN", "static-test-token"):
            resp = client.post(
                "/auth/token",
                headers={"Authorization": "Bearer wrong-token"},
            )
        assert resp.status_code == 403

    def test_exchange_without_auth_returns_403(self, client):
        resp = client.post("/auth/token")
        assert resp.status_code == 403


class TestTokenRefresh:
    def test_refresh_with_valid_token(self, client):
        from services import auth_service

        # 创建 refresh token
        raw = None
        import asyncio

        async def _create():
            return await auth_service.create_refresh_token(client.app.state.db)

        raw = asyncio.get_event_loop().run_until_complete(_create())

        resp = client.post("/auth/refresh", json={"refresh_token": raw})
        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body
        assert body["token_type"] == "bearer"

    def test_refresh_with_invalid_token_returns_401(self, client):
        resp = client.post("/auth/refresh", json={"refresh_token": "not-valid"})
        assert resp.status_code == 401

    def test_refresh_with_revoked_token_returns_401(self, client):
        from services import auth_service
        import asyncio

        async def _setup():
            raw = await auth_service.create_refresh_token(client.app.state.db)
            token_id = await auth_service.validate_refresh_token(client.app.state.db, raw)
            await auth_service.revoke_refresh_token(client.app.state.db, token_id)
            return raw

        raw = asyncio.get_event_loop().run_until_complete(_setup())
        resp = client.post("/auth/refresh", json={"refresh_token": raw})
        assert resp.status_code == 401
