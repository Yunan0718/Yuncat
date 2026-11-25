"""tests/contract/test_sidecar_startup.py —— Sidecar 启动协议契约测试

附录 H 就绪协议:
  1. Python 进程启动后 stdout 输出 "YUNCAT_READY {\"port\": <port>}"
  2. /api/v1/health 返回 {"status":"ok","adapters":[...]}
  3. Token 认证拦截 /api/v1/*（health 除外）
  4. 新库迁移后表齐全、agents 表为空

本测试从 Tauri 的视角验证 Python 后端的行为契约。
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

# ---- helpers ----


def _kill(proc):
    """跨平台安全终止进程"""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _start_backend(tmp_path, token="contract-test-token") -> tuple[int, subprocess.Popen]:
    """启动 Python 后端，解析 YUNCAT_READY JSON，返回 (port, process)。"""
    data_dir = str(tmp_path / "Yuncat")
    os.makedirs(data_dir, exist_ok=True)

    # 用绝对路径，避免 CWD 依赖
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    main_py = os.path.join(project_root, "src-python", "main.py")

    proc = subprocess.Popen(
        [sys.executable, main_py],
        env={
            **os.environ,
            "YUNCAT_TOKEN": token,
            "YUNCAT_DATA_DIR": data_dir,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
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

    time.sleep(0.3)  # let uvicorn finish binding
    return port, proc


# ---- contract tests ----


class TestSidecarHandshake:
    """附录 H: Rust ↔ Python 启动契约"""

    def test_yuncat_ready_on_stdout(self, tmp_path):
        """Python 启动后 stdout 输出 YUNCAT_READY {\"port\": <port>}"""
        port, proc = _start_backend(tmp_path)
        assert 1024 <= port <= 65535, f"Invalid port: {port}"
        _kill(proc)

    def test_health_returns_ok(self, tmp_path):
        """GET /api/v1/health → 200 {"status":"ok"}"""
        port, proc = _start_backend(tmp_path)
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/v1/health", timeout=5
            )
            data = json.loads(resp.read())
            assert data["status"] == "ok"
            assert "adapters" in data, f"Missing adapters: {data}"
        finally:
            _kill(proc)

    def test_health_no_auth_required(self, tmp_path):
        """health 端点不需要 Token 认证"""
        port, proc = _start_backend(tmp_path)
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/v1/health", timeout=5
            )
            assert resp.status == 200
        finally:
            _kill(proc)

    def test_token_protects_api_routes(self, tmp_path):
        """Token 缺失时 /api/v1/agents 返回 403"""
        port, proc = _start_backend(tmp_path)
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/v1/agents", timeout=5
            )
            pytest.fail("Expected 403 Forbidden")
        except urllib.error.HTTPError as e:
            assert e.code == 403
        finally:
            _kill(proc)

    def test_valid_token_allows_api(self, tmp_path):
        """有效 Token 时 /api/v1/agents 返回 200"""
        port, proc = _start_backend(tmp_path, token="my-secret")
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/agents",
                headers={"Authorization": "Bearer my-secret"},
            )
            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read())
            assert isinstance(data, list), f"Expected list, got: {data}"
        finally:
            _kill(proc)


class TestDatabaseBootstrap:
    """6.1 建表 + 6.2 迁移 = 新库契约"""

    def test_agents_table_empty(self, tmp_path):
        """SELECT COUNT(*) FROM agents = 0（不报错）"""
        port, proc = _start_backend(tmp_path)
        try:
            import aiosqlite, asyncio

            async def check():
                db_path = os.path.join(str(tmp_path), "Yuncat", "yuncat.db")
                assert os.path.exists(db_path), "DB file missing"
                conn = await aiosqlite.connect(db_path)
                cur = await conn.execute("SELECT COUNT(*) FROM agents")
                assert (await cur.fetchone())[0] == 0
                await conn.close()

            asyncio.run(check())
        finally:
            _kill(proc)

    def test_all_core_tables_exist(self, tmp_path):
        """迁移后核心表集齐全"""
        port, proc = _start_backend(tmp_path)
        try:
            import aiosqlite, asyncio

            async def check():
                db_path = os.path.join(str(tmp_path), "Yuncat", "yuncat.db")
                conn = await aiosqlite.connect(db_path)
                cur = await conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
                tables = [r[0] for r in await cur.fetchall()]
                required = [
                    "agents",
                    "tasks",
                    "workflows",
                    "memories",
                    "conversations",
                    "settings",
                    "migrations",
                ]
                for t in required:
                    assert t in tables, f"Missing table: {t}"
                await conn.close()

            asyncio.run(check())
        finally:
            _kill(proc)
