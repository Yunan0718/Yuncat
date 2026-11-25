"""services/agent/confirmable.py —— Confirmable 批量执行（从 agent_tool_loop 拆分）

Batch 3.2: 聚合同轮 confirmable 工具，统一推送、并发等待、按 idx 返回结果。
采用委托模式: 模块级函数接收 self 作为首参（对齐 agent_tool_loop.py 模式）。
"""
import asyncio
import logging

from config import calc_context_budget, CONFIRM_TIMEOUT

from .post_process import _post_process_tool_result

_log = logging.getLogger("yuncat.agent")


async def _run_confirmable_batch(
    self, confirmable_calls: list[tuple[int, dict]], ts,
    agent_id: str, task_id: str, model_cfg: dict,
    failure_streak: dict[str, int], degraded_tools: set[str],
    total_calls: int,
) -> list[tuple[int, dict]]:
    """Batch 3.2: 聚合同轮 confirmable 工具，统一推送、并发等待、按 idx 返回结果。"""
    collected = await _collect_confirmable_items(
        self, confirmable_calls, ts, agent_id, task_id, model_cfg,
    )
    await _push_confirm_batch(self, task_id, collected)
    return await _finalize_confirmable_results(
        self, collected, task_id, agent_id, model_cfg, ts,
        failure_streak, degraded_tools, total_calls,
    )


async def _collect_confirmable_items(
    self, confirmable_calls: list[tuple[int, dict]], ts,
    agent_id: str, task_id: str, model_cfg: dict,
) -> list[dict]:
    """执行每个 confirmable 工具的预览阶段，区分 halt / pending / 直接完成。"""
    """准备每个 confirmable 工具：审批方式决定问谁（档位是否允许已在 ToolService 拦过）"""
    from security.approval import resolve_approval_mode

    mode = await resolve_approval_mode(self.db)
    collected: list[dict] = []
    for idx, tc in confirmable_calls:
        collected.append(
            await _prepare_confirmable_item(
                self, idx, tc, ts, agent_id, task_id, model_cfg, mode,
            )
        )
    return collected


def _parse_tool_args(tc: dict) -> dict:
    """解析模型给的工具参数；空串或坏 JSON 都当空参数（后面的校验会报缺参）"""
    import json as _json

    raw = (tc.get("function") or {}).get("arguments") or ""
    try:
        return _json.loads(raw) if str(raw).strip() else {}
    except Exception:
        # 回退：坏 JSON 当空参数，后续的参数校验会明确报缺参
        return {}


def _build_confirm_item(
    idx: int, tc: dict, fn_name: str, fn_args: dict, result: dict,
    confirm_future, confirm_expires_at: float, review: dict | None,
) -> dict:
    """组装一条待处理项（纯函数，方便单测）"""
    return {
        "idx": idx, "tc": tc, "fn_name": fn_name, "fn_args": fn_args,
        "result": result, "confirm_future": confirm_future,
        "confirm_token": result.get("confirm_token", "") if isinstance(result, dict) else "",
        "confirm_expires_at": confirm_expires_at,
        "review": review, "halted": False, "pending": False,
    }


def _apply_review_outcome(item: dict, outcome: str, review: dict | None) -> None:
    """按终审结论标注这一项：拒绝 / 放行 / 交给用户确认（纯函数）"""
    if outcome == "deny":
        reason = (review or {}).get("reason", "")
        item["result"] = {
            "success": False,
            "error": f"审批未通过：{reason}。不要重试同一调用，改为向用户说明原因。",
            "review": review,
        }
        item["halted"] = True
        return
    if outcome == "approve":
        item["ai_approved"] = True
        return
    if item["confirm_future"] and not item["result"].get("_auto_approved"):
        item["pending"] = True
        preview = item["result"].get("data", {}) if isinstance(item["result"], dict) else {}
        if review:
            preview["_review"] = review
        item["preview"] = preview


async def _decide_confirm_approval(
    self, task_id: str, fn_name: str, fn_args: dict, mode: str,
) -> tuple[dict | None, str, bool]:
    """决定这次 confirmable 调用由谁签字，返回 (终审结论, outcome, 是否直接执行)。

    outcome 取 approve / deny / uncertain，uncertain 会走确认弹窗问用户。

    2026-09-17：长任务 / 子 Agent 的 task 级预授权过去等于免审直通——计划审过之后，
    步骤里每个危险工具都直接执行，AI 终审没有机会参与，几种权限档在长任务里退化成
    同一件事（文档口径是 MAX 不等于免审批，代码里其实连问谁都没问）。
    现在 task 级预授权按 AI 代审处理：终审说 proceed 才执行、halt 就拦下、拿不准就
    回到问你；只有用户显式选完全放开（off）时才真的不问。
    """
    from .tool_dispatch import review_dangerous_tool
    from security.approval import APPROVAL_AI, APPROVAL_OFF, review_outcome

    # agent_service._should_auto_approve 把 task 级标志和全局设置合成一个布尔；
    # 这里只要 task 级那半边——它是长任务/子 Agent 的预授权，必须经 AI 代审。
    task_auto = mode != APPROVAL_OFF and bool(
        task_id and self._task_auto_approve.get(task_id),
    )
    # 终审结论只取一次（FinalReviewAgent 自带冷却窗口），几种走向共用同一个结论。
    # 结论要当决策用时必须 force 逐次审：不能把上一个工具的结论套到下一个工具上。
    ai_decides = mode == APPROVAL_AI or task_auto
    # 2026-10-08：AI 代审时审查结论**就是决策**——审查员失联/输出解析不了必须交回用户，
    # 不能默认放行（实测旧行为对 `rm -rf /` 也因解析失败而 pass）。
    # 非代审档位保持原语义：审查只是加一道保险，没结论不阻塞执行。
    review = await review_dangerous_tool(
        task_id, fn_name, fn_args, force=ai_decides,
        approve_on_uncertain=not ai_decides,
    )
    outcome = review_outcome(review)
    if not ai_decides and outcome == "approve":
        # 非代审档位下终审只有否决权：可以拦，不能代替用户点头
        return review, "uncertain", await self._should_auto_approve(task_id)
    if outcome == "deny":
        return review, outcome, False
    if outcome == "approve":
        return review, outcome, True
    if task_auto:
        # 长任务不再免审直通：终审拿不准就交回问你，而不是默认放行
        return review, outcome, False
    return review, outcome, await self._should_auto_approve(task_id)


