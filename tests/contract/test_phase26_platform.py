"""tests/contract/test_phase26_platform.py —— Phase 26 AgentPlatformBridge 合约测试"""
import os
import sys
import pytest
import tempfile


# ---- AgentPlatformBridge ----

def test_platform_bridge_init():
    """AgentPlatformBridge 基本初始化"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from adapters.platform_bridge import AgentPlatformBridge, PlatformTool
    bridge = AgentPlatformBridge()
    assert bridge._tools_cache is None


@pytest.mark.asyncio
async def test_probe_capabilities_no_manifest():
    """无 manifest 时 probe 返回 source=none"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from adapters.platform_bridge import AgentPlatformBridge
    bridge = AgentPlatformBridge(manifest_dir="/nonexistent")
    result = await bridge.probe_capabilities("nonexistent_agent")
    assert result["source"] == "none"
    assert result["total_tools"] == 0


@pytest.mark.asyncio
async def test_probe_capabilities_hermes_manifest():
    """Hermes manifest 加载成功"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from adapters.platform_bridge import AgentPlatformBridge
    bridge = AgentPlatformBridge()
    result = await bridge.probe_capabilities("hermes")
    assert result["source"] == "manifest"
    assert result["total_tools"] >= 5  # hermes.yaml 有 7 个工具
    assert any(t["name"] == "hermes_web_search" for t in result["tools"])


@pytest.mark.asyncio
async def test_probe_capabilities_openclaw_manifest():
    """OpenClaw manifest 加载成功"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from adapters.platform_bridge import AgentPlatformBridge
    bridge = AgentPlatformBridge()
    result = await bridge.probe_capabilities("openclaw")
    assert result["source"] == "manifest"
    assert any(t["name"] == "openclaw_vision" for t in result["tools"])
    assert any(t["name"] == "openclaw_notify" for t in result["tools"])


def test_platform_tool_dataclass():
    """PlatformTool dataclass 结构"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from adapters.platform_bridge import PlatformTool
    t = PlatformTool(name="test", description="desc", category="search", agent_id="a")
    assert t.name == "test"
    assert t.is_available is True


@pytest.mark.asyncio
async def test_list_tools_caching():
    """list_tools 第二次调用走缓存"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from adapters.platform_bridge import AgentPlatformBridge
    bridge = AgentPlatformBridge()
    tools1 = await bridge.list_tools("hermes")
    tools2 = await bridge.list_tools("hermes")
    assert len(tools1) == len(tools2)
    assert bridge._tools_cache is not None


@pytest.mark.asyncio
async def test_execute_tool_default_not_implemented():
    """基类 execute_tool 返回 not_implemented"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from adapters.platform_bridge import AgentPlatformBridge
    bridge = AgentPlatformBridge()
    result = await bridge.execute_tool("hermes", "test_tool", {})
    assert result["ok"] is False
    assert "not implemented" in result["error"]


# ---- ECC 角色（Phase 28: 已迁移到 DB SEED_ROLES） ----

def test_ecc_extended_has_26_roles():
    """Phase 28: 21 ECC 角色在 DB SEED_ROLES 中"""
    import asyncio, tempfile, os as _os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from repositories import init_db, run_migrations, get_enabled_roles

    async def verify():
        db_path = _os.path.join(tempfile.gettempdir(), "test_p26_roles.db")
        try: _os.remove(db_path)
        except: pass
        db = await init_db(db_path)
        await run_migrations(db)
        roles = await get_enabled_roles(db)
        ids = {r["id"] for r in roles}
        for rid in ["api-designer", "refactor-advisor", "type-design-analyzer",
                    "seo-specialist", "build-error-resolver", "comment-analyzer",
                    "harness-optimizer", "conversation-analyzer",
                    "code-simplifier", "code-explorer", "a11y-architect"]:
            assert rid in ids, f"Phase 26 role '{rid}' missing from DB"
        await db.close()
        _os.remove(db_path)
    asyncio.run(verify())


# ---- DAG 模板（Phase 26 追加 11 个） ----

def test_phase26_dag_templates_registered():
    """11 个新 DAG 模板在 BUILTIN_TEMPLATES 中"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from prompts.workflow_templates import BUILTIN_TEMPLATES
    names = [t["name"] for t in BUILTIN_TEMPLATES]
    expected = [
        "zh_security_review", "zh_tdd_loop", "zh_diagnose_loop", "zh_prototype",
        "zh_triage", "zh_dead_code_cleanup", "zh_coverage_boost",
        "zh_prp_plan", "zh_prp_pr", "zh_quality_gate", "zh_multi_model_plan",
    ]
    for name in expected:
        assert name in names, f"DAG template '{name}' not registered"


