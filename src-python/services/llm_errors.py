"""services/llm_errors.py —— LLM 错误分类器（DeepSeek Harness 借鉴）

来源：@deepseek-ai/dsh-llm/error（DeepSeek Harness 的 LLM 错误层）。
核心原则：**route on code, never parse message** —— 用稳定的机器码分类错误，
而不是靠解析人类可读的 message 字符串。

背景：Yuncat 有双后端（Hermes/OpenClaw）+ 17 个 provider
（DeepSeek/OpenAI/Claude/GLM/Qwen/...），各 provider 对同一类错误的措辞不一致：
- OpenAI: "maximum context length exceeded"
- DeepSeek: "input is too long for this model"
- 通用: "insufficient quota" / "out of credits"

本模块用正则分类器统一识别，供上层按 code 处理：
- CONTEXT_WINDOW_EXCEEDED → 触发上下文压缩 / 提示截断
- QUOTA_EXCEEDED → 提示用户配额耗尽（充值/换 key）
"""
from __future__ import annotations

import re

# 稳定错误码（对标 DSH canonical codes）——上层 route 用，永不解析 message
CONTEXT_WINDOW_EXCEEDED = "CONTEXT_WINDOW_EXCEEDED"
QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
UNKNOWN = "UNKNOWN"
# 2026-09-10（对标 dsh httpErrorCode）：状态码优先的机器码——上层按码决策，不解析文案。
AUTH = "AUTH"
INVALID_REQUEST = "INVALID_REQUEST"
RATE_LIMIT = "RATE_LIMIT"
SERVER = "SERVER"
EMPTY_RESPONSE = "EMPTY_RESPONSE"
TRANSPORT = "TRANSPORT"

