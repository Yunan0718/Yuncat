"""services/long_task/analysis_agent.py —— Task 6.10 分析阶段 Agent

长任务生命周期的第二步（clarifying → analyzing → decomposing）：
在目标确认后、分解前，对任务进行 5 维度分析，输出结构化报告。

5 维度（参考 MetaGPT RequirementsAnalysis / OpenHands task analysis 主流做法）：
1. complexity    —— 复杂度评估（level + factors）
2. dependencies  —— 依赖关系（external/internal/blockers）
3. risks         —— 风险评估（items + severity + mitigation）
4. resources     —— 资源需求（tech_stack/tools/skills）
5. acceptance    —— 验收标准（criteria + test_points）

使用 flash 模型，max_tokens=800。LLM 失败有规则兜底（基于 confirmed_goal 生成简报）。

设计决策：
- 不新建 DB 表——报告持久化到 task_state.long_task.analysis_report JSON 子节点
  （对齐 _LongTaskStateStore 模式，避免 schema 迁移成本）
- WS 事件 analysis_started / analysis_report 推送给前端，前端可展示
- best-effort：分析失败不阻塞主流程，fallback 后继续 decomposing
"""
import json
import logging
from typing import Any

from config import DEFAULT_AGENT_ID

logger = logging.getLogger("yuncat.long_task")

# 常量
_MAX_TOKENS = 800
_TEMPERATURE = 0.3

# 兜底报告（LLM 失败时返回——基于 confirmed_goal 的最小可用分析）
def _fallback_report(confirmed_goal: str) -> dict[str, Any]:
    return {
        "complexity": {
            "level": "moderate",
            "summary": f"基于目标「{confirmed_goal[:80]}」的复杂度评估",
            "factors": ["需进一步分解后确认"],
        },
        "dependencies": {
            "external": [],
            "internal": [],
            "blockers": [],
        },
        "risks": {
            "items": [],
            "summary": "风险待分解后评估",
        },
        "resources": {
            "tech_stack": [],
            "tools": [],
            "skills": [],
        },
        "acceptance": {
            "criteria": [confirmed_goal],
            "test_points": [],
        },
        "_fallback": True,  # 标记为兜底报告，供前端展示降级提示
    }


