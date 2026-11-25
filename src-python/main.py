"""src-python/main.py —— Yuncat 后端入口

lifespan + WebSocket + Token + CORS + 路由聚合
API 端点分布在 routes/ 目录中，按功能域拆分：
  agents  tasks  sessions  workspace  workflows  knowledge  models  system  tools
"""
import asyncio
import hmac
import logging
import os
import sys
import time

# H6 修复：事件循环策略从模块顶层移入 __main__ 块——避免作为模块被导入时产生竞态条件
# （原代码在模块顶层调用，若 asyncio 已选好策略则变更可能产生竞态）

# ── 路径修复（必须在任何 from config / from repositories 之前）──
# 嵌入式 Python 的 _pth 文件会忽略 PYTHONPATH 环境变量。
# 此处不依赖 PYTHONPATH——自己把需要的路径加入 sys.path。
_this_dir = os.path.dirname(os.path.abspath(__file__))
_pref = "\\\\?\\"
if _this_dir.startswith(_pref):
    _this_dir = _this_dir[len(_pref):]
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
# 嵌入式 Python：确保 Lib/site-packages 也在路径中（_pth 文件负责 Lib，但有些包在 site-packages）
_embed_lib = os.path.join(os.path.dirname(sys.executable), "Lib", "site-packages") if getattr(sys, "frozen", False) or "python-embed" in sys.executable.lower() else ""
if _embed_lib and os.path.isdir(_embed_lib) and _embed_lib not in sys.path:
    sys.path.insert(1, _embed_lib)

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import (
    APP_VERSION, TOKEN, DB_PATH, DATA_DIR,
    DEFAULT_AGENT_ID,
    WORKFLOW_FROM_CHAT_MAX_STEPS,
    WORKFLOW_FROM_CHAT_STEP_USER_INPUT_BUDGET,
    WORKFLOW_FROM_CHAT_ANALYZE_INPUT_BUDGET,
    WORKFLOW_FROM_CHAT_IMPLEMENT_OUTPUT_BUDGET,
    WORKFLOW_FROM_CHAT_NAME_PREVIEW_CHARS,
    WORKFLOW_FROM_CHAT_DESCRIPTION_PREVIEW_CHARS,
    RATE_LIMIT_ENABLED,
    HTTP_MAX_BODY_SIZE,
)

# Round 1 S4: TOKEN 已读入 config.TOKEN 内存值——从 env 删除防子进程继承
# 履行 CLAUDE.md 铁律："YUNCAT_TOKEN 在 config.py 中通过 os.environ.pop() 读取后立即删除"
# 所有后续子进程（Hermes HA / OpenClaw / 持久 Shell / python script）均无法读取此变量
os.environ.pop("YUNCAT_TOKEN", None)

# ---- 日志（轮转，单文件 ≤ 10MB，保留 3 个备份） ----
# delay=True: 推迟文件打开到首次 emit，避免 Windows asyncio 句柄冲突。
# uvicorn 日志由 main() 中的 log_config 接管——禁止 uvicorn 写 stderr。
# Task 7.15: 结构化 JSON 日志——ELK/Datadog 可索引，含 trace_id/agent_id/task_id 字段
from logging.handlers import RotatingFileHandler
os.makedirs(DATA_DIR, exist_ok=True)
_log_file = os.path.join(DATA_DIR, "yuncat.log")
_handler = RotatingFileHandler(_log_file, maxBytes=10_485_760, backupCount=3, encoding="utf-8", delay=True)
from yuncat_utils.structured_logging import setup_structured_logging
setup_structured_logging(_handler, level=logging.INFO)
logger = logging.getLogger("yuncat")
# Phase 47 加固: 确认 YUNCAT_TOKEN 已从 env 删除（防子进程继承）
# settings.token 已在 config.py 加载时读取，env 中的值已无业务用途
logger.debug(
    "YUNCAT_TOKEN popped from env (config.TOKEN loaded=%s)",
    "yes" if TOKEN else "<empty>",
)

from yuncat_utils.process import find_free_port


# ---- 后台任务注册表 ----
_background_tasks: list[asyncio.Task] = []


# ---- Lifespan (从 lifespan.py 导入) ----
from lifespan import create_lifespan

# ---- 创建 App ----
app = FastAPI(lifespan=create_lifespan(DB_PATH, DATA_DIR, TOKEN, _background_tasks), title="Yuncat", version=APP_VERSION)

