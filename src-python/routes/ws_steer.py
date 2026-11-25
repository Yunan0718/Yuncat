"""routes/ws_steer.py —— 运行中追加指令（steer）的 WS 处理

前端在任务 running 时发送：
    {"type": "steer", "task_id": "...", "prompt": "..."}

后端校验任务仍在运行后入队；工具循环在下一轮模型调用前 claim 并应用。
"""
from __future__ import annotations

import logging

from config import WS_PROMPT_MAX_CHARS
from services.agent.steer_inbox import SteerQueueFull, SteerTaskUnavailable, enqueue_steer

_log = logging.getLogger("yuncat.ws")


async def handle_steer(app, ws, msg: dict) -> None:
    task_id = str(msg.get("task_id") or "").strip()
    prompt = str(msg.get("prompt") or "").strip()
    if not task_id or not prompt:
        await ws.send_json({
            "type": "steer_error", "task_id": task_id,
            "error": "缺少 task_id 或 prompt",
        })
        return
    if len(prompt) > WS_PROMPT_MAX_CHARS:
        await ws.send_json({
            "type": "steer_error", "task_id": task_id,
            "error": f"追加指令过长（{len(prompt)} 字符，上限 {WS_PROMPT_MAX_CHARS}）",
        })
        return

    service = app.state.agent_service
    try:
        _task, queued = await enqueue_steer(service, task_id, prompt)
    except (SteerQueueFull, SteerTaskUnavailable) as exc:
        await ws.send_json({
            "type": "steer_error", "task_id": task_id, "error": str(exc),
        })
        return
    _log.info("steer queued task=%s queued=%d", task_id[:8], queued)
    await ws.send_json({
        "type": "steer_queued", "task_id": task_id,
        "queued": queued, "prompt": prompt,
    })
