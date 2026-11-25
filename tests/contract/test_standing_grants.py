"""contract: Scoped standing grants —— 持久化重复授权（对标 OpenClaw）

覆盖 fail-closed 语义（过期 / 撤销 / 异常一律拒绝）、scope 隔离与通配、
续期行为，以及 tool_service / approval_inbox 两处接入点。
"""
import os
import sys

import aiosqlite
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from repositories.schema import SCHEMA_SQL  # noqa: E402
from services.standing_grants import (  # noqa: E402
    grant,
    is_granted,
    list_grants,
    revoke,
    revoke_scope,
)


async def _new_db():
    """创建带完整 schema 的内存库（含 standing_grants）。"""
    db = await aiosqlite.connect(":memory:")
    await db.executescript(SCHEMA_SQL)
    await db.commit()
    return db


async def _expire_all(db) -> None:
    """把所有授权改成已过期——用于验证 fail-closed。"""
    await db.execute("UPDATE standing_grants SET expires_at = 1")
    await db.commit()


# ---- 基本授予 / 校验 ----

@pytest.mark.asyncio
async def test_grant_then_is_granted():
    """授予后同 scope 应命中。"""
    db = await _new_db()
    res = await grant(db, "write_file", agent_id="hermes", project_id="p1", ttl_sec=3600)
    assert res["ok"] is True
    assert res["renewed"] is False
    assert await is_granted(db, "write_file", "hermes", "p1") is True
    await db.close()


@pytest.mark.asyncio
async def test_grant_rejects_empty_tool_name():
    """工具名为空必须拒绝（避免误建通配授权）。"""
    db = await _new_db()
    res = await grant(db, "   ", ttl_sec=3600)
    assert res["ok"] is False
    assert await is_granted(db, "") is False
    await db.close()


# ---- fail-closed ----

@pytest.mark.asyncio
async def test_expired_grant_is_denied():
    """过期授权必须失效——fail-closed，不默认放行。"""
    db = await _new_db()
    await grant(db, "write_file", ttl_sec=3600)
    assert await is_granted(db, "write_file") is True
    await _expire_all(db)
    assert await is_granted(db, "write_file") is False, "过期授权不得继续生效"
    await db.close()


@pytest.mark.asyncio
async def test_revoked_grant_is_denied():
    """撤销后必须立即失效。"""
    db = await _new_db()
    res = await grant(db, "delete_file", ttl_sec=3600)
    assert await is_granted(db, "delete_file") is True
    assert await revoke(db, res["id"]) is True
    assert await is_granted(db, "delete_file") is False, "撤销后不得继续生效"
    await db.close()


@pytest.mark.asyncio
async def test_revoke_scope_denies_all_matching():
    """按 scope 撤销应命中全部精确匹配项。"""
    db = await _new_db()
    await grant(db, "run_command", agent_id="codex", ttl_sec=3600)
    await grant(db, "run_command", agent_id="codex", ttl_sec=7200)
    assert await is_granted(db, "run_command", "codex") is True
    n = await revoke_scope(db, "run_command", agent_id="codex")
    assert n == 1, "续期不产生重复行，故应只撤销 1 条"
    assert await is_granted(db, "run_command", "codex") is False
    await db.close()


@pytest.mark.asyncio
async def test_is_granted_fail_closed_on_db_error():
    """查询异常必须视为未授权——绝不因报错而放行。"""

    class _BrokenDB:
        async def execute(self, *a, **k):
            raise RuntimeError("db down")

    assert await is_granted(_BrokenDB(), "write_file") is False


@pytest.mark.asyncio
async def test_is_granted_no_db_is_false():
    """无 db（如某些测试路径）时不得放行。"""
    from services.tools.tool_service import is_standing_granted
    assert await is_standing_granted(None, "write_file") is False


# ---- scope 隔离与通配 ----

@pytest.mark.asyncio
async def test_scope_isolation_by_tool():
    """不同工具互不通用。"""
    db = await _new_db()
    await grant(db, "write_file", ttl_sec=3600)
    assert await is_granted(db, "write_file") is True
    assert await is_granted(db, "delete_file") is False
    await db.close()


@pytest.mark.asyncio
async def test_scope_isolation_by_agent():
    """指定 agent 的授权不得被其他 agent 冒用。"""
    db = await _new_db()
    await grant(db, "run_command", agent_id="codex", ttl_sec=3600)
    assert await is_granted(db, "run_command", "codex") is True
    assert await is_granted(db, "run_command", "claude") is False
    await db.close()


@pytest.mark.asyncio
async def test_wildcard_agent_matches_any():
    """agent_id 为空串 = 通配，匹配任意 agent。"""
    db = await _new_db()
    await grant(db, "write_file", agent_id="", ttl_sec=3600)
    assert await is_granted(db, "write_file", "codex") is True
    assert await is_granted(db, "write_file", "claude") is True
    assert await is_granted(db, "write_file", "") is True
    await db.close()


