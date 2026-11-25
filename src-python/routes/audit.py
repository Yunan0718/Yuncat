"""routes/audit.py —— H4: 审计日志查看端点

- GET /api/v1/audit-log: 查询 audit_log 表（追加式审计日志，记录所有副作用工具调用）

对标主流：VS Code Output panel、Cursor activity log、Claude Code audit trail。
Yuncat 用 SQLite 追加表实现轻量版（Round 1 S6 已建表 + audit_repo.list_audit_logs）。

设计:
- 默认返回最近 100 条，上限 1000 防滥用
- tool_name 过滤——快速定位某工具的所有调用
- since_ts 过滤——支持前端"自上次刷新以来"增量查询
- 只读——不暴露任何写入/删除接口（审计日志 append-only，防篡改）
"""
import logging
import time as _t

from fastapi import APIRouter, Request

from repositories.audit_repo import list_audit_logs

router = APIRouter(prefix="/api/v1", tags=["audit"])
_log = logging.getLogger("yuncat.audit")

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


@router.get("/audit-log")
async def get_audit_log(
    request: Request,
    limit: int = DEFAULT_LIMIT,
    tool_name: str = "",
    since_ts: int = 0,
):
    """查询审计日志（最新优先）。

    Query params:
      limit:     返回条数（默认 100，上限 1000）
      tool_name: 按工具名过滤（精确匹配，空=全部）
      since_ts:  Unix 时间戳，只返回 ts >= since_ts 的记录（0=全部）

    Returns: {"logs": [...], "count": int, "query": {limit, tool_name, since_ts}}
    """
    # 参数校验
    limit = max(1, min(limit, MAX_LIMIT))
    tool_name = (tool_name or "").strip()
    since_ts = max(0, int(since_ts))

    # 获取全局 DB 连接（app.state.db）
    conn = getattr(request.app.state, "db", None)
    if conn is None:
        _log.warning("audit-log: DB not initialized (app.state.db is None)")
        return {"logs": [], "count": 0, "error": "db_not_initialized",
                "query": {"limit": limit, "tool_name": tool_name, "since_ts": since_ts}}

    try:
        logs = await list_audit_logs(
            conn, limit=limit, tool_name=tool_name, since_ts=since_ts,
        )
    except Exception as e:
        _log.warning("audit-log query failed: %s", e, exc_info=True)
        return {"logs": [], "count": 0, "error": str(e),
                "query": {"limit": limit, "tool_name": tool_name, "since_ts": since_ts}}

    return {
        "logs": logs,
        "count": len(logs),
        "query": {"limit": limit, "tool_name": tool_name, "since_ts": since_ts},
        "server_ts": int(_t.time()),
    }
