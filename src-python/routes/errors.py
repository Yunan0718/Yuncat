"""routes/errors.py —— Batch 8.6: 本地错误事件查询/清空端点

- GET /api/v1/errors: 查询 error_events 表（HTTP 错误响应持久化记录）
- DELETE /api/v1/errors: 清空（带确认参数）

对标：
- VS Code Problems panel / Cursor diagnostics
- Sentry issue list（本地化版本，不上报外部）
- audit_log 路由的查询模式（list_audit_logs）

设计：
- 默认返回最近 50 条，上限 500 防滥用
- level 过滤：4xx / 5xx / all
- since_ts 过滤：支持前端"自上次刷新以来"增量查询
- 走 token_middleware（仅认证用户可查，避免未授权读取 stack_trace）
- 与 audit_log 语义分离——audit_log 是工具副作用审计，error_events 是 HTTP 错误响应
"""
import logging
import time as _t

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/v1", tags=["errors"])
_log = logging.getLogger("yuncat.errors")

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


@router.get("/errors")
async def list_errors(
    request: Request,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    level: str = "all",  # all | 4xx | 5xx
    since_ts: int = 0,
):
    """查询错误事件（最新优先）。

    Query params:
      limit:    返回条数（默认 50，上限 500）
      offset:   偏移量（分页）
      level:    all | 4xx | 5xx
      since_ts: Unix 时间戳，只返回 created_at >= since_ts 的记录（0=全部）

    Returns: {"errors": [...], "count": int, "total": int, "query": {...}}
    """
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, int(offset))
    level = (level or "all").lower()
    if level not in ("all", "4xx", "5xx"):
        level = "all"
    since_ts = max(0, int(since_ts))

    conn = getattr(request.app.state, "db", None)
    if conn is None:
        return {"errors": [], "count": 0, "total": 0, "error": "db_not_initialized",
                "query": {"limit": limit, "offset": offset, "level": level, "since_ts": since_ts}}

    where_clauses = []
    params: list = []
    if level == "4xx":
        where_clauses.append("status_code >= 400 AND status_code < 500")
    elif level == "5xx":
        where_clauses.append("status_code >= 500")
    if since_ts > 0:
        where_clauses.append("created_at >= ?")
        params.append(since_ts)
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    try:
        # 总数
        cur = await conn.execute(f"SELECT COUNT(*) FROM error_events{where_sql}", params)
        row = await cur.fetchone()
        total = row[0] if row else 0

        # 分页查询
        sql = (
            f"SELECT id, error_code, status_code, message, path, method, "
            f"trace_id, task_id, agent_id, stack_trace, created_at "
            f"FROM error_events{where_sql} "
            f"ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        cur = await conn.execute(sql, [*params, limit, offset])
        rows = await cur.fetchall()
        errors = [
            {
                "id": r[0],
                "error_code": r[1],
                "status_code": r[2],
                "message": r[3],
                "path": r[4],
                "method": r[5],
                "trace_id": r[6] or "",
                "task_id": r[7] or "",
                "agent_id": r[8] or "",
                "stack_trace": r[9] or "",
                "created_at": r[10],
            }
            for r in rows
        ]
    except Exception as e:
        _log.warning("errors query failed: %s", e, exc_info=True)
        return {"errors": [], "count": 0, "total": 0, "error": str(e),
                "query": {"limit": limit, "offset": offset, "level": level, "since_ts": since_ts}}

    return {
        "errors": errors,
        "count": len(errors),
        "total": total,
        "query": {"limit": limit, "offset": offset, "level": level, "since_ts": since_ts},
        "server_ts": int(_t.time()),
    }


@router.delete("/errors")
async def clear_errors(
    request: Request,
    confirm: bool = False,
    older_than_ts: int = 0,
):
    """清空错误事件。

    Query params:
      confirm:        必须为 true 才执行清空（防误操作）
      older_than_ts:  可选——仅清空 created_at < older_than_ts 的记录（0=全部）

    Returns: {"deleted": int}
    """
    if not confirm:
        raise HTTPException(status_code=400, detail="confirm=true required to clear error events")

    conn = getattr(request.app.state, "db", None)
    if conn is None:
        return {"deleted": 0, "error": "db_not_initialized"}

    try:
        if older_than_ts > 0:
            cur = await conn.execute(
                "DELETE FROM error_events WHERE created_at < ?", (older_than_ts,),
            )
        else:
            cur = await conn.execute("DELETE FROM error_events")
        deleted = cur.rowcount or 0
        await conn.commit()
    except Exception as e:
        _log.warning("errors clear failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"clear failed: {e}")

    return {"deleted": deleted, "server_ts": int(_t.time())}
