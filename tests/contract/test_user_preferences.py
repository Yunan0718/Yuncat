"""contract: 非技术性用户偏好捕获/检索/调和（B 设计项）

背景（2026-09-03 优化）：用户明确表达的偏好（"回复简洁"、"用中文沟通"）
此前无任何捕获入口——feedback_learning 仅从工具委托信号被动推导，
且偏好无源隔离、无管理界面、无注入。

本文件锁定 user_preferences 服务的不变量：
- capture 后 list 能命中（源隔离 source="user_preference"）
- 空文本 fail-closed（ValueError）
- 同类别新偏好自动取代旧偏好（最新获胜，A-lite 调和）
- 项目隔离：项目级偏好仅该项目可见，全局(NULL)对所有项目可见
- build_preference_snippet 仅含有效（未取代）偏好
- delete 后彻底移除
"""
import os
import sys

import aiosqlite
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from repositories.schema import SCHEMA_SQL  # noqa: E402
from services.user_preferences import (  # noqa: E402
    build_preference_snippet,
    capture_preference,
    delete_preference,
    list_preferences,
)


async def _new_db():
    db = await aiosqlite.connect(":memory:")
    await db.executescript(SCHEMA_SQL)
    await db.commit()
    return db


async def test_capture_then_list():
    db = await _new_db()
    node = await capture_preference(db, "回复简洁")
    assert node["source"] == "user_preference"
    rows = await list_preferences(db)
    assert len(rows) == 1
    assert rows[0]["content"] == "回复简洁"
    assert "preference" in rows[0]["tags"]


async def test_empty_text_fails_closed():
    db = await _new_db()
    with pytest.raises(ValueError):
        await capture_preference(db, "   ")


async def test_same_category_latest_wins():
    db = await _new_db()
    await capture_preference(db, "回复详细", category="回复风格")
    await capture_preference(db, "回复简洁", category="回复风格")
    rows = await list_preferences(db)
    # 旧偏好被标记 superseded，列表仅含最新一条
    assert len(rows) == 1, "同类别旧偏好应被取代，不应并存"
    assert rows[0]["content"] == "回复简洁"
    # 含 superseded 时能看到两条（旧的那条带标记）
    all_rows = await list_preferences(db, include_superseded=True)
    assert len(all_rows) == 2
    superseded = [r for r in all_rows if "superseded" in r["tags"]]
    assert len(superseded) == 1
    assert superseded[0]["content"] == "回复详细"


async def test_snippet_excludes_superseded():
    db = await _new_db()
    await capture_preference(db, "回复详细", category="回复风格")
    await capture_preference(db, "回复简洁", category="回复风格")
    snippet = await build_preference_snippet(db)
    assert "回复简洁" in snippet
    assert "回复详细" not in snippet
    assert "## 用户偏好" in snippet


async def test_project_isolation():
    db = await _new_db()
    await capture_preference(db, "全局偏好")  # project_id=None
    await capture_preference(db, "项目偏好", project_id="proj-a")
    # 全局对所有项目可见
    global_only = await list_preferences(db, project_id="proj-x")
    assert len(global_only) == 1
    assert global_only[0]["content"] == "全局偏好"
    # proj-a 看到自己 + 全局
    proj_a = await list_preferences(db, project_id="proj-a")
    assert len(proj_a) == 2
    contents = {r["content"] for r in proj_a}
    assert contents == {"全局偏好", "项目偏好"}


async def test_delete_removes():
    db = await _new_db()
    node = await capture_preference(db, "临时偏好")
    assert await delete_preference(db, node["id"]) is True
    assert await list_preferences(db) == []
    # 不存在的 id 删除返回 False
    assert await delete_preference(db, "pref_nonexistent") is False


async def test_snippet_empty_when_no_prefs():
    db = await _new_db()
    assert await build_preference_snippet(db) == ""
