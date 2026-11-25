"""services/token_tracker.py —— 统一 Token 用量追踪（Phase 56-P3）

优先级链路（按准确度从高到低）:
  1. 当前 Adapter 内存中的 `last_usage`（厂商 API 实时返回，零额外调用）
  2. 数据库中持久化的 `token_usage` 事件（任务结束后写入）
  3. 本地 CJK 感知估算（最终 fallback）

所有 len(x)//4 估算点全部替换为本模块的 estimate_tokens()。
"""
import calendar
import logging
import time
from config import count_tokens, estimate_tokens, estimate_tokens_for_messages
from services.llm_usage import (
    FIELD_CACHE_MISS_TOKENS,
    FIELD_CACHED_TOKENS,
    FIELD_COMPLETION_TOKENS,
    FIELD_PROMPT_TOKENS,
    FIELD_REASONING_TOKENS,
    FIELD_TOTAL_TOKENS,
    LEGACY_CACHED_TOKENS_KEY,
    TOKEN_USAGE_EVENT_SQL,
)

_log = logging.getLogger("yuncat.token_tracker")

# 事件体里可能没有 total_tokens（历史事件只记了输入/输出）——缺失时按 输入+输出 兜底，
# 否则旧数据会凭空变成 0（这正是此前 daily 聚合的老实现做的事）。
_TOTAL_TOKENS_EXPR = (
    f"COALESCE(json_extract(event_data,'$.{FIELD_TOTAL_TOKENS}'),"
    f"COALESCE(json_extract(event_data,'$.{FIELD_PROMPT_TOKENS}'),0)"
    f"+COALESCE(json_extract(event_data,'$.{FIELD_COMPLETION_TOKENS}'),0))"
)


async def get_daily_usage(db, days: int = 30) -> list[dict]:
    """获取最近 N 天的每日 token 用量（账本口径：输入 + 输出）。

    保留旧的返回形状（date 为 epoch 天桶），内部改走 get_token_usage_overview——
    同一份聚合只有一处实现，避免两边口径再次漂移。
    """
    overview = await get_token_usage_overview(db, days=days)
    daily = []
    for item in overview.get("daily", []):
        day = item.get("day", "")
        try:
            epoch_day = int(calendar.timegm(time.strptime(day, "%Y-%m-%d")))
        except (TypeError, ValueError):
            epoch_day = 0
        daily.append({
            "date": epoch_day,
            "prompt_tokens": item.get(FIELD_PROMPT_TOKENS, 0),
            "completion_tokens": item.get(FIELD_COMPLETION_TOKENS, 0),
            "total_tokens": item.get("tokens", 0),
        })
    return daily


async def get_token_usage_overview(db, days: int = 7, session_id: str = "") -> dict:
    """账本口径的 token 用量汇总（**输入 + 输出**）——用量看板/会话用量/缓存命中率共用。

    2026-10-22：此前 Dashboard 与会话用量读 ``conversations.token_count``，那个字段
    **只记输出 token**；真实消耗以输入为主（实测 227,959 输入 vs 2,270 输出，差 101 倍），
    界面上的"token 用量"因此严重少报，且与任务级成本面板互相打架。

    事件类型与字段名一律引用 services/llm_usage.py 的常量（改名时不会静默查空）。
    days <= 0 表示不限时间（会话级统计用）。
    """
    clause, params = _usage_scope(days, session_id)
    result = await _usage_totals(db, clause, params)
    if days > 0:
        result["daily"] = await _usage_daily(db, clause, params, days)
        result["days"] = days
    return result


async def get_session_token_totals(db, days: int = 0) -> dict[str, dict]:
    """按会话汇总 token 用量（账本口径：输入 + 输出）——会话列表/统计用。

    与 get_token_usage_overview 同源同口径：事件类型与字段名一律走
    services/llm_usage.py 的常量，聚合表达式共用 _TOTAL_TOKENS_EXPR。
    返回 {session_id: {"total_tokens", "prompt_tokens", "completion_tokens", "call_count"}}。
    """
    where = ["event_type IN " + TOKEN_USAGE_EVENT_SQL]
    params: list = []
    if days > 0:
        where.append("created_at > ?")
        params.append(int(time.time()) - days * 86400)
    try:
        cursor = await db.execute(
            f"""SELECT json_extract(event_data, '$.session_id') AS sid,
                       SUM({_TOTAL_TOKENS_EXPR}),
                       SUM(COALESCE(json_extract(event_data,'$.{FIELD_PROMPT_TOKENS}'),0)),
                       SUM(COALESCE(json_extract(event_data,'$.{FIELD_COMPLETION_TOKENS}'),0)),
                       COUNT(*)
                FROM analytics_events WHERE {' AND '.join(where)}
                GROUP BY sid""",
            params,
        )
        rows = await cursor.fetchall()
    except Exception:
        # 留痕：静默返回空会让界面显示"没用过 token"，与"真的没用量"无法区分
        _log.warning("按会话汇总 token 用量失败（返回空，界面会显示无用量）", exc_info=True)
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        sid = row[0]
        if not sid:
            continue
        out[str(sid)] = {
            "total_tokens": row[1] or 0,
            "prompt_tokens": row[2] or 0,
            "completion_tokens": row[3] or 0,
            "call_count": row[4] or 0,
        }
    return out


def _usage_scope(days: int, session_id: str) -> tuple[str, list]:
    """构造 WHERE 子句——事件类型走常量（不写字面量）。"""
    where = ["event_type IN " + TOKEN_USAGE_EVENT_SQL]
    params: list = []
    if days > 0:
        where.append("created_at > ?")
        params.append(int(time.time()) - days * 86400)
    if session_id:
        where.append("json_extract(event_data, '$.session_id') = ?")
        params.append(session_id)
    return " AND ".join(where), params


