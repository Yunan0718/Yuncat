"""services/deliberation_complexity.py —— 复杂度判定 + 角色选择。

从 deliberation_service.py 拆分而来，保留全部功能。
模块级函数以 self 为首参，由 DeliberationService 薄包装委托调用。
"""
from __future__ import annotations

import logging
import re

from config import (
    calc_context_budget,
    DELIBERATION_ROLE_LIST_MAX,
    DELIBERATION_TASK_PROMPT_PREVIEW_CHARS,
)

_log = logging.getLogger("yuncat.deliberation")

COMPLEXITY_SIGNALS = {
    "extreme": ["微服务", "分布式", "从零搭建", "整体迁移", "跨系统", "多端同步"],
    "high": ["设计", "架构", "重构", "权限系统", "数据库设计", "安全审计", "选型评估"],
    "medium": ["新增功能", "优化性能", "集成", "多模块", "重构一个模块"],
    "low": ["修复", "改一行", "加个字段", "写个脚本", "问个问题"],
}

# 复杂度 → 最少角色数
COMPLEXITY_ROLE_COUNTS = {"extreme": 6, "high": 4, "medium": 2, "low": 1}

# 默认角色优先级列表（按讨论通用性排序）——select_roles 从前往后取
DEFAULT_ROLE_ORDER = [
    "architect", "developer", "security", "product",
    "devops", "reviewer", "tester", "backend",
    "frontend", "dba", "performance", "ux",
    "coordinator", "sre", "data_scientist",
    "ml_engineer", "compliance", "tech_writer",
    "mobile", "project_manager",
]


def _get_role_model(self, role_id: str) -> str | None:
    """按角色获取模型级别——flash 用于分析角色，pro 用于构建角色"""
    tier = self.ROLE_MODEL_TIERS.get(role_id, "flash")
    cfg_key = "primary" if tier == "pro" else "secondary"
    return cfg_key


async def _maybe_generate_temp_role(
    self, task_input: str, selected_roles: list[dict],
) -> dict | None:
    """Phase 30: LLM 判断是否需要 DB 中没有的临时角色。

    返回临时角色 dict {name, prompt_template, is_temporary=True} 或 None。
    """
    if not self.agent_service:
        return None

    all_agent_ids = list(self.agent_service.adapters.keys())
    if not all_agent_ids:
        return None

    # Phase 56-P2: 临时角色判断预算按当前模型窗口动态计算
    model_cfg = {}
    try:
        if self.model_config:
            model_cfg = await self.model_config.get_active_config()
    except Exception:
        _log.debug("获取模型配置失败", exc_info=True)
    _task_budget = calc_context_budget(model_cfg, fraction=0.015, min_chars=1000, max_chars=4000)

    role_names = ", ".join(r.get("name", r.get("id", "?")) for r in selected_roles)

    TEMP_ROLE_PROMPT = f"""## 角色：讨论协调员
已有角色: {role_names}

## 任务
{task_input[:_task_budget]}

## 判断
1. 这个任务是否需要已有角色未能覆盖的专业视角？
2. 如果需要——生成一个临时角色(name + 专业背景 + 讨论贡献)，JSON格式
3. 如果已有角色足够，回复 NO

## 输出
如果需要: {{"name":"角色名","prompt_template":"## 角色：xxx\\n## 专业背景\\n...\\n## 任务\\n{{{{task}}}}"}}
如果不需要: NO"""

    try:
        output = await self.agent_service._invoke_with_role(
            all_agent_ids[0], TEMP_ROLE_PROMPT, task_input,
        )
        output = output.strip()
        if not output or output.upper().startswith("NO"):
            return None

        # 提取 JSON
        import json as _json, re
        m = re.search(r'\{.*\}', output, re.DOTALL)
        if not m:
            return None
        data = _json.loads(m.group(0))
        return {
            "name": data.get("name", "临时角色"),
            "prompt_template": data.get("prompt_template", ""),
            "is_temporary": True,
            "category": "temporary",
        }
    except Exception:
        _log.debug("Temp role generation declined or failed")
        return None


def assess_complexity(self, prompt: str) -> dict:
    """复杂度判定——关键词打分 → {level, suggested_roles, effort}

    Phase 22: +effort 字段供 DynamicOrchestrator 使用。
    """
    import re
    score = min(len(prompt) * 0.01, 3.0)
    negated = re.sub(r'(?:不要|不想|不需要|不用|别|要不要|是不是|需不需要|该不该)\S{0,6}(?:微服务|分布式|重构|架构)', '', prompt)
    for level, keywords in COMPLEXITY_SIGNALS.items():
        weight = {"extreme": 3.0, "high": 2.0, "medium": 1.0, "low": 0.5}[level]
        score += sum(weight if kw in negated else 0 for kw in keywords)

    if score >= 12:
        level = "extreme"
    elif score >= 6:
        level = "high"
    elif score >= 2:
        level = "medium"
    else:
        level = "low"

    # Phase 22: effort 映射
    from services.dynamic_orchestrator import complexity_to_effort
    return {
        "level": level, "score": round(score, 1),
        "effort": complexity_to_effort(level),
    }


async def select_roles(self, level: str, db=None, count: int | None = None,
                       task_prompt: str = "") -> list[dict]:
    """根据复杂度级别选择角色。Phase 23: LLM 辅助（task_prompt 非空时）。

    优先级: 按 DEFAULT_ROLE_ORDER 排序，取前 count 个。
    """
    count = count or COMPLEXITY_ROLE_COUNTS.get(level, 1)

    if not db:
        return []

    try:
        from repositories import get_enabled_roles
        all_roles = await get_enabled_roles(db)
    except Exception:
        _log.warning("Failed to load roles from DB, using defaults")
        return []

    if not all_roles:
        return []

    # Phase 23: LLM 辅助选择——"这个任务最需要哪些角色？"
    if task_prompt and self.agent_service and count >= 2:
        try:
            order = await _llm_rank_roles(self, task_prompt, all_roles)
            if order:
                sorted_roles = sorted(all_roles, key=lambda r: order.get(r["id"], 999))
                return sorted_roles[:count]
        except Exception:
            _log.warning("LLM role ranking failed, using default order")

    # 回退：按 DEFAULT_ROLE_ORDER 默认排序
    order_map = {rid: i for i, rid in enumerate(DEFAULT_ROLE_ORDER)}
    sorted_roles = sorted(all_roles, key=lambda r: order_map.get(r["id"], 999))
    return sorted_roles[:count]


async def _llm_rank_roles(self, task_prompt: str, roles: list[dict]) -> dict[str, int]:
    """LLM 对角色进行排序——返回 {role_id: rank}"""
    role_list = "\n".join(f"- {r['id']}: {r.get('description', r['name'])}" for r in roles[:DELIBERATION_ROLE_LIST_MAX])
    prompt = f"""## 任务
{task_prompt[:DELIBERATION_TASK_PROMPT_PREVIEW_CHARS]}

## 可用角色
{role_list}

## 要求
根据任务内容，选出最相关的 {min(len(roles), 5)} 个角色，按重要性从高到低排列。
输出格式（每行一个角色 ID，不要其他内容）：
role_id_1
role_id_2
..."""

    all_agent_ids = list(self.agent_service.adapters.keys())
    if not all_agent_ids:
        return {}
    output = await self.agent_service._invoke_with_role(all_agent_ids[0], "", prompt)

    ranking: dict[str, int] = {}
    for i, line in enumerate(output.strip().split("\n")):
        rid = line.strip()
        if rid in {r["id"] for r in roles}:
            ranking[rid] = i
    return ranking
