"""services/long_task/adversarial_agent.py —— Phase 84 对抗验证 Agent

薄包装 services.dynamic_orchestrator.AdversarialReviewer，接入长任务编排。

3 个检查点（由 orchestrator 在对应时机调用）:
  - goal_confirmed:  目标确认后——验证目标合理性
  - plan_generated:  计划生成后——验证计划可执行性
  - output_merge:    子节点输出合并前——验证产出一致性

返回结构:
  {
    "pass": bool, "refuted_count": int, "surviving_count": int,
    "action": "proceed"|"replan",
    "reason": str, "stage": str, "task_id": str, "ts": int, "cached"?: bool,
  }

设计参考:
  - final_review_agent.py —— 冷却窗口 + best-effort fallback + 全局单例模式
  - dynamic_orchestrator.py:AdversarialReviewer —— 核心对抗验证逻辑（双后端并行）

为什么是薄包装而非直接调 AdversarialReviewer:
  - AdversarialReviewer 无冷却/无缓存/无 fallback——高频调用会拖慢编排
  - 需要统一返回结构（pass/action）供 orchestrator 决策
  - 需要全局单例供路由层手动触发
"""
from __future__ import annotations

import logging
import time as _time

_log = logging.getLogger("yuncat.adversarial")

# ── 冷却窗口（秒）── 3 个 stage 都不高频，统一 30s/task+stage
_COOLDOWN_SECONDS = 30


class AdversarialAgent:
    """Phase 84: 对抗验证 Agent——薄包装 AdversarialReviewer。

    生命周期：lifespan 创建单例并注入 app.state.adversarial_agent。
    被 orchestrator（3 检查点）和 routes/adversarial.py（手动触发）调用。
    """

    def __init__(self, agent_service, model_config=None):
        self._agent_service = agent_service
        self._model_config = model_config
        # 冷却窗口：key 格式 "{task_id}:{stage}" 或 "{task_id}:{stage}:{step_id}"
        self._cooldowns: dict[str, float] = {}
        # 冷却期内缓存最近一次结果（避免短时间重复调 LLM）
        self._cache: dict[str, dict] = {}

    async def challenge_plan(
        self,
        task_id: str,
        stage: str,
        plan_text: str,
        task_input: str,
        step_id: str | None = None,
        force: bool = False,
    ) -> dict:
        """对抗验证计划/产出。

        Args:
            task_id: 任务 ID
            stage: 检查点——goal_confirmed | plan_generated | output_merge
            plan_text: 待验证的文本（目标/计划/产出）
            task_input: 原始任务输入（供 reviewer 理解上下文）
            step_id: plan_step ID（output_merge stage 用，per step 冷却）
            force: True 跳过冷却 + 跳过缓存

        Returns:
            {pass, refuted_count, surviving_count, action, reason, stage, task_id, ts, cached?}
            action: "proceed"（无反驳）| "replan"（被反驳，触发回退/重新分解）
        """
        key = f"{task_id}:{stage}" + (f":{step_id}" if step_id else "")

        # 冷却检查——冷却期内返回缓存（无缓存则 fallback pass）
        if not force and self._in_cooldown(key):
            cached = self._cache.get(key)
            if cached:
                return {**cached, "cached": True}
            return self._fallback(task_id, stage, "cooldown no cache")

        # 空文本直接 pass（无内容可验证）
        if not plan_text or not plan_text.strip():
            result = self._fallback(task_id, stage, "empty plan_text")
            self._record(key, result)
            return result

        # 构造 proposals——AdversarialReviewer 期望 [{role_name, output}]
        proposals = [{"role_name": step_id or stage, "output": plan_text}]

        try:
            from services.dynamic_orchestrator import AdversarialReviewer
            reviewer = AdversarialReviewer(self._agent_service)
            surviving, refuted = await reviewer.review(proposals, task_input)
        except Exception as e:
            _log.debug("adversarial challenge failed: %s", e, exc_info=True)
            result = self._fallback(task_id, stage, f"exception: {e}")
            self._record(key, result)
            return result

        refuted_count = len(refuted)
        passed = refuted_count == 0
        action = "proceed" if passed else "replan"
        reason = f"{len(surviving)} survived, {refuted_count} refuted"

        result = {
            "pass": passed,
            "refuted_count": refuted_count,
            "surviving_count": len(surviving),
            "action": action,
            "reason": reason,
            "stage": stage,
            "task_id": task_id,
            "ts": int(_time.time()),
        }
        self._record(key, result)
        _log.info(
            "Adversarial %s for task %s: %s",
            stage, task_id, reason,
        )
        return result

    # ---- 内部辅助 ----

    def _in_cooldown(self, key: str) -> bool:
        last = self._cooldowns.get(key)
        if last is None:
            return False
        return _time.monotonic() - last < _COOLDOWN_SECONDS

    def _record(self, key: str, result: dict) -> None:
        """记录冷却时间戳 + 缓存结果"""
        self._cooldowns[key] = _time.monotonic()
        self._cache[key] = result

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

    @staticmethod
    def _fallback(task_id: str, stage: str, reason: str) -> dict:
        """best-effort fallback——任何失败均返回 pass，不阻塞主流程"""
        return {
            "pass": True,
            "refuted_count": 0,
            "surviving_count": 1,
            "action": "proceed",
            "reason": f"fallback: {reason}",
            "stage": stage,
            "task_id": task_id,
            "ts": int(_time.time()),
        }


# ── 全局单例（仿 final_review_agent.py 模式）──

_agent: AdversarialAgent | None = None


def get_adversarial_agent() -> AdversarialAgent | None:
    """获取全局 AdversarialAgent 单例（未初始化返回 None）"""
    return _agent


def set_adversarial_agent(agent: AdversarialAgent | None) -> None:
    """设置全局 AdversarialAgent 单例（lifespan 启动时调，测试可覆盖）"""
    global _agent
    _agent = agent
