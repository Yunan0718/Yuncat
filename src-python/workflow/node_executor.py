"""workflow/node_executor.py —— DAG 节点执行器（Phase 20: +worktree +model）
Phase 33: 节点间上下文传递 — 后续节点可看到前面所有节点的输出摘要。
Phase 100: +manager（条件路由）+merge（汇总）+summary（摘要）节点执行器。
"""
import json
import logging
import re
import time as _time
import uuid as _uuid
from typing import Any, Protocol

from config import (
    DEFAULT_AGENT_ID,
    SUB_AGENT_OUTPUT_PREVIEW_CHARS,
    SUB_AGENT_PROMPT_PREVIEW_CHARS,
    SUB_AGENT_PLAN_STEP_OUTPUT_PREVIEW_CHARS,
    WORKFLOW_NODE_SUMMARY_MAX_CHARS,
)

_log = logging.getLogger("yuncat.dag.executor")


class NodeExecutor(Protocol):
    """工作流节点执行器协议"""
    async def execute(self, node: dict, context: dict[str, Any]) -> dict[str, Any]: ...


class AgentNodeExecutor:
    """调用 AgentService 执行节点的默认执行器。

    Phase 20:
      - 节点 `model` 字段: 指定模型（如 deepseek-v4-flash/pro），覆盖全局配置
      - 节点 `worktree: true`: 自动创建 git worktree 隔离环境
    Phase 33:
      - 上下文注入: `node_results` 中前面已执行节点的输出摘要
      - `agent_id` 支持: 节点可指定用哪个 Agent 执行
    """

    def __init__(self, agent_service=None, worktree_service=None):
        self.agent_service = agent_service
        self._worktree = worktree_service

    async def execute(self, node: dict, context: dict[str, Any]) -> dict[str, Any]:
        """执行 Agent 节点——通过 AgentService 调用 Agent。
        自动注入前面已执行节点的输出作为上下文。

        Phase 43 F3.3: OC 工具节点映射 (oc_vision→openclaw_vision, oc_search→web_search, oc_messaging→telegram_send)
        """
        node_type = node.get("type", "default")
        prompt = node.get("prompt", context.get("input", ""))
        agent_id = node.get("agent_id", DEFAULT_AGENT_ID)

        # Phase 43 F3.3: OC 工具节点直接执行工具
        _OC_TOOL_MAP = {
            "oc_vision": ("openclaw", "openclaw_vision"),
            "oc_search": ("openclaw", "web_search"),
            "oc_messaging": ("openclaw", "telegram_send"),
        }
        if node_type in _OC_TOOL_MAP:
            oc_agent, tool_name = _OC_TOOL_MAP[node_type]
            if self.agent_service and hasattr(self.agent_service, "adapters"):
                oc = self.agent_service.adapters.get(oc_agent)
                if oc and hasattr(oc, "execute_tool"):
                    try:
                        result = await oc.execute_tool(tool_name, {"prompt": prompt} if tool_name != "telegram_send" else {"chat_id": "", "text": prompt})
                        import json as _json
                        return {"output": _json.dumps(result, ensure_ascii=False)[:SUB_AGENT_OUTPUT_PREVIEW_CHARS], "agent_id": oc_agent, "tool": tool_name}
                    except Exception as e:
                        return {"output": f"[OC工具错误] {e}", "agent_id": oc_agent}
            return {"output": "[OC不可用]", "agent_id": oc_agent}

        # Phase 33: 注入前面已执行节点的输出摘要
        node_results = context.get("node_results", {})
        prev_outputs = self._build_prev_outputs_context(node_results, node.get("id", ""))
        if prev_outputs:
            prompt = prev_outputs + "\n\n---\n## 当前任务\n" + prompt

        # Phase 20: 节点级模型选择 — model_override 通过 ctx 传递给 AgentService.invoke()
        # invoke() → _run_agent() → _get_model_config() 读取 ctx.get("model_override")
        node_model = node.get("model")
        if node_model and self.agent_service:
            ctx = context.copy()
            ctx["model_override"] = node_model
        else:
            ctx = context

        # Phase 20: worktree 隔离
        wt_info = None
        if node.get("worktree") and self._worktree:
            import uuid as _uuid
            node_tid = node.get("id", str(_uuid.uuid4()))
            try:
                wt_info = await self._worktree.create_worktree(node_tid)
                if wt_info:
                    ctx["cwd"] = wt_info.path
                    _log.info("Worktree created for node %s: %s", node.get("id"), wt_info.path)
            except Exception:
                _log.warning("Failed to create worktree for node %s", node.get("id"))

        try:
            if self.agent_service:
                result = await self.agent_service.invoke(
                    agent_id=agent_id, prompt=prompt, context=ctx,
                )
                return {"output": str(result), "agent_id": agent_id,
                        "worktree": wt_info.branch if wt_info else None}
        finally:
            if wt_info:
                try:
                    await self._worktree.merge_worktree(
                        node.get("id", "unknown"), strategy="squash",
                    )
                except Exception:
                    _log.debug("合并 worktree 失败", exc_info=True)

        return {"output": f"[stub] {agent_id}: {prompt[:SUB_AGENT_PROMPT_PREVIEW_CHARS]}", "agent_id": agent_id}

    @staticmethod
    def _build_prev_outputs_context(node_results: dict, self_id: str) -> str:
        """构建前面已执行节点的输出摘要（排除自己）。
        每个节点最多 300 字摘要。"""
        parts = []
        for nid, result in node_results.items():
            if nid == self_id:
                continue
            output = result.get("output", "") if isinstance(result, dict) else str(result)
            if output:
                summary = output[:WORKFLOW_NODE_SUMMARY_MAX_CHARS] + ("..." if len(output) > WORKFLOW_NODE_SUMMARY_MAX_CHARS else "")
                parts.append(f"## 步骤: {nid}\n{summary}")
        if parts:
            return "## 前面已完成的步骤\n" + "\n\n".join(parts)
        return ""


