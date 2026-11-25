"""system_tool_context.py —— 上下文/度量工具"""
import logging
from collections import OrderedDict

from ..registry import tool
from config import SYSTEM_TOOL_CONTEXT_PROBLEM_PREVIEW_CHARS, TOOL_RESULT_CACHE_MAX
from services.llm_usage import TOKEN_USAGE_EVENT_SQL

_log = logging.getLogger("yuncat.tools")


# ---- Phase 24: docs_lookup + sequential_thinking 工具 ----

@tool(
    name="docs_lookup", agents=["hermes", "openclaw"],
    description=(
        "Query the latest official documentation for a library. "
        "Use this instead of relying on training data when the answer depends on API details. "
        "Examples: docs_lookup(library='fastapi', query='lifespan async context manager')"
    ),
    parameters={
        "type": "object",
        "properties": {
            "library": {"type": "string", "description": "Library name (e.g. fastapi, sqlalchemy, react)"},
            "query": {"type": "string", "description": "What you want to know about this library"},
        },
        "required": ["library", "query"],
    },
    is_read_only=True,
    is_concurrency_safe=True,
    groups=["search"],
    tool_timeout=60,  # P2-2.4: 文档查询超时（Context7 API / web search）
)
async def docs_lookup(library: str, query: str) -> dict:
    """查询最新的库文档——Context7 API 优先，回退 web search。"""
    from tools.result import ToolResult
    try:
        from services.context7_client import get_context7
        c7 = await get_context7()
        result = await c7.lookup(library, query)
        if result.get("error"):
            return ToolResult(
                success=False, error=result["error"],
                meta={"tool_name": "docs_lookup"},
            ).to_dict()
        return ToolResult(
            success=True,
            data={"library": library, "query": query, "results": result.get("results", [])},
            observation=f"查询 {library} 文档：返回 {len(result.get('results', []))} 条结果",
            meta={"tool_name": "docs_lookup", "source": "context7"},
        ).to_dict()
    except Exception:
        # Fallback: 构造文档查询 URL，供 Agent 参考
        import urllib.parse
        encoded = urllib.parse.quote(f"{library} {query}")
        return ToolResult(
            success=True,
            data={
                "library": library, "query": query,
                "web_search_hint": f"https://www.google.com/search?q={encoded}+documentation",
                "direct_doc_url": f"https://docs.python.org/3/search.html?q={encoded}" if library == "python" else "",
            },
            observation=f"docs_lookup: Context7 不可用。建议用 web search 查询 '{library} {query}' 的官方文档。",
            meta={"tool_name": "docs_lookup", "fallback": "web_search"},
        ).to_dict()


@tool(
    name="sequential_thinking", agents=["hermes", "openclaw"],
    description=(
        "Break down a complex problem into sequential reasoning steps. "
        "Each step: thought (current reasoning), next_thought_needed (whether to continue), "
        "is_revision (correct previous steps if needed). "
        "Use this when the task requires multi-step analysis, debugging, or architectural decisions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "problem": {"type": "string", "description": "The complex problem to analyze"},
            "max_steps": {"type": "integer", "default": 20, "description": "Maximum reasoning steps"},  # Phase 48-H: 放宽上限对标主流
        },
        "required": ["problem"],
    },
    is_read_only=True,
    is_concurrency_safe=True,
    groups=["advanced"],
    tool_timeout=300,  # P2-2.4: 结构化推理超时（可能触发 LLM 调用）
)
async def sequential_thinking(problem: str, max_steps: int = 20) -> dict:  # Phase 48-H: 放宽上限对标主流
    """结构化推理——逐步分解复杂问题"""
    from tools.result import ToolResult
    # 这个工具的核心价值是引导 Agent 结构化思考——返回一个框架而非直接答案
    thinking_prompt = f"""## 结构化推理框架
请对以下问题逐步推理。每步输出包含：
- thought: 本步的推理内容
- next_thought_needed: 是否需要继续（true/false）
- is_revision: 是否修正了之前的错误假设

问题：{problem}

最多 {max_steps} 步。现在开始第 1 步。"""

    return ToolResult(
        success=True,
        data={
            "framework": "sequential_thinking",
            "problem": problem,
            "max_steps": max_steps,
            "prompt": thinking_prompt,
        },
        observation=f"开启 {max_steps} 步推理框架分析：{problem[:SYSTEM_TOOL_CONTEXT_PROBLEM_PREVIEW_CHARS]}",
        meta={"tool_name": "sequential_thinking"},
    ).to_dict()


