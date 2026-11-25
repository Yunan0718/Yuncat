"""tests/unit/test_route_model_tier.py —— Task 7.6: RouteDecision 加 model_tier 测试

验证 RouteDecision 的 model_tier 字段：
- 难度→模型真正分发，而非所有任务用同一模型
- task_type 映射到 model_tier（light/standard/heavy）
- route_task 返回的 RouteDecision 含正确的 model_tier

对标 OpenAI model selection / Claude model router——
简单任务用小模型（快+便宜），复杂任务用大模型（强+准）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ═══════════════════════════════════════════════════════════════
# RouteDecision model_tier 字段
# ═══════════════════════════════════════════════════════════════

class TestRouteDecisionModelTier:
    """Task 7.6: RouteDecision 加 model_tier 字段"""

    def test_route_decision_has_model_tier_field(self):
        """RouteDecision 有 model_tier 字段，默认 'standard'"""
        from services.task_router import RouteDecision
        rd = RouteDecision(agent_id="hermes")
        assert hasattr(rd, "model_tier")
        assert rd.model_tier == "standard"

    def test_explicit_model_tier_in_constructor(self):
        """构造函数可显式传 model_tier"""
        from services.task_router import RouteDecision
        rd = RouteDecision(agent_id="hermes", model_tier="heavy")
        assert rd.model_tier == "heavy"

    def test_backward_compatible_without_model_tier(self):
        """旧代码不传 model_tier 时默认 'standard'（向后兼容）"""
        from services.task_router import RouteDecision
        rd = RouteDecision(
            agent_id="hermes",
            confidence=0.85,
            capabilities_needed=["code_analysis"],
        )
        assert rd.model_tier == "standard"


# ═══════════════════════════════════════════════════════════════
# task_type → model_tier 映射
# ═══════════════════════════════════════════════════════════════

class TestTaskTypeToModelTierMapping:
    """Task 7.6: task_type 映射到 model_tier"""

    def test_light_tasks_get_light_tier(self):
        """简单任务 → 'light'（小模型即可）"""
        from services.task_router import TASK_TIER_MAP
        light_tasks = [
            "realtime_query", "general_question", "notification",
            "file_operation", "command_execution",
        ]
        for task_type in light_tasks:
            assert TASK_TIER_MAP.get(task_type) == "light", (
                f"{task_type} 应映射到 'light'"
            )

    def test_standard_tasks_get_standard_tier(self):
        """常规开发任务 → 'standard'（标准模型）"""
        from services.task_router import TASK_TIER_MAP
        standard_tasks = [
            "code_generation", "code_review", "image_analysis", "web_research",
        ]
        for task_type in standard_tasks:
            assert TASK_TIER_MAP.get(task_type) == "standard", (
                f"{task_type} 应映射到 'standard'"
            )

    def test_heavy_tasks_get_heavy_tier(self):
        """复杂任务 → 'heavy'（大模型）"""
        from services.task_router import TASK_TIER_MAP
        heavy_tasks = ["bug_fix", "architecture"]
        for task_type in heavy_tasks:
            assert TASK_TIER_MAP.get(task_type) == "heavy", (
                f"{task_type} 应映射到 'heavy'"
            )

    def test_unknown_task_defaults_to_standard(self):
        """未知 task_type → 'standard'（保守默认）"""
        from services.task_router import resolve_model_tier
        assert resolve_model_tier("unknown_task_type") == "standard"
        assert resolve_model_tier("") == "standard"


# ═══════════════════════════════════════════════════════════════
# route_task 集成测试
# ═══════════════════════════════════════════════════════════════

class TestRouteTaskSetsModelTier:
    """Task 7.6: route_task 返回的 RouteDecision 含正确的 model_tier"""

    @pytest.mark.asyncio
    async def test_realtime_query_gets_light_tier(self):
        """'今天星期几' → realtime_query → model_tier='light'"""
        from services.task_router import route_task
        adapters = {"hermes": object(), "openclaw": object()}
        route = await route_task("今天星期几", adapters)
        assert route.task_type == "realtime_query"
        assert route.model_tier == "light"

    @pytest.mark.asyncio
    async def test_code_generation_gets_standard_tier(self):
        """'帮我写代码' → code_generation → model_tier='standard'"""
        from services.task_router import route_task
        adapters = {"hermes": object(), "openclaw": object()}
        route = await route_task("帮我写一个 REST API 接口", adapters)
        assert route.task_type == "code_generation"
        assert route.model_tier == "standard"

    @pytest.mark.asyncio
    async def test_bug_fix_gets_heavy_tier(self):
        """'修复 bug' → bug_fix → model_tier='heavy'"""
        from services.task_router import route_task
        adapters = {"hermes": object(), "openclaw": object()}
        route = await route_task("修复这个 bug", adapters)
        assert route.task_type == "bug_fix"
        assert route.model_tier == "heavy"

    @pytest.mark.asyncio
    async def test_architecture_gets_heavy_tier(self):
        """'重构架构' → architecture → model_tier='heavy'"""
        from services.task_router import route_task
        adapters = {"hermes": object(), "openclaw": object()}
        route = await route_task("重构这个模块的架构", adapters)
        assert route.task_type == "architecture"
        assert route.model_tier == "heavy"

    @pytest.mark.asyncio
    async def test_general_question_gets_light_tier(self):
        """'你好' → general_question → model_tier='light'"""
        from services.task_router import route_task
        adapters = {"hermes": object(), "openclaw": object()}
        route = await route_task("你好", adapters)
        assert route.task_type == "general_question"
        assert route.model_tier == "light"

    @pytest.mark.asyncio
    async def test_model_tier_in_reasoning(self):
        """model_tier 出现在 reasoning 中（供调试/日志）"""
        from services.task_router import route_task
        adapters = {"hermes": object(), "openclaw": object()}
        route = await route_task("修复 bug", adapters)
        assert "model_tier" in route.reasoning or "heavy" in route.reasoning