class HumanNodeExecutor:
    """HITL 审批节点执行器——发送 WS 通知并等待用户决定。

    Phase 33: 通过 agent_service 的 push_hitl_request 发送审批到前端，
    等待用户 approve/reject。超时默认拒绝。
    """

    def __init__(self, agent_service=None, timeout_sec: int = 3600):
        self.agent_service = agent_service
        self.timeout = timeout_sec
        self._pending: dict[tuple, dict] = {}  # Phase 39: (run_id, node_id) → {event, result}

    async def execute(self, node: dict, context: dict[str, Any]) -> dict[str, Any]:
        """执行 HITL 节点——推送审批请求到前端并等待。

        context["node_results"] 中已有前面节点的输出，可供用户参考。
        """
        prompt = node.get("prompt", "请审核前面的执行结果")
        node_id = node.get("id", "unknown")
        run_id = context.get("run_id", "")

        # Phase 33: 注入前面节点的输出供用户审查
        node_results = context.get("node_results", {})
        review_context = ""
        for nid, result in node_results.items():
            output = result.get("output", "") if isinstance(result, dict) else str(result)
            if output:
                review_context += f"\n### {nid}\n{output[:SUB_AGENT_PLAN_STEP_OUTPUT_PREVIEW_CHARS]}\n"

        full_prompt = f"{prompt}\n\n## 需审核的内容\n{review_context}" if review_context else prompt

        if self.agent_service:
            try:
                import asyncio
                approval = await asyncio.wait_for(
                    self._request_approval(run_id, node_id, full_prompt),
                    timeout=self.timeout,
                )
                if approval:
                    return {"output": "approved", "node_id": node_id, "status": "approved"}
                else:
                    return {"output": "rejected by user", "node_id": node_id, "status": "rejected",
                            "error": "用户拒绝了审批请求"}
            except asyncio.TimeoutError:
                return {"output": "approval timeout", "node_id": node_id, "status": "timeout",
                        "error": "审批超时，自动拒绝"}

        return {"output": "HITL stub — no agent_service", "node_id": node_id, "status": "stub"}

    async def _request_approval(self, run_id: str, node_id: str, prompt: str) -> bool:
        """Phase 39: 真双向 HITL — 发送 WS 审批通知到前端，用 asyncio.Event 等待用户响应。"""
        import asyncio
        if not self.agent_service:
            return True  # 无审批通道时默认通过

        try:
            await self.agent_service.push_hitl_request(
                run_id, node_id, prompt, self.timeout,
            )
        except Exception:
            return True

        # 真等待——前端确认后才返回
        event = asyncio.Event()
        key = (run_id, node_id)
        self._pending[key] = {"event": event, "result": None}
        try:
            await asyncio.wait_for(event.wait(), timeout=self.timeout)
            result = self._pending.get(key, {}).get("result",
                {"approved": False, "feedback": "审批超时"})
            return result.get("approved", False)
        except asyncio.TimeoutError:
            return False  # 超时 → 拒绝
        finally:
            # Phase 87 审查 M-2: 无条件清理 _pending，防止 CancelledError 或其他
            # 非 TimeoutError 异常导致 entry 泄漏（原代码仅 TimeoutError 路径 pop）
            self._pending.pop(key, None)

    def resolve_approval(self, run_id: str, node_id: str, approved: bool, feedback: str = "") -> None:
        """前端 WS 调用此方法完成审批"""
        key = (run_id, node_id)
        entry = self._pending.get(key)
        if entry:
            entry["result"] = {"approved": approved, "feedback": feedback}
            entry["event"].set()


