"""子 Agent 控制面：列表、停止、追加指令。"""
import os
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class _AgentService:
    def __init__(self):
        self.tasks = {
            "parent": {"id": "parent", "children": [{"id": "child", "status": "running"}]},
            "child": {
                "id": "child", "parent_task_id": "parent", "status": "running",
                "session_id": "s1", "agent_id": "yuncat",
            },
        }
        self.cancelled = []
        self.events = []

    async def get_task(self, task_id):
        return self.tasks.get(task_id)

    async def cancel_task(self, task_id):
        self.cancelled.append(task_id)
        self.tasks["child"]["status"] = "cancelled"

    async def _safe_send(self, task_id, payload):
        self.events.append((task_id, payload))


def _client(monkeypatch):
    from routes.tasks import router
    from services.agent import steer_inbox

    steer_inbox._PENDING.clear()
    service = _AgentService()
    app = FastAPI()
    app.state.agent_service = service
    app.include_router(router)
    return TestClient(app), service


def test_list_and_stop_subagent(monkeypatch):
    client, service = _client(monkeypatch)
    listed = client.get("/api/v1/tasks/parent/subagents").json()
    assert listed["items"][0]["id"] == "child"
    stopped = client.post("/api/v1/tasks/parent/subagents/child/stop").json()
    assert stopped["ok"] is True
    assert service.cancelled == ["child"]


def test_steer_subagent_queues_and_notifies_parent(monkeypatch):
    client, service = _client(monkeypatch)
    response = client.post(
        "/api/v1/tasks/parent/subagents/child/steer",
        json={"prompt": "先跑定向测试"},
    )
    assert response.status_code == 200
    assert response.json()["queued"] == 1
    from services.agent import steer_inbox
    assert steer_inbox.pending_steer_count("child") == 1
    assert service.events[-1][1]["type"] == "subagent_steer_queued"


def test_wrong_parent_and_terminal_child_are_rejected(monkeypatch):
    client, service = _client(monkeypatch)
    assert client.post(
        "/api/v1/tasks/other/subagents/child/stop",
    ).status_code == 404
    service.tasks["child"]["status"] = "completed"
    assert client.post(
        "/api/v1/tasks/parent/subagents/child/steer",
        json={"prompt": "continue"},
    ).status_code == 409
