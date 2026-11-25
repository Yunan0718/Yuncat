"""harness/budget_manager.py —— 预算硬计数器（Phase C）

设计要点（docs/budget-reliability-design.md §2-§4）：
  - contextvars.ContextVar 挂 attempt 上下文：并发安全、无全局单例、无跨任务污染。
  - 计数项：tool_invocations（ToolService 埋点）、tokens（adapter last_usage 增量累加）、
    seconds（attempt 墙钟，检查点驱动校验——不另起定时器，零开销）。
  - 超限语义：current >= limit 即触发（严格边界）。
  - 超限回调注册表：set_exceeded_handler——由 lifespan 启动时注册实际处理
    （kill + 状态迁移）；tool_service / adapters 只依赖本模块纯计数 API，
    不感知 harness 细节；默认 handler no-op（仅记日志）。
  - 无 attempt 上下文（非 harness 场景）时全部 API no-op——零侵入现有工具/对话路径。

注意：tokens 计数口径 = last_usage["total_tokens"] 的增量累加（adapter 的 last_usage
每次请求会被覆盖，必须由本模块维护累计值）。
"""
from __future__ import annotations

import logging
import time as _time
from contextvars import ContextVar, Token
from typing import Any, Callable

from harness.schemas import ExecutionBudget

_log = logging.getLogger("yuncat.harness.budget")

BUDGET_TYPE_TOOL = "tool_invocations"
BUDGET_TYPE_TOKENS = "tokens"
BUDGET_TYPE_SECONDS = "seconds"


class BudgetExceeded(Exception):
    """预算超限。budget_type: tool_invocations / tokens / seconds。"""

    def __init__(self, budget_type: str, limit: int, current: int, *, task_id: str = ""):
        self.budget_type = budget_type
        self.limit = limit
        self.current = current
        self.task_id = task_id
        super().__init__(
            f"budget_exceeded[{budget_type}] task={task_id or '-'} current={current} limit={limit}"
        )


class _BudgetState:
    """attempt 级预算状态——由 ContextVar 持有，不落全局单例。

    exceeded/exceeded_type：任一计数超限后置位（熔断）——adapter 热路径
    吞掉异常（不中断响应），但后续工具调用经 count_tool_invocation 直接拒绝，
    agent 循环据此感知超限并停止。避免"token 超限但工具继续执行"的缺口。
    """

    __slots__ = (
        "task_id", "attempt_id", "budget", "started_at",
        "tool_invocations", "tokens", "exceeded", "exceeded_type", "_token",
    )

    def __init__(self, task_id: str, attempt_id: str, budget: ExecutionBudget):
        self.task_id = task_id
        self.attempt_id = attempt_id
        self.budget = budget
        self.started_at = _time.time()
        self.tool_invocations = 0
        self.tokens = 0
        self.exceeded = False
        self.exceeded_type: str | None = None
        self._token: Token | None = None

    def elapsed_seconds(self) -> float:
        return _time.time() - self.started_at


_budget_var: ContextVar[_BudgetState | None] = ContextVar("harness_budget", default=None)

# 超限回调注册表——默认 no-op（仅日志），lifespan 启动时注册实际处理。
_exceeded_handler: Callable[[BudgetExceeded], Any] | None = None


def set_exceeded_handler(handler: Callable[[BudgetExceeded], Any] | None) -> None:
    """注册/注销超限回调。handler 为同步约定；handler 异常被吞（热路径防御）。"""
    global _exceeded_handler
    _exceeded_handler = handler


def enter_attempt(task_id: str, attempt_id: str, budget: ExecutionBudget) -> None:
    """挂 attempt 预算上下文。预算全 None 也挂（校验逐项放行）。

    支持嵌套：内层 enter 覆盖外层，配对 exit 后恢复外层。
    """
    state = _BudgetState(task_id, attempt_id, budget)
    state._token = _budget_var.set(state)


def exit_attempt() -> None:
    """退出当前 attempt 上下文，恢复外层。无上下文/重复调用 no-op。"""
    state = _budget_var.get()
    if state is None or state._token is None:
        return
    _budget_var.reset(state._token)


def current() -> _BudgetState | None:
    """读取当前 attempt 预算状态（调试/审计用）。无上下文返回 None。"""
    return _budget_var.get()


def _raise_exceeded(state: _BudgetState, budget_type: str, limit: int, current_val: int) -> None:
    """置熔断标记 → 触发已注册回调（先）→ 抛 BudgetExceeded（后）。

    回调异常不影响主路径；熔断标记使后续工具调用（count_tool_invocation）
    直接拒绝，即使本异常在 adapter 热路径被吞掉。
    """
    state.exceeded = True
    state.exceeded_type = budget_type
    exc = BudgetExceeded(budget_type, limit, current_val, task_id=state.task_id)
    handler = _exceeded_handler
    if handler is not None:
        try:
            handler(exc)
        except Exception:
            _log.warning("budget exceeded handler failed", exc_info=True)
    raise exc


def _check_seconds(state: _BudgetState) -> None:
    limit = state.budget.max_seconds
    if limit is None:
        return
    elapsed = int(state.elapsed_seconds())
    if elapsed >= limit:
        _raise_exceeded(state, BUDGET_TYPE_SECONDS, limit, elapsed)


def count_tool_invocation() -> None:
    """工具执行计数（真正执行前调用）。超限触发回调并抛 BudgetExceeded。无上下文 no-op。

    已熔断（tokens/seconds 超限被 adapter 吞掉后）：直接拒绝，不再计数。
    """
    state = _budget_var.get()
    if state is None:
        return
    if state.exceeded:
        _raise_exceeded(state, state.exceeded_type or BUDGET_TYPE_TOOL, 0, state.tool_invocations)
    state.tool_invocations += 1
    limit = state.budget.max_tool_invocations
    if limit is not None and state.tool_invocations >= limit:
        _raise_exceeded(state, BUDGET_TYPE_TOOL, limit, state.tool_invocations)
    _check_seconds(state)


def accumulate_tokens(usage: dict) -> None:
    """adapter 汇报 usage 时增量累加 total_tokens。无上下文 no-op。

    usage 缺少 total_tokens 时回退 prompt_tokens + completion_tokens；
    两者皆缺按 0 处理（不抛、不阻断既有返回路径）。
    """
    state = _budget_var.get()
    if state is None:
        return
    total = usage.get("total_tokens")
    if total is None:
        total = int(usage.get("prompt_tokens", 0) or 0) + int(usage.get("completion_tokens", 0) or 0)
    state.tokens += int(total or 0)
    limit = state.budget.max_tokens
    if limit is not None and state.tokens >= limit:
        _raise_exceeded(state, BUDGET_TYPE_TOKENS, limit, state.tokens)
    _check_seconds(state)
