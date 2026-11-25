"""services/long_task/review_hooks.py — 长任务审查 Hook 模块

6b-3 修复：从 orchestrator.py 提取 9 个 review hook 方法——
  - FinalReview 终审（pre/post_execution + 写 plan_steps.review.final_reviews）
  - 对抗验证（adversarial_challenge + 计数 + 写 adversarial_reviews）
  - 标准查询（standards_lookup_hook + domain 推断 + 写 standards_lookups）

采用委托模式：模块级函数接收 self 作为首参（对齐 agent_tool_loop.py 模式），
orchestrator.py 保留薄方法包装以保持调用方不变。

设计原则：
  - 所有 hook 均 best-effort——失败/缺单例返回 None，不阻塞主流程
  - 写入 plan_steps.review 时与现有 final_reviews/adversarial_reviews/standards_lookups
    平行存储，便于前端审查 Tab 分别展示
"""
import json as _json
import logging
import time as _time

_log = logging.getLogger("yuncat.orchestrator")


# ═══════════════════════════════════════════════════════════
# Phase 83: FinalReview 终审接入
# ═══════════════════════════════════════════════════════════

async def _final_review_pre(
    self, task_id: str, step: dict, prompt: str, goal: str,
) -> dict | None:
    """Phase 83: pre_execution 终审——best-effort，失败/缺单例返回 None"""
    try:
        from services.long_task.final_review_agent import get_final_review_agent
        agent = get_final_review_agent()
        if agent is None:
            return None
        return await agent.review(
            task_id=task_id,
            stage="pre_execution",
            step_id=step.get("id"),
            context={
                "goal": goal,
                "step_title": step.get("title", ""),
                "step_description": step.get("description", ""),
                "acceptance_criteria": step.get("acceptance_criteria", []),
                "step_prompt": prompt,
            },
        )
    except Exception:
        _log.debug("final_review pre_execution failed", exc_info=True)
        return None


async def _final_review_post(
    self, task_id: str, step: dict, output: str, goal: str,
) -> None:
    """Phase 83: post_execution 终审——只读，结果写 plan_steps.review"""
    try:
        from services.long_task.final_review_agent import get_final_review_agent
        agent = get_final_review_agent()
        if agent is None:
            return
        review = await agent.review(
            task_id=task_id,
            stage="post_execution",
            step_id=step.get("id"),
            context={
                "goal": goal,
                "step_title": step.get("title", ""),
                "step_description": step.get("description", ""),
                "acceptance_criteria": step.get("acceptance_criteria", []),
                "output": output,
            },
        )
        await _write_step_review(self, step.get("id"), "post_execution", review)
    except Exception:
        _log.debug("final_review post_execution failed", exc_info=True)


async def _write_step_review(self, step_id: str | None, stage: str, review: dict) -> None:
    """写审查结果到 plan_steps.review.final_reviews（参考 inject_constraint 写法）"""
    if not step_id:
        return
    cur = await self._conn.execute(
        "SELECT review FROM plan_steps WHERE id=?", (step_id,),
    )
    row = await cur.fetchone()
    if not row:
        return
    try:
        existing = _json.loads(row[0]) if row[0] else {}
    except (_json.JSONDecodeError, TypeError):
        existing = {}
    existing.setdefault("final_reviews", []).append({
        "stage": stage,
        "ts": review.get("ts", int(_time.time())),
        "pass": review.get("pass"),
        "score": review.get("score"),
        "action": review.get("action"),
        "reason": review.get("reason"),
        "issues": review.get("issues", []),
    })
    await self._conn.execute(
        "UPDATE plan_steps SET review=?, updated_at=? WHERE id=?",
        (_json.dumps(existing, ensure_ascii=False), int(_time.time()), step_id),
    )
    await self._conn.commit()


# ═══════════════════════════════════════════════════════════
# Phase 84: 对抗验证接入
# ═══════════════════════════════════════════════════════════

async def _adversarial_challenge(
    self, task_id: str, stage: str, text: str,
    task_input: str, agent_id: str, step_id: str | None = None,
) -> dict | None:
    """对抗验证——best-effort，失败/缺单例返回 None（不阻塞主流程）

    2026-09-17: 这是 MAX 专属能力（多一轮审查，贵）。非 MAX 直接返回 None——
    不发起审查也不占 token；MAX 档允许最多 3 次重规划，具体用几次
    由对抗 Agent 的结论决定（见 _bump_adversarial_retry）。
    """
    from services.capability_limits import limits_for

    try:
        _state = await self._store.get_state(task_id)
    except Exception:
        _state = {}
    _level = (_state or {}).get("capability_level", 2 if (_state or {}).get("max_mode") else 1)
    _ceiling = limits_for(_level).adversarial_replans
    if _ceiling <= 0:
        return None
    self._adversarial_retry_ceiling[task_id] = _ceiling
    try:
        from services.long_task.adversarial_agent import get_adversarial_agent
        agent = get_adversarial_agent()
        if agent is None:
            return None
        return await agent.challenge_plan(
            task_id=task_id,
            stage=stage,
            plan_text=text,
            task_input=task_input,
            step_id=step_id,
        )
    except Exception:
        _log.debug("adversarial %s failed", stage, exc_info=True)
        return None