# ---- CORS ----
# Electron + 开发模式 Origin 兼容。放行 localhost 所有端口。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
    ],
    allow_origin_regex=r"https?://(127\.0\.0\.1|localhost)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# ---- 请求体大小限制中间件 ----
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Batch C M14: 修复 chunked transfer-encoding 绕过。

    原实现仅检查 content-length header——chunked 编码无此 header，
    导致 body 大小限制被完全绕过。现对 chunked 请求包装 receive()
    跟踪实际字节数，超限时返回 413。对标 nginx client_body_buffer_size
    + proxy_request_buffering 的 streaming 防护。
    """
    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > HTTP_MAX_BODY_SIZE:
                    return PlainTextResponse("Request body too large", status_code=413)
            except ValueError:
                return PlainTextResponse("Invalid content-length", status_code=400)
        else:
            # Batch C M14: 无 content-length 时检查 transfer-encoding
            transfer_encoding = request.headers.get("transfer-encoding", "").lower()
            if "chunked" in transfer_encoding:
                return await self._call_with_chunked_limit(request, call_next)
        return await call_next(request)

    async def _call_with_chunked_limit(self, request: Request, call_next):
        """对 chunked 请求包装 receive() 跟踪字节数。"""
        original_receive = request._receive
        received = 0

        async def limited_receive():
            nonlocal received
            message = await original_receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                received += len(body)
                if received > HTTP_MAX_BODY_SIZE:
                    raise _BodyTooLargeError()
            return message

        request._receive = limited_receive
        try:
            return await call_next(request)
        except _BodyTooLargeError:
            return PlainTextResponse("Request body too large", status_code=413)
        finally:
            request._receive = original_receive


class _BodyTooLargeError(Exception):
    """Batch C M14: 内部信号异常——chunked body 超限时中断读取。"""

app.add_middleware(BodySizeLimitMiddleware)


# ---- Batch 7.5: API 限流中间件 ----
# 注册在 token_middleware 之前 → 请求处理时晚于 token 认证，可复用认证后的上下文。
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Token Bucket 限流。 exempt 路径与健康检查直接放行。"""
    if not RATE_LIMIT_ENABLED:
        return await call_next(request)

    path = request.url.path
    from services.rate_limit import get_rate_limiter

    limiter = await get_rate_limiter()
    client = request.client
    key = f"ip:{client.host}" if client and client.host else "ip:unknown"
    allowed, retry_after = await limiter.is_allowed(key, request.method, path)
    if allowed:
        return await call_next(request)

    response = error_response(
        status_code=429,
        message="请求过于频繁，请稍后再试",
        code="RATE_LIMITED",
        details={"retry_after": round(retry_after, 2)},
    )
    response.headers["Retry-After"] = str(int(retry_after) + 1)
    return response


# ---- Token 认证中间件 ----
@app.middleware("http")
async def token_middleware(request: Request, call_next):
    """验证 Bearer Token（health 端点、OPTIONS 预检和 WebSocket 除外）。

    Batch 7.6: 同时支持静态 TOKEN 与有状态的 access token，保持向后兼容。
    """
    # OPTIONS 预检请求不携带 Authorization header，必须放行
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if path.startswith("/api/") and path not in ("/api/v1/health", "/api/v1/health/live", "/api/v1/health/ready"):
        auth = request.headers.get("Authorization")
        if not auth or not auth.startswith("Bearer "):
            return error_response(status_code=403, message="Invalid token")
        bearer = auth[7:]
        # 先校验静态 TOKEN（向后兼容）
        if hmac.compare_digest(bearer, TOKEN):
            request.state.auth_type = "static"
        else:
            # 再校验 access token
            from services.auth_service import validate_access_token
            if validate_access_token(bearer):
                request.state.auth_type = "access"
            else:
                return error_response(status_code=403, message="Invalid token")
    # /docs /redoc /openapi.json 也要求认证
    if path in ("/docs", "/redoc", "/openapi.json"):
        auth = request.headers.get("Authorization")
        if not auth or not auth.startswith("Bearer "):
            return error_response(status_code=403, message="Invalid token")
        bearer = auth[7:]
        from services.auth_service import validate_access_token
        if not hmac.compare_digest(bearer, TOKEN) and not validate_access_token(bearer):
            return error_response(status_code=403, message="Invalid token")
    response = await call_next(request)
    return response


