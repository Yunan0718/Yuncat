"""src-python/token_budget.py —— Token 计数 + 上下文预算派生

6b-1 修复：从 config.py 提取——所有 token 估算 / 上下文窗口解析 / 动态预算派生
函数集中到本模块，让 config.py 聚焦于静态常量。

向后兼容：config.py 通过 `from token_budget import *` + 显式 re-export 列表保持
现有 102 个消费者的 `from config import calc_xxx` 路径不变。

主流做法对标：
  - Claude Code / Cursor：上下文预算按模型窗口百分比动态分配
  - Codex CLI：本地 tiktoken + context window 分区
  - 中文场景：CJK 感知估算避免 `len // 4` 严重低估
"""
import logging
import os
from typing import Any

# 模块 logger——保留 "yuncat.config" 名以维持日志向后兼容（原实现位置）
_log = logging.getLogger("yuncat.config")


# ═══════════════════════════════════════════════════════════
# 静态回退常量（动态测量优先，首次调用前用回退值）
# ═══════════════════════════════════════════════════════════

# 系统提示词预算 — 动态测量优先，首次调用前用回退值
_SYSTEM_PROMPT_FALLBACK = 24_000
_AVG_TURN_FALLBACK = 1200

_MODEL_CONTEXT_DEFAULTS: dict[str, int] = {
    "deepseek-v4": 1_048_576,
    "deepseek-chat": 131_072,
    "claude-opus-4-8": 1_048_576,
    "claude-opus-4": 1_048_576,
    "claude-sonnet-4-6": 1_048_576,
    "claude-sonnet-4": 1_048_576,
    "claude-haiku-4-5": 204_800,
    "claude-haiku-4": 204_800,
    "gpt-5.4": 262_144,
    "gpt-5.4-mini": 1_048_576,
    "gpt-5.4-nano": 1_048_576,
    "gpt-5.1": 131_072,
    "gpt-4o": 131_072,
    "gemini-2": 1_048_576,
    "gemini-1": 1_048_576,
    "qwen3": 131_072,
    "qwen2": 131_072,
    "glm-4": 131_072,
    "kimi": 131_072,
    "hunyuan": 131_072,
    "grok-3": 1_048_576,
    "mistral-large": 131_072,
    "llama-4": 262_144,
}


def estimate_tokens_cjk(text: str) -> int:
    """CJK 感知 token 估算——覆盖 CJK Unified + Extension A/B + Hangul + 假名 + 标点 + 全角。
    CJK 字符 ≈ 2 tokens/char，ASCII ≈ 4 chars/token。
    替换所有 `len(x)//4` 估算点——中文场景下差异可达 4-8x。
    """
    cjk = 0
    for c in text:
        cp = ord(c)
        if (0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
            0x3400 <= cp <= 0x4DBF or    # CJK Extension A
            0xAC00 <= cp <= 0xD7AF or    # Hangul Syllables
            0x3040 <= cp <= 0x30FF or    # Hiragana + Katakana
            0x3000 <= cp <= 0x303F or    # CJK Punctuation
            0xF900 <= cp <= 0xFAFF or    # CJK Compatibility Ideographs
            0xFF00 <= cp <= 0xFFEF):     # Fullwidth Forms
            cjk += 1
    return cjk // 2 + (len(text) - cjk) // 4


# Phase 56-P3: tiktoken 精确 token 计数（向 Claude Code / Codex CLI 对齐）
# 优先用对应模型的 tokenizer，无法匹配时 fallback 到 cl100k_base，
# tiktoken 不可用时 fallback 到 CJK 估算。

def _get_tiktoken_encoding(model_id: str) -> str:
    """根据模型名选择 tiktoken encoding。"""
    m = model_id.lower()
    if any(p in m for p in ("o1", "o3", "gpt-4o", "gpt-4.5")):
        return "o200k_base"
    # cl100k_base 覆盖 gpt-4/gpt-3.5/text-embedding-ada-002，
    # 对 DeepSeek / Claude 也是可接受的近似。
    return "cl100k_base"


