"""services/agent/event_seq.py —— 工具事件序号（按 task 单调递增）

回放时 tool/call 与 tool/result 要靠 seq 配对，所以每个 task 维护一个计数器。
原先计数器直接放在 agent_tool_loop 的模块级 dict 里，**任务结束从不清**——
跑过工具的任务每个留一条（长跑进程下缓慢增长）。

这里独立成模块：① 任务结束可显式清理；② 异常路径下有条数上限与过期淘汰兜底。
"""
from __future__ import annotations

import time as _time

# 内存护栏：上限 + 过期（秒）
_MAX_TASKS = 256
_TTL_SEC = 3600.0

# task_id -> (seq, 最后更新时间)
task_event_seq: dict[str, tuple[int, float]] = {}


def next_seq(task_id: str) -> int:
    """为指定 task 的下一个工具事件分配单调递增 seq。"""
    seq, ts = task_event_seq.get(task_id, (0, _time.time()))
    seq += 1
    task_event_seq[task_id] = (seq, ts)
    _evict_stale()
    return seq


def clear_task_seq(task_id: str) -> None:
    """任务结束时清掉计数（正常路径显式清理，异常路径由上限兜底）。"""
    if task_id:
        task_event_seq.pop(task_id, None)


def _evict_stale() -> None:
    """淘汰过期与超量的计数器（dict 保序 → 先淘汰最早插入的）。"""
    now = _time.time()
    for key in [k for k, v in task_event_seq.items() if now - v[1] > _TTL_SEC]:
        task_event_seq.pop(key, None)
    while len(task_event_seq) > _MAX_TASKS:
        task_event_seq.pop(next(iter(task_event_seq)), None)
