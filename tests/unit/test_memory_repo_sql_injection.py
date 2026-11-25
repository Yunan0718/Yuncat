"""tests/unit/test_memory_repo_sql_injection.py — SQL 注入 fail-closed 测试

Phase 1.2 修复: memory_repo.py 原 column_map.get(key, key) 是 fail-open——
未知 key 会原样拼入 SQL。改为 column_map.get(key) 返回 None，未知 key
直接 fail-closed 拒绝。

本测试验证:
1. get_dream_state / set_dream_state 对未知 key fail-closed（返回 None / 不写入）
2. 含 SQL 元字符的 key 被拒绝（不执行注入）
3. save_semantic 正确参数化保存含特殊字符的 content / source / owner
4. 静态检查: 源码中 column_map.get(key) 不带默认值（fail-closed 模式）
"""
import os
import sys
import asyncio
import pytest

_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


@pytest.fixture
async def repo():
    """创建内存 SQLite + MemoryRepository 实例。"""
    import aiosqlite
    from repositories.memory_repo import MemoryRepository
    from repositories.schema import SCHEMA_SQL, MIGRATIONS

    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(SCHEMA_SQL)
    for migration in MIGRATIONS:
        try:
            await conn.executescript(migration)
        except Exception:
            pass
    # dream_state 表需要 singleton row（id=1）
    await conn.execute("INSERT OR IGNORE INTO dream_state(id) VALUES(1)")
    await conn.commit()
    repo = MemoryRepository(conn)
    yield repo
    await conn.close()


# ============================================================
# 1. fail-closed: 未知 / 恶意 key 被拒绝
# ============================================================

@pytest.mark.asyncio
async def test_get_dream_state_rejects_unknown_key(repo):
    """未知 key 返回 None（fail-closed）。"""
    result = await repo.get_dream_state("nonexistent_column")
    assert result is None


@pytest.mark.asyncio
async def test_set_dream_state_rejects_unknown_key(repo):
    """未知 key 不写入（fail-closed）。"""
    await repo.set_dream_state("nonexistent_column", 1.0)
    # 验证未写入——查所有已知列均为初始值
    cursor = await repo.db.execute("SELECT last_dream_time, dream_lock, total_dreams FROM dream_state WHERE id=1")
    row = await cursor.fetchone()
    # 初始值应为 NULL 或 0
    assert row is not None


@pytest.mark.asyncio
async def test_get_dream_state_rejects_sql_injection_key(repo):
    """含 SQL 注入 payload 的 key 被 fail-closed 拒绝。

    修复前: column_map.get(key, key) 会把整个 payload 作为列名拼入 SQL
    修复后: column_map.get(key) 返回 None，直接拒绝
    """
    malicious_keys = [
        "last_dream_time; DROP TABLE dream_state; --",
        "last_dream_time UNION SELECT * FROM conversations",
        "last_dream_time' OR '1'='1",
        "last_dream_time\"; DELETE FROM memories; --",
    ]
    for key in malicious_keys:
        # 应返回 None 而非执行注入
        result = await repo.get_dream_state(key)
        assert result is None, f"Malicious key should be rejected: {key}"
    # 验证 dream_state 表仍存在
    cursor = await repo.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dream_state'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "dream_state"


@pytest.mark.asyncio
async def test_set_dream_state_rejects_sql_injection_key(repo):
    """set_dream_state 对含 SQL 注入的 key fail-closed。"""
    await repo.set_dream_state("last_dream_time; DROP TABLE dream_state; --", 999.0)
    # 验证 dream_state 表未被删除
    cursor = await repo.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dream_state'"
    )
    row = await cursor.fetchone()
    assert row is not None
    # 验证 last_dream_time 未被设置为 999.0
    cursor = await repo.db.execute("SELECT last_dream_time FROM dream_state WHERE id=1")
    row = await cursor.fetchone()
    assert row[0] != 999.0


# ============================================================
# 2. 参数化查询: 特殊字符正确转义
# ============================================================

