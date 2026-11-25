"""tests/unit/test_error_response.py —— Task 7.16: 统一错误响应格式测试

对标主流 API 错误响应格式：
  - RFC 7807 (Problem Details for HTTP APIs)
  - JSON:API errors
  - FastAPI 社区惯例

统一格式：{"code": "ERROR_CODE", "message": "...", "details": {...}}

前端 useApi.ts 的 ApiError 已支持从 detail/message 字段提取友好消息，
统一后前端自动兼容（message 字段优先级仅次于 detail）。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ============================================================
# error_response() helper 单元测试
# ============================================================
class TestErrorResponseHelper:
    """测试 error_response() helper 的基本行为。"""

    def test_returns_correct_status_code(self):
        from schemas.error_response import error_response

        resp = error_response(status_code=404, message="Not found")
        assert resp.status_code == 404

    def test_returns_unified_format(self):
        from schemas.error_response import error_response

        resp = error_response(status_code=400, message="Bad request")
        data = json.loads(resp.body)
        assert "code" in data
        assert "message" in data
        assert "details" in data
        assert data["message"] == "Bad request"

    def test_code_auto_derived_from_status(self):
        from schemas.error_response import error_response

        resp = error_response(status_code=404, message="Not found")
        data = json.loads(resp.body)
        assert data["code"] == "NOT_FOUND"

    def test_code_auto_derived_for_common_statuses(self):
        """HTTP status → error code 映射覆盖常见状态码。"""
        from schemas.error_response import error_response

        cases = {
            400: "BAD_REQUEST",
            401: "UNAUTHORIZED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            409: "CONFLICT",
            413: "PAYLOAD_TOO_LARGE",
            422: "UNPROCESSABLE_ENTITY",
            429: "RATE_LIMITED",
            500: "INTERNAL_ERROR",
            503: "SERVICE_UNAVAILABLE",
        }
        for status, expected_code in cases.items():
            resp = error_response(status_code=status, message="test")
            data = json.loads(resp.body)
            assert data["code"] == expected_code, f"status {status} should map to {expected_code}, got {data['code']}"

    def test_custom_code_overrides_auto(self):
        from schemas.error_response import error_response

        resp = error_response(status_code=400, message="test", code="CUSTOM_CODE")
        data = json.loads(resp.body)
        assert data["code"] == "CUSTOM_CODE"

    def test_details_default_empty_dict(self):
        from schemas.error_response import error_response

        resp = error_response(status_code=400, message="test")
        data = json.loads(resp.body)
        assert data["details"] == {}

    def test_details_passed_through(self):
        from schemas.error_response import error_response

        resp = error_response(status_code=400, message="test", details={"field": "value"})
        data = json.loads(resp.body)
        assert data["details"] == {"field": "value"}


# ============================================================
# 全局异常处理器集成测试
# ============================================================
class TestGlobalExceptionHandlers:
    """测试 register_exception_handlers() 注册的全局异常处理器。"""

    @pytest.fixture
    def test_app(self):
        """创建带全局异常处理器的最小 FastAPI app。"""
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel

        from schemas.error_response import register_exception_handlers

        app = FastAPI()
        register_exception_handlers(app)

        @app.get("/raise-http-exception")
        async def raise_http_exception():
            raise HTTPException(status_code=404, detail="Resource not found")

        @app.get("/raise-400")
        async def raise_400():
            raise HTTPException(status_code=400, detail="Bad input")

        @app.get("/raise-500")
        async def raise_500():
            raise HTTPException(status_code=500, detail="Server error")

        @app.get("/raise-403")
        async def raise_403():
            raise HTTPException(status_code=403, detail="Forbidden")

        @app.get("/unhandled-exception")
        async def unhandled_exception():
            raise RuntimeError("Unexpected error")

        class UserModel(BaseModel):
            name: str
            age: int

        @app.post("/validation-error")
        async def validation_error(user: UserModel):
            return {"name": user.name, "age": user.age}

        @app.get("/direct-error-response")
        async def direct_error_response():
            from schemas.error_response import error_response
            return error_response(status_code=409, message="Conflict occurred", code="DUPLICATE_RESOURCE")

        return app

    @pytest.fixture
    def client(self, test_app):
        from fastapi.testclient import TestClient

        with TestClient(test_app, raise_server_exceptions=False) as c:
            yield c

    def test_http_exception_unified_format(self, client):
        """HTTPException 被统一格式化为 {code, message, details}。"""
        resp = client.get("/raise-http-exception")
        assert resp.status_code == 404
        data = resp.json()
        assert data["code"] == "NOT_FOUND"
        assert data["message"] == "Resource not found"
        assert "details" in data

    def test_http_exception_400(self, client):
        resp = client.get("/raise-400")
        assert resp.status_code == 400
        data = resp.json()
        assert data["code"] == "BAD_REQUEST"
        assert data["message"] == "Bad input"

    def test_http_exception_403(self, client):
        resp = client.get("/raise-403")
        assert resp.status_code == 403
        data = resp.json()
        assert data["code"] == "FORBIDDEN"
        assert data["message"] == "Forbidden"

    def test_http_exception_500(self, client):
        resp = client.get("/raise-500")
        assert resp.status_code == 500
        data = resp.json()
        assert data["code"] == "INTERNAL_ERROR"
        assert data["message"] == "Server error"

    def test_unhandled_exception_returns_500(self, client):
        """未捕获异常返回 500 + 统一格式（不泄露堆栈）。"""
        resp = client.get("/unhandled-exception")
        assert resp.status_code == 500
        data = resp.json()
        assert data["code"] == "INTERNAL_ERROR"
        assert "message" in data
        # 不应泄露内部异常堆栈/详细信息
        assert "Unexpected error" not in data.get("message", "")

    def test_validation_error_returns_422(self, client):
        """Pydantic 验证错误返回 422 + 统一格式 + details 含错误详情。"""
        resp = client.post("/validation-error", json={"name": "test"})  # 缺 age
        assert resp.status_code == 422
        data = resp.json()
        assert data["code"] == "VALIDATION_ERROR"
        assert "message" in data
        assert "details" in data

    def test_detail_string_preserved_as_message(self, client):
        """HTTPException.detail 字符串被保留为 message。"""
        resp = client.get("/raise-http-exception")
        data = resp.json()
        assert data["message"] == "Resource not found"

    def test_direct_error_response_passthrough(self, client):
        """直接 return error_response() 不被异常处理器干扰。"""
        resp = client.get("/direct-error-response")
        assert resp.status_code == 409
        data = resp.json()
        assert data["code"] == "DUPLICATE_RESOURCE"
        assert data["message"] == "Conflict occurred"
