"""tests/contract/test_phase49_backend.py —— Phase 49-C/D 后端契约测试

验证:
  1. POST /conversations/{id}/edit -> 返回新 conv_id
  2. GET  /conversations/{id}/versions -> 返回版本树
  3. POST /conversations/{id}/edit + task_id -> 派生新任务，parent_task_id 正确
  4. POST /tasks/{id}/retry -> 基于消息重试，parent_task_id 正确
  5. GET  /tasks/{id}/state -> 返回 task_state / status / messages
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest


TOKEN = "contract-test-token"


def _kill(proc):
    # 先按进程树收：sidecar 后端会派生 MCP stdio server 等子进程，
    # 只 terminate 父进程会让它们变孤儿（实测全量测试后残留一个 python.exe 不退出，
    # 还锁住临时库文件）。Windows 没有进程组，用 taskkill /T；POSIX 用进程组。
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=15,
            )
        else:
            os.killpg(os.getpgid(proc.pid), 15)
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _start_backend(tmp_path):
    data_dir = str(tmp_path / "Yuncat")
    os.makedirs(data_dir, exist_ok=True)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    main_py = os.path.join(project_root, "src-python", "main.py")
    proc = subprocess.Popen(
        [sys.executable, main_py],
        env={**os.environ, "YUNCAT_TOKEN": TOKEN, "YUNCAT_DATA_DIR": data_dir},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    port = None
    start = time.time()
    while time.time() - start < 60:  # 2026-09-10: 后端实际启动≈30s，预算持平会偶发挂，翻倍兜底
        line = proc.stdout.readline()
        if not line:
            break
        if line.startswith("YUNCAT_READY"):
            ready_payload = json.loads(line.split(" ", 1)[1])
            port = int(ready_payload.get("port"))
            break
    if port is None:
        _kill(proc)
        raise RuntimeError("Backend did not emit YUNCAT_READY within 60s")
    time.sleep(0.5)
    return port, proc


def _api(port, path, method="GET", data=None):
    url = f"http://127.0.0.1:{port}{path}"
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _wait_task(port, task_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = _api(port, f"/api/v1/tasks/{task_id}")
        if status == 200 and body.get("status") != "running":
            return body
        time.sleep(0.5)
    raise RuntimeError(f"Task {task_id} did not leave running status in {timeout}s")


def _submit_task(port, prompt):
    status, body = _api(port, "/api/v1/tasks", method="POST", data={
        "prompt": prompt,
        "agent_id": "yuncat",  # 2026-09-10: hermes 下线，默认原生
    })
    assert status == 200, body
    return body["task_id"]


def _get_user_conv_id(port, task_id):
    task = _wait_task(port, task_id)
    session_id = task["session_id"]
    status, body = _api(port, f"/api/v1/sessions/{session_id}/messages")
    assert status == 200, body
    msgs = [m for m in body if m.get("role") == "user"]
    assert msgs, "No user message found"
    return msgs[0]["id"], task


class TestPhase49Backend:
    """Phase 49-C/D 契约测试"""

    def test_edit_conversation_and_versions(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            task_id = _submit_task(port, "hello phase49 edit test")
            conv_id, _ = _get_user_conv_id(port, task_id)

            status, body = _api(port, f"/api/v1/conversations/{conv_id}/edit", method="POST", data={
                "content": "edited content",
            })
            assert status == 200, body
            assert "conv_id" in body
            assert "task_id" not in body
            new_conv_id = body["conv_id"]

            status, body = _api(port, f"/api/v1/conversations/{conv_id}/versions")
            assert status == 200, body
            assert body["root_id"] == conv_id
            ids = {n["id"] for n in body["nodes"]}
            assert conv_id in ids
            assert new_conv_id in ids
            assert any(e["from"] == conv_id and e["to"] == new_conv_id for e in body["edges"])
        finally:
            _kill(proc)

    def test_edit_conversation_spawns_task(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            task_id = _submit_task(port, "hello phase49 spawn test")
            conv_id, task = _get_user_conv_id(port, task_id)
            original_session = task["session_id"]

            status, body = _api(port, f"/api/v1/conversations/{conv_id}/edit", method="POST", data={
                "content": "spawned task content",
                "task_id": task_id,
            })
            assert status == 200, body
            assert "conv_id" in body
            assert "task_id" in body
            new_task_id = body["task_id"]

            new_task = _wait_task(port, new_task_id)
            assert new_task["parent_task_id"] == task_id
            assert new_task["session_id"] == original_session
        finally:
            _kill(proc)

    def test_retry_task(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            task_id = _submit_task(port, "hello phase49 retry test")
            conv_id, _ = _get_user_conv_id(port, task_id)

            status, body = _api(port, f"/api/v1/tasks/{task_id}/retry", method="POST", data={
                "conv_id": conv_id,
            })
            assert status == 200, body
            assert "task_id" in body
            new_task_id = body["task_id"]

            new_task = _wait_task(port, new_task_id)
            assert new_task["parent_task_id"] == task_id
        finally:
            _kill(proc)

    def test_task_state_endpoint(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            task_id = _submit_task(port, "hello phase49 state test")
            conv_id, task = _get_user_conv_id(port, task_id)

            status, body = _api(port, f"/api/v1/tasks/{task_id}/state")
            assert status == 200, body
            assert body["task_id"] == task_id
            assert "status" in body
            assert "task_state" in body
            assert body["messages"] is not None
            assert any(m.get("id") == conv_id for m in body["messages"])
        finally:
            _kill(proc)

    def test_inline_completion_endpoint(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            status, body = _api(port, "/api/v1/inline-completion", method="POST", data={
                "language": "python",
                "prefix": 'name = "foo"\nname = ',
                "suffix": "",
            })
            assert status == 200, body
            assert "items" in body
            assert isinstance(body["items"], list)
            assert len(body["items"]) > 0
            assert "insertText" in body["items"][0]
        finally:
            _kill(proc)