# ═══════════════════════════════════════════════════════════
# Phase 43 P0-DYN: Agent 自主请求工具 — 被截断结果的恢复
# ═══════════════════════════════════════════════════════════

# 工具结果缓存 — key: "task_id:tool_name:index"
# OrderedDict 实现 true LRU——访问时 move_to_end，淘汰时 popitem(last=False)
_TOOL_RESULT_CACHE: OrderedDict[str, str] = OrderedDict()


def _cache_tool_result(task_id: str, tool_name: str, index: int, full_content: str):
    """缓存完整工具结果供 request_more_context 检索。

    内存 LRU 只覆盖最近 N 条；同时落盘一份（spill），让重启/LRU 淘汰后仍可取回。
    落盘失败不影响内存路径——spill 是增强，不是前置条件。
    """
    key = f"{task_id}:{tool_name}:{index}"
    _TOOL_RESULT_CACHE[key] = full_content
    _TOOL_RESULT_CACHE.move_to_end(key)
    while len(_TOOL_RESULT_CACHE) > TOOL_RESULT_CACHE_MAX:
        _TOOL_RESULT_CACHE.popitem(last=False)
    try:
        from services.tool_spill import save_spill
        save_spill(task_id, tool_name, index, full_content)
    except Exception:
        _log.debug("save spill failed (non-critical)", exc_info=True)


@tool(
    name="request_more_context", agents=["hermes", "openclaw"],
    description=(
        "请求更多上下文 — 当之前被截断的工具结果需要完整内容时调用。"
        "参数: tool_name=被截断的工具名, result_index=第几个结果(默认0)"
    ),
    parameters={
        "type": "object",
        "properties": {
            "tool_name": {"type": "string",
                          "description": "被截断的工具名 (如 read_file/write_file/run_command)"},
            "result_index": {"type": "integer",
                             "description": "被截断的工具结果索引 (默认0=第一个)", "default": 0},
        },
        "required": ["tool_name"],
    },
    is_read_only=True, is_concurrency_safe=True,
    groups=["context"],
)
async def request_more_context(tool_name: str, result_index: int = 0) -> dict:
    """Agent 主动请求之前被截断的工具结果的完整内容。
    对标 Codex configurable limits — Agent 自己判断"我需要更多"。
    """
    from tools.result import ToolResult
    key = f"{tool_name}:{result_index}"
    # 1) 内存 LRU（最快）——逆序搜索（最近优先）
    for full_key in reversed(list(_TOOL_RESULT_CACHE.keys())):
        if full_key.endswith(key) or key in full_key:
            content = _TOOL_RESULT_CACHE[full_key]
            _TOOL_RESULT_CACHE.move_to_end(full_key)  # LRU: 标记为最近访问
            return ToolResult(
                success=True,
                data={"tool_name": tool_name, "result_index": result_index,
                      "content": content, "chars": len(content)},
                observation=f"返回 {tool_name} 第 {result_index} 个结果的完整内容 ({len(content)} 字符)",
                meta={"tool_name": "request_more_context", "retrieved_chars": len(content),
                      "source": "memory"},
            ).to_dict()

    # 2) 落盘 spill（重启/LRU 淘汰后仍可命中）
    try:
        from services.tool_spill import find_spill
        spilled = find_spill(tool_name, result_index)
    except Exception:
        spilled = None
    if spilled is not None:
        return ToolResult(
            success=True,
            data={"tool_name": tool_name, "result_index": result_index,
                  "content": spilled, "chars": len(spilled)},
            observation=f"返回 {tool_name} 第 {result_index} 个结果的完整内容 ({len(spilled)} 字符，来自落盘 spill)",
            meta={"tool_name": "request_more_context", "retrieved_chars": len(spilled),
                  "source": "spill"},
        ).to_dict()

    return ToolResult(
        success=False,
        error=(
            f"未找到 {tool_name} 第 {result_index} 个结果：内存 LRU（上限 "
            f"{TOOL_RESULT_CACHE_MAX} 条）与落盘 spill 均未命中。"
        ),
        meta={"tool_name": "request_more_context"},
    ).to_dict()


