"""services/completion_service.py — 行内补全服务

Task 6.1: 双层补全——LLM FIM 优先（对标 Cursor / GitHub Copilot / Continue），
规则引擎兜底（无 FIM 配置或 LLM 失败时）。

支持的 FIM provider:
- DeepSeek: /beta/completions 端点，prompt 用 <｜fim▁begin｜>...<｜fim▁hole｜>...<｜fim▁end｜> 格式
- Codestral/Mistral: /v1/fim/completions 端点，prompt/suffix 分离字段
"""
import asyncio
import hashlib
import logging
import time
from typing import NotRequired, TypedDict

import httpx

from config import (
    COMPLETION_SUGGESTION_MAX_LEN,
    COMPLETION_SUGGESTION_MAX_LINES,
    COMPLETION_SUGGESTION_MAX_ITEMS,
    FIM_TIMEOUT_SEC,
    FIM_MAX_TOKENS,
    FIM_MIN_PREFIX_CHARS,
    FIM_MAX_PREFIX_CHARS,
    FIM_MAX_SUFFIX_CHARS,
    FIM_CACHE_TTL_SEC,
)

_log = logging.getLogger("yuncat.completion")


class ReplaceRange(TypedDict):
    startLineNumber: int
    startColumn: int
    endLineNumber: int
    endColumn: int


class CompletionItem(TypedDict):
    insertText: str
    replaceRange: NotRequired[ReplaceRange]


_MAX_LEN = COMPLETION_SUGGESTION_MAX_LEN

_KEYWORD_SKELETONS: dict[str, tuple[str, ...]] = {
    "python": (
        "def ",
        "class ",
        "if ",
        "for ",
        "import ",
        "from ",
    ),
    "javascript": (
        "function ",
        "class ",
        "if ",
        "for ",
        "import ",
    ),
    "typescript": (
        "function ",
        "class ",
        "if ",
        "for ",
        "import ",
    ),
}

_SKELETON_BODY: dict[str, str] = {
    "def ": "def name(self):\n    pass",
    "class ": "class Name:\n    pass",
    "if ": "if condition:\n    pass",
    "for ": "for item in items:\n    pass",
    "import ": "import module",
    "from ": "from module import name",
    "function ": "function name() {\n    \n}",
}


def _current_line(prefix: str) -> str:
    if not prefix:
        return ""
    return prefix.splitlines()[-1]


def _keyword_suggestion(language: str, current_line: str) -> str | None:
    stripped = current_line.lstrip()
    for kw in _KEYWORD_SKELETONS.get(language, ()):
        if stripped == kw:
            return _SKELETON_BODY.get(kw, "")
    if language not in ("python", "javascript", "typescript"):
        for kw in ("def ", "function ", "class ", "if ", "for ", "import "):
            if stripped == kw:
                return _SKELETON_BODY.get(kw, "")
    return None


def _continuation(text: str, pos: int) -> str | None:
    rest = text[pos:]
    lines = rest.splitlines()
    if not lines or not rest.strip():
        return None
    suggestion = "\n".join(lines[:COMPLETION_SUGGESTION_MAX_LINES]).rstrip()
    if not suggestion.strip():
        return None
    if len(suggestion) > _MAX_LEN:
        suggestion = suggestion[:_MAX_LEN].rstrip()
    return suggestion


def _dedupe(items: list[CompletionItem]) -> list[CompletionItem]:
    seen: set[str] = set()
    result: list[CompletionItem] = []
    for it in items:
        txt = it["insertText"]
        if txt not in seen:
            seen.add(txt)
            result.append(it)
    return result


def suggest_inline_completions(
    language: str,
    prefix: str,
    suffix: str,
    filename: str | None = None,
) -> list[CompletionItem]:
    """基于重复模式与关键词骨架生成本地行内补全建议（规则引擎——同步、无 LLM）。"""
    del filename
    current_line = _current_line(prefix)
    if not current_line.strip():
        return []

    if not suffix.strip():
        body = _keyword_suggestion(language, current_line)
        if body:
            return [{"insertText": body}]

    full = prefix + suffix
    cursor = len(prefix)
    items: list[CompletionItem] = []
    start = full.find(current_line)
    while start != -1:
        match_end = start + len(current_line)
        if match_end != cursor:
            cont = _continuation(full, match_end)
            if cont:
                items.append({"insertText": cont})
                if len(items) >= COMPLETION_SUGGESTION_MAX_ITEMS:
                    break
        start = full.find(current_line, start + 1)

    return _dedupe(items)[:COMPLETION_SUGGESTION_MAX_ITEMS]


# ============================================================
# Task 6.1: LLM FIM 内联补全
# ============================================================

# 简单 TTL 缓存——相同 prefix+suffix+provider 在 TTL 内不重复请求
_fim_cache: dict[str, tuple[float, list[CompletionItem]]] = {}


def _cache_key(prefix: str, suffix: str, provider: str, model: str) -> str:
    raw = f"{provider}:{model}:{hashlib.sha256((prefix + suffix).encode()).hexdigest()[:16]}"
    return raw


def _cache_get(key: str) -> list[CompletionItem] | None:
    entry = _fim_cache.get(key)
    if entry and (time.time() - entry[0]) < FIM_CACHE_TTL_SEC:
        return entry[1]
    return None


