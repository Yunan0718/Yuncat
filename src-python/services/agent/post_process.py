"""services/agent/post_process.py —— 工具结果后处理与标准查询触发（从 agent_tool_loop 拆分）

G3: 工具执行后统一处理——WS预览、失败分析、降级跟踪、结果格式化。
Phase 85: 工具连续失败 ≥ 2 次时触发标准查询。
采用委托模式: 模块级函数接收 self 作为首参（对齐 agent_tool_loop.py 模式）。
"""
import logging

from config import (
    calc_tool_result_limit_with_declaration,
    L4_LONG_OUTPUT_THRESHOLD_CHARS,
)

from .progress import _get_tool_declared_max

_log = logging.getLogger("yuncat.agent")

# Phase 47 P5.3 / Phase 48 T2-7: 工具降级——连续失败3次则从本轮移除(对标 Claude Code graceful degradation)
# Phase 48 T2-7: 原 _tool_failure_streak / _DEGRADED_TOOLS 是模块级全局，并发 task 互相
# 污染降级状态（task A 的 clear() 会清空 task B 的计数）。改为 execute_tool_loop 的局部
# 变量，经参数传递给 _execute_exclusive_tools，实现 per-task 隔离。
_TOOL_DEGRADE_THRESHOLD = 3
# Phase 85: 连续失败 ≥ 2 次触发 standards_lookup（对齐 standards_lookup_agent._FAILURE_STREAK_THRESHOLD）
# 比降级阈值（3）小一档——先查文档给建议，仍失败再降级。
_STANDARDS_LOOKUP_TRIGGER_THRESHOLD = 2
# 用户显式读取的文件已由 read_file 自己按上下文上限分段并给出 next_offset，
# 通用摘要再截一次会丢掉用户真正要看的正文（对齐 DSH spill 跳过 read）。
_SPILL_EXEMPT_TOOLS = frozenset({"read_file"})


async def _trigger_standards_lookup(
    self, task_id: str, tool_name: str, result_str: str,
    streak: int, agent_id: str, model_cfg: dict,
) -> None:
    """Phase 85: 工具连续失败 ≥ 2 次时触发标准查询——best-effort，不阻塞主流程

    should_lookup 命中（连续失败 + error 关键词）则调 lookup，
    结果通过 _pending_failure_analysis 注入下轮 system message（追加到现有分析后）。
    失败/缺单例静默返回（不影响工具循环）。

    与 _analyze_tool_failure_and_send 的关系：
      - _analyze_tool_failure_and_send：每次失败都调，给一句话快速建议
      - _trigger_standards_lookup：连续失败 ≥2 次调，4 步降级文档查询（更深）
    """
    try:
        from services.long_task.standards_lookup_agent import get_standards_lookup_agent
        agent = get_standards_lookup_agent()
        if agent is None:
            return
        # 触发条件——should_lookup 内部判断 error 关键词 + 连续失败
        if not agent.should_lookup(
            error=result_str, tool_name=tool_name, failure_streak=streak,
        ):
            return
        problem = f"工具 '{tool_name}' 连续失败 {streak} 次。最近错误：{result_str[:500]}"
        result = await agent.lookup(problem, domain="general")
        if not result.get("ok"):
            return
        recs = result.get("recommendations", [])
        if not recs:
            return
        # 拼接建议注入下轮——与 _pending_failure_analysis 模式一致（追加而非覆盖）
        advice_text = "；".join(recs[:3])
        source_type = result.get("source_type", "")
        extra = f"\n[标准查询建议·{source_type}] {advice_text}"
        existing = getattr(self, "_pending_failure_analysis", None) or ""
        self._pending_failure_analysis = (existing + extra) if existing else extra
        # 推 WS 让前端实时看到查询结果
        await self._safe_send(task_id, {
            "type": "standards_lookup_result",
            "task_id": task_id,
            "tool_name": tool_name,
            "summary": result.get("summary", ""),
            "recommendations": recs,
            "sources": result.get("sources", []),
            "source_type": source_type,
        })
    except Exception:
        _log.debug("standards_lookup trigger failed", exc_info=True)


def _format_retained_result(summarized: str, original: str, fn_name: str, result_index: int) -> tuple[str, int]:
    """格式化模型可见摘要，并返回精确省略的 UTF-8 字节数。"""
    omitted_bytes = max(0, len(original.encode("utf-8")) - len(summarized.encode("utf-8")))
    content = (
        summarized
        + f"\n\n[结果已摘要: {len(original)} → {len(summarized)} 字符；"
        + f"省略 {omitted_bytes} bytes]"
        + f"\n[使用 request_more_context('{fn_name}', {result_index}) 获取完整结果]"
    )
    return content, omitted_bytes