def test_phase26_dag_templates_no_cycles():
    """所有新 DAG 模板无循环"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from prompts.workflow_templates import BUILTIN_TEMPLATES
    from workflow.engine import WorkflowEngine
    engine = WorkflowEngine()
    new_names = [
        "zh_security_review", "zh_tdd_loop", "zh_diagnose_loop", "zh_prototype",
        "zh_triage", "zh_dead_code_cleanup", "zh_coverage_boost",
        "zh_prp_plan", "zh_prp_pr", "zh_quality_gate", "zh_multi_model_plan",
    ]
    for name in new_names:
        tmpl = next((t for t in BUILTIN_TEMPLATES if t["name"] == name), None)
        assert tmpl, f"Template {name} not found"
        engine._validate_graph(tmpl["graph"]["nodes"], tmpl["graph"]["edges"])
        # 无异常 = 通过


# ---- Tool manifests ----

def test_hermes_manifest_exists():
    manifest = os.path.join(
        os.path.dirname(__file__), "..", "..", "src-python", "config", "tools", "hermes.yaml",
    )
    assert os.path.exists(manifest)
    content = open(manifest, encoding="utf-8").read()
    assert "hermes_web_search" in content
    assert "hermes_shell_exec" in content
    assert "hermes_browser" in content


def test_openclaw_manifest_exists():
    manifest = os.path.join(
        os.path.dirname(__file__), "..", "..", "src-python", "config", "tools", "openclaw.yaml",
    )
    assert os.path.exists(manifest)
    content = open(manifest, encoding="utf-8").read()
    assert "openclaw_vision" in content
    assert "openclaw_notify" in content


# ---- main.py 注入 ----

def test_main_has_platform_bridge():
    # Phase 38 起 AgentPlatformBridge 不再创建孤立实例（无人调用），挂载语句
    # 已在 lifespan_postinit.py:481-483 注释掉。契约相应调整为：
    # ① 模块与类仍保留（可复用）；② lifespan 中不再出现未注释的挂载语句。
    module_code = open(
        os.path.join(
            os.path.dirname(__file__), "..", "..", "src-python",
            "adapters", "platform_bridge.py",
        ),
        encoding="utf-8",
    ).read()
    assert "class AgentPlatformBridge" in module_code

    for name in ("lifespan.py", "lifespan_postinit.py"):
        code = open(
            os.path.join(os.path.dirname(__file__), "..", "..", "src-python", name),
            encoding="utf-8",
        ).read()
        active = [
            ln.strip() for ln in code.splitlines()
            if "AgentPlatformBridge" in ln and not ln.strip().startswith("#")
        ]
        assert not active, f"{name} 不应再挂载 AgentPlatformBridge 孤立实例: {active}"


# ---- 回归: 已有模板仍存在 ----

def test_existing_templates_preserved():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from prompts.workflow_templates import BUILTIN_TEMPLATES
    names = [t["name"] for t in BUILTIN_TEMPLATES]
    for name in ["zh_data_analysis", "zh_code_review", "zh_doc_generation", "zh_craft_fix_loop",
                 "zh_auto_fix", "zh_architect_editor", "zh_speckit_pipeline"]:
        assert name in names, f"Existing template '{name}' preserved"