@pytest.mark.asyncio
async def test_project_scope_isolation():
    """项目维度隔离，空串作通配。"""
    db = await _new_db()
    await grant(db, "write_file", project_id="p1", ttl_sec=3600)
    assert await is_granted(db, "write_file", "", "p1") is True
    assert await is_granted(db, "write_file", "", "p2") is False
    await db.close()


# ---- 续期与上限 ----

@pytest.mark.asyncio
async def test_second_grant_renews_not_duplicates():
    """同 scope 再授权应续期而非插入重复行。"""
    db = await _new_db()
    await grant(db, "write_file", ttl_sec=3600)
    res = await grant(db, "write_file", ttl_sec=7200)
    assert res["ok"] is True
    assert res["renewed"] is True
    assert len(await list_grants(db)) == 1, "续期不得产生重复授权行"
    await db.close()


@pytest.mark.asyncio
async def test_ttl_capped_at_max():
    """TTL 超过上限须被截断，防止事实上的永久授权。"""
    import time as _time
    from config import STANDING_GRANT_MAX_TTL_SEC
    db = await _new_db()
    before = _time.time()
    res = await grant(db, "write_file", ttl_sec=STANDING_GRANT_MAX_TTL_SEC * 10)
    assert res["ok"] is True
    # expires_at 是绝对时间戳，与授权前时刻相减得到实际窗口
    window = res["expires_at"] - before
    assert window <= STANDING_GRANT_MAX_TTL_SEC + 5, \
        f"TTL 应被截断到上限 {STANDING_GRANT_MAX_TTL_SEC}s，实际窗口 {window:.0f}s"
    await db.close()


@pytest.mark.asyncio
async def test_list_grants_default_only_active():
    """默认只列出当前有效项。"""
    db = await _new_db()
    await grant(db, "write_file", ttl_sec=3600)
    await grant(db, "delete_file", ttl_sec=3600)
    await revoke_scope(db, "delete_file")
    assert len(await list_grants(db)) == 1
    assert len(await list_grants(db, include_revoked=True)) == 2
    await db.close()


# ---- 接入点集成 ----

@pytest.mark.asyncio
async def test_tool_service_wrapper_hits_grant():
    """tool_service.is_standing_granted 应命中已授予的授权。"""
    from services.tools.tool_service import is_standing_granted
    db = await _new_db()
    assert await is_standing_granted(db, "write_file", "hermes", "p1") is False
    await grant(db, "write_file", agent_id="hermes", project_id="p1", ttl_sec=3600)
    assert await is_standing_granted(db, "write_file", "hermes", "p1") is True
    await db.close()


@pytest.mark.asyncio
async def test_inbox_remember_persists_standing_grant():
    """approval_inbox 的 remember 路径应持久化一条 grant（重启后仍生效）。"""
    from services.approval_inbox import ApprovalInboxService
    db = await _new_db()
    svc = ApprovalInboxService(db)
    await svc._persist_standing_grant("write_file", "p1")
    grants = await list_grants(db)
    assert len(grants) == 1
    assert grants[0]["tool_name"] == "write_file"
    assert grants[0]["project_id"] == "p1"
    assert grants[0]["source"] == "inbox_approval"
    assert grants[0]["is_active"] is True
    await db.close()


@pytest.mark.asyncio
async def test_inbox_persist_failure_is_non_fatal():
    """持久化失败不得影响审批结果（副作用已执行成功）。"""
    from services.approval_inbox import ApprovalInboxService

    class _BrokenDB:
        async def execute(self, *a, **k):
            raise RuntimeError("db down")

        async def commit(self):
            pass

    svc = ApprovalInboxService(_BrokenDB())
    # 不应抛异常
    await svc._persist_standing_grant("write_file", "p1")


# ---- 参数模式匹配（对标主流 Tool(specifier)）----

def test_matches_pattern_prefix_with_space():
    """`git diff *` 前缀含尾空格——不误匹配 git diff-index。"""
    from services.standing_grants import matches_pattern
    assert matches_pattern("git diff *", "git diff --stat") is True
    assert matches_pattern("git diff *", "git diff-index") is False


def test_matches_pattern_prefix_without_space():
    """`git diff*` 前缀更短，会匹配 git diff-index（空格的意义所在）。"""
    from services.standing_grants import matches_pattern
    assert matches_pattern("git diff*", "git diff-index") is True
    assert matches_pattern("git diff*", "git diff --stat") is True


def test_matches_pattern_exact_and_empty():
    """无 * 为精确匹配；空模式匹配全部。"""
    from services.standing_grants import matches_pattern
    assert matches_pattern("rm -rf /", "rm -rf /") is True
    assert matches_pattern("rm -rf /", "rm -rf /tmp") is False
    assert matches_pattern("", "anything at all") is True


