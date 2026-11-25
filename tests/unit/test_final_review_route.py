"""tests/unit/test_final_review_route.py —— Phase 83 Gap 4 路由契约测试

POST /api/v1/tasks/{task_id}/review 的 4 个分支：
1. 200 —— 正常触发终审（force=True 跳过冷却）
2. 400 —— 非法 stage
3. 503 —— FinalReviewAgent 单例未初始化
4. 500 —— 审查异常

路由用全局单例 get_final_review_agent()（非 app.state），测试经
set_final_review_agent 注入 mock，finally 恢复原始单例。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.final_review import router as final_review_router


# ═══════════════════════════════════════════════════════════════
# Mock FinalReviewAgent
# ═══════════════════════════════════════════════════════════════

class _MockFinalReviewAgent:
    """模拟 FinalReviewAgent——review 返回可控结果或抛异常"""

    def __init__(self, result: dict | None = None, raise_exc: Exception | None = None):
        self._result = result
        self._raise = raise_exc
        self.review_calls: list[tuple] = []

    async def review(self, task_id, stage, context, step_id=None, force=False):
        self.review_calls.append((task_id, stage, context, step_id, force))
        if self._raise:
            raise self._raise
        return self._result


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(final_review_router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _inject(mock_agent):
    """注入 mock 单例，返回原始单例"""
    from services.long_task.final_review_agent import (
        get_final_review_agent, set_final_review_agent,
    )
    original = get_final_review_agent()
    set_final_review_agent(mock_agent)
    return original


def _restore(original):
    from services.long_task.final_review_agent import set_final_review_agent
    set_final_review_agent(original)


# ═══════════════════════════════════════════════════════════════
# 4 分支契约测试
# ═══════════════════════════════════════════════════════════════

class TestTriggerReview:
    """POST /api/v1/tasks/{task_id}/review"""

    def test_200_normal_trigger(self, client):
        """正常触发终审——force=True，返回 review"""
        review = {
            "pass": True, "score": 85, "action": "proceed",
            "issues": [], "reason": "ok",
            "stage": "pre_execution", "task_id": "t1", "ts": 12345,
        }
        mock = _MockFinalReviewAgent(result=review)
        original = _inject(mock)
        try:
            resp = client.post("/api/v1/tasks/t1/review", json={
                "stage": "pre_execution",
                "context": {"goal": "做 X", "step_title": "步骤1"},
                "step_id": "n1",
            })
        finally:
            _restore(original)

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["task_id"] == "t1"
        assert body["review"]["action"] == "proceed"
        assert body["review"]["score"] == 85
        # review 被调用，force=True（手动触发跳过冷却）
        assert len(mock.review_calls) == 1
        assert mock.review_calls[0][0] == "t1"
        assert mock.review_calls[0][1] == "pre_execution"
        assert mock.review_calls[0][3] == "n1"  # step_id
        assert mock.review_calls[0][4] is True   # force

    def test_400_invalid_stage(self, client):
        """非法 stage → 400"""
        mock = _MockFinalReviewAgent()
        original = _inject(mock)
        try:
            resp = client.post("/api/v1/tasks/t1/review", json={
                "stage": "invalid_stage",
                "context": {},
            })
        finally:
            _restore(original)

        assert resp.status_code == 400
        assert "非法 stage" in resp.json()["detail"]
        # review 未被调用（校验在前）
        assert len(mock.review_calls) == 0

    def test_503_agent_missing(self, client):
        """FinalReviewAgent 单例未初始化（None）→ 503"""
        original = _inject(None)
        try:
            resp = client.post("/api/v1/tasks/t1/review", json={
                "stage": "post_execution",
                "context": {"output": "done"},
            })
        finally:
            _restore(original)

        assert resp.status_code == 503
        assert "未初始化" in resp.json()["detail"]

    def test_500_review_exception(self, client):
        """审查抛异常 → 500"""
        mock = _MockFinalReviewAgent(raise_exc=RuntimeError("LLM 网络不可用"))
        original = _inject(mock)
        try:
            resp = client.post("/api/v1/tasks/t1/review", json={
                "stage": "pre_dangerous_tool",
                "context": {"tool_name": "delete_file", "args": {"path": "/"}},
            })
        finally:
            _restore(original)

        assert resp.status_code == 500
        assert "审查失败" in resp.json()["detail"]

    def test_200_minimal_body(self, client):
        """最小请求体（仅 stage，context/step_id 可选）→ 200"""
        review = {
            "pass": False, "score": 40, "action": "ask_user",
            "issues": ["需确认"], "reason": "可疑",
            "stage": "pre_dangerous_tool", "task_id": "t2", "ts": 0,
        }
        mock = _MockFinalReviewAgent(result=review)
        original = _inject(mock)
        try:
            resp = client.post("/api/v1/tasks/t2/review", json={
                "stage": "pre_dangerous_tool",
            })
        finally:
            _restore(original)

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["task_id"] == "t2"
        assert body["review"]["action"] == "ask_user"
        # context 默认 {}，step_id 默认 None，force=True
        assert mock.review_calls[0][2] == {}      # context
        assert mock.review_calls[0][3] is None     # step_id
        assert mock.review_calls[0][4] is True     # force

    def test_422_missing_stage(self, client):
        """缺 stage 字段 → pydantic 422"""
        mock = _MockFinalReviewAgent()
        original = _inject(mock)
        try:
            resp = client.post("/api/v1/tasks/t1/review", json={
                "context": {},
            })
        finally:
            _restore(original)

        assert resp.status_code == 422  # pydantic 校验失败
