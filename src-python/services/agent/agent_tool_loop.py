"""services/agent_tool_loop.py —— Agent 工具调用执行循环模块

从 agent_service.py 拆分: _execute_tool_loop。
采用委托模式: 模块级函数接收 self 作为首参。
"""
import asyncio
import logging
import time as _time
from uuid import uuid4

from config import (COMPACT_TRIGGER_MICRO, COMPACT_TRIGGER_STANDARD,
                    COMPACT_TRIGGER_EMERGENCY, COMPACT_KEEP_RECENT_EMERGENCY,
                    COMPACT_MICRO_COOLDOWN_SEC, COMPACT_COOLDOWN_SEC, CONFIRM_TIMEOUT,
                    calc_tool_result_limit, calc_tool_result_limit_with_declaration,
                    calc_max_tool_rounds,
                    protect_critical_info, adaptive_truncate,
                    L4_LONG_OUTPUT_THRESHOLD_CHARS,
                    calc_context_budget, DEFAULT_AGENT_ID,
                    OPENCLAW_AGENT_ID, HERMES_AGENT_ID,
                    FINISH_REASON_LENGTH_NOTICE, MAX_PARALLEL_TOOL_CALLS)

# ==== 从子模块 re-export（向后兼容：外部代码/测试仍可从 agent_tool_loop 导入）====
from .progress import (  # noqa: E402
    _stall_suggestion,
    _evaluate_round_progress,
    _compute_estimate,
    _get_tool_declared_max,
)
from .budget import (  # noqa: E402
    _init_loop_budgets,
    _load_token_budget,
    _accumulate_round_tokens,
    _track_token_usage,
    _emit_lifecycle_events,
)
from .post_process import (  # noqa: E402
    _TOOL_DEGRADE_THRESHOLD,
    _STANDARDS_LOOKUP_TRIGGER_THRESHOLD,
    _trigger_standards_lookup,
    _post_process_tool_result,
)
from security.policy import confirmable_tool_names  # noqa: E402
from services.capability_limits import current_limits, resolve_tool_round_limit; from services.agent.reasoning_trace import record_reasoning, joined_reasoning; from .tool_result_persist import persist_tool_results; from .pending_tool_calls import register_pending; from .steer_inbox import prepare_round  # noqa: E402
from .tool_dispatch import split_by_concurrency  # noqa: E402
from .confirmable import (  # noqa: E402
    _run_confirmable_batch,
    _collect_confirmable_items,
    _push_confirm_batch,
    _finalize_confirmable_results,
)

_log = logging.getLogger("yuncat.agent")

# YAML 工具清单缓存 — 避免每个 tool round 都读磁盘
_oc_tool_names_cache: tuple[float, set[str]] | None = None
_OC_TOOL_CACHE_TTL = 60

# G3: 并行工具调用——资源键提取 + 资源锁管理
# 同资源（如同一文件路径）的工具串行，避免写冲突。
_FILE_TOOLS = frozenset({"write_file", "edit_file", "delete_file", "read_file"})
_FS_TOOLS = frozenset({"mkdir", "delete_dir", "rename_file"})
_SHELL_TOOLS = frozenset({"run_command", "open_shell", "close_shell"})
# G3: 并行上限——防止大量 tool_calls 同时执行导致资源尖峰
_PARALLEL_TOOL_SEMAPHORE_LIMIT = MAX_PARALLEL_TOOL_CALLS  # 保留作为默认值参考；实际取值见 capability_limits

# Phase 108: 工具事件有序 seq——按 task_id 单调递增，保证回放时 tool/call 与
# tool/result 成对。计数器独立在 event_seq 模块（带上限与清理，避免无限增长）。
from .event_seq import next_seq as _tool_event_seq  # noqa: E402


def _get_resource_key(tool_name: str, args: dict) -> str:
    """G3: 提取工具资源键——同资源键串行，不同资源键并行。

    对标 Claude Code: 独立资源的写操作可并行（如 write_file 到不同路径），
    同资源的操作串行（如两次 write_file 到同一文件）。

    Args:
        tool_name: 工具名
        args: 工具参数 dict

    Returns:
        资源键字符串。相同键 → 串行；不同键 → 可并行。
    """
    if tool_name in _FILE_TOOLS:
        path = args.get("path", args.get("file", ""))
        return f"{tool_name}:{path}" if path else f"{tool_name}:*"
    if tool_name in _FS_TOOLS:
        path = args.get("path", "")
        return f"{tool_name}:{path}" if path else f"{tool_name}:*"
    if tool_name in _SHELL_TOOLS:
        return f"{tool_name}:{args.get('shell_id', 'default')}"
    if tool_name == "git":
        return "git:*"  # Git 操作始终串行（共享仓库状态）
    return f"{tool_name}:*"  # 未知工具按工具名串行（向后兼容）


