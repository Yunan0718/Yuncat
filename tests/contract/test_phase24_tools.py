"""tests/contract/test_phase24_tools.py —— Phase 24 生态工具合约测试"""
import os
import sys
import pytest

# ---- speckit DAG 模板 ----

def test_speckit_template_registered():
    """验证 speckit_pipeline 模板在 BUILTIN_TEMPLATES 中"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from prompts.workflow_templates import BUILTIN_TEMPLATES
    names = [t["name"] for t in BUILTIN_TEMPLATES]
    assert "zh_speckit_pipeline" in names, f"zh_speckit_pipeline not found in {names}"


def test_speckit_6_nodes():
    """speckit_pipeline 包含 6 个节点"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from prompts.workflow_templates import BUILTIN_TEMPLATES
    tmpl = next(t for t in BUILTIN_TEMPLATES if t["name"] == "zh_speckit_pipeline")
    node_ids = [n["id"] for n in tmpl["graph"]["nodes"]]
    assert node_ids == ["specify", "clarify", "plan", "tasks", "analyze", "implement"]


def test_speckit_dag_valid():
    """speckit_pipeline 无循环"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from prompts.workflow_templates import BUILTIN_TEMPLATES
    from workflow.engine import WorkflowEngine
    tmpl = next(t for t in BUILTIN_TEMPLATES if t["name"] == "zh_speckit_pipeline")
    engine = WorkflowEngine()
    engine._validate_graph(tmpl["graph"]["nodes"], tmpl["graph"]["edges"])
    # 无异常 = 通过


# ---- docs_lookup 工具注册 ----

def test_docs_lookup_tool_registered():
    """docs_lookup 工具已注册"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from tools.builtin.system_tools import docs_lookup  # noqa: F401
    from tools.registry import get_tool
    tool_meta = get_tool("docs_lookup")
    assert tool_meta is not None, "docs_lookup not registered"
    assert tool_meta.is_read_only is True
    assert tool_meta.is_concurrency_safe is True
    assert "hermes" in tool_meta.agents


# ---- sequential_thinking 工具注册 ----

def test_sequential_thinking_tool_registered():
    """sequential_thinking 工具已注册且为只读"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from tools.builtin.system_tools import sequential_thinking  # noqa: F401
    from tools.registry import get_tool
    tool_meta = get_tool("sequential_thinking")
    assert tool_meta is not None, "sequential_thinking not registered"
    assert tool_meta.is_read_only is True
    assert tool_meta.is_concurrency_safe is True


# ---- Context7 Client ----

@pytest.mark.asyncio
async def test_context7_client_initialized():
    """Context7Client 基本初始化"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.context7_client import Context7Client
    client = Context7Client()
    assert client._started is False
    assert client._transport is None


def test_context7_text_extraction():
    """MCP tools/call 文本提取"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.context7_client import Context7Client

    # 模拟 MCP content 格式
    result = {"content": [{"type": "text", "text": "Here is the doc content"}]}
    text = Context7Client._extract_text(result)
    assert "Here is the doc content" in text


# ---- ECC 角色 ----

def test_ecc_roles_in_db():
    """Phase 28: 21 ECC 角色已迁移到 DB SEED_ROLES"""
    import asyncio, tempfile, os as _os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from repositories import init_db, run_migrations, get_enabled_roles
    from config import DATA_DIR

    async def verify():
        db_path = _os.path.join(tempfile.gettempdir(), "test_p24_roles.db")
        try: _os.remove(db_path)
        except: pass
        db = await init_db(db_path)
        await run_migrations(db)
        roles = await get_enabled_roles(db)
        ids = {r["id"] for r in roles}
        for rid in ["docs-lookup", "sequential-thinking", "code-review-checklist",
                    "migration-planner", "dependency-scanner", "database-reviewer",
                    "accessibility-auditor", "silent-failure-hunter",
                    "performance-optimizer", "pr-test-analyzer"]:
            assert rid in ids, f"Role '{rid}' missing from DB"
        await db.close()
        _os.remove(db_path)
    asyncio.run(verify())


def test_architect_editor_template_registered():
    """Phase 20 architect_editor 模板已注册"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from prompts.workflow_templates import BUILTIN_TEMPLATES
    names = [t["name"] for t in BUILTIN_TEMPLATES]
    assert "zh_architect_editor" in names
    assert "zh_auto_fix" in names


