"""services/long_task/goal_clarifier.py —— Phase 78 目标澄清 Agent

长任务生命周期的第一步：在分解前澄清目标。
- generate_questions：生成最多 5 个澄清问题（范围/验收/约束/优先级/假设）
- refine_goal：根据 Q&A 提炼确认目标 + 验收等级 + 假设

使用 flash 模型，max_tokens=400。LLM 失败有规则兜底。

Task 6.11 增强：
- user_context（小白友好）：根据 skill_level=beginner|intermediate|expert
  适配 prompt 语言风格。beginner → 通俗语言/避免专业术语/举例；
  expert → 专业术语/边界条件/性能考量。
- confidence 自主判断：refine_goal 输出 confidence(0.0-1.0) +
  needs_more_clarification(bool) + clarification_reason(str)。
  阈值 _CONFIDENCE_THRESHOLD=0.7，低于则自主追问（供 orchestrator
  实现 auto_followup 双向讨论）。
"""
import json
import logging
from typing import Any

from config import DEFAULT_AGENT_ID

logger = logging.getLogger("yuncat.long_task")

# 常量
_MAX_QUESTIONS = 5
_MAX_TOKENS = 400
_TEMPERATURE = 0.3

# Task 6.11: confidence 阈值与默认值
# 主流做法参考：LangChain Agent "should_continue" 用 0.7 作为置信度阈值；
# ChatGPT "clarify before acting" 也是 ~70% 置信度以下追问。
_CONFIDENCE_THRESHOLD = 0.7
_DEFAULT_CONFIDENCE = 0.5  # LLM 未返回或解析失败时的中性默认（不触发追问）
_DEFAULT_CLARIFICATION_REASON = "目标描述不够清晰，需要进一步澄清细节"

# 问题类型
QUESTION_TYPES = {"scope", "acceptance", "constraint", "priority", "assumption"}

# 合法的 user_context.skill_level 值
_VALID_SKILL_LEVELS = {"beginner", "intermediate", "expert"}

# 兜底问题（LLM 失败时返回）
_DEFAULT_QUESTION = {
    "id": "q-fallback",
    "type": "scope",
    "question": "请用一句话描述你期望的最终结果",
}

# 兜底验收等级
_DEFAULT_GRADING_LEVELS = ["能运行", "有测试"]


