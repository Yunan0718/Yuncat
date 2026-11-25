"""routes/auth.py —— Batch 7.6: Bearer token 兑换与刷新

提供 /auth/token（用静态 TOKEN 兑换 access + refresh token）与
/auth/refresh（用 refresh token 换取新的 access token）。

注意：/auth/* 路径在 token_middleware 中不被拦截，因此无需携带
Authorization header 访问本路由；兑换端点本身校验静态 TOKEN。
"""
import hmac
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from config import TOKEN
from services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])
_log = logging.getLogger("yuncat.auth")


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str


class AccessTokenResponse(BaseModel):
    access_token: str
    expires_in: int
    token_type: str


def _extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization")
    if auth and auth.startswith("Bearer "):
        return auth[7:]
    return None


@router.post("/token", response_model=TokenResponse)
async def exchange_token(request: Request):
    """Electron 主进程用静态 TOKEN 兑换 access + refresh token。"""
    provided = _extract_bearer(request)
    if not provided or not hmac.compare_digest(provided, TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token")

    conn = request.app.state.db
    access_token = auth_service.create_access_token()
    refresh_token = await auth_service.create_refresh_token(conn)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=auth_service.ACCESS_TOKEN_TTL_SECONDS,
        token_type="bearer",
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_access_token(payload: RefreshRequest, request: Request):
    """用 refresh token 换取新的 access token + rotated refresh token。

    Batch A H4: 实现刷新令牌轮换（RFC 6749 §10.4）——旧 refresh token 使用后即撤销，
    返回新的 refresh token。客户端必须更新存储的 refresh token。
    """
    conn = request.app.state.db
    result = await auth_service.rotate_refresh_token(conn, payload.refresh_token)
    if result is None:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    _token_id, new_refresh_token = result
    access_token = auth_service.create_access_token()
    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        expires_in=auth_service.ACCESS_TOKEN_TTL_SECONDS,
        token_type="bearer",
    )
