"""schemas/error_response.py —— Task 7.16: 统一错误响应格式

统一格式：{"code": "ERROR_CODE", "message": "...", "details": {...}}

对标：
- RFC 7807 (Problem Details for HTTP APIs) —— HTTP 错误响应互联网标准
- JSON:API errors —— {"errors": [{"code", "title", "detail"}]}
- FastAPI 社区惯例 —— HTTPException 返回 {"detail": "..."}

设计决策：
- code 字段为字符串常量（如 "NOT_FOUND"），便于前端编程式判断
- message 字段为人类可读描述，前端直接展示给用户
- details 字段为结构化补充信息（如验证错误详情），可选
- 通过全局异常处理器自动统一所有 HTTPException——无需逐个修改 raise HTTPException
- 前端 useApi.ts 的 ApiError 已支持从 detail/message 字段提取友好消息（优先级：
  detail > message > 纯文本 > statusText），统一后前端自动兼容

Batch 8.6: 错误事件持久化到 error_events 表（fire-and-forget，不阻塞响应）。
- 4xx 不记录 stack_trace（避免噪音膨胀 DB）
- 5xx 记录完整 traceback.format_exc()
- 启动早期 db 不可用时不崩溃（try/except 兜底）
- 不上报外部（CLAUDE.md "数据不出本机"）——与 Sentry 等遥测服务语义不同
"""
import logging
import traceback
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("yuncat")

# HTTP status code → error code 映射
# 对标 HTTP 标准状态码语义，覆盖 Yuncat 所有路由使用的状态码
_STATUS_TO_CODE: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    422: "UNPROCESSABLE_ENTITY",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
    504: "GATEWAY_TIMEOUT",
}


def error_response(
    status_code: int = 400,
    message: str = "",
    code: str | None = None,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """统一错误响应 helper。

    返回 JSONResponse，body 格式：
    ``{"code": "ERROR_CODE", "message": "...", "details": {...}}``

    Args:
        status_code: HTTP 状态码（如 404, 400, 500）
        message: 人类可读的错误描述，前端直接展示给用户
        code: 错误代码（默认从 status_code 自动推导，如 404 → "NOT_FOUND"）
        details: 额外结构化详情（如验证错误列表），默认空 dict
    """
    if code is None:
        code = _STATUS_TO_CODE.get(status_code, "ERROR")
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "details": details or {},
        },
    )


async def _persist_error_event(
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    stack_trace: str | None = None,
) -> None:
    """Batch 8.6: fire-and-forget 持久化错误事件到 error_events 表。

    设计要点：
    - 任何异常都不阻塞响应——外层调用方已 try/except 兜底
    - 启动早期 db 不可用时跳过（request.app.state 可能未初始化）
    - 仅 5xx 记录 stack_trace（4xx 是客户端错误，无堆栈噪音价值）
    - 提取 trace_id / task_id / agent_id 上下文（若 headers/state 中存在）
    """
    try:
        db = getattr(request.app.state, "db", None)
        if db is None:
            return
        # 上下文提取——失败不影响持久化
        trace_id = ""
        task_id = ""
        agent_id = ""
        try:
            trace_id = request.headers.get("x-trace-id", "") or ""
        except Exception:
            pass
        try:
            # state 上的上下文由中间件填充（若有）
            state_tid = getattr(request.state, "task_id", None)
            if state_tid:
                task_id = str(state_tid)
            state_aid = getattr(request.state, "agent_id", None)
            if state_aid:
                agent_id = str(state_aid)
        except Exception:
            pass
        await db.execute(
            "INSERT INTO error_events(error_code, status_code, message, path, method, "
            "trace_id, task_id, agent_id, stack_trace) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                error_code,
                status_code,
                message[:2000] if message else "",
                request.url.path[:500] if request.url.path else "",
                request.method[:10] if request.method else "",
                trace_id,
                task_id,
                agent_id,
                stack_trace,
            ),
        )
        await db.commit()
    except Exception:
        # 完全静默——错误持久化失败不能影响错误响应本身
        logger.debug("Batch 8.6: persist error_event failed", exc_info=True)


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器——统一所有错误响应格式。

    处理三类异常：
    1. ``HTTPException`` —— FastAPI 标准 HTTP 异常（157 处 raise HTTPException 自动统一）
    2. ``RequestValidationError`` —— Pydantic 请求体验证失败（422）
    3. ``Exception`` —— 兜底未捕获异常（返回 500，不泄露堆栈给客户端）

    注册后所有 ``raise HTTPException(status, detail)`` 自动返回统一格式，
    无需逐个修改路由代码。

    Batch 8.6: 每个 handler 内 fire-and-forget 持久化错误事件到 error_events 表。
    """

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException):
        # detail 可能是字符串、dict 或其他类型
        if isinstance(exc.detail, dict):
            # dict 类型：尝试提取 message，其余字段归入 details
            message = str(exc.detail.get("message") or exc.detail.get("detail") or "")
            extra = {k: v for k, v in exc.detail.items() if k not in ("message", "detail")}
            details = extra or None
        else:
            message = str(exc.detail) if exc.detail is not None else ""
            details = None
        error_code = _STATUS_TO_CODE.get(exc.status_code, "ERROR")
        # 4xx 不记录 stack（客户端错误，无堆栈价值）；5xx 记录
        stack = traceback.format_exc() if exc.status_code >= 500 else None
        await _persist_error_event(
            request, exc.status_code, error_code, message, stack_trace=stack,
        )
        return error_response(
            status_code=exc.status_code,
            message=message,
            details=details,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request: Request, exc: RequestValidationError):
        # 422 客户端错误——不记录 stack
        await _persist_error_event(
            request, 422, "VALIDATION_ERROR", "请求参数验证失败",
        )
        return error_response(
            status_code=422,
            message="请求参数验证失败",
            code="VALIDATION_ERROR",
            details={"errors": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def _generic_exception_handler(request: Request, exc: Exception):
        # 记录完整堆栈到日志，但不返回给客户端（安全考虑）
        logger.exception(
            "Unhandled exception in %s %s",
            request.method,
            request.url.path,
        )
        # 5xx——记录完整 stack 便于排查
        stack = traceback.format_exc()
        await _persist_error_event(
            request, 500, "INTERNAL_ERROR", "内部服务器错误", stack_trace=stack,
        )
        return error_response(
            status_code=500,
            message="内部服务器错误",
            code="INTERNAL_ERROR",
        )
