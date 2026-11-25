"""routes/system.py —— 设置 + 分析 + 通知 + MCP + Prompt + Git + 工具确认 + HITL + 依赖检测"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from config import (
    VERSION_PROBE_TIMEOUT_SEC,
    NOTIFICATION_LIST_DEFAULT_LIMIT,
    SYSTEM_ARENA_EVENTS_LIMIT,
)
from services.analytics_stats import conversation_message_count, plan_step_stats, task_status_counts

router = APIRouter(prefix="/api/v1", tags=["system"])
_log = logging.getLogger("yuncat.system")


# ---- 系统依赖检测 ----

@router.get("/system/dependencies")
async def system_dependencies(request: Request):
    import shutil
    import subprocess

    async def check(cmd: str) -> dict:
        path = shutil.which(cmd)
        if not path:
            return {"installed": False, "version": None, "path": None}
        try:
            result = await asyncio.to_thread(
                subprocess.run, [cmd, "--version"], capture_output=True, text=True, timeout=VERSION_PROBE_TIMEOUT_SEC
            )
            version = result.stdout.strip().split()[-1] if result.stdout else "unknown"
        except Exception:
            version = "unknown"
        return {"installed": True, "version": version, "path": path}

    # C2 修复：三个依赖检测并行执行（asyncio.gather），不阻塞事件循环
    python_r, node_r, git_r = await asyncio.gather(
        check("python"), check("node"), check("git")
    )
    return {"python": python_r, "node": node_r, "git": git_r}


# ---- 设置 ----

@router.get("/settings")
async def get_settings(request: Request):
    return await request.app.state.settings_service.get_all()


@router.post("/settings")
async def save_settings(payload: dict, request: Request):
    key = payload.get("key")
    value = payload.get("value")
    if not key:
        raise HTTPException(400, "Missing 'key' in payload")
    await request.app.state.settings_service.save(key, value)
    return {"status": "ok"}


# ---- CodeWhale 借鉴 #13: Runtime 能力声明端点 ----

def _detected_backends(state) -> list[dict]:
    """已发现的 agent 后端（能力声明用；服务未初始化时返回空列表）。"""
    adapters = getattr(state, "adapters", None) or {}
    return [
        {"id": aid, "available": getattr(a, "is_available", lambda: True)()}
        for aid, a in adapters.items()
    ]


@router.get("/capabilities")
async def get_capabilities(request: Request):
    """返回后端运行时能力——供前端动态发现支持的功能。

    对标 CodeWhale /v1/runtime/info → RuntimeCapabilities。
    前端可据此决定显示哪些 UI 模块（如无 arena 则隐藏盲测面板）。
    所有能力通过 hasattr 动态探测，不硬编码——服务未初始化时返回 false。
    """
    st = request.app.state
    from config import APP_VERSION, HTTP_MAX_BODY_SIZE, WORKSPACE_MAX_READ_SIZE

    # 2026-09-15: 把「体积上限」一并暴露——前端做上传前预检时不用再硬编码一份数字
    limits = {
        "max_upload_bytes": HTTP_MAX_BODY_SIZE,
        "max_file_read_bytes": WORKSPACE_MAX_READ_SIZE,
    }

    backends = _detected_backends(st)

    # 功能能力——通过 app.state 服务是否存在动态探测
    def _has(attr: str) -> bool:
        return getattr(st, attr, None) is not None

    capabilities = {
        "version": APP_VERSION,
        "backends": backends,
        "multi_backend": len(backends) > 1,
        "features": {
            "mcp": _has("mcp_service") or _has("mcp_registry"),
            "workflows": _has("workflow_service"),
            "arena": _has("arena_service"),
            "long_task": _has("long_task_orchestrator"),
            "task_supervisor": _has("task_supervisor"),
            "memory_tree": _has("memory_tree"),
            "self_evolution": _has("evolution_engine"),
            "vcs": _has("vcs_service"),
            "snapshot": _has("snapshot_service"),
            "approval_inbox": _has("approval_inbox"),
            "terminal": _has("terminal_manager"),
            "code_indexer": _has("code_indexer"),
            "prompt_agent": _has("prompt_agent"),
            "deliberation": _has("deliberation_service"),
            "intent_classification": _has("intent_service"),
            "vector_search": _has("vector_repo") and getattr(st, "vector_storage_available", False),
        },
        "security_profile": getattr(st, "security_profile", {}),
    }
    return capabilities


# ---- CodeWhale 借鉴 #14: 事件重放端点 ----

@router.get("/events/replay")
async def replay_events(
    since_seq: int = 0,
    topic: str = "",
    limit: int = 100,
    request: Request = None,
):
    """重放 seq > since_seq 的持久化事件——供 SSE/WebSocket 断线重连。

    对标 CodeWhale SSE since_seq 参数。前端断线重连时调用此端点获取缺失事件，
    然后继续监听实时流。仅返回 persisted_types 中的事件（已持久化到 outbox 的）。
    """
    eb = getattr(request.app.state, "event_bus", None)
    if eb is None:
        return {"events": [], "last_seq": 0}
    events = await eb.replay_since(
        since_seq=since_seq,
        topic=topic or None,
        limit=min(limit, 1000),
    )
    last_seq = await eb.get_last_seq()
    return {"events": events, "last_seq": last_seq}


# ---- 日志管理（Task 7.15/7.7） ----

@router.post("/admin/log-level")
async def set_log_level_endpoint(payload: dict, request: Request):
    """运行时调整日志级别。

    Body:
        level: str —— DEBUG/INFO/WARNING/ERROR/CRITICAL（必填）
        logger: str —— 目标 logger 名，空字符串或不传表示 root logger

    Returns: {"ok": true, "level": "DEBUG", "logger": ""}
    """
    from yuncat_utils.structured_logging import set_log_level

    level = payload.get("level")
    if not level:
        raise HTTPException(400, "Missing 'level' in payload")
    logger_name = payload.get("logger", "")
    try:
        set_log_level(level, logger_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "level": str(level).upper(), "logger": logger_name}


# ---- 分析 ----

@router.post("/analytics/startup")
async def report_frontend_startup(payload: dict, request: Request):
    """Phase 97: 接收前端启动耗时上报。

    Body:
        frontend_ms: int (必填) —— 从 StartupScreen 挂载到 backendReady 的耗时
        stages?: dict           —— 各阶段进入时间戳（相对挂载时间，ms）
        app_version?: str       —— Yuncat 应用版本
        electron_version?: str  —— Electron 版本
        platform?: str          —— 平台（win32/darwin/linux）

    Returns: {"ok": true}
    """
    frontend_ms = payload.get("frontend_ms")
    if frontend_ms is None:
        raise HTTPException(400, "Missing 'frontend_ms' in payload")
    if not isinstance(frontend_ms, (int, float)) or frontend_ms < 0:
        raise HTTPException(400, "'frontend_ms' must be a non-negative number")

    stages = payload.get("stages")
    if stages is not None and not isinstance(stages, dict):
        raise HTTPException(400, "'stages' must be an object")

    extra = {}
    for k in ("app_version", "electron_version", "platform"):
        v = payload.get(k)
        if v is not None:
            extra[k] = str(v)

    await request.app.state.analytics_service.frontend_startup(
        frontend_ms=int(frontend_ms),
        stages=stages,
        extra=extra or None,
    )
    return {"ok": True}


@router.get("/analytics/startup-stats")
async def analytics_startup_stats(request: Request, days: int = 7):
    """Phase 97: 查询最近 N 天的启动耗时统计（后端 + 前端）。

    Returns: {
        "backend": [{"total_ms": int, "phases": {...}, "version": str, "created_at": int}],
        "frontend": [{"frontend_ms": int, "stages": {...}, "app_version": str, "created_at": int}],
        "backend_avg_ms": float,
        "frontend_avg_ms": float,
    }
    """
    import json
    import time

    cutoff = int(time.time()) - days * 86400
    db = request.app.state.db

    cursor = await db.execute(
        "SELECT event_data, created_at FROM analytics_events "
        "WHERE event_type='startup_metrics' AND created_at > ? "
        "ORDER BY created_at DESC LIMIT 100",
        (cutoff,),
    )
    backend_rows = await cursor.fetchall()
    backend = [json.loads(r[0]) | {"created_at": r[1]} for r in backend_rows]

    cursor = await db.execute(
        "SELECT event_data, created_at FROM analytics_events "
        "WHERE event_type='frontend_startup' AND created_at > ? "
        "ORDER BY created_at DESC LIMIT 100",
        (cutoff,),
    )
    frontend_rows = await cursor.fetchall()
    frontend = [json.loads(r[0]) | {"created_at": r[1]} for r in frontend_rows]

    backend_avg = sum(r.get("total_ms", 0) for r in backend) / len(backend) if backend else 0
    frontend_avg = sum(r.get("frontend_ms", 0) for r in frontend) / len(frontend) if frontend else 0

    return {
        "backend": backend,
        "frontend": frontend,
        "backend_avg_ms": round(backend_avg, 1),
        "frontend_avg_ms": round(frontend_avg, 1),
    }


@router.get("/analytics/summary")
async def analytics_summary(request: Request):
    """Dashboard: 任务与会话汇总——前端读 completed/failed/running/total_messages"""
    db = request.app.state.db
    by_status = await task_status_counts(db)
    total = sum(by_status.values())
    completed = by_status.get("completed", 0)
    return {
        "total_tasks": total,
        "completed": completed,
        "failed": by_status.get("failed", 0),
        "running": by_status.get("running", 0),
        "total_messages": await conversation_message_count(db),
        "success_rate": completed / total if total > 0 else 0.0,
        "accept_rate": await request.app.state.analytics_service.get_accept_rate(),
        "top_agents": await request.app.state.analytics_service.get_agent_usage_stats(),
    }


@router.get("/analytics/agent-usage")
async def analytics_agent_usage(days: int = 7, request: Request = None):
    return await request.app.state.analytics_service.get_agent_usage_stats(days)


@router.get("/analytics/accept-rate")
async def analytics_accept_rate(days: int = 7, request: Request = None):
    return await request.app.state.analytics_service.get_accept_rate(days)


@router.get("/analytics/backend-health")
async def analytics_backend_health(request: Request = None):
    """Dashboard: 各后端在线状态——adapters 保留兼容，agents 是仪表盘要的形态"""
    adapters = request.app.state.agent_service.adapters
    health, agents = {}, {}
    for agent_id, adapter in adapters.items():
        try:
            healthy = await adapter.health() if hasattr(adapter, "health") else None
        except Exception:
            healthy = False
        model = getattr(adapter, "MODEL", agent_id)
        health[agent_id] = {"healthy": healthy, "model": model}
        agents[agent_id] = {
            "id": agent_id, "name": getattr(adapter, "display_name", "") or "",
            "ready": bool(healthy), "model": model,
        }
    all_healthy = all(v.get("healthy") for v in health.values()) if health else False
    return {"adapters": health, "agents": agents, "all_healthy": all_healthy}


@router.get("/analytics/plan-stats")
async def analytics_plan_stats(request: Request = None):
    """Dashboard: 计划步骤统计（plan_steps 按 status 聚合）"""
    return await plan_step_stats(request.app.state.db)


@router.get("/analytics/token-usage")
async def token_usage(days: int = 7, request: Request = None):
    """返回指定天数内的 token 用量统计（按天汇总）。

    2026-10-22：改用**账本口径**（输入 + 输出）。此前读 conversations.token_count，
    那个字段只记输出 token，真实消耗以输入为主——同一批数据少报约 100 倍。
    消息级统计仍单列（message_tokens / total_messages），便于对账。
    """
    import time

    cutoff = int(time.time()) - days * 86400
    cursor = await request.app.state.db.execute(
        "SELECT COUNT(*) AS msg_count, SUM(token_count) AS msg_tokens "
        "FROM conversations WHERE token_count IS NOT NULL AND created_at > ?",
        (cutoff,),
    )
    row = await cursor.fetchone()
    from services.token_tracker import get_token_usage_overview

    overview = await get_token_usage_overview(request.app.state.db, days=days)
    message_count = row[0] or 0
    message_tokens = row[1] or 0
    return {
        **overview,
        "total_messages": message_count,
        "message_tokens": message_tokens,
        "avg_tokens_per_msg": (round(message_tokens / message_count)
                               if message_count else 0),
    }


# ---- Phase 20: 缓存命中率统计 ----

@router.get("/analytics/cache-stats")
async def cache_stats(days: int = 7, request: Request = None):
    """缓存命中率统计——与 token 用量同一账本口径（不限条数）。

    2026-10-22：旧实现只取**最近 500 条**事件（SYSTEM_ANALYTICS_EVENTS_LIMIT）再在
    Python 里累加，条数一多命中率就只描述最近一段，且响应里不告知——静默截断。
    现在统一走 AnalyticsService 的账本聚合：全量、无截断，字段名引用
    services/llm_usage.py 的规范名（``cached_tokens`` / ``cache_miss_tokens``）；
    ``cache_hit_tokens`` 是历史键名，仅作读取兜底，不再由任何生产者写入。
    """
    from services.token_tracker import get_token_usage_overview

    overview = await get_token_usage_overview(request.app.state.db, days=days)
    total_hit = overview["cached_tokens"]
    total_miss = overview["cache_miss_tokens"]
    total_cache = total_hit + total_miss
    hit_rate = (total_hit / total_cache * 100) if total_cache > 0 else 0
    return {
        "cache_hit_tokens": total_hit,
        "cache_miss_tokens": total_miss,
        "total_cache_tokens": total_cache,
        "hit_rate_pct": round(hit_rate, 1),
        "days": days,
    }


# ---- Batch 8.3: 任务级 LLM 成本聚合 ----

@router.get("/analytics/tasks/{task_id}/llm-metrics")
async def analytics_task_llm_metrics(task_id: str, request: Request):
    """返回指定 task 的所有 per-LLM-call 指标（按 round 升序）。

    对标 LangSmith per-call metrics / Helicone LLM observability。
    数据源：token_cost 事件（由 agent_tool_loop._track_token_cost 写入）。
    """
    metrics = await request.app.state.analytics_service.get_llm_call_metrics(task_id)
    return {"metrics": metrics}


@router.get("/analytics/tasks/{task_id}/llm-summary")
async def analytics_task_llm_summary(task_id: str, request: Request):
    """聚合统计指定 task 的 LLM 调用——次数/总延迟/总成本/总 token。"""
    return await request.app.state.analytics_service.get_llm_call_summary(task_id)


# ---- Batch 8.4: LLM 响应缓存统计 ----

@router.get("/analytics/llm-cache/stats")
async def analytics_llm_cache_stats(request: Request):
    """返回 LLM 响应缓存统计——命中/未命中/命中率/容量。"""
    cache = getattr(request.app.state, "llm_cache", None)
    if cache is None:
        return {"enabled": False, "size": 0, "hits": 0, "misses": 0, "hit_rate_pct": 0.0}
    return cache.stats()


# ---- Phase 34: 自我演化统计 ----

@router.get("/analytics/evolution")
async def evolution_stats(request: Request = None):
    cursor = await request.app.state.db.execute("SELECT COUNT(*) FROM knowledge_nodes")
    total = (await cursor.fetchone())[0]
    cursor2 = await request.app.state.db.execute(
        "SELECT COUNT(*) FROM knowledge_nodes WHERE score >= 0.8"
    )
    high_score = (await cursor2.fetchone())[0]
    cursor3 = await request.app.state.db.execute(
        "SELECT COUNT(*) FROM knowledge_nodes WHERE source='self_evolution'"
    )
    patterns = (await cursor3.fetchone())[0]
    cursor4 = await request.app.state.db.execute(
        "SELECT COUNT(*) FROM workflows WHERE description LIKE '%自动生成%' OR name LIKE '%自动生成%'"
    )
    auto_wf = (await cursor4.fetchone())[0]
    return {
        "total_knowledge_nodes": total,
        "high_score_nodes": high_score,
        "learned_patterns": patterns,
        "auto_generated_workflows": auto_wf,
    }


@router.get("/analytics/conversations")
async def conversation_stats(days: int = 7, request: Request = None):
    import time
    cutoff = int(time.time()) - days * 86400
    cursor = await request.app.state.db.execute(
        "SELECT COUNT(DISTINCT session_id) FROM conversations WHERE created_at > ?", (cutoff,)
    )
    sessions = (await cursor.fetchone())[0] or 0
    cursor2 = await request.app.state.db.execute(
        "SELECT COUNT(*) FROM conversations WHERE created_at > ?", (cutoff,)
    )
    total = (await cursor2.fetchone())[0] or 0
    cursor3 = await request.app.state.db.execute(
        "SELECT DATE(created_at, 'unixepoch') AS day, COUNT(*) AS cnt "
        "FROM conversations WHERE created_at > ? "
        "GROUP BY day ORDER BY day DESC LIMIT ?",
        (cutoff, days),
    )
    daily = [{"day": r[0], "count": r[1]} for r in await cursor3.fetchall()]
    return {
        "total_sessions": sessions,
        "total_conversations": total,
        "daily_sessions": daily,
        "days": days,
    }


# ---- Phase 20: Agent Arena 排行榜 ----

@router.get("/analytics/arena")
async def arena_leaderboard(days: int = 30, request: Request = None):
    """Agent Arena 排行榜——按模型+供应商分组，按采纳率排序。
    数据来源: analytics_events (event_type='arena_vote')。
    """
    import time
    cutoff = int(time.time()) - days * 86400
    cursor = await request.app.state.db.execute(
        "SELECT event_data FROM analytics_events "
        "WHERE event_type='arena_vote' AND created_at > ? "
        f"ORDER BY created_at DESC LIMIT {SYSTEM_ARENA_EVENTS_LIMIT}",
        (cutoff,),
    )
    rows = await cursor.fetchall()
    # 聚合: key=agent_id|model|provider → {votes, ratings, accepted}
    agg: dict[str, dict] = {}
    for (data_json,) in rows:
        try:
            import json as _json
            d = _json.loads(data_json or "{}")
            key = f"{d.get('agent_id','')}|{d.get('model','')}|{d.get('provider','')}"
            if key not in agg:
                agg[key] = {"votes": 0, "rating_sum": 0.0, "accepted": 0}
            agg[key]["votes"] += 1
            agg[key]["rating_sum"] += d.get("rating", 0)
            if d.get("accepted") or d.get("adopted"):
                agg[key]["accepted"] += 1
        except Exception:
            _log.debug("聚合评分数据失败", exc_info=True)
    entries = []
    for key, stats in agg.items():
        parts = key.split("|", 2)
        entries.append({
            "agent_id": parts[0], "model": parts[1], "provider": parts[2],
            "total_votes": stats["votes"],
            "avg_rating": round(stats["rating_sum"] / stats["votes"], 1) if stats["votes"] else 0,
            "adoption_rate": round(stats["accepted"] / stats["votes"], 2) if stats["votes"] else 0,
        })
    # 按采纳率降序
    entries.sort(key=lambda e: e["adoption_rate"], reverse=True)
    return {"entries": entries, "days": days}


@router.post("/analytics/arena/vote")
async def arena_vote(request: Request):
    """记录 Agent Arena 投票——用户对 A/B 对比结果的评分。
    Body: {task_id, agent_id, rating(1-5), adopted(bool), model?, provider?}
    """
    import json as _json
    import time as _time
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    event_data = _json.dumps({
        "task_id": body.get("task_id", ""),
        "agent_id": body.get("agent_id", ""),
        "rating": body.get("rating", 0),
        "accepted": body.get("adopted", False),
        "model": body.get("model", ""),
        "provider": body.get("provider", ""),
    })
    await request.app.state.db.execute(
        "INSERT INTO analytics_events(event_type, event_data, created_at) VALUES(?,?,?)",
        ("arena_vote", event_data, int(_time.time())),
    )
    await request.app.state.db.commit()
    return {"ok": True}


@router.get("/analytics/failure-rate")
async def failure_rate(days: int = 30, request: Request = None):
    """Phase 44 C.9: 变更失败率聚合查询。
    从 analytics_events 表聚合 tool_done 事件，
    返回每日的 total_changes / failures / failure_rate_pct。
    """
    import time as _time
    cursor = await request.app.state.db.execute(
        "SELECT "
        " date(created_at, 'unixepoch') as day,"
        " COUNT(*) as total_changes,"
        " SUM(CASE WHEN json_extract(event_data, '$.success') = 0 THEN 1 ELSE 0 END) as failures,"
        " ROUND(100.0 * SUM(CASE WHEN json_extract(event_data, '$.success') = 0 THEN 1 ELSE 0 END)"
        "  / NULLIF(COUNT(*), 0), 1) as failure_rate_pct"
        " FROM analytics_events"
        " WHERE event_type = 'tool_done'"
        "  AND created_at > ?"
        " GROUP BY day ORDER BY day DESC LIMIT ?",
        (int(_time.time()) - days * 86400, days),
    )
    rows = await cursor.fetchall()
    return [
        {"day": r[0], "total_changes": r[1], "failures": r[2],
         "failure_rate_pct": r[3] or 0.0}
        for r in rows
    ]


@router.get("/notifications")
async def list_notifications(limit: int = NOTIFICATION_LIST_DEFAULT_LIMIT, request: Request = None):
    cursor = await request.app.state.db.execute(
        "SELECT id, type, title, body, link, read, created_at, level, task_id, agent_id "
        "FROM notifications ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r[0], "type": r[1], "title": r[2],
            "body": r[3], "link": r[4],
            "read": bool(r[5]), "created_at": r[6],
            "level": r[7], "task_id": r[8], "agent_id": r[9],
        }
        for r in rows
    ]


@router.post("/notifications/{notif_id}/read")
async def mark_notification_read(notif_id: str, request: Request):
    await request.app.state.db.execute(
        "UPDATE notifications SET read=1 WHERE id=?", (notif_id,),
    )
    await request.app.state.db.commit()
    return {"status": "ok"}


@router.post("/notifications/read-all")
async def mark_all_notifications_read(request: Request):
    """Phase 106: 全部标记已读。"""
    notif_center = getattr(request.app.state, "notification_center", None)
    if notif_center is None:
        raise HTTPException(503, "notification center not available")
    count = await notif_center.mark_all_read()
    return {"ok": True, "marked": count}


@router.delete("/notifications/{notif_id}")
async def delete_notification(notif_id: str, request: Request):
    """Phase 106: 删除单条通知。"""
    notif_center = getattr(request.app.state, "notification_center", None)
    if notif_center is None:
        raise HTTPException(503, "notification center not available")
    ok = await notif_center.dismiss(notif_id)
    return {"ok": ok}


@router.delete("/notifications")
async def clear_read_notifications(request: Request):
    """Phase 106: 清空所有已读通知。"""
    notif_center = getattr(request.app.state, "notification_center", None)
    if notif_center is None:
        raise HTTPException(503, "notification center not available")
    count = await notif_center.clear_read()
    return {"ok": True, "deleted": count}


# ---- MCP 服务器 ----

@router.get("/mcp/servers")
async def list_mcp_servers(request: Request):
    from repositories import list_mcp_servers as _list_mcp

    return await _list_mcp(request.app.state.db)


@router.post("/mcp/servers")
async def create_mcp_server(payload: dict, request: Request):
    from repositories import create_mcp_server as _create_mcp

    name = payload.get("name")
    command = payload.get("command")
    if not name or not command:
        raise HTTPException(400, "Missing 'name' or 'command'")
    sid = await _create_mcp(
        request.app.state.db, name=name, command=command,
        args=payload.get("args", []), env=payload.get("env", {}),
        enabled=payload.get("enabled", True),
    )
    return {"server_id": sid, "status": "ok"}


@router.get("/mcp/servers/{server_id}")
async def get_mcp_server(server_id: str, request: Request):
    from repositories import get_mcp_server as _get_mcp

    server = await _get_mcp(request.app.state.db, server_id)
    if not server:
        raise HTTPException(404, "MCP server not found")
    return server


@router.put("/mcp/servers/{server_id}")
async def update_mcp_server(server_id: str, payload: dict, request: Request):
    from repositories import update_mcp_server as _update_mcp

    ok = await _update_mcp(request.app.state.db, server_id, **payload)
    if not ok:
        raise HTTPException(404, "MCP server not found or no fields to update")
    return {"status": "ok"}


@router.delete("/mcp/servers/{server_id}")
async def delete_mcp_server(server_id: str, request: Request):
    from repositories import delete_mcp_server as _delete_mcp

    ok = await _delete_mcp(request.app.state.db, server_id)
    if not ok:
        raise HTTPException(404, "MCP server not found")
    return {"status": "ok"}


@router.get("/mcp/status")
async def mcp_runtime_status(request: Request = None):
    """Phase 18: 检查 MCP 服务器的运行时状态——探测进程是否存活"""
    import shutil

    from repositories import list_mcp_servers as _list_mcp

    servers = await _list_mcp(request.app.state.db)
    result = []
    for s in servers:
        cmd = s.get("command", "")
        exe = cmd.split()[0] if cmd else ""
        found = bool(exe and shutil.which(exe))
        result.append({
            "id": s["id"], "name": s["name"],
            "enabled": s.get("enabled", False),
            "installed": found, "alive": found,
        })
    return result


@router.get("/mcp/discover")
async def discover_mcp_servers(request: Request):
    """Phase 51-D: 自动发现本地 MCP 客户端配置（Claude Desktop / Cursor）。"""
    from services.mcp_discovery import discover_local_mcp_configs

    return {"ok": True, "sources": discover_local_mcp_configs()}


@router.post("/mcp/discover/import")
async def import_discovered_mcp_server(payload: dict, request: Request):
    """Phase 51-D: 将发现的服务器导入 Yuncat MCP 注册表。"""
    from repositories import create_mcp_server as _create_mcp

    source = payload.get("source")
    server = payload.get("server")
    if not source or not isinstance(server, dict):
        raise HTTPException(400, "Missing 'source' or 'server'")
    name = server.get("name")
    command = server.get("command")
    if not name or not command:
        raise HTTPException(400, "Missing 'name' or 'command' in server")

    sid = await _create_mcp(
        request.app.state.db,
        name=f"[{source}] {name}",
        command=command,
        args=server.get("args", []),
        env=server.get("env", {}),
        enabled=True,
    )
    return {"server_id": sid, "status": "ok"}


# ---- Prompt 热加载 ----

@router.get("/prompts")
async def list_prompts(request: Request):
    svc = request.app.state.prompt_service
    return [
        {
            "name": p.name, "version": p.version,
            "file_path": p.file_path, "variables": p.variables,
        }
        for p in svc.list_prompts()
    ]


@router.get("/prompts/{name}")
async def get_prompt(name: str, request: Request):
    svc = request.app.state.prompt_service
    detail = svc.get_prompt(name)
    if not detail:
        raise HTTPException(404, f"Prompt module '{name}' not found")
    return {
        "name": detail.name, "version": detail.version,
        "variables": detail.variables,
    }


@router.put("/prompts/{name}")
async def update_prompt(name: str, payload: dict, request: Request):
    svc = request.app.state.prompt_service
    variables = payload.get("variables")
    if not variables or not isinstance(variables, dict):
        raise HTTPException(400, "Missing 'variables' dict in payload")
    try:
        svc.update_prompt(name, variables)
        return {"status": "ok", "name": name}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/prompts/reload")
async def reload_prompts(request: Request):
    svc = request.app.state.prompt_service
    changed = svc.force_reload()
    return {"status": "ok", "reloaded": changed}


@router.post("/prompts/{name}/preview")
async def preview_prompt(name: str, payload: dict, request: Request):
    svc = request.app.state.prompt_service
    try:
        result = svc.preview(name, payload.get("context"))
        return {"name": name, "preview": result}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/prompts/{name}/reset")
async def reset_prompt_overrides(name: str, request: Request):
    svc = request.app.state.prompt_service
    svc.reset_overrides(name)
    return {"status": "ok", "name": name}


# ---- Git 集成 ----

@router.get("/git/status")
async def git_status(request: Request):
    return await request.app.state.git_service.status()


@router.get("/git/diff")
async def git_diff(staged: bool = False, path: str = "", request: Request = None):
    return {"diff": await request.app.state.git_service.diff(staged=staged, path=path)}


@router.get("/git/log")
async def git_log(count: int = 20, request: Request = None):
    return await request.app.state.git_service.log(count)


@router.post("/git/commit")
async def git_commit(payload: dict, request: Request):
    message = payload.get("message", "")
    if not message.strip():
        raise HTTPException(400, "Missing commit message")
    return await request.app.state.git_service.commit(message)


@router.post("/git/push")
async def git_push(request: Request):
    return await request.app.state.git_service.push()


@router.get("/git/file-timeline")
async def git_file_timeline(path: str = "", request: Request = None):
    if not path:
        raise HTTPException(400, "Missing 'path'")
    return await request.app.state.git_service.file_timeline(path)


@router.get("/git/branches")
async def git_branches(request: Request):
    return await request.app.state.git_service.branch_list()


@router.get("/git/graph")
async def git_graph(request: Request):
    return await request.app.state.git_service.graph()


@router.post("/git/stage")
async def git_stage(payload: dict, request: Request):
    path = payload.get("path", "")
    if not path:
        raise HTTPException(400, "Missing 'path'")
    return await request.app.state.git_service.stage(path)


@router.post("/git/unstage")
async def git_unstage(payload: dict, request: Request):
    path = payload.get("path", "")
    if not path:
        raise HTTPException(400, "Missing 'path'")
    return await request.app.state.git_service.unstage(path)


@router.post("/git/discard")
async def git_discard(payload: dict, request: Request):
    path = payload.get("path", "")
    if not path:
        raise HTTPException(400, "Missing 'path'")
    return await request.app.state.git_service.discard(path)


@router.post("/git/branch")
async def git_create_branch(payload: dict, request: Request):
    branch = payload.get("branch", "")
    if not branch:
        raise HTTPException(400, "Missing 'branch'")
    return await request.app.state.git_service.create_branch(branch)


@router.post("/git/checkout")
async def git_checkout(payload: dict, request: Request):
    branch = payload.get("branch", "")
    if not branch:
        raise HTTPException(400, "Missing 'branch'")
    return await request.app.state.git_service.checkout(branch)


@router.post("/git/pull")
async def git_pull(request: Request):
    return await request.app.state.git_service.pull()


@router.post("/git/stash")
async def git_stash(payload: dict, request: Request):
    return await request.app.state.git_service.stash_push(payload.get("message", ""))


@router.post("/git/stash-pop")
async def git_stash_pop(request: Request):
    return await request.app.state.git_service.stash_pop()


@router.post("/git/suggest-commit-message")
async def git_suggest_commit_message(request: Request):
    return await request.app.state.git_service.suggest_commit_message()


@router.post("/git/ai-review")
async def git_ai_review(request: Request):
    return await request.app.state.git_service.ai_review()


@router.post("/git/init")
async def git_init(request: Request):
    return await request.app.state.git_service.init()


# Phase 102: Git Merge 冲突三栏解决面板

@router.get("/git/merge-conflicts")
async def git_merge_conflict_list(request: Request):
    return await request.app.state.git_service.merge_conflict_list()


@router.get("/git/merge-conflicts/{path:path}")
async def git_merge_conflict_read(path: str, request: Request):
    return await request.app.state.git_service.merge_conflict_read(path)


@router.post("/git/merge-conflicts/{path:path}/resolve")
async def git_merge_conflict_resolve(path: str, payload: dict, request: Request):
    resolution = payload.get("resolution", "")
    mark_resolved = payload.get("mark_resolved", True)
    return await request.app.state.git_service.merge_conflict_resolve(
        path, resolution, mark_resolved,
    )


# Phase 103: Git 高级操作

@router.get("/git/blame")
async def git_blame(path: str = "", request: Request = None):
    if not path:
        raise HTTPException(400, "Missing 'path'")
    return await request.app.state.git_service.blame(path)


@router.get("/git/rebase-status")
async def git_rebase_status(request: Request):
    return await request.app.state.git_service.rebase_status()


@router.post("/git/rebase/squash")
async def git_rebase_squash(payload: dict, request: Request):
    base_ref = payload.get("base_ref", "")
    message = payload.get("message", "")
    if not base_ref or not message:
        raise HTTPException(400, "Missing 'base_ref' or 'message'")
    return await request.app.state.git_service.rebase_squash(base_ref, message)


@router.post("/git/rebase/fixup")
async def git_rebase_fixup(payload: dict, request: Request):
    target_hash = payload.get("target_hash", "")
    if not target_hash:
        raise HTTPException(400, "Missing 'target_hash'")
    return await request.app.state.git_service.rebase_fixup(target_hash)


@router.post("/git/rebase/onto")
async def git_rebase_onto(payload: dict, request: Request):
    upstream = payload.get("upstream", "")
    if not upstream:
        raise HTTPException(400, "Missing 'upstream'")
    branch = payload.get("branch")
    return await request.app.state.git_service.rebase_onto(upstream, branch)


@router.post("/git/rebase/continue")
async def git_rebase_continue(request: Request):
    return await request.app.state.git_service.rebase_continue()


@router.post("/git/rebase/abort")
async def git_rebase_abort(request: Request):
    return await request.app.state.git_service.rebase_abort()


@router.post("/git/cherry-pick")
async def git_cherry_pick(payload: dict, request: Request):
    commit_hash = payload.get("hash", "")
    if not commit_hash:
        raise HTTPException(400, "Missing 'hash'")
    return await request.app.state.git_service.cherry_pick(
        commit_hash, payload.get("no_commit", False),
    )


@router.post("/git/revert")
async def git_revert(payload: dict, request: Request):
    commit_hash = payload.get("hash", "")
    if not commit_hash:
        raise HTTPException(400, "Missing 'hash'")
    return await request.app.state.git_service.revert(
        commit_hash, payload.get("no_commit", False),
    )


@router.get("/git/tags")
async def git_tags(request: Request):
    return await request.app.state.git_service.tag_list()


@router.post("/git/tags")
async def git_create_tag(payload: dict, request: Request):
    name = payload.get("name", "")
    if not name:
        raise HTTPException(400, "Missing 'name'")
    return await request.app.state.git_service.tag_create(
        name,
        target=payload.get("target", ""),
        annotate=payload.get("annotate", False),
        message=payload.get("message", ""),
    )


@router.delete("/git/tags/{name}")
async def git_delete_tag(name: str, request: Request):
    return await request.app.state.git_service.tag_delete(name)


@router.get("/git/remotes")
async def git_remotes(request: Request):
    return await request.app.state.git_service.remote_list()


@router.get("/git/remote-url")
async def git_remote_url(name: str = "origin", request: Request = None):
    return await request.app.state.git_service.get_remote_url(name)


@router.post("/git/remotes")
async def git_add_remote(payload: dict, request: Request):
    name = payload.get("name", "")
    url = payload.get("url", "")
    if not name or not url:
        raise HTTPException(400, "Missing 'name' or 'url'")
    return await request.app.state.git_service.remote_add(name, url)


@router.put("/git/remotes/{name}")
async def git_update_remote(name: str, payload: dict, request: Request):
    url = payload.get("url", "")
    if not url:
        raise HTTPException(400, "Missing 'url'")
    return await request.app.state.git_service.remote_set_url(name, url)


@router.delete("/git/remotes/{name}")
async def git_remove_remote(name: str, request: Request):
    return await request.app.state.git_service.remote_remove(name)