async def _prepare_confirmable_item(
    self, idx: int, tc: dict, ts, agent_id: str, task_id: str,
    model_cfg: dict, mode: str,
) -> dict:
    """准备一条 confirmable 调用：AI 审批 → 拒绝 / 待确认 / 直接执行"""
    fn_name = tc["function"]["name"]
    fn_args = _parse_tool_args(tc)
    _args_preview_limit = calc_context_budget(model_cfg, 0.002, min_chars=300, max_chars=1500)
    await self._safe_send(task_id, {
        "type": "tool_start", "task_id": task_id,
        "tool_name": fn_name, "arguments_preview": str(fn_args)[:_args_preview_limit],
        "agent_source": agent_id,
    })
    review, outcome, auto_ok = await _decide_confirm_approval(
        self, task_id, fn_name, fn_args, mode,
    )
    if outcome == "deny":
        # 已否决：连待确认状态都不创建，避免留下悬空 token
        result, confirm_future, confirm_expires_at = {}, None, 0.0
    else:
        result = await ts.execute_confirmable(
            self.db, fn_name, fn_args, agent_id=agent_id,
            auto_approve=auto_ok, task_id=task_id,
        )
        confirm_future = result.get("_confirm_future") if isinstance(result, dict) else None
        # Batch 4b: 取出确认 token 过期时间（随 WS 推送倒计时），并从 result 移除避免泄漏进 JSON
        confirm_expires_at = result.pop("_confirm_expires_at", 0.0) if isinstance(result, dict) else 0.0
    if outcome in ("approve", "deny"):
        await self._safe_send(task_id, {
            "type": "approval_review", "task_id": task_id, "tool_name": fn_name,
            "decision": outcome, "mode": mode,
            "reason": (review or {}).get("reason", ""),
        })
    item = _build_confirm_item(
        idx, tc, fn_name, fn_args, result, confirm_future, confirm_expires_at, review,
    )
    _apply_review_outcome(item, outcome, review)
    return item



async def _push_confirm_batch(self, task_id: str, collected: list[dict]) -> None:
    """Batch 3.2: 将待确认项目批量推送；只有 1 个时保持原单弹窗路径。"""
    pending = [it for it in collected if it["pending"]]
    if not pending:
        return
    if len(pending) == 1:
        it = pending[0]
        await self.push_tool_confirm(
            task_id, it["fn_name"], it["preview"], it["confirm_token"],
            expires_at=it.get("confirm_expires_at", 0.0),
        )
        return
    items = [
        {
            "tool_name": it["fn_name"], "preview": it["preview"],
            "confirm_token": it["confirm_token"],
            "expires_at": it.get("confirm_expires_at", 0.0),
        }
        for it in pending
    ]
    await self.push_tool_confirm_batch(task_id, items)


async def _finalize_confirmable_results(
    self, collected: list[dict], task_id: str, agent_id: str,
    model_cfg: dict, ts, failure_streak: dict[str, int],
    degraded_tools: set[str], total_calls: int,
) -> list[tuple[int, dict]]:
    """并发等待批量确认 Future，处理超时/异常，按原始 idx 返回 post-process 结果。"""
    import json as _json
    from services.tools.tool_service import cancel_pending_state
    pending = [it for it in collected if it["pending"]]
    if pending:
        futures = [asyncio.wait_for(it["confirm_future"], timeout=CONFIRM_TIMEOUT) for it in pending]
        gathered = await asyncio.gather(*futures, return_exceptions=True)
        for it, executed in zip(pending, gathered):
            if isinstance(executed, asyncio.TimeoutError):
                # Batch 3.7: 超时返回结构化信息，让 Agent 知道原因和下一步建议
                it["result"] = {
                    "success": False,
                    "error": "用户确认超时（30分钟）",
                    "action": "timeout",
                    "reason": "timeout",
                    "recommendation": "请重新运行工具以获取最新确认",
                    "meta": {"tool_name": it.get("fn_name", "")},
                }
                await cancel_pending_state(it["confirm_token"], self.db)
            elif isinstance(executed, BaseException):
                it["result"] = {"success": False, "error": str(executed), "action": "cancelled"}
                await cancel_pending_state(it["confirm_token"], self.db)
            else:
                it["result"] = executed
    final: list[tuple[int, dict]] = []
    for it in collected:
        result = it["result"]
        if isinstance(result, dict):
            result.pop("_confirm_future", None)
        result_str = _json.dumps(result, ensure_ascii=False)
        final.append((
            it["idx"],
            await _post_process_tool_result(
                self, it["tc"], it["fn_name"], it["fn_args"], result_str,
                task_id, agent_id, model_cfg, ts, failure_streak,
                degraded_tools, total_calls, it["idx"],
            ),
        ))
    return final
