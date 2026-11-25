"""routes/mcp.py —— MCP 运行时工具接口（Phase 51-D）"""
from fastapi import APIRouter, HTTPException, Request

from repositories import get_mcp_server
from services.mcp_client import McpProtocolError, StdioTransport

router = APIRouter(prefix="/api/v1", tags=["mcp"])


@router.get("/mcp/servers/{server_id}/tools")
async def list_mcp_server_tools(server_id: str, request: Request):
    """连接到指定 MCP 服务器并列出其提供的工具。"""
    server = await get_mcp_server(request.app.state.db, server_id)
    if not server:
        raise HTTPException(404, "MCP server not found")
    if not server.get("enabled"):
        raise HTTPException(400, "MCP server disabled")

    command = [server["command"], *server.get("args", [])]
    transport = StdioTransport(command=command, env=server.get("env"))
    try:
        await transport.start()
        tools = await transport.list_tools()
    except McpProtocolError as e:
        # 上游 MCP 服务器协议/通信失败——502（上游返回了不可用的响应）
        raise HTTPException(502, f"MCP 服务器通信失败：{e}")
    except FileNotFoundError as e:
        # 命令不存在 = 本地配置问题，让用户改配置而不是当成服务端故障
        raise HTTPException(400, f"MCP 服务器命令不存在：{command[0]}")
    except OSError as e:
        raise HTTPException(400, f"MCP 服务器无法启动：{e}")
    except Exception as e:
        raise HTTPException(500, f"MCP 服务器连接失败：{e}")
    finally:
        await transport.stop()

    return {
        "ok": True,
        "tools": [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ],
    }