def _bump_adversarial_retry(self, task_id: str, stage: str) -> bool:
    """递增对抗验证 replan 计数——达到该档上限返回 False

    2026-09-17: 上限按能力档（MAX 最多 3 次；非 MAX 0 次——那时根本不发起审查）。
    上限由 _adversarial_challenge 写入 self._adversarial_retry_ceiling；缺省当 0（保守）。
    """
    per_task = self._adversarial_retries.setdefault(task_id, {})
    count = per_task.get(stage, 0)
    if count >= int(self._adversarial_retry_ceiling.get(task_id, 0)):
        return False
    per_task[stage] = count + 1
    return True


async def _write_adversarial_review(
    self, step_id: str | None, stage: str, review: dict,
) -> None:
    """写对抗验证结果到 plan_steps.review.adversarial_reviews

    与 final_reviews 平行存储，便于前端审查 Tab 分别展示。
    """
    if not step_id:
        return
    cur = await self._conn.execute(
        "SELECT review FROM plan_steps WHERE id=?", (step_id,),
    )
    row = await cur.fetchone()
    if not row:
        return
    try:
        existing = _json.loads(row[0]) if row[0] else {}
    except (_json.JSONDecodeError, TypeError):
        existing = {}
    existing.setdefault("adversarial_reviews", []).append({
        "stage": stage,
        "ts": review.get("ts", int(_time.time())),
        "pass": review.get("pass"),
        "refuted_count": review.get("refuted_count"),
        "surviving_count": review.get("surviving_count"),
        "action": review.get("action"),
        "reason": review.get("reason"),
    })
    await self._conn.execute(
        "UPDATE plan_steps SET review=?, updated_at=? WHERE id=?",
        (_json.dumps(existing, ensure_ascii=False), int(_time.time()), step_id),
    )
    await self._conn.commit()


# ═══════════════════════════════════════════════════════════
# Phase 85: 标准查询接入
# ═══════════════════════════════════════════════════════════

async def _standards_lookup_hook(
    self, task_id: str, step: dict, error: str, agent_id: str,
) -> None:
    """Phase 85: 节点失败时调 should_lookup → 命中则 lookup 并写 plan_steps.review

    best-effort，失败/缺单例不阻塞主流程。
    结果写入 plan_steps.review.standards_lookups（与 final_reviews/adversarial_reviews
    平行存储），供 SupervisorView.review_log 展示，用户可基于建议决定 replan。
    同时推 WS 让前端实时显示查询结果。
    """
    try:
        from services.long_task.standards_lookup_agent import get_standards_lookup_agent
        agent = get_standards_lookup_agent()
        if agent is None:
            return
        # 触发条件检查——error 关键词命中才查
        if not agent.should_lookup(error=error):
            return
        # 推断 domain——基于 confirmed_goal 简单关键词匹配
        state = await self._store.get_state(task_id)
        confirmed_goal = state.get("confirmed_goal", "")
        domain = _infer_domain(confirmed_goal)
        # 问题描述 = 步骤标题 + 错误信息
        problem = f"步骤「{step.get('title', '')}」执行失败：{error}"
        result = await agent.lookup(problem, domain=domain)
        await _write_standards_lookup(self, step.get("id"), result)
        # 推送 WS 让前端实时看到查询结果
        if result.get("ok"):
            await self._ws_manager.safe_send(task_id, {
                "type": "standards_lookup_result",
                "task_id": task_id,
                "step_id": step.get("id"),
                "summary": result.get("summary", ""),
                "recommendations": result.get("recommendations", []),
                "sources": result.get("sources", []),
                "source_type": result.get("source_type", ""),
            })
    except Exception:
        _log.debug("standards_lookup hook failed", exc_info=True)


def _infer_domain(confirmed_goal: str) -> str:
    """从 confirmed_goal 简单推断问题领域——仅供 standards_lookup 上下文提示"""
    if not confirmed_goal:
        return "general"
    goal_lower = confirmed_goal.lower()
    if "前端" in confirmed_goal or "frontend" in goal_lower or "react" in goal_lower:
        return "frontend"
    if "数据库" in confirmed_goal or "database" in goal_lower or "sql" in goal_lower:
        return "database"
    if "python" in goal_lower or "fastapi" in goal_lower or "django" in goal_lower:
        return "python"
    return "general"


async def _write_standards_lookup(
    self, step_id: str | None, result: dict,
) -> None:
    """写标准查询结果到 plan_steps.review.standards_lookups

    与 final_reviews/adversarial_reviews 平行存储，便于前端审查 Tab 分别展示。
    """
    if not step_id:
        return
    cur = await self._conn.execute(
        "SELECT review FROM plan_steps WHERE id=?", (step_id,),
    )
    row = await cur.fetchone()
    if not row:
        return
    try:
        existing = _json.loads(row[0]) if row[0] else {}
    except (_json.JSONDecodeError, TypeError):
        existing = {}
    existing.setdefault("standards_lookups", []).append({
        "ts": result.get("ts", int(_time.time())),
        "ok": result.get("ok"),
        "source_type": result.get("source_type"),
        "summary": result.get("summary", ""),
        "recommendations": result.get("recommendations", []),
        "sources": result.get("sources", []),
        "reason": result.get("reason", ""),
    })
    await self._conn.execute(
        "UPDATE plan_steps SET review=?, updated_at=? WHERE id=?",
        (_json.dumps(existing, ensure_ascii=False), int(_time.time()), step_id),
    )
    await self._conn.commit()
