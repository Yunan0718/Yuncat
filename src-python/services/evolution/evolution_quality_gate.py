"""services/evolution/evolution_quality_gate.py —— Batch 5.3: Self-Evolution 质量门控

职责：
1. dry_run(workflow_graph, sample_inputs) —— 用 DryRunNodeExecutor 跑 workflow，返回成功率；
2. ab_validate(workflow_graph, baseline_graph, sample_inputs) —— 新 workflow vs baseline 的 A/B 对比；
3. evaluate(sandbox_result, ab_result, workflow_graph) —— 综合评分，决定 pending / auto_rejected。

设计原则：
- 与真实 WorkflowEngine 解耦：使用独立的 engine 实例 + DryRunWorkflowRepo；
- 不调用 LLM、不写 DB、不执行真实工具；
- 评分可配置，默认阈值来自 config.py。
"""
import logging
from typing import Any

from workflow.engine import WorkflowEngine

from config import (
    SELF_EVOLUTION_EVALUATION_PASS_THRESHOLD,
)
from services.evolution.dry_run_executor import (
    DryRunNodeExecutor,
    DryRunWorkflowRepo,
)

_log = logging.getLogger("yuncat.evolution.quality_gate")


# ── 评分权重 ──
_WEIGHT_COMPLETION = 0.4
_WEIGHT_NODE_SUCCESS = 0.2
_WEIGHT_AB_WIN = 0.3
_BONUS_HUMAN_NODE = 0.1
_PENALTY_DANGEROUS_TOOL = 0.2

_DANGEROUS_TOOL_NAMES = {"run_command", "write_file", "edit_file", "delete_file", "shell"}
_DANGEROUS_KEYWORDS = ["rm -rf", "drop table", "delete from", "format ", "shutdown"]


def _build_baseline_graph() -> dict:
    """无 workflow 时的 baseline：单节点直接调用 Agent。"""
    return {
        "nodes": [
            {
                "id": "baseline_agent",
                "type": "default",
                "prompt": "{{input}}",
            }
        ],
        "edges": [],
    }


def _extract_nodes_and_edges(graph: dict) -> tuple[list[dict], list[dict]]:
    """兼容 graph 中 nodes 为 list 或 dict 的两种形式。"""
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if isinstance(nodes, dict):
        nodes = list(nodes.values())
    return nodes, edges


def _has_dangerous_content(graph: dict) -> bool:
    """检查 workflow 是否包含显式危险工具或危险关键词。"""
    nodes, _ = _extract_nodes_and_edges(graph)
    for node in nodes:
        node_type = node.get("type", "default")
        # tool_execute 节点直接检查 tool_name
        if node_type == "tool_execute":
            tool_name = node.get("tool_name") or node.get("tool") or ""
            if tool_name.lower() in _DANGEROUS_TOOL_NAMES:
                return True
            args = node.get("arguments") or node.get("args") or {}
            args_json = str(args).lower()
            for kw in _DANGEROUS_KEYWORDS:
                if kw in args_json:
                    return True
        # 所有节点检查 prompt 中的危险关键词
        prompt = str(node.get("prompt", "")).lower()
        for kw in _DANGEROUS_KEYWORDS:
            if kw in prompt:
                return True
    return False


def _has_human_node(graph: dict) -> bool:
    """检查是否包含人类审批节点。"""
    nodes, _ = _extract_nodes_and_edges(graph)
    return any(node.get("type") == "human" for node in nodes)


