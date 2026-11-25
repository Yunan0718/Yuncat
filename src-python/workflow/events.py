"""workflow/events.py —— v41 Append-only 事件日志（借鉴 Claude Code agent loop）

每节点开始/结束时追加一条事件到 workflow_runs.events_log 列。
崩溃后可从事件日志精确恢复——已完成节点跳过，进行中节点重置为 pending。
"""
from __future__ import annotations

import json
import time as _time
from dataclasses import dataclass, field

from config import MAX_EVENT_LOG_ENTRIES


@dataclass
class WorkflowEvent:
    event_id: int
    run_id: str
    node_id: str
    event_type: str  # "node_started" | "node_completed" | "node_failed" | "node_hitl"
    payload: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=_time.time)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "event_type": self.event_type,
            "payload": self.payload,
            "timestamp": self.timestamp,
        }


class EventLogger:
    """Append-only 事件日志——写入 workflow_runs.events_log（TEXT, JSON Array）。

    最多保留 1000 条，超出截断最旧。
    """

    MAX_EVENTS = MAX_EVENT_LOG_ENTRIES  # 事件日志上限——统一引用 config.py

    def __init__(self, repo):
        self.repo = repo

    async def record_event(
        self, run_id: str, node_id: str, event_type: str, payload: dict | None = None,
    ) -> None:
        events_json = await self.repo.get_events_log(run_id)
        events = json.loads(events_json or "[]")
        event = WorkflowEvent(
            event_id=len(events) + 1,
            run_id=run_id,
            node_id=node_id,
            event_type=event_type,
            payload=payload or {},
        )
        events.append(event.to_dict())
        if len(events) > self.MAX_EVENTS:
            events = events[-self.MAX_EVENTS:]
        await self.repo.set_events_log(run_id, json.dumps(events))


async def recover_from_log(repo, run_id: str) -> dict:
    """v41: 从事件日志精确恢复——比 snapshot 更细粒度。

    返回 {node_results, node_states} 可直接注入 engine.run 的 resume_from_snapshot。
    """
    events_json = await repo.get_events_log(run_id)
    events = json.loads(events_json or "[]")
    completed = set()
    in_progress = set()
    for evt in events:
        if evt["event_type"] == "node_completed":
            completed.add(evt["node_id"])
            in_progress.discard(evt["node_id"])
        elif evt["event_type"] == "node_started":
            in_progress.add(evt["node_id"])
    node_results = {nid: {"output": "recovered"} for nid in completed}
    node_states = {nid: "pending" for nid in in_progress}
    for nid in completed:
        node_states[nid] = "success"
    return {"node_results": node_results, "node_states": node_states}
