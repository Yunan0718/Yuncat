"""services/mcp_client.py —— MCP Client 基础设施（Phase 20）

Model Context Protocol (MCP) 通过 stdio JSON-RPC 2.0 与外部工具服务器通信。
为 Phase 24 的 CodeGraph/context7/sequential-thinking 集成提供传输层。

协议层:
  - stdio 子进程管理
  - JSON-RPC 2.0 消息帧（Content-Length header + JSON body）
  - initialize → initialized 握手
  - tools/list + tools/call
"""
import asyncio
import json as _json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from config import (
    APP_VERSION,
    MCP_PROCESS_CLOSE_TIMEOUT_SEC,
    MCP_REQUEST_TIMEOUT_SEC,
)

_log = logging.getLogger("yuncat.mcp")


@dataclass
class McpTool:
    """MCP 工具描述符"""
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)
    # Task 7.9: MCP 2025-06-18 规范——工具 annotations（含 readOnlyHint 等安全提示）
    annotations: dict = field(default_factory=dict)


class McpProtocolError(RuntimeError):
    """MCP 服务器返回 JSON-RPC 错误、连接中断或请求超时。

    为什么要有这个异常（AGENTS.md 规则 9）：
    旧实现把协议错误折进返回值——`{"error": "..."}`——调用方很容易把它当成
    「正常响应但内容为空」。表现出来就是：MCP 服务器其实挂了/超时了，工具列表
    却是空的，UI 显示「该服务器没有提供工具」，真因被静默吞掉。
    协议层错误必须以异常冒泡，调用方要么显式处理、要么显式记日志。
    """