# ═══════════════════════════════════════════════════════════
# Phase 45-E: Agent 自观测
# ═══════════════════════════════════════════════════════════

@tool(
    name="get_own_metrics", agents=["hermes", "openclaw"],
    description=(
        "查看 Yuncat 自身的运行指标: 后台 Loop 状态、EventBus 统计、Token 用量等。"
        "Agent 可以调用此工具了解自己运行得好不好。"
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
    is_read_only=True, is_concurrency_safe=True,
    groups=["context"],
)
async def get_own_metrics() -> dict:
    """Agent 自观测——查看 Yuncat 自身的运行时指标。

    Phase 45-E: 接入 LoopSupervisor.get_status() + EventBus.get_stats()。
    """
    import time as _t
    from tools.result import ToolResult
    from context import get_app, get_db

    result: dict = {
        "uptime_seconds": 0,
        "event_bus": {},
        "loop_status": {},
        "compact_health": {},
        "last_hour": {},
    }

    # 1. LoopSupervisor 状态 + Circuit Breaker + EventBus
    app = get_app()
    if app and hasattr(app, "state"):
        sv = getattr(app.state, "supervisor", None)
        if sv:
            result["loop_status"] = sv.get_status()
        ag = getattr(app.state, "agent_service", None)
        if ag:
            result["uptime_seconds"] = int(_t.time() - getattr(ag, '_start_time', _t.time()))
        # 2. Circuit Breaker 状态
        ms = getattr(getattr(app.state, "agent_service", None), '_memory_service', None)
        if ms:
            result["compact_health"] = {
                "circuit_breaker_open": len(getattr(ms, '_compact_circuit_broken', set())),
                "failure_streaks": {k[:8]: v for k, v in
                    getattr(ms, '_compact_failure_streak', {}).items()},
            }
        # 3. EventBus 统计
        eb = getattr(app.state, "event_bus", None)
        if eb and hasattr(eb, 'get_stats'):
            result["event_bus"] = eb.get_stats()

    # 4. Token 用量（最近 1 小时）
    try:
        db = get_db()
        if db:
            cursor = await db.execute(
                "SELECT COUNT(*), COALESCE(SUM(json_extract(event_data,'$.total_tokens')),0) "
                f"FROM analytics_events WHERE event_type IN {TOKEN_USAGE_EVENT_SQL} "
                "AND created_at > ?",
                (int(_t.time()) - 3600,),
            )
            row = await cursor.fetchone()
            if row:
                result["last_hour"] = {"api_calls": row[0] or 0,
                                       "total_tokens": row[1] or 0}
    except Exception:
        _log.debug("查询运行指标失败", exc_info=True)

    return ToolResult(
        success=True,
        data=result,
        observation=(
            f"Yuncat 运行指标: 运行 {result['uptime_seconds']}s, "
            f"{len(result.get('loop_status', {}))} 个后台 Loop, "
            f"熔断 session: {result.get('compact_health', {}).get('circuit_breaker_open', 0)}"
        ),
        meta={"tool_name": "get_own_metrics"},
    ).to_dict()


# ═══════════════════════════════════════════════════════════
# CodeWhale 借鉴: Agent 驱动的上下文清除（purge）
# ═══════════════════════════════════════════════════════════

@tool(
    name="purge_context", agents=["hermes", "openclaw"],
    description=(
        "Remove consumed tool outputs from conversation history to free context space. "
        "Automatically cascades ToolUse↔ToolResult pairs (deleting one deletes the other). "
        "Use 1-based message IDs from the conversation. When in doubt, keep the message. "
        "Example: purge_context(session_id='abc123', message_indices=[5, 6, 7])"
    ),
    parameters={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Session ID to purge messages from",
            },
            "message_indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "1-based message IDs to remove (tool results you've already consumed)",
            },
        },
        "required": ["session_id", "message_indices"],
    },
    is_read_only=False,
    is_concurrency_safe=False,
    concurrency_safety="exclusive",
    groups=["context"],
)
async def purge_context(session_id: str, message_indices: list[int]) -> dict:
    """CodeWhale 借鉴: Agent 主动清理已消费的工具输出。

    配对级联删除——ToolUse ↔ ToolResult 必须成对删除，否则 API 报错。
    保守原则: When in doubt, keep the message.
    """
    from tools.result import ToolResult
    from context import get_app

    try:
        app = get_app()
        if not app or not hasattr(app, "state"):
            return ToolResult(
                success=False, error="App context not available",
                meta={"tool_name": "purge_context"},
            ).to_dict()

        memory_service = getattr(app.state, "memory_service", None)
        if not memory_service:
            return ToolResult(
                success=False, error="Memory service not available",
                meta={"tool_name": "purge_context"},
            ).to_dict()

        # 获取会话历史——直接操作内存缓存（_sessions）
        sessions = getattr(memory_service, "_sessions", {})
        if session_id not in sessions:
            return ToolResult(
                success=False, error=f"Session {session_id[:8]}... not found in memory cache",
                meta={"tool_name": "purge_context"},
            ).to_dict()

        history = sessions[session_id]
        if not isinstance(history, list):
            return ToolResult(
                success=False, error="Session history is not a list",
                meta={"tool_name": "purge_context"},
            ).to_dict()

        # 1-based → 0-based 转换 + 边界校验
        indices_0based = set()
        for idx in message_indices:
            i = idx - 1  # 1-based → 0-based
            if 0 <= i < len(history):
                indices_0based.add(i)

        if not indices_0based:
            return ToolResult(
                success=True,
                data={"removed_count": 0, "cascade_count": 0,
                      "message": "No valid indices to remove"},
                observation="purge_context: 无有效索引可删除",
                meta={"tool_name": "purge_context"},
            ).to_dict()

        # 架构铁律 #2: 先写 DB 再更新内存缓存
        # 阶段 1: 计算完整删除集（含级联配对）——不修改 history
        from services.agent.agent_compact import _compute_purge_indices
        full_indices = _compute_purge_indices(history, indices_0based)

        # 阶段 2: 收集要删除消息的 DB conv_id
        conv_ids: list[str] = []
        for i in full_indices:
            if 0 <= i < len(history):
                cid = history[i].get("id")
                if cid:
                    conv_ids.append(cid)

        # 阶段 3: 先删 DB 行——写穿透
        if conv_ids:
            try:
                from context import get_db
                from repositories import delete_conversations_by_ids
                db = get_db()
                if db:
                    deleted = await delete_conversations_by_ids(db, conv_ids)
                    _log.info("purge_context: deleted %d DB rows (conv_ids=%s)",
                              deleted, conv_ids[:3])
            except Exception:
                _log.warning("purge_context: DB delete failed, proceeding with memory-only",
                             exc_info=True)

        # 阶段 4: 再删内存缓存
        from services.agent.agent_compact import execute_purge_operations
        result = execute_purge_operations(history, indices_0based)

        return ToolResult(
            success=True,
            data=result,
            observation=(
                f"purge_context: 删除 {result['removed_count']} 条消息"
                f"（含 {result['cascade_count']} 条配对级联）"
            ),
            meta={"tool_name": "purge_context", **result},
        ).to_dict()
    except Exception as e:
        return ToolResult(
            success=False, error=str(e),
            meta={"tool_name": "purge_context"},
        ).to_dict()
