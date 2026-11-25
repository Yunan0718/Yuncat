"""tests/unit/test_long_task_api.py —— Phase 79 长任务分解 API 测试

用最小 FastAPI app + mock app.state.task_decomposer，只挂 long_task router。
不启动完整 lifespan——避免依赖真实 adapters / model_config。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.long_task import router as long_task_router


# ═══════════════════════════════════════════════════════════════
# Mock
# ═══════════════════════════════════════════════════════════════

class _MockDecomposer:
    """模拟 TaskDecomposer——记录调用 + 返回预设结果"""
    def __init__(self):
        self.decompose_calls: list[dict] = []
        self.regenerate_calls: list[dict] = []
        self.decompose_result = {
            "nodes": [{"id": "n1", "level": 0, "title": "根", "parent_id": None,
                       "acceptance_criteria": ["完成"], "dependencies": []}],
            "root_count": 1,
            "total": 1,
            "source": "llm",
        }
        self.regenerate_result = {
            "nodes": [{"id": "n2", "level": 1, "title": "新子节点", "parent_id": "p1",
                       "acceptance_criteria": ["..."], "dependencies": []}],
            "deleted_count": 2,
            "source": "llm",
        }
        self.raise_on_decompose = None
        self.raise_on_regenerate = None

    async def decompose(self, task_id, confirmed_goal, agent_id="hermes", standards_context=None):
        self.decompose_calls.append({
            "task_id": task_id, "confirmed_goal": confirmed_goal, "agent_id": agent_id,
            "standards_context": standards_context,
        })
        if self.raise_on_decompose:
            raise self.raise_on_decompose
        return self.decompose_result

    async def regenerate_subtree(self, task_id, node_id, agent_id="hermes"):
        self.regenerate_calls.append({
            "task_id": task_id, "node_id": node_id, "agent_id": agent_id,
        })
        if self.raise_on_regenerate:
            raise self.raise_on_regenerate
        return self.regenerate_result


class _MockDB:
    """模拟 aiosqlite——get_plan_tree_by_task 调用"""
    def __init__(self, rows=None):
        self.rows = rows or []

    async def execute(self, sql, params=()):
        class _Cursor:
            def __init__(self, rows):
                self._rows = rows
            async def fetchall(self):
                return self._rows
        return _Cursor(self.rows)


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def mock_app():
    """最小 FastAPI app + mock decomposer + mock db"""
    app = FastAPI()
    app.include_router(long_task_router)
    app.state.task_decomposer = _MockDecomposer()
    app.state.db = _MockDB(rows=[])  # get_decomposition 返回空数组
    return app


@pytest.fixture
def client(mock_app):
    return TestClient(mock_app)


# ═══════════════════════════════════════════════════════════════
# POST /api/v1/tasks/{task_id}/decompose
# ═══════════════════════════════════════════════════════════════

class TestDecomposeAPI:
    """POST /api/v1/tasks/{task_id}/decompose"""

    def test_decompose_success(self, client, mock_app):
        """正常调用 → 200 + 返回分解结果"""
        resp = client.post("/api/v1/tasks/task-1/decompose", json={
            "confirmed_goal": "做一个 X",
            "agent_id": "hermes",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == "task-1"
        assert body["source"] == "llm"
        assert body["total"] == 1
        assert len(body["nodes"]) == 1
        # 验证 decomposer 被正确调用
        assert len(mock_app.state.task_decomposer.decompose_calls) == 1
        call = mock_app.state.task_decomposer.decompose_calls[0]
        assert call["task_id"] == "task-1"
        assert call["confirmed_goal"] == "做一个 X"
        assert call["agent_id"] == "hermes"

    def test_decompose_default_agent_id(self, client, mock_app):
        """不传 agent_id → 默认 'hermes'"""
        resp = client.post("/api/v1/tasks/task-2/decompose", json={
            "confirmed_goal": "做 Y",
        })
        assert resp.status_code == 200
        call = mock_app.state.task_decomposer.decompose_calls[0]
        assert call["agent_id"] == "hermes"

    def test_decompose_missing_confirmed_goal(self, client):
        """缺 confirmed_goal → 422（pydantic 校验）"""
        resp = client.post("/api/v1/tasks/task-3/decompose", json={})
        assert resp.status_code == 422

    def test_decompose_internal_error_returns_500(self, client, mock_app):
        """decomposer 抛异常 → 500"""
        mock_app.state.task_decomposer.raise_on_decompose = RuntimeError("boom")
        resp = client.post("/api/v1/tasks/task-4/decompose", json={
            "confirmed_goal": "Z",
        })
        assert resp.status_code == 500
        assert "分解失败" in resp.json()["detail"]

    def test_decompose_decomposer_not_initialized(self, mock_app):
        """app.state.task_decomposer=None → 503"""
        mock_app.state.task_decomposer = None
        client = TestClient(mock_app)
        resp = client.post("/api/v1/tasks/task-5/decompose", json={
            "confirmed_goal": "W",
        })
        assert resp.status_code == 503
        assert "未初始化" in resp.json()["detail"]


# ═══════════════════════════════════════════════════════════════
# GET /api/v1/tasks/{task_id}/decomposition
# ═══════════════════════════════════════════════════════════════

class TestGetDecompositionAPI:
    """GET /api/v1/tasks/{task_id}/decomposition"""

    def test_get_decomposition_empty(self, client):
        """无分解节点 → 200 + nodes=[]"""
        resp = client.get("/api/v1/tasks/task-empty/decomposition")
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == "task-empty"
        assert body["nodes"] == []
        assert body["total"] == 0


# ═══════════════════════════════════════════════════════════════
# POST /api/v1/tasks/{task_id}/nodes/{node_id}/decompose
# ═══════════════════════════════════════════════════════════════

class TestRegenerateSubtreeAPI:
    """POST /api/v1/tasks/{task_id}/nodes/{node_id}/decompose"""

    def test_regenerate_success(self, client, mock_app):
        """正常调用 → 200 + 返回新子节点"""
        resp = client.post("/api/v1/tasks/task-r/nodes/p1/decompose")
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == "task-r"
        assert body["node_id"] == "p1"
        assert body["deleted_count"] == 2
        assert len(body["nodes"]) == 1
        # 验证 decomposer 调用
        assert len(mock_app.state.task_decomposer.regenerate_calls) == 1
        call = mock_app.state.task_decomposer.regenerate_calls[0]
        assert call["task_id"] == "task-r"
        assert call["node_id"] == "p1"

    def test_regenerate_node_not_found_returns_404(self, client, mock_app):
        """regenerate_subtree 抛 ValueError → 404"""
        mock_app.state.task_decomposer.raise_on_regenerate = ValueError("node not found: ghost")
        resp = client.post("/api/v1/tasks/task-r2/nodes/ghost/decompose")
        assert resp.status_code == 404
        assert "node not found" in resp.json()["detail"]

    def test_regenerate_internal_error_returns_500(self, client, mock_app):
        """regenerate_subtree 抛 RuntimeError → 500"""
        mock_app.state.task_decomposer.raise_on_regenerate = RuntimeError("db broken")
        resp = client.post("/api/v1/tasks/task-r3/nodes/n1/decompose")
        assert resp.status_code == 500
        assert "重分解失败" in resp.json()["detail"]

    def test_regenerate_decomposer_not_initialized(self, mock_app):
        """app.state.task_decomposer=None → 503"""
        mock_app.state.task_decomposer = None
        client = TestClient(mock_app)
        resp = client.post("/api/v1/tasks/task-r4/nodes/n1/decompose")
        assert resp.status_code == 503
        assert "未初始化" in resp.json()["detail"]
