"""services/plan_executor.py —— Plan 步骤执行器 (Phase 42)

从 AgentService.execute_plan_steps() 提取独立接口。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.agent.agent_service import AgentService

_log = logging.getLogger("yuncat.plan_executor")


class PlanExecutor:
    """Plan 步骤执行器 — 通过 self._ag 委托到 AgentService。"""

    def __init__(self, agent_service: AgentService):
        self._ag = agent_service

    async def execute_plan_steps(self, parent_task_id: str, agent_id: str) -> None:
        await self._ag.execute_plan_steps(parent_task_id, agent_id)