class MockNodeExecutor(NodeExecutor):
    """测试用 Mock 执行器——按 node 类型返回预定义输出"""

    def __init__(self, responses: dict[str, dict] | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, node: dict, context: dict[str, Any]) -> dict[str, Any]:
        node_id = node.get("id", "unknown")
        self.calls.append((node_id, dict(context)))
        response = self.responses.get(
            node_id,
            {"output": f"node {node_id} executed", "node_id": node_id},
        )
        if "error" in response:
            raise RuntimeError(response["error"])
        return response


class SubWorkflowExecutor:
    """Phase 53: 子工作流节点执行器。

    节点配置：
      - type: "sub_workflow"
      - sub_workflow_id: 被调用子工作流的 workflow_id
      - prompt（可选）: 子工作流 initial_input，缺省使用父上下文 input

    子工作流使用确定性 run_id = "{parent_run_id}:{node_id}"，保证父级恢复时
    能找到同一子运行并继续。
    """

    def __init__(self, workflow_service):
        self.workflow_service = workflow_service

    async def execute(self, node: dict, context: dict[str, Any]) -> dict[str, Any]:
        child_wf_id = node.get("sub_workflow_id") or node.get("workflow_id")
        if not child_wf_id:
            return {"error": "Missing sub_workflow_id", "status": "error"}

        node_id = node.get("id", "unknown")
        parent_run_id = context.get("run_id", "")
        child_run_id = f"{parent_run_id}:{node_id}" if parent_run_id else str(_uuid.uuid4())
        child_input = node.get("prompt") if node.get("prompt") is not None else context.get("input", "")

        engine = getattr(self.workflow_service, "engine", None) if self.workflow_service else None
        if not engine:
            return {"error": "Workflow engine not available", "status": "error"}

        node_results = context.get("node_results", {})
        prev_result = node_results.get(node_id)

        try:
            if isinstance(prev_result, dict) and prev_result.get("status") == "paused":
                # 父级恢复：从子运行的快照继续
                snapshot = await engine.recover_from_snapshot(child_run_id)
                child_result = await engine.run(
                    child_wf_id, "", run_id=child_run_id,
                    resume_from_snapshot=snapshot,
                )
            else:
                await self.workflow_service.insert_child_run(
                    child_run_id, child_wf_id, parent_run_id, node_id,
                )
                child_result = await engine.run(
                    child_wf_id, child_input, run_id=child_run_id,
                )

            await self._finish_child_run(child_run_id, child_result)
            return {
                "output": child_result.get("final_output", ""),
                "child_run_id": child_run_id,
                "status": child_result.get("status", "completed"),
            }
        except Exception as exc:
            _log.exception("Sub-workflow node %s failed", node_id)
            return {"error": str(exc), "child_run_id": child_run_id, "status": "error"}

    async def _finish_child_run(self, run_id: str, result: dict) -> None:
        if not self.workflow_service:
            return
        status = result.get("status", "completed")
        error = result.get("error", "")
        try:
            await self.workflow_service.db.execute(
                "UPDATE workflow_runs SET status=?, finished_at=?, error_message=? WHERE id=?",
                (status, int(_time.time()), error or None, run_id),
            )
            await self.workflow_service.db.commit()
        except Exception:
            _log.debug("Failed to finish child run %s", run_id, exc_info=True)


# ============================================================
# Phase 43 G2.2: tool_execute 节点执行器
# ============================================================

