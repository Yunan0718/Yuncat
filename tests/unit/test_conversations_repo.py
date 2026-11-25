"""tests/unit/test_conversations_repo.py —— Phase 49-C: 消息版本链单元测试"""
import pytest

from repositories import insert_conversation, edit_conversation, get_conversation_versions


@pytest.mark.asyncio
async def test_edit_conversation_creates_version(db):
    """edit_conversation 基于原消息创建新版本并建立版本链。"""
    session_id = "s1"
    orig_id = await insert_conversation(
        db, session_id, "user", "hello", agent_id="hermes",
    )
    await db.commit()

    result = await edit_conversation(db, orig_id, "hello edited")
    new_id = result["conv_id"]
    root_id = result["root_id"]

    assert new_id != orig_id
    assert root_id == orig_id

    cursor = await db.execute(
        "SELECT edited_to FROM conversations WHERE id=?", (orig_id,)
    )
    assert (await cursor.fetchone())[0] == new_id

    cursor = await db.execute(
        "SELECT edited_from, variant_of, version_index FROM conversations WHERE id=?",
        (new_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == orig_id
    assert row[1] == orig_id
    assert row[2] == 1


@pytest.mark.asyncio
async def test_edit_conversation_multiple_versions(db):
    """多次编辑同一条消息产生递增 version_index 与正确边。"""
    orig_id = await insert_conversation(
        db, "s2", "user", "v0", agent_id="hermes",
    )
    await db.commit()

    r1 = await edit_conversation(db, orig_id, "v1")
    r2 = await edit_conversation(db, orig_id, "v2")

    assert r1["root_id"] == orig_id
    assert r2["root_id"] == orig_id

    tree = await get_conversation_versions(db, orig_id)
    assert tree["root_id"] == orig_id
    assert len(tree["nodes"]) == 3
    # 原消息 -> v1 (edited_from), 原消息 -> v2 (edited_from + edited_to)
    assert len(tree["edges"]) == 3
    assert any(e["from"] == orig_id and e["to"] == r1["conv_id"] for e in tree["edges"])
    assert any(e["from"] == orig_id and e["to"] == r2["conv_id"] for e in tree["edges"])


@pytest.mark.asyncio
async def test_get_conversation_versions_unknown(db):
    """查询不存在的消息返回空树。"""
    tree = await get_conversation_versions(db, "nonexistent")
    assert tree["root_id"] == "nonexistent"
    assert tree["nodes"] == []
    assert tree["edges"] == []


@pytest.mark.asyncio
async def test_edit_conversation_not_found(db):
    """编辑不存在的消息抛出 ValueError。"""
    with pytest.raises(ValueError):
        await edit_conversation(db, "missing", "x")
