"""tests/unit/test_standards_lookup_route.py —— Phase 85 路由契约测试

POST /api/v1/tasks/{task_id}/lookup-standards 的 5 个分支：
1. 200 —— 正常触发标准查询（force=True 跳过冷却）
2. 400 —— 非法 domain
3. 400 —— 空 problem_description
4. 503 —— StandardsLookupAgent 单例未初始化
5. 500 —— 查询异常

路由用全局单例 get_standards_lookup_agent()（非 app.state），测试经
set_standards_lookup_agent 注入 mock，finally 恢复原始单例。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.standards_lookup import router as standards_lookup_router


# ═══════════════════════════════════════════════════════════════
# Mock StandardsLookupAgent
# ═══════════════════════════════════════════════════════════════

class _MockStandardsLookupAgent:
    """模拟 StandardsLookupAgent——lookup 返回可控结果或抛异常"""

    def __init__(self, result: dict | None = None, raise_exc: Exception | None = None):
        self._result = result
        self._raise = raise_exc
        self.lookup_calls: list[tuple] = []

    async def lookup(self, problem_description, domain="general", force=False):
        self.lookup_calls.append((problem_description, domain, force))
        if self._raise:
            raise self._raise
        return self._result


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(standards_lookup_router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _inject(mock_agent):
    """注入 mock 单例，返回原始单例"""
    from services.long_task.standards_lookup_agent import (
        get_standards_lookup_agent, set_standards_lookup_agent,
    )
    original = get_standards_lookup_agent()
    set_standards_lookup_agent(mock_agent)
    return original


def _restore(original):
    from services.long_task.standards_lookup_agent import set_standards_lookup_agent
    set_standards_lookup_agent(original)


# ═══════════════════════════════════════════════════════════════
# 5 分支契约测试
# ═══════════════════════════════════════════════════════════════

class TestTriggerLookup:
    """POST /api/v1/tasks/{task_id}/lookup-standards"""

    def test_200_normal_trigger(self, client):
        """正常触发标准查询——force=True，返回 sources/summary/recommendations"""
        result = {
            "ok": True, "sources": ["fastapi lifespan"],
            "summary": "找到 FastAPI lifespan 文档",
            "recommendations": ["用 @asynccontextmanager", "在 lifespan 中初始化资源"],
            "source_type": "context7", "ts": 12345,
        }
        mock = _MockStandardsLookupAgent(result=result)
        original = _inject(mock)
        try:
            resp = client.post("/api/v1/tasks/t1/lookup-standards", json={
                "problem_description": "ModuleNotFoundError: No module named 'fastapi'",
                "domain": "python",
            })
        finally:
            _restore(original)

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["task_id"] == "t1"
        assert body["source_type"] == "context7"
        assert len(body["recommendations"]) == 2
        assert "FastAPI" in body["summary"]
        # lookup 被调用，force=True（手动触发跳过冷却）
        assert len(mock.lookup_calls) == 1
        call = mock.lookup_calls[0]
        assert call[0] == "ModuleNotFoundError: No module named 'fastapi'"
        assert call[1] == "python"
        assert call[2] is True  # force

    def test_400_invalid_domain(self, client):
        """非法 domain → 400"""
        mock = _MockStandardsLookupAgent()
        original = _inject(mock)
        try:
            resp = client.post("/api/v1/tasks/t1/lookup-standards", json={
                "problem_description": "TypeError: bad arg",
                "domain": "invalid_domain",
            })
        finally:
            _restore(original)

        assert resp.status_code == 400
        assert "非法 domain" in resp.json()["detail"]

    def test_400_empty_problem_description(self, client):
        """空 problem_description → 400"""
        mock = _MockStandardsLookupAgent()
        original = _inject(mock)
        try:
            resp = client.post("/api/v1/tasks/t1/lookup-standards", json={
                "problem_description": "",
                "domain": "python",
            })
        finally:
            _restore(original)

        assert resp.status_code == 400
        assert "problem_description" in resp.json()["detail"]

    def test_503_agent_not_initialized(self, client):
        """StandardsLookupAgent 单例未初始化 → 503"""
        original = _inject(None)
        try:
            resp = client.post("/api/v1/tasks/t1/lookup-standards", json={
                "problem_description": "TypeError: x",
                "domain": "python",
            })
        finally:
            _restore(original)

        assert resp.status_code == 503
        assert "StandardsLookupAgent 未初始化" in resp.json()["detail"]

    def test_500_lookup_exception(self, client):
        """查询抛异常 → 500"""
        mock = _MockStandardsLookupAgent(raise_exc=RuntimeError("Context7 超时"))
        original = _inject(mock)
        try:
            resp = client.post("/api/v1/tasks/t1/lookup-standards", json={
                "problem_description": "ImportError: cannot import 'x'",
                "domain": "python",
            })
        finally:
            _restore(original)

        assert resp.status_code == 500
        assert "标准查询失败" in resp.json()["detail"]

    def test_200_default_domain_general(self, client):
        """domain 默认 general——正常触发"""
        result = {
            "ok": True, "sources": [],
            "summary": "未找到直接相关文档",
            "recommendations": ["升级查询关键词"],
            "source_type": "generic_advice", "ts": 12345,
        }
        mock = _MockStandardsLookupAgent(result=result)
        original = _inject(mock)
        try:
            resp = client.post("/api/v1/tasks/t1/lookup-standards", json={
                "problem_description": "编译失败：找不到符号",
            })
        finally:
            _restore(original)

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        # 默认 domain=general
        call = mock.lookup_calls[0]
        assert call[1] == "general"