class _ResourceLockManager:
    """G3: 资源键锁管理器——同资源键串行，不同资源键并行。

    对标 Claude Code: 使用 per-resource asyncio.Lock 实现细粒度并发控制。
    """

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, resource_key: str) -> asyncio.Lock:
        if resource_key not in self._locks:
            self._locks[resource_key] = asyncio.Lock()
        return self._locks[resource_key]

    def acquire(self, resource_key: str):
        """返回 async context manager，获取指定资源键的锁。"""
        return self._get_lock(resource_key)


async def _load_oc_tool_names() -> set[str]:
    """加载 OpenClaw 工具名集合（60s 缓存）"""
    global _oc_tool_names_cache
    now = _time.monotonic()
    if _oc_tool_names_cache and now - _oc_tool_names_cache[0] < _OC_TOOL_CACHE_TTL:
        return _oc_tool_names_cache[1]
    names: set[str] = set()
    try:
        import yaml, os
        _yml = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                            "config", "tools", "openclaw.yaml")
        if os.path.exists(_yml):
            with open(_yml, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            names = {t["name"] for t in (data.get("tools") or [])}
    except Exception:
        _log.debug("加载 openclaw.yaml 失败", exc_info=True)
    _oc_tool_names_cache = (now, names)
    return names


async def _index_oc_tool_result(
    self, fn_name: str, result, task_id: str, agent_id: str,
) -> None:
    """Phase D F1.3+F1.4: OC 工具结果入 MemoryTree + 反馈学习。

    Fire-and-forget：失败不阻塞主流程。
    """
    try:
        mt = getattr(self, "_memory_tree", None)
        if mt and hasattr(mt, "index_from_text"):
            import json as _json
            result_text = _json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result)
            await mt.index_from_text(
                f"OpenClaw tool '{fn_name}' result for task {task_id}: {result_text}",
                source=f"openclaw_tool:{fn_name}",
            )
        fb = getattr(self, "_feedback_learner", None)
        if fb and hasattr(fb, "record_feedback"):
            success = isinstance(result, dict) and (result.get("ok") or result.get("success"))
            await fb.record_feedback(
                "human_accepted" if success else "human_rejected",
                task_id=task_id,
                context=f"openclaw_tool:{fn_name}",
                agent_id=agent_id,
            )
    except Exception:
        _log.debug("OC tool result indexing/feedback failed", exc_info=True)


def _format_tool_result_for_model(rs: str, limit: int) -> str:
    """把 safe 工具的原始结果整理成模型可见文本：超限才压缩，绝不因关键词缺失而丢正文。"""
    content = rs if len(rs) <= limit else (protect_critical_info(rs, limit) or rs[:limit])
    if len(rs) <= L4_LONG_OUTPUT_THRESHOLD_CHARS:
        return content
    if len(rs) > limit:
        return content + (
            f"\n\n[结果已截断: {len(rs)} → {len(content)} 字符]"
            f"\n[L4 SI 提示] 输出较长已截断。请审视是否包含关键信息；"
            f"若缺失可调整参数/换工具重试。"
        )
    return content + (
        f"\n\n[L4 SI 提示] 输出较长（{len(rs)}字符）。"
        f"请审视是否包含完成任务所需关键信息；若需更精确结果可调整参数重试。"
    )


async def _execute_safe_tools(
    self, _safe_calls: list[dict], tool_call_accumulator: dict,
    _streaming_results: dict, ts, agent_id: str, task_id: str,
    model_cfg: dict, _openclaw_tool_names: set[str],
) -> list[dict]:  # 执行 safe 级别工具——复用流式结果 + 并发执行剩余
    import json as _json
    tool_results: list[dict] = []
    _retry_calls: list[dict] = []
    _streamed_results: list[tuple[dict, str]] = []
    for tc in _safe_calls:
        orig_idx = None
        for idx, acc in tool_call_accumulator.items():
            if acc["id"] == tc["id"]:
                orig_idx = idx
                break
        if orig_idx is not None and orig_idx in _streaming_results and _streaming_results[orig_idx]:
            _streamed_results.append((tc, _streaming_results[orig_idx]))
        else:
            _retry_calls.append(tc)
    _total_safe = len(_streamed_results) + len(_retry_calls)
    for tc, rs in _streamed_results:
        fn = tc["function"]["name"]
        _declared = _get_tool_declared_max(fn)
        _safe_limit = calc_tool_result_limit_with_declaration(model_cfg, _total_safe, _declared)
        _content = _format_tool_result_for_model(rs, _safe_limit)
        tool_results.append({
            "role": "tool", "tool_call_id": tc["id"],
            "content": _content,
        })
    if _retry_calls:
        async def _run_safe_tool(tc: dict):
            fn = tc["function"]["name"]
            try:
                args = _json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"].strip() else {}
            except Exception:
                args = {}
            try:
                if fn in _openclaw_tool_names:
                    oc = self.adapters.get(OPENCLAW_AGENT_ID)
                    if oc and hasattr(oc, "execute_tool"):
                        result = await oc.execute_tool(fn, args)
                        await _index_oc_tool_result(self, fn, result, task_id, agent_id)
                        return _json.dumps(result, ensure_ascii=False)
                    return _json.dumps({"success": False, "error": "OpenClaw不可用"}, ensure_ascii=False)
                return _json.dumps(await ts.execute(fn, args, agent_id=agent_id, task_id=task_id),
                                   ensure_ascii=False)
            except Exception as exc:
                return _json.dumps({"error": str(exc)}, ensure_ascii=False)
        results = await asyncio.gather(*[_run_safe_tool(tc) for tc in _retry_calls],
                                        return_exceptions=True)
        for tc, rs in zip(_retry_calls, results):
            fn = tc["function"]["name"]
            _declared = _get_tool_declared_max(fn)
            _safe_limit = calc_tool_result_limit_with_declaration(model_cfg, _total_safe, _declared)
            _rs_str = str(rs)
            _content = _format_tool_result_for_model(_rs_str, _safe_limit)
            tool_results.append({
                "role": "tool", "tool_call_id": tc["id"],
                "content": _content,
            })
    return tool_results


