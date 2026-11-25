"""memory_tool.py —— P0-1.12: 记忆工具（search_memory + store_memory）

对标 Letta/MemGPT 的记忆工具化理念（core_memory_append / archival_memory_search）。
让 Agent 能主动检索/写入记忆，而非仅依赖 build_agent_context 的被动注入。

Phase 47 工具策略层（tools_policy.yaml:48）已声明 search_memory 允许，
但 tools/registry.py 未注册 → Agent 无法主动检索记忆。
本模块补齐该断点。
"""
from ..registry import tool
from config import (
    MEMORY_TOOL_SEARCH_LIMIT, MEMORY_TOOL_STORE_MAX_CHARS,
    TOOL_SIZE_CONVERSATION_SEARCH_CHARS,
)


@tool(
    name="search_memory", agents=["hermes", "openclaw"],
    description=(
        "Search the workspace memory tree for relevant past context, "
        "decisions, or task lessons. Use this when you need to recall "
        "information from previous sessions or related work. "
        "Returns formatted markdown with relevant memory snippets."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language query describing what to recall",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of memory snippets to return (default 5)",
                "default": 5,
            },
        },
        "required": ["query"],
    },
    is_read_only=True, is_concurrency_safe=True,
    groups=["memory"],
)
async def search_memory(query: str, limit: int = 5) -> dict:
    """检索工作区记忆树——混合检索（FTS5 关键词 + chromadb 向量）

    对标 Letta archival_memory_search / Cursor @codebase 语义搜索。
    """
    from tools.result import ToolResult
    from context import get_app

    try:
        app = get_app()
        memory_tree = getattr(app.state, "memory_tree", None) if app and hasattr(app, "state") else None
    except Exception:
        memory_tree = None

    if memory_tree is None:
        return ToolResult(
            success=False,
            error="Memory tree not available",
            meta={"tool_name": "search_memory"},
        ).to_dict()

    try:
        # 限制 limit 在合理范围
        safe_limit = max(1, min(int(limit or 5), MEMORY_TOOL_SEARCH_LIMIT))
        ctx = await memory_tree.retrieve_context(query, limit=safe_limit)
        return ToolResult(
            success=True,
            data={"query": query, "results": ctx, "count": safe_limit},
            observation=ctx if ctx else "无相关记忆",
            meta={"tool_name": "search_memory", "query": query},
        ).to_dict()
    except Exception as e:
        return ToolResult(
            success=False,
            error=f"Memory search failed: {e}",
            meta={"tool_name": "search_memory"},
        ).to_dict()


@tool(
    name="store_memory", agents=["hermes", "openclaw"],
    description=(
        "Store a piece of information into the workspace memory tree for "
        "future recall. Use this to persist important decisions, task lessons, "
        "or project context that should be available in later sessions. "
        "The content will be indexed for both keyword and semantic search."
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The information to store (decisions, lessons, context)",
            },
            "source": {
                "type": "string",
                "description": "Optional source label (e.g. 'task_lesson', 'decision')",
                "default": "tool_store",
            },
        },
        "required": ["content"],
    },
    is_read_only=False, is_concurrency_safe=True,
    groups=["memory"],
)
async def store_memory(content: str, source: str = "tool_store") -> dict:
    """写入工作区记忆树——索引到 FTS5 + chromadb

    对标 Letta core_memory_append / Claude Code .claude/rules 持久化。
    """
    from tools.result import ToolResult
    from context import get_app

    if not content or not content.strip():
        return ToolResult(
            success=False,
            error="content must not be empty",
            meta={"tool_name": "store_memory"},
        ).to_dict()

    # 截断超长内容——防止单条记忆占用过多索引空间
    safe_content = content[:MEMORY_TOOL_STORE_MAX_CHARS]

    try:
        app = get_app()
        memory_tree = getattr(app.state, "memory_tree", None) if app and hasattr(app, "state") else None
    except Exception:
        memory_tree = None

    if memory_tree is None:
        return ToolResult(
            success=False,
            error="Memory tree not available",
            meta={"tool_name": "store_memory"},
        ).to_dict()

    try:
        # Batch 2.1: 工具写入记忆时携带当前 workspace 作为 project_id，实现项目隔离。
        # 与 agent_context.py 的 RAG 检索侧使用同一规范化规则（os.path.normcase），
        # 保证 Windows 路径大小写不敏感时等值比较一致。
        from security.sandbox import get_workspace
        import os as _os
        workspace_project_id = _os.path.normcase(str(get_workspace()))
        await memory_tree.index_from_text(
            text=safe_content, source=source,
            project_id=workspace_project_id,
        )
        return ToolResult(
            success=True,
            data={"stored": True, "source": source, "chars": len(safe_content)},
            observation=f"已存储记忆（{len(safe_content)} 字符，来源: {source}）",
            meta={"tool_name": "store_memory", "source": source},
        ).to_dict()
    except Exception as e:
        return ToolResult(
            success=False,
            error=f"Memory store failed: {e}",
            meta={"tool_name": "store_memory"},
        ).to_dict()