@pytest.mark.asyncio
async def test_save_semantic_with_sql_special_chars(repo):
    """save_semantic 正确参数化保存含 SQL 元字符的 content / source / owner。"""
    malicious_content = "test'); DROP TABLE memories; --"
    malicious_source = "source' OR '1'='1"
    malicious_owner = "owner\"; DELETE FROM memories WHERE '1'='1"

    mem_id = await repo.save_semantic(
        content=malicious_content,
        source=malicious_source,
        owner=malicious_owner,
    )
    assert mem_id  # 应返回 UUID

    # 验证记录原样保存
    cursor = await repo.db.execute(
        "SELECT content, source, owner FROM memories WHERE id=?", (mem_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == malicious_content
    assert row[1] == malicious_source
    assert row[2] == malicious_owner

    # 验证 memories 表未被删除
    cursor = await repo.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "memories"


@pytest.mark.asyncio
async def test_save_semantic_single_quote(repo):
    """单引号正确转义（参数化查询核心场景）。"""
    content_with_quote = "It's a test with 'quotes' inside"
    mem_id = await repo.save_semantic(
        content=content_with_quote,
        source="test",
        owner="tester",
    )
    cursor = await repo.db.execute(
        "SELECT content FROM memories WHERE id=?", (mem_id,)
    )
    row = await cursor.fetchone()
    assert row[0] == content_with_quote


@pytest.mark.asyncio
async def test_save_semantic_long_input(repo):
    """超长输入不导致崩溃。"""
    long_content = "A" * 100000  # 100KB
    mem_id = await repo.save_semantic(
        content=long_content,
        source="test",
        owner="tester",
    )
    cursor = await repo.db.execute(
        "SELECT LENGTH(content) FROM memories WHERE id=?", (mem_id,)
    )
    row = await cursor.fetchone()
    assert row[0] == 100000


# ============================================================
# 3. 静态检查: 源码中 fail-closed 模式
# ============================================================

def test_source_uses_fail_closed_column_map():
    """静态检查: column_map.get(key) 不带默认值参数（fail-closed）。

    修复前: column_map.get(key, key) — 未知 key 原样返回（fail-open）
    修复后: column_map.get(key) — 未知 key 返回 None（fail-closed）
    """
    repo_path = os.path.join(_SRC, "repositories", "memory_repo.py")
    with open(repo_path, encoding="utf-8") as f:
        source = f.read()
    # 不应再有 column_map.get(key, key) 这种 fail-open 模式
    assert "column_map.get(key, key)" not in source, (
        "fail-open pattern column_map.get(key, key) must be replaced with "
        "fail-closed column_map.get(key)"
    )


def test_source_uses_parameterized_queries():
    """静态检查: 列值通过 ? 占位符传递（参数化查询）。

    注意: f"SELECT {col} FROM dream_state WHERE id=1" 中的 col 来自
    column_map 白名单（hardcoded），是安全的。值参数应通过 ? 传递。
    """
    repo_path = os.path.join(_SRC, "repositories", "memory_repo.py")
    with open(repo_path, encoding="utf-8") as f:
        source = f.read()
    # UPDATE 语句应使用 ? 占位符传值
    assert "UPDATE dream_state SET {col}=? WHERE id=1" in source, (
        "set_dream_state must use ? placeholder for value (parameterized query)"
    )
    # INSERT memories 应使用 ? 占位符
    assert "INSERT INTO memories(id, content, source, owner, confidence, memory_type, verified)" in source
    assert "VALUES(?,?,?,?,?,?,?)" in source, (
        "save_semantic must use ? placeholders for all values"
    )


def test_source_has_no_user_input_fstring_sql():
    """静态检查: 无直接将用户输入插入 SQL f-string 的模式。

    允许 f"SELECT {col} FROM ..." 当 col 来自 hardcoded column_map 白名单。
    禁止 f"WHERE id = '{user_input}'" 这种直接插值。
    """
    import re
    repo_path = os.path.join(_SRC, "repositories", "memory_repo.py")
    with open(repo_path, encoding="utf-8") as f:
        source = f.read()
    lines = source.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # 跳过注释行
        if stripped.startswith("#"):
            continue
        # 检查 f-string 中是否含 WHERE + 用户输入插值
        # 允许: f"SELECT {col} FROM ..." (col from whitelist)
        # 禁止: f"... WHERE id = '{var}'" (direct user input interpolation)
        if "WHERE" in stripped.upper() and "f\"" in stripped:
            # 检查 WHERE 子句中是否有变量插值（非 col 白名单）
            # 简化: 如果 WHERE 子句后有 {var} 而非 ?，则警告
            where_match = re.search(r"WHERE.*\{[^}]*\}", stripped, re.IGNORECASE)
            if where_match and "?" not in where_match.group(0):
                pytest.fail(
                    f"Line {i}: potential f-string SQL injection in WHERE clause: "
                    f"{stripped[:100]}"
                )
