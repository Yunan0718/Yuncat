"""tests/contract/test_prompt_hotreload.py —— Phase 4: Prompt 热加载契约测试

验证:
  1. GET /api/v1/prompts 列出所有 prompt 模块（含 __version__）
  2. GET /api/v1/prompts/{name} 返回变量当前值
  3. PUT /api/v1/prompts/{name} 更新变量 → overrides 生效
  4. POST /api/v1/prompts/reload 强制重载
  5. POST /api/v1/prompts/{name}/preview 渲染预览
  6. POST /api/v1/prompts/{name}/reset 清除 overrides
  7. 未知模块返回 404
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest


# ---- helpers (copied from test_sidecar_startup.py for standalone) ----

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

    time.sleep(0.5)
    return port, proc


def _api(port, path, method="GET", data=None, token="contract-test-token"):
    """发送 HTTP 请求到后端。返回 (status, body_dict)。"""
    url = f"http://127.0.0.1:{port}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestPromptList:
    """GET /api/v1/prompts —— 列表所有 prompt 模块"""

    def test_list_returns_all_modules(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            status, body = _api(port, "/api/v1/prompts")
            assert status == 200
            assert isinstance(body, list)
            assert len(body) >= 5  # commander, intent_classifier, memory_context, tool_descriptions, workflow_templates
            names = [p["name"] for p in body]
            assert "commander" in names
            assert "memory_context" in names
            # 每个模块都有 __version__
            for p in body:
                assert "version" in p
                assert "variables" in p
                assert "file_path" in p
        finally:
            _kill(proc)


class TestPromptGet:
    """GET /api/v1/prompts/{name} —— 获取单个模块详情"""

    def test_get_commander(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            status, body = _api(port, "/api/v1/prompts/commander")
            assert status == 200
            assert body["name"] == "commander"
            assert body["version"] == "1.0.0"
            assert "COMMANDER_STATIC_PREFIX" in body["variables"]
        finally:
            _kill(proc)

    def test_get_unknown_404(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            status, body = _api(port, "/api/v1/prompts/nonexistent")
            assert status == 404
        finally:
            _kill(proc)


class TestPromptUpdateReset:
    """PUT /api/v1/prompts/{name} —— 更新 + 重置 overrides"""

    def test_update_and_read_back(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            # 写 overrides
            status, body = _api(
                port, "/api/v1/prompts/intent_classifier",
                method="PUT",
                data={"variables": {"INTENT_CLASSIFIER_PROMPT": "OVERRIDDEN: test"}},
            )
            assert status == 200

            # 读回——overrides 优先
            status, body = _api(port, "/api/v1/prompts/intent_classifier")
            assert status == 200
            assert body["variables"]["INTENT_CLASSIFIER_PROMPT"] == "OVERRIDDEN: test"

            # reset
            status, body = _api(port, "/api/v1/prompts/intent_classifier/reset", method="POST")
            assert status == 200

            # 读回原始值
            status, body = _api(port, "/api/v1/prompts/intent_classifier")
            assert status == 200
            assert "OVERRIDDEN" not in body["variables"]["INTENT_CLASSIFIER_PROMPT"]
        finally:
            _kill(proc)

    def test_update_disallowed_var(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            status, body = _api(
                port, "/api/v1/prompts/commander",
                method="PUT",
                data={"variables": {"NOT_A_REAL_VAR": "nope"}},
            )
            assert status == 400
        finally:
            _kill(proc)


class TestPromptReload:
    """POST /api/v1/prompts/reload —— 强制重载"""

    def test_force_reload(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            status, body = _api(port, "/api/v1/prompts/reload", method="POST")
            assert status == 200
            assert "reloaded" in body
            assert len(body["reloaded"]) >= 5
        finally:
            _kill(proc)


class TestPromptPreview:
    """POST /api/v1/prompts/{name}/preview —— 上下文预览"""

    def test_preview_memory_context(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            status, body = _api(
                port, "/api/v1/prompts/memory_context/preview",
                method="POST",
                data={"context": {}},
            )
            assert status == 200
            assert body["name"] == "memory_context"
            assert "preview" in body
        finally:
            _kill(proc)

    def test_preview_commander_with_context(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            status, body = _api(
                port, "/api/v1/prompts/commander/preview",
                method="POST",
                data={"context": {"rules_context": "测试规则", "memory_context": "测试记忆"}},
            )
            assert status == 200
            assert body["name"] == "commander"
            assert "COMMANDER_STATIC_PREFIX" in body["preview"]
        finally:
            _kill(proc)
