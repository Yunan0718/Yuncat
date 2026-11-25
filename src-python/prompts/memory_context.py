"""prompts/memory_context.py —— P5: Memory 上下文注入模板 + Dream 整合提示词"""
from __future__ import annotations

__version__ = "1.0.0"

import re

from config import (
    MEMORY_CONTEXT_ITEM_MIN_CHARS,
    MEMORY_CONTEXT_PREVIEW_CHARS,
    MEMORY_CONTEXT_SESSION_BUDGET_FRACTION,
    MEMORY_SEMANTIC_MAX,
    MEMORY_SEMANTIC_PREVIEW_CHARS,
    MEMORY_TREE_RESULT_CONTENT_CHARS,
    MEMORY_TREE_RESULT_TITLE_CHARS,
    MEMORY_TREE_TAGS_MAX,
)
from text_utils import truncate_with_pointer

MEMORY_CONTEXT_TEMPLATE = """## 对话历史（最近 {turn_count} 轮）
{session_context}

## 相关记忆与知识
{results}

## 对话摘要
{summary}"""


def _dedup_by_id(items: list[dict]) -> list[dict]:
    """按 id 去重，保留第一次出现（调用方已按分数排序）。"""
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        rid = item.get("id")
        if rid and rid in seen:
            continue
        if rid:
            seen.add(rid)
        out.append(item)
    return out


def _content_key(text: str | None) -> str:
    """用于跨源内容去重的归一化 key。"""
    return re.sub(r"\s+", " ", str(text).lower().strip())[:200]


def _build_combined_results(
    memories: list[dict] | None,
    knowledge: list[dict] | None,
) -> list[tuple[str, dict]]:
    """合并 memories 与 knowledge，按 id 去重 + 跨源内容去重，按分数降序。

    返回 (kind, item) 列表，kind 为 "memory" 或 "knowledge"。
    """
    memories = _dedup_by_id(memories or [])
    knowledge = _dedup_by_id(knowledge or [])
    seen_content: set[str] = set()
    candidates: list[tuple[float, str, dict]] = []
    for item in memories:
        candidates.append((float(item.get("score", 0.0)), "memory", item))
    for item in knowledge:
        score = float(item.get("final_score", item.get("score", 0.0)))
        candidates.append((score, "knowledge", item))
    candidates.sort(key=lambda x: x[0], reverse=True)

    out: list[tuple[str, dict]] = []
    for _score, kind, item in candidates:
        key = _content_key(item.get("content"))
        if key and key in seen_content:
            continue
        if key:
            seen_content.add(key)
        out.append((kind, item))
    return out


def _format_memory_item(item: dict, content_budget: int) -> str:
    """格式化语义记忆条目。"""
    content = truncate_with_pointer(
        item.get("content", ""), content_budget, item.get("id"),
    )
    return f"- [{item.get('memory_type', 'fact')}] {content}"


def _format_knowledge_item(item: dict, content_budget: int) -> str:
    """格式化知识树条目。"""
    title = str(item.get("title", ""))[:MEMORY_TREE_RESULT_TITLE_CHARS]
    tags = item.get("tags", []) or []
    tags_str = (
        f" [{', '.join(str(t) for t in tags[:MEMORY_TREE_TAGS_MAX])}]"
        if tags else ""
    )
    src_label = "🔍语义" if "vector" in item.get("sources", []) else "📝关键词"
    score = float(item.get("final_score", item.get("score", 0.0)))
    head = f"- **{title}**{tags_str} {src_label} (评分: {score:.2f})"
    content = truncate_with_pointer(
        item.get("content", ""), content_budget, item.get("id"),
    )
    if not content:
        return head
    return f"{head}\n  {content}"


def _format_session_context(
    session_context: list[dict], total_budget: int | None,
) -> str:
    """格式化对话历史，受 total_budget 时动态压缩单条预览。"""
    if not session_context:
        return "（新对话）"
    preview = MEMORY_CONTEXT_PREVIEW_CHARS
    if total_budget is not None:
        session_budget = min(
            len(session_context) * MEMORY_CONTEXT_PREVIEW_CHARS,
            int(total_budget * MEMORY_CONTEXT_SESSION_BUDGET_FRACTION),
        )
        preview = max(20, session_budget // max(1, len(session_context)))
        preview = min(preview, MEMORY_CONTEXT_PREVIEW_CHARS)
    return "\n".join(
        f"{'用户' if m.get('role') == 'user' else 'Agent'}: "
        f"{truncate_with_pointer(m.get('content', ''), preview)}"
        for m in session_context
    )


def _compute_item_budget(
    total_budget: int | None,
    session_context_len: int,
    result_count: int,
) -> tuple[int, int]:
    """根据总预算计算每条结果的内容预算与可返回结果数。"""
    if total_budget is None or result_count <= 0:
        return MEMORY_SEMANTIC_PREVIEW_CHARS, result_count
    session_budget = min(
        session_context_len * MEMORY_CONTEXT_PREVIEW_CHARS,
        int(total_budget * MEMORY_CONTEXT_SESSION_BUDGET_FRACTION),
    )
    # 100 为模板固定开销的保守估计
    available = max(0, total_budget - session_budget - 100)
    max_results = max(1, available // MEMORY_CONTEXT_ITEM_MIN_CHARS)
    capped = min(result_count, max_results)
    if capped <= 0:
        return MEMORY_CONTEXT_ITEM_MIN_CHARS, 0
    per_item = available // capped
    per_item = max(
        MEMORY_CONTEXT_ITEM_MIN_CHARS,
        min(per_item, MEMORY_SEMANTIC_PREVIEW_CHARS, MEMORY_TREE_RESULT_CONTENT_CHARS),
    )
    return per_item, capped


def render_memory_context(
    session_context: list[dict],
    semantic_results: list[dict],
    summary: str,
    *,
    knowledge_results: list[dict] | None = None,
    total_budget: int | None = None,
) -> str:
    """渲染记忆上下文注入文本。

    Batch 2.7: 支持 knowledge_results 与 total_budget，实现 memories / knowledge tree
    之间的按 id 去重、跨源内容去重，以及按模型窗口动态计算的统一截断。
    """
    combined = _build_combined_results(semantic_results, knowledge_results)
    per_item_budget, max_results = _compute_item_budget(
        total_budget, len(session_context), len(combined),
    )
    combined = combined[:max_results]

    lines: list[str] = []
    for kind, item in combined:
        if kind == "memory":
            lines.append(_format_memory_item(item, per_item_budget))
        else:
            lines.append(_format_knowledge_item(item, per_item_budget))

    session_text = _format_session_context(session_context, total_budget)
    results_text = "\n".join(lines) if lines else "（无相关记忆）"

    return MEMORY_CONTEXT_TEMPLATE.format(
        turn_count=len(session_context),
        session_context=session_text,
        results=results_text,
        summary=summary or "（无摘要）",
    )


# v41: Auto-Dream 整合提示词
DREAM_CONSOLIDATE_PROMPT = """整合以下记忆片段。规则：
1. 合并重复或高度相似的事实——保留置信度最高的版本
2. 将模糊表述固化为确定事实（"可能"→"确认"、"应该"→"是"）
3. 丢弃已过时的信息（如"当前版本是 1.2"已被新版替代）
4. 标记相互冲突的事实对——列出冲突点，不自行裁决
5. 输出不超过 500 字的整合结果，中文。

## 记忆片段
{facts}

## 整合结果"""
