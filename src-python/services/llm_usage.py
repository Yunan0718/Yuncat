"""services/llm_usage.py —— token 用量的统一采集与口径归一

2026-09-10（用户决策：**统计 token 消耗，不再统计价格**）调研结论：

1. **厂商没有"逐次用量查询"API。** 各家提供的官方接口只有余额 / 配额窗口：
   DeepSeek `GET /user/balance`、Moonshot `GET /v1/users/me/balance`、
   OpenRouter `GET /api/v1/credits`、SiliconFlow `GET /v1/user/info`、
   xAI `GET /billing/credits`；OpenAI `/v1/organization/costs` 与 Anthropic
   `/v1/organizations/usage_report` 需要**组织级 admin key**（普通项目 key 返回 403）；
   Google / Mistral / Groq / Azure 等没有公开接口。
   → 厂商 API 只能做"余额对账"，**不能**用来统计 token 消耗。

2. **主流做法是本地账本**：从每次响应自带的 `usage` 字段累计（pi-ai-usage、dsh-stats-panel、
   ccusage 等全是这么做的），厂商 API 仅用于交叉核对。Yuncat 已有这套链路
   （`analytics_events` 事件 + `tasks.used_tokens`），方向正确。

3. 两个必须修的坑（实测都在仓库里）：
   - **流式默认拿不到 usage**：OpenAI 兼容端点必须显式发
     ``stream_options: {"include_usage": true}``，否则流式请求的 token 统计恒为 0；
   - usage 的细分口径各家不同：OpenAI 把缓存命中放在
     ``prompt_tokens_details.cached_tokens``，DeepSeek 用平铺的 ``prompt_cache_hit_tokens``；
     推理 token 在 ``completion_tokens_details.reasoning_tokens``。
     这些是"不可见但真实消耗"的部分，不采集就是漏记。

4. 价格统计从未真正工作过：``calc_cost`` 读 ``pricing.input_per_1k``，而 YAML 存的是
   ``{input, output}``（每百万）——口径不匹配，恒为 0。既然去掉价格，这个坑一并消失。
"""
from __future__ import annotations

# ── 唯一真源：token 用量的事件类型与字段名 ───────────────────────────────
# 2026-09-14 教训：这些名字原本在「写入端（适配器/agent_finalize）」与
# 「读取端（缓存命中率端点、上下文占用估算）」各写一份，改名时立刻漂移——
# 实测一次归一化重命名就让缓存命中率静默变成 0。任何新增生产者/消费者
# **必须引用这里的常量**，不要再写字面量（有契约测试守住，见
# tests/contract/test_token_usage_contract.py）。
#
# TOKEN_USAGE_EVENTS：历史事件类型并存——改名前的 'token_cost' 仍要能聚合，
# 本地数据不做迁移。
TOKEN_USAGE_EVENTS: tuple[str, ...] = ("token_usage", "token_cost")

# 事件类型 SQL 片段（analytics_events.event_type 过滤用）
TOKEN_USAGE_EVENT_SQL: str = "('token_usage','token_cost')"

# usage 字典的规范字段名（适配器 last_usage / 事件 event_data 共用）
FIELD_PROMPT_TOKENS = "prompt_tokens"
FIELD_COMPLETION_TOKENS = "completion_tokens"
FIELD_TOTAL_TOKENS = "total_tokens"
FIELD_CACHED_TOKENS = "cached_tokens"
FIELD_CACHE_MISS_TOKENS = "cache_miss_tokens"
FIELD_REASONING_TOKENS = "reasoning_tokens"

# 归一化后 usage 一定包含的键（契约测试据此校验生产/消费一致性）
CANONICAL_USAGE_FIELDS: tuple[str, ...] = (
    FIELD_PROMPT_TOKENS,
    FIELD_COMPLETION_TOKENS,
    FIELD_TOTAL_TOKENS,
    FIELD_CACHED_TOKENS,
    FIELD_CACHE_MISS_TOKENS,
    FIELD_REASONING_TOKENS,
)

# 已废弃的旧键名：任何**生产者**都不应再写；读取端仅可为兼容历史数据而兜底。
# 契约测试会扫描 src-python 源码，出现这些名字必须显式标注 legacy 兼容。
LEGACY_USAGE_FIELDS: tuple[str, ...] = (
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "cache_hit_tokens",
)

# 旧事件体里的缓存命中字段（读取兜底用）。放在本文件是因为这里才是"字段名唯一真源"，
# 消费端引用常量即可，不必在自己的源码里写字面量（契约测试会拦）。
LEGACY_CACHED_TOKENS_KEY = "cache_hit_tokens"

# OpenAI 兼容流式必须显式要 usage，否则最后一个 chunk 里没有该对象。
# 作为可选参数发送：若某网关不认识它，走"摘参数重试"降级（见 services/reasoning.py）。
STREAM_USAGE_OPTIONS: dict = {"stream_options": {"include_usage": True}}


def stream_usage_options() -> dict:
    """返回流式请求应附加的 usage 开关（浅拷贝，调用方可直接 update 进 payload）。"""
    return dict(STREAM_USAGE_OPTIONS)


def normalize_usage(usage: dict | None) -> dict:
    """把各家口径的 usage 归一成统一字段，**零假设、缺什么补 0**。

    返回 ``CANONICAL_USAGE_FIELDS`` 列出的全部键：

    - ``total_tokens`` 缺失时回退 prompt + completion；
    - 缓存命中：OpenAI ``prompt_tokens_details.cached_tokens`` /
      平铺 ``cached_tokens`` / DeepSeek ``prompt_cache_hit_tokens``（历史键名，读取兜底）；
    - 缓存未命中：厂商直接给就用，否则按 ``prompt - cached`` 推导
      （DeepSeek 的 hit/miss 是互补的，推导不会丢信息）；
    - 推理 token：``completion_tokens_details.reasoning_tokens``
      （o 系/思考模型的这部分是真实计费输出，肉眼不可见）。
    """
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    total = usage.get("total_tokens")
    total = int(total) if total is not None else prompt + completion

    p_details = usage.get("prompt_tokens_details")
    c_details = usage.get("completion_tokens_details")
    p_details = p_details if isinstance(p_details, dict) else {}
    c_details = c_details if isinstance(c_details, dict) else {}

    # 缓存命中优先级：OpenAI 嵌套明细 > 平铺 cached_tokens > DeepSeek 旧键名（兼容）
    cached = p_details.get("cached_tokens")
    if cached is None:
        cached = usage.get("cached_tokens", usage.get("prompt_cache_hit_tokens", 0))
    cached = int(cached or 0)

    miss = usage.get("cache_miss_tokens")
    if miss is None:
        miss = usage.get("prompt_cache_miss_tokens")  # 历史键名，读取兜底
    miss = int(miss) if miss is not None else max(0, prompt - cached)

    reasoning = usage.get("reasoning_tokens", c_details.get("reasoning_tokens", 0))

    return {
        FIELD_PROMPT_TOKENS: prompt,
        FIELD_COMPLETION_TOKENS: completion,
        FIELD_TOTAL_TOKENS: total,
        FIELD_CACHED_TOKENS: cached,
        FIELD_CACHE_MISS_TOKENS: miss,
        FIELD_REASONING_TOKENS: int(reasoning or 0),
    }
