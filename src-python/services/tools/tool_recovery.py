"""services/tool_recovery.py —— 工具失败自愈引擎 (Phase 42)

从 AgentService 提取：_analyze_tool_failure_and_send +
_suggest_alternative_tool + _map_args_for_alternative。

注：_suggest_alternative_tool / _map_args_for_alternative 是 agent_service.py
的**模块级函数**（非 AgentService 实例方法），需用模块函数导入调用，
不能用 self._ag._xxx()——否则抛 AttributeError。
_analyze_tool_failure_and_send 是 AgentService 实例方法，可用 self._ag._xxx()。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.agent.agent_service import AgentService

_log = logging.getLogger("yuncat.tool_recovery")


class ToolRecoveryEngine:
    """工具失败自愈引擎 — 通过 self._ag 委托到 AgentService。"""

    ALTERNATIVES = {
        "read_file": "search_files", "search_files": "list_dir",
        "run_command": "search_files", "list_dir": "search_files",
        "write_file": "read_file", "delete_file": "list_dir",
        "mkdir": "list_dir", "rename_file": "list_dir",
        "openclaw_vision": "read_file", "web_search": "web_fetch",
        "web_fetch": "read_file",
    }

    def __init__(self, agent_service: AgentService):
        self._ag = agent_service

    def suggest_alternative_tool(self, failed_tool: str) -> str | None:
        # 模块级函数——不能通过 self._ag._xxx() 调用（会抛 AttributeError）
        from services.agent.agent_service import _suggest_alternative_tool
        return _suggest_alternative_tool(failed_tool)

    def map_args_for_alternative(self, from_tool: str, to_tool: str,
                                  args: dict) -> dict:
        # 模块级函数——不能通过 self._ag._xxx() 调用（会抛 AttributeError）
        from services.agent.agent_service import _map_args_for_alternative
        return _map_args_for_alternative(from_tool, to_tool, args)

    async def analyze_tool_failure_and_send(self, task_id: str, tool_name: str,
                                             args: dict, result_str: str,
                                             agent_id: str, model_cfg: dict) -> None:
        # 实例方法——可用 self._ag._xxx() 委托
        await self._ag._analyze_tool_failure_and_send(
            task_id, tool_name, args, result_str, agent_id, model_cfg)
