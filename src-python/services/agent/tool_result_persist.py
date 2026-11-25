"""services/agent/tool_result_persist.py —— 工具结果写穿透

DSH 的 `tool/result` 是 durable session event；Yuncat 此前只把工具结果追加到
内存 history，重启后 assistant 的 tool_calls 没有配对的 tool 消息，恢复历史
会变成非法的 OpenAI 请求。

本模块把每轮的 tool_results 逐条写入 conversations（role="tool"），
tool_call_id 由 `tool_message_codec` 编码进 tool_calls_json。
"""
from __future__ import annotations

import logging

_log = logging.getLogger("yuncat.agent")


async def persist_tool_results(memory_service, session_id: str, agent_id: str,
                               tool_results: list[dict], task_id: str = "") -> int:
    """写入本轮全部工具结果，返回成功条数。失败逐条留痕，不阻断主流程。"""
    if memory_service is None or not tool_results:
        return 0
    written = 0
    for result in tool_results:
        if not isinstance(result, dict):
            continue
        try:
            await memory_service.add_message(
                session_id, "tool",
                str(result.get("content") or ""),
                agent_id=agent_id,
                tool_call_id=str(result.get("tool_call_id") or ""),
            )
            written += 1
        except Exception:
            _log.warning("持久化 tool 结果失败（不影响本轮执行）", exc_info=True)
    if task_id:
        from .pending_tool_calls import clear_pending
        clear_pending(task_id)
    return written
