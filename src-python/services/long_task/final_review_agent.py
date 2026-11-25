"""services/long_task/final_review_agent.py —— Phase 83 终审 Agent

3 个审查 stage:
  - pre_execution: plan_step 启动前审查（节点 prompt + 目标对齐）
  - post_execution: plan_step 完成后审查（output vs acceptance_criteria）
  - pre_dangerous_tool: confirmable 工具执行前审查（工具名 + 参数 + 上下文）

冷却窗口: 避免高频审查拖慢执行
  - pre_dangerous_tool: 60s/task（危险工具审查重，per task 共享冷却）
  - pre/post_execution: 30s/plan_step（每步独立冷却）

返回结构:
  {
    "pass": bool, "score": int, "issues": [str],
    "action": "proceed"|"ask_user"|"retry"|"halt",
    "reason": str, "stage": str, "task_id": str, "ts": int, "cached"?: bool,
  }

设计参考:
  - task_supervisor.py:_invoke_lightweight_llm —— 轻量 LLM 调用模式
  - dynamic_orchestrator.py:AdversarialReviewer —— 审查返回结构 + JSON 解析
"""
import json as _json
import logging
import re
import time as _time

from config import calc_context_budget, get_default_model_config, DEFAULT_AGENT_ID

_log = logging.getLogger("yuncat.final_review")

# ── 冷却窗口（秒）──
_COOLDOWN_PRE_DANGEROUS_TOOL = 60  # 危险工具审查重，per task 60s
_COOLDOWN_EXECUTION = 30           # per plan_step 30s


class FinalReviewUnavailable(RuntimeError):
    """终审无法给出结论（adapter 缺失/未就绪/报错/空响应）。

    单独成类型的原因：调用方需要区分「审查员说通过」与「审查员没说话」——
    前者可以放行，后者在 AI 代审档位下必须交回用户（fail-closed）。
    """