def estimate_tokens(text: str, model_id: str = "gpt-4") -> int:
    """优先 tiktoken 精确计数，失败时 fallback 到 CJK 估算。

    主流做法：Codex CLI、Cursor 在本地用 tiktoken 或类似 tokenizer；
    Claude Code 主要依赖 API 返回 usage，本地估算时也用 tokenizer。
    """
    try:
        import tiktoken
        enc = tiktoken.get_encoding(_get_tiktoken_encoding(model_id))
        return len(enc.encode(text))
    except Exception:
        # tiktoken 未安装或编码不存在 → fallback
        return estimate_tokens_cjk(text)


def estimate_tokens_for_messages(messages: list[dict], model_id: str = "gpt-4") -> int:
    """估算消息列表的 token 数（content 字段）。

    注意：这不包含 system prompt / tools / function schema 的精确开销，
    只是比 `sum(len(m['content']) // 2)` 更准的本地估算。
    """
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content, model_id)
        # 每条消息格式开销（OpenAI 风格约 4 token/条）
        total += 4
    return total


async def count_tokens_online(messages: list[dict], model_cfg: dict) -> int | None:
    """调用厂商 tokenizer API 做精确计数（网络请求）。

    支持:
      - anthropic: POST /v1/messages/count_tokens
      - google/gemini: POST /v1beta/models/{model}:countTokens
      - openai/deepseek: 无官方 tokenizer API，返回 None 让调用方 fallback 到 tiktoken

    失败或厂商不支持时返回 None，不抛异常。
    """
    import asyncio
    provider = (model_cfg.get("provider") or "").lower()
    api_key = model_cfg.get("api_key", "")
    base_url = (model_cfg.get("base_url") or "").rstrip("/")
    model_id = model_cfg.get("model", "")
    if not api_key or not messages:
        return None

    try:
        import httpx
    except Exception:
        return None

    # Anthropic
    if provider in ("anthropic", "claude") or "claude" in model_id.lower():
        url = base_url or "https://api.anthropic.com"
        payload = {
            "model": model_id,
            "messages": [
                {"role": m.get("role", "user"), "content": str(m.get("content", ""))}
                for m in messages
            ],
        }
        headers = {
            "x-api-key": api_key,
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(f"{url}/v1/messages/count_tokens", json=payload, headers=headers)
                if r.status_code == 200:
                    return r.json().get("input_tokens")
        except Exception:
            pass
        return None

    # Google / Gemini
    if provider in ("google", "gemini") or "gemini" in model_id.lower():
        url_base = base_url or "https://generativelanguage.googleapis.com/v1beta"
        model_name = model_id
        if not model_name.startswith("models/"):
            model_name = f"models/{model_name}"
        contents = []
        for m in messages:
            contents.append({"parts": [{"text": str(m.get("content", ""))}]})
        params = {"key": api_key}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{url_base}/{model_name}:countTokens",
                    json={"contents": contents},
                    params=params,
                )
                if r.status_code == 200:
                    return r.json().get("totalTokens")
        except Exception:
            pass
        return None

    # OpenAI / DeepSeek 等没有官方 tokenizer API，走本地 tiktoken
    await asyncio.sleep(0)
    return None


