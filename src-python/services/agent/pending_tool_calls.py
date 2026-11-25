"""services/agent/pending_tool_calls.py —— 未被结果覆盖的工具调用登记

DSH 在取消时会给未启动的调用补一条合成结果，保证 tool/call 与 tool/result 成对。
Yuncat 的工具调用在内存 accumulator 里，任务取消后无法再从循环外拿到；这里按 task
登记本轮调用，正常完成后清空，取消时 drain 并写合成结果。
"""
from __future__ import annotations

import json
import logging
import time as _time

_log = logging.getLogger("yuncat.agent")

_PENDING: dict[str, dict] = {}

# 内存护栏：异常路径（工具结果没落地、任务被硬杀）可能不清理登记 → 条数上限 + 过期淘汰。
# 正常路径每轮 persist_tool_results 会清空，这里只兜底。
_PENDING_MAX_TASKS = 64
_PENDING_TTL_SEC = 3600.0


def _evict_stale() -> None:
    """淘汰过期与超量的登记（dict 保序 → 先淘汰最早插入的）。"""
    now = _time.time()
    for key in [k for k, v in _PENDING.items()
                if now - float(v.get("ts") or 0) > _PENDING_TTL_SEC]:
        _PENDING.pop(key, None)
    while len(_PENDING) > _PENDING_MAX_TASKS:
        _PENDING.pop(next(iter(_PENDING)), None)


def register_pending(task_id: str, session_id: str, agent_id: str,
                     calls: list[dict]) -> None:
    """登记一轮待执行的工具调用（同 id 覆盖，幂等）。"""
    if not task_id or not calls:
        return
    entry = _PENDING.setdefault(task_id, {
        "session_id": session_id, "agent_id": agent_id, "calls": {}, "ts": _time.time(),
    })
    entry["session_id"] = session_id or entry.get("session_id", "")
    entry["agent_id"] = agent_id or entry.get("agent_id", "")
    for call in calls:
        if not isinstance(call, dict):
            continue
        call_id = str(call.get("id") or "")
        if not call_id:
            continue
        fn = call.get("function") or {}
        entry["calls"][call_id] = {
            "name": str(fn.get("name") or ""),
            "arguments": str(fn.get("arguments") or ""),
        }
    # 插入之后再淘汰，保证登记数始终 ≤ 上限（插入前淘汰会多留一条）
    _evict_stale()


def clear_pending(task_id: str) -> None:
    """本轮工具结果已全部落地，清空登记。"""
    if task_id:
        _PENDING.pop(task_id, None)


def drain_pending(task_id: str) -> dict | None:
    """取走并清空某任务的登记（取消路径用）。"""
    return _PENDING.pop(task_id, None) if task_id else None


def pending_count(task_id: str) -> int:
    return len((_PENDING.get(task_id) or {}).get("calls", {}))


async def persist_cancelled_results(service, task_id: str, task: dict | None = None) -> int:
    """取消时为未覆盖的调用写合成 tool 结果，返回写入条数。"""
    entry = drain_pending(task_id)
    if not entry or not entry.get("calls"):
        return 0
    session_id = entry.get("session_id") or (task or {}).get("session_id", "")
    agent_id = entry.get("agent_id") or (task or {}).get("agent_id", "")
    if not session_id:
        _log.warning("取消时缺少 session_id，跳过合成工具结果 task=%s", task_id[:8])
        return 0
    results = [
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps({
                "success": False,
                "error": "任务已取消：该工具调用未返回结果",
                "cancelled": True,
            }, ensure_ascii=False),
        }
        for call_id in entry["calls"]
    ]
    from .tool_result_persist import persist_tool_results

    written = await persist_tool_results(
        getattr(service, "_memory_service", None), session_id, agent_id, results,
    )
    _log.info("取消任务 %s：补写 %d 条合成工具结果", task_id[:8], written)
    return written
