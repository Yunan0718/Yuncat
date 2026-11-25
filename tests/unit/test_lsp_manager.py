"""tests/unit/test_lsp_manager.py —— 语言服务器管理（B 方案）

覆盖点：
1. 解析启动命令的四条分支（缺 Node / 缺 npx / 覆盖路径不存在 / 正常 npx）；
2. LSP 分帧编解码的边界（正常、截断、超大帧头、非法帧头）；
3. 真实子进程跑一遍：握手拿 capabilities → 转发一条请求 → 收回响应 → 干净退出。

第 3 条用一个假语言服务器（就是本进程的 Python 解释器跑一段脚本），
不依赖 Node / pyright / 网络，CI 上也能稳定跑。
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from services import lsp_manager


FAKE_SERVER = """
import json, sys

def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\\r\\n", b"\\n"):
            break
        key, _, value = line.decode("latin-1").partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))

def write_message(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body)
    sys.stdout.buffer.flush()

while True:
    message = read_message()
    if message is None:
        break
    msg_id, method = message.get("id"), message.get("method")
    if msg_id is not None and method == "initialize":
        write_message({"jsonrpc": "2.0", "id": msg_id,
                       "result": {"capabilities": {"hoverProvider": True, "completionProvider": {}}}})
    elif msg_id is not None:
        write_message({"jsonrpc": "2.0", "id": msg_id, "result": {"echo": message.get("params")}})
    else:
        write_message({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                       "params": {"uri": "file:///demo.py", "diagnostics": []}})
"""


@pytest.fixture
def fake_server(tmp_path):
    script = tmp_path / "fake_lsp_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    return [sys.executable, str(script)]


# ── 1. 启动命令解析 ────────────────────────────────────────────

def test_missing_node_is_reported_with_code(monkeypatch):
    monkeypatch.setattr(lsp_manager.shutil, "which", lambda _name: None)
    monkeypatch.delenv(lsp_manager.ENV_PYRIGHT_LANGSERVER, raising=False)
    status = lsp_manager.language_status("python")
    assert status["available"] is False
    assert status["code"] == "node-missing"
    assert "Node" in status["reason"]


def test_missing_npx_is_reported(monkeypatch):
    monkeypatch.setattr(lsp_manager.shutil, "which", lambda name: "node" if name == "node" else None)
    monkeypatch.delenv(lsp_manager.ENV_PYRIGHT_LANGSERVER, raising=False)
    status = lsp_manager.language_status("python")
    assert status["available"] is False
    assert status["code"] == "npx-missing"


def test_env_override_must_exist(monkeypatch, tmp_path):
    monkeypatch.setattr(lsp_manager.shutil, "which", lambda name: "node" if name == "node" else None)
    monkeypatch.setenv(lsp_manager.ENV_PYRIGHT_LANGSERVER, str(tmp_path / "nope.js"))
    status = lsp_manager.language_status("python")
    assert status["available"] is False
    assert status["code"] == "override-missing"


def test_env_override_is_used_when_present(monkeypatch, tmp_path):
    script = tmp_path / "pyright-langserver.js"
    script.write_text("// stub", encoding="utf-8")
    monkeypatch.setattr(lsp_manager.shutil, "which", lambda name: "node" if name == "node" else None)
    monkeypatch.setenv(lsp_manager.ENV_PYRIGHT_LANGSERVER, str(script))
    command, reason, code = lsp_manager.resolve_python_server()
    assert command == ["node", str(script), "--stdio"]
    assert reason == "" and code == ""


def test_default_path_uses_npx_pyright(monkeypatch):
    monkeypatch.setattr(lsp_manager.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.delenv(lsp_manager.ENV_PYRIGHT_LANGSERVER, raising=False)
    command, reason, code = lsp_manager.resolve_python_server()
    assert command is not None and "--package" in command and "pyright" in command
    assert command[-1] == "--stdio"
    assert reason == "" and code == ""


def test_unsupported_language():
    status = lsp_manager.language_status("cobol")
    assert status["available"] is False
    assert status["code"] == "unsupported-language"


# ── 2. 分帧 ───────────────────────────────────────────────────

def test_encode_message_frames_body_length_correctly():
    raw = lsp_manager.encode_message({"jsonrpc": "2.0", "id": 1})
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"Content-Length: ")
    assert int(head.split(b":")[1]) == len(body)
    assert json.loads(body.decode("utf-8"))["id"] == 1


async def _reader_with(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def test_read_message_round_trip():
    payload = {"jsonrpc": "2.0", "method": "x", "params": {"n": 2}}
    reader = await _reader_with(lsp_manager.encode_message(payload) * 2)
    first = await lsp_manager._read_message(reader)
    second = await lsp_manager._read_message(reader)
    assert first == payload and second == payload


async def test_read_message_rejects_oversized_frame():
    raw = b"Content-Length: 999999999\r\n\r\n{}"
    reader = await _reader_with(raw)
    with pytest.raises(ValueError):
        await lsp_manager._read_message(reader)


async def test_read_message_rejects_bad_header():
    reader = await _reader_with(b"Content-Length: abc\r\n\r\n{}")
    with pytest.raises(ValueError):
        await lsp_manager._read_message(reader)


async def test_read_message_returns_none_on_eof():
    reader = await _reader_with(b"")
    assert await lsp_manager._read_message(reader) is None


# ── 3. 真跑一个语言服务器 ─────────────────────────────────────

async def test_session_handshake_relay_and_stop(fake_server, tmp_path):
    session = lsp_manager.LspSession("python", str(tmp_path), fake_server)
    capabilities = await session.start()
    assert capabilities.get("hoverProvider") is True
    assert session.alive is True

    received: list[dict] = []

    async def collect(message: dict) -> None:
        received.append(message)

    session.add_listener(collect)
    # 透传一条请求：id 不在握手 pending 里，响应应当走监听器回到调用方
    assert await session.send({"jsonrpc": "2.0", "id": 7, "method": "echo", "params": {"a": 1}}) is True
    for _ in range(200):
        if any(message.get("id") == 7 for message in received):
            break
        await asyncio.sleep(0.02)
    echoed = [message for message in received if message.get("id") == 7]
    assert echoed, f"没有收到 id=7 的响应：{received}"
    assert echoed[0]["result"]["echo"] == {"a": 1}
    # 通知（无 id）同样要透传——界面上的诊断气泡就是靠这条回来的
    assert any(message.get("method") == "textDocument/publishDiagnostics" for message in received)

    session.remove_listener(collect)
    await session.stop()
    assert session.alive is False


async def test_session_raises_when_server_exits_immediately(tmp_path):
    # 直接退出的进程：握手必须失败并给出原因，而不是一直等
    session = lsp_manager.LspSession("python", str(tmp_path), [sys.executable, "-c", "raise SystemExit(3)"])
    with pytest.raises(RuntimeError):
        await session.start()
