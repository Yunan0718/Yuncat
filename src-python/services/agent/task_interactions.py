"""任务终止时统一清理进行中的交互等待。"""
from services.agent.steer_inbox import discard_pending_steers
from tools.builtin.system_tool_ask_user import cancel_questions_for_task


async def discard_task_interactions(service, task_id: str) -> None:
    """结束任务时唤醒所有等待方，并让前端收起失去归属的交互框。"""
    await discard_pending_steers(service, task_id)
    # 内存护栏：任务结束清掉"未落地工具调用"登记（取消路径已先 drain 补合成结果）
    from .pending_tool_calls import clear_pending
    clear_pending(task_id)
    # 内存护栏：工具事件序号计数器同样按任务清理
    from .event_seq import clear_task_seq
    clear_task_seq(task_id)
    for token in cancel_questions_for_task(task_id):
        await service._safe_send(task_id, {
            "type": "user_question_closed", "task_id": task_id,
            "token": token, "reason": "task_ended",
        })
