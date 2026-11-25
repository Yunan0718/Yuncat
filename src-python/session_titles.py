"""src-python/session_titles.py —— 会话标题策略（纯函数，无 IO）

对标 deepseek-harness 的 `session-title` 能力缝（packages/session/session-title）：

1. 标题是**最新生效值**（latest-wins），不是每次查询重新派生的临时值；
2. 用户手动命名会**置顶**（source=user），自动生成不得覆盖；
3. 没有模型可用时也必须有确定性回退（fallback），不能出现空标题；
4. 模型润色（provider）是可选增强，失败时回退标题继续有效。

为什么放在根级模块（与 config.py / text_utils.py 同层）：
repositories 层要复用同一份派生规则，services / routes 再往上复用同一份实现。
放进 services/ 会迫使 repositories 反向依赖 services（分层倒置），
两处各写一份派生逻辑又会出现"接口算一次、列表算一次"的漂移（AGENTS 规则 10）。
"""
from __future__ import annotations

import re

# 标题来源：user=用户命名并置顶；fallback=首条消息派生；provider=模型润色
SOURCE_USER = "user"
SOURCE_FALLBACK = "fallback"
SOURCE_PROVIDER = "provider"

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ELLIPSIS = "…"


def normalize_title(raw: str | None, max_chars: int) -> str:
    """清洗标题候选：去代码块与控制字符 → 折叠空白 → 截断。

    max_chars <= 0 表示不限制长度。
    """
    if not raw:
        return ""
    text = _CODE_FENCE_RE.sub(" ", str(raw))
    text = _CONTROL_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def derive_fallback_title(message: str | None, max_chars: int) -> str:
    """从首条用户消息派生确定性标题（无模型也能用）。"""
    text = normalize_title(message, 0)
    if not text or max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    clipped = text[: max_chars - 1].rstrip()
    # 英文按词边界切，避免把单词截成半截；中文没有空格，直接切。
    space = clipped.rfind(" ")
    if space >= max_chars // 2:
        clipped = clipped[:space].rstrip()
    return (clipped + _ELLIPSIS) if clipped else _ELLIPSIS


def resolve_title(
    stored: str | None,
    first_message: str | None,
    stored_max_chars: int,
    fallback_max_chars: int,
) -> str:
    """标题生效优先级：已存标题（用户命名/模型润色） > 首条消息派生。

    已存标题为空串时视为"没有标题"，回退到派生规则——
    这样老库（迁移前只有 conversations）也能拿到可读标题。
    """
    normalized = normalize_title(stored, stored_max_chars)
    if normalized:
        return normalized
    return derive_fallback_title(first_message, fallback_max_chars)
