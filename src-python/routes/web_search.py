"""routes/web_search.py —— Task 6.3: @web: 引用支持

GET /api/v1/web/search?q={query} —— 委托 OC web_search 工具，不可用时回退搜索 URL hint

对标 Cursor @web 引用——让用户在聊天框用 @web:keyword 注入 web 搜索结果到上下文。

设计:
- 遵循三端分工铁律：Yuncat 不调 LLM。web search 委托 OC 的 web_search 工具
- OC 不可用时返回搜索 URL hint（Google/Bing/DuckDuckGo），让 Agent 用 web_search 工具自行搜索
- 不抓取 HTML（避免脆弱性），不调外部搜索 API（避免 key 管理）
"""
import logging
import urllib.parse

from fastapi import APIRouter, HTTPException, Request

from config import ERROR_PREVIEW_CHARS, OPENCLAW_AGENT_ID

_log = logging.getLogger("yuncat.web_search")

router = APIRouter(prefix="/api/v1", tags=["web_search"])


@router.get("/web/search")
async def web_search(q: str, request: Request) -> dict:
    """Task 6.3: @web: 引用——委托 OC web_search，不可用时回退搜索 URL hint。

    流程:
    1. 委托 OC 的 web_search 工具（通过 capability_router.delegate_tool）
    2. OC 不可用或调用失败 → 降级返回搜索 URL hint（不调 LLM，不抓取 HTML）

    Returns:
        成功: {ok: True, query, results, source: "openclaw"}
        降级: {ok: True, query, results: [], source: "url_hint", url_hint: {...}, hint: "..."}
        失败: {ok: False, error, results: []}
    """
    if not q.strip():
        return {"ok": False, "error": "Query is empty", "results": []}

    # 1. 委托 OC web_search
    try:
        from adapters.registry import get_global_adapters
        from context import get_current_agent_id
        from services.capability_router import delegate_tool

        adapters = get_global_adapters()
        if adapters:
            oc_adapter = adapters.get(OPENCLAW_AGENT_ID)
            if oc_adapter and hasattr(oc_adapter, "execute_tool"):
                from_agent_id = get_current_agent_id() or "unknown"
                result = await delegate_tool(
                    from_agent_id=from_agent_id,
                    to_agent_id="openclaw",
                    tool_name="web_search",
                    arguments={"query": q, "num_results": 5},
                    adapters=adapters,
                )
                if result.get("ok") or result.get("success"):
                    return {
                        "ok": True,
                        "query": q,
                        "results": result.get("result", result.get("results", [])),
                        "source": "openclaw",
                    }
                # OC 调用失败 → 降级到 URL hint
                _log.warning("OC web_search failed: %s",
                             str(result.get("error", ""))[:ERROR_PREVIEW_CHARS])

    except Exception as e:
        _log.warning("web_search delegate failed: %s", e)

    # 2. 降级——返回搜索 URL hint（不调 LLM，不抓取 HTML）
    encoded = urllib.parse.quote(q)
    return {
        "ok": True,
        "query": q,
        "results": [],
        "source": "url_hint",
        "url_hint": {
            "google": f"https://www.google.com/search?q={encoded}",
            "bing": f"https://www.bing.com/search?q={encoded}",
            "duckduckgo": f"https://duckduckgo.com/?q={encoded}",
        },
        "hint": "OpenClaw web_search unavailable. Use the URL above to search manually, "
                "or call the web_search tool directly.",
    }
