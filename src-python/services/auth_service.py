"""services/auth_service.py —— Batch 7.6: Bearer token 生成/验证/刷新

桌面单用户场景：
- access token：无状态，15min TTL，格式 id:expiry:signature。
- refresh token：32-byte hex 随机串，SHA-256 hash 持久化到 auth_tokens 表，30d TTL。
- 签名密钥由每次启动生成的静态 TOKEN 经 PBKDF2 派生，Electron 重启后旧 token 自动失效。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time as _time
from typing import Callable

import aiosqlite

from config import (
    ACCESS_TOKEN_TTL_SECONDS,
    REFRESH_TOKEN_TTL_SECONDS,
    AUTH_SECRET_SALT_KEY,
    AUTH_TOKEN_PBKDF2_ITERATIONS,
)

logger = logging.getLogger("yuncat.auth")

_TOKEN_BYTES = 32
_SIGNATURE_SEPARATOR = ":"
_ACCESS_TOKEN_PARTS = 3


def _derive_signing_key(token: str | None = None) -> bytes:
    """用 PBKDF2-HMAC-SHA256 从静态 TOKEN 派生签名密钥。

    Batch A H3: token 默认参数在 import 时绑定，importlib.reload(config) 后不更新。
    改为运行时从 config 读取，支持热重载场景。
    """
    if token is None:
        from config import TOKEN as _token
        token = _token
    token_bytes = token.encode("utf-8")
    salt = hashlib.sha256(AUTH_SECRET_SALT_KEY.encode("utf-8")).digest()
    return hashlib.pbkdf2_hmac(
        "sha256",
        token_bytes,
        salt,
        AUTH_TOKEN_PBKDF2_ITERATIONS,
        dklen=_TOKEN_BYTES,
    )


def _sign(token_id: str, expiry: int, key: bytes) -> str:
    """对 id:expiry 做 HMAC-SHA256，返回 hex。"""
    payload = f"{token_id}{_SIGNATURE_SEPARATOR}{expiry}".encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def create_access_token(
    clock: Callable[[], float] | None = None,
) -> str:
    """创建新的 access token。"""
    now = int(clock() if clock else _time.time())
    token_id = secrets.token_hex(_TOKEN_BYTES)
    expiry = now + ACCESS_TOKEN_TTL_SECONDS
    key = _derive_signing_key()
    signature = _sign(token_id, expiry, key)
    return _SIGNATURE_SEPARATOR.join([token_id, str(expiry), signature])


def validate_access_token(
    token: str,
    clock: Callable[[], float] | None = None,
) -> bool:
    """验证 access token 是否有效且未过期。"""
    if not token or _SIGNATURE_SEPARATOR not in token:
        return False
    parts = token.split(_SIGNATURE_SEPARATOR)
    if len(parts) != _ACCESS_TOKEN_PARTS:
        return False
    token_id, expiry_str, signature = parts
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    now = int(clock() if clock else _time.time())
    if now >= expiry:
        return False
    key = _derive_signing_key()
    expected = _sign(token_id, expiry, key)
    return hmac.compare_digest(signature, expected)


def _hash_refresh_token(raw_token: str) -> str:
    """计算 refresh token 的 SHA-256 hash。"""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def create_refresh_token(
    conn: aiosqlite.Connection,
    session_id: str | None = None,
    clock: Callable[[], float] | None = None,
) -> str:
    """创建 refresh token 并持久化其 hash，返回原始随机串。"""
    now = int(clock() if clock else _time.time())
    token_id = secrets.token_hex(16)
    raw_token = secrets.token_hex(_TOKEN_BYTES)
    token_hash = _hash_refresh_token(raw_token)
    expires_at = now + REFRESH_TOKEN_TTL_SECONDS

    await conn.execute(
        """
        INSERT INTO auth_tokens (id, token_type, token_hash, session_id, expires_at, created_at, last_used_at)
        VALUES (?, 'refresh', ?, ?, ?, ?, ?)
        """,
        (token_id, token_hash, session_id, expires_at, now, now),
    )
    await conn.commit()
    return raw_token


async def validate_refresh_token(
    conn: aiosqlite.Connection,
    raw_token: str,
    clock: Callable[[], float] | None = None,
) -> str | None:
    """验证 refresh token；有效时更新 last_used_at 并返回 token id，否则 None。"""
    if not raw_token:
        return None
    token_hash = _hash_refresh_token(raw_token)
    now = int(clock() if clock else _time.time())

    cursor = await conn.execute(
        """
        SELECT id FROM auth_tokens
        WHERE token_hash = ? AND token_type = 'refresh'
          AND (expires_at IS NULL OR expires_at > ?)
          AND revoked_at IS NULL
        """,
        (token_hash, now),
    )
    row = await cursor.fetchone()
    if row is None:
        return None

    token_id = row[0]
    await conn.execute(
        "UPDATE auth_tokens SET last_used_at = ? WHERE id = ?",
        (now, token_id),
    )
    await conn.commit()
    return token_id


async def revoke_refresh_token(
    conn: aiosqlite.Connection,
    token_id: str,
    clock: Callable[[], float] | None = None,
) -> None:
    """撤销指定 refresh token。"""
    now = int(clock() if clock else _time.time())
    await conn.execute(
        "UPDATE auth_tokens SET revoked_at = ? WHERE id = ?",
        (now, token_id),
    )
    await conn.commit()


async def rotate_refresh_token(
    conn: aiosqlite.Connection,
    raw_token: str,
    clock: Callable[[], float] | None = None,
) -> tuple[str, str] | None:
    """Batch A H4: 刷新令牌轮换——验证旧 token → 撤销 → 签发新 token。

    对标 OAuth 2.0 RFC 6749 §10.4: refresh token 应在使用后轮换，
    限制被盗令牌的有效窗口（原实现仅更新 last_used_at，令牌 30d 内可重复使用）。

    返回 (token_id, new_raw_token) 或 None（验证失败时）。
    """
    token_id = await validate_refresh_token(conn, raw_token, clock=clock)
    if token_id is None:
        return None
    # 撤销旧 token（防止重放）
    await revoke_refresh_token(conn, token_id, clock=clock)
    # 签发新 refresh token
    new_raw_token = await create_refresh_token(conn, clock=clock)
    return (token_id, new_raw_token)


async def revoke_all_refresh_tokens(
    conn: aiosqlite.Connection,
    session_id: str | None = None,
    clock: Callable[[], float] | None = None,
) -> None:
    """按 session_id 撤销全部 refresh token；session_id 为 None 时撤销全部。"""
    now = int(clock() if clock else _time.time())
    if session_id:
        await conn.execute(
            "UPDATE auth_tokens SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL",
            (now, session_id),
        )
    else:
        await conn.execute(
            "UPDATE auth_tokens SET revoked_at = ? WHERE revoked_at IS NULL",
            (now,),
        )
    await conn.commit()