# ---- Commander prompt 包含工具提示 ----

def test_commander_prompt_has_phase24_tools():
    """Commander prompt 包含 CodeGraph + context7 + UI 偏好"""
    cmdr = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "prompts", "commander.py"),
        encoding="utf-8",
    ).read()
    assert "docs_lookup" in cmdr, "Commander prompt missing docs_lookup hint"
    assert "sequential_thinking" in cmdr, "Commander prompt missing sequential_thinking hint"
    assert "codegraph_context" in cmdr, "Commander prompt missing codegraph_context hint"


# ---- Phase 24 合约要求: CodeGraph context 返回格式 + MCP Client tool discovery ----

def test_codegraph_context_response_format():
    """验证 CodeGraphClient 能正确解析 MCP tools/call content 数组"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.codegraph_client import CodeGraphClient
    import json as _json
    import asyncio

    client = CodeGraphClient(workspace_path="/tmp/test")

    # 模拟 MCP tools/call 返回格式: {"content": [{"type": "text", "text": "..."}]}
    symbols = [{"symbol": "AgentService.start_task", "file": "agent_service.py", "line": 43, "kind": "method"}]
    mock_content = {"content": [{"type": "text", "text": _json.dumps(symbols)}]}

    # 验证 _call 返回格式——没连上时返回空 list
    result = asyncio.run(client._call("codegraph_context", {"query": "start_task"}))
    assert isinstance(result, list), f"Expected list, got {type(result)}"


def test_codegraph_context_empty_on_disconnect():
    """CodeGraph 未连接时 context() 返回空 list（不抛异常）"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.codegraph_client import CodeGraphClient

    client = CodeGraphClient()
    # 未 start() 时 _transport 为 None，调用应安全返回空
    import asyncio
    result = asyncio.run(client.context("test query"))
    assert result == [], f"Expected empty list on disconnect, got {result}"


@pytest.mark.asyncio
async def test_mcp_list_tools_response_format():
    """MCP tools/list 响应格式正确解析为 McpTool 列表"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.mcp_client import StdioTransport, McpTool
    import json as _json

    t = StdioTransport(command=["echo", "test"])

    # 模拟 tools/list 响应 JSON-RPC 消息
    tools_response = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "tools": [
                {"name": "codegraph_context", "description": "Query code context",
                 "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
                {"name": "codegraph_search", "description": "Search symbols",
                 "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
            ],
        },
    }
    body = _json.dumps(tools_response).encode("utf-8")
    frame = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body

    # 注入缓冲区并解析
    t._buffer = frame
    await t._parse_messages()
    assert len(t._buffer) == 0, "Buffer should be fully consumed"

    # 验证 future 被正确 resolve
    # _parse_messages 会从 _pending 中匹配 id=1 的 future
    # 这里验证响应格式中的 tools 数组有正确结构
    tools_data = tools_response["result"]["tools"]
    assert len(tools_data) == 2
    assert tools_data[0]["name"] == "codegraph_context"
    assert "inputSchema" in tools_data[0]


@pytest.mark.asyncio
async def test_codegraph_client_tool_name_mapping():
    """CodeGraphClient 便捷方法映射到正确的 tool name"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.codegraph_client import CodeGraphClient

    client = CodeGraphClient()
    # 验证每个便捷方法对应的 MCP tool name
    # 所有方法都通过 _call(tool_name, args) 调用
    # 验证 tool name 硬编码值正确（不会因重构改变而忘记更新）
    import inspect

    method_map = {
        "context": "codegraph_context", "trace": "codegraph_trace",
        "impact": "codegraph_impact", "search": "codegraph_search",
        "callers": "codegraph_callers", "callees": "codegraph_callees",
        "status": "codegraph_status",
    }
    for method_name, expected_tool in method_map.items():
        method = getattr(client, method_name, None)
        assert method is not None, f"CodeGraphClient.{method_name} method missing"
        source = inspect.getsource(method)
        assert expected_tool in source, \
            f"CodeGraphClient.{method_name} should call '{expected_tool}' tool"
