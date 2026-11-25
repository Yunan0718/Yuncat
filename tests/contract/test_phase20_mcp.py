"""tests/contract/test_phase20_mcp.py —— Phase 20 MCP Client 合约测试"""
import os
import sys
import json
import pytest


def test_mcp_message_framing():
    """验证 MCP JSON-RPC 消息帧格式匹配"""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    body = payload.encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    framed = header + body

    # 验证帧格式
    assert b"Content-Length:" in framed
    assert b"\r\n\r\n" in framed

    # 解析验证
    header_end = framed.index(b"\r\n\r\n")
    header_str = framed[:header_end].decode("utf-8")
    length = int(header_str.split(":")[1].strip())
    assert length == len(body)


def test_mcp_request_id_increment():
    """验证请求 ID 自增"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.mcp_client import StdioTransport

    t = StdioTransport(command=["echo", "test"])
    id1 = t._next_request_id()
    id2 = t._next_request_id()
    assert id2 > id1


def test_mcp_tool_descriptor():
    """验证 McpTool dataclass"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.mcp_client import McpTool

    tool = McpTool(
        name="codegraph_context",
        description="Query code context",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    assert tool.name == "codegraph_context"
    assert "query" in tool.input_schema.get("properties", {})


@pytest.mark.asyncio
async def test_mcp_jsonrpc_message_parsing():
    """验证 JSON-RPC 响应解析"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.mcp_client import StdioTransport
    import asyncio

    # 模拟包含 Content-Length 帧的缓冲区
    response = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"tools": [
            {"name": "test_tool", "description": "A test tool", "inputSchema": {}}
        ]},
    })
    body = response.encode("utf-8")
    frame = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body

    t = StdioTransport(command=["echo", "test"])
    t._buffer = frame
    await t._parse_messages()

    # Buffer should be empty after parsing
    assert len(t._buffer) == 0


def test_codegraph_client_initialization():
    """验证 CodeGraphClient 基础初始化"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.codegraph_client import CodeGraphClient

    client = CodeGraphClient(workspace_path="/tmp/test")
    assert client._ws == "/tmp/test"
    assert client._transport is None
    assert client._tools == {}