async def _execute_exclusive_tools(
    self, calls: list[dict], ts, agent_id: str, task_id: str,
    _confirmable_names: set[str], _openclaw_tool_names: set[str],
    model_cfg: dict, adapter, context: dict,
    failure_streak: dict[str, int] | None = None,
    degraded_tools: set[str] | None = None,
) -> list[dict]:
    """顺序/并行执行 lock + exclusive 工具——含确认流 + 格式化 + 截断 + 失败恢复

    Phase 48 T2-7: failure_streak / degraded_tools 改为参数传入（原模块级全局
    导致并发 task 互相污染降级状态）。None 时初始化为空（向后兼容）。

    G3: 非确认工具并行执行（资源键锁控制同资源串行），确认工具保持串行。
    对标 Claude Code: LLM 一次返回多个 tool_calls 时，独立资源并行执行。
    """
    import json as _json
    if failure_streak is None:
        failure_streak = {}
    if degraded_tools is None:
        degraded_tools = set()

    # 原始优先级保持不变: degraded > OC > confirmable > regular
    results: list[dict | None] = [None] * len(calls)
    parallel_calls: list[tuple[int, dict]] = []
    confirmable_calls: list[tuple[int, dict]] = []
    # Phase 108: 工具事件有序持久化——派发时即分配 seq 并写 tool_call 事件，
    # 保证回放时按模型序配对（无视并行派发重叠）。tool_result 在结果算出处补写。
    _seq_map: dict[int, int] = {}
    from services.event_log import EventLog
    for idx, tc in enumerate(calls):
        fn_name = tc["function"]["name"]
        _seq = _tool_event_seq(task_id)
        _seq_map[idx] = _seq
        try:
            # seq 作为顶层字段传入（rebuild_history 用 e["seq"] 配对 tool_call/tool_result），
            # 不能只塞进 payload——否则顶层 seq 恒为 0，多工具时 tool_call_id 全部碰撞。
            await EventLog.append(task_id, "tool_call", agent_id=agent_id, tool_name=fn_name,
                            seq=_seq,
                            payload={"call_id": tc.get("id"),
                                     "args_preview": str(tc.get("function", {}).get("arguments", ""))[:200]})
        except Exception:
            pass
        if fn_name in degraded_tools:
            results[idx] = {
                "role": "tool", "tool_call_id": tc["id"],
                "content": '{"success":false,"error":"工具已降级(连续失败3次),请使用替代方案","degraded":true}',
            }
        elif fn_name in _openclaw_tool_names:
            parallel_calls.append((idx, tc))
        elif fn_name in _confirmable_names:
            confirmable_calls.append((idx, tc))
        else:
            parallel_calls.append((idx, tc))

    # G3: 并行执行非确认工具（OC + regular）
    if parallel_calls:
        _lock_mgr = _ResourceLockManager()
        # 单轮工具并行上限按能力档（默认 8 / MAX 16）
        _semaphore = asyncio.Semaphore(current_limits().parallel_tool_calls)

        async def _run_parallel(idx: int, tc: dict) -> None:
            fn_name = tc["function"]["name"]
            try:
                fn_args = _json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"].strip() else {}
            except Exception:
                fn_args = {}
            _args_preview_limit = calc_context_budget(model_cfg, 0.002, min_chars=300, max_chars=1500)
            await self._safe_send(task_id, {
                "type": "tool_start", "task_id": task_id,
                "tool_name": fn_name, "arguments_preview": str(fn_args)[:_args_preview_limit],
                "agent_source": agent_id,
            })
            # G3: 资源键锁——同资源串行，不同资源并行；信号量限流
            _resource_key = _get_resource_key(fn_name, fn_args)
            try:
                async with _lock_mgr.acquire(_resource_key):
                    async with _semaphore:
                        try:
                            if fn_name in _openclaw_tool_names:
                                oc = self.adapters.get(OPENCLAW_AGENT_ID)
                                result = await oc.execute_tool(fn_name, fn_args) if oc and hasattr(oc, "execute_tool") \
                                    else {"success": False, "error": "OpenClaw adapter 不可用"}
                                await _index_oc_tool_result(self, fn_name, result, task_id, agent_id)
                            else:
                                from tools.registry import get_tool
                                if agent_id == DEFAULT_AGENT_ID and not get_tool(fn_name):
                                    ha = self.adapters.get(HERMES_AGENT_ID)
                                    if ha and ha._ha_available and ha._ha_process and ha._ha_process.returncode is None:
                                        result = await ha._proxy_tool(fn_name, fn_args)
                                    else:
                                        result = await ts.execute(fn_name, fn_args, agent_id=agent_id, task_id=task_id)
                                else:
                                    result = await ts.execute(fn_name, fn_args, agent_id=agent_id, task_id=task_id)
                            result_str = _json.dumps(result, ensure_ascii=False)
                            # Phase 108: 补写 tool_result 事件（与 tool_call 同 seq，保证回放配对有序）
                            _seq = _seq_map.get(idx)
                            if _seq is not None:
                                try:
                                    await EventLog.append(task_id, "tool_result", agent_id=agent_id,
                                                    tool_name=fn_name,
                                                    seq=_seq,
                                                    payload={
                                                             "preview": result_str[:300],
                                                             "success": bool(result.get("success")) if isinstance(result, dict) else not result_str.startswith('{"error"')})
                                except Exception:
                                    _log.debug("写 tool_result 事件失败", exc_info=True)
                        except Exception as exc:
                            result_str = _json.dumps({"error": str(exc)}, ensure_ascii=False)
                # G3: 后处理在锁外执行（WS预览/失败分析/格式化），减少锁持有时间
                results[idx] = await _post_process_tool_result(
                    self, tc, fn_name, fn_args, result_str, task_id, agent_id,
                    model_cfg, ts, failure_streak, degraded_tools, len(calls), idx,
                )
            except Exception as exc:
                _log.warning("G3 parallel tool '%s' failed: %s", fn_name, exc, exc_info=True)
                results[idx] = {
                    "role": "tool", "tool_call_id": tc["id"],
                    "content": _json.dumps({"success": False, "error": f"并行执行异常: {exc}"}, ensure_ascii=False),
                }

        await asyncio.gather(
            *[_run_parallel(idx, tc) for idx, tc in parallel_calls],
            return_exceptions=True,
        )

    # G3 / Batch 3.2: 批量执行确认工具（同一轮多个 confirmable 聚合为一次弹窗）
    if confirmable_calls:
        for idx, res in await _run_confirmable_batch(
            self, confirmable_calls, ts, agent_id, task_id,
            model_cfg, failure_streak, degraded_tools, len(calls),
        ):
            results[idx] = res

    # 按原始顺序返回（过滤 None —— 理论上不应有 None）
    return [r for r in results if r is not None]


