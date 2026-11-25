"""services/agent/reasoning_trace.py —— 思考链（reasoning_content）的累计与落库

2026-10-07：原本思考链只存在内存 + task_state 快照里，conversations 表的
reasoning_content 列**从来没被写过**（实测全库非空数 0）——重启后思考链丢失，
前端也拿不回历史 thinking。

单独成文件的原因：这两个消费方（agent_tool_loop / agent_finalize）都已贴着
尺寸基线，静态棘轮禁止继续变长，新逻辑按仓内既有做法（runtime-bootstrap.js /
auth-recovery.js / provider_key_state.py）外置。
"""
from __future__ import annotations


class ReasoningTrace:
    """按任务累计思考链分片，供收尾时一次性取出落库。

    一个任务一个实例（存在 agent service 上，与 _task_snapshots 同模式）。
    """

    def __init__(self) -> None:
        self._parts: list[str] = []

    def add(self, piece: str, limit: int = 0) -> None:
        """累计一轮的 thinking 分片；limit>0 时按上限截断。"""
        if not piece:
            return
        self._parts.append(piece[:limit] if limit > 0 else piece)

    def joined(self) -> str:
        """全部思考链（分片间空行分隔）。"""
        return "\n\n".join(self._parts)

    def reset(self) -> None:
        self._parts.clear()


def record_reasoning(owner, piece: str, limit: int = 0) -> None:
    """把一轮 thinking 记到 owner._reasoning_trace 上（缺失时静默跳过）。"""
    trace = getattr(owner, "_reasoning_trace", None)
    if trace is not None:
        trace.add(piece, limit)


def joined_reasoning(owner) -> str:
    """取 owner 上累计的思考链（缺失时返回空串）。"""
    trace = getattr(owner, "_reasoning_trace", None)
    return trace.joined() if trace is not None else ""
