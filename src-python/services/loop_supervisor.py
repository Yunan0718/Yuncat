"""services/loop_supervisor.py —— 统一后台 Loop 调度器 (Phase 45)

对标 OpenAI Symphony OTP Supervisor，管理所有自主运行的 Loop 进程。

核心概念:
  LoopSpec     — 注册规格 (对标 OTP child_spec)
  LoopStatus   — 运行时状态 (对标 OTP child status)
  RestartStrategy — 重启策略 (:permanent / :transient / :temporary)

Supervisor 职责:
  1. register()  — 注册 Loop 并持久化到 DB
  2. start_all() — 按优先级启动所有已注册 Loop
  3. start_one() — 幂等启动单个 Loop（事件触发场景）
  4. get_status()— 查询所有 Loop 运行时状态
  5. shutdown_all() — 按优先级倒序优雅关闭
"""
import asyncio
import json as _json
import logging
import time as _time
from dataclasses import dataclass, field
from enum import Enum

from config import (
    LOOP_SHUTDOWN_TIMEOUT_SECONDS,
    LOOP_TRIGGER_MAX_ATTEMPTS,
    LOOP_TRIGGER_CLAIM_STALE_SEC,
)

_log = logging.getLogger("yuncat.supervisor")


class RestartStrategy(Enum):
    PERMANENT = "permanent"    # 崩溃后始终重启
    TRANSIENT = "transient"    # 正常退出不重启，崩溃重启
    TEMPORARY = "temporary"    # 永不重启


@dataclass
class LoopSpec:
    """Loop 注册规格——对标 OTP child_spec。

    coro_fn 签名: async fn(state: dict) -> None
    state 会在每次 tick 前传入，允许 Loop 跨重启携带状态。
    """
    loop_id: str
    coro_fn: callable                     # async callable: fn(state: dict) -> None
    loop_type: str = "timer"              # timer / event / spec / goal
    interval: int = 60                    # 秒（timer 类型）
    priority: int = 0                     # 数字越大优先级越高。负数=后台低优
    strategy: RestartStrategy = RestartStrategy.PERMANENT
    max_restarts: int = 3
    require_db: bool = True
    cooldown: int = 5                     # 重启冷却秒数
    state: dict = field(default_factory=dict)


@dataclass
class LoopStatus:
    """Loop 运行时状态——对标 OTP child status"""
    loop_id: str
    status: str = "registered"  # registered / running / paused / degraded / escalated / completed
    last_run: float = 0.0
    last_ok: float = 0.0
    restart_count: int = 0
    error_count: int = 0
    last_error: str = ""
    iteration_count: int = 0
    task: asyncio.Task | None = None
    last_escalated_at: float = 0.0


