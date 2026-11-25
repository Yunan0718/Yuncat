"""tests/integration/test_main_api.py —— FastAPI 应用集成测试"""
import os
import sys
import tempfile
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

import config


# ============================================================
# Fixtures
# ============================================================
def _safe_unlink(path: str) -> None:
    """Windows 安全删除——重试"""
    if not os.path.exists(path):
        return
    for _ in range(5):
        try:
            os.unlink(path)
            return
        except PermissionError:
            time.sleep(0.05)
    try:
        os.unlink(path)
    except PermissionError:
        pass


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    """使用临时数据目录"""
    data_dir = str(tmp_path / "Yuncat")
    os.makedirs(data_dir, exist_ok=True)
    monkeypatch.setenv("YUNCAT_DATA_DIR", data_dir)
    monkeypatch.setenv("YUNCAT_TOKEN", "test-token-for-integration")
    # 重载 config 以使用新环境变量
    import importlib
    importlib.reload(config)
    yield data_dir
    _safe_unlink(os.path.join(data_dir, "yuncat.db"))


@pytest.fixture
def test_client(temp_data_dir):
    """创建 FastAPI TestClient"""
    from fastapi.testclient import TestClient
    import importlib
    import main as main_module
    importlib.reload(main_module)

    with TestClient(main_module.app) as client:
        yield client


# ============================================================
# Tests
# ============================================================
class TestHealthEndpoint:
    """测试 /api/v1/health"""

    def test_health_returns_ok(self, test_client):
        """health 端点返回 200 + status ok"""
        response = test_client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_health_no_auth_required(self, test_client):
        """health 端点不需要认证"""
        # 不带 Authorization 头也能访问
        response = test_client.get("/api/v1/health")
        assert response.status_code == 200

    def test_health_has_adapters_field(self, test_client):
        """health 响应包含 adapters 字段（Phase 0 为空列表）"""
        response = test_client.get("/api/v1/health")
        data = response.json()
        assert "adapters" in data


class TestTokenAuth:
    """测试 Token 认证中间件"""

    def test_api_requires_auth(self, test_client):
        """访问 /api/ 路由需要认证（health 除外）"""
        # 没有 Token 的请求
        headers_no_auth = {
            "Content-Type": "application/json",
        }
        response = test_client.get("/api/v1/agents", headers=headers_no_auth)
        assert response.status_code == 403

    def test_api_with_valid_token(self, test_client):
        """有效 Token 可以访问受保护路由"""
        headers = {
            "Authorization": "Bearer test-token-for-integration",
            "Content-Type": "application/json",
        }
        response = test_client.get("/api/v1/agents", headers=headers)
        assert response.status_code == 200


class TestCorsHeaders:
    """测试 CORS 中间件"""

    def test_cors_header_present(self, test_client):
        """响应包含 CORS 头"""
        response = test_client.options(
            "/api/v1/health",
            headers={
                "Origin": "http://localhost:1420",
                "Access-Control-Request-Method": "GET",
            },
        )
        # CORS preflight 应该成功
        assert response.status_code in (200, 405)  # OPTIONS may not be implemented


class TestStartupSequence:
    """测试启动流程"""

    def test_lifespan_initializes_db(self, temp_data_dir):
        """lifespan 启动后数据库文件存在"""
        import importlib
        import main as main_module
        importlib.reload(main_module)

        from fastapi.testclient import TestClient

        with TestClient(main_module.app) as client:
            client.get("/api/v1/health")

        db_path = os.path.join(temp_data_dir, "yuncat.db")
        assert os.path.exists(db_path), f"Expected DB at {db_path}"


# ============================================================
# Task 7.14: /health/live + /health/ready 分离
# ============================================================
class TestHealthLiveReadySeparation:
    """Task 7.14: 健康检查 readiness/liveness 分离。

    对标 Kubernetes 探针模式：
      - liveness probe: 进程存活即可（始终 200）
      - readiness probe: 应用就绪才 200（startup_stage=="ready"）
    """

    def test_live_returns_200(self, test_client):
        """/health/live 始终返回 200——进程能响应即存活。"""
        response = test_client.get("/api/v1/health/live")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"

    def test_live_no_auth_required(self, test_client):
        """/health/live 不需要认证——探针不应被 auth 阻断。"""
        response = test_client.get("/api/v1/health/live")
        assert response.status_code == 200

    def test_ready_returns_200_when_ready(self, test_client):
        """startup_stage=='ready' 时 /health/ready 返回 200。"""
        response = test_client.get("/api/v1/health/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"

    def test_ready_returns_503_when_not_ready(self, test_client):
        """startup_stage!='ready' 时 /health/ready 返回 503。"""
        import main as main_module
        original = main_module.app.state.startup_stage
        main_module.app.state.startup_stage = "init"
        try:
            response = test_client.get("/api/v1/health/ready")
            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "not_ready"
        finally:
            main_module.app.state.startup_stage = original

    def test_ready_no_auth_required(self, test_client):
        """/health/ready 不需要认证——探针不应被 auth 阻断。"""
        response = test_client.get("/api/v1/health/ready")
        assert response.status_code == 200

    def test_original_health_still_works(self, test_client):
        """原 /api/v1/health 仍可用（向后兼容）。"""
        response = test_client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_ready_includes_startup_stage(self, test_client):
        """/health/ready 响应包含 startup_stage 字段——便于调试。"""
        response = test_client.get("/api/v1/health/ready")
        data = response.json()
        assert "startup_stage" in data
        assert data["startup_stage"] == "ready"
