"""LoopSupervisor 管理 API：状态 + 重启 + 间隔调整（含边界校验）。"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class _FakeSupervisor:
    def __init__(self):
        self._specs = {
            "scheduler": SimpleNamespace(
                loop_type="timer", interval=30, priority=0,
                strategy=SimpleNamespace(value="permanent"),
            )
        }
        self._loops = {
            "scheduler": SimpleNamespace(
                status="running", last_run=100, last_ok=100,
                restart_count=0, error_count=0, iteration_count=5, last_error="",
            )
        }
        self.started = []

    async def start_one(self, loop_id):
        self.started.append(loop_id)

    def set_interval(self, loop_id, interval):
        if loop_id not in self._specs:
            return False
        self._specs[loop_id].interval = interval
        return True


@pytest.fixture
def supervisor_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.supervisor import router

    app = FastAPI()
    app.state.supervisor = _FakeSupervisor()
    app.include_router(router)
    return TestClient(app), app


def test_status_lists_loops(supervisor_client):
    client, _ = supervisor_client
    body = client.get("/api/v1/supervisor/status").json()
    assert body["ok"] is True
    assert body["total"] == 1
    assert body["running"] == 1
    assert body["loops"][0]["loop_id"] == "scheduler"
    assert body["loops"][0]["interval"] == 30


def test_interval_updates(supervisor_client):
    client, app = supervisor_client
    resp = client.post("/api/v1/supervisor/loops/scheduler/interval?interval=45")
    assert resp.status_code == 200, resp.text
    assert app.state.supervisor._specs["scheduler"].interval == 45


def test_interval_rejects_zero_and_huge(supervisor_client):
    """interval=0 会让循环空转打满 CPU；超大值等于停掉循环——都拒绝。"""
    client, _ = supervisor_client
    assert client.post("/api/v1/supervisor/loops/scheduler/interval?interval=0").status_code == 400
    assert client.post("/api/v1/supervisor/loops/scheduler/interval?interval=9999999").status_code == 400


def test_interval_unknown_loop_404(supervisor_client):
    client, _ = supervisor_client
    assert client.post("/api/v1/supervisor/loops/nope/interval?interval=30").status_code == 404


def test_restart_starts_and_404_for_unknown(supervisor_client):
    client, app = supervisor_client
    assert client.post("/api/v1/supervisor/loops/scheduler/restart").status_code == 200
    assert app.state.supervisor.started == ["scheduler"]
    assert client.post("/api/v1/supervisor/loops/nope/restart").status_code == 404