# ---- Task 7.15: trace_id 中间件——为每个请求生成唯一 trace_id ----
# 注入 contextvars 供结构化 JSON 日志使用。对标 OpenTelemetry trace propagation。
# 优先读 X-Request-ID header（支持上游传入），否则生成 16 位 hex ID。
@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    """为每个 HTTP 请求注入 trace_id 到 contextvars——日志自动包含。"""
    from context import set_current_trace_id, reset_current_trace_id
    import uuid as _uuid
    trace_id = request.headers.get("X-Request-ID") or _uuid.uuid4().hex[:16]
    token = set_current_trace_id(trace_id)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = trace_id
        return response
    finally:
        reset_current_trace_id(token)


# ---- Task 7.16: 全局异常处理器——统一错误响应格式 ----
# 所有 raise HTTPException 自动统一为 {"code": "ERROR_CODE", "message": "...", "details": {...}}
# 前端 useApi.ts 的 ApiError 已支持从 message 字段提取友好消息（优先级：detail > message）
from schemas.error_response import register_exception_handlers, error_response
register_exception_handlers(app)


# ---- WebSocket (从 routes/ws.py 导入) ----
from routes.ws import register_ws_routes
register_ws_routes(app, TOKEN)

# 终端 WS 与 /ws 同级（不带 /api/v1 前缀）——前端 TerminalPanel 连的是 /ws/terminal。
# 2026-09-15 修复：此前它挂在带前缀的 router 上（/api/v1/ws/terminal），前后端路径不一致，
# 集成终端从未连上；守卫见 tests/contract/test_api_surface_match.py 的 WS 用例。
from routes.workspace import ws_terminal as _ws_terminal
app.add_api_websocket_route("/ws/terminal", _ws_terminal)
# 2026-09-16: LSP 通道——与终端一致，不走 REST 前缀（前端按 ws://127.0.0.1:<port>/ws/lsp 连）
from routes.lsp import ws_lsp as _ws_lsp  # noqa: E402
app.add_api_websocket_route("/ws/lsp", _ws_lsp)

# ---- 核心端点（不归属任何子路由） ----
@app.get("/api/v1/health")
async def health(request: Request):
    stage = getattr(request.app.state, "startup_stage", "unknown")
    vector_ok = getattr(request.app.state, "vector_storage_available", True)
    return {
        "status": "ok",
        "adapters": list(request.app.state.adapters.keys()),
        "startup_stage": stage,
        "vector_storage": vector_ok,
    }


# Task 7.14: 健康检查 readiness/liveness 分离——对标 Kubernetes 探针模式
# liveness: 进程存活即可（始终 200）——用于容器重启判定
# readiness: 应用就绪才 200（startup_stage=="ready"）——用于流量路由判定
@app.get("/api/v1/health/live")
async def health_live():
    """Liveness probe——进程能响应即存活。"""
    return {"status": "alive"}


@app.get("/api/v1/health/ready")
async def health_ready(request: Request):
    """Readiness probe——startup_stage=='ready' 时才返回 200，否则 503。"""
    stage = getattr(request.app.state, "startup_stage", "unknown")
    if stage == "ready":
        return {"status": "ready", "startup_stage": stage}
    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "startup_stage": stage},
    )


@app.get("/api/v1/monitoring")
async def monitoring(request: Request):
    return await request.app.state.agent_service.get_monitoring_data()


@app.get("/api/v1/token-usage")
async def token_usage(days: int = 30, request: Request = None):
    """Token 用量查询（按天聚合）。

    数据源：analytics_events 的 token 用量事件（本地账本）。
    2026-09-14：移除厂商 API 拉取——厂商没有逐次用量查询接口，且原先那条
    对 DeepSeek ``/v1/usage``（官方文档并不存在该端点）的调用恒为 None，
    属于"看着能用其实永远不生效"的死链路。
    """
    from services.token_tracker import get_daily_usage

    return {"ok": True, "daily": await get_daily_usage(request.app.state.db, days)}


