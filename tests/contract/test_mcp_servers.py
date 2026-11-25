"""tests/contract/test_mcp_servers.py —— Phase 4: MCP 服务器管理契约测试

验证:
  1. POST /api/v1/mcp/servers 创建服务器
  2. GET /api/v1/mcp/servers 列出所有
  3. GET /api/v1/mcp/servers/{id} 获取单个
  4. PUT /api/v1/mcp/servers/{id} 更新
  5. DELETE /api/v1/mcp/servers/{id} 删除
  6. 404: 获取/更新/删除不存在的服务器
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest


def _kill(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _start_backend(tmp_path, token="contract-test-token") -> tuple[int, subprocess.Popen]:
    data_dir = str(tmp_path / "Yuncat")
    os.makedirs(data_dir, exist_ok=True)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    main_py = os.path.join(project_root, "src-python", "main.py")
    proc = subprocess.Popen(
        [sys.executable, main_py],
        env={**os.environ, "YUNCAT_TOKEN": token, "YUNCAT_DATA_DIR": data_dir},
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


def _api(port, path, method="GET", data=None, token="contract-test-token"):
    url = f"http://127.0.0.1:{port}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


SERVER_PAYLOAD = {
    "name": "Test MCP Server",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    "env": {"HOME": "/home/user"},
}


class TestMcpCrud:
    def test_create_and_list(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            # 创建
            status, body = _api(port, "/api/v1/mcp/servers", method="POST", data=SERVER_PAYLOAD)
            assert status == 200
            assert "server_id" in body

            # 列表应包含新创建的
            status, body = _api(port, "/api/v1/mcp/servers")
            assert status == 200
            assert isinstance(body, list)
            assert len(body) >= 1
            assert any(s["name"] == "Test MCP Server" for s in body)
        finally:
            _kill(proc)

    def test_get_by_id(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            _, created = _api(port, "/api/v1/mcp/servers", method="POST", data=SERVER_PAYLOAD)
            sid = created["server_id"]

            status, body = _api(port, f"/api/v1/mcp/servers/{sid}")
            assert status == 200
            assert body["name"] == "Test MCP Server"
            assert body["command"] == "npx"
            assert body["enabled"] is True
        finally:
            _kill(proc)

    def test_update(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            _, created = _api(port, "/api/v1/mcp/servers", method="POST", data=SERVER_PAYLOAD)
            sid = created["server_id"]

            status, _ = _api(
                port, f"/api/v1/mcp/servers/{sid}",
                method="PUT", data={"name": "Updated Server", "enabled": False},
            )
            assert status == 200

            # 验证更新
            status, body = _api(port, f"/api/v1/mcp/servers/{sid}")
            assert body["name"] == "Updated Server"
            assert body["enabled"] is False
        finally:
            _kill(proc)

    def test_delete(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            _, created = _api(port, "/api/v1/mcp/servers", method="POST", data=SERVER_PAYLOAD)
            sid = created["server_id"]

            status, _ = _api(port, f"/api/v1/mcp/servers/{sid}", method="DELETE")
            assert status == 200

            # 确认已删除
            status, _ = _api(port, f"/api/v1/mcp/servers/{sid}")
            assert status == 404
        finally:
            _kill(proc)

    def test_404_on_missing(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            status, _ = _api(port, "/api/v1/mcp/servers/nonexistent-id")
            assert status == 404

            status, _ = _api(
                port, "/api/v1/mcp/servers/nonexistent-id",
                method="PUT", data={"name": "X"},
            )
            assert status == 404
        finally:
            _kill(proc)

    def test_validation_requires_name_and_command(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            status, _ = _api(port, "/api/v1/mcp/servers", method="POST", data={"name": "NoCmd"})
            assert status == 400
        finally:
            _kill(proc)
