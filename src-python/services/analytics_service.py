"""services/analytics_service.py —— 可观测性分析（v33 双通道 + 批量缓冲）

v33 双通道写入：
  - 批量通道（task_submitted, task_completed 等）：缓冲 20 条后 executemany
  - 直写通道（task_failed, human_rejected）：直接 INSERT+COMMIT，零丢失
"""
import asyncio
import json
import logging
import time as _time
from typing import Any

from config import (
    ANALYTICS_ERROR_PREVIEW_CHARS,
    ANALYTICS_LOG_DATA_PREVIEW_CHARS,
    ANALYTICS_REASON_PREVIEW_CHARS,
    ANALYTICS_FLUSH_BATCH_SIZE,
)

_log = logging.getLogger("yuncat.analytics")

# v33: 关键事件——直接写，不可丢失
CRITICAL_EVENTS = {"task_failed", "human_rejected", "workflow_failed"}


class AnalyticsService:
    """埋点服务：非阻塞异步记录关键事件"""

    def __init__(self, db):
        self.db = db
        self._buffer: list[tuple[str, str]] = []
        self._flush_lock = asyncio.Lock()
        self._critical_lock = asyncio.Lock()

    async def track(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """记录事件（fire-and-forget）"""
        event_data = json.dumps(data or {})
        try:
            if event_type in CRITICAL_EVENTS:
                async with self._critical_lock:
                    await self.db.execute(
                        "INSERT INTO analytics_events(event_type, event_data) VALUES(?,?)",
                        (event_type, event_data),
                    )
                    await self.db.commit()
                return

            async with self._flush_lock:
                self._buffer.append((event_type, event_data))
                if len(self._buffer) >= ANALYTICS_FLUSH_BATCH_SIZE:
                    await self._flush()
        except Exception as e:
            if event_type in CRITICAL_EVENTS:
                _log.error(
                    f"Critical event lost: {event_type}, data: {str(data or {})[:ANALYTICS_LOG_DATA_PREVIEW_CHARS]}, err: {e}"
                )

    async def _flush(self) -> None:
        """批量写入缓冲"""
        if not self._buffer:
            return
        batch, self._buffer = self._buffer[:], []
        await self.db.executemany(
            "INSERT INTO analytics_events(event_type, event_data) VALUES(?,?)", batch
        )
        await self.db.commit()

    async def flush_all(self) -> None:
        """强制刷新——shutdown 时调用"""
        async with self._flush_lock:
            await self._flush()

    # ---- 预定义埋点 ----
    async def task_submitted(self, agent_id: str, intent_type: str = "", prompt_len: int = 0) -> None:
        await self.track("task_submitted", {
            "agent_id": agent_id, "intent_type": intent_type, "prompt_len": prompt_len,
        })

    async def task_completed(
        self, task_id: str, agent_id: str, duration_ms: int = 0, token_count: int = 0,
    ) -> None:
        await self.track("task_completed", {
            "task_id": task_id, "agent_id": agent_id,
            "duration_ms": duration_ms, "token_count": token_count,
        })

    async def task_failed(self, task_id: str, agent_id: str, error: str) -> None:
        await self.track("task_failed", {
            "task_id": task_id, "agent_id": agent_id, "error": error[:ANALYTICS_ERROR_PREVIEW_CHARS],
        })

    async def human_accepted(self, task_id: str, result_type: str = "text") -> None:
        await self.track("human_accepted", {
            "task_id": task_id, "result_type": result_type,
        })

    async def human_rejected(self, task_id: str, reason: str = "") -> None:
        await self.track("human_rejected", {
            "task_id": task_id, "reason": reason[:ANALYTICS_REASON_PREVIEW_CHARS],
        })

    async def workflow_completed(
        self, workflow_id: str, node_count: int = 0, duration_ms: int = 0, success_nodes: int = 0,
    ) -> None:
        await self.track("workflow_completed", {
            "workflow_id": workflow_id, "node_count": node_count,
            "duration_ms": duration_ms, "success_nodes": success_nodes,
        })

    # ---- Phase 97: 启动指标 ----
    async def startup_metrics(
        self,
        phases: dict[str, int],
        total_ms: int,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Phase 97: 记录后端启动各阶段耗时到 analytics_events。

        phases: 各阶段耗时（ms），如 {"init_ms": 12, "db_init_ms": 340, "adapters_ms": 1200, "ready_ms": 50}
        total_ms: 总启动耗时（从 lifespan 入口到 startup_stage="ready"）
        extra: 额外上下文（version, platform, python_version, is_first_run 等）
        """
        data: dict[str, Any] = {"phases": phases, "total_ms": total_ms}
        if extra:
            data.update(extra)
        await self.track("startup_metrics", data)

    async def frontend_startup(
        self,
        frontend_ms: int,
        stages: dict[str, int] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Phase 97: 记录前端启动耗时（从 StartupScreen 挂载到 backendReady）。

        frontend_ms: 前端总启动耗时（ms）
        stages: 各阶段进入时间戳（ms，相对挂载时间），如 {"init": 0, "db-init": 800, "adapters": 1500, "ready": 2100}
        extra: 额外上下文（app_version, electron_version, platform 等）
        """
        data: dict[str, Any] = {"frontend_ms": frontend_ms}
        if stages:
            data["stages"] = stages
        if extra:
            data.update(extra)
        await self.track("frontend_startup", data)

    # ---- 查询接口（Dashboard 使用） ----
    async def get_accept_rate(self, days: int = 7) -> dict:
        """用户接受率（类比 Cursor Keep Rate）"""
        cutoff = int(_time.time()) - days * 86400
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM analytics_events WHERE event_type='human_accepted' AND created_at>?",
            (cutoff,),
        )
        accepted = (await cursor.fetchone())[0]
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM analytics_events WHERE event_type='human_rejected' AND created_at>?",
            (cutoff,),
        )
        rejected = (await cursor.fetchone())[0]
        total = accepted + rejected
        return {"accepted": accepted, "rejected": rejected, "rate": accepted / total if total > 0 else 0}

    async def get_agent_usage_stats(self, days: int = 7) -> list:
        """Agent 使用排行"""
        cutoff = int(_time.time()) - days * 86400
        cursor = await self.db.execute(
            """SELECT json_extract(event_data, '$.agent_id') as agent, COUNT(*) as count
               FROM analytics_events WHERE event_type='task_submitted' AND created_at>?
               GROUP BY agent ORDER BY count DESC""",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [{"agent": r[0] or "unknown", "count": r[1]} for r in rows]

    async def get_workflow_count(self, days: int = 7) -> float:
        """工作流完成数"""
        cutoff = int(_time.time()) - days * 86400
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM analytics_events WHERE event_type='workflow_completed' AND created_at>?",
            (cutoff,),
        )
        completed = (await cursor.fetchone())[0]
        return float(completed)

    # ---- Task 7.26: LLM 全链路追踪查询接口 ----
    async def get_llm_call_metrics(self, task_id: str) -> list[dict]:
        """返回指定 task 的所有 per-LLM-call 指标，按 round 升序。

        对标 LangSmith per-call metrics / Helicone LLM observability。
        数据源：token_usage 事件（由 _track_token_usage 写入，含 duration_ms / round
        / prompt_tokens / completion_tokens / total_tokens / cached_tokens
        / reasoning_tokens / model / agent_id / provider）。

        2026-09-10：统计口径去价格化（只记 token）。同时兼容读取改名前的
        ``token_cost`` 事件——本地历史数据无需迁移即可继续聚合。
        """
        from services.llm_usage import TOKEN_USAGE_EVENT_SQL

        cursor = await self.db.execute(
            f"""SELECT event_data FROM analytics_events
               WHERE event_type IN {TOKEN_USAGE_EVENT_SQL}
                 AND json_extract(event_data, '$.task_id')=?
               ORDER BY CAST(json_extract(event_data, '$.round') AS INTEGER) ASC""",
            (task_id,),
        )
        rows = await cursor.fetchall()
        metrics: list[dict] = []
        for row in rows:
            try:
                data = json.loads(row[0])
                metrics.append(data)
            except (json.JSONDecodeError, TypeError):
                _log.debug("解析 token_usage event_data 失败", exc_info=True)
        return metrics

    async def get_llm_call_summary(self, task_id: str) -> dict:
        """聚合统计指定 task 的 LLM 调用——总延迟 / 总 token / 调用次数。

        对标 OpenTelemetry span summary / LangSmith run aggregate。
        复用 get_llm_call_metrics 在 Python 层聚合（per-task 数据量小，通常 <50 轮）。

        2026-09-10 去价格化：不再输出 total_cost_usd（calc_cost 的定价口径与
        YAML 从未对上、恒为 0，且厂商定价漂移不适合做本地统计口径）；
        新增 total_cached_tokens / total_reasoning_tokens。

        Returns:
            dict：call_count, total_duration_ms, total_tokens, total_prompt_tokens,
                  total_completion_tokens, total_cached_tokens, total_reasoning_tokens。
                  无数据时各字段为 0。
        """
        metrics = await self.get_llm_call_metrics(task_id)
        call_count = len(metrics)
        if call_count == 0:
            return {
                "call_count": 0,
                "total_duration_ms": 0,
                "total_tokens": 0,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_cached_tokens": 0,
                "total_reasoning_tokens": 0,
            }
        return {
            "call_count": call_count,
            "total_duration_ms": sum(m.get("duration_ms", 0) for m in metrics),
            "total_tokens": sum(m.get("total_tokens", 0) for m in metrics),
            "total_prompt_tokens": sum(m.get("prompt_tokens", 0) for m in metrics),
            "total_completion_tokens": sum(m.get("completion_tokens", 0) for m in metrics),
            "total_cached_tokens": sum(m.get("cached_tokens", 0) for m in metrics),
            "total_reasoning_tokens": sum(m.get("reasoning_tokens", 0) for m in metrics),
        }
