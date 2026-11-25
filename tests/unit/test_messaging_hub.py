"""tests/unit/test_messaging_hub.py —— Phase 51-F: MessagingHub"""
import pytest


class FakeToolService:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.last_tool = None
        self.last_args = None
        self.last_agent_id = None

    async def execute(self, tool_name: str, arguments: dict, agent_id: str | None = None, **kwargs):
        self.last_tool = tool_name
        self.last_args = arguments
        self.last_agent_id = agent_id
        return {"ok": self.ok}


@pytest.mark.asyncio
async def test_send_message_via_tool_service():
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.messaging_hub import send_message

    ts = FakeToolService()
    result = await send_message("telegram", "hello", tool_service=ts)
    assert result["ok"] is True
    assert result["platform"] == "telegram"
    assert ts.last_tool == "openclaw_notify"
    assert ts.last_args["platform"] == "telegram"
    assert ts.last_agent_id == "openclaw"


@pytest.mark.asyncio
async def test_send_message_missing_message():
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.messaging_hub import send_message

    result = await send_message("telegram", "")
    assert result["ok"] is False
    assert "message is required" in result["error"]


@pytest.mark.asyncio
async def test_list_supported_platforms():
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.messaging_hub import list_supported_platforms

    platforms = await list_supported_platforms()
    assert "telegram" in platforms
    assert "discord" in platforms
