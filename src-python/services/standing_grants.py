"""services/standing_grants.py —— Scoped standing grants（持久化授权规则）

重复 / 定时任务（cron、automation）在用户**一次审批**后于有效期内复用该授权，
不必每次弹窗；到期或撤销立即失效。

与既有三套机制的分工：
- `runtime_states` + `system_tool_confirm`：一次性确认令牌，每次调用都要弹窗
- `tool_service._session_allowed_tools`：内存态 session 允许列表，进程重启即失效
- **standing_grants（本模块）**：持久化、带 TTL、按 scope 匹配、可主动撤销

规则语法对标主流（Claude Code 的 `Tool(specifier)`）：
- `tool_name` = 工具名；`pattern` = 参数模式（空串 = 匹配该工具全部调用，等价裸工具名）
- 尾部 `*` 启用**前缀匹配**，且空格有意义：
  - `git diff *` → 前缀 `'git diff '`，匹配 `git diff --stat`，**不**匹配 `git diff-index`
  - `git diff*`  → 前缀 `'git diff'`，会匹配 `git diff-index`（前缀更短，更宽）

判定顺序对标主流（deny → allow，先命中者胜，**窄 allow 不能为宽 deny 开例外**）：
1. 任一命中 deny → 拒绝
2. 任一命中 allow → 通过
3. 无命中 → 拒绝（fail-closed）

Scope = (tool_name, pattern, rule_type, agent_id, project_id)。
**空串 agent_id / project_id 作通配**——匹配全部 agent / 全部项目。

安全不变量（fail-closed，绝不因异常而放行）：
- 过期（expires_at <= now）→ 视为未授权
- 已撤销（revoked_at IS NOT NULL）→ 视为未授权
- 查询抛异常 → 视为未授权
"""
import logging
import time as _time
import uuid as _uuid

from config import (
    STANDING_GRANT_MAX_TTL_SEC,
    STANDING_GRANT_TTL_SEC,
)

_log = logging.getLogger("yuncat.standing_grants")

ALLOW = "allow"
DENY = "deny"


def _norm(value: str | None) -> str:
    """scope 分量归一化——None 视为通配空串。"""
    return (value or "").strip()


def matches_pattern(pattern: str, subject: str) -> bool:
    """参数模式匹配——对标主流 `Tool(specifier)` 语法。

    - pattern 空串 → 匹配该工具的全部调用
    - pattern 以 '*' 结尾 → 前缀匹配，前缀为去掉 '*' 的剩余部分
      （故 `git diff *` 的前缀含尾空格，不会误匹配 `git diff-index`）
    - 否则 → 精确匹配
    """
    p = (pattern or "").strip()
    if not p:
        return True
    s = subject or ""
    if p.endswith("*"):
        return s.startswith(p[:-1])
    return s == p


def _row_to_dict(row) -> dict:
    """aiosqlite.Row → dict，附带 is_active 便捷字段。"""
    if hasattr(row, "keys"):
        d = {k: row[k] for k in row.keys()}
    else:
        cols = ["id", "tool_name", "pattern", "rule_type", "agent_id",
                "project_id", "granted_at", "expires_at", "revoked_at",
                "source", "grant_reason"]
        d = {c: row[i] for i, c in enumerate(cols)}
    now = _time.time()
    d["is_active"] = (
        d.get("revoked_at") is None
        and (d.get("expires_at") or 0) > now
    )
    return d