async def _usage_totals(db, clause: str, params: list) -> dict:
    """按账本聚合总量（输入/输出/缓存/推理 + 调用次数）。"""
    try:
        cursor = await db.execute(
            f"""SELECT COUNT(*),
                       SUM({_TOTAL_TOKENS_EXPR}),
                       SUM(COALESCE(json_extract(event_data,'$.{FIELD_PROMPT_TOKENS}'),0)),
                       SUM(COALESCE(json_extract(event_data,'$.{FIELD_COMPLETION_TOKENS}'),0)),
                       SUM(COALESCE(json_extract(event_data,'$.{FIELD_CACHED_TOKENS}'),
                                    json_extract(event_data,'$.{LEGACY_CACHED_TOKENS_KEY}'),0)),
                       SUM(COALESCE(json_extract(event_data,'$.{FIELD_REASONING_TOKENS}'),0)),
                       SUM(COALESCE(json_extract(event_data,'$.{FIELD_CACHE_MISS_TOKENS}'),0))
                FROM analytics_events WHERE {clause}""",
            params,
        )
        row = await cursor.fetchone()
        return {
            "total_tokens": row[1] or 0,
            "prompt_tokens": row[2] or 0,
            "completion_tokens": row[3] or 0,
            "cached_tokens": row[4] or 0,
            "reasoning_tokens": row[5] or 0,
            "cache_miss_tokens": row[6] or 0,
            "call_count": row[0] or 0,
        }
    except Exception:
        # 留痕：静默返回 0 会让看板显示"没用过 token"，与"真的没用量"无法区分
        _log.warning("读取 token 用量账本失败（返回 0，界面会显示无用量）", exc_info=True)
        return {
            "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "cached_tokens": 0, "reasoning_tokens": 0, "cache_miss_tokens": 0,
            "call_count": 0,
        }


async def _usage_daily(db, clause: str, params: list, days: int) -> list[dict]:
    """按天汇总（与总量同一账本口径，图表与数字不会打架）。"""
    cursor = await db.execute(
        f"""SELECT DATE(created_at, 'unixepoch') AS day,
                   SUM({_TOTAL_TOKENS_EXPR}),
                   SUM(COALESCE(json_extract(event_data,'$.{FIELD_PROMPT_TOKENS}'),0)),
                   SUM(COALESCE(json_extract(event_data,'$.{FIELD_COMPLETION_TOKENS}'),0))
            FROM analytics_events WHERE {clause}
            GROUP BY day ORDER BY day DESC LIMIT ?""",
        [*params, days],
    )
    return [
        {"day": r[0], "tokens": r[1] or 0,
         FIELD_PROMPT_TOKENS: r[2] or 0,
         FIELD_COMPLETION_TOKENS: r[3] or 0}
        for r in await cursor.fetchall()
    ]


async def estimate_context_usage(
    db,
    session_id: str,
    messages: list[dict],
    adapter=None,
    model_id: str = "gpt-4",
    model_cfg: dict | None = None,
    use_online: bool = False,
) -> dict:
    """获取当前会话的上下文占用估算。

    优先级:
      1. adapter.last_usage（厂商 API 实时返回，最准）
      2. DB 中最近一次 token_usage 事件（任务结束后持久化）
      3. 统一 count_tokens 接口（厂商 tokenizer API → tiktoken → CJK）

    返回 dict 与 /api/v1/sessions/:id/context-stats 格式兼容。
    """
    # 1) Adapter 内存中的实时 usage（Claude Code / Cursor 风格）
    if adapter is not None:
        usage = getattr(adapter, "last_usage", {}) or {}
        prompt_tokens = usage.get(FIELD_PROMPT_TOKENS, 0)
        completion_tokens = usage.get(FIELD_COMPLETION_TOKENS, 0)
        if prompt_tokens > 0:
            # 在 API 返回的真实 prompt_tokens 基础上，增量估算新增消息
            new_msgs = max(0, len(messages) - 1)
            estimated = prompt_tokens + new_msgs * 200
            return {
                "estimated_tokens": estimated,
                "source": "adapter_last_usage",
                "last_real_prompt_tokens": prompt_tokens,
                "last_real_completion_tokens": completion_tokens,
            }

    # 2) DB 中最近一次 token_usage 事件
    try:
        cursor = await db.execute(
            "SELECT event_data FROM analytics_events "
            f"WHERE event_type IN {TOKEN_USAGE_EVENT_SQL} "
            "AND json_extract(event_data, '$.session_id')=? "
            "ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        )
        row = await cursor.fetchone()
        if row:
            import json as _json
            data = _json.loads(row[0])
            prompt_tokens = data.get(FIELD_PROMPT_TOKENS, 0)
            if prompt_tokens > 0:
                new_msgs = max(0, len(messages) - data.get("message_count", 0))
                estimated = prompt_tokens + new_msgs * 400  # 保守增量
                return {
                    "estimated_tokens": estimated,
                    "source": "db_incremental",
                    "last_real_prompt_tokens": prompt_tokens,
                }
    except Exception:
        _log.debug("从 DB 获取上下文用量失败，退回统一 count_tokens", exc_info=True)

    # 3) 统一 count_tokens 接口：vendor API → tiktoken → CJK
    counted = await count_tokens(messages, model_cfg=model_cfg, prefer_online=use_online, model_id=model_id)
    return {"estimated_tokens": counted["tokens"], "source": counted["source"]}
