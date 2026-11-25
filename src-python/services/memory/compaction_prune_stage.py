"""services/memory/compaction_prune_stage.py —— 压缩前的减压与折叠阶段

从 memory_compact.py 拆出来：memory_compact.py 已接近 800 行上限，
新逻辑单独放这里（也便于单独测试）。
"""
from __future__ import annotations

import logging

from services.memory.tool_result_pruner import (
    estimate_saved_tokens,
    persist_pruned_tool_results,
    prune_budgets_from_settings,
    prune_tool_results,
)

_log = logging.getLogger("yuncat.memory")


async def prune_for_pressure(
    self, session_id: str, conv: list[dict], current_tokens: int, max_tokens: int,
) -> tuple[list[dict], bool]:
    """裁剪超预算的 tool 结果——DSH compaction-tool-result-pruner。

    Returns: (裁剪后的历史, 压力是否已解除到不必再做摘要)
    """
    pruned, saved_chars, originals = prune_tool_results(conv, **prune_budgets_from_settings())
    if not saved_chars:
        return conv, False
    saved_tokens = estimate_saved_tokens(saved_chars)
    self._sessions[session_id] = pruned
    await persist_pruned_tool_results(self.db, session_id, pruned, originals)
    _log.info("Pruned %d oversized tool result(s): ~%d chars (~%d tokens) saved",
              len(originals), saved_chars, saved_tokens)
    return pruned, current_tokens - saved_tokens < max_tokens * self.COMPACT_THRESHOLD


def collapse_older(self, older: list[dict]) -> list[dict]:
    """Phase 44 C.5: Collapse 层——非活跃区折叠，减少送 LLM 压缩的 token 成本。"""
    if len(older) <= 30:
        return older
    from services.agent.agent_service import AgentService
    before = len(older)
    older = AgentService._collapse_inactive_context(older, active_window=20)
    _log.info("Collapse: %d older messages → %d (saved %d for LLM compaction)",
              before, len(older), before - len(older))
    return older


def tool_ratio_gate(self, session_id: str, conv: list[dict], usage_ratio: float) -> bool:
    """tool 结果占比门控——占比太低说明上下文不是被工具输出撑爆的，不压缩。"""
    tool_ratio = self._calc_tool_result_ratio(conv)
    min_ratio = (self.COMPACT_MIN_TOOL_RESULT_RATIO_LOW
                 if usage_ratio > 0.95 else self.COMPACT_MIN_TOOL_RESULT_RATIO_HIGH)
    if tool_ratio < min_ratio:
        _log.debug(f"Session {session_id}: tool ratio {tool_ratio:.0%} < {min_ratio:.0%}, skip compact")
        return True
    return False
