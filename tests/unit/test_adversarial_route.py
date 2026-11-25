"""tests/unit/test_adversarial_route.py —— Phase 84 路由契约测试

POST /api/v1/tasks/{task_id}/challenge 的 4 个分支：
1. 200 —— 正常触发对抗验证（force=True 跳过冷却）
2. 400 —— 非法 stage / 空 plan_text
3. 503 —— AdversarialAgent 单例未初始化
4. 500 —— 对抗验证异常

路由用全局单例 get_adversarial_agent()（非 app.state），测试经
set_adversarial_agent 注入 mock，finally 恢复原始单例。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.adversarial import router as adversarial_router


# ═══════════════════════════════════════════════════════════════
# Mock AdversarialAgent
# ═══════════════════════════════════════════════════════════════

class _MockAdversarialAgent:
    """模拟 AdversarialAgent——challenge_plan 返回可控结果或抛异常"""

    def __init__(self, result: dict | None = None, raise_exc: Exception | None = None):
        self._result = result
        self._raise = raise_exc
        self.challenge_calls: list[tuple] = []

    async def challenge_plan(self, task_id, stage, plan_text, task_input,
                              step_id=None, force=False):
        self.challenge_calls.append((task_id, stage, plan_text, task_input, step_id, force))
        if self._raise:
            raise self._raise
        return self._result


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(adversarial_router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _inject(mock_agent):
    """注入 mock 单例，返回原始单例"""
    from services.long_task.adversarial_agent import (
        get_adversarial_agent, set_adversarial_agent,
    )
    original = get_adversarial_agent()
    set_adversarial_agent(mock_agent)
    return original


def _restore(original):
    from services.long_task.adversarial_agent import set_adversarial_agent
    set_adversarial_agent(original)


# ═══════════════════════════════════════════════════════════════
# 4 分支契约测试
# ═══════════════════════════════════════════════════════════════

class TestTriggerChallenge:
    """POST /api/v1/tasks/{task_id}/challenge"""

    def test_200_normal_trigger(self, client):
        """正常触发对抗验证——force=True，返回 review"""
        review = {
            "pass": False, "refuted_count": 1, "surviving_count": 0,
            "action": "replan", "reason": "0 survived, 1 refuted",
            "stage": "plan_generated", "task_id": "t1", "ts": 12345,
        }
        mock = _MockAdversarialAgent(result=review)
        original = _inject(mock)
        try:
            resp = client.post("/api/v1/tasks/t1/challenge", json={
                "stage": "plan_generated",
                "plan_text": "1. 步骤一\n2. 步骤二",
                "task_input": "做 X",
                "step_id": "n1",
            })
        finally:
            _restore(original)

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["task_id"] == "t1"
        assert body["review"]["action"] == "replan"
        assert body["review"]["refuted_count"] == 1
        # challenge_plan 被调用，force=True（手动触发跳过冷却）
        assert len(mock.challenge_calls) == 1
        call = mock.challenge_calls[0]
        assert call[0] == "t1"           # task_id
        assert call[1] == "plan_generated"  # stage
        assert call[4] == "n1"           # step_id
        assert call[5] is True           # force

    def test_400_invalid_stage(self, client):
        """非法 stage → 400"""
        mock = _MockAdversarialAgent()
        original = _inject(mock)
        try:
            resp = client.post("/api/v1/tasks/t1/challenge", json={
                "stage": "invalid_stage",
                "plan_text": "目标",
                "task_input": "",
            })
        finally:
            _restore(original)

        assert resp.status_code == 400
        assert "非法 stage" in resp.json()["detail"]

    def test_400_empty_plan_text(self, client):
        """空 plan_text → 400"""
        mock = _MockAdversarialAgent()
        original = _inject(mock)
        try:
            resp = client.post("/api/v1/tasks/t1/challenge", json={
                "stage": "goal_confirmed",
                "plan_text": "",
                "task_input": "",
            })
        finally:
            _restore(original)

        assert resp.status_code == 400
        assert "plan_text" in resp.json()["detail"]

    def test_503_agent_not_initialized(self, client):
        """AdversarialAgent 单例未初始化 → 503"""
        original = _inject(None)
        try:
            resp = client.post("/api/v1/tasks/t1/challenge", json={
                "stage": "goal_confirmed",
                "plan_text": "目标",
                "task_input": "",
            })
        finally:
            _restore(original)

        assert resp.status_code == 503
        assert "AdversarialAgent 未初始化" in resp.json()["detail"]

    def test_500_challenge_exception(self, client):
        """对抗验证抛异常 → 500"""
        mock = _MockAdversarialAgent(raise_exc=RuntimeError("LLM 超时"))
        original = _inject(mock)
        try:
            resp = client.post("/api/v1/tasks/t1/challenge", json={
                "stage": "output_merge",
                "plan_text": "产出",
                "task_input": "目标",
                "step_id": "s1",
            })
        finally:
            _restore(original)

        assert resp.status_code == 500
        assert "对抗验证失败" in resp.json()["detail"]