@app.get("/api/v1/shutdown")
async def shutdown():
    """优雅关闭——Electron 窗口关闭前调用此端点触发 lifespan cleanup。

    Batch A H2: 原 sys.exit(0) 在 asyncio task 中只取消该 task，不会停止事件循环。
    改用 daemon 线程发送 SIGINT——uvicorn 的信号处理器设置 should_exit=True，
    触发 lifespan finally 块（flush DB, cancel tasks, close conn）。
    signal.raise_signal 在 CPython 中将信号投递到主线程的信号处理器执行。
    """
    import signal as _signal
    import threading as _threading
    import time as _time

    def _delayed_shutdown():
        _time.sleep(0.1)  # 让 HTTP 响应先返回
        _signal.raise_signal(_signal.SIGINT)

    _threading.Thread(target=_delayed_shutdown, daemon=True).start()
    return {"ok": True, "message": "shutting down"}


# Phase 27: Slash Command 列表
@app.get("/api/v1/slash-commands")
async def list_slash_commands(request: Request):
    return request.app.state.slash_commands.list_all()


# Phase 64: /api/v1/commands 别名——对标 Claude Code / Cursor 命名风格
# 与 /api/v1/slash-commands 返回相同数据（含 local 标记）
@app.get("/api/v1/commands")
async def list_commands(request: Request):
    return request.app.state.slash_commands.list_all()



from routes.agents import router as agents_router
from routes.attachments import router as attachments_router
from routes.jobs import router as jobs_router
from routes.schedules import router as schedules_router
from routes.tasks import router as tasks_router
from routes.sessions import router as sessions_router
from routes.workspace import router as workspace_router
from routes.workflows import router as workflows_router
from routes.knowledge import router as knowledge_router
from routes.models import router as models_router
from routes.system import router as system_router
from routes.tools import router as tools_router
from routes.skills import router as skills_router
from routes.mcp import router as mcp_router
from routes.config import router as config_router
from routes.roles import router as roles_router
from routes.snapshot import router as snapshot_router
from routes.notify import router as notify_router

# Batch 7.6: Bearer token 兑换与刷新
from routes.auth import router as auth_router
app.include_router(auth_router)

app.include_router(agents_router)
app.include_router(attachments_router)
app.include_router(jobs_router)
app.include_router(schedules_router)
app.include_router(tasks_router)
app.include_router(sessions_router)
app.include_router(workspace_router)
from routes.lsp import router as lsp_router  # noqa: E402
app.include_router(lsp_router)
# 2026-09-16: 语言服务器子进程随应用退出回收。
# 放在这里而不是 lifespan 里：lifespan.py 的 create_lifespan/lifespan 已是超大函数，
# 静态棘轮禁止它们再变长；模块级注册不碰函数体。进程被强杀时另有 atexit 兜底。
from services import lsp_manager as _lsp_manager  # noqa: E402
# 注：这个 FastAPI 版本没有 app.add_event_handler，事件要挂在 router 上；
app.router.add_event_handler("shutdown", _lsp_manager.stop_all)
app.include_router(workflows_router)
app.include_router(knowledge_router)
app.include_router(models_router)
app.include_router(system_router)
# Batch 5.1: Arena 盲测 A/B
from routes.arena import router as arena_router
app.include_router(arena_router)
# Batch 5.3: Self-Evolution 质量门控 pending install 审批 API
from routes.evolution import router as evolution_router
app.include_router(evolution_router)
app.include_router(tools_router)
app.include_router(skills_router)
app.include_router(mcp_router)
app.include_router(config_router)
app.include_router(roles_router)
app.include_router(snapshot_router)
app.include_router(notify_router)
from routes.hooks import router as hooks_router
app.include_router(hooks_router)
from routes.supervisor import router as supervisor_router
app.include_router(supervisor_router)
from routes.completion import router as completion_router
app.include_router(completion_router)
# Phase 72: 统一 PromptLibrary 目录——聚合 DB roles + YAML roles/behaviors + workflow templates
from routes.prompt_library import router as prompt_library_router
app.include_router(prompt_library_router)
# Phase 74: PromptAgent 后台服务——推荐角色/行为/工作流 + 渲染 prompt 片段
from routes.prompt_agent import router as prompt_agent_router
app.include_router(prompt_agent_router)
# B: 用户偏好管理接口（捕获/列出/删除）
from routes.preferences import router as preferences_router
app.include_router(preferences_router)
# Phase 79: 长任务分解 API——decompose / decomposition / nodes/{id}/decompose
from routes.long_task import router as long_task_router
app.include_router(long_task_router)
# Phase 81: 长任务监控 API——view / inject / pause / resume / spec / ask
from routes.task_supervisor import router as task_supervisor_router
app.include_router(task_supervisor_router)
# Phase 83: 终审 Agent API——手动触发 pre/post_execution / pre_dangerous_tool 审查
from routes.final_review import router as final_review_router
app.include_router(final_review_router)
# Phase 84: 对抗验证 Agent API——手动触发 goal/plan/output 对抗验证
from routes.adversarial import router as adversarial_router
app.include_router(adversarial_router)
# Phase 85: 标准/文档查询 Agent API——手动触发 4 步降级文档查询
from routes.standards_lookup import router as standards_lookup_router
app.include_router(standards_lookup_router)
# Phase 96: 配置备份/恢复 API——加密导出 yuncat.db + cred.key / staging 恢复
from routes.backup import router as backup_router
app.include_router(backup_router)
# Phase 96-5: 日志查看器 API——只读 tail + 下载 yuncat.log
from routes.logs import router as logs_router
app.include_router(logs_router)

