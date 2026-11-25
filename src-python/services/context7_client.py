"""services/context7_client.py —— Context7 MCP Client 包装（Phase 24）

Context7 提供实时最新文档查询，通过 MCP 协议暴露 resolve-library-id 和 query-docs。
此模块封装为 Agent 可调用的 docs_lookup 工具——避免 Agent 用训练数据猜过期 API。

前置: 依赖系统 PATH 中的 Node.js（npx / node），Yuncat 不再内置 node-embed。
"""
import logging
import os

from services.mcp_client import StdioTransport

_log = logging.getLogger("yuncat.context7")


class Context7Client:
    """Context7 MCP Client——实时文档查询。

    用法:
        client = Context7Client()
        await client.start()
        docs = await client.lookup("fastapi", "lifespan async")
        await client.stop()
    """

    def __init__(self):
        self._transport: StdioTransport | None = None
        self._started = False

    async def _ensure_started(self):
        if self._started:
            return
        from utils.runtime_paths import get_node_exe
        node_exe = get_node_exe()

        self._transport = StdioTransport(
            command=[node_exe, "-e",
                     "import('@upstash/context7').then(m=>m.startServer())"],
        )
        await self._transport.start()
        self._started = True
        _log.info("Context7 MCP server connected")

    async def stop(self):
        if self._transport:
            await self._transport.stop()
            self._started = False

    async def lookup(self, library: str, query: str) -> dict:
        """查询指定库的最新文档。

        需要: npx @upstash/context7（首次使用自动下载，需联网）。
        离线或 npm 不可用时返回错误提示。
        """
        await self._ensure_started()
        if not self._transport or not self._transport.is_alive:
            return {
                "error": "Context7 not connected. Install with: npm install @upstash/context7",
                "results": [],
            }

        try:
            # Step 1: resolve library ID
            resolved = await self._transport.call_tool("resolve-library-id", {"library": library})
            lib_id = self._extract_text(resolved)

            if not lib_id:
                return {"error": f"Library '{library}' not found in Context7", "results": []}

            # Step 2: query docs
            docs = await self._transport.call_tool("query-docs", {
                "libraryId": lib_id,
                "query": query,
            })
            content = self._extract_text(docs)

            return {
                "library": library,
                "library_id": lib_id,
                "results": [{"content": content}] if content else [],
                "query": query,
            }
        except Exception as e:
            _log.warning("Context7 lookup failed for %s: %s", library, e)
            return {"error": str(e), "results": []}

    @staticmethod
    def _extract_text(result: dict) -> str:
        """从 MCP tools/call 响应中提取文本内容"""
        if isinstance(result, dict) and "content" in result:
            for item in result["content"]:
                if item.get("type") == "text":
                    import json as _json
                    text = item["text"]
                    try:
                        data = _json.loads(text)
                        return data.get("text", text) if isinstance(data, dict) else text
                    except (_json.JSONDecodeError, TypeError):
                        return text
        return str(result) if result else ""


# 全局单例
_context7: Context7Client | None = None


async def get_context7() -> Context7Client:
    global _context7
    if _context7 is None:
        _context7 = Context7Client()
    return _context7
