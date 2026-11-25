"""tests/unit/test_intent_service_prompt_recommendation.py —— Phase 88

验证 IntentService 串联 PromptAgent.recommend() 的行为：
1. prompt_agent 存在时，analyze() 调 recommend() 并写入 route_info["prompt_recommendation"]
2. recommend() 抛异常时，analyze() 不阻塞（best-effort）
3. _dispatch 把 prompt_recommendation 放入 context["extra_context"] 透传给 invoke
4. prompt_agent=None 时，route_info 无 prompt_recommendation（向后兼容）
5. set_prompt_agent 支持 lazy 注入
"""
import os
import sys

import pytest
from config import DEFAULT_AGENT_ID

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ═══════════════════════════════════════════════════════════════
# Mock 组件
# ═══════════════════════════════════════════════════════════════

class _MockPromptAgent:
    """模拟 PromptAgent——recommend() 返回可控结果"""
    def __init__(self, result: dict | None = None, raises: Exception | None = None):
        self._result = result or {
            "role_ids": ["code_reviewer"],
            "behavior_ids": ["strict_testing"],
            "workflow_template": "tdd_loop",
            "reason": "test recommendation",
            "confidence": 0.85,
            "source": "rule",
        }
        self._raises = raises
        self.recommend_calls: list[tuple] = []

    async def recommend(self, task_input: str, agent_id: str = "hermes", project_id: str = "") -> dict:
        self.recommend_calls.append((task_input, agent_id, project_id))
        if self._raises:
            raise self._raises
        return self._result


class _TrackingAgentService:
    """模拟 AgentService——invoke() 记录 context 参数"""
    def __init__(self):
        self.invoke_calls: list[dict] = []
        self.adapters = {}

    async def invoke(self, agent_id: str, prompt: str, context: dict | None = None) -> str:
        self.invoke_calls.append({
            "agent_id": agent_id, "prompt": prompt, "context": context,
        })
        return "task-mock-id"

    async def is_running(self, agent_id: str) -> bool:
        return True


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _make_intent_service(prompt_agent=None):
    """构造 IntentService——默认用 mock 组件

    绕过 LLM 分类器（_llm_classifier=None），走正则保底路径。
    """
    from services.intent_service import IntentService
    agent_svc = _TrackingAgentService()
    svc = IntentService(
        agent_service=agent_svc,
        workflow_service=None,
        rules_repo=None,
        llm_classifier=None,  # 走正则保底
        deliberation_service=None,
        model_config=None,
        adapters={},
        prompt_agent=prompt_agent,
    )
    return svc, agent_svc


# ═══════════════════════════════════════════════════════════════
# TestPromptRecommendation
# ═══════════════════════════════════════════════════════════════

class TestPromptRecommendation:
    """Phase 88: IntentService.analyze 串联 PromptAgent.recommend"""

    async def test_analyze_calls_recommend_and_writes_route_info(self):
        """prompt_agent 存在时，analyze() 调 recommend() 并写入 route_info"""
        pa = _MockPromptAgent()
        svc, _ = _make_intent_service(prompt_agent=pa)

        # 用匹配正则的 prompt 触发 direct 路由
        result = await svc.analyze("写代码实现一个函数")

        # recommend 被调用
        assert len(pa.recommend_calls) == 1
        assert pa.recommend_calls[0][0] == "写代码实现一个函数"
        # route_info 含 prompt_recommendation
        rec = result["route_info"].get("prompt_recommendation")
        assert rec is not None
        assert rec["role_ids"] == ["code_reviewer"]
        assert rec["behavior_ids"] == ["strict_testing"]
        assert rec["workflow_template"] == "tdd_loop"

    async def test_recommend_exception_does_not_block(self):
        """recommend() 抛异常时，analyze() 不阻塞，route_info 无 prompt_recommendation"""
        pa = _MockPromptAgent(raises=RuntimeError("recommend boom"))
        svc, _ = _make_intent_service(prompt_agent=pa)

        result = await svc.analyze("写代码实现一个函数")

        # recommend 被调用（但抛异常）
        assert len(pa.recommend_calls) == 1
        # route_info 不含 prompt_recommendation（best-effort 跳过）
        assert "prompt_recommendation" not in result["route_info"]
        # task_id 仍然正常返回（主流程不阻塞）
        assert result["task_id"] == "task-mock-id"

    async def test_dispatch_passes_extra_context_to_invoke(self):
        """_dispatch 把 prompt_recommendation 放入 context["extra_context"] 透传给 invoke"""
        pa = _MockPromptAgent()
        svc, agent_svc = _make_intent_service(prompt_agent=pa)

        await svc.analyze("写代码实现一个函数")

        # invoke 被调用
        assert len(agent_svc.invoke_calls) == 1
        call = agent_svc.invoke_calls[0]
        # context 含 extra_context
        assert call["context"] is not None
        extra = call["context"].get("extra_context")
        assert extra is not None
        assert "prompt_recommendation" in extra
        assert extra["prompt_recommendation"]["role_ids"] == ["code_reviewer"]

    async def test_no_prompt_agent_no_recommendation(self):
        """prompt_agent=None 时，route_info 无 prompt_recommendation（向后兼容）"""
        svc, _ = _make_intent_service(prompt_agent=None)

        result = await svc.analyze("写代码实现一个函数")

        assert "prompt_recommendation" not in result["route_info"]
        assert result["task_id"] == "task-mock-id"

    async def test_set_prompt_agent_lazy_injection(self):
        """set_prompt_agent 支持 lazy 注入——构造时 None，后注入"""
        svc, _ = _make_intent_service(prompt_agent=None)
        # 初始无 prompt_agent
        assert svc._prompt_agent is None

        # lazy 注入
        pa = _MockPromptAgent()
        svc.set_prompt_agent(pa)
        assert svc._prompt_agent is pa

        # 注入后 analyze 能调 recommend
        result = await svc.analyze("写代码实现一个函数")
        assert len(pa.recommend_calls) == 1
        assert "prompt_recommendation" in result["route_info"]

    async def test_recommend_uses_classified_agent_id(self):
        """recommend() 用 _classify 返回的 agent_id 调用"""
        pa = _MockPromptAgent()
        svc, _ = _make_intent_service(prompt_agent=pa)

        # "写代码" 匹配正则 → route agent_id = 原生 DEFAULT_AGENT_ID
        await svc.analyze("写代码实现一个函数")

        assert len(pa.recommend_calls) == 1
        # 正则规则 r"(写代码|编程|实现|开发|函数|脚本|bug|修复|重构)" → 原生 Agent
        # （外部 Agent 不参与自动路由，2026-09-10 决策）
        called_agent_id = pa.recommend_calls[0][1]
        assert called_agent_id == DEFAULT_AGENT_ID
