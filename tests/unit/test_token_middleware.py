"""tests/unit/test_token_middleware.py —— Batch 7.6: HTTP token 中间件测试

验证 token_middleware 的安全契约：
1. 有效静态 Bearer token → 200
2. 有效 access token → 200
3. 过期/无效 access token → 403
4. 无效 token → 403
5. 缺失 Authorization → 403
6. 空 Authorization → 403
7. OPTIONS 预检 → 放行
8. /api/v1/health → 放行

本测试用 minimal app 复现中间件模式锁定契约；真实 main.py 中间件由
contract/test_phase49_backend.py 端到端覆盖。
"""
import hmac
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from services import auth_service

# 测试用 token（不与真实 YUNCAT_TOKEN 冲突）
_TEST_TOKEN = "test-secret-token-for-unit-test-only-7c9f2a"


def _build_app(token: str) -> FastAPI:
    """构造与 main.py:token_middleware 相同模式的 minimal app。"""
    app = FastAPI()

    @app.middleware("http")
    async def token_middleware(request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if path.startswith("/api/") and path not in (
            "/api/v1/health",
            "/api/v1/health/live",
            "/api/v1/health/ready",
        ):
            auth = request.headers.get("Authorization")
            if not auth or not auth.startswith("Bearer "):
                return JSONResponse(status_code=403, content={"error": "Invalid token"})
            bearer = auth[7:]
            if hmac.compare_digest(bearer, token):
                request.state.auth_type = "static"
            elif auth_service.validate_access_token(bearer):
                request.state.auth_type = "access"
            else:
                return JSONResponse(status_code=403, content={"error": "Invalid token"})
        if path in ("/docs", "/redoc", "/openapi.json"):
            auth = request.headers.get("Authorization")
            if not auth or not auth.startswith("Bearer "):
                return JSONResponse(status_code=403, content={"error": "Invalid token"})
            bearer = auth[7:]
            if not hmac.compare_digest(bearer, token) and not auth_service.validate_access_token(bearer):
                return JSONResponse(status_code=403, content={"error": "Invalid token"})
        return await call_next(request)

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/secure")
    async def secure():
        return {"data": "secret"}

    @app.get("/docs")
    async def docs():
        return {"docs": True}

    return app


@pytest.fixture
def client():
    with mock.patch("config.TOKEN", _TEST_TOKEN):
        yield TestClient(_build_app(_TEST_TOKEN))


def _make_access_token():
    with mock.patch("config.TOKEN", _TEST_TOKEN):
        return auth_service.create_access_token()


class TestValidToken:
    def test_valid_static_bearer_token_passes_api(self, client):
        resp = client.get(
            "/api/v1/secure",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"data": "secret"}

    def test_valid_access_token_passes_api(self, client):
        access_token = _make_access_token()
        resp = client.get(
            "/api/v1/secure",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert resp.status_code == 200

    def test_valid_static_bearer_token_passes_docs(self, client):
        resp = client.get(
            "/docs",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
        )
        assert resp.status_code == 200

    def test_valid_access_token_passes_docs(self, client):
        access_token = _make_access_token()
        resp = client.get(
            "/docs",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert resp.status_code == 200


class TestInvalidToken:
    def test_invalid_token_rejected(self, client):
        resp = client.get(
            "/api/v1/secure",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 403
        assert resp.json() == {"error": "Invalid token"}

    def test_expired_access_token_rejected(self, client):
        clock = 0.0
        with mock.patch("config.TOKEN", _TEST_TOKEN):
            access_token = auth_service.create_access_token(lambda: clock)
        clock += auth_service.ACCESS_TOKEN_TTL_SECONDS + 1
        with mock.patch("services.auth_service._time.time", lambda: clock):
            resp = client.get(
                "/api/v1/secure",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        assert resp.status_code == 403

    def test_missing_authorization_rejected(self, client):
        resp = client.get("/api/v1/secure")
        assert resp.status_code == 403

    def test_empty_authorization_rejected(self, client):
        resp = client.get("/api/v1/secure", headers={"Authorization": ""})
        assert resp.status_code == 403

    def test_non_bearer_authorization_rejected(self, client):
        resp = client.get(
            "/api/v1/secure",
            headers={"Authorization": f"Token {_TEST_TOKEN}"},
        )
        assert resp.status_code == 403


class TestExemptions:
    def test_health_no_auth(self, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200

    def test_options_no_auth(self, client):
        resp = client.options("/api/v1/secure")
        # OPTIONS 被中间件放行，但路由本身不存在 OPTIONS handler，故返回 405 而非 403
        assert resp.status_code == 405
