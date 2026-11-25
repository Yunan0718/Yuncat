"""services/model_router.py —— 模型分级路由

根据任务特征（prompt、工具名、上下文、MAX 开关）选择模型 tier，
并封装带 fallback 的模型调用。

v4 第一阶段使用规则评估复杂度，避免额外 LLM 调用带来的延迟和成本。
后续可替换为 LLM-based classify_complexity。
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Awaitable, Callable, TypeVar

_log = logging.getLogger("yuncat.model_router")


class TaskComplexity(str, Enum):
    LITE = "lite"
    NORMAL = "normal"
    MAX = "max"


# 视觉任务强信号：用户消息中包含图片/截图/附件等
_VISION_KEYWORDS = [
    "图片", "截图", "照片", "图像", "看图", "分析图", "vision",
    "image", "screenshot", "picture", "photo", "diagram", "chart",
]

# MAX 级任务强信号
_MAX_KEYWORDS = [
    "架构", "重构", "拆分", "设计", "复杂调试", "性能优化",
    "安全审计", "漏洞", "注入", "并发", "分布式", "微服务",
    "architecture", "refactor", "redesign", "complex debug", "performance",
    "security audit", "vulnerability", "injection", "concurrency",
]

# LITE 级任务强信号
_LITE_KEYWORDS = [
    "读取", "查看", "搜索", "列表", "目录", "文档", "解释",
    "read", "search", "list", "directory", "doc", "explain", "what is",
    "how to", "介绍一下",
]

# 工具名强信号（仅作参考，不硬编码）
_COMPLEXITY_TOOL_HINTS = {
    "max": {"architecture_design", "complex_debug", "multi_file_refactor"},
    "lite": {"read_file", "search_files", "list_dir", "docs_lookup", "web_search"},
}

T = TypeVar("T")


def is_vision_task(prompt: str, conversation_context: dict | None = None) -> bool:
    """启发式判断是否为视觉任务。"""
    text = (prompt or "").lower()
    # 检查附件/图片标记
    if conversation_context:
        attachments = conversation_context.get("attachments", [])
        for att in attachments:
            mime = (att.get("mime_type") or "").lower()
            if mime.startswith("image/"):
                return True
    return any(kw.lower() in text for kw in _VISION_KEYWORDS)


def classify_complexity(prompt: str, conversation_context: dict | None = None) -> float:
    """返回 0~1 的复杂度分数，分数越高越复杂。

    v4 第一阶段使用关键词 + 上下文长度启发式，无 LLM 调用。
    """
    text = (prompt or "").lower()
    score = 0.5  # 默认中等

    # 1. prompt 长度：超长提示更可能复杂
    length = len(prompt or "")
    if length > 4000:
        score += 0.15
    elif length < 200:
        score -= 0.1

    # 2. 关键词
    max_hits = sum(1 for kw in _MAX_KEYWORDS if kw.lower() in text)
    lite_hits = sum(1 for kw in _LITE_KEYWORDS if kw.lower() in text)
    if max_hits:
        score += 0.15 * min(max_hits, 3)
    if lite_hits:
        score -= 0.08 * min(lite_hits, 3)

    # 3. 多文件引用/代码块暗示常规编码任务
    if "```" in text or "@file:" in text:
        score += 0.05

    return max(0.0, min(1.0, score))


async def route_model_tier(
    prompt: str,
    tool_name: str = "",
    conversation_context: dict | None = None,
    max_mode_enabled: bool = False,
    capability_level: int = 1,
) -> str:
    """根据 prompt + 上下文选择模型 tier。

    返回 'lite' | 'normal' | 'max' | 'vision'。
    capability_level: 0=轻量(lite 偏向) / 1=标准(默认) / 2=强力(max_mode)。
    """
    if max_mode_enabled:
        return "max"

    # 1. 视觉任务强制 Vision（实际模型选择时仍需检查是否支持 vision）
    if is_vision_task(prompt, conversation_context):
        return "vision"

    # 2. 工具名强信号
    if tool_name in _COMPLEXITY_TOOL_HINTS["max"]:
        return "max"
    if tool_name in _COMPLEXITY_TOOL_HINTS["lite"]:
        return "lite"

    # 3. 启发式复杂度评分
    score = classify_complexity(prompt, conversation_context)
    if score >= 0.8:
        return "max"
    if score <= 0.3:
        return "lite"
    # 轻量档(0): 标准复杂度区间不升到 normal，保持 lite
    if capability_level == 0:
        return "lite"
    return "normal"


class AllModelsUnavailableError(Exception):
    """tier 内及 fallback 链所有模型均不可用。"""

    def __init__(self, tier: str, tried: list[tuple[str, str]]):
        self.tier = tier
        self.tried = tried
        super().__init__(f"All models unavailable for tier '{tier}': {tried}")


# ════════════════════════════════════════════════════════════
# 长任务步骤 tier 推断
# ════════════════════════════════════════════════════════════

# 中英双语关键词，避免 decompose prompt 语言切换后匹配失效
ROLE_HINTS = {
    "scaffold": ["创建目录", "初始化", "mkdir", "scaffold", "create directory", "init"],
    "read_only": ["读取", "分析", "理解", "review existing", "read", "analyze", "inspect"],
    "key_implementation": [
        "实现", "核心", "认证", "中间件", "加密",
        "implement", "core", "auth", "middleware", "encrypt",
    ],
    "security": ["安全", "审计", "漏洞", "注入", "security", "audit", "vulnerability", "injection"],
    "architecture": ["架构", "设计", "重构", "拆分", "architecture", "design", "refactor", "split"],
    "testing": ["测试", "pytest", "单元测试", "test", "unit test"],
    "documentation": ["README", "文档", "注释", "documentation", "comment"],
}


def _infer_step_role(text: str) -> str:
    """根据 title/description 关键词推断 step_role。"""
    lowered = (text or "").lower()
    scores: dict[str, int] = {}
    for role, hints in ROLE_HINTS.items():
        scores[role] = sum(1 for h in hints if h.lower() in lowered)
    if not scores or max(scores.values()) == 0:
        return "implementation"
    return max(scores, key=scores.get)


def _resolve_step_tier_from_role(step_role: str) -> str:
    """step_role -> tier 映射。"""
    if step_role in ("scaffold", "read_only", "documentation"):
        return "lite"
    if step_role in ("key_implementation", "security", "architecture"):
        return "max"
    return "normal"


def infer_step_tier(text: str) -> str:
    """根据步骤 title/description 推断应使用的模型 tier。

    对外暴露的便捷入口：先推断 step_role，再映射到 tier。
    """
    return _resolve_step_tier_from_role(_infer_step_role(text))


async def call_with_fallback(
    tier: str,
    resolve_model: Callable[[str, list[tuple[str, str]]], Awaitable[dict | None]],
    make_request: Callable[[dict], Awaitable[T]],
    is_model_unavailable: Callable[[Exception], bool],
) -> T:
    """选择模型并带 fallback 调用。

    流程：
      1. 取 tier 内第一个模型。
      2. 执行 make_request。
      3. 若抛出的异常被 is_model_unavailable 判定为模型不可用，
         标记该模型为本次不可用，切换到 fallback 链中的下一个模型。
      4. 所有模型都失败时抛出 AllModelsUnavailableError。

    resolve_model(tier, excluded) -> dict | None：由调用方注入，
    通常是 ModelConfigService.resolve_model_for_task 的包装。
    """
    tried: list[tuple[str, str]] = []
    current_tier = tier
    # 简单 fallback：当前 tier 失败后降到 normal（如还不是 normal）
    tiers_to_try = [current_tier]
    if current_tier != "normal":
        tiers_to_try.append("normal")
    if current_tier not in ("lite", "normal"):
        tiers_to_try.append("lite")

    for t in tiers_to_try:
        while True:
            model_cfg = await resolve_model(t, tried)
            if not model_cfg:
                break
            key = (model_cfg.get("provider", ""), model_cfg.get("model", ""))
            if key in tried or not key[0] or not key[1]:
                break
            tried.append(key)
            try:
                return await make_request(model_cfg)
            except Exception as e:
                if is_model_unavailable(e):
                    _log.warning(
                        "Model %s/%s unavailable in tier %s: %s",
                        key[0], key[1], t, e,
                    )
                    continue
                raise

    raise AllModelsUnavailableError(tier, tried)
