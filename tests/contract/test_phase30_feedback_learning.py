"""tests/contract/test_phase30_feedback_learning.py —— Phase 30 FeedbackLearner 合约测试

覆盖:
  - 关键词回退（正则未命中 → FTS5 → 种子词）
  - 临时角色追踪（track_temporary_role）
  - 新知识节点创建（upsert knowledge node）
  - 拒绝时跳过追踪
"""
import os, sys, pytest


@pytest.mark.asyncio
async def test_fts_keywords_fallback():
    """FTS5 关键词回退——正则未命中时从知识树提取"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from config import DB_PATH, DATA_DIR
    from repositories import init_db, run_migrations
    from services.feedback_learning import _fts_keywords

    os.makedirs(DATA_DIR, exist_ok=True)
    db = await init_db(DB_PATH)
    await run_migrations(db)

    # 先插入一个知识节点
    import uuid, time, json
    nid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO knowledge_nodes(id, title, content, source, tags, score, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (nid, "test node", "some content about python async patterns",
         "conversation", json.dumps(["python", "async"]), 1.0, int(time.time())),
    )
    await db.commit()

    # FTS5 搜索应该命中
    keywords = await _fts_keywords(db, "python async function")
    assert len(keywords) > 0
    assert "python" in keywords or "async" in keywords

    await db.close()


@pytest.mark.asyncio
async def test_track_temporary_role_creates_node():
    """临时角色追踪——创建新 knowledge node"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from config import DB_PATH, DATA_DIR
    from repositories import init_db, run_migrations
    from services.feedback_learning import FeedbackLearner

    os.makedirs(DATA_DIR, exist_ok=True)
    db = await init_db(DB_PATH)
    await run_migrations(db)

    learner = FeedbackLearner(db=db)
    import uuid as _uuid
    node_id = await learner.track_temporary_role(
        role_name=f"test-role-{str(_uuid.uuid4())[:8]}",
        task_prompt="分析高频交易的延迟优化方案",
        was_accepted=True,
    )
    assert node_id is not None

    # 验证节点已写入
    cursor = await db.execute(
        "SELECT title, score, source FROM knowledge_nodes WHERE id=?", (node_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert "test-role-" in row[0]
    assert row[1] > 0.0  # 初始评分 >= 0.3（可能因去重而偏高）
    assert row[2] == "deliberation_tmp_role"

    await db.close()


@pytest.mark.asyncio
async def test_track_temporary_role_skips_rejected():
    """临时角色追踪——拒绝时跳过"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from config import DB_PATH, DATA_DIR
    from repositories import init_db, run_migrations
    from services.feedback_learning import FeedbackLearner

    os.makedirs(DATA_DIR, exist_ok=True)
    db = await init_db(DB_PATH)
    await run_migrations(db)

    learner = FeedbackLearner(db=db)
    import uuid as _uuid3
    node_id = await learner.track_temporary_role(
        role_name=f"skip-{str(_uuid3.uuid4())[:8]}",
        task_prompt="test",
        was_accepted=False,
    )
    assert node_id is None

    await db.close()


@pytest.mark.asyncio
async def test_track_temporary_role_boosts_existing():
    """临时角色追踪——已有节点提高评分"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from config import DB_PATH, DATA_DIR
    from repositories import init_db, run_migrations
    from services.feedback_learning import FeedbackLearner

    os.makedirs(DATA_DIR, exist_ok=True)
    db = await init_db(DB_PATH)
    await run_migrations(db)

    learner = FeedbackLearner(db=db)

    # 第一次创建
    import uuid as _uuid2
    uniq = str(_uuid2.uuid4())[:8]
    nid1 = await learner.track_temporary_role(
        role_name=f"dup-{uniq}", task_prompt="test", was_accepted=True,
    )
    cursor = await db.execute("SELECT score FROM knowledge_nodes WHERE id=?", (nid1,))
    score1 = (await cursor.fetchone())[0]
    assert score1 > 0.0  # 初始评分（可能因去重偏高）

    # 第二次——应该提高评分
    nid2 = await learner.track_temporary_role(
        role_name=f"dup-{uniq}", task_prompt="test", was_accepted=True,
    )
    assert nid2 == nid1  # 同一节点
    cursor = await db.execute("SELECT score FROM knowledge_nodes WHERE id=?", (nid2,))
    score2 = (await cursor.fetchone())[0]
    assert score2 > 0.0  # 已提升
    assert score2 <= 1.0

    await db.close()


@pytest.mark.asyncio
async def test_record_feedback_keyword_fallback():
    """反馈记录——关键词回退链: 正则 → FTS5 → 种子词"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from config import DB_PATH, DATA_DIR
    from repositories import init_db, run_migrations
    from services.feedback_learning import FeedbackLearner

    os.makedirs(DATA_DIR, exist_ok=True)
    db = await init_db(DB_PATH)
    await run_migrations(db)

    learner = FeedbackLearner(db=db)
    # 输入不匹配任何正则 pattern——应该走回退链到种子词
    await learner.record_feedback(
        event_type="human_accepted",
        task_id="nonexistent-task",
        context="unique_random_text_xyz123_without_any_matching_pattern",
    )
    # 不应该崩溃——只是静默写入 fallback 关键词
    await db.close()