async def _check_compaction(
    self, adapter, context: dict, session_id: str, task_id: str,
    model_cfg: dict,
) -> None:
    """Token驱动三级压缩——emergency trim → micro compact → standard compact

    G1: 主动压缩——adapter.last_usage.prompt_tokens 是上一次 LLM 调用的
    prompt 大小，工具结果追加后实际上下文已膨胀，存在"滞后一步"问题。
    用 estimate_history_tokens(history) 估算当前 token，取 max(stale, est)
    确保不会因滞后漏触发（保守策略：高估优先触发压缩）。
    """
    from config import resolve_context_window
    from services.agent.agent_compact import estimate_history_tokens
    _usage = getattr(adapter, "last_usage", {}) or {}
    _stale_tokens = _usage.get("prompt_tokens", 0)
    _estimated = estimate_history_tokens(
        context["history"], model_id=model_cfg.get("model", "gpt-4"),
    )
    # max(滞后, 当前估算) — 取较大值防止漏触发（保守）
    _current_tokens = max(_stale_tokens, _estimated)
    _window = resolve_context_window(model_cfg)
    _t60 = int(_window * COMPACT_TRIGGER_MICRO)
    _t90 = int(_window * COMPACT_TRIGGER_STANDARD)
    _t98 = int(_window * COMPACT_TRIGGER_EMERGENCY)

    if _current_tokens >= _t98:
        _log.warning("EMERGENCY trim: tokens=%d/%d", _current_tokens, _window)
        await self._safe_send(task_id, {
            "type": "context_emergency", "task_id": task_id,
            "usage_pct": round(_current_tokens / _window * 100, 1),
        })
        context["history"] = self.compactor.emergency_trim_history(
            context["history"], model_config=model_cfg,
        )
        self._needs_full_compact.add(session_id)
    elif _current_tokens >= _t60 and _current_tokens < _t90:
        _last_at = self._micro_compact_last_applied.get(session_id, 0)
        if _time.time() - _last_at > COMPACT_MICRO_COOLDOWN_SEC:
            shrunk = self.compactor.micro_compact_history(context["history"], model_cfg=model_cfg)
            if shrunk > 0:
                self._micro_compact_last_applied[session_id] = _time.time()
                await self._safe_send(task_id, {
                    "type": "context_micro_compact", "task_id": task_id,
                    "bytes_saved": shrunk,
                    "usage_pct": round(_current_tokens / _window * 100, 1),
                })
    if _current_tokens >= _t90 and self._memory_service:
        await self._safe_send(task_id, {
            "type": "pre_compact_warning", "task_id": task_id,
            "usage_pct": round(_current_tokens / _window * 100, 1),
        })
        try:
            # M4: 估算 system prompt token 数，让压缩时 recent budget 扣除 system 占用
            _sys_tokens = 0
            _sys_text = (context.get("system") or "") + (context.get("memory") or "")
            if _sys_text:
                try:
                    from config import estimate_tokens
                    _sys_tokens = estimate_tokens(_sys_text, model_cfg.get("model", "gpt-4"))
                except Exception:
                    _log.debug("M4 system token estimate failed", exc_info=True)
            await self._memory_service.maybe_compact_context(
                session_id, current_tokens=_current_tokens, max_tokens=_window,
                system_prompt_tokens=_sys_tokens,
            )
            await self.compactor.reattach_context_after_compact(session_id, context, model_cfg)
        except Exception:
            _log.warning("Compact reattachment failed", exc_info=True)