class ToolExecuteNodeExecutor:
    """直接调用 Yuncat 工具的节点执行器——不走 Agent 循环。

    节点配置:
      - type: "tool_execute"
      - tool_name: 工具名
      - arguments: 工具参数字典（支持 {{node_id.output}} 占位符替换）
      - agent_id (可选): 执行上下文 agent_id
    """

    def __init__(self, tool_service=None):
        self.tool_service = tool_service

    async def execute(self, node: dict, context: dict[str, Any]) -> dict[str, Any]:
        tool_name = node.get("tool_name") or node.get("tool")
        if not tool_name:
            return {"error": "Missing tool_name", "status": "error"}

        raw_args = node.get("arguments") or node.get("args") or {}
        node_results = context.get("node_results", {})
        arguments = self._resolve_arguments(raw_args, node_results)

        agent_id = node.get("agent_id", context.get("agent_id", DEFAULT_AGENT_ID))
        task_id = context.get("run_id", "")

        if not self.tool_service:
            return {"error": "ToolService not available", "status": "error"}

        try:
            result = await self.tool_service.execute(
                tool_name=tool_name,
                arguments=arguments,
                agent_id=agent_id,
                task_id=task_id,
                mode="workflow",
            )
            output = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            return {
                "output": output[:SUB_AGENT_OUTPUT_PREVIEW_CHARS],
                "tool_name": tool_name,
                "agent_id": agent_id,
                "status": "success",
            }
        except Exception as exc:
            _log.exception("tool_execute node failed: %s", tool_name)
            return {"error": str(exc), "tool_name": tool_name, "status": "error"}

    @staticmethod
    def _resolve_arguments(args: Any, node_results: dict) -> dict:
        """解析参数中的 {{node_id.field}} 占位符。"""
        if not isinstance(args, dict):
            return {}

        def _resolve_value(value: Any) -> Any:
            if isinstance(value, str):
                def _replace_placeholder(m: re.Match) -> str:
                    ref = m.group(1).strip()
                    parts = ref.split(".", 1)
                    if len(parts) == 2:
                        nid, field = parts
                        result = node_results.get(nid, {})
                        if isinstance(result, dict):
                            return str(result.get(field, ""))
                    return ""
                return re.sub(r"\{\{([^}]+)\}\}", _replace_placeholder, value)
            if isinstance(value, dict):
                return {k: _resolve_value(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_resolve_value(v) for v in value]
            return value

        return {k: _resolve_value(v) for k, v in args.items()}


# ============================================================
# Phase 43 H6: HA 节点类型——Hermes Agent 子进程原生 ReAct
# ============================================================

class HANativeNodeExecutor:
    """Hermes Agent 子进程原生节点执行器。

    Phase 43 H6: 支持 ``type: "ha_native"`` 节点——直接调用 Hermes Agent
    子进程的 OpenAI 兼容 API（``http://127.0.0.1:{ha_port}/v1/chat/completions``），
    利用 HA 的原生 ReAct 循环 + 47+ 内置工具，不走 Yuncat 自己的 Agent 循环。

    节点配置:
      - type: "ha_native"
      - prompt: 任务提示（支持 {{node_id.output}} 占位符）
      - model (可选): 模型 id，覆盖默认
      - max_tokens (可选): 单次生成上限
      - temperature (可选): 采样温度
      - tools (可选): 启用的工具列表（HA 原生工具名），不指定则启用全部

    行业标准对标：VSCode Language Server / Jupyter Kernel——子进程提供原生能力，
    宿主仅做协议适配与结果聚合。当 HA 不可用时降级到 ``hermes`` Adapter。
    """

    def __init__(self, agent_service=None):
        self.agent_service = agent_service

    async def execute(self, node: dict, context: dict[str, Any]) -> dict[str, Any]:
        prompt = node.get("prompt", context.get("input", ""))
        node_id = node.get("id", "ha_node")

        # 注入前面节点输出（与 AgentNodeExecutor 行为一致）
        node_results = context.get("node_results", {})
        prev_outputs = AgentNodeExecutor._build_prev_outputs_context(node_results, node_id)
        if prev_outputs:
            prompt = prev_outputs + "\n\n---\n## 当前任务\n" + prompt

        # 优先走 AgentService.invoke(agent_id="hermes")——其内部已自动路由到 HA 子进程
        # 当 HA 不可用时 hermes.py 会自动 fallback 到 Provider API（透明降级）
        if not self.agent_service:
            return {"output": "[HA] AgentService not available", "node_id": node_id, "status": "error"}

        ctx = context.copy()
        # 节点级 model / max_tokens / temperature 覆盖
        node_model = node.get("model")
        if node_model:
            ctx["model_override"] = node_model
        if "max_tokens" in node:
            ctx["max_tokens"] = node["max_tokens"]
        if "temperature" in node:
            ctx["temperature"] = node["temperature"]
        # 工具白名单通过 context 传递给 hermes.py
        if node.get("tools"):
            ctx["ha_tools_filter"] = node["tools"]
        # 标记这是 HA 原生节点（供 hermes.py 优化路径选择）
        ctx["ha_native"] = True
        # P1: 不硬编码 agent_id="hermes"——读取节点配置，缺省回退 DEFAULT_AGENT_ID
        # 对齐其他执行器 (line 51/340/676) 的 node.get("agent_id", DEFAULT_AGENT_ID) 模式
        ha_agent_id = node.get("agent_id", DEFAULT_AGENT_ID)

        try:
            result = await self.agent_service.invoke(
                agent_id=ha_agent_id, prompt=prompt, context=ctx,
            )
            output = str(result) if result is not None else ""
            return {
                "output": output[:SUB_AGENT_OUTPUT_PREVIEW_CHARS],
                "agent_id": ha_agent_id,
                "node_type": "ha_native",
                "status": "success",
            }
        except Exception as exc:
            _log.exception("ha_native node failed: %s", node_id)
            return {
                "output": f"[HA error] {exc}",
                "node_id": node_id,
                "node_type": "ha_native",
                "status": "error",
                "error": str(exc),
            }


# ============================================================
# Phase 100: manager / merge / summary 节点执行器
# ============================================================

# 条件 DSL 支持的运算符（顺序重要——长运算符优先匹配避免前缀歧义）
_CONDITION_OPS = [
    (" contains ", "contains"),
    (" starts_with ", "starts_with"),
    (" ends_with ", "ends_with"),
    (" matches ", "matches"),
    (" == ", "=="),
    (" != ", "!="),
]


def _eval_condition(condition: str, node_results: dict) -> bool:
    """Phase 100: 条件 DSL 求值。

    语法: ``{{node_id.field}} <op> 'expected'``

    - 占位符 ``{{node_id.output}}`` 替换为 node_results 中对应节点的字段值
    - 支持运算符: contains / starts_with / ends_with / matches / == / !=
    - ``matches`` 使用 re.search（部分匹配）
    - 无运算符时对展开后的字符串做 truthy 检查

    Examples:
        >>> _eval_condition("{{A.output}} contains 'TODO'", {"A": {"output": "TODO fix"}})
        True
        >>> _eval_condition("{{A.output}} == 'ok'", {"A": {"output": "ok"}})
        True
    """
    def _replace_placeholder(m: re.Match) -> str:
        ref = m.group(1).strip()
        parts = ref.split(".", 1)
        if len(parts) == 2:
            nid, field = parts
            result = node_results.get(nid, {})
            if isinstance(result, dict):
                return str(result.get(field, ""))
        return ""

    expanded = re.sub(r"\{\{([^}]+)\}\}", _replace_placeholder, condition)

    for sep, op_name in _CONDITION_OPS:
        if sep in expanded:
            idx = expanded.index(sep)
            left = expanded[:idx].strip().strip("'\"")
            right = expanded[idx + len(sep):].strip().strip("'\"")
            if op_name == "contains":
                return right in left
            if op_name == "starts_with":
                return left.startswith(right)
            if op_name == "ends_with":
                return left.endswith(right)
            if op_name == "matches":
                try:
                    return re.search(right, left) is not None
                except re.error:
                    return False
            if op_name == "==":
                return left == right
            if op_name == "!=":
                return left != right
    # 无运算符——truthy 检查（空字符串/None → False）
    return bool(expanded.strip())


class ManagerNodeExecutor:
    """Phase 100: 条件路由节点——根据上游输出或自定义条件选择分支。

    节点配置::

        {
            "type": "manager",
            "conditions": [
                {"when": "{{A.output}} contains 'TODO'", "goto": "branch_b"},
                {"when": "default", "goto": "branch_c"}
            ]
        }

    引擎在 manager 完成后只激活 ``selected_branch`` 对应的下游边，
    非选定分支的子节点被标记为 ``skipped``。

    返回: ``{"output": ..., "selected_branch": "node_id", "condition_matched": ...}``
    """

    async def execute(self, node: dict, context: dict[str, Any]) -> dict[str, Any]:
        conditions = node.get("conditions", [])
        node_results = context.get("node_results", {})

        for cond in conditions:
            when = cond.get("when", "default")
            if when == "default" or _eval_condition(when, node_results):
                goto = cond.get("goto")
                return {
                    "output": f"Routed to {goto}",
                    "selected_branch": goto,
                    "condition_matched": when,
                }

        return {
            "output": "No branch selected",
            "selected_branch": None,
            "error": "No condition matched and no default provided",
            "status": "error",
        }


class MergeNodeExecutor:
    """Phase 100: 汇总节点——等待多个上游分支完成后合并 output。

    引擎的 in_degree 机制天然保证 merge 节点在所有上游完成后才执行。
    本执行器读取 ``inputs`` 列表对应节点的输出并合并。

    节点配置::

        {
            "type": "merge",
            "inputs": ["node_a", "node_b"],
            "strategy": "concat",        // 或 "json"
            "separator": "\\n\\n---\\n\\n"
        }

    ``inputs`` 缺省时合并所有已完成节点（排除自身）。
    """

    async def execute(self, node: dict, context: dict[str, Any]) -> dict[str, Any]:
        node_results = context.get("node_results", {})
        inputs = node.get("inputs", [])
        if not inputs:
            self_id = node.get("id", "")
            inputs = [nid for nid in node_results if nid != self_id]

        strategy = node.get("strategy", "concat")
        separator = node.get("separator", "\n\n---\n\n")

        parts: list[str] = []
        for nid in inputs:
            result = node_results.get(nid, {})
            output = result.get("output", "") if isinstance(result, dict) else str(result)
            if output:
                parts.append(f"## {nid}\n{output}")

        if strategy == "json":
            merged = json.dumps(
                {nid: (node_results.get(nid, {}).get("output", "")
                       if isinstance(node_results.get(nid), dict)
                       else str(node_results.get(nid, "")))
                 for nid in inputs},
                ensure_ascii=False,
            )
        else:
            merged = separator.join(parts)

        return {
            "output": merged,
            "merged_from": inputs,
            "merge_strategy": strategy,
        }


class SummaryNodeExecutor:
    """Phase 100: 摘要节点——调用模型对上游结果生成摘要，写入 knowledge_nodes。

    节点配置::

        {
            "type": "summary",
            "inputs": ["node_a", "node_b"],   // 可选，缺省所有已完成节点
            "title": "摘要标题",
            "prompt": "自定义摘要指令",          // 可选
            "persist": true                     // 缺省 true，写入 knowledge_nodes
        }

    需要: agent_service（调模型）+ db（写 knowledge_nodes）。
    source='workflow_summary'，与 workflow_run 的自动沉淀区分。
    """

    def __init__(self, agent_service=None, db=None):
        self.agent_service = agent_service
        self.db = db

    async def execute(self, node: dict, context: dict[str, Any]) -> dict[str, Any]:
        node_results = context.get("node_results", {})
        inputs = node.get("inputs", [])
        if not inputs:
            self_id = node.get("id", "")
            inputs = [nid for nid in node_results if nid != self_id]

        # 收集上游输出
        parts: list[str] = []
        for nid in inputs:
            result = node_results.get(nid, {})
            output = result.get("output", "") if isinstance(result, dict) else str(result)
            if output:
                parts.append(f"### {nid}\n{output}")
        content = "\n\n".join(parts)

        title = node.get("title", f"工作流摘要 {node.get('id', '')}")
        custom_prompt = node.get(
            "prompt", "请对以下内容生成简洁摘要，突出关键信息和结论：",
        )

        # 调用模型生成摘要
        summary_text = ""
        if self.agent_service:
            try:
                agent_id = node.get("agent_id", DEFAULT_AGENT_ID)
                result = await self.agent_service.invoke(
                    agent_id=agent_id,
                    prompt=f"{custom_prompt}\n\n{content}",
                    context=context,
                )
                summary_text = str(result)
            except Exception as e:
                _log.warning("Summary node model call failed: %s", e)
                summary_text = content[:500]
        else:
            summary_text = content[:500]

        # 写入 knowledge_nodes
        persist = node.get("persist", True)
        persisted = False
        if persist and self.db:
            try:
                await self.db.execute(
                    "INSERT OR REPLACE INTO knowledge_nodes("
                    "id, title, content, source, score, tags, created_at, updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    (str(_uuid.uuid4()), title, summary_text[:2000],
                     "workflow_summary", 0.8, "[]",
                     int(_time.time()), int(_time.time())),
                )
                await self.db.commit()
                persisted = True
            except Exception:
                _log.warning("Failed to write summary to knowledge_nodes", exc_info=True)

        return {
            "output": summary_text,
            "summary_title": title,
            "summarized_from": inputs,
            "persisted": persisted,
        }
