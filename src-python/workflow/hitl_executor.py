"""workflow/hitl_executor.py —— HITL 人机交互节点执行器

HumanNodeExecutor: 发送 WS workflow_hitl 消息 → await human_decision → 批准/拒绝/重试/超时
"""
import asyncio
import logging
from typing import Any

from config import HITL_DEFAULT_TIMEOUT_SEC  # HITL 审批默认超时

_log = logging.getLogger("yuncat.hitl")


class HumanNodeExecutor:
    """人机交互节点执行器。

    执行 type="human" 的工作流节点——向用户发送审批请求，
    等待用户做出决策（批准/拒绝/重试），超时则自动拒绝。
    """

    def __init__(self, agent_service=None):
        self.agent_service = agent_service
        # run_id → asyncio.Event 映射，用于等待用户决策
        self._pending_decisions: dict[str, asyncio.Event] = {}
        # run_id → decision 映射
        self._decisions: dict[str, dict] = {}
        self._default_timeout = HITL_DEFAULT_TIMEOUT_SEC  # HITL 审批默认超时（config 中可调）

    async def execute(self, node: dict, context: dict[str, Any]) -> dict[str, Any]:
        """暂停工作流，等待用户决策"""
        run_id = context.get("run_id", "")
        node_id = node.get("id", "unknown")
        prompt = node.get("prompt", "请确认此步骤的执行结果")

        # 创建等待事件
        event = asyncio.Event()
        decision_key = f"{run_id}:{node_id}"
        self._pending_decisions[decision_key] = event

        try:
            # 发送 WS 消息（Phase 87 审查 M-3: 移入 try 块——push 失败时 finally
            # 仍清理 _pending_decisions，避免 entry 泄漏）
            if self.agent_service:
                await self.agent_service.push_hitl_request(
                    run_id=run_id, node_id=node_id, prompt=prompt,
                    timeout=self._default_timeout,
                )
            # 等待用户决策或超时
            await asyncio.wait_for(event.wait(), timeout=self._default_timeout)
            decision = self._decisions.pop(decision_key, {"action": "timeout"})
        except asyncio.TimeoutError:
            decision = {"action": "timeout"}
        finally:
            self._pending_decisions.pop(decision_key, None)
            self._decisions.pop(decision_key, None)  # 无条件清理防泄漏

        action = decision.get("action", "timeout")
        if action == "approve":
            return {"output": decision.get("note", "已批准"), "approved": True}
        elif action == "retry":
            raise RuntimeError(f"用户请求重试: {decision.get('note', '')}")
        else:
            # reject 或 timeout
            return {
                "error": decision.get("note", "用户拒绝或超时"),
                "approved": False,
                "action": action,
            }

    def submit_decision(self, run_id: str, node_id: str, decision: dict) -> bool:
        """用户提交决策——唤醒等待中的工作流节点。

        返回 True 表示成功提交，False 表示决策键不存在（已超时或已处理）。
        """
        decision_key = f"{run_id}:{node_id}"
        self._decisions[decision_key] = decision
        event = self._pending_decisions.get(decision_key)
        if event:
            event.set()
            return True
        return False