class AnalysisAgent:
    """分析阶段 Agent——长任务分解前的 5 维度分析

    Task 6.10：在 clarifying 和 decomposing 之间插入 analyzing phase，
    生成结构化分析报告，供后续 decomposer 和 executor 参考。
    """

    def __init__(self, adapters: dict, model_config):
        self._adapters = adapters
        self._model_config = model_config

    async def analyze(
        self,
        confirmed_goal: str,
        original_prompt: str = "",
        agent_id: str = DEFAULT_AGENT_ID,
    ) -> dict[str, Any]:
        """生成 5 维度分析报告

        Args:
            confirmed_goal: 已确认的目标（refine_goal 输出）
            original_prompt: 用户原始任务描述（供分析参考）
            agent_id: 使用的 Agent

        Returns:
            {
                "complexity": {"level", "summary", "factors": [...]},
                "dependencies": {"external": [...], "internal": [...], "blockers": [...]},
                "risks": {"items": [{"description", "severity", "mitigation"}], "summary"},
                "resources": {"tech_stack": [...], "tools": [...], "skills": [...]},
                "acceptance": {"criteria": [...], "test_points": [...]},
            }
            LLM 失败时返回 _fallback_report（含 _fallback: True 标记）
        """
        if not confirmed_goal:
            return _fallback_report(original_prompt or "未提供目标")

        adapter = self._adapters.get(agent_id)
        if adapter is None:
            return _fallback_report(confirmed_goal)

        try:
            cfg = await self._model_config.get_active_config(agent_id)
        except Exception:
            return _fallback_report(confirmed_goal)

        prompt = self._build_analysis_prompt(confirmed_goal, original_prompt)
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
            logger.warning("analysis_agent.invoke failed, using fallback", exc_info=True)
            return _fallback_report(confirmed_goal)

        if "error" in result:
            logger.warning("analysis_agent.invoke returned error: %s", result.get("error"))
            return _fallback_report(confirmed_goal)

        content = result.get("content", "")
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError, TypeError):
            logger.warning("analysis_agent LLM output not valid JSON, using fallback")
            return _fallback_report(confirmed_goal)

        return self._normalize_report(parsed, confirmed_goal)

    # ── 内部辅助 ──

    def _build_analysis_prompt(self, confirmed_goal: str, original_prompt: str) -> str:
        """构建 5 维度分析的 prompt"""
        original_section = ""
        if original_prompt and original_prompt != confirmed_goal:
            original_section = f"\n## 原始任务描述\n{original_prompt}\n"

        return f"""你是任务分析专家。对以下确认目标进行 5 维度分析，输出结构化报告。

## 确认目标
{confirmed_goal}{original_section}

## 5 维度定义
1. complexity —— 复杂度评估：level（simple/moderate/complex）+ factors（关键复杂度因素）
2. dependencies —— 依赖关系：external（外部库/服务）+ internal（内部模块）+ blockers（阻塞项）
3. risks —— 风险评估：items（每项含 description/severity(high/medium/low)/mitigation）
4. resources —— 资源需求：tech_stack（技术栈）+ tools（工具）+ skills（技能）
5. acceptance —— 验收标准：criteria（成功标准）+ test_points（可验证点）

## 输出格式（严格 JSON）
{{
  "complexity": {{
    "level": "moderate",
    "summary": "一句话复杂度总结",
    "factors": ["因素1", "因素2"]
  }},
  "dependencies": {{
    "external": ["外部依赖1"],
    "internal": ["内部模块1"],
    "blockers": ["阻塞项1"]
  }},
  "risks": {{
    "items": [
      {{"description": "风险描述", "severity": "medium", "mitigation": "缓解措施"}}
    ],
    "summary": "一句话风险总结"
  }},
  "resources": {{
    "tech_stack": ["技术1"],
    "tools": ["工具1"],
    "skills": ["技能1"]
  }},
  "acceptance": {{
    "criteria": ["验收标准1"],
    "test_points": ["验证点1"]
  }}
}}

要求：
- 基于目标内容分析，不要臆造不存在的依赖
- 每个维度至少 1 个条目（可为空数组但字段必须存在）
- severity 只能是 high/medium/low

只输出 JSON，不要其他文字。"""

    def _normalize_report(
        self, parsed: Any, confirmed_goal: str,
    ) -> dict[str, Any]:
        """规范化 LLM 输出——确保 5 维度结构完整"""
        if not isinstance(parsed, dict):
            return _fallback_report(confirmed_goal)

        def _ensure_list(val) -> list:
            if not isinstance(val, list):
                return []
            return [str(x).strip() for x in val if str(x).strip()]

        def _ensure_str(val, default: str = "") -> str:
            s = str(val).strip() if val is not None else ""
            return s or default

        def _extract_section(name: str) -> dict:
            """提取某维度子 dict（非 dict 则空 dict）"""
            sec = parsed.get(name, {})
            return sec if isinstance(sec, dict) else {}

        # complexity
        complexity_raw = _extract_section("complexity")
        level = str(complexity_raw.get("level", "moderate")).strip().lower()
        if level not in ("simple", "moderate", "complex"):
            level = "moderate"

        # dependencies
        deps_raw = _extract_section("dependencies")

        # risks.items 规范化（每项含 description/severity/mitigation）
        risks_raw = _extract_section("risks")
        risk_items_raw = risks_raw.get("items", [])
        if not isinstance(risk_items_raw, list):
            risk_items_raw = []
        risk_items: list[dict] = []
        for item in risk_items_raw:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity", "medium")).strip().lower()
            if severity not in ("high", "medium", "low"):
                severity = "medium"
            risk_items.append({
                "description": _ensure_str(item.get("description"), "未描述风险"),
                "severity": severity,
                "mitigation": _ensure_str(item.get("mitigation"), "无缓解措施"),
            })

        # resources / acceptance
        resources_raw = _extract_section("resources")
        acceptance_raw = _extract_section("acceptance")

        return {
            "complexity": {
                "level": level,
                "summary": _ensure_str(complexity_raw.get("summary"), f"复杂度：{level}"),
                "factors": _ensure_list(complexity_raw.get("factors")),
            },
            "dependencies": {
                "external": _ensure_list(deps_raw.get("external")),
                "internal": _ensure_list(deps_raw.get("internal")),
                "blockers": _ensure_list(deps_raw.get("blockers")),
            },
            "risks": {
                "items": risk_items,
                "summary": _ensure_str(risks_raw.get("summary"), f"{len(risk_items)} 项风险"),
            },
            "resources": {
                "tech_stack": _ensure_list(resources_raw.get("tech_stack")),
                "tools": _ensure_list(resources_raw.get("tools")),
                "skills": _ensure_list(resources_raw.get("skills")),
            },
            "acceptance": {
                "criteria": _ensure_list(acceptance_raw.get("criteria")),
                "test_points": _ensure_list(acceptance_raw.get("test_points")),
            },
        }


# ── 全局单例（对齐 Phase 78/79/80 模式）──
_analysis_agent: AnalysisAgent | None = None


def get_analysis_agent() -> AnalysisAgent | None:
    """路由层用——获取全局 AnalysisAgent 单例"""
    return _analysis_agent


def set_analysis_agent(agent: AnalysisAgent) -> None:
    """lifespan 用——注入全局单例"""
    global _analysis_agent
    _analysis_agent = agent