def _cache_set(key: str, items: list[CompletionItem]) -> None:
    _fim_cache[key] = (time.time(), items)
    # 简单 LRU——缓存超 100 项时清空最早的半数
    if len(_fim_cache) > 100:
        sorted_keys = sorted(_fim_cache, key=lambda k: _fim_cache[k][0])
        for k in sorted_keys[:50]:
            del _fim_cache[k]


def _truncate_for_fim(prefix: str, suffix: str) -> tuple[str, str]:
    """截断 prefix/suffix 到 FIM 限制——避免大文件全量发送。"""
    if len(prefix) > FIM_MAX_PREFIX_CHARS:
        # 保留末尾（最接近光标的部分）
        prefix = prefix[-FIM_MAX_PREFIX_CHARS:]
    if len(suffix) > FIM_MAX_SUFFIX_CHARS:
        # 保留开头（最接近光标的部分）
        suffix = suffix[:FIM_MAX_SUFFIX_CHARS]
    return prefix, suffix


def _build_deepseek_fim_prompt(prefix: str, suffix: str) -> str:
    """DeepSeek FIM prompt 格式——用特殊 token 标记 prefix/suffix。"""
    return f"<｜fim▁begin｜>{prefix}<｜fim▁hole｜>{suffix}<｜fim▁end｜>"


async def _call_deepseek_fim(
    base_url: str, api_key: str, model: str,
    prefix: str, suffix: str,
) -> list[CompletionItem]:
    """DeepSeek FIM API 调用——/beta/completions 端点。"""
    prompt = _build_deepseek_fim_prompt(prefix, suffix)
    url = base_url.rstrip("/").replace("/v1", "/beta") + "/completions"
    payload = {
        "model": model,
        "prompt": prompt,
        "max_tokens": FIM_MAX_TOKENS,
        "stop": ["\n\n", "```"],
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=FIM_TIMEOUT_SEC) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    choices = data.get("choices", [])
    items: list[CompletionItem] = []
    for ch in choices[:COMPLETION_SUGGESTION_MAX_ITEMS]:
        text = ch.get("text", "").strip()
        if text:
            items.append({"insertText": text})
    return items


async def _call_codestral_fim(
    base_url: str, api_key: str, model: str,
    prefix: str, suffix: str,
) -> list[CompletionItem]:
    """Codestral/Mistral FIM API 调用——/v1/fim/completions 端点。"""
    url = base_url.rstrip("/") + "/fim/completions"
    payload = {
        "model": model,
        "prompt": prefix,
        "suffix": suffix,
        "max_tokens": FIM_MAX_TOKENS,
        "stop": ["\n\n", "```"],
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=FIM_TIMEOUT_SEC) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    choices = data.get("choices", [])
    items: list[CompletionItem] = []
    for ch in choices[:COMPLETION_SUGGESTION_MAX_ITEMS]:
        msg = ch.get("message", {})
        text = (msg.get("content") or "").strip()
        if text:
            items.append({"insertText": text})
    return items


# provider → FIM 调用函数映射
_FIM_DISPATCH: dict[str, callable] = {
    "deepseek": _call_deepseek_fim,
    "mistral": _call_codestral_fim,
}


async def suggest_inline_completions_llm(
    language: str,
    prefix: str,
    suffix: str,
    model_config: dict,
    filename: str | None = None,
) -> list[CompletionItem]:
    """Task 6.1: LLM FIM 内联补全——调用 provider 的 FIM 端点。

    Args:
        model_config: ModelConfigService.get_active_config() 返回的配置 dict
            必须含 provider / base_url / api_key，以及可选的 fim 子配置

    Returns:
        补全项列表；若 provider 不支持 FIM 或调用失败，返回空列表（调用方应回退规则引擎）。
    """
    provider = model_config.get("provider", "")
    api_key = model_config.get("api_key", "")
    base_url = model_config.get("base_url", "")

    # fim 配置可在 provider config 顶层或 fim 子段
    fim_cfg = model_config.get("fim") or {}
    fim_model = fim_cfg.get("model") or model_config.get("fim_model", "")
    fim_format = fim_cfg.get("format") or provider  # 默认用 provider 名作 format

    if not (provider and api_key and base_url and fim_model):
        return []

    if len(prefix) < FIM_MIN_PREFIX_CHARS:
        return []

    # 截断 + 缓存
    prefix_t, suffix_t = _truncate_for_fim(prefix, suffix)
    cache_key = _cache_key(prefix_t, suffix_t, provider, fim_model)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # 分发到 provider-specific FIM 调用
    caller = _FIM_DISPATCH.get(fim_format)
    if not caller:
        _log.debug("FIM not supported for provider/format: %s", fim_format)
        return []

    try:
        items = await caller(base_url, api_key, fim_model, prefix_t, suffix_t)
        _cache_set(cache_key, items)
        return items
    except httpx.HTTPStatusError as e:
        _log.debug("FIM HTTP error %s: %s", e.response.status_code, str(e)[:200])
    except (httpx.RequestError, asyncio.TimeoutError) as e:
        _log.debug("FIM request failed: %s", str(e)[:200])
    except Exception:
        _log.debug("FIM unexpected error", exc_info=True)
    return []


def clear_fim_cache() -> None:
    """清空 FIM 缓存（测试用）。"""
    _fim_cache.clear()
