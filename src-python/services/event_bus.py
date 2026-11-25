"""services/event_bus.py —— 内部事件总线（发布/订阅解耦 Service）

v39: 轻量级事件总线——各 Service 通过 EventBus 松耦合通信。
v40 (Batch 7.4): 关键事件持久化到 event_outbox，崩溃后可重投。

典型用例：
  - agent_service 发布 task_completed → analytics_service 订阅后记录埋点
  - tool_service 发布 tool_executed → rules_service 订阅后检查规则冲突
  - memory_service 发布 dream_completed → agent_service 订阅后刷新上下文

线程安全：asyncio.Queue + asyncio.create_task 消费者模式。
"""
import asyncio
import json as _json
import logging
import time as _time
import uuid as _uuid
from typing import Any, Callable, Awaitable

from config import QUEUE_MAXSIZE  # Phase 48-F: 背压上限统一管理
from config import EVENT_BUS_MAX_RESTART_DELAY_SEC  # Phase 5 P2: 重启退避上限
from config import (
    EVENT_BUS_PERSISTED_TYPES,
    EVENT_BUS_OUTBOX_MAX_DELIVERY_ATTEMPTS,
    EVENT_BUS_OUTBOX_POLL_INTERVAL_SEC,
    EVENT_BUS_OUTBOX_REDELIVERY_GRACE_SEC,
    EVENT_BUS_OUTBOX_BATCH_SIZE,
    EVENT_BUS_BACKOFF_RESET_SUCCESSES,
    EVENT_BUS_SLOW_HANDLER_SEC,
)

_log = logging.getLogger("yuncat.event_bus")