class EvolutionQualityGate:
    """Self-Evolution workflow 质量门控。"""

    def __init__(
        self,
        executors: dict[str, Any] | None = None,
        pass_threshold: float = SELF_EVOLUTION_EVALUATION_PASS_THRESHOLD,
    ):
        self._executors = executors or {"default": DryRunNodeExecutor()}
        self._pass_threshold = pass_threshold

    async def dry_run(self, workflow_graph: dict, sample_inputs: list[str]) -> dict:
        """对 workflow_graph 用多个样本跑 dry-run，返回聚合结果。

        Returns:
            {
                "runs": [run_result, ...],
                "completion_rate": float,
                "node_success_rate": float,
                "total_nodes": int,
                "total_error_nodes": int,
            }
        """
        wf = {"nodes": workflow_graph.get("nodes", []), "edges": workflow_graph.get("edges", [])}
        repo = DryRunWorkflowRepo(wf)
        engine = WorkflowEngine(repo=repo, executors=self._executors)

        runs: list[dict] = []
        total_nodes = 0
        total_error_nodes = 0
        completed_count = 0

        for idx, sample_input in enumerate(sample_inputs):
            run_id = f"dry-run-{idx}"
            try:
                result = await engine.run("dry-run-wf", sample_input, run_id=run_id)
            except Exception as exc:
                _log.debug("Dry-run failed for sample %d: %s", idx, exc, exc_info=True)
                result = {"status": "failed", "error": str(exc), "results": {}}

            status = result.get("status", "failed")
            if status in ("completed", "partial_error"):
                completed_count += 1

            error_nodes = result.get("error_nodes", []) or []
            results = result.get("results", {}) or {}
            total_nodes += len(results)
            total_error_nodes += len(error_nodes)

            runs.append({
                "input": sample_input,
                "status": status,
                "error_nodes": error_nodes,
                "final_output": result.get("final_output", ""),
            })

        sample_count = max(1, len(sample_inputs))
        completion_rate = completed_count / sample_count
        node_success_rate = (
            (total_nodes - total_error_nodes) / total_nodes if total_nodes > 0 else 1.0
        )

        return {
            "runs": runs,
            "completion_rate": round(completion_rate, 2),
            "node_success_rate": round(node_success_rate, 2),
            "total_nodes": total_nodes,
            "total_error_nodes": total_error_nodes,
        }

    async def ab_validate(
        self,
        workflow_graph: dict,
        baseline_graph: dict | None,
        sample_inputs: list[str],
    ) -> dict:
        """新 workflow vs baseline 在相同样本上 dry-run，返回 A/B 指标。"""
        baseline = baseline_graph or _build_baseline_graph()
        new_result = await self.dry_run(workflow_graph, sample_inputs)
        base_result = await self.dry_run(baseline, sample_inputs)

        wins = 0
        losses = 0
        ties = 0
        for new_run, base_run in zip(new_result["runs"], base_result["runs"]):
            new_ok = new_run["status"] in ("completed", "partial_error")
            base_ok = base_run["status"] in ("completed", "partial_error")
            if new_ok and not base_ok:
                wins += 1
            elif not new_ok and base_ok:
                losses += 1
            else:
                ties += 1

        total = max(1, len(sample_inputs))
        return {
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "win_rate": round(wins / total, 2),
            "baseline_completion_rate": base_result["completion_rate"],
            "new_completion_rate": new_result["completion_rate"],
        }

    def evaluate(
        self,
        sandbox_result: dict,
        ab_result: dict,
        workflow_graph: dict,
    ) -> dict:
        """综合评分。

        Returns:
            {"passed": bool, "score": float, "reason": str}
        """
        completion = sandbox_result.get("completion_rate", 0.0)
        node_success = sandbox_result.get("node_success_rate", 0.0)
        win_rate = ab_result.get("win_rate", 0.0)

        score = (
            completion * _WEIGHT_COMPLETION
            + node_success * _WEIGHT_NODE_SUCCESS
            + win_rate * _WEIGHT_AB_WIN
        )

        if _has_human_node(workflow_graph):
            score += _BONUS_HUMAN_NODE
        if _has_dangerous_content(workflow_graph):
            score -= _PENALTY_DANGEROUS_TOOL

        score = max(0.0, min(1.0, round(score, 2)))
        passed = score >= self._pass_threshold

        reasons = [
            f"completion={completion}",
            f"node_success={node_success}",
            f"ab_win_rate={win_rate}",
        ]
        if _has_human_node(workflow_graph):
            reasons.append("has_human_node")
        if _has_dangerous_content(workflow_graph):
            reasons.append("dangerous_content_penalty")

        reason = f"score={score}; " + "; ".join(reasons)
        return {"passed": passed, "score": score, "reason": reason}
