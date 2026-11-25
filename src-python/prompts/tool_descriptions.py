"""prompts/tool_descriptions.py —— P4: 工具 Function Calling 描述模板

生成 OpenAI Function Calling 兼容的工具 JSON Schema。
对标 Claude Code: 每个工具 description 含触发条件 + 使用场景 + 限制说明。
"""
__version__ = "1.1.0"

# ── 关键工具的增强描述——Agent 用这些来判断"什么时候用什么工具" ──
_TOOL_ENHANCED = {
    "sequential_thinking": (
        "Step-by-step reasoning for complex problems. Decompose into numbered steps, "
        "reason through each systematically. USE: multi-step tasks, architecture decisions, "
        "debugging, comparing multiple approaches. Max 8 steps. Do NOT use for simple "
        "single-step operations or known-answer questions. Each step should focus on one sub-problem."
    ),
    "codegraph_context": (
        "Query code structure and symbol relationships (LSP-level semantic understanding). "
        "Understand function call chains, module dependencies, and change impact. "
        "PREFERRED over search_files for understanding project structure. "
        "USE: finding function definitions, tracing call paths, impact analysis. "
        "FALLBACK: if codegraph is unavailable, use search_files + read_file instead."
    ),
    "docs_lookup": (
        "Query latest official documentation for a library or framework. "
        "USE when uncertain about API signature, parameter order, return type, "
        "version compatibility, or best practices. Never answer API usage questions "
        "from memory — training data may be outdated. "
        "Example: docs_lookup(library='fastapi', query='lifespan async')"
    ),
    "request_more_context": (
        "Recover full content of a previously truncated tool result. "
        "USE when you see '[结果已摘要]' or '[截断]' markers and you genuinely "
        "need the truncated information to continue. "
        "Do NOT call on every turn — only when the truncated content is blocking progress."
    ),
    "search_files": (
        "Fast text search in the codebase using ripgrep (regex-capable, highly optimized). "
        "PREFERRED over reading files one by one when looking for specific patterns, "
        "function names, error messages, or TODO markers across the project. "
        "Keep queries short (1-5 keywords) — not full sentences. "
        "If no results, change keywords rather than adding more terms."
    ),
    "list_dir": (
        "List directory contents. USE as the first step when exploring an unknown project — "
        "find the entry points (README.md, package.json, pyproject.toml) before reading files. "
        "Do NOT recursively list huge directories — start shallow, go deep selectively."
    ),
    "read_file": (
        "Read a file's contents. USE with specific file paths you already know. "
        "For exploring unknown projects, use list_dir first to discover the structure. "
        "For large files (>500 lines), read the first 100-200 lines to understand the "
        "overview before reading the rest. Use offset+limit to read specific sections."
    ),
    "write_file": (
        "Create or overwrite a file. Returns a diff preview first — file is NOT written "
        "until user confirms. USE when creating new files or when modifying >50% of "
        "an existing file. For smaller edits, prefer edit_file (SEARCH/REPLACE) which "
        "saves 80% tokens on large files. Parent directories are created automatically."
    ),
    "edit_file": (
        "Edit a file using SEARCH/REPLACE pattern. The SEARCH string must match EXACTLY "
        "one occurrence in the file (including whitespace and indentation). "
        "USE for small targeted edits (<50% of file). Keep SEARCH strings short but unique "
        "— long strings are more likely to fail on whitespace differences. "
        "FALLBACK: if 2 consecutive edit_file calls fail, re-read the file and consider write_file."
    ),
    "run_command": (
        "Execute a shell command. Only whitelisted commands are allowed (git, python, node, "
        "npm, pip, docker, etc.). USE for system operations, test execution, package management. "
        "For complex scripts, write_file + run_command is preferred over long inline commands. "
        "Default timeout: 60s. For long operations (builds, installs), pass timeout_sec explicitly."
    ),
    "delegate_to_agent": (
        "Send a task to an external agent backend (OpenClaw/Hermes/Codex/Claude). "
        "STRICT RULE: only call this when the user explicitly names that backend in the "
        "current turn (e.g. 'let OpenClaw look at this image'). External backends are off by "
        "default and only usable in MAX mode; never delegate on your own judgment — do the "
        "task with your own native tools first and state clearly if a capability is missing. "
        "Do not verbally suggest delegation instead of calling. "
        "Multiple delegate calls can be issued in parallel when tasks are independent."
    ),
    "cron_schedule": (
        "Create a recurring or one-shot scheduled task. Tasks persist across restarts "
        "(stored in SQLite). USE for periodic operations like daily backups, weekly reports, "
        "or one-shot reminders. Supports standard 5-field cron expressions."
    ),
}


def render_tool_schema(tool_name: str, tool_info: dict) -> dict:
    """生成 OpenAI Function Calling 兼容的工具描述——优先用增强描述。"""
    desc = _TOOL_ENHANCED.get(tool_name, tool_info.get("description", ""))
    base = {
        "name": tool_name,
        "description": desc,
        "parameters": tool_info.get("parameters", {"type": "object", "properties": {}}),
    }
    # confirmable 标记追加到 description 末尾
    if tool_info.get("confirmable"):
        base["description"] += " ⚠️ Requires user confirmation before execution."
    return base


def list_tools_for_agent(agent_id: str | None = None) -> list[dict]:
    """获取 Agent 可用的工具 Function Calling schema 列表"""
    from tools.registry import list_tools as _list_tools
    tools = _list_tools(agent_id)
    return [render_tool_schema(t["name"], t) for t in tools]