async def grant(
    db, tool_name: str, agent_id: str = "", project_id: str = "",
    ttl_sec: int | None = None, source: str = "user_approval",
    reason: str = "", pattern: str = "", rule_type: str = ALLOW,
) -> dict:
    """授予（或续期）一条规则。

    同 scope（含 pattern / rule_type）已存在**有效**规则时续期（更新 expires_at），
    不插入重复行。ttl_sec 超过 STANDING_GRANT_MAX_TTL_SEC 时截断到上限。
    rule_type 非法值一律回退为 'deny'（安全侧默认，避免手误写成宽放行）。
    """
    if not tool_name or not tool_name.strip():
        return {"ok": False, "error": "tool_name 不能为空"}
    ttl = STANDING_GRANT_TTL_SEC if ttl_sec is None else int(ttl_sec)
    if ttl <= 0:
        return {"ok": False, "error": "ttl_sec 必须为正数"}
    ttl = min(ttl, STANDING_GRANT_MAX_TTL_SEC)
    if rule_type not in (ALLOW, DENY):
        _log.warning("Unknown rule_type %r — falling back to 'deny'", rule_type)
        rule_type = DENY

    tool, agent, project = _norm(tool_name), _norm(agent_id), _norm(project_id)
    pat = (pattern or "").strip()
    now = _time.time()
    expires_at = now + ttl
    try:
        cur = await db.execute(
            "UPDATE standing_grants SET expires_at=?, source=?, grant_reason=? "
            "WHERE tool_name=? AND pattern=? AND rule_type=? "
            "  AND agent_id=? AND project_id=? "
            "  AND revoked_at IS NULL AND expires_at > ?",
            (expires_at, source, reason, tool, pat, rule_type,
             agent, project, now),
        )
        await db.commit()
        if cur.rowcount > 0:
            _log.info("Standing grant renewed: %s/%s rule=%s ttl=%ds",
                      tool, pat or "*", rule_type, ttl)
            return {"ok": True, "renewed": True, "tool_name": tool,
                    "pattern": pat, "rule_type": rule_type,
                    "agent_id": agent, "project_id": project,
                    "expires_at": expires_at}

        gid = str(_uuid.uuid4())
        await db.execute(
            "INSERT INTO standing_grants(id, tool_name, pattern, rule_type, "
            "agent_id, project_id, granted_at, expires_at, revoked_at, "
            "source, grant_reason) VALUES(?,?,?,?,?,?,?,?,NULL,?,?)",
            (gid, tool, pat, rule_type, agent, project, now, expires_at,
             source, reason),
        )
        await db.commit()
        _log.info("Standing grant created: %s/%s rule=%s ttl=%ds",
                  tool, pat or "*", rule_type, ttl)
        return {"ok": True, "renewed": False, "id": gid, "tool_name": tool,
                "pattern": pat, "rule_type": rule_type,
                "agent_id": agent, "project_id": project,
                "expires_at": expires_at}
    except Exception as e:
        _log.exception("Standing grant failed: tool=%s", tool)
        return {"ok": False, "error": str(e)}


async def persist_remember(
    db, tool_name: str, project_id: str = "", source: str = "chat_approval",
    reason: str = "用户勾选「始终允许」",
) -> None:
    """用户勾选「始终允许 / remember」时持久化一条 standing grant。

    非阻塞副作用：失败仅记日志，不影响本次审批结果（工具已执行成功）。
    与内存态 session 允许列表互补——session 列表进程重启即失效，本函数查库可跨重启，
    被 tool_service.execute_confirmable 的 is_standing_granted 命中后跳过确认弹窗。

    修复点：commit c023f72 落地了 standing_grants 表与读取侧（is_standing_granted 已接入
    execute_confirmable 自动放行），但写入侧长期只有 Inbox 审批路径在 remember=True 时调用，
    而聊天对话里的「始终允许」复选框（主审批面）只调 add_session_allowed_tool（内存态、重启即失效）。
    导致「持久化重复授权」功能形同虚设——本函数把两个入口统一为「内存 + 持久」双写。
    """
    if not tool_name or not tool_name.strip():
        return
    try:
        res = await grant(
            db, tool_name, agent_id="", project_id=_norm(project_id),
            source=source, reason=reason,
        )
        if not res.get("ok"):
            _log.warning("Standing grant persist failed for %s: %s", tool_name, res.get("error"))
    except Exception:
        _log.warning("Standing grant persist error (non-fatal)", exc_info=True)


