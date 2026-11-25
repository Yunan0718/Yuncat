"""services/agent/tool_dispatch.py —— 工具调度的组装计算（从 agent_tool_loop 拆出）

Phase 45 D.7 的并发分组规则原本内联在 execute_tool_loop 里。
agent_tool_loop.py 已经超过文件行数门禁（静态棘轮守卫不允许它再长），
因此把这块纯计算逻辑放到这里，与 .confirmable 当初拆出去同一个理由。
"""


def split_by_concurrency(executable_calls: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """按注册表声明的并发级别分组：safe / safe_with_lock / 其余(exclusive)

    分组只看工具名，不看参数；未注册的工具按 exclusive 处理（fail-closed）。
    """
    from tools.registry import get_concurrency

    safe: list[dict] = []
    locked: list[dict] = []
    exclusive: list[dict] = []
    for call in executable_calls:
        level = get_concurrency(call["function"]["name"])
        if level == "safe":
            safe.append(call)
        elif level == "safe_with_lock":
            locked.append(call)
        else:
            exclusive.append(call)
    return safe, locked, exclusive


async def review_dangerous_tool(
    task_id: str, tool_name: str, args: dict, force: bool = False,
    approve_on_uncertain: bool = True,
) -> dict | None:
    """危险工具执行前终审。

    调 FinalReviewAgent.review(stage="pre_dangerous_tool")。
    返回 None 表示**没有可用的审查员**；返回 dict 含 action 决定后续流程。

    force=True 跳过终审自带的冷却缓存——用于审批方式=ai：
    那时结论就是决策，不能把上一个工具的结论套到下一个工具上。

    approve_on_uncertain（2026-10-08）：
      审查员没给出结论时怎么算。AI 代审档位必须传 False——
      审查结论就是决策，拿不到结论要交回用户，不能默认放行。
      实测旧行为对 `rm -rf /` 也因"解析失败"而 pass。
    """
    import logging

    log = logging.getLogger("yuncat.agent")
    try:
        from services.long_task.final_review_agent import get_final_review_agent

        agent = get_final_review_agent()
        if agent is None:
            return None
        return await agent.review(
            task_id=task_id,
            stage="pre_dangerous_tool",
            context={"tool_name": tool_name, "args": args, "exec_context": ""},
            force=force,
            approve_on_uncertain=approve_on_uncertain,
        )
    except Exception:
        log.debug("final_review pre_dangerous_tool failed", exc_info=True)
        return None
