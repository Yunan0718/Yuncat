"""tests/unit/test_goal_clarifier.py —— Phase 78 GoalClarifier 单元测试

覆盖：
1. generate_questions —— 正常 / LLM 失败 / adapter 缺失 / model_config 失败 /
   error 返回 / JSON 解析失败 / previous_qa / 超 5 截断 / 类型非法 / 空列表
2. refine_goal —— 正常 / LLM 失败兜底 / JSON 失败 / grading_levels 边界 /
   confirmed_goal 空 / qa_list 空

设计：mock adapter.invoke 返回预设 content；mock model_config.get_active_config。
不启动真实子进程，不发起真实 HTTP。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from config import DEFAULT_AGENT_ID


# ═══════════════════════════════════════════════════════════════
# Mock 工具
# ═══════════════════════════════════════════════════════════════

class _MockAdapter:
    """模拟 adapter——可配置 invoke 返回值，记录调用"""

    def __init__(self, invoke_return=None):
        self.agent_id = "hermes"
        self._invoke_return = invoke_return if invoke_return is not None else {"content": "[]"}
        self.invoke_calls: list[tuple[str, dict]] = []

    async def invoke(self, prompt: str, context: dict | None = None) -> dict:
        self.invoke_calls.append((prompt, context or {}))
        if isinstance(self._invoke_return, Exception):
            raise self._invoke_return
        return self._invoke_return


class _MockModelConfig:
    """模拟 ModelConfigService——get_active_config 返回预设 cfg"""

    def __init__(self, cfg=None):
        self._cfg = cfg if cfg is not None else {
            "model": "test-flash",
            "api_key": "test-key",
            "base_url": "http://test.local/v1",
        }

    async def get_active_config(self, agent_id: str = "") -> dict:
        if isinstance(self._cfg, Exception):
            raise self._cfg
        return self._cfg


@pytest.fixture
def make_clarifier():
    """工厂 fixture——返回 (clarifier, adapter, model_config) 三元组

    注：DEFAULT_AGENT_ID 已从 hermes 改为 yuncat（Phase N），
    fixture 注册默认 agent id，使未显式传 agent_id 的调用命中 adapter。
    """
    def _make(invoke_return=None, cfg=None):
        adapter = _MockAdapter(invoke_return=invoke_return)
        mc = _MockModelConfig(cfg=cfg)
        from services.long_task.goal_clarifier import GoalClarifier
        clarifier = GoalClarifier(adapters={DEFAULT_AGENT_ID: adapter}, model_config=mc)
        return clarifier, adapter, mc
    return _make


# ═══════════════════════════════════════════════════════════════
# generate_questions
# ═══════════════════════════════════════════════════════════════

class TestGenerateQuestions:
    """generate_questions —— 问题生成"""

    async def test_normal_returns_questions(self, make_clarifier):
        """正常返回问题列表——每个问题含 id/type/question"""
        questions_json = json.dumps([
            {"id": "q1", "type": "scope", "question": "做什么？"},
            {"id": "q2", "type": "acceptance", "question": "如何验收？"},
        ])
        clarifier, adapter, _ = make_clarifier(invoke_return={"content": questions_json})

        result = await clarifier.generate_questions("实现登录功能")

        assert len(result) == 2
        assert all("id" in q and "type" in q and "question" in q for q in result)
        assert result[0]["type"] == "scope"
        assert result[1]["type"] == "acceptance"

    async def test_max_5_questions_truncated(self, make_clarifier):
        """超过 5 个问题 → 截断到 5 个"""
        questions_json = json.dumps([
            {"id": f"q{i}", "type": "scope", "question": f"问题{i}"}
            for i in range(1, 8)  # 7 个
        ])
        clarifier, _, _ = make_clarifier(invoke_return={"content": questions_json})

        result = await clarifier.generate_questions("任务")

        assert len(result) == 5

    async def test_invalid_type_normalized_to_scope(self, make_clarifier):
        """type 非法 → 规范化为 'scope'"""
        questions_json = json.dumps([
            {"id": "q1", "type": "invalid_type", "question": "问题"},
        ])
        clarifier, _, _ = make_clarifier(invoke_return={"content": questions_json})

        result = await clarifier.generate_questions("任务")

        assert result[0]["type"] == "scope"

    async def test_llm_exception_returns_default(self, make_clarifier):
        """adapter.invoke 抛异常 → 返回 1 个默认问题"""
        clarifier, _, _ = make_clarifier(invoke_return=RuntimeError("网络错误"))

        result = await clarifier.generate_questions("任务")

        assert len(result) == 1
        assert result[0]["id"] == "q-fallback"
        assert "最终结果" in result[0]["question"]

    async def test_adapter_missing_returns_default(self, make_clarifier):
        """adapters 中无指定 agent_id → 默认问题"""
        from services.long_task.goal_clarifier import GoalClarifier
        mc = _MockModelConfig()
        clarifier = GoalClarifier(adapters={}, model_config=mc)  # 无 adapter

        result = await clarifier.generate_questions("任务", agent_id="hermes")

        assert len(result) == 1
        assert result[0]["id"] == "q-fallback"

    async def test_model_config_failure_returns_default(self, make_clarifier):
        """model_config.get_active_config 抛异常 → 默认问题"""
        clarifier, _, _ = make_clarifier(cfg=RuntimeError("配置加载失败"))

        result = await clarifier.generate_questions("任务")

        assert len(result) == 1
        assert result[0]["id"] == "q-fallback"

    async def test_adapter_error_returns_default(self, make_clarifier):
        """adapter 返回 {"error": ...} → 默认问题"""
        clarifier, _, _ = make_clarifier(invoke_return={"error": "模型不可用"})

        result = await clarifier.generate_questions("任务")

        assert len(result) == 1
        assert result[0]["id"] == "q-fallback"

    async def test_json_parse_failure_returns_default(self, make_clarifier):
        """content 非 JSON → 默认问题"""
        clarifier, _, _ = make_clarifier(invoke_return={"content": "不是 JSON"})

        result = await clarifier.generate_questions("任务")

        assert len(result) == 1
        assert result[0]["id"] == "q-fallback"

    async def test_empty_list_returns_default(self, make_clarifier):
        """LLM 返回空数组 → 默认问题"""
        clarifier, _, _ = make_clarifier(invoke_return={"content": "[]"})

        result = await clarifier.generate_questions("任务")

        assert len(result) == 1
        assert result[0]["id"] == "q-fallback"

    async def test_previous_qa_no_error(self, make_clarifier):
        """带 previous_qa 不报错（追问场景）"""
        questions_json = json.dumps([
            {"id": "q1", "type": "assumption", "question": "追问？"},
        ])
        clarifier, _, _ = make_clarifier(invoke_return={"content": questions_json})

        result = await clarifier.generate_questions(
            "任务", previous_qa=[{"question": "Q1", "answer": "A1"}],
        )

        assert len(result) == 1
        assert result[0]["type"] == "assumption"

    async def test_max_tokens_400_passed(self, make_clarifier):
        """context 含 max_tokens=400"""
        questions_json = json.dumps([{"id": "q1", "type": "scope", "question": "Q"}])
        clarifier, adapter, _ = make_clarifier(invoke_return={"content": questions_json})

        await clarifier.generate_questions("任务")

        assert len(adapter.invoke_calls) == 1
        ctx = adapter.invoke_calls[0][1]
        assert ctx["max_tokens"] == 400


# ═══════════════════════════════════════════════════════════════
# refine_goal
# ═══════════════════════════════════════════════════════════════

class TestRefineGoal:
    """refine_goal —— 目标提炼"""

    async def test_normal_refine(self, make_clarifier):
        """正常返回 confirmed_goal + grading_levels + assumptions"""
        refined_json = json.dumps({
            "confirmed_goal": "实现用户登录功能",
            "grading_levels": ["能运行", "有测试", "有文档", "性能达标"],
            "assumptions": ["使用 JWT", "单端登录"],
        })
        clarifier, _, _ = make_clarifier(invoke_return={"content": refined_json})

        result = await clarifier.refine_goal([
            {"question": "做什么？", "answer": "登录功能"},
        ])

        assert result["confirmed_goal"] == "实现用户登录功能"
        assert len(result["grading_levels"]) == 4
        assert result["assumptions"] == ["使用 JWT", "单端登录"]

    async def test_grading_levels_too_few_padded(self, make_clarifier):
        """grading_levels <2 → 用默认 2 级"""
        refined_json = json.dumps({
            "confirmed_goal": "目标",
            "grading_levels": ["仅一级"],
        })
        clarifier, _, _ = make_clarifier(invoke_return={"content": refined_json})

        result = await clarifier.refine_goal([{"question": "Q", "answer": "A"}])

        assert len(result["grading_levels"]) == 2
        assert result["grading_levels"] == ["能运行", "有测试"]

    async def test_grading_levels_too_many_truncated(self, make_clarifier):
        """grading_levels >5 → 截断到 5"""
        refined_json = json.dumps({
            "confirmed_goal": "目标",
            "grading_levels": ["l1", "l2", "l3", "l4", "l5", "l6", "l7"],
        })
        clarifier, _, _ = make_clarifier(invoke_return={"content": refined_json})

        result = await clarifier.refine_goal([{"question": "Q", "answer": "A"}])

        assert len(result["grading_levels"]) == 5

    async def test_empty_confirmed_goal_fallback(self, make_clarifier):
        """confirmed_goal 为空 → 用 Q&A 兜底"""
        refined_json = json.dumps({
            "confirmed_goal": "",
            "grading_levels": ["l1", "l2"],
        })
        clarifier, _, _ = make_clarifier(invoke_return={"content": refined_json})

        result = await clarifier.refine_goal([
            {"question": "Q", "answer": "用第一个回答兜底"},
        ])

        assert result["confirmed_goal"] == "用第一个回答兜底"
        assert len(result["grading_levels"]) == 2

    async def test_llm_exception_fallback(self, make_clarifier):
        """LLM 异常 → 用 Q&A 兜底"""
        clarifier, _, _ = make_clarifier(invoke_return=RuntimeError("网络错误"))

        result = await clarifier.refine_goal([
            {"question": "Q", "answer": "异常时兜底"},
        ])

        assert result["confirmed_goal"] == "异常时兜底"
        assert len(result["grading_levels"]) == 2

    async def test_json_failure_fallback(self, make_clarifier):
        """JSON 解析失败 → 用 Q&A 兜底"""
        clarifier, _, _ = make_clarifier(invoke_return={"content": "不是 JSON"})

        result = await clarifier.refine_goal([
            {"question": "Q", "answer": "JSON 失败兜底"},
        ])

        assert result["confirmed_goal"] == "JSON 失败兜底"

    async def test_empty_qa_list_fallback(self, make_clarifier):
        """qa_list 为空 → 返回'请用户确认最终目标'"""
        refined_json = json.dumps({"confirmed_goal": "", "grading_levels": []})
        clarifier, _, _ = make_clarifier(invoke_return={"content": refined_json})

        result = await clarifier.refine_goal([])

        assert result["confirmed_goal"] == "请用户确认最终目标"
        assert len(result["grading_levels"]) == 2

    async def test_adapter_missing_fallback(self, make_clarifier):
        """adapter 缺失 → 用 Q&A 兜底"""
        from services.long_task.goal_clarifier import GoalClarifier
        mc = _MockModelConfig()
        clarifier = GoalClarifier(adapters={}, model_config=mc)

        result = await clarifier.refine_goal([
            {"question": "Q", "answer": "无 adapter 兜底"},
        ])

        assert result["confirmed_goal"] == "无 adapter 兜底"

    async def test_assumptions_normalized(self, make_clarifier):
        """assumptions 非列表或含空值 → 规范化为字符串列表"""
        refined_json = json.dumps({
            "confirmed_goal": "目标",
            "grading_levels": ["l1", "l2"],
            "assumptions": ["有效假设", "", 123, None],
        })
        clarifier, _, _ = make_clarifier(invoke_return={"content": refined_json})

        result = await clarifier.refine_goal([{"question": "Q", "answer": "A"}])

        # 空字符串被过滤，非字符串被 str() 转换
        assert "有效假设" in result["assumptions"]
        assert all(isinstance(a, str) and a for a in result["assumptions"])


# ═══════════════════════════════════════════════════════════════
# Task 6.11: confidence + needs_more_clarification
# ═══════════════════════════════════════════════════════════════

class TestRefineGoalConfidence:
    """Task 6.11: refine_goal 输出 confidence + needs_more_clarification + clarification_reason"""

    async def test_normal_refine_includes_confidence(self, make_clarifier):
        """正常返回包含 confidence 字段"""
        refined_json = json.dumps({
            "confirmed_goal": "实现登录",
            "grading_levels": ["能运行", "有测试"],
            "assumptions": [],
            "confidence": 0.85,
            "needs_more_clarification": False,
            "clarification_reason": "",
        })
        clarifier, _, _ = make_clarifier(invoke_return={"content": refined_json})

        result = await clarifier.refine_goal([{"question": "Q", "answer": "A"}])

        assert "confidence" in result
        assert "needs_more_clarification" in result
        assert "clarification_reason" in result
        assert result["confidence"] == 0.85
        assert result["needs_more_clarification"] is False

    async def test_high_confidence_no_more_clarification(self, make_clarifier):
        """confidence=0.9 → needs_more_clarification=False"""
        refined_json = json.dumps({
            "confirmed_goal": "目标",
            "grading_levels": ["l1", "l2"],
            "confidence": 0.9,
            "needs_more_clarification": False,
        })
        clarifier, _, _ = make_clarifier(invoke_return={"content": refined_json})

        result = await clarifier.refine_goal([{"question": "Q", "answer": "A"}])

        assert result["confidence"] == 0.9
        assert result["needs_more_clarification"] is False

    async def test_low_confidence_needs_more_clarification(self, make_clarifier):
        """confidence=0.3 + needs_more_clarification=True → 自主判断需追问"""
        refined_json = json.dumps({
            "confirmed_goal": "目标",
            "grading_levels": ["l1", "l2"],
            "confidence": 0.3,
            "needs_more_clarification": True,
            "clarification_reason": "用户对验收标准描述模糊",
        })
        clarifier, _, _ = make_clarifier(invoke_return={"content": refined_json})

        result = await clarifier.refine_goal([{"question": "Q", "answer": "A"}])

        assert result["confidence"] == 0.3
        assert result["needs_more_clarification"] is True
        assert "验收标准" in result["clarification_reason"]

    async def test_confidence_clamped_high(self, make_clarifier):
        """confidence=1.5 → clamp 到 1.0"""
        refined_json = json.dumps({
            "confirmed_goal": "目标",
            "grading_levels": ["l1", "l2"],
            "confidence": 1.5,
        })
        clarifier, _, _ = make_clarifier(invoke_return={"content": refined_json})

        result = await clarifier.refine_goal([{"question": "Q", "answer": "A"}])

        assert result["confidence"] == 1.0

    async def test_confidence_clamped_low(self, make_clarifier):
        """confidence=-0.5 → clamp 到 0.0"""
        refined_json = json.dumps({
            "confirmed_goal": "目标",
            "grading_levels": ["l1", "l2"],
            "confidence": -0.5,
        })
        clarifier, _, _ = make_clarifier(invoke_return={"content": refined_json})

        result = await clarifier.refine_goal([{"question": "Q", "answer": "A"}])

        assert result["confidence"] == 0.0

    async def test_missing_confidence_defaults_to_0_5(self, make_clarifier):
        """LLM 未返回 confidence → 默认 0.5（中性，不触发追问）"""
        refined_json = json.dumps({
            "confirmed_goal": "目标",
            "grading_levels": ["l1", "l2"],
        })
        clarifier, _, _ = make_clarifier(invoke_return={"content": refined_json})

        result = await clarifier.refine_goal([{"question": "Q", "answer": "A"}])

        assert result["confidence"] == 0.5
        assert result["needs_more_clarification"] is False

    async def test_threshold_0_7_boundary_high(self, make_clarifier):
        """confidence=0.7（边界）→ needs_more_clarification=False（>=0.7 不追问）"""
        refined_json = json.dumps({
            "confirmed_goal": "目标",
            "grading_levels": ["l1", "l2"],
            "confidence": 0.7,
        })
        clarifier, _, _ = make_clarifier(invoke_return={"content": refined_json})

        result = await clarifier.refine_goal([{"question": "Q", "answer": "A"}])

        assert result["needs_more_clarification"] is False

    async def test_threshold_0_69_boundary_low(self, make_clarifier):
        """confidence=0.69 → needs_more_clarification=True（<0.7 触发追问）"""
        refined_json = json.dumps({
            "confirmed_goal": "目标",
            "grading_levels": ["l1", "l2"],
            "confidence": 0.69,
        })
        clarifier, _, _ = make_clarifier(invoke_return={"content": refined_json})

        result = await clarifier.refine_goal([{"question": "Q", "answer": "A"}])

        assert result["needs_more_clarification"] is True

    async def test_needs_more_clarification_default_reason(self, make_clarifier):
        """needs_more_clarification=True 但无 reason → 填默认 reason"""
        refined_json = json.dumps({
            "confirmed_goal": "目标",
            "grading_levels": ["l1", "l2"],
            "confidence": 0.3,
            "needs_more_clarification": True,
            # 故意不提供 clarification_reason
        })
        clarifier, _, _ = make_clarifier(invoke_return={"content": refined_json})

        result = await clarifier.refine_goal([{"question": "Q", "answer": "A"}])

        assert result["needs_more_clarification"] is True
        # 默认 reason 非空
        assert isinstance(result["clarification_reason"], str)
        assert len(result["clarification_reason"]) > 0

    async def test_fallback_confidence_0_5_no_more(self, make_clarifier):
        """LLM 失败兜底 → confidence=0.5, needs_more_clarification=False（不追问兜底目标）"""
        clarifier, _, _ = make_clarifier(invoke_return=RuntimeError("LLM 挂了"))

        result = await clarifier.refine_goal([{"question": "Q", "answer": "兜底目标"}])

        assert result["confirmed_goal"] == "兜底目标"
        assert result["confidence"] == 0.5
        assert result["needs_more_clarification"] is False


# ═══════════════════════════════════════════════════════════════
# Task 6.11: user_context（小白友好）
# ═══════════════════════════════════════════════════════════════

class TestUserContext:
    """Task 6.11: user_context 适配用户技能等级（小白友好）"""

    async def test_generate_questions_accepts_user_context(self, make_clarifier):
        """generate_questions 接受 user_context 参数不报错"""
        questions_json = json.dumps([{"id": "q1", "type": "scope", "question": "Q"}])
        clarifier, adapter, _ = make_clarifier(invoke_return={"content": questions_json})

        result = await clarifier.generate_questions(
            "任务", user_context={"skill_level": "beginner"},
        )

        assert len(result) == 1
        assert len(adapter.invoke_calls) == 1

    async def test_beginner_context_adds_instruction(self, make_clarifier):
        """skill_level=beginner → prompt 含新手适配指令"""
        questions_json = json.dumps([{"id": "q1", "type": "scope", "question": "Q"}])
        clarifier, adapter, _ = make_clarifier(invoke_return={"content": questions_json})

        await clarifier.generate_questions(
            "任务", user_context={"skill_level": "beginner"},
        )

        prompt = adapter.invoke_calls[0][0]
        # 新手适配指令应包含"通俗"或"避免专业术语"或"举例"等关键词
        assert any(kw in prompt for kw in ["通俗", "避免专业术语", "举例", "新手", "beginner"])

    async def test_expert_context_adds_instruction(self, make_clarifier):
        """skill_level=expert → prompt 含专家适配指令"""
        questions_json = json.dumps([{"id": "q1", "type": "scope", "question": "Q"}])
        clarifier, adapter, _ = make_clarifier(invoke_return={"content": questions_json})

        await clarifier.generate_questions(
            "任务", user_context={"skill_level": "expert"},
        )

        prompt = adapter.invoke_calls[0][0]
        # 专家适配指令应包含"术语"或"边界"或"性能"等关键词
        assert any(kw in prompt for kw in ["术语", "边界", "性能", "expert", "专家"])

    async def test_no_context_no_skill_instruction(self, make_clarifier):
        """user_context=None → prompt 不含技能适配指令"""
        questions_json = json.dumps([{"id": "q1", "type": "scope", "question": "Q"}])
        clarifier, adapter, _ = make_clarifier(invoke_return={"content": questions_json})

        await clarifier.generate_questions("任务")

        prompt = adapter.invoke_calls[0][0]
        # 用"用户技能等级"作为精确标记——避免与 role 名"目标澄清专家"中的"专家"混淆
        assert "用户技能等级" not in prompt
        assert "避免专业术语" not in prompt
        assert "通俗" not in prompt

    async def test_intermediate_context_treated_as_default(self, make_clarifier):
        """skill_level=intermediate → 同默认，无额外指令"""
        questions_json = json.dumps([{"id": "q1", "type": "scope", "question": "Q"}])
        clarifier, adapter, _ = make_clarifier(invoke_return={"content": questions_json})

        await clarifier.generate_questions(
            "任务", user_context={"skill_level": "intermediate"},
        )

        prompt = adapter.invoke_calls[0][0]
        assert "用户技能等级" not in prompt
        assert "避免专业术语" not in prompt
        assert "通俗" not in prompt

    async def test_refine_goal_accepts_user_context(self, make_clarifier):
        """refine_goal 接受 user_context 参数不报错"""
        refined_json = json.dumps({
            "confirmed_goal": "目标",
            "grading_levels": ["l1", "l2"],
            "confidence": 0.8,
        })
        clarifier, adapter, _ = make_clarifier(invoke_return={"content": refined_json})

        result = await clarifier.refine_goal(
            [{"question": "Q", "answer": "A"}],
            user_context={"skill_level": "beginner"},
        )

        assert result["confirmed_goal"] == "目标"
        prompt = adapter.invoke_calls[0][0]
        assert any(kw in prompt for kw in ["通俗", "避免专业术语", "举例", "新手"])

    async def test_invalid_skill_level_treated_as_default(self, make_clarifier):
        """skill_level 非法值 → 当默认处理（无额外指令）"""
        questions_json = json.dumps([{"id": "q1", "type": "scope", "question": "Q"}])
        clarifier, adapter, _ = make_clarifier(invoke_return={"content": questions_json})

        await clarifier.generate_questions(
            "任务", user_context={"skill_level": "guru"},  # 非法值
        )

        prompt = adapter.invoke_calls[0][0]
        assert "用户技能等级" not in prompt
        assert "避免专业术语" not in prompt
        assert "通俗" not in prompt
