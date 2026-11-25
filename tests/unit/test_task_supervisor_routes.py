"""tests/unit/test_task_supervisor_routes.py —— Phase 81 路由契约测试

用最小 FastAPI app + mock app.state.task_supervisor，只挂 task_supervisor router。
6 个端点的契约验证：GET view / POST inject / pause / resume / spec / ask。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.task_supervisor import router as task_supervisor_router


# ═══════════════════════════════════════════════════════════════
# Mock TaskSupervisor
# ═══════════════════════════════════════════════════════════════

class _MockView:
    """模拟 SupervisorView"""
    def to_dict(self):
        return {
            "task_id": "t1",
            "status": "executing",
            "confirmed_goal": "做一个 X",
            "grading_levels": ["能运行"],
            "assumptions": [],
            "plan_tree": [],
            "current_step_id": None,
            "elapsed_seconds": 100,
            "estimated_remaining_seconds": None,
            "review_log": [],
            "stall_rounds": 0,
            "suggestions": [],
        }


class _MockTaskSupervisor:
    """模拟 TaskSupervisor——记录所有调用"""
    def __init__(self):
        self.pause_calls: list[str] = []
        self.resume_calls: list[str] = []
        self.inject_calls: list[tuple] = []
        self.spec_calls: list[tuple] = []
        self.ask_calls: list[tuple] = []
        self.view_calls: list[str] = []
        self.ask_response = "这是 Supervisor 的回答"

    async def get_view(self, task_id):
        self.view_calls.append(task_id)
        return _MockView()

    async def pause(self, task_id):
        self.pause_calls.append(task_id)

    async def resume(self, task_id):
        self.resume_calls.append(task_id)
        # Batch 3b B2: supervisor.resume 透传 orchestrator.resume 的真实结果
        # （{"ok": bool, "status": str, "error": str}），路由层据此做 409/200 分流。
        # mock 必须返回 dict，否则 routes/task_supervisor.py 的 result.get("ok") 会崩。
        return {"ok": True, "status": "running"}

    async def inject_constraint(self, task_id, constraint):
        self.inject_calls.append((task_id, constraint))

    async def spec(self, task_id, new_goal=None):
        self.spec_calls.append((task_id, new_goal))

    async def ask_supervisor(self, task_id, question):
        self.ask_calls.append((task_id, question))
        return self.ask_response


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def mock_supervisor():
    return _MockTaskSupervisor()


@pytest.fixture
def app(mock_supervisor):
    app = FastAPI()
    app.include_router(task_supervisor_router)
    app.state.task_supervisor = mock_supervisor
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


# ═══════════════════════════════════════════════════════════════
# 6 端点契约测试
# ═══════════════════════════════════════════════════════════════

class TestGetView:
    def test_returns_supervisor_view(self, client, mock_supervisor):
        resp = client.get("/api/v1/tasks/t1/supervisor")
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == "t1"
        assert body["status"] == "executing"
        assert body["confirmed_goal"] == "做一个 X"
        assert mock_supervisor.view_calls == ["t1"]

    def test_no_supervisor_returns_503(self):
        app = FastAPI()
        app.include_router(task_supervisor_router)
        # 不设 app.state.task_supervisor
        client = TestClient(app)
        resp = client.get("/api/v1/tasks/t1/supervisor")
        assert resp.status_code == 503
        assert "未初始化" in resp.json()["detail"]


class TestInject:
    def test_inject_constraint(self, client, mock_supervisor):
        resp = client.post("/api/v1/tasks/t1/supervisor/inject", json={"constraint": "用 Python 3.12"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["constraint"] == "用 Python 3.12"
        assert mock_supervisor.inject_calls == [("t1", "用 Python 3.12")]

    def test_inject_missing_constraint_returns_422(self, client, mock_supervisor):
        resp = client.post("/api/v1/tasks/t1/supervisor/inject", json={})
        assert resp.status_code == 422  # pydantic 校验失败


class TestPause:
    def test_pause_task(self, client, mock_supervisor):
        resp = client.post("/api/v1/tasks/t1/supervisor/pause")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["status"] == "paused"
        assert mock_supervisor.pause_calls == ["t1"]


class TestResume:
    def test_resume_task(self, client, mock_supervisor):
        resp = client.post("/api/v1/tasks/t1/supervisor/resume")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["status"] == "running"
        assert mock_supervisor.resume_calls == ["t1"]


class TestSpec:
    def test_spec_with_new_goal(self, client, mock_supervisor):
        resp = client.post("/api/v1/tasks/t1/supervisor/spec", json={"new_goal": "新目标"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["new_goal"] == "新目标"
        assert mock_supervisor.spec_calls == [("t1", "新目标")]

    def test_spec_without_new_goal(self, client, mock_supervisor):
        resp = client.post("/api/v1/tasks/t1/supervisor/spec", json={})
        assert resp.status_code == 200
        assert mock_supervisor.spec_calls == [("t1", None)]


class TestAsk:
    def test_ask_returns_answer(self, client, mock_supervisor):
        resp = client.post("/api/v1/tasks/t1/supervisor/ask", json={"question": "下一步？"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == "t1"
        assert body["question"] == "下一步？"
        assert body["answer"] == "这是 Supervisor 的回答"
        assert mock_supervisor.ask_calls == [("t1", "下一步？")]

    def test_ask_missing_question_returns_422(self, client, mock_supervisor):
        resp = client.post("/api/v1/tasks/t1/supervisor/ask", json={})
        assert resp.status_code == 422
