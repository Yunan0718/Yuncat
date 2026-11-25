"""services/mcp_bridge.py —— Phase U.2+U.3: MCP 工具桥接与注册

职责:
- 扫描 DB 中 enabled=true 的 MCP 服务器
- 通过 StdioTransport 启动并列举工具
- 将 MCP 工具注册到 tools/registry.py（Hermes + OpenClaw 统一可见）
- 执行阶段按工具名路由到对应 MCP 服务器
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from config import ADAPTER_TOOL_DESC_MAX_CHARS, DEFAULT_AGENT_ID, OPENCLAW_AGENT_ID

_log = logging.getLogger("yuncat.mcp.bridge")

# 已启动的 MCP 传输层：server_id -> StdioTransport
_transports: dict[str, Any] = {}
# 已注册到 registry 的 MCP 工具：tool_name -> (server_id, original_name)
_mcp_tool_registry: dict[str, tuple[str, str]] = {}
# lifespan 启动时缓存的服务器配置（供无 DB 连接的场景复用）
_server_configs: list[dict] = []
# 健康状态：server_id -> {"healthy": bool, "last_check": float, "error": str|None}
_server_health: dict[str, dict] = {}


def set_server_configs(servers: list[dict]) -> None:
    """缓存 MCP 服务器配置，通常在 lifespan 启动时调用。"""
    global _server_configs
    _server_configs = list(servers)


def _make_handler(server_id: str, original_name: str):
    """构造转发到 MCP 服务器的工具执行函数。"""
    async def _handler(**arguments):
        return await call_mcp_tool(server_id, original_name, arguments)
    return _handler


def _tool_to_meta(server_id: str, tool: Any) -> dict:
    """将 MCP 工具描述转为 ToolMeta / payload 可用的 dict。

    Task 7.9: 从 MCP annotations.readOnlyHint 提取 is_read_only。
    无 annotations 或 readOnlyHint 缺失时默认 is_read_only=False（fail-safe）。

    支持两种输入形式：
      - McpTool 对象（StdioTransport.list_tools() 返回）
      - dict（原始 JSON-RPC 工具描述）
    """
    if isinstance(tool, dict):
        name = tool.get("name", "")
        description = tool.get("description", "") or ""
        input_schema = tool.get("inputSchema") or tool.get("input_schema") or {}
        annotations = tool.get("annotations") or {}
    else:
        name = getattr(tool, "name", "")
        description = getattr(tool, "description", "") or ""
        input_schema = getattr(tool, "input_schema", None) or {}
        annotations = getattr(tool, "annotations", None) or {}
    is_read_only = bool(annotations.get("readOnlyHint", False))
    return {
        "server_id": server_id,
        "original_name": name,
        "name": name,
        "description": description[:ADAPTER_TOOL_DESC_MAX_CHARS],
        "parameters": input_schema,
        # Task 7.9: 供 _register_tool 据此设置 confirmable
        "is_read_only": is_read_only,
    }


async def _get_transport(server_id: str, command: list[str], args: list[str], env: dict) -> Any:
    """获取或启动指定 MCP 服务器的 StdioTransport。"""
    from services.mcp_client import StdioTransport
    transport = _transports.get(server_id)
    if transport is not None and transport.is_alive:
        return transport
    transport = StdioTransport(command=command + args, env=env or {})
    await transport.start()
    _transports[server_id] = transport
    return transport


async def discover_tools(conn=None) -> list[dict]:
    """发现所有 enabled MCP 服务器的工具，并注册到 tools/registry。

    参数:
        conn: 可选的数据库连接；传入时从 DB 拉取服务器列表，否则使用已缓存配置。
    返回:
        可用于 Agent payload 的工具描述列表（含 server_id/original_name 元信息）。
    """
    global _server_configs
    if conn is not None:
        from repositories import list_mcp_servers as _list_mcp
        servers = await _list_mcp(conn)
        set_server_configs(servers)
    else:
        servers = list(_server_configs)

    discovered: list[dict] = []
    for srv in servers:
        if not srv.get("enabled"):
            continue
        sid = srv["id"]
        command = [srv["command"]] if isinstance(srv["command"], str) else list(srv["command"] or [])
        args = list(srv.get("args") or [])
        env = srv.get("env") or {}
        try:
            transport = await _get_transport(sid, command, args, env)
            tools = await transport.list_tools()
            for t in tools:
                meta = _tool_to_meta(sid, t)
                discovered.append(meta)
                _register_tool(meta)
        except Exception as e:
            # 降级（best-effort）：单个 MCP 服务器不可用不阻断其它服务器的工具注册，
            # 但必须留痕——写健康状态 + warning 日志，不能静默跳过（AGENTS.md 规则 9）。
            _log.warning("MCP server %s discover failed: %s", sid, e)
            _server_health[sid] = {
                "healthy": False,
                "last_check": time.time(),
                "error": str(e),
            }
    return discovered


def _register_tool(meta: dict) -> None:
    """将单个 MCP 工具注册到 tools/registry.py（幂等）。

    Phase 47 4.2: 改用公开 API register_tool() 替代直接访问 _tools 私有字典。
    Task 7.9: 根据 is_read_only 设置 confirmable——只读工具无需确认，
              写操作需用户确认（人在循环，对标 MCP 规范 + Claude Code）。
    """
    from tools.registry import register_tool, ToolMeta, get_tool
    name = meta["name"]
    if name in _mcp_tool_registry:
        return
    # 命名冲突时跳过（不覆盖原生工具）
    if get_tool(name) is not None:
        _log.warning("MCP tool name collision skipped: %s", name)
        return
    _mcp_tool_registry[name] = (meta["server_id"], meta["original_name"])
    # Task 7.9: 从 meta 读取 is_read_only（_tool_to_meta 从 annotations.readOnlyHint 提取）
    is_read_only = bool(meta.get("is_read_only", False))
    tool_meta = ToolMeta(
        name=name,
        func=_make_handler(meta["server_id"], meta["original_name"]),
        agents=[DEFAULT_AGENT_ID, OPENCLAW_AGENT_ID],
        description=meta["description"],
        parameters=meta["parameters"],
        confirmable=not is_read_only,  # Task 7.9: 只读→不确认，写操作→确认
        is_read_only=is_read_only,
        is_concurrency_safe=False,
        concurrency_safety="exclusive",
        groups=["mcp"],
    )
    try:
        register_tool(tool_meta)
    except ValueError:
        # 并发注册时可能已存在——幂等跳过
        _log.debug("MCP tool %s already registered, skipping", name)
        return
    _log.debug("Registered MCP tool: %s (server=%s, read_only=%s)",
               name, meta["server_id"], is_read_only)


async def call_mcp_tool(server_id: str, tool_name: str, arguments: dict) -> dict:
    """调用指定 MCP 服务器上的工具。"""
    transport = _transports.get(server_id)
    if transport is None or not transport.is_alive:
        return {"success": False, "error": f"MCP server {server_id} not connected"}
    try:
        result = await transport.call_tool(tool_name, arguments or {})
        return {"success": True, "result": result}
    except Exception as e:
        _log.debug("MCP tool call failed: %s/%s", server_id, tool_name, exc_info=True)
        return {"success": False, "error": str(e)}


async def call_tool_by_name(tool_name: str, arguments: dict) -> dict:
    """按已注册工具名执行 MCP 调用。"""
    mapping = _mcp_tool_registry.get(tool_name)
    if not mapping:
        return {"success": False, "error": f"MCP tool {tool_name} not registered"}
    server_id, original_name = mapping
    return await call_mcp_tool(server_id, original_name, arguments)


async def start_server(server_id: str, command: str, args: list[str], env: dict | None = None) -> bool:
    """显式启动单个 MCP 服务器（供 lifespan / 手动 API 使用）。"""
    try:
        cmd = [command] if isinstance(command, str) else list(command or [])
        await _get_transport(server_id, cmd, list(args or []), env or {})
        return True
    except Exception:
        _log.debug("MCP server start failed: %s", server_id, exc_info=True)
        return False


async def stop_server(server_id: str) -> None:
    """停止单个 MCP 服务器。"""
    transport = _transports.pop(server_id, None)
    if transport:
        try:
            await transport.stop()
        except Exception:
            _log.debug("MCP server stop failed: %s", server_id, exc_info=True)


async def stop_all_servers() -> None:
    """停止所有已启动的 MCP 服务器。"""
    await asyncio.gather(*[stop_server(sid) for sid in list(_transports.keys())], return_exceptions=True)


# ============================================================
# Phase U.2-A: MCP 服务器心跳 / 健康监控
# ============================================================

async def health_check(server_id: str) -> dict:
    """对指定 MCP 服务器执行一次健康探测。

    探测方式：调用轻量的 tools/list；成功则标记 healthy=True。
    失败时关闭旧 transport，允许下次调用时自动重建。
    """
    import time as _time

    transport = _transports.get(server_id)
    if transport is None or not transport.is_alive:
        _server_health[server_id] = {
            "healthy": False,
            "last_check": _time.time(),
            "error": "transport not connected",
        }
        return _server_health[server_id]

    try:
        await transport.list_tools()
        _server_health[server_id] = {
            "healthy": True,
            "last_check": _time.time(),
            "error": None,
        }
        return _server_health[server_id]
    except Exception as e:
        _log.warning("MCP health check failed for %s: %s", server_id, e)
        # 关闭失效 transport，下次 discover/call 会重建
        await stop_server(server_id)
        _server_health[server_id] = {
            "healthy": False,
            "last_check": _time.time(),
            "error": str(e),
        }
        return _server_health[server_id]


async def health_check_all() -> dict[str, dict]:
    """对所有已启动/配置的 MCP 服务器执行健康探测。"""
    checked: dict[str, dict] = {}
    # 已缓存配置中的 enabled 服务器都要检查（即使当前未连接）
    configured = {srv["id"] for srv in _server_configs if srv.get("enabled")}
    for sid in configured:
        checked[sid] = await health_check(sid)
    # 清理已不在配置中的健康记录
    for sid in list(_server_health.keys()):
        if sid not in configured:
            _server_health.pop(sid, None)
    return checked


def get_health_status() -> dict[str, dict]:
    """返回最近一次健康状态快照（不触发探测）。"""
    return dict(_server_health)


async def register_mcp_tools(conn) -> list[dict]:
    """lifespan 入口：从 DB 加载并注册全部 enabled MCP 工具。"""
    return await discover_tools(conn)


class MCPBridge:
    """Phase U.2+U.3: MCP 工具桥接入口（兼容 FUSION.md 设计）。

    所有方法均为薄包装，实际逻辑见模块级函数。
    """

    @staticmethod
    async def discover_tools(conn=None) -> list[dict]:
        return await discover_tools(conn)

    @staticmethod
    async def call_tool_by_name(tool_name: str, arguments: dict) -> dict:
        return await call_tool_by_name(tool_name, arguments)

    @staticmethod
    async def start_server(server_id: str, command: str, args: list[str], env: dict | None = None) -> bool:
        return await start_server(server_id, command, args, env)

    @staticmethod
    async def stop_server(server_id: str) -> None:
        await stop_server(server_id)

    @staticmethod
    async def stop_all_servers() -> None:
        await stop_all_servers()

    @staticmethod
    async def health_check(server_id: str) -> dict:
        return await health_check(server_id)

    @staticmethod
    async def health_check_all() -> dict[str, dict]:
        return await health_check_all()

    @staticmethod
    def get_health_status() -> dict[str, dict]:
        return get_health_status()
