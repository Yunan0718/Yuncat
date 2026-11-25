"""tests/contract/test_mcp_discovery.py —— Phase 51-D: MCP 本地配置自动发现"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest


def _module():
    sys_path = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
    sys.path.insert(0, sys_path)
    from services import mcp_discovery
    return mcp_discovery


def _kill(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _start_backend(tmp_path, token="contract-test-token", discovery_paths: str | None = None) -> tuple[int, subprocess.Popen]:
    data_dir = str(tmp_path / "Yuncat")
    os.makedirs(data_dir, exist_ok=True)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    main_py = os.path.join(project_root, "src-python", "main.py")
    env = {**os.environ, "YUNCAT_TOKEN": token, "YUNCAT_DATA_DIR": data_dir}
    if discovery_paths is not None:
        env["YUNCAT_MCP_DISCOVERY_PATHS"] = discovery_paths
    proc = subprocess.Popen(
        [sys.executable, main_py],
        env=env,
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


def test_parse_mcp_servers():
    mcp_discovery = _module()
    data = {
        "mcpServers": {
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                "env": {"KEY": "value"},
            },
            "sqlite": {
                "command": "uvx",
                "args": ["mcp-server-sqlite", "--db-path", "test.db"],
            },
        }
    }
    servers = mcp_discovery._parse_mcp_servers(data)
    assert len(servers) == 2
    names = {s["name"] for s in servers}
    assert names == {"filesystem", "sqlite"}
    fs = next(s for s in servers if s["name"] == "filesystem")
    assert fs["command"] == "npx"
    assert fs["args"] == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    assert fs["env"] == {"KEY": "value"}


def test_parse_mcp_servers_empty():
    mcp_discovery = _module()
    assert mcp_discovery._parse_mcp_servers({}) == []
    assert mcp_discovery._parse_mcp_servers({"mcpServers": {}}) == []
    assert mcp_discovery._parse_mcp_servers({"mcpServers": []}) == []


def test_discover_from_path(tmp_path):
    mcp_discovery = _module()
    config_file = tmp_path / "claude_desktop_config.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
        }
    }), encoding="utf-8")

    result = mcp_discovery._discover_from_path(str(config_file), "claude_desktop")
    assert result["source"] == "claude_desktop"
    assert result["path"] == str(config_file)
    assert len(result["servers"]) == 1
    assert result["servers"][0]["name"] == "fetch"


def test_discover_from_missing_path():
    mcp_discovery = _module()
    assert mcp_discovery._discover_from_path("/nonexistent/mcp.json", "cursor") is None


def test_discover_local_mcp_configs(tmp_path, monkeypatch):
    mcp_discovery = _module()

    # 创建 Cursor 风格配置
    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir()
    cursor_path = cursor_dir / "mcp.json"
    cursor_path.write_text(json.dumps({
        "mcpServers": {
            "puppeteer": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-puppeteer"]},
        }
    }), encoding="utf-8")

    # 创建 Claude Desktop 风格配置
    claude_dir = tmp_path / "Claude"
    claude_dir.mkdir()
    claude_path = claude_dir / "claude_desktop_config.json"
    claude_path.write_text(json.dumps({
        "mcpServers": {
            "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
        }
    }), encoding="utf-8")

    # mock 候选路径，避免读取真实用户配置
    monkeypatch.setattr(
        mcp_discovery, "_config_paths",
        lambda: [("cursor", str(cursor_path)), ("claude_desktop", str(claude_path))],
    )

    results = mcp_discovery.discover_local_mcp_configs()
    assert len(results) == 2
    sources = {r["source"] for r in results}
    assert sources == {"cursor", "claude_desktop"}

    names = {s["name"] for r in results for s in r["servers"]}
    assert names == {"puppeteer", "github"}


class TestMcpDiscoveryApi:
    def test_api_discover_and_import(self, tmp_path):
        # 准备临时 Cursor 配置
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        cursor_path = cursor_dir / "mcp.json"
        cursor_path.write_text(json.dumps({
            "mcpServers": {
                "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
            }
        }), encoding="utf-8")

        # 启动后端，通过环境变量覆盖发现路径
        port, proc = _start_backend(
            tmp_path,
            discovery_paths=f"cursor:{cursor_path}",
        )
        try:
            # 调用发现端点
            status, body = _api(port, "/api/v1/mcp/discover")
            assert status == 200
            assert body["ok"] is True
            assert len(body["sources"]) == 1
            assert body["sources"][0]["source"] == "cursor"
            assert any(s["name"] == "fetch" for s in body["sources"][0]["servers"])

            # 导入发现的服务器
            server = next(s for s in body["sources"][0]["servers"] if s["name"] == "fetch")
            status, body = _api(
                port, "/api/v1/mcp/discover/import",
                method="POST", data={"source": "cursor", "server": server},
            )
            assert status == 200
            assert "server_id" in body

            # 确认已出现在列表
            status, body = _api(port, "/api/v1/mcp/servers")
            assert status == 200
            assert any(s["name"] == "[cursor] fetch" for s in body)
        finally:
            _kill(proc)

    def test_api_import_validation(self, tmp_path):
        port, proc = _start_backend(tmp_path)
        try:
            status, _ = _api(
                port, "/api/v1/mcp/discover/import",
                method="POST", data={"source": "cursor"},
            )
            assert status == 400
        finally:
            _kill(proc)