class LoopSupervisor:
    """统一后台 Loop 调度器。

    使用方式 (lifespan.py):
        supervisor = LoopSupervisor(db=conn)
        await supervisor.register(LoopSpec("healer", healer_fn, interval=30, priority=-1))
        await supervisor.start_all()

    关闭 (shutdown):
        await supervisor.shutdown_all()
    """

    # escalation 冷却（秒）——防止 knowledge_nodes spamming
    ESCALATION_COOLDOWN = 3600

    def __init__(self, db):
        self.db = db
        self._loops: dict[str, LoopStatus] = {}
        self._specs: dict[str, LoopSpec] = {}
        self._max_concurrent = 20  # Phase 48-H: 放宽上限对标主流
        self._sem: asyncio.Semaphore | None = None
        self._shutdown: asyncio.Event = asyncio.Event()  # Phase 46 fix: init as Event not None

    def register_sync(self, spec: LoopSpec) -> None:
        """同步注册——用于 lifespan 中的非异步初始化场景。
        DB 持久化延迟到 start_all() 时批量写入。
        """
        self._specs[spec.loop_id] = spec
        if spec.loop_id not in self._loops:
            self._loops[spec.loop_id] = LoopStatus(loop_id=spec.loop_id,
                                                    status="registered")
        _log.info("Loop registered (sync): %s (type=%s, interval=%ds, priority=%d)",
                  spec.loop_id, spec.loop_type, spec.interval, spec.priority)

    def set_dependency(self, loop_id: str, depends_on: list[str]) -> None:
        """Phase: Loop 依赖图——loop A 必须在 loop B 之后执行。

        depends_on: 此 Loop 依赖的前置 Loop ID 列表。
        start_all() 按拓扑顺序启动——先启动被依赖的，再启动依赖方。
        """
        if not hasattr(self, '_dependencies'):
            self._dependencies: dict[str, list[str]] = {}
        self._dependencies[loop_id] = depends_on

    async def start_all_with_deps(self) -> None:
        """按依赖图拓扑排序 + 优先级分层启动。被依赖的 Loop 先启动。"""
        if not hasattr(self, '_dependencies') or not self._dependencies:
            return await self.start_all()

        # Kahn 算法拓扑排序
        in_degree: dict[str, int] = {}
        children: dict[str, list[str]] = {}
        for loop_id in self._specs:
            in_degree.setdefault(loop_id, 0)
            children.setdefault(loop_id, [])
        for loop_id, deps in self._dependencies.items():
            in_degree[loop_id] = len(deps)
            for dep_id in deps:
                children.setdefault(dep_id, []).append(loop_id)

        queue = [lid for lid, deg in in_degree.items() if deg == 0]
        started = set()
        self._sem = asyncio.Semaphore(self._max_concurrent)
        self._shutdown = asyncio.Event()

        while queue:
            layer = []
            for lid in queue:
                if lid in started or lid not in self._specs:
                    continue
                spec = self._specs[lid]
                loop = self._loops[lid]
                if loop.status != "running":
                    task = asyncio.create_task(
                        self._supervised_loop(spec), name=f"loop_{lid}"
                    )
                    loop.task = task
                    loop.status = "running"
                    _log.info("Loop %s started (deps=%s)", lid, self._dependencies.get(lid, []))
                started.add(lid)
                layer.append(lid)

                # 释放子节点
                for child_id in children.get(lid, []):
                    in_degree[child_id] -= 1
                    if in_degree[child_id] <= 0:
                        if child_id not in [x for x in queue if x not in started]:
                            queue.append(child_id)

            # 按优先级排序当前层
            layer.sort(key=lambda lid: -self._specs[lid].priority)
            queue = [lid for lid in queue if lid not in started]
            await asyncio.sleep(0.5)  # 给被依赖 Loop 的启动留时间

        remaining = [lid for lid in self._specs if lid not in started]
        for lid in remaining:
            spec = self._specs[lid]
            task = asyncio.create_task(self._supervised_loop(spec), name=f"loop_{lid}")
            self._loops[lid].task = task
            self._loops[lid].status = "running"

        _log.info("Supervisor started (with deps): %d/%d loops running", len(started), len(self._loops))

    def set_interval(self, loop_id: str, new_interval: int) -> bool:
        """Phase: 运行时动态调整 Loop 间隔——不需重启。

        对标 Claude Code: 根据系统负载动态调整检查频率。
        """
        if loop_id not in self._specs:
            return False
        self._specs[loop_id].interval = new_interval
        _log.info("Loop %s interval adjusted to %ds", loop_id, new_interval)
        return True

    async def register(self, spec: LoopSpec) -> None:
        """注册一个 Loop。若 DB 中已有运行中的同 ID Loop，恢复其状态。

        幂等——重复注册同一个 loop_id 只会更新 spec，不会创建重复任务。
        """
        # 持久化到 DB
        try:
            await self.db.execute(
                "INSERT OR REPLACE INTO loop_registry(loop_id, loop_type, status, config, state) "
                "VALUES(?,?,?,?,?)",
                (spec.loop_id, spec.loop_type, "registered",
                 _json.dumps({"interval": spec.interval, "priority": spec.priority,
                            "strategy": spec.strategy.value, "max_restarts": spec.max_restarts}),
                 _json.dumps(spec.state)),
            )
            await self.db.commit()
        except Exception:
            _log.debug("Failed to persist loop %s to DB", spec.loop_id)

        self._specs[spec.loop_id] = spec
        if spec.loop_id not in self._loops:
            self._loops[spec.loop_id] = LoopStatus(loop_id=spec.loop_id,
                                                    status="registered")
        _log.info("Loop registered: %s (type=%s, interval=%ds, priority=%d)",
                  spec.loop_id, spec.loop_type, spec.interval, spec.priority)

    async def start_one(self, loop_id: str) -> None:
        """启动单个 Loop——不影响已在运行的 Loop。幂等。

        用于事件触发动态注册后立即启动。
        """
        if loop_id not in self._specs:
            raise ValueError(f"Loop {loop_id} not registered")
        if self._loops[loop_id].status == "running":
            _log.debug("Loop %s already running, skip start_one", loop_id)
            return
        if self._shutdown.is_set():
            _log.warning("Loop %s won't start: supervisor is shutting down", loop_id)
            return
        # Phase 46 fix: ensure semaphore is initialized even without start_all()
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_concurrent)
        if self._shutdown is None:
            self._shutdown = asyncio.Event()
        spec = self._specs[loop_id]
        task = asyncio.create_task(
            self._supervised_loop(spec), name=f"loop_{loop_id}"
        )
        self._loops[loop_id].task = task
        self._loops[loop_id].status = "running"
        _log.info("Loop %s started via start_one", loop_id)

    async def start_all(self) -> None:
        """启动所有已注册的 Loop——按优先级降序（高优先先启动）。

        用于首次启动（lifespan startup）。
        """
        self._sem = asyncio.Semaphore(self._max_concurrent)
        self._shutdown = asyncio.Event()
        # Batch 5a C2: 重启恢复——上一进程崩溃残留的 claimed trigger 重置回 pending
        try:
            await self._recover_stale_claims()
        except Exception:
            _log.debug("Failed to recover stale loop claims", exc_info=True)
        ordered = sorted(self._specs.values(), key=lambda s: -s.priority)
        for spec in ordered:
            # 从 DB 恢复状态
            try:
                await self._restore_from_db(spec)
            except Exception:
                _log.debug("从 DB 恢复 Loop 状态失败", exc_info=True)
            if self._loops[spec.loop_id].status == "running":
                continue  # 从 DB 恢复的 running Loop 稍后通过 resume_after_restart 处理
            task = asyncio.create_task(
                self._supervised_loop(spec), name=f"loop_{spec.loop_id}"
            )
            self._loops[spec.loop_id].task = task
            self._loops[spec.loop_id].status = "running"
        _log.info("Supervisor started: %d loops running", len(self._loops))

    async def _restore_from_db(self, spec: LoopSpec) -> None:
        """从 loop_registry 表恢复 Loop 状态（跨重启）。"""
        try:
            cursor = await self.db.execute(
                "SELECT status, state, error_count, last_error FROM loop_registry WHERE loop_id=?",
                (spec.loop_id,),
            )
            row = await cursor.fetchone()
            if row and row[0] in ("running", "degraded"):
                loop = self._loops[spec.loop_id]
                loop.status = row[0]
                loop.error_count = row[2] or 0
                loop.last_error = row[3] or ""
                try:
                    spec.state = _json.loads(row[1]) if row[1] else {}
                except Exception:
                    _log.debug("解析 Loop state JSON 失败", exc_info=True)
                _log.info("Loop %s restored from DB: status=%s, errors=%d",
                         spec.loop_id, loop.status, loop.error_count)
        except Exception:
            _log.debug("从 DB 恢复 Loop 状态失败", exc_info=True)

    async def _supervised_loop(self, spec: LoopSpec) -> None:
        """带监督的 Loop 包装器。

        Timer 类型: sleep(interval) → 信号量 → coro_fn(state)
        Event 类型: 轮询 loop_trigger_queue → coro_fn(payload)
        Spec/Goal 类型: 委托 run_spec_loop（已在 deliberation_service 中实现）
        """
        loop = self._loops[spec.loop_id]
        while self._shutdown and not self._shutdown.is_set():
            try:
                if spec.loop_type == "event":
                    await self._tick_event_loop(spec, loop)
                elif spec.loop_type in ("spec", "goal"):
                    await self._tick_spec_loop(spec, loop)
                else:
                    await self._tick_timer_loop(spec, loop)
            except asyncio.CancelledError:
                loop.status = "completed"
                break
            except Exception as e:
                loop.error_count += 1
                loop.last_error = str(e)[:500]
                _log.error("Loop %s error #%d: %s", spec.loop_id, loop.error_count, e)
                if loop.error_count >= spec.max_restarts:
                    if _time.time() - loop.last_escalated_at > self.ESCALATION_COOLDOWN:
                        await self._escalate(spec.loop_id, loop)
                        loop.last_escalated_at = _time.time()
                    if spec.strategy == RestartStrategy.PERMANENT:
                        loop.error_count = 0
                        loop.status = "degraded"
                        await asyncio.sleep(spec.cooldown)
                    else:
                        break
                else:
                    await asyncio.sleep(spec.cooldown)

    async def _tick_timer_loop(self, spec: LoopSpec, loop: LoopStatus) -> None:
        """定时 Loop 的单个 tick: sleep → 信号量 → 执行 → 心跳。"""
        await asyncio.sleep(spec.interval)
        if self._shutdown and self._shutdown.is_set():
            raise asyncio.CancelledError()
        async with self._sem:
            loop.last_run = _time.time()
            await spec.coro_fn(spec.state)
            loop.last_ok = _time.time()
            loop.error_count = 0
            loop.iteration_count += 1
            await self._heartbeat(spec.loop_id, loop.iteration_count)

    async def _tick_event_loop(self, spec: LoopSpec, loop: LoopStatus) -> None:
        """事件驱动 Loop: 轮询 loop_trigger_queue，有事件时处理。"""
        await asyncio.sleep(max(spec.interval, 10))  # 最少 10s 间隔
        if self._shutdown and self._shutdown.is_set():
            raise asyncio.CancelledError()
        trigger_id = None
        try:
            cursor = await self.db.execute(
                "SELECT id, trigger_type, payload FROM loop_trigger_queue "
                "WHERE (loop_id=? OR loop_id IS NULL) AND status='pending' "
                "ORDER BY priority DESC, created_at ASC LIMIT 1",
                (spec.loop_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return
            trigger_id, trigger_type, payload_str = row[0], row[1], row[2]
            # 认领事件
            await self.db.execute(
                "UPDATE loop_trigger_queue SET status='claimed', claimed_by=?, claimed_at=? "
                "WHERE id=?",
                (spec.loop_id, int(_time.time()), trigger_id),
            )
            await self.db.commit()
            async with self._sem:
                loop.last_run = _time.time()
                payload = _json.loads(payload_str) if payload_str else {}
                payload["trigger_type"] = trigger_type  # Phase 45 修复: 注入触发器类型
                payload["trigger_id"] = trigger_id
                await spec.coro_fn(payload)
                loop.last_ok = _time.time()
                loop.error_count = 0
                loop.iteration_count += 1
            # 标记完成
            await self.db.execute(
                "UPDATE loop_trigger_queue SET status='completed', completed_at=? WHERE id=?",
                (int(_time.time()), trigger_id),
            )
            await self.db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Batch 5a C2: 处理异常时回收 claimed——重投或标记 dead，避免永久滞留
            if trigger_id is not None:
                await self._requeue_failed_trigger(trigger_id, str(e)[:500])
            raise  # 交给 _supervised_loop 处理

    async def _requeue_failed_trigger(self, trigger_id: int, error: str) -> None:
        """Batch 5a C2: 触发处理失败 → attempts+1 重投；超上限标记 dead 不再重投。

        对齐 event_bus.py outbox 的 attempts 模式（loop_trigger_queue 已有 attempts 列）。
        """
        try:
            cursor = await self.db.execute(
                "UPDATE loop_trigger_queue SET attempts=attempts+1 WHERE id=? RETURNING attempts",
                (trigger_id,),
            )
            row = await cursor.fetchone()
            attempts = row[0] if row else LOOP_TRIGGER_MAX_ATTEMPTS
            if attempts >= LOOP_TRIGGER_MAX_ATTEMPTS:
                await self.db.execute(
                    "UPDATE loop_trigger_queue SET status='dead', error=? WHERE id=?",
                    (error, trigger_id),
                )
            else:
                await self.db.execute(
                    "UPDATE loop_trigger_queue SET status='pending', claimed_by=NULL, "
                    "claimed_at=NULL WHERE id=?",
                    (trigger_id,),
                )
            await self.db.commit()
        except Exception:
            _log.exception("Failed to requeue loop trigger %s", trigger_id)

    async def _recover_stale_claims(self) -> int:
        """Batch 5a C2: 重启恢复——claimed 超过阈值的 trigger（进程已死）重置回 pending。"""
        cutoff = int(_time.time() - LOOP_TRIGGER_CLAIM_STALE_SEC)
        cursor = await self.db.execute(
            "UPDATE loop_trigger_queue SET status='pending', claimed_by=NULL, claimed_at=NULL "
            "WHERE status='claimed' AND claimed_at < ?",
            (cutoff,),
        )
        await self.db.commit()
        if cursor.rowcount:
            _log.warning("Recovered %d stale claimed loop triggers", cursor.rowcount)
        return cursor.rowcount

    async def _tick_spec_loop(self, spec: LoopSpec, loop: LoopStatus) -> None:
        """Spec/Goal Loop: 委托给 coro_fn 执行完整的三阶段循环。

        coro_fn 应该调用 deliberation_service.run_spec_loop() 并在内部管理生命周期。
        """
        async with self._sem:
            loop.last_run = _time.time()
            await spec.coro_fn(spec.state)
            loop.last_ok = _time.time()
            loop.error_count = 0
            loop.iteration_count += 1
            await self._heartbeat(spec.loop_id, loop.iteration_count)
        # Spec Loop 完成后不自动重启——它是单次任务
        # Phase 46 fix: 设置 completed + raise CancelledError 终止 while 循环
        loop.status = "completed"
        await self.db.execute(
            "UPDATE loop_registry SET status='completed' WHERE loop_id=?",
            (spec.loop_id,),
        )
        await self.db.commit()
        raise asyncio.CancelledError()

    async def _heartbeat(self, loop_id: str, count: int) -> None:
        """写入心跳——供 get_own_metrics 工具查询。"""
        try:
            await self.db.execute(
                "INSERT OR REPLACE INTO loop_heartbeats(loop_id, last_beat, iteration_count) "
                "VALUES(?,?,?)",
                (loop_id, int(_time.time()), count),
            )
            await self.db.commit()
        except Exception:
            _log.debug("写入 Loop 心跳失败", exc_info=True)

    async def _escalate(self, loop_id: str, loop: LoopStatus) -> None:
        """Loop 多次失败 → 升级到 knowledge_nodes + 更新 DB 状态。"""
        try:
            await self.db.execute(
                "UPDATE loop_registry SET status='escalated', error_count=?, last_error=? "
                "WHERE loop_id=?",
                (loop.error_count, loop.last_error or "", loop_id),
            )
            await self.db.commit()
            # 写入 knowledge_nodes 供 Dashboard 可见
            await self.db.execute(
                "INSERT OR REPLACE INTO knowledge_nodes(id, title, content, source, "
                "score, tags, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (f"escalation_loop_{loop_id}",
                 f"Loop 升级: {loop_id}",
                 _json.dumps({
                     "loop_id": loop_id,
                     "error_count": loop.error_count,
                     "last_error": loop.last_error,
                     "action_required": (
                         f"Loop '{loop_id}' 连续 {loop.error_count} 次失败。"
                         "检查 backend.log 排查根因。"
                     ),
                 }, ensure_ascii=False),
                 "loop_supervisor", 0.9, '["escalation","needs_review","loop"]',
                 int(_time.time()), int(_time.time())),
            )
            await self.db.commit()
        except Exception:
            _log.debug("Failed to escalate loop %s", loop_id)

    def get_status(self) -> dict:
        """获取所有 Loop 的运行状态——供 get_own_metrics 工具和 Dashboard 使用。"""
        return {
            lid: {
                "status": ls.status,
                "last_run": ls.last_run,
                "last_ok": ls.last_ok,
                "error_count": ls.error_count,
                "restart_count": ls.restart_count,
                "iteration_count": ls.iteration_count,
                "last_error": ls.last_error[:200] if ls.last_error else "",
            }
            for lid, ls in self._loops.items()
        }

    async def resume_after_restart(self) -> int:
        """进程重启后恢复所有之前处于 running/degraded 状态的 Loop。

        从 loop_registry 表读取状态，重新创建 asyncio task。
        返回恢复的 Loop 数量。
        """
        restored = 0
        try:
            cursor = await self.db.execute(
                "SELECT loop_id, loop_type, status, state, error_count "
                "FROM loop_registry WHERE status IN ('running','degraded')"
            )
            rows = await cursor.fetchall()
            for row in rows:
                loop_id, loop_type, status, state_str, error_count = (
                    row[0], row[1], row[2], row[3], row[4] or 0
                )
                if loop_id not in self._specs:
                    continue  # 未被当前配置注册的 Loop 不恢复
                spec = self._specs[loop_id]
                try:
                    spec.state = _json.loads(state_str) if state_str else {}
                except Exception:
                    _log.debug("解析 Loop state JSON 失败", exc_info=True)
                loop = self._loops.get(loop_id)
                if not loop:
                    loop = LoopStatus(loop_id=loop_id)
                    self._loops[loop_id] = loop
                loop.status = status
                loop.error_count = error_count
                task = asyncio.create_task(
                    self._supervised_loop(spec), name=f"loop_{loop_id}"
                )
                loop.task = task
                loop.status = "running"
                restored += 1
                _log.info("Loop %s resumed after restart (status was '%s')", loop_id, status)
        except Exception:
            _log.exception("Failed to resume loops after restart")
        return restored

    async def shutdown_all(self) -> None:
        """优雅关闭所有 Loop——按优先级倒序（叶子先关，父后关）。"""
        if self._shutdown:
            self._shutdown.set()
        _log.info("Supervisor shutting down %d loops...", len(self._loops))
        ordered = sorted(
            self._loops.values(),
            key=lambda l: -self._specs[l.loop_id].priority if l.loop_id in self._specs else 0
        )
        for loop in ordered:
            if loop.task and not loop.task.done():
                loop.task.cancel()
        tasks = [l.task for l in ordered if l.task and not l.task.done()]
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=LOOP_SHUTDOWN_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                _log.warning("Supervisor shutdown: %d tasks did not finish in 10s",
                            sum(1 for t in tasks if not t.done()))
        _log.info("Supervisor shutdown complete")
