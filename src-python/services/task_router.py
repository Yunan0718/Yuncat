"""services/task_router.py — 任务感知五层路由引擎 (Phase 43 R1-R2)

L1 分类 (规则引擎 + LLM 备选) → L2 能力映射 → L3 路由决策
→ L4 缺口注入 → L5 回退。

将现有的 capability_router.route_by_capability() 包装为完整的任务感知路由。
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from config import (
    DEFAULT_AGENT_ID,
    EXTERNAL_AGENT_IDS,
    EXTERNAL_AGENT_AUTO_ROUTE,
)

_log = logging.getLogger("yuncat.task_router")

# ═══════════════════════════════════════════════════════════
# L1: 任务分类 — 规则引擎 (10 模式)
# ═══════════════════════════════════════════════════════════

TASK_PATTERNS: list[tuple[str, str, float]] = [
    (r"今天|星期几|现在几点|当前时间|日期", "realtime_query", 0.95),
    (r"天气|温度|汇率|股价|新闻|搜索.*最新|查一下.*最新", "realtime_query", 0.90),
    (r"写|创建|生成|实现.*代码|开发.*功能|新增.*功能", "code_generation", 0.85),
    (r"分析|审查|review|检查.*代码|审计|code review", "code_review", 0.80),
    (r"图片|截图|照片|图像|视觉|看图|识图|分析.*图|OCR", "image_analysis", 0.95),
    (r"搜|查一下|搜索|找.*文档|查.*官网|搜索.*资料|找.*资料", "web_research", 0.85),
    (r"发.*消息|通知|提醒|发送到|推送|转发", "notification", 0.90),
    (r"运行|执行|跑.*测试|build|编译|启动|部署|npm|pip|git|docker", "command_execution", 0.80),
    (r"删除|重命名|复制|移动|新建.*文件|创建.*文件|mkdir", "file_operation", 0.85),
    (r"修复.*bug|fix|debug|调试|排错|修复.*错误", "bug_fix", 0.80),
    (r"架构|设计.*系统|重构|refactor|拆分.*模块", "architecture", 0.70),
    (r"解释|什么是|为什么|how to|怎么|原理", "general_question", 0.75),
]

# ═══════════════════════════════════════════════════════════
# L2: 任务→能力映射
# ═══════════════════════════════════════════════════════════

TASK_CAPABILITY_MAP: dict[str, list[str]] = {
    "realtime_query":     ["realtime_info", "datetime"],
    "code_generation":    ["code_analysis", "file_ops"],
    "code_review":        ["code_analysis"],
    "image_analysis":     ["vision"],
    "web_research":       ["realtime_info"],
    "notification":       ["messaging"],
    "command_execution":  ["file_ops", "shell"],
    "file_operation":     ["file_ops"],
    "bug_fix":            ["code_analysis", "file_ops", "shell"],
    "architecture":       ["general_reasoning", "code_analysis"],
    "general_question":   ["general_reasoning"],
}

# ═══════════════════════════════════════════════════════════
# L3: 能力注册表 — 每个能力有哪些 Provider
# ═══════════════════════════════════════════════════════════

CAPABILITY_REGISTRY: dict[str, dict[str, Any]] = {
    "realtime_info": {
        # 2026-09-10: 移除 hermes via_delegate 条目——hermes 已下线，
        # 且 openclaw 自身已以更高优先级提供 web_search/web_fetch，该条目只会
        # 让 L4 缺口注入产出「需要 hermes 后端」的误导提示。
        "providers": [
            {"agent": "openclaw", "tool": "web_search", "priority": 1},
            {"agent": "openclaw", "tool": "web_fetch", "priority": 2},
        ],
    },
    "datetime": {
        "exclusive": True,
        "providers": [
            {"agent": "yuncat", "tool": "run_command", "priority": 1,
             "prompt_hint": {"interpreter": "python",
                "description": "获取系统当前日期时间",
                "example": "python -c \"import datetime; print(datetime.datetime.now())\""}},
        ],
    },
    "file_ops": {
        "exclusive": True,
        "providers": [
            {"agent": "yuncat", "tool": "read_file|write_file|list_dir|delete_file|rename_file|mkdir|search_files", "priority": 1},
        ],
    },
    "vision": {
        "exclusive": True,
        "providers": [
            {"agent": "openclaw", "tool": "openclaw_vision", "priority": 1},
        ],
    },
    "code_analysis": {
        "exclusive": True,
        "providers": [
            {"agent": "yuncat",
             "tool": "search_files|codegraph_context|sequential_thinking|read_file", "priority": 1},
        ],
    },
    "messaging": {
        "exclusive": True,
        "providers": [
            {"agent": "openclaw",
             "tool": "telegram_send|discord_send|slack_send|dingtalk_send|wechat_work_send|whatsapp_send",
             "priority": 1},
        ],
    },
    "general_reasoning": {
        "exclusive": True,
        "providers": [
            {"agent": "yuncat", "tool": "sequential_thinking", "priority": 1},
        ],
    },
    "shell": {
        "exclusive": True,
        "providers": [
            {"agent": "yuncat", "tool": "run_command", "priority": 1},
        ],
    },
}

# 能力冲突解决: 两个冲突的能力同时命中 → 选优先级更高的
CONFLICT_RESOLUTION: dict[tuple[str, str], str] = {
    ("code_generation", "file_operation"):   "code_generation",
    ("web_research", "general_question"):    "web_research",
    ("bug_fix", "code_generation"):          "bug_fix",
    ("architecture", "code_generation"):     "architecture",
}


# ═══════════════════════════════════════════════════════════
# Task 7.6: task_type → model_tier 映射（难度→模型分发）
# ═══════════════════════════════════════════════════════════
# 对标 OpenAI model selection / Claude model router——
# 简单任务用小模型（快+便宜），复杂任务用大模型（强+准）。
# tier 值: "light"（小模型）/ "standard"（标准模型）/ "heavy"（大模型）
# 后续可由 CapabilityRouter 反馈学习闭环动态调整（如某类任务成功率低则升级 tier）
TASK_TIER_MAP: dict[str, str] = {
    # light: 简单查询/通知/文件操作——小模型即可（快+省 token）
    "realtime_query":      "light",
    "general_question":    "light",
    "notification":        "light",
    "file_operation":      "light",
    "command_execution":   "light",
    # standard: 常规开发——标准模型（代码生成/review/搜索）
    "code_generation":     "standard",
    "code_review":         "standard",
    "image_analysis":      "standard",
    "web_research":        "standard",
    # heavy: 复杂架构/调试——大模型（强推理）
    "bug_fix":             "heavy",
    "architecture":        "heavy",
}


def resolve_model_tier(task_type: str) -> str:
    """Task 7.6: 解析 task_type → model_tier。

    未知 task_type 保守默认 'standard'（而非 'light'）——
    未知难度按中等处理，避免低估难度导致能力不足。
    """
    return TASK_TIER_MAP.get(task_type, "standard")


@dataclass
class RouteDecision:
    """路由决策结果"""
    agent_id: str
    task_type: str = "general_question"
    confidence: float = 0.5
    pre_inject_prompt: str = ""
    capabilities_needed: list[str] = field(default_factory=list)
    fallback_agents: list[str] = field(default_factory=list)
    reasoning: str = ""
    # Task 7.6: model_tier——难度→模型分发（light/standard/heavy）
    # 对标 OpenAI model selection / Claude model router——
    # 简单任务用小模型（快+便宜），复杂任务用大模型（强+准）
    model_tier: str = "standard"


def classify_task(prompt: str) -> list[tuple[str, float]]:
    """L1: 规则引擎分类。返回 [(task_type, confidence), ...]

    匹配所有命中 pattern 的类型，按置信度降序排列。
    最多返回 3 个匹配——超过 3 个用 CONFLICT_RESOLUTION 解析。
    """
    matches: list[tuple[str, float]] = []
    for pattern, task_type, confidence in TASK_PATTERNS:
        if re.search(pattern, prompt):
            matches.append((task_type, confidence))

    if not matches:
        return [("general_question", 0.3)]

    # 排序: 高置信度优先
    matches.sort(key=lambda x: -x[1])

    # 冲突检测: 如果有 2+ 个 matches，检查 CONFLICT_RESOLUTION
    if len(matches) >= 2:
        primary = matches[0][0]
        for t2, _ in matches[1:]:
            winner = CONFLICT_RESOLUTION.get((primary, t2))
            if winner and winner != primary:
                # 替换 primary 为 winner
                primary = winner
        # 过滤——只保留 winner 和没冲突的
        resolved: list[tuple[str, float]] = []
        for t, c in matches:
            conflict_winner = CONFLICT_RESOLUTION.get((primary, t))
            if conflict_winner == primary or t == primary or t not in [m[0] for m in resolved]:
                resolved.append((t, c))
            if len(resolved) >= 3:
                break
        matches = resolved if resolved else matches[:3]

    return matches[:3]


async def route_task(
    prompt: str,
    adapters: dict,
    db=None,
    default_agent: str = DEFAULT_AGENT_ID,
    allow_external: bool = EXTERNAL_AGENT_AUTO_ROUTE,
) -> RouteDecision:
    """五层路由——任务分类 → 能力映射 → 路由决策 → 缺口注入 → 回退。

    L1: classify_task(prompt) → 任务类型 + 置信度
    L2: TASK_CAPABILITY_MAP[task_type] → 所需能力列表
    L3: 对每种能力，找最佳 Provider——评分 + exclusive 优先
    L4: 如果首选 Agent 缺少某些能力 → pre_inject 路由提示
    L5: 如果所有能力都无法匹配 → 回退到 default_agent

    返回 RouteDecision——agent_id + pre_inject_prompt + fallbacks。
    """
    # L1: 任务分类
    task_matches = classify_task(prompt)
    primary_task, confidence = task_matches[0]
    _log.info("L1 classify: '%s' -> %s (%.2f)", prompt[:80], primary_task, confidence)

    # L2: 能力映射
    caps_needed = TASK_CAPABILITY_MAP.get(primary_task, ["general_reasoning"])

    # 次要任务类型的能力也纳入
    for task_type, _ in task_matches[1:]:
        extra_caps = TASK_CAPABILITY_MAP.get(task_type, [])
        for c in extra_caps:
            if c not in caps_needed:
                caps_needed.append(c)

    # L3: 评分——按能力找最佳 Agent
    scores: dict[str, float] = {}
    for cap_id in caps_needed:
        cap = CAPABILITY_REGISTRY.get(cap_id)
        if not cap:
            continue
        if cap.get("exclusive"):
            # 独占能力——直接给满分
            top_provider = cap["providers"][0]
            agent = top_provider["agent"]
            if agent in adapters:
                scores[agent] = scores.get(agent, 0) + 10
        else:
            for p in cap.get("providers", []):
                agent = p["agent"]
                if agent in adapters:
                    scores[agent] = scores.get(agent, 0) + p.get("priority", 1)

    # Phase 9-X: 外部 Agent 默认不自动路由。
    # 用户未显式选择时，即便某外部 Agent 能力更匹配，也落回原生 Yuncat——
    # 由原生 Agent 通过 delegate_to_agent 工具在确认流下按需委托，绝不静默整任务接管。
    if not allow_external:
        scores = {a: s for a, s in scores.items() if a not in EXTERNAL_AGENT_IDS}

    if not scores:
        best_agent = default_agent
        confidence = 0.3
    else:
        best_agent = max(scores, key=scores.get)
        # 置信度基于得分差距——差距大则置信度高
        second_best = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0
        score_gap = scores[best_agent] - second_best
        confidence = min(0.95, max(0.5, score_gap / max(scores[best_agent], 1)))

    _log.info("L3 route: '%s' -> %s (confidence=%.2f, scores=%s)",
              primary_task, best_agent, confidence, scores)

    # fallback: 所有不在 best_agent 的 adapter
    fallbacks = [a for a in adapters if a != best_agent]

    # L4: 缺口注入——首选 Agent 缺哪项能力？
    pre_inject_lines = []
    for cap_id in caps_needed:
        cap = CAPABILITY_REGISTRY.get(cap_id)
        if not cap:
            continue
        has_own = any(
            p["agent"] == best_agent and not p.get("via_delegate")
            for p in cap.get("providers", [])
        )
        if not has_own:
            # 找备选——从其他 Agent 的能力中
            for p in cap.get("providers", []):
                if p["agent"] != best_agent and p["agent"] in adapters:
                    if p.get("via_delegate"):
                        pre_inject_lines.append(
                            f"- {cap_id} → delegate_to_agent(\"{p['target_agent']}\", \"{p['tool']}\")"
                        )
                    else:
                        pre_inject_lines.append(
                            f"- {cap_id} → 需要 {p['agent']} 后端 ({p['tool']})"
                        )
                    break

    pre_inject = ""
    if pre_inject_lines:
        pre_inject = "## ⚡ 路由提示\n本任务被 Yuncat 路由到 {agent} 后端。"\
            " 以下能力需要跨后端委托:\n".format(agent=best_agent)
        pre_inject += "\n".join(pre_inject_lines)
        pre_inject += "\n\n直接调 delegate_to_agent 工具——不要口头建议。"

    # Task 7.6: 根据 task_type 解析 model_tier（难度→模型分发）
    model_tier = resolve_model_tier(primary_task)

    reasoning = f"L1 classify='{primary_task}'({confidence:.2f}) → L2 caps={caps_needed} → L3 scores={scores} → model_tier={model_tier}"

    return RouteDecision(
        agent_id=best_agent,
        task_type=primary_task,
        confidence=confidence,
        pre_inject_prompt=pre_inject,
        capabilities_needed=caps_needed,
        fallback_agents=fallbacks,
        reasoning=reasoning,
        model_tier=model_tier,
    )