async def is_granted(
    db, tool_name: str, agent_id: str = "", project_id: str = "",
    subject: str = "",
) -> bool:
    """判定该调用是否被**放行**。

    subject = 待匹配的参数主体（如 run_command 的命令串、write_file 的路径）。
    判定顺序 deny → allow，先命中者胜；无命中返回 False（fail-closed）。
    """
    if not tool_name or not tool_name.strip():
        return False
    tool, agent, project = _norm(tool_name), _norm(agent_id), _norm(project_id)
    try:
        rules = await _active_rules(db, tool, agent, project)
    except Exception:
        _log.debug("Standing grant lookup failed — deny by default", exc_info=True)
        return False

    for rule_type, pattern in rules:
        if rule_type == DENY and matches_pattern(pattern, subject):
            # deny 优先，且不可被更窄的 allow 开例外（对标主流语义）
            return False
    for rule_type, pattern in rules:
        if rule_type == ALLOW and matches_pattern(pattern, subject):
            return True
    return False


async def _active_rules(db, tool: str, agent: str, project: str) -> list[tuple]:
    """取出该 scope 下全部有效规则的 (rule_type, pattern)。"""
    cur = await db.execute(
        "SELECT rule_type, pattern FROM standing_grants "
        "WHERE tool_name=? AND revoked_at IS NULL AND expires_at > ? "
        "  AND (agent_id=? OR agent_id='') "
        "  AND (project_id=? OR project_id='')",
        (tool, _time.time(), agent, project),
    )
    rows = await cur.fetchall()
    return [(r[0], r[1] or "") for r in rows]


async def revoke(db, grant_id: str) -> bool:
    """按 id 撤销单条规则（软删除，保留审计痕迹）。返回是否命中。"""
    try:
        cur = await db.execute(
            "UPDATE standing_grants SET revoked_at=? "
            "WHERE id=? AND revoked_at IS NULL",
            (_time.time(), grant_id),
        )
        await db.commit()
        return cur.rowcount > 0
    except Exception:
        _log.exception("Revoke grant failed: %s", grant_id)
        return False


async def revoke_scope(
    db, tool_name: str, agent_id: str = "", project_id: str = "",
    pattern: str = "", rule_type: str | None = None,
) -> int:
    """按 scope 撤销全部匹配规则（**精确匹配，不使用通配**）。返回撤销条数。

    rule_type=None 时不限规则类型（allow + deny 一并撤销）。
    """
    clauses = [
        "tool_name=?", "pattern=?", "agent_id=?", "project_id=?",
        "revoked_at IS NULL",
    ]
    params = [_norm(tool_name), (pattern or "").strip(),
              _norm(agent_id), _norm(project_id)]
    if rule_type is not None:
        clauses.append("rule_type=?")
        params.append(rule_type)
    try:
        cur = await db.execute(
            "UPDATE standing_grants SET revoked_at=? WHERE "
            + " AND ".join(clauses),
            [_time.time()] + params,
        )
        await db.commit()
        return cur.rowcount or 0
    except Exception:
        _log.exception("Revoke grant scope failed: %s", tool_name)
        return 0


async def list_grants(
    db, include_expired: bool = False, include_revoked: bool = False,
) -> list[dict]:
    """列出规则。默认只返回**当前有效**的项。"""
    clauses: list[str] = []
    params: list = []
    if not include_revoked:
        clauses.append("revoked_at IS NULL")
    if not include_expired:
        clauses.append("expires_at > ?")
        params.append(_time.time())
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        cur = await db.execute(
            "SELECT id, tool_name, pattern, rule_type, agent_id, project_id, "
            "granted_at, expires_at, revoked_at, source, grant_reason "
            "FROM standing_grants" + where + " ORDER BY granted_at DESC",
            params,
        )
        rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception:
        _log.exception("List grants failed")
        return []


async def purge_expired(db) -> int:
    """物理清理已过期 / 已撤销且超过保留窗口的记录。返回删除条数。

    保留窗口 = STANDING_GRANT_MAX_TTL_SEC，保留期内仍可审计，之后才删除。
    """
    cutoff = _time.time() - STANDING_GRANT_MAX_TTL_SEC
    try:
        cur = await db.execute(
            "DELETE FROM standing_grants "
            "WHERE (revoked_at IS NOT NULL AND revoked_at < ?) OR expires_at < ?",
            (cutoff, cutoff),
        )
        await db.commit()
        return cur.rowcount or 0
    except Exception:
        _log.exception("Purge expired grants failed")
        return 0