@tool(
    name="remember_preference", agents=["hermes", "openclaw"],
    description=(
        "记住用户明确表达的长期偏好（如回复风格、语言、工具选择、沟通习惯）。"
        "当用户说『我喜欢/我偏好/记住：…』或直接表达偏好时调用。"
        "偏好会被持久化、跨会话保留，并自动注入后续对话。"
        "可选 category 用于归类（同一类别的新偏好会自动取代旧的）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "用户偏好的原文，例如『回复简洁，不要啰嗦』",
            },
            "category": {
                "type": "string",
                "description": "可选分类，例如『回复风格』『语言』『工具』；同类别新偏好会取代旧偏好",
                "default": "",
            },
        },
        "required": ["content"],
    },
    is_read_only=False, is_concurrency_safe=True,
    groups=["memory"],
)
async def remember_preference(content: str, category: str = "") -> dict:
    """捕获用户明确表达的偏好——源隔离到 knowledge_nodes(source='user_preference')。

    对标 Claude Code 的「remember that I prefer X」显式记忆；与 store_memory
    的通用记忆树区分：偏好走独立 source，便于检索/注入/管理且不被任务教训污染。
    同类别自动调和（最新获胜），避免相互矛盾的偏好长期共存。
    """
    from tools.result import ToolResult
    from context import get_app

    if not content or not content.strip():
        return ToolResult(
            success=False,
            error="content must not be empty",
            meta={"tool_name": "remember_preference"},
        ).to_dict()

    try:
        app = get_app()
        db = getattr(app.state, "db", None) if app and hasattr(app, "state") else None
    except Exception:
        db = None
    if db is None:
        return ToolResult(
            success=False,
            error="Database not available",
            meta={"tool_name": "remember_preference"},
        ).to_dict()

    try:
        from security.sandbox import get_workspace
        import os as _os
        workspace_project_id = _os.path.normcase(str(get_workspace()))
        from repositories.user_preferences_repo import capture_preference
        node = await capture_preference(
            db, content.strip(),
            project_id=workspace_project_id,
            category=category.strip() or None,
        )
        return ToolResult(
            success=True,
            data={"id": node["id"], "category": category or None},
            observation=f"已记住偏好：{content.strip()[:60]}",
            meta={"tool_name": "remember_preference", "id": node["id"]},
        ).to_dict()
    except Exception as e:
        return ToolResult(
            success=False,
            error=f"Failed to remember preference: {e}",
            meta={"tool_name": "remember_preference"},
        ).to_dict()


@tool(
    name="search_conversation_history", agents=["hermes", "openclaw"],
    description=(
        "Full-text search across ALL past conversation sessions "
        "(FTS5 index with LIKE fallback). Use this to recall what was "
        "discussed or decided in previous sessions — beyond the "
        "automatically inherited last-session summary. Returns "
        "session_id, preview, role and timestamp per hit."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword or phrase to search for in past conversations",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of hits to return (default 10)",
                "default": 10,
            },
        },
        "required": ["query"],
    },
    is_read_only=True, is_concurrency_safe=True,
    groups=["memory"],
    max_result_size_chars=TOOL_SIZE_CONVERSATION_SEARCH_CHARS,
)
async def search_conversation_history(query: str, limit: int = 10) -> dict:
    """跨会话历史检索——补齐 Hermes 式「搜索自己的过往对话」能力。

    4.3 核查结论的剩余缺口：跨会话回忆此前只有「被动继承上一个会话摘要」，
    agent 无法自主翻找更早的历史。查询逻辑与 HTTP 端点
    /api/v1/conversations/search 共用 conversations_repo.search_conversations 单源。
    """
    from tools.result import ToolResult
    from context import get_app

    try:
        app = get_app()
        db = getattr(app.state, "db", None) if app and hasattr(app, "state") else None
    except Exception:
        db = None
    if db is None:
        return ToolResult(
            success=False,
            error="Database not available",
            meta={"tool_name": "search_conversation_history"},
        ).to_dict()

    try:
        from repositories.conversations_repo import search_conversations as _search
        results = await _search(db, query, limit)
        return ToolResult(
            success=True,
            data={"query": query, "results": results, "count": len(results)},
            observation=f"历史会话检索 '{query}'：{len(results)} 条命中",
            meta={"tool_name": "search_conversation_history", "matches": len(results)},
        ).to_dict()
    except Exception as e:
        return ToolResult(
            success=False,
            error=f"Conversation search failed: {e}",
            meta={"tool_name": "search_conversation_history"},
        ).to_dict()