# H4: 审计日志查看 API——只读查询 audit_log 表（Round 1 S6 已建表）
from routes.audit import router as audit_router
app.include_router(audit_router)

# Batch 8.6: 本地错误事件查询/清空 API——error_events 表持久化 HTTP 错误响应
from routes.errors import router as errors_router
app.include_router(errors_router)

# Phase 107: 统一审批 Inbox
from routes.approval import router as approval_router
from routes.workflow_triggers import router as workflow_triggers_router
app.include_router(approval_router)
app.include_router(workflow_triggers_router)

# Phase 104: GitHub/GitLab PR 集成
from routes.vcs import router as vcs_router
app.include_router(vcs_router)

# Task 6.3: @web: 引用支持——web search 端点（委托 OC web_search，不可用时回退 URL hint）
from routes.web_search import router as web_search_router
app.include_router(web_search_router)

# ---- P1-P3: 用户体验端点 ----

# 2026-08-31: _fts5_phrase 已随查询逻辑一并下沉到
# repositories/conversations_repo.search_conversations 单源（HTTP 端点与
# agent 工具共用），此处不再重复定义。


@app.get("/api/v1/conversations/search")
async def search_conversations(q: str = "", limit: int = 20, request: Request = None):
    """P2: 全文搜索所有 session 对话

    Batch 3b B4: FTS5 索引查询优先（conversations 是增长最快的表，原
    LIKE '%kw%' 全表扫描 O(n) 随历史膨胀线性劣化）。
    2026-08-31: 查询逻辑抽取到 repositories/conversations_repo.search_conversations
    单源——HTTP 端点与 agent 工具 search_conversation_history 共用，防两端漂移。
    """
    from repositories.conversations_repo import search_conversations as _search
    results = await _search(request.app.state.db, q, limit)
    return {"results": results}



