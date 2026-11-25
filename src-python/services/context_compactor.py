"""services/context_compactor.py —— 三级上下文压缩器 (Phase 41)

真正委托模式: AgentService 通过 self.compactor.xxx() 调用，
压缩逻辑仍在 AgentService 中（已验证 200+ 行），此处提供统一接口。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from config import COMPACT_KEEP_RECENT_EMERGENCY  # Phase 5 P2: 紧急修剪保留条数统一管理

if TYPE_CHECKING:
    from services.agent.agent_service import AgentService

_log = logging.getLogger("yuncat.compactor")


class ContextCompactor:
    """三级压缩器委托 — 持有 AgentService 引用，所有方法转发到对应实现。

    区别于静态代理: 每个方法都是实例方法，通过 self._ag 转发，
    确保 AgentService._micro_compact_history() 内部调 _micro_dump_tool_output()
    仍然在同一个实例上下文中执行。
    """

    def __init__(self, agent_service: AgentService):
        self._ag = agent_service

    # ── 一级: 微型压缩 (0 API 调用) ──

    @staticmethod
    def micro_dump_tool_output(content: str) -> str:
        from services.agent.agent_service import AgentService
        return AgentService._micro_dump_tool_output(content)

    def micro_compact_history(self, history: list[dict], model_cfg: dict | None = None) -> int:
        return self._ag._micro_compact_history(history, model_cfg=model_cfg)

    # ── 二级: 标准压缩 (LLM 结构化摘要) ──

    async def maybe_compact_context(self, session_id: str, current_tokens: int,
                                     max_tokens: int) -> bool:
        ms = getattr(self._ag, '_memory_service', None)
        if ms is None:
            return False
        return await ms.maybe_compact_context(session_id, current_tokens, max_tokens)

    # ── 三级: 紧急修剪 ──

    def emergency_trim_history(self, history: list[dict],
                                keep_recent: int = COMPACT_KEEP_RECENT_EMERGENCY,
                                model_config: dict | None = None) -> list[dict]:
        return self._ag._emergency_trim_history(history, keep_recent, model_config=model_config)

    # ── 压缩后恢复 ──

    async def reattach_context_after_compact(self, session_id: str, context: dict,
                                              model_cfg: dict) -> None:
        await self._ag._reattach_context_after_compact(session_id, context, model_cfg)

    def extract_recent_files(self, history: list[dict]) -> list[str]:
        return self._ag._extract_recent_files(history)
