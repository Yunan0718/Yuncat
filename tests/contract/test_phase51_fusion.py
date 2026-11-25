"""tests/contract/test_phase51_fusion.py —— Phase 51-F: HE/OC 深度融合 API 契约"""
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


class TestPhase51FusionApi:
    def test_agent_profile_yuncat(self, tmp_path):
        """2026-09-10: hermes 下线，profile 白名单改为 yuncat/openclaw/codex"""
        port, proc = _start_backend(tmp_path)
        try:
            status, body = _api(port, "/api/v1/agents/yuncat/profile")
            assert status == 200
            assert body["agent_id"] == "yuncat"
            assert "capabilities" in body
            assert "mcp_servers" in body
            assert "skills" in body
            assert "memory_providers" in body
        finally:
            _kill(proc)

    def test_agent_profile_openclaw(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            status, body = _api(port, "/api/v1/agents/openclaw/profile")
            assert status == 200
            assert body["agent_id"] == "openclaw"
            assert "vision" in body["capabilities"]
        finally:
            _kill(proc)

    def test_messaging_platforms(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            status, body = _api(port, "/api/v1/messaging/platforms")
            assert status == 200
            assert body["ok"] is True
            assert "telegram" in body["platforms"]
            assert "discord" in body["platforms"]
        finally:
            _kill(proc)