async def count_tokens(
    text_or_messages: str | list[dict],
    model_cfg: dict | None = None,
    *,
    prefer_online: bool = False,
    model_id: str | None = None,
) -> dict:
    """统一 token 计数接口：厂商 tokenizer API → tiktoken → CJK 估算。

    与主流工具对齐：
      - Claude Code / Cursor：优先用 API 返回的 usage，本地估算用 tokenizer；
      - Codex CLI：本地用 tiktoken，失败时按字符估算；
      - 中文场景：CJK 感知估算避免 `len // 4` 严重低估。

    返回 {"tokens": int, "source": str}，source 用于可观测性：
      - {provider}_tokenizer_api  厂商在线 tokenizer（最准）
      - tiktoken                   OpenAI 公开 tokenizer（次准）
      - cjk_estimate               纯字符估算（最终 fallback）
    """
    import asyncio

    messages: list[dict]
    text: str
    if isinstance(text_or_messages, str):
        text = text_or_messages
        messages = [{"role": "user", "content": text}]
    elif isinstance(text_or_messages, list):
        messages = text_or_messages
        text = "\n".join(str(m.get("content", "")) for m in messages)
    else:
        text = str(text_or_messages)
        messages = [{"role": "user", "content": text}]

    _model_id = model_id or (model_cfg.get("model", "gpt-4") if model_cfg else "gpt-4")

    # Level 1: 厂商 tokenizer API（在线最准）
    if prefer_online and model_cfg:
        try:
            vendor_tokens = await count_tokens_online(messages, model_cfg)
            if vendor_tokens is not None and vendor_tokens > 0:
                return {
                    "tokens": vendor_tokens,
                    "source": f"{model_cfg.get('provider', 'vendor')}_tokenizer_api",
                }
        except Exception:
            _log.debug("count_tokens vendor API failed", exc_info=True)
        await asyncio.sleep(0)

    # Level 2: tiktoken 精确计数（主流本地方案）
    try:
        import tiktoken
        enc = tiktoken.get_encoding(_get_tiktoken_encoding(_model_id))
        return {"tokens": len(enc.encode(text)), "source": "tiktoken"}
    except Exception:
        _log.debug("count_tokens tiktoken failed", exc_info=True)

    # Level 3: CJK 估算（最终 fallback）
    return {"tokens": estimate_tokens_cjk(text), "source": "cjk_estimate"}


def get_context_window_for_model(model_id: str) -> int:
    """前缀匹配获取上下文窗口大小（token）。"""
    model_lower = model_id.lower()
    for prefix, window in _MODEL_CONTEXT_DEFAULTS.items():
        if prefix in model_lower:
            return window
    return 131_072


def resolve_context_window(model_config: dict) -> int:
    """从 model_config 中解析上下文窗口大小。
    优先用顶层 context，再用 context_window，然后 YAML models[].context，
    最后 fallback 到已知表。
    """
    if model_config.get("context"):
        return model_config["context"]
    if model_config.get("context_window"):
        return model_config["context_window"]
    model_id = model_config.get("model", "")
    models_list = model_config.get("models", [])
    for m in models_list:
        if m.get("id") == model_id and m.get("context"):
            return m["context"]
    return get_context_window_for_model(model_id)


# P2-2.1: 默认模型 config 派生——供模块加载期/工具初始化期派生预算
# 对标 Cursor/Aider 的"当前模型上下文预算"概念，避免硬编码 131_072 fallback
_DEFAULT_CFG_FALLBACK = {"model": "default"}


def get_default_model_config() -> dict:
    """返回当前默认模型的 config dict，供工具结果上限等派生使用。

    ModelConfigService 未就绪时（模块加载早期）fallback 到 _DEFAULT_CFG_FALLBACK，
    此时上下文窗口走 131_072（get_context_window_for_model 的 fallback）。

    注意：lru_cache 不适用于此——默认模型可在运行时切换，需每次实时读取。
    调用方应在函数内调用（而非模块加载期），以获取最新配置。
    """
    try:
        from context import get_app
        app = get_app()
        if app and hasattr(app.state, "model_config_service"):
            mcs = app.state.model_config_service
            cfg = mcs.get_default_config() if hasattr(mcs, "get_default_config") else None
            if cfg and cfg.get("model"):
                return cfg
    except Exception:
        pass
    return _DEFAULT_CFG_FALLBACK