class FinalReviewAgent:
    """Phase 83: 终审 Agent——在关键节点审查执行，给出结构化判断

    生命周期：lifespan 创建单例并注入 app.state.final_review_agent。
    被 agent_tool_loop（pre_dangerous_tool）和 orchestrator（pre/post_execution）调用。
    """

    def __init__(self, adapters, model_config):
        self._adapters = adapters
        self._model_config = model_config
        # 冷却窗口：key 格式 "{task_id}:{stage}" 或 "{task_id}:{stage}:{step_id}"
        self._cooldowns: dict[str, float] = {}
        # 冷却期内缓存最近一次审查结果（避免短时间重复调 LLM，复用上次结论）
        self._cache: dict[str, dict] = {}

    async def review(
        self,
        task_id: str,
        stage: str,
        context: dict,
        step_id: str | None = None,
        force: bool = False,
        approve_on_uncertain: bool = True,
    ) -> dict:
        """审查入口——组装 prompt → LLM 调用 → 解析 JSON → 返回结构化结果。

        Args:
            task_id: 长任务 id
            stage: pre_execution / post_execution / pre_dangerous_tool
            context: 审查上下文（goal/step_title/output/tool_name/args 等）
            step_id: plan_step id（pre/post_execution 用，冷却按 step 隔离）
            force: True 跳过冷却窗口（手动触发时用）
            approve_on_uncertain:
                **审查员没给出结论时怎么算**（2026-10-08 新增）。
                - True（默认，非代审档位）：best-effort——审查只是加一道保险，
                  没结论不阻塞执行，审查员只有否决权。
                - False（AI 代审 / 长任务预授权）：审查结论**就是决策**，
                  拿不到结论必须交回用户。实测旧行为对 `rm -rf /` 也因解析失败而 pass。

        Returns:
            {pass, score, issues, action, reason, stage, task_id, ts, cached?}
        """
        if stage not in ("pre_execution", "post_execution", "pre_dangerous_tool"):
            return self._uncertain(stage, task_id, "unknown stage", approve_on_uncertain)

        cooldown_key = self._cooldown_key(task_id, stage, step_id)
        cached = self._cached_or_none(cooldown_key, stage, force)
        if cached is not None:
            return cached

        result = await self._run_review(stage, task_id, context, approve_on_uncertain)
        if result is not None:
            self._cooldowns[cooldown_key] = _time.monotonic()
            self._cache[cooldown_key] = result
            return result
        return self._uncertain(
            stage, task_id,
            "审查员没给出可识别的结论（输出无法解析，或缺少/写错 action 字段）",
            approve_on_uncertain)

    def _cached_or_none(self, cooldown_key: str, stage: str, force: bool) -> dict | None:
        """冷却期内的缓存命中（含"冷却中但无缓存"→按不确定处理）。"""
        if force or not self._in_cooldown(cooldown_key, stage):
            return None
        cached = self._cache.get(cooldown_key)
        return {**cached, "cached": True} if cached else None

    async def _run_review(self, stage: str, task_id: str, context: dict,
                          approve_on_uncertain: bool) -> dict | None:
        """真正跑一次审查：渲染 prompt → 调 LLM → 解析。不可用/解析失败返回 None。"""
        from prompts.spec_prompts import render_final_review_prompt

        prompt = render_final_review_prompt(
            stage, self._format_task_context(context),
            self._format_review_input(stage, context),
        )
        try:
            raw = await self._invoke_llm(prompt)
        except FinalReviewUnavailable as e:
            _log.warning("final_review unavailable: %s", e)
            return self._uncertain(
                stage, task_id, f"reviewer unavailable: {e}", approve_on_uncertain)
        return self._parse_result(raw, stage, task_id, approve_on_uncertain)

    # ── 冷却窗口 ──

    def _cooldown_key(self, task_id: str, stage: str, step_id: str | None) -> str:
        """pre_dangerous_tool per task；pre/post_execution per step"""
        if stage == "pre_dangerous_tool":
            return f"{task_id}:{stage}"
        return f"{task_id}:{stage}:{step_id or 'none'}"

    def _in_cooldown(self, key: str, stage: str) -> bool:
        last = self._cooldowns.get(key)
        if last is None:
            return False
        cooldown = (_COOLDOWN_PRE_DANGEROUS_TOOL
                    if stage == "pre_dangerous_tool" else _COOLDOWN_EXECUTION)
        return (_time.monotonic() - last) < cooldown

    # ── Phase 87 审查 C3: 任务结束清理 per-task 缓存（避免内存泄漏）──

    def cleanup_task(self, task_id: str) -> None:
        """任务结束时清理该 task 的冷却/缓存条目

        key 格式 "{task_id}:{stage}[:{step_id}]"——按前缀匹配删除。
        """
        prefix = f"{task_id}:"
        for key in list(self._cooldowns.keys()):
            if key.startswith(prefix):
                del self._cooldowns[key]
        for key in list(self._cache.keys()):
            if key.startswith(prefix):
                del self._cache[key]

    # ── Prompt 组装 ──

    def _format_task_context(self, context: dict) -> str:
        lines = []
        if context.get("goal"):
            lines.append(f"确认目标: {context['goal']}")
        if context.get("step_title"):
            lines.append(f"当前步骤: {context['step_title']}")
        if context.get("step_description"):
            lines.append(f"步骤描述: {context['step_description']}")
        criteria = context.get("acceptance_criteria")
        if criteria:
            lines.append(f"验收标准: {criteria}")
        return "\n".join(lines) if lines else "（无任务上下文）"

    def _format_review_input(self, stage: str, context: dict) -> str:
        # P2-2.2: 截断长度从当前默认模型上下文窗口动态派生（原硬编码 500/2000/2000）
        _ctx = get_default_model_config()
        _exec_ctx_chars = calc_context_budget(_ctx, 0.002, min_chars=300, max_chars=800)
        _output_chars = calc_context_budget(_ctx, 0.01, min_chars=800, max_chars=3000)
        if stage == "pre_dangerous_tool":
            args = _json.dumps(context.get("args", {}), ensure_ascii=False, indent=2)
            return (f"工具名: {context.get('tool_name', '?')}\n"
                    f"参数: {args}\n"
                    f"执行上下文: {str(context.get('exec_context', ''))[:_exec_ctx_chars]}")
        if stage == "post_execution":
            return f"步骤产出:\n{str(context.get('output', ''))[:_output_chars]}"
        # pre_execution
        return f"步骤 prompt:\n{str(context.get('step_prompt', ''))[:_output_chars]}"

    # ── LLM 调用（参考 task_supervisor._invoke_lightweight_llm）──

    async def _invoke_llm(self, prompt: str) -> str:
        """轻量 LLM 调用——单次 invoke（非 ReAct loop）。

        2026-10-08 修复：此前 adapter 缺失/未就绪/返回错误 dict 时都吞成空字符串，
        上层再 fail-open 放行。实测 `adapter.invoke()` 在未配置模型时返回
        `{"error": "未配置模型..."}`，`result.get("content", "")` 取到空串，
        于是**任何一次审查失败都变成"通过"**——包括对 `rm -rf /` 这种调用。

        现在把所有失败原因**显式抛出**，由 review() 统一按审批模式决定是
        fail-closed（AI 代审时交回用户）还是放行（非代审时只是不阻塞）。
        """
        adapter = self._adapters.get(DEFAULT_AGENT_ID)
        if adapter is None:
            raise FinalReviewUnavailable(f"{DEFAULT_AGENT_ID} adapter missing")
        if not getattr(adapter, "_ready", None) or not adapter._ready.is_set():
            raise FinalReviewUnavailable(f"{DEFAULT_AGENT_ID} adapter not ready")
        try:
            result = await adapter.invoke(prompt)
        except Exception as e:
            raise FinalReviewUnavailable(f"adapter.invoke failed: {e}") from e
        if isinstance(result, dict):
            if result.get("error"):
                raise FinalReviewUnavailable(str(result["error"])[:200])
            text = result.get("content", "")
        else:
            text = result or ""
        if not str(text).strip():
            raise FinalReviewUnavailable("empty LLM response")
        return str(text)

    # ── 结果解析 ──

    def _parse_result(self, raw: str, stage: str, task_id: str,
                      approve_on_uncertain: bool = True) -> dict | None:
        """解析 LLM 输出为结构化结果。

        2026-10-22：AI 代审时「审查结论就是决策」，**只有模型明确写出
        action=proceed 才算放行**。此前 action 缺失或写成非法值时会被推断成
        proceed（"pass=true 就当同意"），等于审查员没表态我们替他点头——
        和 2026-10-08 修的"解析失败默认通过"是同一类 fail-open，只是深了一层。
        非代审档位（审查只是加一道保险）保持原推断语义，不改变既有行为。
        """
        parsed = _extract_json(raw)
        if not isinstance(parsed, dict):
            # 解析失败不再自己决定放行——由 review() 按审批模式决定
            # （AI 代审 → 交回用户；非代审 → 不阻塞）。原实现无条件 pass 属危险 fail-open。
            _log.warning("final_review: failed to parse LLM output")
            return None
        action = str(parsed.get("action") or "").strip().lower()
        if action not in ("proceed", "ask_user", "retry", "halt"):
            if not approve_on_uncertain:
                # 代审档：模型没给出可识别的结论 → 交回用户，绝不代它点头
                _log.warning(
                    "final_review: action %r 无法识别（代审档，按不确定处理）",
                    parsed.get("action"),
                )
                return None
            action = "proceed" if parsed.get("pass", True) else "ask_user"
        issues = parsed.get("issues", [])
        if not isinstance(issues, list):
            issues = [str(issues)]
        # score 容错——LLM 可能返回字符串/null/浮点，转 int 失败则默认 80
        try:
            score = int(parsed.get("score", 80))
        except (TypeError, ValueError):
            score = 80
        return {
            "pass": bool(parsed.get("pass", True)),
            "score": score,
            "issues": [str(i) for i in issues],
            "action": action,
            "reason": str(parsed.get("reason", "")),
            "stage": stage,
            "task_id": task_id,
            "ts": int(_time.time()),
        }

    def _pass(self, stage: str, task_id: str, reason: str) -> dict:
        """默认通过——审查失败时不阻塞执行（best-effort）"""
        return {
            "pass": True, "score": 80, "issues": [],
            "action": "proceed", "reason": reason,
            "stage": stage, "task_id": task_id, "ts": int(_time.time()),
        }

    def _uncertain(self, stage: str, task_id: str, reason: str,
                   approve_on_uncertain: bool) -> dict:
        """审查员没给出结论时的结论。

        - approve_on_uncertain=True（默认，审查只是加道保险）：放行，不阻塞执行。
        - False（AI 代审 / 长任务预授权，审查结论即决策）：交回用户
          （action=ask_user），绝不默认放行。
        """
        return {
            "pass": bool(approve_on_uncertain),
            "score": 0,
            "issues": [reason],
            "action": "proceed" if approve_on_uncertain else "ask_user",
            "reason": reason,
            "stage": stage, "task_id": task_id, "ts": int(_time.time()),
            "reviewer_unavailable": True,
        }


# ── JSON 提取（复用 dynamic_orchestrator._extract_json 模式）──

def _extract_json(text: str) -> dict | None:
    """从 LLM 输出提取 JSON——支持 ```json 代码块、裸 JSON、{ ... } 块"""
    if not text:
        return None
    m = re.search(r'```(?:json)?\s*\n(.*?)```', text, re.DOTALL)
    if m:
        try:
            return _json.loads(m.group(1))
        except _json.JSONDecodeError:
            pass
    try:
        return _json.loads(text)
    except _json.JSONDecodeError:
        pass
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return _json.loads(m.group(0))
        except _json.JSONDecodeError:
            pass
    return None


# ── 全局单例（对齐 long_task 家族模式）──

_agent: FinalReviewAgent | None = None


def get_final_review_agent() -> FinalReviewAgent | None:
    """路由层/agent_tool_loop 用——获取全局 FinalReviewAgent 单例"""
    return _agent


def set_final_review_agent(agent: FinalReviewAgent) -> None:
    """lifespan 用——注入全局 FinalReviewAgent 单例"""
    global _agent
    _agent = agent
