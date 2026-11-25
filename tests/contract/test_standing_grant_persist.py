"""contract: 聊天「始终允许」须持久化为 standing grant（跨重启仍生效）

背景（2026-09-03 复查修复）：standing_grants 表与读取侧（is_standing_granted 已接入
execute_confirmable 自动放行）在 commit c023f72 落地，但写入侧长期只有 Inbox 审批路径在
remember=True 时调用；聊天对话里的「始终允许」复选框（主审批面）只调 add_session_allowed_tool
（内存态、进程重启即失效），导致「持久化重复授权」功能形同虚设——用户勾了「始终允许」，
重启后照样每次弹窗。

本文件锁定修复后的不变量：
- persist_remember 写入后，is_standing_granted 返回 True（读取侧能命中 → 跳过确认弹窗）
- 不同工具不受影响（fail-closed）
- 同一工具重复「始终允许」续期而非插入重复行
- revoke 后读取侧立即失效
- list_grants 仅返回当前有效项
"""
import os
import sys

import aiosqlite
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from repositories.schema import SCHEMA_SQL  # noqa: E402
from services.standing_grants import (  # noqa: E402
    is_granted,
    list_grants,
    persist_remember,
    revoke,
)


async def _new_db():
    db = await aiosqlite.connect(":memory:")
    await db.executescript(SCHEMA_SQL)
    await db.commit()
    return db


async def test_remember_persists_and_is_consulted():
    db = await _new_db()
    # 聊天路径勾选「始终允许」→ 应持久化 standing grant
    await persist_remember(db, "write_file")
    # 读取侧（execute_confirmable 用它判断是否跳过确认）必须命中
    assert await is_granted(db, "write_file") is True
    assert await is_granted(db, "write_file") is True


async def test_other_tools_not_granted_fail_closed():
    db = await _new_db()
    await persist_remember(db, "write_file")
    # 未授权的工具必须 fail-closed（返回 False，仍弹确认）
    assert await is_granted(db, "run_command") is False
    assert await is_granted(db, "delete_file") is False


async def test_repeated_remember_renews_not_duplicates():
    db = await _new_db()
    await persist_remember(db, "run_command")
    await persist_remember(db, "run_command")  # 同 scope 续期
    rows = await list_grants(db)
    assert len(rows) == 1, "同 scope 重复「始终允许」应续期而非插重复行"
    assert rows[0]["tool_name"] == "run_command"


async def test_revoke_makes_grant_inactive():
    db = await _new_db()
    res = await persist_remember(db, "write_file")
    # persist_remember 内部调 grant，取回 id 验证 revoke
    gid = (await list_grants(db))[0]["id"]
    assert await is_granted(db, "write_file") is True
    assert await revoke(db, gid) is True
    # 撤销后立即失效（读取侧 fail-closed）
    assert await is_granted(db, "write_file") is False
    assert len(await list_grants(db)) == 0


async def test_list_grants_only_active_by_default():
    db = await _new_db()
    await persist_remember(db, "write_file")
    await persist_remember(db, "run_command")
    active = await list_grants(db)
    assert len(active) == 2
    # 不复活已撤销项
    gid = active[0]["id"]
    await revoke(db, gid)
    assert len(await list_grants(db)) == 1


async def test_empty_project_is_wildcard_scope():
    """persist_remember 默认 project_id=''（通配），任何 project 下都应命中。"""
    db = await _new_db()
    await persist_remember(db, "write_file")  # project_id 默认空串
    assert await is_granted(db, "write_file", project_id="some-project") is True
    assert await is_granted(db, "write_file", project_id="") is True
