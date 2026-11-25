"""tests/contract/test_phase25_media.py —— Phase 25 图片粘贴/通讯网关合约测试"""
import os
import sys
import pytest


# ---- Vision API image_url 格式 ----

def test_adapter_stream_has_attachments_support():
    """base.py stream() 包含 Phase 25 image_url 逻辑"""
    base_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "adapters", "base.py"),
        encoding="utf-8",
    ).read()
    assert "image_url" in base_code, "base.py stream() missing Vision API image_url support"
    assert "attachments" in base_code, "base.py stream() missing attachments processing"


# ---- WS attachments ----

def test_main_ws_has_attachments():
    ws_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "routes", "ws.py"),
        encoding="utf-8",
    ).read()
    assert '"attachments"' in ws_code or "'attachments'" in ws_code, "routes/ws.py submit_task missing attachments"
    assert "extra_context" in ws_code, "routes/ws.py missing extra_context for attachments"


# ---- start_task extra_context ----

def test_agent_service_has_extra_context():
    as_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "services", "agent", "agent_service.py"),
        encoding="utf-8",
    ).read()
    assert "extra_context" in as_code, "agent_service.py missing extra_context support"


# ---- 通讯网关 ----

def test_main_has_notify_endpoint():
    notify_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "routes", "notify.py"),
        encoding="utf-8",
    ).read()
    assert "/notify" in notify_code, "routes/notify.py missing notify endpoint"
    assert "openclaw" in notify_code.lower(), "notify endpoint should reference OpenClaw"


# ---- 前端 ----

def test_taskcenter_has_paste_handler():
    chat_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-frontend", "src", "components", "chat", "ChatView.tsx"),
        encoding="utf-8",
    ).read()
    assert "onPaste" in chat_code, "ChatView missing paste handler"
    assert "onDrop" in chat_code, "ChatView missing drop handler"
    assert "attachmentPreviews" in chat_code, "ChatView missing attachments state"


def test_use_websocket_has_attachments_param():
    ws_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-frontend", "src", "hooks", "useWebSocket.ts"),
        encoding="utf-8",
    ).read()
    assert "attachments" in ws_code, "useWebSocket missing attachments parameter"


# ---- 视觉链路: OpenClaw supports_image ----

def test_openclaw_has_supports_image():
    oc_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "adapters", "openclaw.py"),
        encoding="utf-8",
    ).read()
    assert "supports_image=True" in oc_code, "OpenClaw should have supports_image=True"


# ---- 回归: adapter.stream 向后兼容 ----

def test_base_stream_still_yields_dict():
    """base.py stream() 返回 AsyncIterator[dict]"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from adapters.base import OpenAiCompatibleAdapter
    import inspect
    sig = inspect.signature(OpenAiCompatibleAdapter.stream)
    assert "dict" in str(sig.return_annotation), "stream must return AsyncIterator[dict]"
