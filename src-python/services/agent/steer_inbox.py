"""services/agent/steer_inbox.py —— 运行中追加指令（steer）

DSH 的 inbox 有三种语义：followup（下一轮）、steer（当前轮下一步）、inject（不唤醒）。
Yuncat 一个任务就是一次执行，这里实现最小可用的 steer：

- 前端在任务 running 时发送 `steer`；
- 后端按 task 入队；
- 工具循环在**下一轮模型调用之前** claim，把指令作为 user 消息追加到 history，
  并持久化到 conversations；
- 通过 WS 回 `steer_applied`，前端可提示"已生效"。

限制：一次工具都不调用、单轮就结束的任务，steer 没有"下一步"可插入；
此时返回的 `steer_queued` 之后不会变成 applied（后续可扩展为自动 followup）。
"""
from __future__ import annotations

import logging
import time

from .steer_config import STEER_MAX_PENDING_PER_TASK, STEER_MAX_PENDING_TASKS

_log = logging.getLogger("yuncat.agent")

_PENDING: dict[str, list[dict]] = {}


class SteerQueueFull(RuntimeError):
    """该任务或全局 steer inbox 已达上限。"""


class SteerTaskUnavailable(RuntimeError):
    """目标任务不存在或已不在运行中。"""


def push_steer(task_id: str, prompt: str, session_id: str = "", agent_id: str = "") -> int:
    """入队一条追加指令，返回该任务当前排队数。"""
    text = str(prompt or "").strip()
    if not task_id or not text:
        return 0
    if task_id not in _PENDING and len(_PENDING) >= STEER_MAX_PENDING_TASKS:
        raise SteerQueueFull("待追加指令的任务数已达上限")
    items = _PENDING.setdefault(task_id, [])
    if len(items) >= STEER_MAX_PENDING_PER_TASK:
        raise SteerQueueFull("当前任务的追加指令数量已达上限")
    items.append({
        "prompt": text, "session_id": session_id,
        "agent_id": agent_id, "at": time.time(),
    })
    return len(items)


def pending_steer_count(task_id: str) -> int:
    return len(_PENDING.get(task_id, []))


def claim_steers(task_id: str, limit: int = STEER_MAX_PENDING_PER_TASK) -> list[dict]:
    """取走最多 limit 条；取空后删除任务条目。"""
    items = _PENDING.get(task_id)
    if not items:
        return []
    taken = items[:limit]
    del items[:limit]
    if not items:
        _PENDING.pop(task_id, None)
    return taken


def clear_steers(task_id: str) -> int:
    """丢弃并返回尚未生效的指令数（任务取消/结束时调用）。"""
    return len(_PENDING.pop(task_id, []))


async def enqueue_steer(service, task_id: str, prompt: str) -> tuple[dict, int]:
    """校验任务状态并入队；WS 与 HTTP 控制面共用这一入口。"""
    task = await service.get_task(task_id)
    if not task or task.get("status") != "running":
        raise SteerTaskUnavailable("任务不在运行中，无法追加指令")
    queued = push_steer(
        task_id, prompt,
        task.get("session_id", ""), task.get("agent_id", ""),
    )
    return task, queued


async def discard_pending_steers(service, task_id: str) -> None:
    """任务结束时丢弃未生效指令，并明确告知用户。"""
    discarded = clear_steers(task_id)
    if discarded:
        await service._safe_send(task_id, {
            "type": "steer_error", "task_id": task_id,
            "error": "任务已结束，追加指令未生效",
        })


async def apply_pending_steers(service, task_id: str, session_id: str,
                               agent_id: str, context: dict) -> int:
    """把已排队指令追加到 history 并持久化；返回应用条数。"""
    items = claim_steers(task_id)
    if not items:
        return 0
    history = context.setdefault("history", [])
    memory = getattr(service, "_memory_service", None)
    applied = 0
    for item in items:
        text = item["prompt"]
        history.append({"role": "user", "content": text})
        if memory is not None:
            try:
                await memory.add_message(session_id, "user", text, agent_id=agent_id)
            except Exception:
                _log.warning("持久化 steer 指令失败（仍会进入本轮上下文）", exc_info=True)
        try:
            await service._safe_send(task_id, {
                "type": "steer_applied", "task_id": task_id, "prompt": text,
            })
        except Exception:
            _log.debug("推送 steer_applied 失败", exc_info=True)
        applied += 1
    return applied


async def prepare_round(service, task_id: str, session_id: str, agent_id: str,
                        context: dict, prompt: str) -> str | None:
    """返回本轮要传给 adapter 的 prompt。

    - 首次调用：传原始任务 prompt（history 已由 build_agent_context 去重）；
    - 后续调用（同一 loop 或 stop-hook 的二次 loop）：先应用 steer，再返回 None。
      这样原始 prompt 不会每一轮重复，stop-hook 重入也不会再发一次。
    """
    if not context.get("_agent_prompt_sent"):
        context["_agent_prompt_sent"] = True
        return prompt
    await apply_pending_steers(service, task_id, session_id, agent_id, context)
    return None