@pytest.mark.asyncio
async def test_pattern_rules_isolate_by_arguments():
    """同一工具可按参数模式分别授权。"""
    db = await _new_db()
    await grant(db, "run_command", pattern="git diff *", ttl_sec=3600)
    assert await is_granted(db, "run_command", subject="git diff --stat") is True
    assert await is_granted(db, "run_command", subject="git status") is False
    await db.close()


# ---- deny 优先（对标主流）----

@pytest.mark.asyncio
async def test_deny_takes_precedence_over_allow():
    """宽 deny 必须压过窄 allow——deny 不可携带 allowlist 例外。"""
    db = await _new_db()
    await grant(db, "run_command", pattern="git *", ttl_sec=3600)
    await grant(db, "run_command", pattern="git push *", rule_type="deny", ttl_sec=3600)
    assert await is_granted(db, "run_command", subject="git status") is True
    assert await is_granted(db, "run_command", subject="git push origin main") is False
    await db.close()


@pytest.mark.asyncio
async def test_revoked_deny_no_longer_blocks():
    """撤销 deny 后，原本被压制的 allow 应恢复生效。"""
    db = await _new_db()
    await grant(db, "run_command", pattern="git *", ttl_sec=3600)
    deny = await grant(db, "run_command", pattern="git push *",
                       rule_type="deny", ttl_sec=3600)
    assert await is_granted(db, "run_command", subject="git push origin") is False
    await revoke(db, deny["id"])
    assert await is_granted(db, "run_command", subject="git push origin") is True
    await db.close()


@pytest.mark.asyncio
async def test_expired_deny_no_longer_blocks():
    """过期的 deny 不得继续压制（fail-closed 只针对放行侧）。"""
    db = await _new_db()
    await grant(db, "run_command", pattern="git *", ttl_sec=3600)
    await grant(db, "run_command", pattern="git push *", rule_type="deny", ttl_sec=3600)
    await db.execute(
        "UPDATE standing_grants SET expires_at=1 WHERE rule_type='deny'")
    await db.commit()
    assert await is_granted(db, "run_command", subject="git push origin") is True
    await db.close()


@pytest.mark.asyncio
async def test_unknown_rule_type_falls_back_to_deny():
    """非法 rule_type 必须回退到 deny（安全侧默认），绝不能变成放行。"""
    db = await _new_db()
    res = await grant(db, "run_command", rule_type="alow", ttl_sec=3600)  # 故意拼错
    assert res["ok"] is True
    assert res["rule_type"] == "deny"
    assert await is_granted(db, "run_command", subject="anything") is False
    await db.close()


@pytest.mark.asyncio
async def test_deny_scope_isolation_by_agent():
    """deny 也按 agent 隔离，不被其他 agent 的调用误伤或误用。"""
    db = await _new_db()
    await grant(db, "run_command", agent_id="codex", pattern="rm *",
                rule_type="deny", ttl_sec=3600)
    await grant(db, "run_command", agent_id="claude", pattern="rm *", ttl_sec=3600)
    assert await is_granted(db, "run_command", "codex", subject="rm -rf /") is False
    assert await is_granted(db, "run_command", "claude", subject="rm -rf /") is True
    await db.close()


# ---- 参数主体提取与端到端 ----

def test_grant_subject_extraction():
    """从工具参数提取待匹配主体。"""
    from services.tools.tool_service import _grant_subject
    assert _grant_subject({"command": "git status"}) == "git status"
    assert _grant_subject({"path": "/tmp/a.txt"}) == "/tmp/a.txt"
    assert _grant_subject({"url": "https://x.com"}) == "https://x.com"
    assert _grant_subject({}) == ""
    assert _grant_subject(None) == ""


@pytest.mark.asyncio
async def test_tool_service_matches_command_arguments():
    """端到端：run_command 的命令串参与模式匹配。"""
    from services.tools.tool_service import is_standing_granted
    db = await _new_db()
    await grant(db, "run_command", pattern="git diff *", ttl_sec=3600)
    assert await is_standing_granted(
        db, "run_command", arguments={"command": "git diff --stat"}) is True
    assert await is_standing_granted(
        db, "run_command", arguments={"command": "git diff-index"}) is False
    await db.close()


@pytest.mark.asyncio
async def test_purge_expired_removes_old_records():
    """清理只删除超过保留窗口的过期/撤销记录，保留期内仍可审计。"""
    from services.standing_grants import purge_expired
    db = await _new_db()
    await grant(db, "write_file", ttl_sec=3600)
    assert await purge_expired(db) == 0, "保留期内的过期记录不应被立即清除"
    await _expire_all(db)
    # 过期时间为 1（远早于保留窗口起点）→ 应被清除
    import time as _time
    from config import STANDING_GRANT_MAX_TTL_SEC
    await db.execute("UPDATE standing_grants SET expires_at=?",
                     (_time.time() - STANDING_GRANT_MAX_TTL_SEC - 10,))
    await db.commit()
    assert await purge_expired(db) == 1
    await db.close()
