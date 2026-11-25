"""context.py —— 应用全局上下文单例 (Phase 47)

对标:
  Flask:  from flask import current_app          → current_app._get_current_object()
  Django: from django.apps import apps            → apps.get_app_config('myapp')
  VSCode: global extension context                → vscode.extensions.getExtension()
  Claude Code CLI: getApp() singleton             → import { getApp } from './app'

设计原则:
  - 单例 (module-level global) → 一个进程只有一个 app 实例
  - 启动时通过 lifespan 注入: set_app(app)
  - 工具层/任意代码通过: from context import get_app; app = get_app()
  - FastAPI Depends() 级别的类型安全需要全局路由重构 → Phase 49

与 sys.modules 扫描相比:
  - O(1) vs O(N) — 不遍历整个模块字典
  - 类型安全 — 显式的 get_app() -> FastAPI 实例
  - 并发安全 — set_app() 在 lifespan startup 中调用，仅一次
  - 可测试 — 测试中 set_app(test_client.app)
"""
from __future__ import annotations

import contextvars
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

_log = logging.getLogger("yuncat.context")
_app: "FastAPI | None" = None

# Phase 48 T2-5: 当前调用链的 agent_id（async-safe，对标 Flask request context）
# ToolService.execute 在调用 handler 前设置，工具 handler（如 delegate_to_agent）读取。
_current_agent_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_agent_id", default=None,
)

# Phase 108+: 当前调用链的能力策略开关集合（capability_flags）。
# 由 agent_tool_loop 在任务开始时从 task context 设置；ToolService.execute 据此
# 在分发前 fail-closed 拦截被禁用的功能（轻量档不调用 web_search/子 Agent 等）。
# 默认 None 表示未施加策略（legacy/系统调用/测试路径不受影响）。
_current_capability_flags: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "current_capability_flags", default=None,
)


# 2026-09-17: 能力档位（默认/MAX）也放进 context，供数值上限
# （services/capability_limits.py）在任何深度的调用链里取到；与 flags 同生同死。
_current_capability_level: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_capability_level", default=None,
)


def set_current_capability_level(level) -> contextvars.Token:
    return _current_capability_level.set(None if level is None else int(level))


def reset_current_capability_level(token: contextvars.Token) -> None:
    _current_capability_level.reset(token)


def get_current_capability_level() -> int | None:
    return _current_capability_level.get()


def set_current_capability_flags(flags: dict | None) -> contextvars.Token:
    """设置当前调用链的能力策略开关集合——agent_tool_loop 在任务开始时调用。"""
    return _current_capability_flags.set(flags)


def reset_current_capability_flags(token: contextvars.Token) -> None:
    """重置能力策略开关集合——agent_tool_loop 在任务结束时调用。"""
    _current_capability_flags.reset(token)


def get_current_capability_flags() -> dict | None:
    """获取当前调用链的能力策略开关集合——ToolService.execute 读取做硬门控。"""
    return _current_capability_flags.get()

# Task 7.8: spawn_subagent 递归深度追踪——async-safe，通过 asyncio.create_task
# 自动传播到子 Agent 后台任务。depth 0=主 Agent, 1=子, 2=孙, ...
_subagent_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "subagent_depth", default=0,
)

# Phase 108: 跨 Agent 委派递归深度追踪——对标 dsh delegationDepth。
# delegate_to_agent 链（A→B→C）的深度，0=主 Agent。ContextVar 跨 create_task 传播。
_delegation_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "delegation_depth", default=0,
)

# Phase 108: 子 Agent 隔离标记——spawn_subagent 时置 True，子 Agent 后台任务继承。
# 隔离子 Agent 的非只读工具被 classify_permission 钉死为 ALWAYS_DENY（永不需用户确认、
# 永不执行破坏性工具），对标 dsh child isolation（审批钉 never + toolFilter）。
_isolation_mode: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "isolation_mode", default=False,
)

# Task 7.15: trace_id / task_id 上下文——用于结构化 JSON 日志
# trace_id 由 HTTP 中间件在每个请求开始时生成（或读 X-Request-ID header）
# task_id 由任务执行路径设置（agent_tool_loop / workflow engine 等）
# async-safe: contextvars 自动隔离并发 task，对标 Flask request context
_current_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_trace_id", default="",
)
_current_task_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_task_id", default="",
)

# Phase 107: 当前任务语言上下文——用于子 Agent / 工具继承父任务 UI 语言
_current_language: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_language", default="zh",
)


def set_app(app: "FastAPI") -> None:
    """启动时注入 FastAPI 实例——由 lifespan startup 调用。

    必须在任何 await 之前调用。
    """
    global _app
    _app = app
    _log.debug("App context injected (pid=%s)", __import__("os").getpid())


def get_app() -> "FastAPI | None":
    """获取当前 FastAPI 实例——任意代码路径可用。

    对标: flask.current_app / django.apps.get_app_config()
    """
    return _app


def get_db():
    """获取 SQLite 连接——便捷方法（对标 Flask-SQLAlchemy db.session）"""
    app = _app
    if app is None:
        return None
    return getattr(app.state, "db", None)


def get_agent_service():
    """获取 AgentService——便捷方法"""
    app = _app
    if app is None:
        return None
    return getattr(app.state, "agent_service", None)