@app.post("/api/v1/tasks/{task_id}/save-as-workflow")
async def save_task_as_workflow(task_id: str, request: Request):
    """P2: 将已完成对话保存为 DAG 模板。

    智能提取 Agent 的输出中的步骤结构来生成节点——
    如果输出用 Markdown 标题标注了步骤，每个步骤成为独立节点；
    否则退化为基本的 3 节点分析→执行→验证结构。
    """
    import uuid, re, json

    cursor = await request.app.state.db.execute(
        "SELECT input, output, agent_id, session_id FROM tasks WHERE id=? AND status='completed'",
        (task_id,),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Task not found or not completed")

    user_input = row[0]
    agent_output = row[1] or ""
    agent_id = row[2] or DEFAULT_AGENT_ID

    # 解析 Agent 输出中的步骤标题（## N. 或 ## Step N: 或 ### N.）
    step_pattern = re.findall(r'^#{2,3}\s*(?:Step\s*)?(\d+)[.:]\s*(.+)$', agent_output, re.MULTILINE)
    if len(step_pattern) >= 2:
        nodes = []
        for i, (_, title) in enumerate(step_pattern[:WORKFLOW_FROM_CHAT_MAX_STEPS]):
            nodes.append({
                "id": f"step_{i+1}",
                "type": "default",
                "prompt": f"## 步骤 {i+1}: {title.strip()}\n\n上下文: {user_input[:WORKFLOW_FROM_CHAT_STEP_USER_INPUT_BUDGET]}\n\n执行此步骤。前面步骤的结果将在上下文中提供。",
            })
        edges = [{"from": f"step_{i}", "to": f"step_{i+1}"} for i in range(1, len(nodes))]
    else:
        nodes = [
            {"id": "analyze", "type": "default",
             "prompt": f"分析需求并设计方案: {user_input[:WORKFLOW_FROM_CHAT_ANALYZE_INPUT_BUDGET]}"},
            {"id": "implement", "type": "default",
             "prompt": f"按方案实施: {agent_output[:WORKFLOW_FROM_CHAT_IMPLEMENT_OUTPUT_BUDGET] if agent_output else user_input[:WORKFLOW_FROM_CHAT_ANALYZE_INPUT_BUDGET]}"},
            {"id": "verify", "type": "default",
             "prompt": "验证实现是否符合需求，运行测试并报告结果"},
        ]
        edges = [{"from": "analyze", "to": "implement"}, {"from": "implement", "to": "verify"}]

    graph = {"nodes": nodes, "edges": edges}
    wid = str(uuid.uuid4())[:12]
    await request.app.state.db.execute(
        "INSERT INTO workflows(id, name, description, graph, is_template, created_at, updated_at) VALUES(?,?,?,?,1,?,?)",
        (wid, f"保存的对话: {user_input[:WORKFLOW_FROM_CHAT_NAME_PREVIEW_CHARS]}",
         f"从 Agent {agent_id} 的回复导入。原始需求: {user_input[:WORKFLOW_FROM_CHAT_DESCRIPTION_PREVIEW_CHARS]}",
         json.dumps(graph, ensure_ascii=False), int(time.time()), int(time.time())),
    )
    await request.app.state.db.commit()
    return {"ok": True, "workflow_id": wid, "name": user_input[:WORKFLOW_FROM_CHAT_NAME_PREVIEW_CHARS], "nodes": len(nodes)}


# ---- 手动上下文压缩 ----
@app.post("/api/v1/sessions/{session_id}/compact")
async def compact_session(session_id: str, request: Request):
    """手动触发上下文压缩——无需等待 98% 自动阈值。
    用户可以输入 /compact 指令或调用此端点。
    """
    if not hasattr(request.app.state, "memory_service"):
        return {"ok": False, "error": "memory_service not available"}
    model_cfg = {}
    if hasattr(request.app.state, "model_config"):
        try:
            model_cfg = await request.app.state.model_config.get_active_config()
        except Exception:
            logger.debug("获取活跃模型配置失败", exc_info=True)
    result = await request.app.state.memory_service.compact_now(session_id, model_cfg)
    return {
        "ok": result is not None,
        "session_id": session_id,
        "compacted_messages": len(result) if result else 0,
        "summary": result.get("session_intent", "") if result else "",
    }


@app.get("/api/v1/sessions/{session_id}/context-stats")
async def session_context_stats(session_id: str, request: Request):
    """查看会话上下文占用情况——返回 token 估算，帮助用户决定是否手动压缩。"""
    if not hasattr(request.app.state, "memory_service"):
        return {"ok": False, "error": "memory_service not available"}
    ms = request.app.state.memory_service
    conv = ms._sessions.get(session_id, [])

    from config import MAX_CONTEXT_FALLBACK_CHARS, resolve_context_window
    model_cfg = {}
    context_window = MAX_CONTEXT_FALLBACK_CHARS
    if hasattr(request.app.state, "model_config"):
        try:
            model_cfg = await request.app.state.model_config.get_active_config()
            context_window = resolve_context_window(model_cfg)
        except Exception:
            logger.debug("解析上下文窗口失败", exc_info=True)

    # Phase 56-P3: 优先使用当前 Adapter 的 last_usage（厂商 API 实时返回）
    adapter = None
    try:
        cursor = await request.app.state.db.execute(
            "SELECT agent_id FROM tasks WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            adapter = request.app.state.adapters.get(row[0])
    except Exception:
        logger.debug("查询 session 对应 adapter 失败", exc_info=True)
    if adapter is None:
        for a in (request.app.state.adapters or {}).values():
            if getattr(a, "last_usage", {}).get("prompt_tokens", 0) > 0:
                adapter = a
                break

    # Phase 38 / Phase 56-P3: Adapter last_usage 优先 → DB → 厂商 tokenizer API → tiktoken → CJK
    from services.token_tracker import estimate_context_usage
    usage = await estimate_context_usage(
        request.app.state.db, session_id, conv,
        adapter=adapter, model_id=model_cfg.get("model", "gpt-4"),
        model_cfg=model_cfg, use_online=True,
    )
    estimated_tokens = usage["estimated_tokens"]
    return {
        "ok": True,
        "session_id": session_id,
        "message_count": len(conv),
        "estimated_tokens": estimated_tokens,
        "source": usage.get("source", "cjk_estimate"),
        "last_real_prompt_tokens": usage.get("last_real_prompt_tokens"),
        "last_real_completion_tokens": usage.get("last_real_completion_tokens"),
        "context_window": context_window,
        "usage_percent": round(estimated_tokens / context_window * 100, 1) if context_window else 0,
        "threshold_98pct": int(context_window * 0.98),
    }


@app.get("/api/v1/sessions/{session_id}/messages-indexed")
async def session_messages_indexed(session_id: str, request: Request):
    """获取带序号的会话消息列表（供前端右键引用）。"""
    if not hasattr(request.app.state, "agent_service"):
        raise HTTPException(503, "agent_service not available")
    ag = request.app.state.agent_service
    msgs = await ag.get_session_messages_indexed(session_id)
    return {"ok": True, "session_id": session_id, "messages": msgs}


# ---- Main ----
if __name__ == "__main__":
    # H6 修复：事件循环策略在此设置（仅作为主程序运行时，非模块导入时）
    # Windows: ProactorEventLoop 默认在连接重置时触发 OSError 22 / WinError 10054，
    # 造成 WS 断开 + HTTP OPTIONS 返回 400。Selector 事件循环处理此场景更稳健。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # G2 修复: 接受 Electron 传入的 --port 参数——确保前后端端口一致
    import argparse as _argparse
    _ap = _argparse.ArgumentParser()
    _ap.add_argument("--port", type=int, default=0)
    _args = _ap.parse_args()
    if _args.port:
        port = _args.port
    else:
        port = find_free_port()
    os.environ["_YUNCAT_PORT"] = str(port)

    logger.info("Backend starting on 127.0.0.1:%d", port)

    # 父进程死亡自监控：Electron 强杀/崩溃时，后端检测到父进程消失即优雅退出，
    # 避免成为孤儿进程（对齐主流做法——子进程监听父进程，父消失则自尽）。
    # Electron 在 spawn 时注入 YUNCAT_PARENT_PID（main.js spawnBackend:325）。
    _parent_pid = os.environ.get("YUNCAT_PARENT_PID")
    if _parent_pid and _parent_pid.isdigit():
        import threading as _threading
        import signal as _signal
        import time as _time

        def _watch_parent():
            import psutil
            pid = int(_parent_pid)
            while True:
                _time.sleep(5.0)
                if not psutil.pid_exists(pid):
                    logger.warning(
                        "Parent process %s gone — graceful shutdown to avoid orphan", pid)
                    # 与 /shutdown 同机制：daemon 线程发 SIGINT → 主线程 signal handler
                    # 置 should_exit → lifespan finally（flush DB、adapter.stop）。
                    _signal.raise_signal(_signal.SIGINT)
                    return

        _threading.Thread(target=_watch_parent, daemon=True, name="parent-watchdog").start()

    # 接管 uvicorn 日志配置：禁止其写 stderr（Windows 下 stderr 经 Rust pipe
    # 落盘会与 asyncio ProactorEventLoop 冲突触发 OSError 22 → ConnectionResetError）。
    # 所有 uvicorn 日志透传到 root logger → 走我们的 RotatingFileHandler。
    import uvicorn
    _uv_log = uvicorn.config.LOGGING_CONFIG
    _uv_log["loggers"]["uvicorn.access"] = {"handlers": [], "propagate": True, "level": "INFO"}
    _uv_log["loggers"]["uvicorn.error"] = {"handlers": [], "propagate": True, "level": "INFO"}
    _uv_log["loggers"]["uvicorn"] = {"handlers": [], "propagate": True, "level": "INFO"}

    uvicorn.run(
        app, host="127.0.0.1", port=port,
        log_config=_uv_log,
        timeout_graceful_shutdown=5,
    )