class GoalClarifier:
    """目标澄清 Agent——长任务分解前的目标确认

    方案 §8 Phase 78：confirmed_goal 确定后，后续长任务执行中
    除非用户主动修改，否则保持不变。
    """

    def __init__(self, adapters: dict, model_config):
        self._adapters = adapters
        self._model_config = model_config

    async def generate_questions(
        self,
        task_input: str,
        previous_qa: list[dict] | None = None,
        agent_id: str = DEFAULT_AGENT_ID,
        user_context: dict | None = None,
    ) -> list[dict]:
        """生成澄清问题，最多 5 个

        Args:
            task_input: 用户原始任务描述
            previous_qa: 之前的 Q&A 轮次（用于追问）
            agent_id: 使用的 Agent
            user_context: Task 6.11 用户上下文，支持 {"skill_level":
                "beginner|intermediate|expert"} 适配 prompt 语言风格（小白友好）

        Returns:
            [{"id": "q1", "type": "scope|acceptance|constraint|priority|assumption",
              "question": "..."}]
            LLM 失败时返回 [_DEFAULT_QUESTION]
        """
        adapter = self._adapters.get(agent_id)
        if adapter is None:
            return [_DEFAULT_QUESTION]

        try:
            cfg = await self._model_config.get_active_config(agent_id)
        except Exception:
            return [_DEFAULT_QUESTION]

        prompt = self._build_questions_prompt(task_input, previous_qa, user_context)
        try:
            result = await adapter.invoke(
                prompt,
                context={
                    "model_config": cfg,
                    "max_tokens": _MAX_TOKENS,
                    "temperature": _TEMPERATURE,
                },
            )
        except Exception:
            return [_DEFAULT_QUESTION]

        if "error" in result:
            return [_DEFAULT_QUESTION]

        content = result.get("content", "")
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError, TypeError):
            return [_DEFAULT_QUESTION]

        return self._normalize_questions(parsed)

    async def refine_goal(
        self,
        qa_list: list[dict],
        agent_id: str = DEFAULT_AGENT_ID,
        user_context: dict | None = None,
    ) -> dict[str, Any]:
        """根据 Q&A 提炼确认目标

        Args:
            qa_list: [{"question": "...", "answer": "..."}]
            agent_id: 使用的 Agent
            user_context: Task 6.11 用户上下文（同 generate_questions）

        Returns:
            {
                "confirmed_goal": str,  # 一句可独立执行的目标描述
                "grading_levels": list[str],  # 2-5 级验收标准
                "assumptions": list[str],  # 假设列表
                "confidence": float,  # Task 6.11: 0.0-1.0，clamp 到 [0,1]
                "needs_more_clarification": bool,  # confidence < 0.7 时 True
                "clarification_reason": str,  # 追问理由（needs_more=True 时非空）
            }
        """
        adapter = self._adapters.get(agent_id)
        if adapter is None:
            return self._fallback_refine(qa_list)

        try:
            cfg = await self._model_config.get_active_config(agent_id)
        except Exception:
            return self._fallback_refine(qa_list)

        prompt = self._build_refine_prompt(qa_list, user_context)
        try:
            result = await adapter.invoke(
                prompt,
                context={
                    "model_config": cfg,
                    "max_tokens": _MAX_TOKENS,
                    "temperature": _TEMPERATURE,
                },
            )
        except Exception:
            return self._fallback_refine(qa_list)

        if "error" in result:
            return self._fallback_refine(qa_list)

        content = result.get("content", "")
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError, TypeError):
            return self._fallback_refine(qa_list)

        return self._normalize_refined(parsed, qa_list)

    # ── 内部辅助 ──

    def _build_user_context_instruction(self, user_context: dict | None) -> str:
        """Task 6.11: 根据 user_context.skill_level 生成 prompt 适配指令

        主流做法参考：ChatGPT "adaptive complexity" 会根据用户表现调整语言；
        MetaGPT "User Persona" 也用 skill_level 分级。

        - beginner → 通俗语言/避免专业术语/举例说明
        - expert → 专业术语/边界条件/性能考量
        - intermediate / None / 非法值 → 无额外指令（默认）
        """
        if not isinstance(user_context, dict):
            return ""
        skill_level = str(user_context.get("skill_level", "")).lower().strip()
        if skill_level not in _VALID_SKILL_LEVELS:
            return ""  # 非法值当默认处理
        if skill_level == "beginner":
            return (
                "\n\n## 用户技能等级：新手（beginner）\n"
                "请用通俗易懂的语言提问，避免专业术语，必要时举例说明。"
                "关注用户真正想达成的业务目标，而非技术实现细节。"
            )
        if skill_level == "expert":
            return (
                "\n\n## 用户技能等级：专家（expert）\n"
                "可直接使用专业术语，关注边界条件、性能考量、技术取舍。"
                "提问直接切入技术决策点。"
            )
        return ""  # intermediate → 无额外指令

    def _build_questions_prompt(
        self, task_input: str, previous_qa: list | None,
        user_context: dict | None = None,
    ) -> str:
        """构建生成问题的 prompt"""
        prev_text = ""
        if previous_qa:
            prev_lines = []
            for qa in previous_qa:
                prev_lines.append(
                    f"  Q: {qa.get('question', '')}\n  A: {qa.get('answer', '')}"
                )
            prev_text = "\n\n## 已有 Q&A\n" + "\n".join(prev_lines)

        # Task 6.11: 用户技能等级适配指令
        ctx_instruction = self._build_user_context_instruction(user_context)

        return f"""你是目标澄清专家。分析以下任务，生成最多 {_MAX_QUESTIONS} 个澄清问题。

## 任务描述
{task_input}{prev_text}{ctx_instruction}

## 问题类型
- scope: 范围澄清（做什么/不做什么）
- acceptance: 验收标准（如何判断完成）
- constraint: 约束条件（技术/时间/资源限制）
- priority: 优先级（多个目标的取舍）
- assumption: 假设确认（隐含前提是否成立）

## 输出格式（严格 JSON）
[
  {{"id": "q1", "type": "scope", "question": "具体问题"}},
  {{"id": "q2", "type": "acceptance", "question": "..."}}
]

只输出 JSON，不要其他文字。"""

    def _build_refine_prompt(
        self, qa_list: list[dict], user_context: dict | None = None,
    ) -> str:
        """构建提炼目标的 prompt"""
        qa_text = ""
        for i, qa in enumerate(qa_list, 1):
            qa_text += f"\nQ{i}: {qa.get('question', '')}\nA{i}: {qa.get('answer', '')}\n"

        # Task 6.11: 用户技能等级适配指令
        ctx_instruction = self._build_user_context_instruction(user_context)

        return f"""你是目标提炼专家。根据以下 Q&A，提炼出确认目标。

## Q&A{qa_text}{ctx_instruction}

## 输出格式（严格 JSON）
{{
  "confirmed_goal": "一句可独立执行的目标描述",
  "grading_levels": ["能运行", "有测试", "有文档", "性能达标"],
  "assumptions": ["假设1", "假设2"],
  "confidence": 0.85,
  "clarification_reason": ""
}}

要求：
- confirmed_goal 必须是一句可独立执行的目标描述
- grading_levels 为 2-5 级验收标准（从基础到完善）
- assumptions 为提炼的假设列表（可为空）
- confidence 为你对目标清晰度的自信程度（0.0-1.0）：
  * 0.9-1.0：目标非常清晰，可直接执行
  * 0.7-0.89：基本清晰，可执行但有小的不确定
  * 0.5-0.69：部分模糊，建议追问
  * 0.0-0.49：非常模糊，必须追问
- clarification_reason：若 confidence < 0.7，说明哪些方面不清晰（空字符串若无需追问）

只输出 JSON，不要其他文字。"""

    def _normalize_questions(self, parsed) -> list[dict]:
        """规范化问题列表——最多 5 个，类型合法，id 唯一"""
        if not isinstance(parsed, list):
            return [_DEFAULT_QUESTION]

        questions: list[dict] = []
        seen_ids: set = set()
        for i, q in enumerate(parsed):
            if not isinstance(q, dict):
                continue
            question_text = str(q.get("question", "")).strip()
            if not question_text:
                continue
            qid = str(q.get("id", f"q{i + 1}"))
            if qid in seen_ids:
                qid = f"q{i + 1}"
            seen_ids.add(qid)
            qtype = str(q.get("type", "scope"))
            if qtype not in QUESTION_TYPES:
                qtype = "scope"
            questions.append({"id": qid, "type": qtype, "question": question_text})
            if len(questions) >= _MAX_QUESTIONS:
                break

        return questions if questions else [_DEFAULT_QUESTION]

    def _normalize_refined(self, parsed, qa_list: list[dict]) -> dict:
        """规范化提炼结果——confirmed_goal 必须非空，grading_levels 限 2-5 级

        Task 6.11: 增加 confidence / needs_more_clarification / clarification_reason
        规范化。confidence 由 LLM 自主返回，orchestrator 据此决定是否 auto_followup。
        """
        if not isinstance(parsed, dict):
            return self._fallback_refine(qa_list)

        goal = str(parsed.get("confirmed_goal", "")).strip()
        if not goal:
            return self._fallback_refine(qa_list)

        levels = parsed.get("grading_levels", [])
        if not isinstance(levels, list):
            levels = []
        levels = [str(l).strip() for l in levels if str(l).strip()]
        # 限制 2-5 级
        if len(levels) < 2:
            levels = list(_DEFAULT_GRADING_LEVELS)
        elif len(levels) > 5:
            levels = levels[:5]

        assumptions = parsed.get("assumptions", [])
        if not isinstance(assumptions, list):
            assumptions = []
        assumptions = [str(a).strip() for a in assumptions if str(a).strip()]

        # Task 6.11: confidence 自主判断
        # LLM 显式返回 confidence 时按阈值判断；未返回或解析失败时用中性默认（不追问）
        confidence, has_explicit = self._parse_confidence(parsed.get("confidence"))
        if has_explicit:
            needs_more = confidence < _CONFIDENCE_THRESHOLD
        else:
            # LLM 未显式返回 confidence → 中性默认，不触发自主追问
            confidence = _DEFAULT_CONFIDENCE
            needs_more = False

        reason = str(parsed.get("clarification_reason", "")).strip()
        if needs_more and not reason:
            reason = _DEFAULT_CLARIFICATION_REASON

        return {
            "confirmed_goal": goal,
            "grading_levels": levels,
            "assumptions": assumptions,
            "confidence": confidence,
            "needs_more_clarification": needs_more,
            "clarification_reason": reason,
        }

    @staticmethod
    def _parse_confidence(raw) -> tuple[float, bool]:
        """解析 LLM 返回的 confidence 值

        Returns:
            (confidence, has_explicit)
            - has_explicit=True 表示 LLM 显式返回了可解析的 confidence
            - has_explicit=False 表示缺失或无法解析，调用方应使用默认值且不触发追问
        """
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return _DEFAULT_CONFIDENCE, False
        if val < 0.0:
            return 0.0, True
        if val > 1.0:
            return 1.0, True
        return val, True

    def _fallback_refine(self, qa_list: list[dict]) -> dict:
        """refine_goal 失败兜底——用第一个 Q&A 的回答作为 confirmed_goal

        Task 6.11: 兜底结果 confidence=0.5（中性），needs_more_clarification=False。
        设计理由：兜底目标已是"用户第一个回答"——再追问兜底目标无意义，
        应让 orchestrator 继续推进到 decompose，由用户在后续步骤修正。
        """
        if qa_list:
            first_answer = str(qa_list[0].get("answer", "")).strip()
            if first_answer:
                return {
                    "confirmed_goal": first_answer,
                    "grading_levels": list(_DEFAULT_GRADING_LEVELS),
                    "assumptions": [],
                    "confidence": _DEFAULT_CONFIDENCE,
                    "needs_more_clarification": False,
                    "clarification_reason": "",
                }
        return {
            "confirmed_goal": "请用户确认最终目标",
            "grading_levels": list(_DEFAULT_GRADING_LEVELS),
            "assumptions": [],
            "confidence": _DEFAULT_CONFIDENCE,
            "needs_more_clarification": False,
            "clarification_reason": "",
        }
