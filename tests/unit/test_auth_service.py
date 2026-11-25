"""tests/unit/test_auth_service.py —— Batch 7.6: token 生成/验证/刷新测试"""
import os
import sys

import pytest
import aiosqlite

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from services import auth_service
from config import ACCESS_TOKEN_TTL_SECONDS, REFRESH_TOKEN_TTL_SECONDS


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
CREATE INDEX idx_auth_tokens_hash ON auth_tokens(token_hash);
CREATE INDEX idx_auth_tokens_active ON auth_tokens(expires_at, revoked_at);
"""


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(AUTH_TOKENS_DDL)
    yield conn
    await conn.close()


class TestAccessToken:
    def test_create_and_validate(self):
        token = auth_service.create_access_token()
        assert auth_service.validate_access_token(token) is True

    def test_expired_token_rejected(self):
        clock = 0.0
        token = auth_service.create_access_token(lambda: clock)
        clock += ACCESS_TOKEN_TTL_SECONDS + 1
        assert auth_service.validate_access_token(token, lambda: clock) is False

    def test_tampered_signature_rejected(self):
        token = auth_service.create_access_token()
        parts = token.split(":")
        parts[2] = "deadbeef" * 8
        tampered = ":".join(parts)
        assert auth_service.validate_access_token(tampered) is False

    def test_malformed_token_rejected(self):
        assert auth_service.validate_access_token("") is False
        assert auth_service.validate_access_token("only-two:parts") is False
        assert auth_service.validate_access_token("too:many:parts:here") is False


class TestRefreshToken:
    @pytest.mark.asyncio
    async def test_create_and_validate(self, db):
        clock = 1000.0
        raw = await auth_service.create_refresh_token(db, clock=lambda: clock)
        assert len(raw) == 64
        token_id = await auth_service.validate_refresh_token(db, raw, clock=lambda: clock)
        assert token_id is not None

    @pytest.mark.asyncio
    async def test_validate_updates_last_used_at(self, db):
        clock = 1000.0
        raw = await auth_service.create_refresh_token(db, clock=lambda: clock)
        await auth_service.validate_refresh_token(db, raw, clock=lambda: clock + 10)
        cursor = await db.execute(
            "SELECT last_used_at FROM auth_tokens WHERE token_hash = ?",
            (auth_service._hash_refresh_token(raw),),
        )
        row = await cursor.fetchone()
        assert row[0] == int(clock + 10)

    @pytest.mark.asyncio
    async def test_expired_refresh_token_rejected(self, db):
        clock = 1000.0
        raw = await auth_service.create_refresh_token(db, clock=lambda: clock)
        clock += REFRESH_TOKEN_TTL_SECONDS + 1
        assert await auth_service.validate_refresh_token(db, raw, clock=lambda: clock) is None

    @pytest.mark.asyncio
    async def test_revoked_refresh_token_rejected(self, db):
        clock = 1000.0
        raw = await auth_service.create_refresh_token(db, clock=lambda: clock)
        token_id = await auth_service.validate_refresh_token(db, raw, clock=lambda: clock)
        await auth_service.revoke_refresh_token(db, token_id, clock=lambda: clock)
        assert await auth_service.validate_refresh_token(db, raw, clock=lambda: clock) is None

    @pytest.mark.asyncio
    async def test_invalid_refresh_token_rejected(self, db):
        assert await auth_service.validate_refresh_token(db, "not-a-token") is None

    @pytest.mark.asyncio
    async def test_revoke_all_refresh_tokens(self, db):
        clock = 1000.0
        raw1 = await auth_service.create_refresh_token(db, session_id="s1", clock=lambda: clock)
        raw2 = await auth_service.create_refresh_token(db, session_id="s2", clock=lambda: clock)
        await auth_service.revoke_all_refresh_tokens(db, clock=lambda: clock + 1)
        assert await auth_service.validate_refresh_token(db, raw1, clock=lambda: clock + 2) is None
        assert await auth_service.validate_refresh_token(db, raw2, clock=lambda: clock + 2) is None

    @pytest.mark.asyncio
    async def test_revoke_by_session_id(self, db):
        clock = 1000.0
        raw1 = await auth_service.create_refresh_token(db, session_id="s1", clock=lambda: clock)
        raw2 = await auth_service.create_refresh_token(db, session_id="s2", clock=lambda: clock)
        await auth_service.revoke_all_refresh_tokens(db, session_id="s1", clock=lambda: clock + 1)
        assert await auth_service.validate_refresh_token(db, raw1, clock=lambda: clock + 2) is None
        assert await auth_service.validate_refresh_token(db, raw2, clock=lambda: clock + 2) is not None

    @pytest.mark.asyncio
    async def test_rotate_refresh_token_success(self, db):
        """Batch A H4: 轮换成功——旧 token 撤销，新 token 可用。"""
        clock = 1000.0
        raw = await auth_service.create_refresh_token(db, clock=lambda: clock)
        result = await auth_service.rotate_refresh_token(db, raw, clock=lambda: clock + 10)
        assert result is not None
        token_id, new_raw = result
        assert token_id  # 非空
        assert new_raw != raw  # 新 token 与旧 token 不同
        # 旧 token 已撤销——再次验证应失败
        assert await auth_service.validate_refresh_token(db, raw, clock=lambda: clock + 20) is None
        # 新 token 有效
        assert await auth_service.validate_refresh_token(db, new_raw, clock=lambda: clock + 20) is not None

    @pytest.mark.asyncio
    async def test_rotate_refresh_token_invalid_returns_none(self, db):
        """Batch A H4: 无效 token 轮换返回 None。"""
        result = await auth_service.rotate_refresh_token(db, "invalid-token")
        assert result is None

    @pytest.mark.asyncio
    async def test_rotate_refresh_token_old_revoked_after_use(self, db):
        """Batch A H4: 轮换后旧 token 不可重放（RFC 6749 §10.4）。"""
        clock = 1000.0
        raw = await auth_service.create_refresh_token(db, clock=lambda: clock)
        # 第一次轮换——成功
        result1 = await auth_service.rotate_refresh_token(db, raw, clock=lambda: clock + 10)
        assert result1 is not None
        # 第二次用同一个旧 token 轮换——应失败（已撤销）
        result2 = await auth_service.rotate_refresh_token(db, raw, clock=lambda: clock + 20)
        assert result2 is None
