"""services/codegraph_client.py —— CodeGraph MCP Client 包装（Phase 20）

CodeGraph 是 npm 39K★ 的代码知识图谱工具，通过 MCP 协议暴露 10 个工具。
此模块封装 codegraph_* 工具调用——替代自研 SymbolIndexService，省 3 天开发。

前置: 依赖系统 PATH 中的 Node.js（npx / node），Yuncat 不再内置 node-embed。
"""
import logging
import os

from services.mcp_client import StdioTransport, McpTool

_log = logging.getLogger("yuncat.codegraph")


class CodeGraphClient:
    """CodeGraph MCP Client——封装代码智能查询。

    用法:
        client = CodeGraphClient(workspace_path="/path/to/project")
        await client.start()
        symbols = await client.context("find auth functions")
        await client.stop()
    """

    def __init__(self, workspace_path: str | None = None):
        self._ws = workspace_path or os.getcwd()
        self._transport: StdioTransport | None = None
        self._tools: dict[str, McpTool] = {}

    async def start(self) -> None:
        """启动 CodeGraph MCP server"""
        from utils.runtime_paths import get_node_exe
        node_exe = get_node_exe()

        self._transport = StdioTransport(
            command=[node_exe, "-e",
                     "import('@colbymchenry/codegraph').then(m=>m.startServer())"],
            cwd=self._ws,
        )
        await self._transport.start()

        # 发现工具
        tools = await self._transport.list_tools()
        self._tools = {t.name: t for t in tools}
        _log.info("CodeGraph ready: %d tools available (%s)", len(tools), ", ".join(self._tools.keys()))

    async def stop(self) -> None:
        if self._transport:
            await self._transport.stop()

    @property
    def tools(self) -> dict[str, McpTool]:
        return self._tools

    # ---- 便捷方法 ----

    async def context(self, query: str, max_symbols: int = 20) -> list[dict]:
        """查询代码上下文——返回相关符号列表"""
        return await self._call("codegraph_context", {"query": query, "max_symbols": max_symbols})

    async def trace(self, from_sym: str, to_sym: str) -> list[dict]:
        """追踪两个符号之间的调用路径"""
        return await self._call("codegraph_trace", {"from_sym": from_sym, "to_sym": to_sym})

    async def impact(self, symbol: str, radius: int = 2) -> list[dict]:
        """分析修改指定符号的影响范围"""
        return await self._call("codegraph_impact", {"symbol": symbol, "radius": radius})

    async def search(self, name: str, kind: str = "any") -> list[dict]:
        """按名称搜索符号"""
        return await self._call("codegraph_search", {"name": name, "kind": kind})

    async def callers(self, symbol: str, limit: int = 10) -> list[dict]:
        """查找调用者"""
        return await self._call("codegraph_callers", {"symbol": symbol, "limit": limit})

    async def callees(self, symbol: str, limit: int = 10) -> list[dict]:
        """查找被调用者"""
        return await self._call("codegraph_callees", {"symbol": symbol, "limit": limit})

    async def status(self) -> dict:
        """获取索引状态"""
        return await self._call("codegraph_status", {})

    async def _call(self, tool_name: str, args: dict) -> dict | list:
        if not self._transport or not self._transport.is_alive:
            _log.warning("CodeGraph not connected, returning empty")
            return {} if tool_name == "codegraph_status" else []
        result = await self._transport.call_tool(tool_name, args)
        # MCP tools/call 返回 content 数组
        if isinstance(result, dict) and "content" in result:
            for item in result["content"]:
                if item.get("type") == "text":
                    import json as _json
                    try:
                        return _json.loads(item["text"])
                    except (_json.JSONDecodeError, TypeError):
                        return item["text"]
        return result