class StdioTransport:
    """MCP stdio 传输层——管理子进程 + JSON-RPC 消息帧。

    消息格式:
      Content-Length: <N>\r\n\r\n<JSON body>
    """

    def __init__(self, command: list[str], env: dict | None = None, cwd: str | None = None):
        self._command = command
        # 子进程环境 = 继承当前环境 + 用户配置的覆盖项。
        # 旧实现直接用配置里的 env 整体替换环境变量：只要用户在 MCP 配置里写了
        # 任何一个变量，PATH / SystemRoot 就没了——Windows 上 npx/node 找不到、
        # 依赖 SystemRoot 的运行时直接起不来，表现为「服务器莫名其妙启动失败」。
        # 主流做法（VS Code / Claude Desktop / Docker MCP）都是"继承 + 覆盖"。
        self._env = {**os.environ, **(env or {})}
        self._cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._buffer = b""

    async def start(self) -> None:
        """启动子进程并完成 MCP 握手"""
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            cwd=self._cwd,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        # MCP 握手: initialize → initialized
        result = await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "yuncat", "version": APP_VERSION},
        })
        if "error" in result:
            raise McpProtocolError(f"MCP initialize failed: {result['error']}")
        server_info = result.get("result", {})
        _log.info("MCP server connected: %s (v%s)", server_info.get("serverInfo", {}).get("name", "unknown"), server_info.get("protocolVersion", "unknown"))
        # Send initialized notification (no response expected)
        await self._send_notification("notifications/initialized", {})

    async def stop(self) -> None:
        """关闭子进程"""
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._proc and self._proc.returncode is None:
            self._proc.stdin.close()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=MCP_PROCESS_CLOSE_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                self._proc.kill()

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def list_tools(self) -> list[McpTool]:
        """获取服务器提供的工具列表。

        协议错误（服务器返回 error / 超时 / 连接断开）必须抛 McpProtocolError——
        旧实现把它折成空列表，调用方会误以为「这个服务器没有工具」。
        """
        result = await self._request("tools/list", {})
        if "error" in result:
            raise McpProtocolError(f"tools/list failed: {result['error']}")
        tools_data = result.get("result", {}).get("tools", [])
        return [
            McpTool(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
                # Task 7.9: 提取 MCP 规范 annotations（含 readOnlyHint）
                annotations=t.get("annotations") or {},
            )
            for t in tools_data
        ]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """调用指定工具。

        协议错误抛 McpProtocolError——旧实现把 error 原样返回，调用方
        （mcp_bridge.call_mcp_tool）会当成 success=True + 一坨错误负载，
        写操作失败被当成成功，是比报错更糟的结果。
        """
        result = await self._request("tools/call", {
            "name": name,
            "arguments": arguments,
        })
        if "error" in result:
            raise McpProtocolError(f"tools/call {name} failed: {result['error']}")
        if "result" not in result:
            raise McpProtocolError(f"tools/call {name} returned malformed response: {result}")
        return result["result"]

    # ---- 内部实现 ----

    def _next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _request(self, method: str, params: dict) -> dict:
        """发送 JSON-RPC 请求并等待响应"""
        req_id = self._next_request_id()
        # Phase 48 T3-16: asyncio.get_event_loop() Python 3.12 弃用 → get_running_loop()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        payload = _json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        })
        await self._send_raw(payload)
        try:
            return await asyncio.wait_for(future, timeout=MCP_REQUEST_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise McpProtocolError(f"MCP request timeout: {method}") from None

    async def _send_notification(self, method: str, params: dict) -> None:
        """发送 JSON-RPC 通知（无 id，不期待响应）"""
        payload = _json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        })
        await self._send_raw(payload)

    async def _send_raw(self, payload: str) -> None:
        """发送原始消息——Content-Length header + JSON body"""
        if not self._proc or not self._proc.stdin:
            return
        body = payload.encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        try:
            self._proc.stdin.write(header + body)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            _log.warning("MCP stdin closed")

    async def _read_loop(self) -> None:
        """持续读取子进程 stdout，解析 Content-Length 帧"""
        try:
            while self._proc and self._proc.stdout:
                chunk = await self._proc.stdout.read(4096)
                if not chunk:
                    break
                self._buffer += chunk
                await self._parse_messages()
        except asyncio.CancelledError:
            pass
        except Exception:
            _log.exception("MCP read loop error")
        finally:
            # 清理所有 pending futures
            for req_id, future in self._pending.items():
                if not future.done():
                    future.set_result({"error": "MCP connection closed"})
            self._pending.clear()

    async def _parse_messages(self) -> None:
        """从缓冲区解析 JSON-RPC 消息"""
        while b"\r\n\r\n" in self._buffer:
            header_end = self._buffer.index(b"\r\n\r\n")
            header = self._buffer[:header_end].decode("utf-8", errors="replace")
            # 解析 Content-Length
            length = 0
            for line in header.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    try:
                        length = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
            if length <= 0:
                self._buffer = self._buffer[header_end + 4:]
                continue

            body_start = header_end + 4
            if len(self._buffer) < body_start + length:
                break  # 等更多数据

            body = self._buffer[body_start:body_start + length]
            self._buffer = self._buffer[body_start + length:]

            try:
                msg = _json.loads(body.decode("utf-8"))
            except _json.JSONDecodeError:
                continue

            req_id = msg.get("id")
            if req_id is not None:
                future = self._pending.pop(req_id, None)
                if future and not future.done():
                    future.set_result(msg)
            # else: 服务器推送通知——忽略


async def register_mcp_tools(server_id: str, transport: StdioTransport, tool_service) -> list[str]:
    """将 MCP 服务器工具注册到 Yuncat 工具注册表（前缀 mcp_{server_id}_）。"""
    from tools import registry as tool_registry

    tools = await transport.list_tools()
    registered: list[str] = []
    for mt in tools:
        prefixed = f"mcp_{server_id}_{mt.name}"

        async def _handler(tool_name: str = mt.name, **kwargs):
            return await transport.call_tool(tool_name, kwargs)

        # Task 7.9: 根据 MCP annotations.readOnlyHint 设置 is_read_only + confirmable
        is_read_only = bool(mt.annotations.get("readOnlyHint", False))
        tool_registry.tool(
            name=prefixed,
            agents=["hermes"],
            description=f"[MCP {server_id}] {mt.description}"[:280],
            parameters=mt.input_schema or {"type": "object", "properties": {}},
            confirmable=not is_read_only,
            is_read_only=is_read_only,
            is_concurrency_safe=False,
            groups=["mcp", server_id],
        )(_handler)
        registered.append(prefixed)
    _log.info("Registered %d MCP tools for server %s", len(registered), server_id)
    return registered
