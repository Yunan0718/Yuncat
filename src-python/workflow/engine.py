"""workflow/engine.py —— 自研 DAG 编排引擎（~200 行）

v32: 拓扑排序 + asyncio.gather 并行 + checkpoint 快照 + 循环检测
v32 失败语义: error 节点不解锁下游；并行 batch 只读 node_results
"""
import asyncio
import json
import logging

from .node_executor import NodeExecutor
from config import (
    MAX_TOKENS_WORKFLOW_NODE,
    WORKFLOW_EVENT_OUTPUT_MAX_CHARS, SUB_AGENT_ERROR_PREVIEW_CHARS,
    WORKFLOW_NODE_PROMPT_PREVIEW_CHARS, WORKFLOW_NODE_ERROR_PREVIEW_CHARS,
    DEFAULT_AGENT_ID,
)

_log = logging.getLogger("yuncat.dag")

# P0-1.10: 工作流节点并发上限——对标 long_task/executor.py:23 的 Semaphore(3) 模式
# 防止超宽 DAG 一次性 gather 大量节点导致 LLM 调用洪水 / 资源耗尽
MAX_WORKFLOW_CONCURRENT_NODES = 3


class WorkflowEngine:
    def __init__(self, repo=None, executors: dict[str, NodeExecutor] | None = None, event_logger=None, model_config=None, adapters=None):
        self.repo = repo
        self.executors = executors or {}
        self.event_logger = event_logger  # v56: 可选 EventLogger
        self._model_config = model_config  # Phase 33: 节点失败 LLM 分析
        self._adapters = adapters  # Phase 56: 节点失败分析走 Agent Adapter，Yuncat 不调 LLM
        self._pause_events: dict[str, asyncio.Event] = {}  # Phase 51-E: run_id -> pause signal

    def _validate_graph(self, nodes: dict | list, edges: list) -> None:
        """Kahn 算法循环检测——非 DAG 抛 ValueError"""
        # 兼容 list 格式（转换为 dict keyed by id）
        if isinstance(nodes, list):
            nodes = {n["id"]: n for n in nodes}
        # Phase 87 审查 L-2: 空工作流拒绝——原代码空 nodes 直接返回 completed，
        # 误导用户以为执行成功。改为抛 ValueError，由调用方（_run_and_finalize）
        # 捕获后标记 failed。
        if not nodes:
            raise ValueError("Workflow has no nodes")
        in_deg = {nid: 0 for nid in nodes}
        children = {nid: [] for nid in nodes}
        for e in edges:
            children[e["from"]].append(e["to"])
            in_deg[e["to"]] += 1
        from collections import deque
        q = deque(nid for nid, d in in_deg.items() if d == 0)
        visited = 0
        while q:
            nid = q.popleft()
            visited += 1
            for c in children.get(nid, []):
                in_deg[c] -= 1
                if in_deg[c] == 0:
                    q.append(c)
        if visited != len(nodes):
            raise ValueError(
                f"Graph contains a cycle ({visited}/{len(nodes)} nodes reachable)"
            )

    async def run(
        self,
        workflow_id: str,
        initial_input: str,
        run_id: str | None = None,
        resume_from_snapshot: dict | None = None,
        node_overrides: dict[str, dict] | None = None,
    ) -> dict:
        """执行工作流。支持快照恢复和节点参数覆盖。

        返回: {results, final_output, status, error_nodes}
        """
        wf = await self.repo.get_workflow(workflow_id)
        nodes = {n["id"]: n for n in wf["nodes"]}
        edges = wf["edges"]
        # v63: 应用 node_overrides（retry_with_inputs 场景）
        if node_overrides:
            for nid, overrides in node_overrides.items():
                if nid in nodes:
                    nodes[nid] = {**nodes[nid], **overrides}
        self._validate_graph(nodes, edges)

        # 构建依赖图
        in_degree = {nid: 0 for nid in nodes}
        children = {nid: [] for nid in nodes}
        for e in edges:
            children[e["from"]].append(e["to"])
            in_degree[e["to"]] += 1

        node_states = {nid: "pending" for nid in nodes}

        if resume_from_snapshot:
            node_results = resume_from_snapshot.get("node_results", {})
            context = resume_from_snapshot.get("context", {})
            context["input"] = initial_input  # snapshot 不含 input，需补充
            for completed_nid in node_results:
                result = node_results[completed_nid]
                if result.get("status") == "paused":
                    # Phase 53: 子工作流暂停节点在恢复时重试
                    node_states[completed_nid] = "pending"
                elif "error" not in result:
                    node_states[completed_nid] = "success"
                    # Phase 100: manager 节点恢复时只激活选定分支
                    if nodes[completed_nid].get("type") == "manager" and isinstance(result, dict):
                        selected = result.get("selected_branch")
                        for child in children.get(completed_nid, []):
                            if child == selected:
                                in_degree[child] -= 1
                            else:
                                node_states[child] = "skipped"
                    else:
                        for child in children.get(completed_nid, []):
                            in_degree[child] -= 1
                else:
                    # v32: resume 时重置 error 节点为 pending（可重试）
                    node_states[completed_nid] = "pending"
        else:
            node_results = {}
            context = {"input": initial_input}
            if run_id:
                await self._save_snapshot(run_id, context, node_states, node_results)

        while True:
            # Phase 51-E: 检查暂停请求——在 batch 边界安全停止
            if run_id and self._is_pause_requested(run_id):
                current_node_id = self._pick_resume_node_id(in_degree, node_states)
                await self._persist_pause_state(run_id, context, node_states, node_results, current_node_id)
                self._clear_pause_request(run_id)
                return {
                    "results": node_results,
                    "final_output": context.get("last_output", ""),
                    "status": "paused",
                    "error_nodes": [nid for nid, s in node_states.items() if s == "error"],
                    "error_analysis": None,
                    "paused_at": current_node_id,
                }

            ready = [
                nid for nid, deg in in_degree.items()
                if deg == 0 and node_states.get(nid) == "pending"
            ]
            if not ready:
                break

            # P0-1.10: 信号量限流——防止超宽 DAG 一次性 gather 大量节点
            # 对标 long_task/executor.py:138 的 Semaphore(3) 模式
            node_semaphore = asyncio.Semaphore(MAX_WORKFLOW_CONCURRENT_NODES)

            async def execute_node(nid: str):
                node = nodes[nid]
                executor = self.executors.get(node.get("type"))
                # v56: 记录节点开始事件
                if self.event_logger and run_id:
                    await self.event_logger.record_event(run_id, nid, "node_started")
                if not executor:
                    if self.event_logger and run_id:
                        await self.event_logger.record_event(run_id, nid, "node_failed", {"error": f"Unknown type: {node['type']}"})
                    return (nid, {"error": f"Unknown type: {node['type']}"}, "error")
                # P0-1.10: 用 semaphore 限制并发——executor.execute 通常调 LLM
                async with node_semaphore:
                    try:
                        node_ctx = {
                            "input": context["input"],
                            "rules": context.get("rules", ""),
                            "session_id": context.get("session_id", ""),
                            "run_id": run_id or "",
                        }
                        node_ctx["node_results"] = dict(node_results)
                        output = await executor.execute(node, node_ctx)
                        state = "success"
                        if isinstance(output, dict) and output.get("status") == "paused":
                            state = "paused"
                        elif isinstance(output, dict) and output.get("status") == "error":
                            state = "error"
                        # v56: 记录节点完成
                        if self.event_logger and run_id:
                            await self.event_logger.record_event(run_id, nid, "node_completed", {"output": str(output)[:WORKFLOW_EVENT_OUTPUT_MAX_CHARS]})
                        return (nid, output, state)
                    except Exception as e:
                        _log.exception(f"Node {nid} failed")
                        if self.event_logger and run_id:
                            await self.event_logger.record_event(run_id, nid, "node_failed", {"error": str(e)[:SUB_AGENT_ERROR_PREVIEW_CHARS]})
                        return (nid, {"error": str(e)}, "error")

            batch = await asyncio.gather(*[execute_node(nid) for nid in ready])

            paused_nodes: list[str] = []
            for nid, result, state in batch:
                node_results[nid] = result
                node_states[nid] = state
                if state == "success":
                    # 保留所有已完成节点的最后输出（不只是最后一个）
                    context[f"_output_{nid}"] = result.get("output", "")
                    context["last_output"] = result.get("output", "")
                    # Phase 100: manager 节点条件路由——只激活选定分支
                    if nodes[nid].get("type") == "manager" and isinstance(result, dict):
                        selected = result.get("selected_branch")
                        for child in children.get(nid, []):
                            if child == selected:
                                in_degree[child] -= 1
                            else:
                                node_states[child] = "skipped"
                    else:
                        for child in children.get(nid, []):
                            in_degree[child] -= 1
                elif state == "paused":
                    paused_nodes.append(nid)
                elif state == "error":
                    # P0-4.4: 失败节点的下游全部标记为 skipped——
                    # 原代码无 error 分支，失败节点的子节点 in_degree 永不递减 → 永远卡 pending
                    # 对标 Airflow/Prefect/Temporal 的 failure propagation：上游失败 → 下游 skipped
                    # 迭代实现（栈）防递归栈溢出；已处理节点状态变为 skipped 后不会重复入栈
                    stack = list(children.get(nid, []))
                    while stack:
                        child = stack.pop()
                        if node_states.get(child) in (None, "pending"):
                            node_states[child] = "skipped"
                            stack.extend(children.get(child, []))

            # Phase 53: 子工作流节点返回 paused 时，向父工作流传播暂停信号
            if paused_nodes and run_id:
                self.pause(run_id)
                _log.info("Propagating pause from sub-workflow nodes %s to run %s", paused_nodes, run_id)

            if run_id:
                await self._save_snapshot(run_id, context, node_states, node_results)

        any_error = any(s == "error" for s in node_states.values())
        error_nodes = [nid for nid, s in node_states.items() if s == "error"]
        # Phase 100: manager 条件路由未选中的分支标记为 skipped
        skipped_nodes = [nid for nid, s in node_states.items() if s == "skipped"]

        # Phase 33: 节点失败时 LLM 分析原因 + 修复建议（并行 fire-and-forget）
        error_analysis = {}
        if any_error and self._model_config:
            analysis_tasks = []
            for nid in error_nodes:
                analysis_tasks.append(
                    self._analyze_node_failure(nid, nodes.get(nid, {}), node_results.get(nid, {}))
                )
            if analysis_tasks:
                results = await asyncio.gather(*analysis_tasks, return_exceptions=True)
                for nid, r in zip(error_nodes, results):
                    if isinstance(r, str) and r:
                        error_analysis[nid] = r

        # Phase 87 审查 L-1: run 正常结束时清理 _pause_events——若暂停请求在
        # 末批完成后才到达（loop 已 break），原代码不清理导致 entry 泄漏。
        # 暂停分支（L114-121）已自行 _clear_pause_request，这里覆盖完成分支。
        if run_id:
            self._pause_events.pop(run_id, None)

        return {
            "results": node_results,
            "final_output": context.get("last_output", ""),
            "status": "partial_error" if any_error else "completed",
            "error_nodes": error_nodes,
            "skipped_nodes": skipped_nodes,
            "error_analysis": error_analysis if error_analysis else None,
        }

    async def _save_snapshot(
        self, run_id: str, context: dict, node_states: dict, node_results: dict
    ) -> None:
        """保存 checkpoint 快照到 workflow_runs.snapshot

        Phase D W.2: 序列化前触发 pre_compact hooks，允许压缩过大的节点输出。
        """
        snapshot = {
            "context": {k: v for k, v in context.items() if k != "input"},
            "node_states": dict(node_states),
            "node_results": dict(node_results),
        }
        # Phase D W.2: pre_compact hook（可原地修改 snapshot）
        workflow_id = ""
        if self.repo and hasattr(self.repo, "get_run"):
            try:
                run_row = await self.repo.get_run(run_id)
            except Exception:
                run_row = None
            if run_row and isinstance(run_row, dict):
                workflow_id = run_row.get("workflow_id", "")
        try:
            from hooks.registry import run_pre_compact_hooks
            await run_pre_compact_hooks(run_id, workflow_id, snapshot)
        except Exception:
            _log.debug("pre_compact hook failed", exc_info=True)
        if self.repo:
            try:
                await self.repo.update_snapshot(run_id, json.dumps(snapshot))
            except Exception as e:
                _log.error("Failed to save snapshot for run %s: %s", run_id, e)

    async def _analyze_node_failure(self, nid: str, node: dict,
                                      result: dict) -> str | None:
        """LLM 分析节点失败原因 + 修复建议。走默认 Agent Adapter，Yuncat 不调 LLM。"""
        adapter = (self._adapters or {}).get(DEFAULT_AGENT_ID)
        if not adapter:
            return None
        try:
            prompt = node.get("prompt", "")[:WORKFLOW_NODE_PROMPT_PREVIEW_CHARS]
            error = result.get("error", str(result))[:WORKFLOW_NODE_ERROR_PREVIEW_CHARS]
            res = await adapter.invoke(
                f"Node '{nid}' failed.\nPrompt: {prompt}\nError: {error}\n\nSuggest a fix:",
                context={
                    "system": "Analyze the workflow node failure and suggest fix in one sentence.",
                    "max_tokens": MAX_TOKENS_WORKFLOW_NODE,
                    "temperature": 0.2,
                },
            )
            if res.get("error"):
                _log.warning("Workflow node failure analysis adapter error: %s", res["error"])
                return None
            return res.get("content", "").strip()
        except Exception:
            _log.warning("LLM 分析节点失败原因失败", exc_info=True)
        return None

    async def recover_from_snapshot(self, run_id: str) -> dict:
        """从最近快照恢复工作流——优先 snapshot，回退到 event_log"""
        if not self.repo:
            raise ValueError("Repository not configured")
        snapshot_json = await self.repo.get_snapshot(run_id)
        if snapshot_json:
            return json.loads(snapshot_json)
        # v56: 快照缺失时尝试从事件日志恢复
        from .events import recover_from_log
        try:
            return await recover_from_log(self.repo, run_id)
        except Exception:
            raise ValueError(f"No snapshot or event log found for run {run_id}")

    # ============================================================
    # Phase 51-E: Workflow breakpoint resume (pause / resume)
    # ============================================================

    def pause(self, run_id: str) -> None:
        """请求在下一个 batch 边界暂停指定工作流运行。

        P2-5 已知限制（batch-boundary pause）: pause 仅在 while 循环顶部检查（行 124），
        当前 batch 的 ready 节点会执行完才生效。这是 DAG 引擎的经典设计
       （Airflow/Prefect/Cadence 默认行为相同），但 Yuncat 节点可能调用 LLM/subprocess，
        长节点会让 pause 延迟感知。前端 UI 应显示"正在暂停…"状态，告知用户等待。
        对标 Temporal cancellation scope: 要实现立即 pause 需在 executor 内周期性检查
        pause 标志并 raise CancelledError，但会增加 subprocess 清理复杂度，暂不实施。
        """
        event = asyncio.Event()
        event.set()
        self._pause_events[run_id] = event
        _log.info("Pause requested for workflow run %s", run_id)

    def _is_pause_requested(self, run_id: str) -> bool:
        event = self._pause_events.get(run_id)
        return event is not None and event.is_set()

    def _clear_pause_request(self, run_id: str) -> None:
        self._pause_events.pop(run_id, None)

    @staticmethod
    def _pick_resume_node_id(in_degree: dict, node_states: dict) -> str | None:
        """选择暂停后要恢复执行的节点：第一个待执行的就绪节点。"""
        ready = [nid for nid, deg in in_degree.items()
                 if deg == 0 and node_states.get(nid) == "pending"]
        if ready:
            return ready[0]
        pending = [nid for nid, s in node_states.items() if s == "pending"]
        return pending[0] if pending else None

    async def _persist_pause_state(
        self, run_id: str, context: dict,
        node_states: dict, node_results: dict, current_node_id: str | None,
    ) -> None:
        """将暂停状态持久化到 workflow_runs。"""
        if not self.repo:
            return
        snapshot = {
            "context": {k: v for k, v in context.items() if k != "input"},
            "node_states": dict(node_states),
            "node_results": dict(node_results),
        }
        try:
            await self.repo.update_pause_state(
                run_id, json.dumps(context), current_node_id,
                json.dumps(snapshot), "paused",
            )
        except Exception as e:
            _log.error("Failed to persist pause state for run %s: %s", run_id, e)

    async def resume(self, run_id: str) -> dict:
        """加载已持久化的状态并从 current_node_id 继续执行。

        P1-3 修复: 原代码传 "" 作 initial_input，run 行 94 用 "" 覆盖 context["input"]，
        导致恢复后所有节点拿到空 input。_save_snapshot 行 250 显式剥离 input，
        但 _persist_pause_state 行 354 把完整 context（含 input）写入 workflow_runs.context 列。
        所以从 run_row["context"] 读取原始 input。
        对标 Temporal/Cadence workflow 恢复机制：初始 input 必须持久化并可恢复。
        """
        if not self.repo:
            raise ValueError("Repository not configured")
        run_row = await self.repo.get_run(run_id)
        if not run_row:
            raise ValueError(f"Workflow run {run_id} not found")
        workflow_id = run_row.get("workflow_id")
        if not workflow_id:
            raise ValueError(f"Workflow run {run_id} has no workflow_id")
        # P1-3: 从 workflow_runs.context 列恢复原始 input
        original_input = ""
        ctx_json = run_row.get("context")
        if ctx_json:
            try:
                ctx = json.loads(ctx_json)
                original_input = ctx.get("input", "") or ""
            except Exception:
                _log.debug("resume: parse context failed for %s", run_id, exc_info=True)
        snapshot = await self.recover_from_snapshot(run_id)
        return await self.run(
            workflow_id, original_input, run_id=run_id, resume_from_snapshot=snapshot,
        )
