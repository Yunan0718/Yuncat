"""tests/unit/test_adversarial_agent.py —— Phase 84 AdversarialAgent 测试

覆盖：
- AdversarialAgent.challenge_plan 基本流程（3 stage + pass/replan）
- 冷却窗口（per task / per step / force 跳过 / 缓存命中）
- best-effort fallback（空文本 / agent_service 异常 / 无 adapter）
- 全局单例 get/set

设计参考 test_final_review_agent.py 模式——Mock agent_service 而非 adapter
（因为 AdversarialReviewer 调 agent_service._invoke_with_role，不是 adapter.invoke）。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src-python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from services.long_task.adversarial_agent import (
    AdversarialAgent,
    get_adversarial_agent,
    set_adversarial_agent,
    _COOLDOWN_SECONDS,
)


# ═══════════════════════════════════════════════════════════════
# Mock 工具
# ═══════════════════════════════════════════════════════════════

class _MockAgentService:
    """模拟 agent_service——_invoke_with_role 返回预设字符串

    AdversarialReviewer 调 agent_service._invoke_with_role(agent_id, prompt, user_msg)，
    解析返回值中的 [REFUTED] {role_name} 标记决定是否反驳。
    """

    def __init__(
        self,
        review_output: str = "",
        adapters=None,
        raise_on_invoke: bool = False,
    ):
        self.adapters = adapters if adapters is not None else {"hermes": MagicMock()}
        self._review_output = review_output
        self._raise = raise_on_invoke
        self.invoke_calls: list[tuple] = []

    async def _invoke_with_role(self, agent_id, prompt, user_msg):
        self.invoke_calls.append((agent_id, prompt, user_msg))
        if self._raise:
            raise RuntimeError("mock invoke fail")
        return self._review_output


def _make_agent(
    review_output: str = "",
    adapters=None,
    raise_on_invoke: bool = False,
) -> AdversarialAgent:
    """构造带 mock agent_service 的 AdversarialAgent"""
    svc = _MockAgentService(
        review_output=review_output,
        adapters=adapters,
        raise_on_invoke=raise_on_invoke,
    )
    return AdversarialAgent(agent_service=svc, model_config=MagicMock())


def _refuted_marker(role_name: str) -> str:
    """生成 [REFUTED] {role_name} 标记——让 AdversarialReviewer 把该 proposal 归入 refuted"""
    return f"[REFUTED] {role_name}"


# ═══════════════════════════════════════════════════════════════
# TestChallengeBasic —— challenge_plan 基本流程
# ═══════════════════════════════════════════════════════════════

class TestChallengeBasic:
    """Phase 84-1: AdversarialAgent.challenge_plan 基本流程"""

    @pytest.mark.asyncio
    async def test_challenge_pass_no_refutation(self):
        """reviewer 输出无 [REFUTED] 标记——pass=True, action=proceed"""
        agent = _make_agent(review_output="提案看起来合理，未发现问题。")
        result = await agent.challenge_plan(
            task_id="t1", stage="goal_confirmed",
            plan_text="做一个 TODO 应用", task_input="做个 TODO 应用",
        )
        assert result["pass"] is True
        assert result["action"] == "proceed"
        assert result["refuted_count"] == 0
        assert result["surviving_count"] == 1
        assert result["stage"] == "goal_confirmed"
        assert result["task_id"] == "t1"
        assert "cached" not in result  # 首次调用非缓存

    @pytest.mark.asyncio
    async def test_challenge_refuted_triggers_replan(self):
        """reviewer 输出 [REFUTED] {role_name}——pass=False, action=replan"""
        # proposal 的 role_name 默认为 stage 名（goal_confirmed）
        agent = _make_agent(review_output=_refuted_marker("goal_confirmed"))
        result = await agent.challenge_plan(
            task_id="t1", stage="goal_confirmed",
            plan_text="不切实际的目标", task_input="原始输入",
        )
        assert result["pass"] is False
        assert result["action"] == "replan"
        assert result["refuted_count"] == 1
        assert result["surviving_count"] == 0

    @pytest.mark.asyncio
    async def test_challenge_plan_generated_stage(self):
        """plan_generated stage——role_name 为 stage 名"""
        agent = _make_agent(review_output="计划合理")
        result = await agent.challenge_plan(
            task_id="t2", stage="plan_generated",
            plan_text="1. 步骤一\n2. 步骤二", task_input="做 X",
        )
        assert result["pass"] is True
        assert result["stage"] == "plan_generated"

    @pytest.mark.asyncio
    async def test_challenge_output_merge_with_step_id(self):
        """output_merge stage + step_id——role_name 为 step_id"""
        agent = _make_agent(review_output=_refuted_marker("step-42"))
        result = await agent.challenge_plan(
            task_id="t3", stage="output_merge",
            plan_text="产出内容", task_input="目标",
            step_id="step-42",
        )
        assert result["pass"] is False
        assert result["action"] == "replan"
        assert result["refuted_count"] == 1


# ═══════════════════════════════════════════════════════════════
# TestCooldown —— 冷却窗口
# ═══════════════════════════════════════════════════════════════

class TestCooldown:
    """Phase 84-1: 冷却窗口 + 缓存"""

    @pytest.mark.asyncio
    async def test_cooldown_returns_cached(self):
        """冷却期内第二次调用返回缓存结果（cached=True）"""
        agent = _make_agent(review_output="合理")
        first = await agent.challenge_plan(
            "t1", "goal_confirmed", "目标", "输入",
        )
        second = await agent.challenge_plan(
            "t1", "goal_confirmed", "目标", "输入",
        )
        assert first["pass"] is True
        assert second["cached"] is True
        assert second["pass"] is first["pass"]
        # reviewer 只被调用一次（第二次走缓存）——检查 agent_service.invoke_calls
        svc = agent._agent_service
        assert len(svc.invoke_calls) == 1

    @pytest.mark.asyncio
    async def test_force_bypasses_cooldown(self):
        """force=True 跳过冷却——reviewer 被调用两次"""
        agent = _make_agent(review_output="合理")
        first = await agent.challenge_plan(
            "t1", "goal_confirmed", "目标", "输入",
        )
        second = await agent.challenge_plan(
            "t1", "goal_confirmed", "目标", "输入", force=True,
        )
        assert "cached" not in first
        assert "cached" not in second  # force 不走缓存
        svc = agent._agent_service
        assert len(svc.invoke_calls) == 2  # 调了两次

    @pytest.mark.asyncio
    async def test_cooldown_per_step_output_merge(self):
        """output_merge 冷却 per step——不同 step_id 不命中缓存"""
        agent = _make_agent(review_output="合理")
        await agent.challenge_plan(
            "t1", "output_merge", "产出1", "目标", step_id="s1",
        )
        await agent.challenge_plan(
            "t1", "output_merge", "产出2", "目标", step_id="s2",
        )
        svc = agent._agent_service
        assert len(svc.invoke_calls) == 2  # 不同 step 各调一次

    @pytest.mark.asyncio
    async def test_cooldown_different_tasks_not_shared(self):
        """不同 task 的冷却不共享"""
        agent = _make_agent(review_output="合理")
        await agent.challenge_plan("t1", "goal_confirmed", "目标A", "输入")
        await agent.challenge_plan("t2", "goal_confirmed", "目标B", "输入")
        svc = agent._agent_service
        assert len(svc.invoke_calls) == 2


# ═══════════════════════════════════════════════════════════════
# TestBestEffort —— 失败 best-effort fallback
# ═══════════════════════════════════════════════════════════════

class TestBestEffort:
    """Phase 84-1: 失败/异常时 best-effort 不阻塞主流程"""

    @pytest.mark.asyncio
    async def test_empty_plan_text_fallback_pass(self):
        """空 plan_text——返回 fallback pass（无内容可验证）"""
        agent = _make_agent(review_output="不该被调用")
        result = await agent.challenge_plan(
            "t1", "goal_confirmed", "", "输入",
        )
        assert result["pass"] is True
        assert result["action"] == "proceed"
        assert "fallback" in result["reason"]
        svc = agent._agent_service
        assert len(svc.invoke_calls) == 0  # 空文本不调 reviewer

    @pytest.mark.asyncio
    async def test_whitespace_plan_text_fallback_pass(self):
        """纯空白 plan_text——返回 fallback pass"""
        agent = _make_agent(review_output="不该被调用")
        result = await agent.challenge_plan(
            "t1", "plan_generated", "   \n  ", "输入",
        )
        assert result["pass"] is True
        assert "fallback" in result["reason"]

    @pytest.mark.asyncio
    async def test_invoke_exception_fallback_pass(self):
        """_invoke_with_role 抛异常——返回 fallback pass

        AdversarialReviewer 内部已有 try/except，但外层 challenge_plan
        也包了 try/except 防止其他异常冒泡。
        """
        agent = _make_agent(raise_on_invoke=True)
        result = await agent.challenge_plan(
            "t1", "goal_confirmed", "目标", "输入",
        )
        assert result["pass"] is True
        assert result["action"] == "proceed"

    @pytest.mark.asyncio
    async def test_no_adapters_pass(self):
        """agent_service 无 adapter——AdversarialReviewer 返回 (proposals, [])→pass"""
        svc = _MockAgentService(review_output="", adapters={})
        agent = AdversarialAgent(agent_service=svc, model_config=MagicMock())
        result = await agent.challenge_plan(
            "t1", "goal_confirmed", "目标", "输入",
        )
        # AdversarialReviewer 无 adapter 时返回 (proposals, []) → 0 refuted → pass
        assert result["pass"] is True
        assert result["refuted_count"] == 0


# ═══════════════════════════════════════════════════════════════
# TestSingleton —— 全局单例 get/set
# ═══════════════════════════════════════════════════════════════

class TestSingleton:
    """Phase 84-1: 全局单例 get_adversarial_agent / set_adversarial_agent"""

    def test_get_set_singleton(self):
        """set 后 get 返回同一实例"""
        original = get_adversarial_agent()
        try:
            agent = AdversarialAgent(
                agent_service=_MockAgentService(), model_config=MagicMock(),
            )
            set_adversarial_agent(agent)
            assert get_adversarial_agent() is agent
        finally:
            set_adversarial_agent(original)  # 恢复原始单例

    def test_get_returns_none_when_not_set(self):
        """未 set 时 get 返回 None"""
        original = get_adversarial_agent()
        try:
            set_adversarial_agent(None)
            assert get_adversarial_agent() is None
        finally:
            set_adversarial_agent(original)