async def execute_tool_loop(
    self, adapter, context: dict, task_id: str, agent_id: str,
    session_id: str, model_cfg: dict, prompt: str,
) -> str:
    """Tool call 执行循环——stream 处理 + 工具执行 + 上下文压缩。

    context["history"] 会被原地修改（工具结果追加）。
    Returns: full_response 文本。
    """
    # Phase 108+: 把任务上下文中的能力策略开关集合挂到调用链 contextvar，
    # 使 ToolService.execute 在分发每个工具前可读到并做 fail-closed 硬门控。
    # 仅当 chat 任务携带 capability_flags 时生效；系统/测试调用不设置 → 不干预。
    from context import set_current_capability_flags, set_current_capability_level
    set_current_capability_flags((context or {}).get("capability_flags"))
    set_current_capability_level((context or {}).get("capability_level"))
    # Phase 43 P0-DYN: 轮次上限从上下文窗口派生；默认 100（对标 Claude Code），
    # 用户可用 context["max_rounds"] 覆盖，大窗口封顶。
    _user_max_rounds = (context or {}).get("max_rounds")
    _max_tool_rounds = resolve_tool_round_limit(model_cfg, _user_max_rounds)
    tool_round = 0
    # Phase 86: task_lifecycle — stall_check 计数器 + estimate 计时基线
    _loop_started_at = _time.time()
    _rounds_without_progress = 0 ; full_response = ""   # 2026-10-07 合并以保持尺寸基线
    index = 0
    # Batch 5a C1: 最后一轮 LLM 返回的 finish_reason（length=截断 / stop=正常）
    _finish_reason = ""
    # Phase 48 T2-7: per-task 降级状态（原模块级全局并发污染，改为局部变量）
    _failure_streak: dict[str, int] = {}
    _degraded_tools: set[str] = set()
    # 6c-1 修复：预算初始化提取到 _init_loop_budgets
    _budgets = _init_loop_budgets(model_cfg)
    _assistant_msg_budget = _budgets["assistant_msg"]
    _reasoning_msg_budget = _budgets["reasoning_msg"]
    _round_text_log_budget = _budgets["round_text_log"]
    _snapshot_generated_budget = _budgets["snapshot_generated"]
    _snapshot_thinking_budget = _budgets["snapshot_thinking"]
    _stream_error_preview_budget = _budgets["stream_error_preview"]

    # AUDIT #13: Token 预算——6c-1 修复：提取到 _load_token_budget
    _budget_limit, _used_tokens = await _load_token_budget(self, task_id)

    while tool_round < _max_tool_rounds:
        # ==== AUDIT #13: Token 预算检查（每轮 stream 前检查）====
        if _budget_limit > 0 and _used_tokens >= _budget_limit:
            _log.info("AUDIT #13: task %s 预算耗尽 %d/%d, 优雅暂停",
                      task_id[:8], _used_tokens, _budget_limit)
            await self._safe_send(task_id, {
                "type": "budget_exceeded", "task_id": task_id,
                "used_tokens": _used_tokens, "max_budget_tokens": _budget_limit,
            })
            # D3: 乐观锁——仅 running→paused_budget 生效，避免覆盖竞态中已被取消的任务
            await self._mark_task(task_id, "paused_budget", expected_from="running")
            full_response += f"\n\n[Token 预算耗尽: 已用 {_used_tokens} / 上限 {_budget_limit}，任务已暂停]"
            break
        _round_prompt = await prepare_round(self, task_id, session_id, agent_id, context, prompt)

        # ==== 1. 处理一轮 stream ====
        # Phase 38: 上轮失败分析注入 context — Agent 下一轮自然看到建议
        if getattr(self, '_pending_failure_analysis', None):
            context["history"].append({
                "role": "system",
                "content": f"[自动诊断] {self._pending_failure_analysis}",
            })
            self._pending_failure_analysis = None

        tool_call_accumulator: dict[int, dict] = {}
        _streaming_tasks: dict[int, asyncio.Task] = {}
        _streaming_results: dict[int, str] = {}
        _safe_tool_indices: set[int] = set()
        round_text = ""
        round_reasoning = ""  # Phase DS.2: DeepSeek reasoning_content 积累
        _round_start = _time.monotonic()   # 2026-10-07 与 _t0 同源（原用墙钟，相减得 epoch 毫秒）

        async for chunk in self._stream_with_fallback(
            agent_id, _round_prompt, context,
            primary_provider=model_cfg.get("provider", "deepseek"), task_id=task_id,
        ):
            if isinstance(chunk, str):
                chunk = {"type": "text", "data": chunk}
            chunk_type = chunk.get("type", "text")
            chunk_data = chunk.get("data", "")

            if chunk_type == "thinking":
                round_reasoning += chunk_data
                await self._safe_send(task_id, {
                    "type": "thinking_chunk", "task_id": task_id,
                    "data": chunk_data, "index": index,
                })
            elif chunk_type == "tool_call":
                for tc in (chunk_data if isinstance(chunk_data, list) else [chunk_data]):
                    tc_idx = tc.get("index", 0) if isinstance(tc, dict) else 0
                    if tc_idx not in tool_call_accumulator:
                        # P0-2 (2026-07-04): 部分 Provider (vLLM/Ollama) 流式工具调用
                        # 不发 id 字段，原代码用 `if tc["id"]` 过滤导致静默丢弃。
                        # 改为本地生成 fallback id 保证稳定，后续 tool_call_id 回传一致。
                        # 对标 OpenAI SDK 行为：缺失 id 时客户端生成稳定 id。
                        tool_call_accumulator[tc_idx] = {
                            "id": f"call_{uuid4().hex[:8]}",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    acc = tool_call_accumulator[tc_idx]
                    if isinstance(tc, dict):
                        if tc.get("id"):
                            acc["id"] = tc["id"]  # LLM 提供 id 时优先覆盖 fallback
                        fn = tc.get("function", {})
                        if isinstance(fn, dict):
                            if fn.get("name"):
                                acc["function"]["name"] = fn["name"]
                            if fn.get("arguments"):
                                acc["function"]["arguments"] += fn["arguments"]
                    # Phase 45 D.5: 工具调用完整→safe工具立即执行
                    # P0-2: 不再检查 acc["id"]——fallback id 保证非空
                    if acc["function"]["name"] and acc["function"]["arguments"].strip():
                        from tools.registry import get_concurrency
                        _level = get_concurrency(acc["function"]["name"])
                        if _level == "safe" and tc_idx not in _streaming_tasks:
                            _safe_tool_indices.add(tc_idx)
                            _streaming_tasks[tc_idx] = asyncio.create_task(
                                self._streaming_execute_tool(acc, agent_id, task_id, model_cfg)
                            )
                # 发送累积后的完整数据（而非 raw delta），防止 DeepSeek V4 逐字拆分
                accumulated = [
                    {"index": idx, "id": acc["id"], "type": acc["type"],
                     "round": tool_round,
                     "function": {"name": acc["function"]["name"],
                                  "arguments": acc["function"]["arguments"]}}
                    for idx, acc in tool_call_accumulator.items()
                ]
                await self._safe_send(task_id, {
                    "type": "tool_call_chunk", "task_id": task_id,
                    "data": accumulated, "index": index,
                })
            elif chunk_type == "stream_error":
                # Phase 47: API 流错误不再伪装成文本——标记任务失败
                # P1-5 修复: 原代码只 log + append history，无 break/return/_mark_task，
                # 导致任务误标 completed 或基于错误状态继续执行。
                # 对标 OpenAI/Anthropic SDK 流式错误处理：立即终止并标记失败。
                _log.error("Stream error in tool_loop round %d: %s", tool_round, chunk_data)
                _err_preview = chunk_data[:_stream_error_preview_budget]
                # DSH 借鉴: 按稳定错误码给模型/用户明确的处置建议（route on code）
                _err_hint = ""
                _error_code = chunk.get("error_code", "")
                if _error_code == "CONTEXT_WINDOW_EXCEEDED":
                    _err_hint = "上下文已超限——请压缩对话历史或分段处理，不要重复相同长内容。"
                elif _error_code == "QUOTA_EXCEEDED":
                    _err_hint = "配额/余额已耗尽——请提示用户检查账户余额或更换 API Key。"
                elif _error_code == "AUTH":
                    _err_hint = "API Key 无效或无权限——请提示用户在 设置 > 模型配置 检查密钥。"
                elif _error_code == "RATE_LIMIT":
                    _retry_s = int((chunk.get("retry_after_ms") or 0) / 1000)
                    _err_hint = ("触发限流——" + (f"建议 {_retry_s}s 后重试；" if _retry_s else "建议稍后重试；")
                                 + "不要连续重发相同请求。")
                elif _error_code == "SERVER":
                    _err_hint = "服务端错误（5xx）——属服务方瞬时故障，建议稍后重试。"
                elif _error_code == "INVALID_REQUEST":
                    _err_hint = "请求被服务端拒绝——请检查模型名/参数或附件大小是否超限。"
                # 排障线索：request-id 是唯一能对上服务端日志的信息，务必带给用户
                _rid = chunk.get("request_id")
                if _rid:
                    _err_hint += f"（排障请求 id：{_rid}）"
                _hint = f"\n{_err_hint}" if _err_hint else ""
                context["history"].append({
                    "role": "system",
                    "content": f"[流错误] API 调用失败: {_err_preview}{_hint}\n请告知用户当前任务遇到 API 错误。",
                })
                full_response += f"\n\n[流错误] {_err_preview}{_hint}"
                # 通知前端流错误
                await self._safe_send(task_id, {
                    "type": "stream_error", "task_id": task_id, "error": _err_preview,
                    "error_code": _error_code or None,
                    "request_id": chunk.get("request_id"),
                    "retry_after_ms": chunk.get("retry_after_ms"),
                })
                # 标记任务失败（乐观锁：仅 running→failed 生效，避免覆盖竞态中已取消的任务）
                await self._mark_task(
                    task_id, "failed", f"stream error: {_err_preview}",
                    expected_from="running",
                )
                # P1-2: 取消所有未完成的流式 safe 工具任务——防止后台泄漏
                # 这些 task 可能已开始执行文件写入/命令运行，取消后 asyncio.CancelledError
                # 会在 _streaming_execute_tool 内部触发清理（如有）
                for _st_idx, _st_task in _streaming_tasks.items():
                    if not _st_task.done():
                        _st_task.cancel()
                        _log.debug("Cancelled streaming task [%d] due to stream error", _st_idx)
                # 直接 return，不再继续工具循环（break 只跳出 async for，仍会走工具执行逻辑）
                return full_response
            elif chunk_type == "finish":
                # Batch 5a C1: SSE finish_reason 透传——length 时输出截断提示。
                # 必须显式拦截：否则落入下方 else 被当作文本推送/累积。
                if chunk_data:
                    _finish_reason = str(chunk_data)
                continue  # 控制信号不推送前端、不占 chunk index
            else:
                await self.push_stream_chunk(task_id, chunk_data, index)
                full_response += chunk_data
                round_text += chunk_data
            index += 1

        tool_round += 1

        # ==== AUDIT #13: 累加本轮 token 用量 + 持久化 ====
        # adapter.last_usage 含最后一次 LLM 调用的 prompt/completion/total_tokens
        # 累加 total_tokens 得到跨轮次总消耗（每次 LLM 调用的真实成本）
        # 放在 break 之前——即使本轮无工具调用退出，LLM 调用的 token 也要计入
        # 6c-1 修复：提取到 _accumulate_round_tokens
        # Task 7.5: 同时计算 token 成本并持久化到 analytics_events
        # Task 7.26: 携带本轮 LLM 调用 latency + round 序号（全链路追踪）
        _round_duration_ms = int((_time.monotonic() - _round_start) * 1000)
        _used_tokens = await _accumulate_round_tokens(
            self, adapter, task_id, _used_tokens, agent_id, model_cfg,
            duration_ms=_round_duration_ms, round_num=tool_round,
        )

        # ==== 2. 收集流式结果 + 分组并发调度 ====
        # Phase 45 D.5: 收集流式 safe 工具结果
        if _safe_tool_indices:
            for tc_idx in list(_safe_tool_indices):
                task = None
                try:
                    task = _streaming_tasks[tc_idx]
                    if not task.done():
                        task.cancel()
                except Exception:
                    _log.debug("取消流式任务失败", exc_info=True)
                if task is not None and task.done() and not task.cancelled():
                    result = task.result()
                    if result:
                        _streaming_results[tc_idx] = result

        # 收集所有工具调用（safe 工具已执行，结果在 _streaming_results 中）
        # P0-2: 不再检查 tc["id"]——fallback id 保证非空，仅以 function name 为准
        executable_calls = [
            tc for tc in tool_call_accumulator.values()
            if tc["function"]["name"]
        ]
        if not executable_calls: break
        register_pending(task_id, session_id, agent_id, executable_calls)

        # Phase 48-F: 删除单轮工具调用上限 50（人为限制）——对标 Claude Code 无此限制。
        # 防护：calc_tool_result_limit(model_cfg, tool_count) 已按 tool_count 分摊（多工具时每个上限递减）。

        if tool_round >= _max_tool_rounds:
            await self.push_stream_chunk(task_id,
                "\n\n[已达到最大工具轮次 %d]" % _max_tool_rounds, index)
            break

        await self._safe_send(task_id, {
            "type": "tool_round", "task_id": task_id,
            "round": tool_round + 1, "max": _max_tool_rounds,
        })

        ts = self._tool_service
        if ts is None:
            from services.tools.tool_service import ToolService
            ts = ToolService()
        _safe_calls, _lock_calls, _excl_calls = split_by_concurrency(executable_calls)
        try:
            _confirmable_names = confirmable_tool_names()  # 权限档位参与判定
        except Exception:  # pragma: no cover - 注册表不可用时退回静态清单
            _log.warning("confirmable_tool_names failed, fallback", exc_info=True)
            _confirmable_names = {"write_file", "run_command", "delete_file", "rename_file", "delete_dir"}
        _openclaw_tool_names = await _load_oc_tool_names()

        # safe 工具并发执行（Phase 45 D.7）
        tool_results: list[dict] = await _execute_safe_tools(
            self, _safe_calls, tool_call_accumulator, _streaming_results,
            ts, agent_id, task_id, model_cfg, _openclaw_tool_names,
        )
        # lock + exclusive 工具顺序执行（Phase 47 拆分）
        # Phase 48 T2-7: 传 per-task 降级状态（避免并发污染）
        tool_results += await _execute_exclusive_tools(
            self, _lock_calls + _excl_calls, ts, agent_id, task_id,
            _confirmable_names, _openclaw_tool_names, model_cfg, adapter, context,
            failure_streak=_failure_streak, degraded_tools=_degraded_tools,
        )

        # ==== 3. 提交到 history ====
        # Claude Code 风格：assistant 消息同时含 content 和 tool_calls
        assistant_msg: dict = {"role": "assistant", "content": round_text[:_assistant_msg_budget] if round_text else ""}
        # Phase DS.2: DeepSeek V4 多轮契约——reasoning_content 必须保存+回传
        if round_reasoning:
            assistant_msg["reasoning_content"] = round_reasoning[:_reasoning_msg_budget]
            record_reasoning(self, round_reasoning, _reasoning_msg_budget)
        if executable_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": tc["type"],
                 "function": {"name": tc["function"]["name"],
                              "arguments": tc["function"]["arguments"]}}
                for tc in executable_calls
            ]
        context["history"].append(assistant_msg)
        for tr in tool_results: context["history"].append(tr)
        await persist_tool_results(self._memory_service, session_id, agent_id, tool_results)

        # Phase 47: 持久化中间轮 assistant 消息（含 tool_calls 结构）
        # 重启后 session 恢复时能正确重建工具调用序列
        if self._memory_service and executable_calls:
            try:
                await self._memory_service.add_message(
                    session_id, "assistant",
                    round_text[:calc_context_budget(model_cfg, 0.002, min_chars=300, max_chars=1200)] if round_text else "(tool calls)",
                    agent_id=agent_id,
                    tool_calls=assistant_msg.get("tool_calls"),
                    reasoning=round_reasoning[:_reasoning_msg_budget] if round_reasoning else None,
                )
            except Exception:
                _log.debug("Failed to persist tool calls for round", exc_info=True)

        prompt = ""
        # ── Token 驱动上下文压缩（三级策略）──
        await _check_compaction(self, adapter, context, session_id, task_id, model_cfg)

        # Phase 49-D: 每轮结束持久化运行时快照
        await self._write_task_snapshot(task_id, {
            "status": "running", "agent_id": agent_id, "session_id": session_id,
            "round": tool_round,
            "generated_response": full_response[:calc_context_budget(model_cfg, 0.002, min_chars=200, max_chars=800)],
            "thinking": round_reasoning[:calc_context_budget(model_cfg, 0.0015, min_chars=200, max_chars=600)],
            "tool_calls": [
                {"id": tc.get("id", ""),
                 "name": tc.get("function", {}).get("name", "")}
                for tc in executable_calls
            ],
        })

        # ==== Phase 86: task_lifecycle — stall_check + estimate ====
        # 6c-1 修复：提取到 _emit_lifecycle_events——纯 WS 推送，无 DB 副作用
        _rounds_without_progress = await _emit_lifecycle_events(
            self, task_id, tool_round, executable_calls, tool_results,
            round_text, round_reasoning, _rounds_without_progress,
            _loop_started_at, _max_tool_rounds,
        )

    # Batch 5a C1: finish_reason=length——回答被截断时追加提示（任务仍标 completed，其它值忽略）
    if _finish_reason == "length":
        _notice = f"\n\n{FINISH_REASON_LENGTH_NOTICE}"
        full_response += _notice
        await self.push_stream_chunk(task_id, _notice, index)
    self._last_reasoning = joined_reasoning(self)
    return full_response
