"""repositories/audit_repo.py —— Round 1 S6 防篡改审计日志

追加式审计日志（append-only）：记录所有副作用工具调用。
- 无 UPDATE/DELETE 接口——防误改/恶意篡改
- args_summary 截断 ≤500 字符——防日志膨胀
- 写入失败只 _log.debug——不阻塞工具执行（fire-and-forget）

对标主流：Claude Code 的 audit log、VS Code 的 telemetry 审计、
OpenHands 的 action 记录。Yuncat 用 SQLite 追加表实现轻量版。
"""
import logging
import time as _t

_log = logging.getLogger("yuncat.repo.audit")

_MAX_ARGS_SUMMARY = 500


def _truncate_args(arguments: dict) -> str:
    """安全摘要参数——截断长内容，防日志膨胀。

    Task 3.1: 对 str 值先脱敏再截断——防止 audit_log 表里泄露 sk-/password= 等敏感串。
    """
    from security.redact import redact_secrets
    summary = {}
    for k, v in arguments.items():
        if isinstance(v, str):
            v = redact_secrets(v)
            if len(v) > 80:
                v = v[:80] + "..."
        summary[k] = v
    s = str(summary)
    return s[:_MAX_ARGS_SUMMARY] + "..." if len(s) > _MAX_ARGS_SUMMARY else s


async def log_audit(
    conn,
    agent_id: str,
    tool_name: str,
    arguments: dict | None = None,
    success: bool | None = None,
    phase: str = "",
    session_id: str = "",
    task_id: str = "",
) -> None:
    """追加一条审计日志——fire-and-forget，失败不抛。

    success=None 表示未执行（如 preview 阶段）。
    phase: preview / execute / blocked。
    """
    try:
        args_summary = _truncate_args(arguments or {})
        await conn.execute(
            "INSERT INTO audit_log(ts, agent_id, tool_name, args_summary, "
            "success, phase, session_id, task_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(_t.time()),
                agent_id or "unknown",
                tool_name,
                args_summary,
                1 if success is True else (0 if success is False else None),
                phase,
                session_id,
                task_id,
            ),
        )
        await conn.commit()
    except Exception:
        # 审计日志失败绝不阻塞主流程
        _log.debug("audit_log insert failed", exc_info=True)


async def list_audit_logs(
    conn, limit: int = 100, tool_name: str = "",
    since_ts: int = 0,
) -> list[dict]:
    """查询审计日志（最新优先）——供 H4 审计日志查看 UI 使用。"""
    sql = ("SELECT id, ts, agent_id, tool_name, args_summary, success, "
           "phase, session_id, task_id FROM audit_log "
           "WHERE ts >= ?")
    params: list = [since_ts]
    if tool_name:
        sql += " AND tool_name = ?"
        params.append(tool_name)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()
    return [
        {
            "id": r[0], "ts": r[1], "agent_id": r[2], "tool_name": r[3],
            "args_summary": r[4], "success": r[5], "phase": r[6],
            "session_id": r[7], "task_id": r[8],
        }
        for r in rows
    ]