def get_event_bus():
    """获取 EventBus——便捷方法"""
    app = _app
    if app is None:
        return None
    return getattr(app.state, "event_bus", None)


def get_tool_service():
    """获取 ToolService——便捷方法"""
    app = _app
    if app is None:
        return None
    return getattr(app.state, "tool_service", None)


# ---- Phase 48 T2-5: 当前 agent_id 上下文 ----

def set_current_agent_id(agent_id: str | None) -> contextvars.Token:
    """设置当前调用链的 agent_id——由 ToolService.execute 在调用 handler 前设置。

    返回 Token 用于 reset（对标 Flask g 对象的 request-scoped 语义）。
    contextvars 自动隔离并发 task，async-safe。
    """
    return _current_agent_id.set(agent_id)


def reset_current_agent_id(token: contextvars.Token) -> None:
    """重置 agent_id 上下文——ToolService.execute 在 handler 调用后 reset。"""
    _current_agent_id.reset(token)


def get_current_agent_id() -> str | None:
    """获取当前调用链的 agent_id——工具 handler（如 delegate_to_agent）读取。

    返回 None 表示不在 ToolService.execute 调用链中（如直接 adapter.execute_tool）。
    """
    return _current_agent_id.get()


# ---- Task 7.8: spawn_subagent 递归深度上下文 ----

def get_subagent_depth() -> int:
    """获取当前调用链的子 Agent 递归深度。

    depth 0 = 主 Agent，1 = 第一层子 Agent，依此类推。
    通过 ContextVar + asyncio.create_task 自动传播到子 Agent 后台任务。
    """
    return _subagent_depth.get()


def set_subagent_depth(depth: int) -> contextvars.ContextVar:
    """设置子 Agent 递归深度——返回 Token 用于 reset。

    spawn_subagent 工具在调用 AgentService.spawn_subagent 前设置 depth+1，
    子 Agent 后台任务通过 asyncio.create_task 继承此值。
    """
    return _subagent_depth.set(depth)


def reset_subagent_depth(token) -> None:
    """重置子 Agent 递归深度——spawn_subagent 工具在调用后 reset。"""
    _subagent_depth.reset(token)


# ---- Phase 108: 跨 Agent 委派递归深度上下文 ----

def get_delegation_depth() -> int:
    """获取当前调用链的跨 Agent 委派深度（0=主 Agent）。"""
    return _delegation_depth.get()


def set_delegation_depth(depth: int) -> contextvars.Token:
    """设置委派深度——返回 Token 用于 reset。delegate_to_agent 调用前置 depth+1，
    子任务经 asyncio.create_task 继承此值。"""
    return _delegation_depth.set(depth)


def reset_delegation_depth(token) -> None:
    """重置委派深度——delegate_to_agent 调用后 reset。"""
    _delegation_depth.reset(token)


# ---- Phase 108: 子 Agent 隔离标记上下文 ----

def get_isolation_mode() -> bool:
    """当前执行上下文是否处于隔离子 Agent。隔离子 Agent 的非只读工具被钉死为 ALWAYS_DENY。"""
    return _isolation_mode.get()


def set_isolation_mode(enabled: bool) -> contextvars.Token:
    """设置隔离标记——spawn_subagent 在启动子 Agent 前置 True，子任务继承。
    返回 Token 用于 reset。"""
    return _isolation_mode.set(enabled)


def reset_isolation_mode(token) -> None:
    """重置隔离标记——spawn_subagent 调用后 reset。"""
    _isolation_mode.reset(token)


# ---- Task 7.15: trace_id / task_id 上下文 ----

def set_current_trace_id(trace_id: str) -> contextvars.Token:
    """设置当前请求的 trace_id——由 HTTP 中间件在每个请求开始时调用。

    返回 Token 用于 reset。contextvars 自动隔离并发请求，async-safe。
    trace_id 会注入到结构化 JSON 日志的 trace_id 字段。
    """
    return _current_trace_id.set(trace_id)


def reset_current_trace_id(token: contextvars.Token) -> None:
    """重置 trace_id 上下文——中间件在请求结束后 reset。"""
    _current_trace_id.reset(token)


def get_current_trace_id() -> str:
    """获取当前请求的 trace_id——日志 Filter / 任意代码路径读取。"""
    return _current_trace_id.get()


def set_current_task_id(task_id: str) -> contextvars.Token:
    """设置当前任务的 task_id——由任务执行路径调用。

    返回 Token 用于 reset。task_id 会注入到结构化 JSON 日志的 task_id 字段。
    """
    return _current_task_id.set(task_id)


def reset_current_task_id(token: contextvars.Token) -> None:
    """重置 task_id 上下文——任务执行路径在结束后 reset。"""
    _current_task_id.reset(token)


def get_current_task_id() -> str:
    """获取当前任务的 task_id——日志 Filter / 任意代码路径读取。"""
    return _current_task_id.get()


def set_current_language(language: str) -> contextvars.Token:
    """设置当前任务的语言——由任务执行路径调用。

    返回 Token 用于 reset。contextvars 自动隔离并发 task，async-safe。
    """
    return _current_language.set(language)


def reset_current_language(token: contextvars.Token) -> None:
    """重置语言上下文——任务执行路径在结束后 reset。"""
    _current_language.reset(token)


def get_current_language() -> str:
    """获取当前任务的语言——子 Agent / 工具 handler 读取。"""
    return _current_language.get()
