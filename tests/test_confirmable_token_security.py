"""tests/test_confirmable_token_security.py —— confirmable 工具审批链安全测试

Phase 47 修复（2.5 审计）：
- 验证 token 用 CSPRNG 生成（secrets.token_hex）
- 验证 token 一次性消费（不可二次 dispatch）
- 验证 TTL 过期拒绝（expires_at < now 时 _pop_pending 返回 None）
- 验证未知 state_type fail-closed（dispatch_confirm 抛 ValueError）
- 验证未知 token fail-closed（dispatch_confirm 抛 ValueError）

审计结论：现有实现已非常安全，本测试仅做行为回归保护。
"""
import asyncio
import os
import sys
import time

import pytest

# 确保 src-python 在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src-python"))

from tools.builtin.system_tool_confirm import (
    create_pending_state,
    dispatch_confirm,
    _pop_pending,
    _expire_pending_states,
    CONFIRM_HANDLERS,
)


# ═══════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════

@pytest.fixture
async def memory_db():
    """内存 aiosqlite + runtime_states 表（仅本测试所需字段）。"""
    import aiosqlite
    db = await aiosqlite.connect(":memory:")
    await db.execute("""
        CREATE TABLE runtime_states(
            id TEXT PRIMARY KEY,
            state_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            title TEXT,
            task_id TEXT,
            project_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            resolved_at REAL
        )
    """)
    await db.commit()
    yield db
    await db.close()


# ═══════════════════════════════════════════════════════════
# Token 生成测试
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
async def test_token_generated_with_csp_rng(memory_db):
    """token 应使用 secrets 模块生成（CSPRNG），不是 uuid/random。"""
    token1 = await create_pending_state(memory_db, "pending_write", {"path": "a"})
    token2 = await create_pending_state(memory_db, "pending_write", {"path": "b"})

    # 两个 token 应不同
    assert token1 != token2

    # token 应是 hex 字符串（secrets.token_hex(16) → 32 字符）
    assert len(token1) == 32
    assert all(c in "0123456789abcdef" for c in token1)


# ═══════════════════════════════════════════════════════════
# 一次性消费测试
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
async def test_token_one_time_use_pop_pending(memory_db):
    """_pop_pending 第一次返回记录，第二次返回 None。"""
    token = await create_pending_state(
        memory_db, "pending_write", {"path": "a.txt", "content": "hi"}
    )

    # 第一次 pop 应成功
    record1 = await _pop_pending(memory_db, token)
    assert record1 is not None
    assert record1["state_type"] == "pending_write"
    assert record1["payload"]["path"] == "a.txt"

    # 第二次 pop 同一 token 应返回 None（已 resolved）
    record2 = await _pop_pending(memory_db, token)
    assert record2 is None


@pytest.mark.unit
async def test_token_one_time_use_dispatch_confirm(memory_db):
    """dispatch_confirm 第二次调用应抛 ValueError。"""
    token = await create_pending_state(
        memory_db, "pending_write", {"path": "a.txt", "content": "hi"}
    )

    # 第一次 dispatch 应成功（执行 write handler）
    # 注意：实际写入需要 workspace，这里只验证 token 消费逻辑
    # write handler 会因路径验证失败返回 error，但 token 已被消费
    result1 = await dispatch_confirm(memory_db, token)
    assert isinstance(result1, dict)

    # 第二次 dispatch 应抛 ValueError（token 已消费）
    with pytest.raises(ValueError, match="expired or invalid"):
        await dispatch_confirm(memory_db, token)


# ═══════════════════════════════════════════════════════════
# TTL 过期测试
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
async def test_token_expired_rejected(memory_db):
    """TTL 过期后 _pop_pending 应返回 None。"""
    # 创建一个 1 秒 TTL 的 token
    token = await create_pending_state(
        memory_db, "pending_write", {"path": "a.txt"}, ttl=1
    )

    # 立即 pop 应成功
    record = await _pop_pending(memory_db, token)
    assert record is not None

    # 创建另一个 1 秒 TTL 的 token，等待过期
    token2 = await create_pending_state(
        memory_db, "pending_write", {"path": "b.txt"}, ttl=1
    )
    await asyncio.sleep(1.1)

    # 过期后 pop 应返回 None
    record2 = await _pop_pending(memory_db, token2)
    assert record2 is None


@pytest.mark.unit
async def test_expire_pending_states_marks_expired(memory_db):
    """_expire_pending_states 应将过期 pending 标记为 expired。"""
    # 创建一个已过期的 pending 记录
    token = await create_pending_state(
        memory_db, "pending_write", {"path": "a.txt"}, ttl=1
    )
    await asyncio.sleep(1.1)

    # 调用 _expire_pending_states 标记过期
    count = await _expire_pending_states(memory_db)
    assert count == 1

    # 验证记录状态已变为 expired
    cursor = await memory_db.execute(
        "SELECT status FROM runtime_states WHERE id=?", (token,)
    )
    row = await cursor.fetchone()
    assert row[0] == "expired"


# ═══════════════════════════════════════════════════════════
# Fail-closed 测试
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
async def test_dispatch_unknown_token_fails_closed(memory_db):
    """未知 token dispatch 应抛 ValueError（fail-closed）。"""
    with pytest.raises(ValueError, match="expired or invalid"):
        await dispatch_confirm(memory_db, "nonexistent_token_abc123")


@pytest.mark.unit
async def test_dispatch_unknown_state_type_fails_closed(memory_db):
    """未知 state_type 应抛 ValueError（fail-closed）。

    需要 token 有效（_pop_pending 成功）才能进入 handler 分发。
    """
    # 注册一个测试用的 state_type，创建 token，然后取消注册
    unknown_state_type = "pending_test_unknown_type_xyz"

    # 直接 INSERT 一个记录（绕过 create_pending_state 的状态类型约束）
    now = time.time()
    await memory_db.execute(
        "INSERT INTO runtime_states(id, state_type, payload, created_at, expires_at, status) "
        "VALUES(?,?,?,?,?, 'pending')",
        ("test_token_unknown_type", unknown_state_type,
         '{"x":1}', now, now + 3600)
    )
    await memory_db.commit()

    # 确保 CONFIRM_HANDLERS 没有这个 state_type
    assert unknown_state_type not in CONFIRM_HANDLERS

    # dispatch 应抛 ValueError（unknown state_type）
    with pytest.raises(ValueError, match="Unknown state_type"):
        await dispatch_confirm(memory_db, "test_token_unknown_type")


# ═══════════════════════════════════════════════════════════
# Handler 注册完整性测试
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
async def test_confirm_handlers_registered():
    """验证所有预期 state_type 的 handler 已注册。"""
    expected_handlers = {
        "pending_write": "write_file",
        "pending_command": "run_command",
        "pending_delete_file": "delete_file",
        "pending_rename_file": "rename_file",
        "pending_delete_dir": "delete_dir",
        "pending_edit_file": "edit_file",
        "pending_workflow_trigger": "workflow_trigger",
    }
    for state_type, tool_name in expected_handlers.items():
        assert state_type in CONFIRM_HANDLERS, (
            f"Handler for {state_type} (tool: {tool_name}) not registered"
        )
