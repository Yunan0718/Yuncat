"""tests/ci/test_path_resilience.py —— 中文路径 + 长路径容错测试

验收标准（方案 附录 N + PROGRESS.md）：
  - Yuncat 在含中文字符的路径下可正常启动、建库、响应 health
  - Yuncat 在长路径（>200 字符）下可正常启动

本测试模拟 CI 环境：临时创建含中文/长路径的数据目录，启动后端验证。
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

# 项目根目录（无论从哪个子目录跑 pytest，都能找到 src-python/main.py）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _kill(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _start_and_verify(tmp_path, token="ci-test-token", timeout=30):
    """在指定 tmp_path 下启动后端，验证 health + DB 建表。
    返回 (port, proc, db_path)。
    """
    data_dir = str(tmp_path / "Yuncat")
    os.makedirs(data_dir, exist_ok=True)

    proc = subprocess.Popen(
        [sys.executable, os.path.join(PROJECT_ROOT, "src-python", "main.py")],
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
    while time.time() - start < timeout:
        line = proc.stdout.readline()
        if not line:
            break
        if line.startswith("YUNCAT_READY"):
            ready_payload = json.loads(line.split(" ", 1)[1])
            port = int(ready_payload.get("port"))
            break

    if port is None:
        _kill(proc)
        raise RuntimeError(f"Backend did not emit YUNCAT_PORT within {timeout}s (path: {tmp_path})")

    time.sleep(0.3)

    try:
        # 1. health
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=5)
        data = json.loads(resp.read())
        assert data["status"] == "ok"
        assert "adapters" in data, f"Health mismatch: {data}"

        # 2. auth
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/agents", timeout=5)
            pytest.fail("Expected 403 without token")
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # 3. DB check
        import aiosqlite, asyncio

        async def check_db():
            db_path = os.path.join(data_dir, "yuncat.db")
            assert os.path.exists(db_path), f"DB not found: {db_path}"
            conn = await aiosqlite.connect(db_path)
            cur = await conn.execute("SELECT COUNT(*) FROM agents")
            assert (await cur.fetchone())[0] == 0
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = [r[0] for r in await cur.fetchall()]
            for t in ("agents", "tasks", "migrations", "settings"):
                assert t in tables, f"Missing table: {t}"
            await conn.close()

        asyncio.run(check_db())
    finally:
        _kill(proc)

    return port, proc, data_dir


# ============================================================
# 测试用例
# ============================================================


class TestChinesePath:
    """中文路径下后端正常启动"""

    def test_chinese_username_style(self, tmp_path):
        """模拟中文用户名：C:\\Users\\小明\\AppData\\..."""
        cn_dir = tmp_path / "Users" / "小明" / "AppData" / "Roaming"
        cn_dir.mkdir(parents=True)
        _start_and_verify(cn_dir)

    def test_chinese_project_name(self, tmp_path):
        """模拟中文项目名：D:\\项目\\云猫"""
        cn_dir = tmp_path / "项目" / "云猫"
        cn_dir.mkdir(parents=True)
        _start_and_verify(cn_dir)

    def test_chinese_mixed_path(self, tmp_path):
        """中英混合路径：C:\\Users\\张三\\yunmao-data"""
        cn_dir = tmp_path / "Users" / "张三" / "yunmao-data"
        cn_dir.mkdir(parents=True)
        _start_and_verify(cn_dir)


class TestLongPath:
    """长路径（>200 字符）下后端正常启动"""

    def _make_long_path(self, tmp_path, segments=5):
        """构造一个嵌套深目录路径，总长 > 200 字符"""
        base = tmp_path
        # 每层目录名 ~45 字符
        for i in range(segments):
            base = base / f"very-long-directory-name-layer-{i:03d}-abcdefghijklmnopqrstuvwxyz"
        base.mkdir(parents=True)
        return base

    def test_long_path_250_chars(self, tmp_path):
        """路径长度 >200 字符"""
        long_dir = self._make_long_path(tmp_path, segments=5)
        path_str = str(long_dir)
        # 确认确实够长（Windows MAX_PATH=260，我们在 >200 验证）
        assert len(path_str) > 200, f"Path only {len(path_str)} chars: {path_str}"
        _start_and_verify(long_dir, timeout=35)


class TestPythonStartup:
    """验证 main.py 能被直接执行（CI 兼容性）"""

    def test_python_syntax_valid(self):
        """python -c 'import main' 不报 SyntaxError"""
        result = subprocess.run(
            [sys.executable, "-c", "import py_compile; py_compile.compile('"
             + os.path.join(PROJECT_ROOT, "src-python", "main.py").replace("\\", "/")
             + "', doraise=True)"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_repositories_syntax_valid(self):
        """repositories/__init__.py 语法检查"""
        result = subprocess.run(
            [sys.executable, "-c", "import py_compile; py_compile.compile('"
             + os.path.join(PROJECT_ROOT, "src-python", "repositories", "__init__.py").replace("\\", "/")
             + "', doraise=True)"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_guard_syntax_valid(self):
        """guard.py 语法检查"""
        result = subprocess.run(
            [sys.executable, "-c", "import py_compile; py_compile.compile('"
             + os.path.join(PROJECT_ROOT, "src-python", "resources", "guard.py").replace("\\", "/")
             + "', doraise=True)"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"
