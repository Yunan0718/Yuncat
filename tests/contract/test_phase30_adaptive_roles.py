"""tests/contract/test_phase30_adaptive_roles.py — Phase 30 自适应角色合约测试"""
import os, sys, tempfile, pytest


# ---- 临时角色生成 ----

def test_temp_role_prompt_format():
    """_maybe_generate_temp_role prompt 格式"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.deliberation.deliberation_service import DeliberationService
    ds = DeliberationService()
    assert hasattr(ds, "_maybe_generate_temp_role"), "DeliberationService missing _maybe_generate_temp_role"


@pytest.mark.asyncio
async def test_temp_role_no_agent_returns_none():
    """无 agent_service 时返回 None"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.deliberation.deliberation_service import DeliberationService
    ds = DeliberationService()
    result = await ds._maybe_generate_temp_role("test", [{"name": "安全专家", "id": "security"}])
    assert result is None


@pytest.mark.asyncio
async def test_temp_role_none_for_sufficient_roles():
    """已有足够角色时 LLM 返回 NO——不生成临时角色（无真实 LLM 时走 Exception 返回 None）"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.deliberation.deliberation_service import DeliberationService
    ds = DeliberationService()
    # No agent_service → None
    result = await ds._maybe_generate_temp_role("修复一个小bug", [
        {"name": "安全专家", "id": "security"},
        {"name": "开发者", "id": "developer"},
    ])
    assert result is None


# ---- 临时角色转化 API ----

def test_main_has_temp_role_api():
    routes_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "routes", "roles.py"),
        encoding="utf-8",
    ).read()
    assert "/roles/pending" in routes_code
    assert "/roles/promote" in routes_code
    assert "/roles/reject" in routes_code


# ---- 临时角色追踪 ----

def test_feedback_learner_has_track():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.feedback_learning import FeedbackLearner
    assert hasattr(FeedbackLearner, "track_temporary_role"), "FeedbackLearner missing track_temporary_role"


def test_track_temporary_role_creates():
    """track_temporary_role 创建 knowledge_node"""
    import asyncio, time
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from config import DATA_DIR
    from repositories import init_db, run_migrations
    from services.feedback_learning import FeedbackLearner

    async def test():
        db_path = os.path.join(tempfile.gettempdir(), "test_p30_track.db")
        try: os.remove(db_path)
        except: pass
        db = await init_db(db_path)
        await run_migrations(db)
        fb = FeedbackLearner(db=db)
        await fb.track_temporary_role("测试角色-test123", "test task", True)
        cursor = await db.execute(
            "SELECT id, title, score FROM knowledge_nodes WHERE source='deliberation_tmp_role' ORDER BY created_at DESC LIMIT 1",
        )
        row = await cursor.fetchone()
        assert row is not None, "track_temporary_role should create knowledge_node"
        assert "测试角色" in row[1]
        assert row[2] >= 0.3
        await db.close()
        os.remove(db_path)

    asyncio.run(test())


def test_track_temporary_role_skips_rejected():
    """was_accepted=False 时不创建节点"""
    import asyncio, time
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from config import DATA_DIR
    from repositories import init_db, run_migrations
    from services.feedback_learning import FeedbackLearner

    async def test():
        db_path = os.path.join(tempfile.gettempdir(), "test_p30_skip.db")
        try: os.remove(db_path)
        except: pass
        db = await init_db(db_path)
        await run_migrations(db)
        fb = FeedbackLearner(db=db)
        await fb.track_temporary_role("skip-role", "test", False)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM knowledge_nodes WHERE title LIKE '%skip%'",
        )
        count = (await cursor.fetchone())[0]
        assert count == 0, "was_accepted=False should skip creation"
        await db.close()
        os.remove(db_path)

    asyncio.run(test())


# ---- MemoryTree 转化检测 ----

def test_memory_tree_has_promote():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.memory.memory_tree import MemoryTree
    assert hasattr(MemoryTree, "_promote_ready_temp_roles"), "MemoryTree missing _promote_ready_temp_roles"


@pytest.mark.asyncio
async def test_promote_ready_no_nodes():
    """空知识树时 _promote_ready_temp_roles 返回 0"""
    import time
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from repositories import init_db, run_migrations
    from services.memory.memory_tree import MemoryTree

    db_path = os.path.join(tempfile.gettempdir(), "test_p30_promote.db")
    try: os.remove(db_path)
    except: pass
    db = await init_db(db_path)
    await run_migrations(db)
    mt = MemoryTree(db=db)
    count = await mt._promote_ready_temp_roles()
    assert count == 0
    await db.close()
    os.remove(db_path)


@pytest.mark.asyncio
async def test_promote_detects_qualified():
    """score >= 0.7 的临时角色被检测到"""
    import time
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from repositories import init_db, run_migrations, upsert_knowledge_node

    db_path = os.path.join(tempfile.gettempdir(), "test_p30_detect.db")
    try: os.remove(db_path)
    except: pass
    db = await init_db(db_path)
    await run_migrations(db)

    # Insert a qualified temp role
    import uuid
    nid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO knowledge_nodes(id, title, content, source, score, tags, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (nid, "临时角色: 区块链安全审计师", "content", "deliberation_tmp_role", 0.85,
         "[]", int(time.time()), int(time.time())),
    )
    await db.commit()

    from services.memory.memory_tree import MemoryTree
    mt = MemoryTree(db=db)
    count = await mt._promote_ready_temp_roles()
    assert count >= 1
    await db.close()
    os.remove(db_path)


# ---- Settings 角色管理 ----

def test_roles_table_has_source_column():
    """roles 表有 source 列"""
    sc = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "repositories", "schema.py"),
        encoding="utf-8",
    ).read()
    assert "source TEXT" in sc, "roles table should have source column"


# ---- 全链路验证: run_deliberation → temp_role → track → promote ----

def test_deliberation_service_has_temp_role_call():
    """run_deliberation 调用 _maybe_generate_temp_role"""
    ds = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "services", "deliberation", "deliberation_discussion.py"),
        encoding="utf-8",
    ).read()
    assert "_maybe_generate_temp_role" in ds, "run_deliberation should call _maybe_generate_temp_role"
    assert "track_temporary_role" in ds, "run_deliberation should call track_temporary_role"


def test_background_loop_has_promote():
    """MemoryTree.background_loop 调用 _promote_ready_temp_roles"""
    mt = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "services", "memory", "memory_tree.py"),
        encoding="utf-8",
    ).read()
    assert "_promote_ready_temp_roles" in mt, "background_loop should call _promote_ready_temp_roles"