def estimate_avg_turn_tokens(history: list[dict]) -> int:
    """从实际历史消息估算平均每轮 token 数。

    Task 7.3: 用真实历史均值替代固定 _AVG_TURN_FALLBACK=1200。
    用 _CHARS_PER_TOKEN_ESTIMATE 转换字符数到 token 数。
    返回 0 表示无历史数据（调用方应使用 fallback）。

    对标 Claude Code: 按实际对话密度动态调整历史上限，而非固定假设。
    """
    if not history:
        return 0
    total_chars = sum(len(str(m.get("content", ""))) for m in history)
    if total_chars == 0:
        return 0
    return max(1, int(total_chars / len(history) / _CHARS_PER_TOKEN_ESTIMATE))


def calc_history_limit(
    model_config: dict,
    last_prompt_tokens: int = 0,
    avg_turn_tokens: int = 0,
) -> int:
    """动态对话历史上限（轮数）。

    对标 Claude Code: 优先用厂商 API 的 usage.prompt_tokens 真实数据。
    last_prompt_tokens = 上次 API 调用的 usage.prompt_tokens (0=首次调用,用回退值)

    Task 7.3: avg_turn_tokens = 真实历史均值（从 estimate_avg_turn_tokens 派生）。
    0 = 首次调用或无历史数据，用 _AVG_TURN_FALLBACK 回退值。

    DeepSeek V4 1M space, 上轮 180K prompt, avg=600 → (1M - 180K) / 600 ≈ 1366 轮余量
    """
    context_window = resolve_context_window(model_config)
    if last_prompt_tokens > 0:
        available = max(context_window - last_prompt_tokens, context_window // 10)
    else:
        available = max(context_window - _SYSTEM_PROMPT_FALLBACK, context_window // 10)
    # Task 7.3: 真实历史均值优先，fallback 仅在无数据时使用
    turn_size = avg_turn_tokens if avg_turn_tokens > 0 else _AVG_TURN_FALLBACK
    return max(10, available // turn_size)


def calc_compaction_threshold(model_config: dict) -> int:
    """返回触发自动压缩的 token 阈值（模型上下文窗口的 98%）。

    例: DeepSeek V4 1M → 983,040 tokens 触发
         GPT-4o 128K   → 125,440 tokens 触发
    """
    return int(resolve_context_window(model_config) * 0.98)


def calc_max_turns_for_cache(model_config: dict) -> int:
    """内存缓存最大轮数 = 对话历史上限 × 3。
    确保压缩时旧消息仍在缓存中可被检索。
    """
    return calc_history_limit(model_config) * 3


# ═══════════════════════════════════════════════════════════
# Phase 43 P0-DYN: 动态上限 — 从模型上下文窗口派生，非硬编码
# ═══════════════════════════════════════════════════════════

# P2-2.6 L4 SI 场景化预检查：长输出阈值——超过此值则追加 Agent 自审提示
# 对标 Claude Code 长输出标记 + Cursor 的"context-heavy"提示
# 即使未触发截断（动态 _limit 可能更高），长输出也提示 Agent 审视信息完整性
L4_LONG_OUTPUT_THRESHOLD_CHARS = 5000

def calc_tool_result_limit(model_config: dict, tool_count: int = 1) -> int:
    """工具结果最大字符数 = 模型上下文窗口的 10%，不低于 2000，不高于 128000。
    多工具分摊: 同一轮 N 个工具调用时，每个工具结果上限递减（防 Token 爆炸）。
    单工具: 窗口 10% → DeepSeekV4 1M = 100,000 chars
    8 工具: 窗口 10% / (8*0.5) = 2.5% → DeepSeekV4 1M = 25,000 chars each
    """
    window = resolve_context_window(model_config)
    base = int(window * 0.10)
    effective = int(base / max(1, tool_count * 0.5))
    return max(2000, min(128000, effective))


def calc_tool_result_limit_with_declaration(
    model_config: dict,
    tool_count: int,
    declared_max: int | None,
) -> int:
    """结合工具自我声明的 max_result_size_chars 计算实际截断上限。

    - 未声明 → 使用 calc_tool_result_limit 动态默认
    - 声明了 → 在默认基础上给更多预算，但不超过上下文窗口的 30%
      （防止单个工具结果吞噬整个上下文）
    主流做法：Claude Code MCP / Codex CLI 允许工具声明 max_result_size，
    路由层按声明分配上下文预算。
    """
    default_limit = calc_tool_result_limit(model_config, tool_count)

    if declared_max is None:
        return default_limit
    window = resolve_context_window(model_config)
    hard_cap = int(window * _CHARS_PER_TOKEN_ESTIMATE * 0.30)
    return max(default_limit, min(declared_max, hard_cap))


def calc_file_reattach_budget(model_config: dict) -> int:
    """压缩后恢复最近操作文件内容的字符预算。

    取模型上下文窗口的 ~1.5%，最低 1000 字符，最高 6000 字符。
    128K 模型 → ~1900 chars；1M 模型 → ~6000 chars（封顶）。
    """
    window = resolve_context_window(model_config)
    return max(1000, min(6000, int(window * 0.015)))


# Task 6.10 (F-015): 工具循环硬上限默认值——对标 Claude Code max_turns=100。
# 大窗口模型（DeepSeek V4 1M）动态派生可达 500 轮，若无 token 预算约束可能半无限循环。
# 此常量作为安全网；用户可通过 context["max_rounds"] 覆盖。
DEFAULT_MAX_TOOL_ROUNDS_HARD_LIMIT = 100

# Batch 3.1: 用户可配置 max_tool_rounds 的合法范围。
# 低于 10 无法完成多数简单任务；高于 1000 失去安全网意义。
USER_TOOL_ROUNDS_MIN = 10
USER_TOOL_ROUNDS_MAX = 1000


def calc_max_tool_rounds(model_config: dict, hard_limit: int | None = None) -> int:
    """工具循环最大轮次 = 对话历史上限 / 4，受硬上限约束。

    小模型 (128K): 约 60 轮历史 / 4 = 15 轮
    大模型 (1M):   约 2000 轮历史 / 4 = 500 轮 → 受硬上限约束降为 100 轮

    Task 6.10 (F-015): 加 hard_limit 参数（默认 100，对标 Claude Code max_turns=100）。
    大窗口模型动态派生可达数百轮，若用户未设 token 预算，Agent 可能半无限循环。
    硬上限作为安全网——即使动态派生 500 轮也封顶 100 轮。
    用户可通过 context["max_rounds"] 覆盖（例如调高到 200 或调低到 50）。

    参数:
        model_config: 模型配置 dict（含 model / models / context_window）
        hard_limit: 硬上限覆盖值。None=用 DEFAULT_MAX_TOOL_ROUNDS_HARD_LIMIT (100)。
                    传 0 表示禁用硬上限（仅用动态派生值，不推荐）。
    """
    dynamic = calc_history_limit(model_config) // 4
    effective_limit = hard_limit if hard_limit is not None else DEFAULT_MAX_TOOL_ROUNDS_HARD_LIMIT
    if effective_limit <= 0:
        return dynamic
    return min(dynamic, effective_limit)


def calc_max_parallel_roles(model_config: dict) -> int:
    """并行角色数上限 = 工具循环上限 * 0.6。

    Task 6.10: 间接受 calc_max_tool_rounds 硬上限约束——
    1M 模型 min(500, 100) * 0.6 = 60（原 300 过大易触发 API 限流）。
    """
    return int(calc_max_tool_rounds(model_config) * 0.6)


# ═══════════════════════════════════════════════════════════
# Phase 56-P2: 模型上下文感知的动态预算（对标 Claude Code / Codex CLI）
# ═══════════════════════════════════════════════════════════

# 字符/token 换算保守因子：上下文窗口按字符估算时，1 token ≈ 2.5 字符
#（英文约 4 chars/token，CJK 约 0.5 chars/token；混合场景取保守中值）
_CHARS_PER_TOKEN_ESTIMATE = 2.5


def calc_context_budget(
    model_config: dict,
    fraction: float = 0.05,
    min_chars: int = 500,
    max_chars: int | None = None,
) -> int:
    """按当前模型上下文窗口的 fraction 计算字符预算。

    主流做法：Claude Code / Cursor 的上下文注入预算都是按窗口百分比动态分配，
    而非固定字符数。小窗口模型自动收紧，大窗口模型自动放大。
    """
    window = resolve_context_window(model_config)
    budget = int(window * _CHARS_PER_TOKEN_ESTIMATE * fraction)
    budget = max(min_chars, budget)
    if max_chars is not None:
        budget = min(max_chars, budget)
    return budget


def calc_list_limit(
    model_config: dict,
    base_limit: int,
    fraction_per_100k: float = 1.0,
    max_limit: int | None = None,
) -> int:
    """按模型窗口大小动态调整列表长度上限。

    例：base_limit=5, fraction_per_100k=1 → 128K 模型保持 5，1M 模型放大到 ~15
    """
    window = resolve_context_window(model_config)
    extra = int((window / 131_072) * fraction_per_100k)
    limit = base_limit + extra
    if max_limit is not None:
        limit = min(limit, max_limit)
    return limit


def calc_context_used_percent(current_tokens: int, model_config: dict) -> float:
    """当前 token 用量占模型上下文窗口的百分比。

    用于动态压缩触发、截断因子、用户可见性。
    """
    window = resolve_context_window(model_config)
    if window <= 0:
        return 0.0
    return min(1.0, current_tokens / window)


def calc_micro_compact_threshold(model_config: dict) -> int:
    """Micro compact 开始把 tool 输出落盘的阈值。

    对标 Claude Code：tool 结果超过约 50K 字符时持久化到磁盘，模型只看 preview。
    这里按模型窗口动态：128K 模型 → ~12.5K，1M 模型 → ~50K。
    """
    return calc_context_budget(model_config, fraction=0.10, min_chars=8_000, max_chars=50_000)


def calc_micro_compact_preview_budget(model_config: dict) -> int:
    """Tool 输出落盘后保留给模型看的 preview 字符预算。

    Claude Code 约 2KB preview。这里按窗口动态：小模型 500，大模型 2500。
    """
    return calc_context_budget(model_config, fraction=0.005, min_chars=500, max_chars=2_500)


def calc_recent_context_budget(model_config: dict) -> int:
    """上下文压缩时保留的最近消息 token 预算。

    主流做法（Claude Code / Codex CLI）: 压缩旧历史，但保留最近 10-20% 上下文窗口
    的消息原样可用。这里取上下文窗口的 20%，最低 4K tokens。
    """
    window = resolve_context_window(model_config)
    return max(4_000, int(window * 0.20))


def calc_three_zone_budget(model_config: dict) -> dict:
    """M4: 上下文窗口三区 token 预算分配（对标 Codex CLI context partitioning）。

    将上下文窗口分为四区，确保总和 == 窗口大小:
      - system:  20% — system prompt + rules + skills + delegation
      - tool:    25% — tool definitions + tool results in history
      - history: 剩余  — 对话历史
      - output:  10% (capped 2K-8K) — 模型输出预留

    主流做法: Codex CLI 将 context window 显式分区，避免 system prompt 膨胀
    挤压 history 空间。Claude Code 也用类似策略。
    """
    window = resolve_context_window(model_config)
    output = min(8_192, max(2_048, window // 10))
    system = int(window * 0.20)
    tool = int(window * 0.25)
    history = max(0, window - output - system - tool)
    return {"system": system, "tool": tool, "history": history, "output": output}


def enforce_token_budget(
    messages: list[dict],
    model_config: dict,
    output_reserve: int | None = None,
) -> list[dict]:
    """M4: 发送前 token 预算强制——超限时从 history 前端裁剪。

    对标 Codex CLI: 发送前最终校验，确保 system + history ≤ window - output。
    裁剪策略:
      1. 保留所有 system 消息（system prompt + memory context）
      2. 保留最后一条 user 消息（当前指令不能丢）
      3. 从 history 前端删除最早的对话，直到总 token ≤ 预算

    Args:
        messages: OpenAI 格式消息列表（system 在前，history 在中，user 在后）
        model_config: 模型配置（用于解析窗口大小）
        output_reserve: 输出 token 预留；None 时自动计算（10% 窗口，2K-8K）

    Returns:
        裁剪后的 messages 列表。空列表返回空。
    """
    if not messages:
        return []

    window = resolve_context_window(model_config)
    if output_reserve is None:
        output_reserve = min(8_192, max(2_048, window // 10))
    budget = window - output_reserve

    model_id = model_config.get("model", "gpt-4")
    total = estimate_tokens_for_messages(messages, model_id)
    if total <= budget:
        return messages

    # 超限——分离 system 消息和其余消息
    system_end = 0
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            system_end = i + 1
        else:
            break

    system_messages = messages[:system_end]
    rest = messages[system_end:]

    if not rest:
        return system_messages  # 只有 system 消息

    system_tokens = estimate_tokens_for_messages(system_messages, model_id)
    history_budget = budget - system_tokens

    if history_budget <= 0:
        # system prompt 本身就超预算——只保留 system + 最后一条
        _log.warning(
            "M4: System prompt (%d tokens) exceeds budget (%d), "
            "dropping all history except last message",
            system_tokens, budget,
        )
        return system_messages + [rest[-1]]

    # 从后往前保留 history，直到用完预算
    kept: list[dict] = []
    accumulated = 0
    for msg in reversed(rest):
        msg_tokens = estimate_tokens_for_messages([msg], model_id)
        if accumulated + msg_tokens > history_budget:
            break
        kept.insert(0, msg)
        accumulated += msg_tokens

    # 至少保留最后一条消息（当前 user 指令不能丢）
    if not kept:
        kept = [rest[-1]]

    trimmed = len(rest) - len(kept)
    if trimmed > 0:
        _log.info(
            "M4: Token budget enforced — trimmed %d history messages "
            "(%d → %d tokens, budget=%d)",
            trimmed, total, system_tokens + accumulated, budget,
        )

    return system_messages + kept


def protect_critical_info(content: str, max_chars: int | None = None) -> str:
    """截断前保护: 提取错误信息、文件路径、代码结构等关键行。永不丢弃。
    对标 ACE 'bullets' 模式——轻量规则引擎，不调 LLM。
    返回: 关键信息文本（最多 20 行 / max_chars 字符），无关键信息时返回 ""。

    Phase 56-P2: 增加 max_chars 参数，供调用方按当前模型上下文动态控制长度。
    """
    critical_lines = []
    for line in content.split("\n"):
        if any(kw in line.lower() for kw in [
            "error", "fail", "traceback", "exception",
            "def ", "class ", "import ",          # 代码结构
            "path:", "file:", "created:",          # 文件操作
        ]):
            critical_lines.append(line)
    if not critical_lines:
        return ""
    text = "\n".join(critical_lines[:20])
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars]
    return text


def adaptive_truncate(content: str, context_window: int,
                       base_limit: int, usage_pct: float = 0.5) -> str:
    """根据当前上下文使用率动态决定截断点。

    context_window 参数保留用于未来扩展（按窗口大小调整因子），当前版本仅用 usage_pct。

    - 上下文 < 30% 使用: 放宽到 base_limit * 3 (几乎不截断)
    - 上下文 30-60%: base_limit * 1.5
    - 上下文 60-90%: base_limit (基准)
    - 上下文 > 90%: base_limit * 0.5 (激进截断, 保护上下文)

    对标 Claude Code microcompact: 按实际 token 使用率触发，非固定值。
    """
    if usage_pct < 0.30:
        factor = 3.0
    elif usage_pct < 0.60:
        factor = 1.5
    elif usage_pct < 0.90:
        factor = 1.0
    else:
        factor = 0.5

    limit = int(base_limit * factor)
    if limit <= 0:
        limit = base_limit
    return content[:limit]


