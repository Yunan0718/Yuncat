"""services/agent/budget.py —— Token 预算与生命周期事件（从 agent_tool_loop 拆分）

Phase 56-P2: 动态字符预算。
AUDIT #13: Token 预算检查。
Task 7.5 / 7.26: Token 成本持久化 + 全链路追踪。
Phase 86: task_lifecycle — stall_check + estimate 事件推送。
采用委托模式: 模块级函数接收 self 作为首参（对齐 agent_tool_loop.py 模式）。
"""
import logging

from config import calc_context_budget
from services.llm_usage import TOKEN_USAGE_EVENTS

from .progress import (
    _evaluate_round_progress,
    _stall_suggestion,
    _compute_estimate,
)

_log = logging.getLogger("yuncat.agent")


def _init_loop_budgets(model_cfg: dict) -> dict:
    """Phase 56-P2: 计算工具循环的动态字符预算（按模型上下文窗口百分比）。

    6c-1 修复：从 execute_tool_loop 提取——纯计算函数，无副作用。
    """
    return {
        "assistant_msg": calc_context_budget(model_cfg, 0.10, min_chars=8000, max_chars=40000),
        "reasoning_msg": calc_context_budget(model_cfg, 0.08, min_chars=8000, max_chars=50000),
        "round_text_log": calc_context_budget(model_cfg, 0.015, min_chars=2000, max_chars=8000),
        "snapshot_generated": calc_context_budget(model_cfg, 0.008, min_chars=1000, max_chars=4000),
        "snapshot_thinking": calc_context_budget(model_cfg, 0.008, min_chars=1000, max_chars=4000),
        "stream_error_preview": calc_context_budget(model_cfg, 0.002, min_chars=300, max_chars=1500),
    }


async def _load_token_budget(self, task_id: str) -> tuple[int, int]:
    """AUDIT #13: 从 tasks 表读取 max_budget_tokens / used_tokens。

    6c-1 修复：从 execute_tool_loop 提取——单次 DB 读，返回 (limit, used)。
    失败时返回 (0, 0) 表示无预算限制。
    """
    try:
        _cur = await self.db.execute(
            "SELECT max_budget_tokens, used_tokens FROM tasks WHERE id=?", (task_id,)
        )
        _row = await _cur.fetchone()
        if _row:
            return _row[0] or 0, _row[1] or 0
    except Exception:
        # Batch B M9: 原 debug 级别太低——(0, 0) 会静默禁用 token 预算限制
        _log.warning("AUDIT #13: 读取 token 预算失败，token 预算限制将被禁用", exc_info=True)
    return 0, 0


async def _accumulate_round_tokens(
    self, adapter, task_id: str, used_tokens: int,
    agent_id: str = "", model_cfg: dict | None = None,
    duration_ms: int = 0, round_num: int = 0,
) -> int:
    """AUDIT #13: 累加本轮 token 用量并持久化到 tasks 表。

    Task 7.5: 计算 token 成本并持久化到 analytics_events（fire-and-forget）。
    Task 7.26: 携带 duration_ms / round_num 追踪字段——per-LLM-call 全链路追踪
    （轻量级方案，不依赖 OpenTelemetry SDK，扩展 analytics_events 基础设施）。
    6c-1 修复：从 execute_tool_loop 提取——单职责（累加 + 持久化 cost）。
    Returns: 更新后的 used_tokens。
    """
    _usage = getattr(adapter, "last_usage", {}) or {}
    _round_tokens = _usage.get("total_tokens", 0)
    if _round_tokens <= 0:
        return used_tokens
    used_tokens += _round_tokens
    try:
        await self.db.execute(
            "UPDATE tasks SET used_tokens=? WHERE id=?",
            (used_tokens, task_id),
        )
        await self.db.commit()
    except Exception:
        _log.debug("AUDIT #13: 持久化 used_tokens 失败", exc_info=True)
    # 2026-09-10：统计口径改为**只记 token**（含缓存命中/推理 token），
    # 不再计算价格——厂商定价漂移且 calc_cost 的口径与本仓库 YAML 从未对上（恒为 0）。
    await _track_token_usage(
        self, task_id, agent_id, model_cfg, _usage,
        duration_ms=duration_ms, round_num=round_num,
    )
    return used_tokens