async def _post_process_tool_result(
    self, tc: dict, fn_name: str, fn_args: dict, result_str: str,
    task_id: str, agent_id: str, model_cfg: dict, ts,
    failure_streak: dict[str, int], degraded_tools: set[str],
    total_calls: int, result_index: int,
) -> dict:
    """G3: 工具执行后统一处理——WS预览、失败分析、降级跟踪、结果格式化。

    并行和串行路径共用此函数，避免逻辑重复。
    Note: failure_streak 在并行场景下对同名工具有轻微竞争，
    不影响结果正确性（最坏情况降级阈值多一次失败触发）。
    """
    import json as _json
    # Task 3.1: 兜底脱敏——result_str 进入 preview/失败分析/history 前过滤敏感串
    # 路径 deny（is_denied_path）是主防线读前阻止，此处兜底覆盖 run_command 输出含 env 等
    # 对标 LangChain SecretStr 读后脱敏 + Claude Code 工具结果进上下文前过滤
    from security.redact import redact_secrets
    result_str = redact_secrets(result_str)
    try:
        result_dict = _json.loads(result_str)
        result_success = result_dict.get("success", False) if isinstance(result_dict, dict) else False
        # P0-3: 提取 meta 中的 checkpoint_warning 供前端展示红色 toast
        result_meta = result_dict.get("meta") if isinstance(result_dict, dict) else None
    except Exception:
        result_success = False
        result_meta = None
    _declared_max = _get_tool_declared_max(fn_name)
    _preview_limit = calc_tool_result_limit_with_declaration(model_cfg, 1, _declared_max)
    _ws_msg = {
        "type": "tool_result_preview", "task_id": task_id,
        "tool_name": fn_name, "success": result_success,
        "preview": result_str[:_preview_limit], "full_result": result_str[:_preview_limit],
    }
    # P0-3: 透传 meta（含 checkpoint_warning）让前端展示 toast
    if isinstance(result_meta, dict) and result_meta:
        _ws_msg["meta"] = result_meta
    await self._safe_send(task_id, _ws_msg)
    if not result_success:
        from .agent_service import _suggest_alternative_tool
        alt = _suggest_alternative_tool(fn_name)
        if alt:
            _log.info("Retrying with alternative tool: %s → %s", fn_name, alt)
            from .agent_service import _map_args_for_alternative
            alt_args = _map_args_for_alternative(fn_name, alt, dict(fn_args))
            try:
                alt_result = await ts.execute(alt, alt_args, agent_id=agent_id, task_id=task_id)
                result_str = _json.dumps(alt_result, ensure_ascii=False)
                # Task 3.1: 替代工具结果同样需兜底脱敏
                result_str = redact_secrets(result_str)
                # P0-4 修复: 替代工具重试后必须更新 result_success，
                # 否则 line 400 的 if result_success 永远 False（死代码），
                # 重试成功的工具仍被计数为失败，过早触发工具降级。
                if isinstance(alt_result, dict):
                    result_success = bool(alt_result.get("success", False))
            except Exception:
                _log.debug("备选工具重试结果解析失败", exc_info=True)
        await self._analyze_tool_failure_and_send(
            task_id, fn_name, dict(fn_args), result_str, agent_id, model_cfg,
        )
        if result_success:
            failure_streak.pop(fn_name, None)
        else:
            streak = failure_streak.get(fn_name, 0) + 1
            failure_streak[fn_name] = streak
            if streak >= _STANDARDS_LOOKUP_TRIGGER_THRESHOLD:
                await _trigger_standards_lookup(
                    self, task_id, fn_name, result_str, streak, agent_id, model_cfg,
                )
            if streak >= _TOOL_DEGRADE_THRESHOLD:
                degraded_tools.add(fn_name)
                _log.warning("Tool '%s' degraded after %d consecutive failures", fn_name, streak)
                await self._safe_send(task_id, {
                    "type": "tool_degraded", "task_id": task_id,
                    "tool_name": fn_name, "failures": streak,
                })
    _limit = calc_tool_result_limit_with_declaration(model_cfg, total_calls, _declared_max)
    if len(result_str) > _limit and fn_name not in _SPILL_EXEMPT_TOOLS:
        # CodeWhale 借鉴: 大输出路由到 side-store——截断前缓存完整内容，
        # Agent 可通过 request_more_context(tool_name, result_index) 按需读取。
        # 修复: _cache_tool_result 此前已定义但从未调用，导致 request_more_context
        # 始终返回"缓存未找到"——截断提示信息对 Agent 是误导。
        try:
            from tools.builtin.system_tool_context import _cache_tool_result
            _cache_tool_result(task_id, fn_name, result_index, result_str)
        except Exception:
            _log.debug("cache_tool_result failed (non-critical)", exc_info=True)
        from tools.result import summarize_for_llm as _summarize_for_llm
        _NOTICE_RESERVE = 260
        _summary_budget = max(200, _limit - _NOTICE_RESERVE)
        _summarized = _summarize_for_llm(fn_name, result_str, _summary_budget)
        _truncated_content, _omitted_bytes = _format_retained_result(
            _summarized, result_str, fn_name, result_index,
        )
        # P2-2.6 L4 SI 场景化预检查：截断 + 长输出时追加自审提示
        # 对标 Claude Code 长输出标记——引导 Agent 审视摘要信息完整性
        if len(result_str) > L4_LONG_OUTPUT_THRESHOLD_CHARS:
            _truncated_content += (
                f"\n[L4 SI 提示] 输出较长已摘要。请审视摘要是否包含完成任务所需关键信息；"
                f"若缺失可 request_more_context 取回完整内容，或调整参数/换工具重试。"
            )
        return {
            "role": "tool", "tool_call_id": tc["id"],
            "content": _truncated_content,
            "truncated": True, "total_chars": len(result_str),
            "omitted_bytes": _omitted_bytes,
            "retrieve_hint": f"request_more_context('{fn_name}', {result_index})",
        }
    else:
        # P2-2.6 L4 SI 场景化预检查：未截断但长输出时追加自审提示
        # 即使未触发截断（动态 _limit 可能更高），长输出也提示 Agent 审视信息密度
        if len(result_str) > L4_LONG_OUTPUT_THRESHOLD_CHARS:
            _l4_hint = (
                f"\n\n[L4 SI 提示] 输出较长（{len(result_str)}字符）。"
                f"请审视是否包含完成任务所需关键信息；若需更精确结果可调整参数重试。"
            )
            return {
                "role": "tool", "tool_call_id": tc["id"],
                "content": result_str + _l4_hint,
            }
        return {
            "role": "tool", "tool_call_id": tc["id"],
            "content": result_str,
        }