# 事件订阅回调签名: async def handler(event_type: str, data: dict) -> None
EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class EventBus:
    """轻量级发布/订阅事件总线。

    每个事件类型可以有多个订阅者。
    发布者 fire-and-forget——不阻塞主流程。
    Batch 7.4: 对 persisted_types 中的关键事件写入 SQLite event_outbox，
    后台轮询保证 at-least-once 投递；崩溃后可恢复重投。
    """

    def __init__(
        self,
        db: Any | None = None,
        persisted_types: set[str] | None = None,
        max_delivery_attempts: int = EVENT_BUS_OUTBOX_MAX_DELIVERY_ATTEMPTS,
        poll_interval: float = EVENT_BUS_OUTBOX_POLL_INTERVAL_SEC,
        redelivery_grace: float = EVENT_BUS_OUTBOX_REDELIVERY_GRACE_SEC,
        poll_batch_size: int = EVENT_BUS_OUTBOX_BATCH_SIZE,
    ):
        self._subscribers: dict[str, list[EventHandler]] = {}
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(
            maxsize=QUEUE_MAXSIZE
        )
        self._consumer_task: asyncio.Task | None = None
        self._dropped = 0  # 队列满时丢弃计数

        # Batch 7.4: outbox 持久化配置
        self._db = db
        self._persisted_types = frozenset(persisted_types or set())
        self._max_attempts = max(1, max_delivery_attempts)
        self._poll_interval = max(1.0, poll_interval)
        self._redelivery_grace = max(0.0, redelivery_grace)
        self._poll_batch_size = max(1, poll_batch_size)
        self._outbox_poll_task: asyncio.Task | None = None

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """订阅事件——handler 会在事件发布时被异步调用"""
        self._subscribers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """取消订阅"""
        handlers = self._subscribers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    async def publish(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """发布事件——入队后立即返回，不阻塞调用方。

        若事件类型在 persisted_types 中且提供了 db，会先同步写入 event_outbox。
        """
        payload = data or {}
        outbox_id: str | None = None
        if self._db and event_type in self._persisted_types:
            try:
                outbox_id = await self._persist_event(event_type, payload)
            except Exception:
                _log.exception("Failed to persist event '%s' to outbox", event_type)

        # 携带 outbox id 供内存消费者处理成功后标记 processed_at
        if outbox_id:
            payload = {**payload, "_outbox_id": outbox_id}

        try:
            self._queue.put_nowait((event_type, payload))
        except asyncio.QueueFull:
            # 丢弃最旧事件，保留最新
            try:
                self._queue.get_nowait()
                self._queue.put_nowait((event_type, payload))
            except (asyncio.QueueFull, asyncio.QueueEmpty):
                pass
            self._dropped += 1
            if self._dropped % 100 == 0:
                _log.warning(
                    "EventBus dropped %d events due to consumer backlog", self._dropped
                )

    async def start(self) -> None:
        """启动异步消费者与 outbox 轮询器"""
        if self._consumer_task is None:
            self._consumer_task = asyncio.create_task(self._consume_loop())
        if (
            self._db
            and self._persisted_types
            and self._outbox_poll_task is None
        ):
            self._outbox_poll_task = asyncio.create_task(self._outbox_poll_loop())

    async def stop(self) -> None:
        """停止消费者与 outbox 轮询器——等待队列清空"""
        if self._consumer_task:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            self._consumer_task = None
        if self._outbox_poll_task:
            self._outbox_poll_task.cancel()
            try:
                await self._outbox_poll_task
            except asyncio.CancelledError:
                pass
            self._outbox_poll_task = None

    def get_stats(self) -> dict:
        """Phase 38: 暴露事件总线运行状态——供 get_metrics 工具使用"""
        stats = {
            "queue_size": self._queue.qsize(),
            "dropped_events": self._dropped,
            "subscriber_count": len(self._subscribers),
        }
        if self._persisted_types:
            stats["persisted_types"] = sorted(self._persisted_types)
        return stats

    async def get_outbox_stats(self) -> dict:
        """Batch 7.4: 返回 outbox 统计（pending / dead-letter 计数）"""
        if not self._db:
            return {"pending": 0, "total": 0, "failed": 0}
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE processed_at IS NULL AND attempts < ?",
            (self._max_attempts,),
        )
        pending = (await cursor.fetchone())[0]
        cursor = await self._db.execute("SELECT COUNT(*) FROM event_outbox")
        total = (await cursor.fetchone())[0]
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE processed_at IS NULL AND attempts >= ?",
            (self._max_attempts,),
        )
        failed = (await cursor.fetchone())[0]
        return {"pending": pending, "total": total, "failed": failed}

    # ============================================================
    # CodeWhale 借鉴 #14: 事件重放（since_seq）
    # ============================================================
    # 对标 CodeWhale SSE since_seq 参数——断线重连时重放缺失事件。
    # 利用 SQLite 隐式 rowid 作为单调递增 seq（event_outbox.id 为 TEXT PRIMARY KEY，
    # 表未使用 WITHOUT ROWID，rowid 自动分配且单调递增）。
    # 注: rowid 在行删除后可能被复用，但桌面单用户场景下事件保留期足够长，可接受。
    # 如未来需严格保证 seq 不复用，可改用单独的 event_seq 计数器表。

    async def replay_since(
        self,
        since_seq: int = 0,
        topic: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """重放 seq > since_seq 的事件——供 SSE/WebSocket 断线重连使用。

        Args:
            since_seq: 上次收到的最大 seq（rowid），返回此值之后的事件。
                       0 表示从头开始。
            topic: 可选——只返回指定 topic 的事件。None 表示所有 topic。
            limit: 最多返回的事件数（默认 100，防单次拉取过多）。

        Returns:
            list[{seq, id, topic, payload, created_at}]，按 seq 升序排列。
        """
        if not self._db:
            return []
        if topic:
            cursor = await self._db.execute(
                "SELECT rowid, id, topic, payload, created_at FROM event_outbox "
                "WHERE rowid > ? AND topic = ? ORDER BY rowid ASC LIMIT ?",
                (since_seq, topic, max(1, min(limit, 1000))),
            )
        else:
            cursor = await self._db.execute(
                "SELECT rowid, id, topic, payload, created_at FROM event_outbox "
                "WHERE rowid > ? ORDER BY rowid ASC LIMIT ?",
                (since_seq, max(1, min(limit, 1000))),
            )
        rows = await cursor.fetchall()
        return [
            {
                "seq": row[0],
                "id": row[1],
                "topic": row[2],
                "payload": _json.loads(row[3] or "{}"),
                "created_at": row[4],
            }
            for row in rows
        ]

    async def get_last_seq(self) -> int:
        """返回当前 outbox 中最大的 seq（rowid）——供客户端初始化断点。

        无事件时返回 0。
        """
        if not self._db:
            return 0
        cursor = await self._db.execute("SELECT MAX(rowid) FROM event_outbox")
        row = await cursor.fetchone()
        return row[0] if row and row[0] else 0

    # ============================================================
    # Outbox 持久化内部方法
    # ============================================================

    async def _persist_event(self, event_type: str, data: dict[str, Any]) -> str:
        """将事件写入 event_outbox，返回 outbox id"""
        eid = str(_uuid.uuid4())
        await self._db.execute(
            "INSERT INTO event_outbox(id, topic, payload, created_at) VALUES(?,?,?,?)",
            (eid, event_type, _json.dumps(data, ensure_ascii=False), int(_time.time())),
        )
        await self._db.commit()
        return eid

    async def _mark_processed(self, outbox_id: str) -> None:
        """标记 outbox 事件已成功投递"""
        if not self._db:
            return
        try:
            await self._db.execute(
                "UPDATE event_outbox SET processed_at=? WHERE id=?",
                (int(_time.time()), outbox_id),
            )
            await self._db.commit()
        except Exception:
            _log.exception("Failed to mark outbox event %s as processed", outbox_id)

    async def _record_attempt(self, outbox_id: str, error: str) -> None:
        """记录一次失败的投递尝试"""
        if not self._db:
            return
        try:
            await self._db.execute(
                "UPDATE event_outbox SET attempts=attempts+1, error=? WHERE id=?",
                (error, outbox_id),
            )
            await self._db.commit()
        except Exception:
            _log.exception("Failed to record outbox attempt for %s", outbox_id)

    async def _outbox_poll_loop(self) -> None:
        """后台扫描未处理 outbox 事件并重投"""
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._poll_pending()
            except asyncio.CancelledError:
                break
            except Exception:
                _log.exception("EventBus outbox poll loop crashed")

    async def _poll_pending(self) -> None:
        """扫描可重投的 outbox 事件并投递给订阅者"""
        if not self._db or not self._persisted_types:
            return
        # 给内存消费者留出 grace 时间，避免正常路径下立即重复投递
        cutoff = int(_time.time() - self._redelivery_grace)
        cursor = await self._db.execute(
            "SELECT id, topic, payload FROM event_outbox "
            "WHERE processed_at IS NULL AND attempts < ? AND created_at <= ? "
            "ORDER BY created_at ASC LIMIT ?",
            (self._max_attempts, cutoff, self._poll_batch_size),
        )
        rows = await cursor.fetchall()
        for outbox_id, topic, payload_json in rows:
            try:
                data = _json.loads(payload_json or "{}")
            except Exception:
                _log.warning("Failed to decode outbox payload for %s", outbox_id)
                await self._record_attempt(outbox_id, "json_decode_error")
                continue
            await self._dispatch_event(topic, data, outbox_id)

    async def _dispatch_event(
        self,
        event_type: str,
        data: dict[str, Any],
        outbox_id: str | None = None,
    ) -> bool:
        """将事件分发给订阅者，并处理 outbox 状态更新。

        返回 True 表示所有 handler 均干净分发（无异常）。慢 handler 仅记 warning，
        不并发化、不超时中断（保持单消费者串行语义）。
        """
        handlers = self._subscribers.get(event_type, [])
        if not handlers:
            if outbox_id:
                await self._mark_processed(outbox_id)
            return True

        any_error = False
        for handler in handlers:
            # Batch 5a C5: 单 handler 分发计时——超阈值记 warning（含 handler 名 + 事件类型）
            started = _time.monotonic()
            try:
                await handler(event_type, data)
            except Exception:
                any_error = True
                _log.exception("EventBus handler for '%s' failed", event_type)
            elapsed = _time.monotonic() - started
            if elapsed > EVENT_BUS_SLOW_HANDLER_SEC:
                handler_name = (
                    getattr(handler, "__qualname__", None)
                    or getattr(handler, "__name__", None)
                    or repr(handler)
                )
                _log.warning(
                    "EventBus slow handler: handler=%s event=%s took %.2fs",
                    handler_name, event_type, elapsed,
                )

        if outbox_id:
            if any_error:
                await self._record_attempt(outbox_id, "handler_failed")
            else:
                await self._mark_processed(outbox_id)
        return not any_error

    async def _consume_loop(self) -> None:
        """后台消费循环——从队列取事件，分发给所有订阅者。
        消费者崩溃时自动重启，确保 EventBus 通信不会静默失效。"""
        restart_delay = 0.5
        max_restart_delay = EVENT_BUS_MAX_RESTART_DELAY_SEC
        consecutive_clean = 0
        while True:
            try:
                clean = await self._consume_once()
                # Batch 5a C5: 连续 N 次无异常分发后退避重置为初始值（断路器 half-open 对齐）
                if clean:
                    consecutive_clean += 1
                    if consecutive_clean >= EVENT_BUS_BACKOFF_RESET_SUCCESSES:
                        restart_delay = 0.5
                        consecutive_clean = 0
                else:
                    consecutive_clean = 0
            except asyncio.CancelledError:
                break
            except Exception:
                _log.exception("EventBus consumer crashed, restarting in %.1fs", restart_delay)
                await asyncio.sleep(restart_delay)
                restart_delay = min(restart_delay * 2, max_restart_delay)
                consecutive_clean = 0

    async def _consume_once(self) -> bool:
        """消费并分发单个事件。返回该次分发是否干净（无 handler 异常）。"""
        event_type, data = await self._queue.get()
        outbox_id = data.pop("_outbox_id", None)
        clean = await self._dispatch_event(event_type, data, outbox_id)
        self._queue.task_done()
        return clean


# 全局单例
_event_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """获取全局 EventBus——lifespan 中通过 _set_event_bus 注入"""
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus


def _set_event_bus(eb: EventBus) -> None:
    """由 lifespan 注入实际的 EventBus 实例"""
    global _event_bus
    _event_bus = eb


# ============================================================
# 预定义事件类型常量
# ============================================================
class Events:
    TASK_SUBMITTED = "task_submitted"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    TOOL_EXECUTED = "tool_executed"
    TOOL_CONFIRMED = "tool_confirmed"
    DREAM_COMPLETED = "dream_completed"
    WORKFLOW_NODE_STARTED = "workflow_node_started"
    WORKFLOW_NODE_COMPLETED = "workflow_node_completed"
    WORKFLOW_COMPLETED = "workflow_completed"
    AGENT_STARTED = "agent_started"
    AGENT_STOPPED = "agent_stopped"
    MEMORY_STORED = "memory_stored"
    ASSISTANT_TURN_COMPLETE = "assistant_turn_complete"  # Task 6.6: 记忆即时提取触发
    RULE_TRIGGERED = "rule_triggered"
    TOOL_OUTPUT_CHUNK = "tool_output_chunk"  # Phase 47: 命令实时输出流
    LONG_TASK_PHASE = "long_task_phase"  # Phase 106: 长任务阶段切换
    APPROVAL_PENDING = "approval_pending"  # Phase 106: 待审批通知
    # Phase 43 E.1: 融合事件类型
    BACKEND_HEALTH_CHECK = "backend_health_check"
    CROSS_BACKEND_DELEGATION = "cross_backend_delegation"
    MEMORY_SYNC_COMPLETED = "memory_sync_completed"


# ═══════════════════════════════════════════════════════════
# Phase 45 D.8: tool_done 事件标准化结构
#
# 对标 Claude Code 14步工具管线中的埋点字段。
# 所有 tool_done 事件的发布者和消费者应遵循此结构。
# ═══════════════════════════════════════════════════════════
STANDARD_TOOL_EVENT_FIELDS = {
    "tool_name": "str — 工具名",
    "duration_ms": "int — 执行耗时（毫秒）",
    "success": "bool — 是否成功",
    "input_size_chars": "int — 工具输入参数序列化后的字符数",
    "output_size_chars": "int — 工具输出序列化后的字符数",
    "concurrency_mode": "'safe' | 'exclusive' | 'safe_with_lock' — 并发安全级别",
    "retry_count": "int — 自动重试次数（0=未重试）",
    "cache_hit": "bool | None — 是否命中缓存（无缓存时为 None）",
}