# 结构化措辞：显式命名 "context length/window" + 超出动词
_STRUCTURED_CONTEXT_OVERFLOW = re.compile(
    r"(?:^|[^a-z0-9])context[\s_-](?:length|window)[\s_-]"
    r"(?:exceed(?:ed|s)?|overflow(?:ed)?|limit[\s_-]exceeded)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)

# "too large/long for [the] model context [window]"
_TOO_LARGE_FOR_CONTEXT = re.compile(
    r"\b(?:request|prompt|input|messages?)\s+(?:is\s+|are\s+)?"
    r"too\s+(?:large|long)\s+for\s+(?:(?:this|the)\s+)?"
    r"(?:model(?:'s)?\s+)?context(?:\s+window)?\b",
    re.IGNORECASE,
)

# "exceeds/overflows/is larger than" 仅当其宾语显式是 model context
_EXCEEDS_MODEL_CONTEXT = re.compile(
    r"\b(?:input|prompt|request|messages?)\b.{0,40}"
    r"\b(?:exceed(?:s|ed)?|overflows?|is\s+larger\s+than)\b.{0,40}"
    r"\b(?:the\s+)?(?:model(?:'s)?\s+)?context(?:\s+(?:length|window))?\b",
    re.IGNORECASE,
)

# "maximum/max [allowed|supported] context length/window"
_MAX_CONTEXT_WINDOW = re.compile(
    r"\b(?:maximum|max)(?:\s+(?:allowed|supported))?\s+context\s+(?:length|window)\b",
    re.IGNORECASE,
)

# "input/prompt/request too long/large for this/the model"
_TOO_LONG_FOR_MODEL = re.compile(
    r"\b(?:input|prompt|request)\s+(?:is\s+)?too\s+(?:long|large)\s+for\s+(?:this|the)\s+model\b",
    re.IGNORECASE,
)


def is_context_window_exceeded_error(detail: str) -> bool:
    """识别 provider 措辞中的「上下文窗口超限」错误。

    :param detail: 拼接后的 provider 错误文本（error code + type + message）。
    :returns: 是否属于 context window exceeded。
    """
    if not detail:
        return False
    return (
        _STRUCTURED_CONTEXT_OVERFLOW.search(detail) is not None
        or _MAX_CONTEXT_WINDOW.search(detail) is not None
        or _TOO_LARGE_FOR_CONTEXT.search(detail) is not None
        or _TOO_LONG_FOR_MODEL.search(detail) is not None
        or _EXCEEDS_MODEL_CONTEXT.search(detail) is not None
    )


def is_quota_exceeded_error(detail: str) -> bool:
    """识别「配额/余额耗尽」错误（区别于瞬时的限流）。

    :param detail: 拼接后的 provider 错误文本。
    :returns: 是否属于终态配额耗尽（quota/balance/credits/budget）。
    """
    if not detail:
        return False
    return (
        re.search(r"\binsufficient[\s_-]+(?:quota|balance|credits?)\b", detail, re.IGNORECASE) is not None
        or re.search(r"\b(?:quota|usage[\s_-]+limit)[\s_-]+(?:exceeded|exhausted|reached)\b", detail, re.IGNORECASE) is not None
        or re.search(r"\bexceed(?:ed|s)?[\s_-]+(?:(?:your|the)[\s_-]+)?(?:current[\s_-]+)?quota\b", detail, re.IGNORECASE) is not None
        or re.search(r"\b(?:balance|credits?)[\s_-]+(?:exhausted|depleted)\b", detail, re.IGNORECASE) is not None
        or re.search(r"\bout[\s_-]+of[\s_-]+(?:credits?|budget)\b", detail, re.IGNORECASE) is not None
    )


def classify_llm_error(detail: str) -> str:
    """将 provider 错误文本归类为稳定错误码。

    :param detail: 拼接后的 provider 错误文本（code/type/message 拼接成一段）。
    :returns: 稳定错误码（CONTEXT_WINDOW_EXCEEDED / QUOTA_EXCEEDED / UNKNOWN）。
    """
    if is_context_window_exceeded_error(detail):
        return CONTEXT_WINDOW_EXCEEDED
    if is_quota_exceeded_error(detail):
        return QUOTA_EXCEEDED
    return UNKNOWN


# ═══════════════════════════════════════════════════════════════
# 2026-09-10（借鉴 deepseek-harness）：状态码优先分类 + 重试事实 + 请求 id
# 原则：**先用 HTTP 状态定码**，文案分类只用于细化 400/429（如区分"上下文超限"与
# 普通参数错误）。同时捕获 provider 的重试提示（Retry-After）与请求 id，前者决定
# 退避时长，后者是排障/反馈时唯一能对上服务端日志的线索。
# ═══════════════════════════════════════════════════════════════

def code_for_status(status: int, detail: str = "") -> str:
    """把 HTTP 状态映射为稳定错误码（对标 dsh httpErrorCode）。

    400 与 429 用文案分类细化：上下文超限 / 配额耗尽优先于笼统的 INVALID_REQUEST。
    """
    if status in (401, 403):
        return AUTH
    if status == 413:
        return INVALID_REQUEST
    if is_quota_exceeded_error(detail):
        return QUOTA_EXCEEDED
    if status == 429:
        return RATE_LIMIT
    if status == 400:
        if is_context_window_exceeded_error(detail):
            return CONTEXT_WINDOW_EXCEEDED
        return INVALID_REQUEST
    if status >= 500:
        return SERVER
    return f"HTTP_{status}"


def parse_retry_after_ms(value: str | None) -> int | None:
    """解析 Retry-After：秒数或 HTTP-date，返回毫秒；无法解析/已过期返回 None。"""
    if not value:
        return None
    raw = str(value).strip()
    if raw.isdigit():
        delay = int(raw) * 1000
        return delay if delay > 0 else None
    from email.utils import parsedate_to_datetime
    from datetime import datetime, timezone

    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delay = int((when - datetime.now(timezone.utc)).total_seconds() * 1000)
    return delay if delay > 0 else None


def request_id_from_headers(headers) -> str | None:
    """从响应头取 provider 请求 id（排障用）。支持 dict 与 aiohttp CIMultiDict。"""
    if not headers:
        return None
    for key in ("x-request-id", "x-deepseek-request-id", "request-id"):
        try:
            value = headers.get(key)
        except AttributeError:
            return None
        if value:
            return str(value)
    return None


def error_facts(status: int, headers, body_text: str = "") -> dict:
    """把一次失败的 HTTP 响应归一为结构化错误事实（供 adapter 返回给上层）。

    返回 ``{error_code, status, request_id, retry_after_ms, message, detail}``。
    message 优先取 provider 错误体的 message 字段，退回截断后的原始响应。
    """
    message = ""
    detail = ""
    try:
        import json as _json

        parsed = _json.loads(body_text) if body_text else {}
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            message = str(err.get("message") or "")
            detail = " ".join(str(x) for x in (err.get("code"), err.get("type"), err.get("message")) if x)
        elif isinstance(err, str):
            message = err
            detail = err
    except (ValueError, TypeError):
        pass
    if not detail:
        detail = body_text or ""
    if not message:
        message = f"HTTP {status}" + (f": {detail[:200]}" if detail else "")
    return {
        "error_code": code_for_status(status, detail),
        "status": status,
        "request_id": request_id_from_headers(headers),
        "retry_after_ms": parse_retry_after_ms(headers.get("retry-after") if headers else None),
        "message": message,
        "detail": detail,
    }


# 明确可重试的码（瞬时错误）：限流/服务端/传输层
_RETRIABLE_CODES = frozenset({RATE_LIMIT, SERVER, TRANSPORT})


def retriable_for_code(code: str) -> bool:
    """按机器码判断是否值得重试——不依赖文案关键词（比 is_retriable 更稳）。

    AUTH / QUOTA_EXCEEDED / CONTEXT_WINDOW_EXCEEDED / INVALID_REQUEST 属永久性错误：
    重试只会浪费时间和配额，应直接把可执行的处置建议交给上层。
    """
    if code in _RETRIABLE_CODES:
        return True
    if isinstance(code, str) and code.startswith("HTTP_5"):
        return True
    return False


class LlmCallError(Exception):
    """带结构化事实的 LLM 调用错误——供 retry_async 与上层按码处置。

    - ``retriable``：显式可重试性（retry_async 优先采用，不再猜文案）
    - ``retry_after``：服务器指定的等待秒数（Retry-After 归一后）
    - ``request_id``：provider 请求 id（排障对账）
    """

    def __init__(self, message: str, *, error_code: str = UNKNOWN, status: int = 0,
                 request_id: str | None = None, retry_after_ms: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status = status
        self.request_id = request_id
        self.retry_after_ms = retry_after_ms
        self.retriable = retriable_for_code(error_code)
        self.retry_after = (retry_after_ms / 1000.0) if retry_after_ms else None

    @classmethod
    def from_stream_error(cls, chunk: dict) -> "LlmCallError":
        """由 adapter 的 stream_error chunk 构造（字段与 error_facts 对齐）。"""
        return cls(
            str(chunk.get("message") or chunk.get("data") or "LLM 调用失败"),
            error_code=str(chunk.get("error_code") or UNKNOWN),
            status=int(chunk.get("status") or 0),
            request_id=chunk.get("request_id"),
            retry_after_ms=chunk.get("retry_after_ms"),
        )
