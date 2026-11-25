"""services/evolution/dry_run_executor.py —— Batch 5.3: Self-Evolution dry-run 执行器

为 WorkflowEngine 提供一组无副作用的节点执行器，用于在 Self-Evolution
生成 workflow 后做沙箱 smoke test：不调用 LLM、不执行真实工具、不写 DB。

设计原则：
- 完全遵循 workflow.node_executor.NodeExecutor 协议；
- 复用现有 ManagerNodeExecutor / MergeNodeExecutor 的无副作用逻辑；
- 对可能产生副作用的节点（agent/tool/human/sub_workflow/summary）返回确定性 mock；
- DryRunWorkflowRepo 让 WorkflowEngine 无需真实 workflow_runs 即可运行。
"""
import hashlib
import json
import logging
import re
from typing import Any

from workflow.node_executor import (
    MergeNodeExecutor,
    _eval_condition,
)

_log = logging.getLogger("yuncat.evolution.dry_run")


# ── 工具级常量 ──
_MOCK_OUTPUT_MAX_LEN = 200


def _stable_mock_text(seed: str, node_id: str = "") -> str:
    """基于 seed 生成确定性 mock 文本，便于测试断言。"""
    base = f"[dry-run output for {node_id}] " if node_id else "[dry-run output] "
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return (base + f"mock-{digest}")[:_MOCK_OUTPUT_MAX_LEN]


class DryRunWorkflowRepo:
    """不持久化的 workflow repo 适配器——仅供 dry-run 使用。"""

    def __init__(self, wf: dict):
        self._wf = wf

    async def get_workflow(self, workflow_id: str) -> dict:
        return self._wf

    async def update_snapshot(self, run_id: str, snapshot_json: str) -> None:
        pass

    async def get_run(self, run_id: str) -> dict | None:
        return None


class DryRunSummaryNodeExecutor:
    """Dry-run 摘要节点——复用合并逻辑，但不调用模型/不写 DB。"""

    async def execute(self, node: dict, context: dict[str, Any]) -> dict[str, Any]:
        node_results = context.get("node_results", {})
        inputs = node.get("inputs", [])
        if not inputs:
            self_id = node.get("id", "")
            inputs = [nid for nid in node_results if nid != self_id]

        parts: list[str] = []
        for nid in inputs:
            result = node_results.get(nid, {})
            output = result.get("output", "") if isinstance(result, dict) else str(result)
            if output:
                parts.append(f"### {nid}\n{output}")
        content = "\n\n".join(parts)

        title = node.get("title", f"工作流摘要 {node.get('id', '')}")
        summary_text = f"[dry-run summary of {title}]\n{content[:500]}"
        return {
            "output": summary_text,
            "summary_title": title,
            "summarized_from": inputs,
            "persisted": False,
            "dry_run": True,
        }


class DryRunSubWorkflowExecutor:
    """Dry-run 子工作流节点——返回确定性占位，不递归真实执行。"""

    async def execute(self, node: dict, context: dict[str, Any]) -> dict[str, Any]:
        child_wf_id = node.get("sub_workflow_id") or node.get("workflow_id")
        if not child_wf_id:
            return {"error": "Missing sub_workflow_id", "status": "error", "dry_run": True}
        node_id = node.get("id", "unknown")
        return {
            "output": f"[dry-run sub-workflow {child_wf_id} from node {node_id}]",
            "child_run_id": f"dry-run:{node_id}",
            "status": "completed",
            "dry_run": True,
        }


class DryRunNodeExecutor:
    """通用 dry-run 节点执行器分发器。"""

    def __init__(self):
        self._merge_executor = MergeNodeExecutor()
        self._summary_executor = DryRunSummaryNodeExecutor()
        self._sub_workflow_executor = DryRunSubWorkflowExecutor()

    async def execute(self, node: dict, context: dict[str, Any]) -> dict[str, Any]:
        node_type = node.get("type", "default")
        node_id = node.get("id", "unknown")
        prompt = node.get("prompt", context.get("input", ""))

        # 1. 可能产生副作用的 Agent / 工具 / OC 节点 → mock
        if node_type in ("default", "agent", "ha_native"):
            seed = f"{node_id}:{prompt}"
            return {
                "output": _stable_mock_text(seed, node_id),
                "agent_id": node.get("agent_id", "dry-run"),
                "node_type": node_type,
                "status": "success",
                "dry_run": True,
            }

        if node_type in ("oc_vision", "oc_search", "oc_messaging"):
            return {
                "output": f"[OC mock] {node_type} executed",
                "agent_id": "openclaw",
                "tool": node_type,
                "status": "success",
                "dry_run": True,
            }

        if node_type == "human":
            return {
                "output": "approved (dry-run)",
                "status": "approved",
                "node_id": node_id,
                "dry_run": True,
            }

        if node_type == "tool_execute":
            tool_name = node.get("tool_name") or node.get("tool") or "unknown"
            args = node.get("arguments") or node.get("args") or {}
            return {
                "output": json.dumps({
                    "tool_name": tool_name,
                    "arguments": args,
                    "result": f"[dry-run result for {tool_name}]",
                }, ensure_ascii=False),
                "tool_name": tool_name,
                "status": "success",
                "dry_run": True,
            }

        # 2. 无副作用 / 纯逻辑节点 → 尽量复用现有逻辑
        if node_type == "manager":
            conditions = node.get("conditions", [])
            node_results = context.get("node_results", {})
            for cond in conditions:
                when = cond.get("when", "default")
                if when == "default":
                    goto = cond.get("goto")
                    return {
                        "output": f"Routed to {goto}",
                        "selected_branch": goto,
                        "condition_matched": when,
                        "dry_run": True,
                    }
                # 兼容未加 {{}} 占位符的简写条件，例如 "prev.output == 'yes'"
                evaluated = when
                if "{{" not in evaluated:
                    evaluated = re.sub(
                        r"\b([A-Za-z_]\w*\.[A-Za-z_]\w*)\b", r"{{\1}}", evaluated
                    )
                if _eval_condition(evaluated, node_results):
                    goto = cond.get("goto")
                    return {
                        "output": f"Routed to {goto}",
                        "selected_branch": goto,
                        "condition_matched": when,
                        "dry_run": True,
                    }
            return {
                "output": "No branch selected",
                "selected_branch": None,
                "error": "No condition matched and no default provided",
                "dry_run": True,
            }

        if node_type == "merge":
            result = await self._merge_executor.execute(node, context)
            result["dry_run"] = True
            return result

        if node_type == "summary":
            result = await self._summary_executor.execute(node, context)
            return result

        if node_type == "sub_workflow":
            return await self._sub_workflow_executor.execute(node, context)

        # 3. 未知类型 → 返回 error 但不抛异常，保证引擎能继续统计
        _log.debug("Dry-run unknown node type: %s", node_type)
        return {
            "output": f"[dry-run unknown type: {node_type}]",
            "status": "error",
            "error": f"Unknown node type: {node_type}",
            "dry_run": True,
        }