async def _track_token_usage(
    self, task_id: str, agent_id: str, model_cfg: dict | None,
    usage: dict,
    duration_ms: int = 0, round_num: int = 0,
) -> None:
    """持久化 token 用量到 analytics_events 表（事件类型见 TOKEN_USAGE_EVENTS）。

    Task 7.26: 携带 duration_ms / round_num 追踪字段——per-LLM-call latency
    + 所在工具循环轮次，支持全链路追踪（对标 OpenTelemetry span attributes /
    LangSmith per-call latency / Helicone LLM observability）。
    优先用 AnalyticsService.track()（走批量缓冲，对标 v33 双通道设计）；
    不可用时 fallback 到直接 INSERT（确保 cost 不丢失）。
    fire-and-forget——失败不阻塞主流程。

    对标 OpenAI usage tracking / Anthropic billing——每轮 LLM 调用的真实成本
    记录到 analytics_events 供 Dashboard 聚合查询。
    """
    import json as _json
    event_data = {
        "task_id": task_id,
        "agent_id": agent_id,
        "provider": model_cfg.get("provider", "") if model_cfg else "",
        "model": model_cfg.get("model", "") if model_cfg else "",
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        # 细分口径：缓存命中（OpenAI prompt_tokens_details / DeepSeek 平铺）与推理 token。
        # 这两部分"看不见但真实消耗"，漏记会低估用量。
        "cached_tokens": usage.get("cached_tokens", 0),
        "reasoning_tokens": usage.get("reasoning_tokens", 0),
        "duration_ms": duration_ms,
        "round": round_num,
    }
    # 优先用 AnalyticsService（批量缓冲），否则直接 INSERT
    _analytics = getattr(self, "_analytics_service", None)
    if _analytics is None:
        try:
            from context import get_app
            _app = get_app()
            if _app and hasattr(_app.state, "analytics_service"):
                _analytics = _app.state.analytics_service
        except Exception:
            _log.debug("取不到 analytics_service，token 用量事件不落账本", exc_info=True)
    if _analytics and hasattr(_analytics, "track"):
        await _analytics.track(TOKEN_USAGE_EVENTS[0], event_data)
    else:
        try:
            await self.db.execute(
                "INSERT INTO analytics_events(event_type, event_data) VALUES(?,?)",
                (TOKEN_USAGE_EVENTS[0], _json.dumps(event_data, ensure_ascii=False)),
            )
            await self.db.commit()
        except Exception:
            _log.debug("直接 INSERT token 用量事件失败", exc_info=True)


async def _emit_lifecycle_events(
    self, task_id: str, tool_round: int, executable_calls: list[dict],
    tool_results: list[dict], round_text: str, round_reasoning: str,
    rounds_without_progress: int, loop_started_at: float, max_rounds: int,
) -> int:
    """Phase 86: task_lifecycle — stall_check + estimate 事件推送。

    6c-1 修复：从 execute_tool_loop 提取——纯 WS 推送，无 DB 副作用。
    Returns: 更新后的 rounds_without_progress。
    """
    # 1. 实质进度判定（AUDIT #7: 传入 round_reasoning 避免纯推理轮误判 stall）
    _progress = _evaluate_round_progress(tool_results, round_text, round_reasoning)
    if _progress:
        rounds_without_progress = 0
    else:
        rounds_without_progress += 1

    # 2. stall_check（连续 ≥ 3 轮无实质进度）
    if rounds_without_progress >= 3:
        _last_tool = (
            executable_calls[-1]["function"]["name"]
            if executable_calls else None
        )
        await self._safe_send(task_id, {
            "type": "task_lifecycle", "subtype": "stall_check",
            "task_id": task_id,
            "last_tool": _last_tool,
            "rounds_without_progress": rounds_without_progress,
            "suggestion": _stall_suggestion(_last_tool),
        })

    # 3. estimate（每 5 轮推送，第 5 轮起；tool_round 已递增）
    if tool_round >= 5 and tool_round % 5 == 0:
        _elapsed, _remaining, _confidence = _compute_estimate(
            loop_started_at, tool_round, max_rounds,
        )
        await self._safe_send(task_id, {
            "type": "task_lifecycle", "subtype": "estimate",
            "task_id": task_id,
            "elapsed_seconds": _elapsed,
            "estimated_remaining_seconds": _remaining,
            "confidence": _confidence,
        })
    return rounds_without_progress
