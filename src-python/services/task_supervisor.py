"""services/task_supervisor.py — Phase 81 长任务监控服务（Task Supervisor）

与 LoopSupervisor（Phase 45，system-level 后台 Loop 调度器）不同：
TaskSupervisor 是 **task-level** 监控器，生命周期跟随长任务，提供：
- SupervisorView 实时组装（phase/goal/plan_tree/review_log）
- pause/resume 委托 orchestrator（不打断当前 spawn_subagent）
- inject_constraint 委托 orchestrator（asyncio.Queue 投递）
- ask_supervisor 轻量 LLM 回答（不进主 Agent 循环）
- stall 检测（节点连续失败 ≥ 2 → LLM 分析 → 推只读建议 WS）

设计决策见 .trae/documents/phase-80-completion-and-phase-81-task-supervisor.md §B.2
"""
import asyncio
import json as _json
import logging
import time as _time
from dataclasses import dataclass, field
from typing import Any

from config import TASK_HEARTBEAT_TIMEOUT_SECONDS, DEFAULT_AGENT_ID
from repositories.plan_step_repo import get_plan_tree_by_task
from services.event_bus import Events

_log = logging.getLogger("yuncat.task_supervisor")

# 轮询间隔（秒）——monitor_loop 主循环
_MONITOR_POLL_INTERVAL_SECONDS = 5

# stall 阈值——同一 task 连续失败达到此值触发 LLM 分析
_STALL_FAILURE_THRESHOLD = 2

# P0-4.2: 心跳超时阈值——heartbeat_at 超过此值视为 stall，触发 LLM 分析
# 从 config.py 引入（对标 LangGraph 5min / Temporal activity timeout）
_HEARTBEAT_TIMEOUT_SECONDS = TASK_HEARTBEAT_TIMEOUT_SECONDS

# Phase 87 审查 L-3: _suggestions 保留的 task 数上限——completed task 的建议
# 原代码保留供历史查询，但无上限导致长生命周期 app 内存缓慢增长。FIFO 淘汰。
_MAX_SUGGESTION_TASKS = 100


@dataclass
class SupervisorView:
    """长任务监督视图——按需从 DB 组装，不持久化（单一数据源原则）

    字段来源：
    - status/confirmed_goal/grading_levels/assumptions/elapsed_seconds: task_state.long_task
    - plan_tree/current_step_id: plan_steps 表
    - review_log: plan_steps.review JSON 字段聚合
    - stall_rounds: TaskSupervisor 内存计数器
    - suggestions: LLM 生成的只读建议列表
    """
    task_id: str
    status: str = "unknown"  # task_state.long_task.phase
    confirmed_goal: str = ""
    grading_levels: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    plan_tree: list[dict] = field(default_factory=list)
    current_step_id: str | None = None  # 第一个 running 节点
    elapsed_seconds: int = 0
    estimated_remaining_seconds: int | None = None
    review_log: list[dict] = field(default_factory=list)
    stall_rounds: int = 0
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """转 dict 供 API 返回"""
        return {
            "task_id": self.task_id,
            "status": self.status,
            "confirmed_goal": self.confirmed_goal,
            "grading_levels": self.grading_levels,
            "assumptions": self.assumptions,
            "plan_tree": self.plan_tree,
            "current_step_id": self.current_step_id,
            "elapsed_seconds": self.elapsed_seconds,
            "estimated_remaining_seconds": self.estimated_remaining_seconds,
            "review_log": self.review_log,
            "stall_rounds": self.stall_rounds,
            "suggestions": self.suggestions,
        }


class TaskSupervisor:
    """Phase 81: 长任务监控服务

    生命周期：lifespan 启动时创建 + monitor_loop 后台运行；shutdown 时 cancel。
    不注册到 LoopSupervisor（层级不同——task-level vs system-level）。
    """

    def __init__(self, conn, orchestrator, adapters, model_config, ws_manager, event_bus):
        self._conn = conn
        self._orchestrator = orchestrator
        self._adapters = adapters
        self._model_config = model_config
        self._ws = ws_manager
        self._event_bus = event_bus
        # per-task 内存状态
        self._stall_counters: dict[str, int] = {}
        self._suggestions: dict[str, list[str]] = {}
        # Phase 87 审查 M-4: 正在分析中的 task_id 集合——防止 fire-and-forget
        # 的 LLM 分析重复触发，并让轮询循环不被慢 LLM 调用阻塞
        self._analyzing: set[str] = set()
        self._subscribed = False

    # ── monitor_loop ──

    async def monitor_loop(self) -> None:
        """后台监控主循环——订阅 EventBus + 轮询 running 长任务 + stall 检测

        lifespan 通过 asyncio.create_task() 启动；shutdown 时 cancel。
        """
        # 订阅 EventBus 事件（幂等——多次调用只订阅一次）
        if not self._subscribed and self._event_bus:
            self._event_bus.subscribe(Events.TASK_COMPLETED, self._on_task_completed)
            self._event_bus.subscribe(Events.TASK_FAILED, self._on_task_failed)
            self._event_bus.subscribe(Events.TASK_CANCELLED, self._on_task_cancelled)
            self._subscribed = True
            _log.info("TaskSupervisor subscribed to EventBus (TASK_COMPLETED/FAILED/CANCELLED)")

        _log.info("TaskSupervisor monitor_loop started (interval=%ds)", _MONITOR_POLL_INTERVAL_SECONDS)
        while True:
            try:
                await asyncio.sleep(_MONITOR_POLL_INTERVAL_SECONDS)
                await self._poll_running_long_tasks()
            except asyncio.CancelledError:
                _log.info("TaskSupervisor monitor_loop cancelled")
                break
            except Exception:
                _log.exception("TaskSupervisor monitor_loop iteration failed")
                # 不 break——监控循环崩溃自动恢复（下一轮继续）

    async def _poll_running_long_tasks(self) -> None:
        """轮询所有 running 长任务，刷新 view（stall 检测由 EventBus 事件驱动）

        P0-4.2: 新增心跳超时检测——heartbeat_at（或 updated_at fallback）
        超过 _HEARTBEAT_TIMEOUT_SECONDS 视为 stall，触发 LLM 分析。
        原代码仅依赖失败计数（_STALL_FAILURE_THRESHOLD=2），无心跳检测，
        无法发现"卡在某个工具里不报错也不更新心跳"的隐性 stall。
        """
        task_rows = await self._get_running_long_tasks_with_heartbeat()
        now = _time.time()
        for task_id, heartbeat_at, updated_at in task_rows:
            try:
                # 只读刷新——不阻塞，异常不传播
                view = await self.get_view(task_id)
                # P0-4.2: 心跳超时检测——优先 heartbeat_at，fallback updated_at
                # heartbeat_at 仅在 run_command 等长耗时工具内更新；
                # 若两者都为 NULL（任务刚启动未写心跳），跳过心跳检测
                last_activity = heartbeat_at or updated_at
                heartbeat_stalled = False
                if last_activity:
                    idle_seconds = now - float(last_activity)
                    if idle_seconds > _HEARTBEAT_TIMEOUT_SECONDS:
                        heartbeat_stalled = True
                        _log.warning(
                            "Task %s heartbeat stall: idle=%ds (threshold=%ds)",
                            task_id, int(idle_seconds), _HEARTBEAT_TIMEOUT_SECONDS,
                        )

                # stall 触发条件：(失败计数达阈值) OR (心跳超时)
                should_analyze = (
                    (view.stall_rounds >= _STALL_FAILURE_THRESHOLD or heartbeat_stalled)
                    and not self._suggestions.get(task_id)
                    and task_id not in self._analyzing
                )
                if should_analyze:
                    # Phase 87 审查 M-4: fire-and-forget——_analyze_with_llm 含 LLM
                    # 调用（数秒~数十秒），原代码 await 阻塞整个轮询循环，导致其他
                    # 长任务 view 刷新延迟、事件响应不及时。改为后台任务不阻塞。
                    self._analyzing.add(task_id)
                    asyncio.create_task(
                        self._analyze_and_push(task_id, view.stall_rounds)
                    )
            except Exception:
                _log.debug("poll task %s failed", task_id, exc_info=True)

    async def _get_running_long_tasks_with_heartbeat(self) -> list[tuple]:
        """P0-4.2: 查询 running 长任务 + heartbeat_at + updated_at

        返回 [(task_id, heartbeat_at, updated_at), ...]
        heartbeat_at 可能 NULL（任务未执行过长耗时工具）；updated_at 作为 fallback。
        """
        try:
            cursor = await self._conn.execute(
                "SELECT id, heartbeat_at, updated_at FROM tasks "
                "WHERE status='running' AND task_state LIKE '%\"long_task\"%'",
            )
            rows = await cursor.fetchall()
            return [(row[0], row[1], row[2]) for row in rows if row]
        except Exception:
            _log.debug("get_running_long_tasks_with_heartbeat failed", exc_info=True)
            return []

    async def _analyze_and_push(self, task_id: str, stall_rounds: int) -> None:
        """Phase 87 审查 M-4: 后台执行 LLM 分析 + 推送建议（fire-and-forget）

        原先在 _poll_running_long_tasks 中同步 await _analyze_with_llm，阻塞轮询。
        现由 create_task 调度，_analyzing 集合防止同一 task 重复分析。
        """
        try:
            suggestion = await self._analyze_with_llm(task_id)
            if suggestion:
                self._suggestions.setdefault(task_id, []).append(suggestion)
                # Phase 87 审查 L-3: FIFO 上限保护——completed task 的建议原代码
                # 无限保留，长生命周期 app 内存缓慢增长。淘汰最旧的 task 建议。
                while len(self._suggestions) > _MAX_SUGGESTION_TASKS:
                    oldest = next(iter(self._suggestions), None)
                    if oldest is None:
                        break
                    self._suggestions.pop(oldest, None)
                await self._ws.safe_send(task_id, {
                    "type": "supervisor_suggestion",
                    "task_id": task_id,
                    "suggestion": suggestion,
                    "stall_rounds": stall_rounds,
                })
                _log.info("Supervisor suggestion pushed for task %s (stall=%d)",
                          task_id, stall_rounds)
        except Exception:
            _log.debug("analyze_and_push failed for task %s", task_id, exc_info=True)
        finally:
            self._analyzing.discard(task_id)

    # ── SupervisorView 组装 ──

    async def get_view(self, task_id: str) -> SupervisorView:
        """实时组装 SupervisorView——读 task_state.long_task + plan_steps 树 + review"""
        view = SupervisorView(task_id=task_id)

        # 1. 读 task_state.long_task（复用 orchestrator._store 保持单一数据源）
        state = {}
        try:
            state = await self._orchestrator._store.get_state(task_id)
        except Exception:
            _log.debug("get_view: read task_state failed for %s", task_id, exc_info=True)
        view.status = state.get("phase", "unknown")
        view.confirmed_goal = state.get("confirmed_goal", "")
        view.grading_levels = state.get("grading_levels", []) or []
        view.assumptions = state.get("assumptions", []) or []
        started_at = state.get("started_at")
        if started_at:
            view.elapsed_seconds = int(_time.time()) - int(started_at)

        # 2. 读 plan_steps 树
        try:
            tree = await get_plan_tree_by_task(self._conn, task_id)
            view.plan_tree = tree
            # current_step_id = 第一个 running 节点
            running = next((n for n in tree if n.get("status") == "running"), None)
            if running:
                view.current_step_id = running["id"]
            # estimated_remaining: 已完成 / 总数 * 已耗时
            total = len(tree)
            completed = sum(1 for n in tree if n.get("status") == "completed")
            if total > 0 and completed > 0 and view.elapsed_seconds > 0:
                ratio = completed / total
                if ratio > 0:
                    view.estimated_remaining_seconds = int(view.elapsed_seconds / ratio - view.elapsed_seconds)
            # review_log: 聚合各节点 review 字段
            for n in tree:
                review = n.get("review")
                if review and isinstance(review, dict) and review:
                    view.review_log.append({"step_id": n["id"], "title": n["title"], "review": review})
        except Exception:
            _log.debug("get_view: read plan_steps failed for %s", task_id, exc_info=True)

        # 3. 内存计数器
        view.stall_rounds = self._stall_counters.get(task_id, 0)
        view.suggestions = list(self._suggestions.get(task_id, []))

        return view

    # ── 公开 API（委托 orchestrator 或直接 LLM）──

    async def pause(self, task_id: str) -> None:
        """暂停长任务——委托 orchestrator.pause（不打断当前 spawn_subagent）"""
        await self._orchestrator.pause(task_id)
        _log.info("Task %s paused by supervisor", task_id)

    async def resume(self, task_id: str) -> dict:
        """恢复长任务——委托 orchestrator.resume

        Batch 3b B2: 透传 orchestrator.resume 的真实结果（含 not_long_task /
        phase_x 等失败态），供路由层如实响应而非假成功。
        """
        result = await self._orchestrator.resume(task_id)
        if result.get("ok"):
            _log.info("Task %s resumed by supervisor (%s)", task_id, result.get("status"))
        else:
            _log.warning("Task %s resume failed: %s", task_id, result.get("error", ""))
        return result

    async def inject_constraint(self, task_id: str, constraint: str) -> None:
        """注入约束——委托 orchestrator.inject_constraint（写 plan_steps.review + 入队）"""
        await self._orchestrator.inject_constraint(task_id, constraint)
        _log.info("Constraint injected to task %s: %s", task_id, constraint[:80])

    async def spec(self, task_id: str, new_goal: str | None = None) -> None:
        """重新分解——委托 orchestrator.replan（保留 completed 节点）

        new_goal=None 时用当前 confirmed_goal 重新分解。
        """
        if new_goal is None:
            state = await self._orchestrator._store.get_state(task_id)
            new_goal = state.get("confirmed_goal", "")
            if not new_goal:
                raise ValueError("spec: no confirmed_goal to replan with")
        # replan 内部会清 pending + 重新 decompose + execute
        await self._orchestrator.replan(task_id, new_goal)
        # 重置 stall 计数（重新分解后视为全新执行）
        self._stall_counters.pop(task_id, None)
        self._suggestions.pop(task_id, None)
        _log.info("Task %s re-planned by supervisor (new_goal=%s...)", task_id, new_goal[:40])

    async def ask_supervisor(self, task_id: str, question: str) -> str:
        """用户向 Supervisor 提问——轻量 LLM 回答，不进主 Agent 循环

        组装 SupervisorView 摘要 + 用户问题 → adapter.invoke（单次调用）→ 返回建议。
        """
        view = await self.get_view(task_id)
        prompt = self._build_ask_prompt(view, question)
        return await self._invoke_lightweight_llm(task_id, prompt)

    # ── EventBus 事件处理 ──

    async def _on_task_completed(self, event_type: str, data: dict) -> None:
        """TASK_COMPLETED 事件——清理 task 内存状态"""
        task_id = data.get("task_id", "")
        if not task_id:
            return
        self._stall_counters.pop(task_id, None)
        # 保留 suggestions 历史（completed 后仍可查询）
        _log.debug("Task %s completed, cleared stall counter", task_id)

    async def _on_task_failed(self, event_type: str, data: dict) -> None:
        """TASK_FAILED 事件——增加 stall 计数（达阈值时由 _poll 触发 LLM 分析）"""
        task_id = data.get("task_id", "")
        if not task_id:
            return
        # 仅跟踪长任务（非长任务的普通 task 失败不纳入 stall 监控）
        try:
            is_long = await self._orchestrator.is_long_task(task_id)
        except Exception:
            is_long = False
        if not is_long:
            return
        self._stall_counters[task_id] = self._stall_counters.get(task_id, 0) + 1
        _log.info("Task %s failed event, stall counter=%d", task_id, self._stall_counters[task_id])

    async def _on_task_cancelled(self, event_type: str, data: dict) -> None:
        """Phase 87 审查 M-1: TASK_CANCELLED 事件——清理 task 内存状态

        原代码未订阅此事件，取消的长任务在 _stall_counters / _suggestions /
        _analyzing 中泄漏。取消后任务终止，无需保留任何监控状态。
        """
        task_id = data.get("task_id", "")
        if not task_id:
            return
        self._stall_counters.pop(task_id, None)
        self._suggestions.pop(task_id, None)
        self._analyzing.discard(task_id)
        _log.debug("Task %s cancelled, cleared supervisor state", task_id)

    # ── LLM 调用 ──

    async def _analyze_with_llm(self, task_id: str) -> str:
        """stall 检测触发——轻量 LLM 分析当前状态生成只读建议（不自动执行）"""
        view = await self.get_view(task_id)
        running_node = next(
            (n for n in view.plan_tree if n.get("status") == "running"), None
        )
        current_step = running_node.get("title", "无") if running_node else "无"
        completed = sum(1 for n in view.plan_tree if n.get("status") == "completed")
        total = len(view.plan_tree)
        prompt = (
            "你是长任务监督员。检测到任务可能停滞，请分析当前状态并给出只读建议（不要自动执行）。\n\n"
            f"任务状态：{view.status}\n"
            f"确认目标：{view.confirmed_goal or '（未确认）'}\n"
            f"当前步骤：{current_step}\n"
            f"已耗时：{view.elapsed_seconds}秒\n"
            f"计划进度：{completed}/{total} 节点完成\n"
            f"连续失败次数：{view.stall_rounds}\n\n"
            "请给出 2-3 条具体建议（如：重新分解、换思路、查询行业标准、暂停人工介入）。"
        )
        return await self._invoke_lightweight_llm(task_id, prompt)

    def _build_ask_prompt(self, view: SupervisorView, question: str) -> str:
        """组装 ask_supervisor 的 prompt——完整上下文 + 用户问题（无截断）

        移除原 _VIEW_SUMMARY_MAX_CHARS=800 截断——现代 LLM 上下文 128K+，
        不替 LLM 决定信息量。步骤标题替代 UUID，含完整输出/已注入消息/审查记录。
        """
        # 找到当前 running 节点（含标题和完整输出）
        running_node = next(
            (n for n in view.plan_tree if n.get("status") == "running"), None
        )
        current_step = "无"
        current_output = ""
        if running_node:
            current_step = running_node.get("title", "未命名步骤")
            current_output = running_node.get("output") or ""

        # 已完成/总数
        completed = sum(1 for n in view.plan_tree if n.get("status") == "completed")
        total = len(view.plan_tree)

        # 已注入的消息列表（从 review_log 中提取，完整不截断）
        injected_messages: list[str] = []
        for entry in view.review_log:
            review = entry.get("review", {})
            for c in review.get("constraints", []):
                injected_messages.append(c.get("constraint", ""))

        summary = (
            f"任务状态：{view.status}\n"
            f"确认目标：{view.confirmed_goal or '（未确认）'}\n"
            f"当前步骤：{current_step}\n"
        )
        if current_output:
            summary += f"当前步骤输出：\n{current_output}\n"
        summary += (
            f"已耗时：{view.elapsed_seconds}秒\n"
            f"计划进度：{completed}/{total} 节点完成\n"
            f"连续失败：{view.stall_rounds}次\n"
        )
        if injected_messages:
            summary += f"已注入消息（{len(injected_messages)} 条）：\n"
            for i, c in enumerate(injected_messages, 1):
                summary += f"  {i}. {c}\n"
        if view.review_log:
            summary += "审查记录：\n"
            for r in view.review_log:
                review = r.get("review", {})
                brief = review.get("action") or review.get("verdict") or review.get("reason") or "(已审查)"
                summary += f"  - {r.get('title', '?')}: {brief}\n"

        return (
            "你是长任务监督员（Task Supervisor）。根据以下任务实时状态回答用户问题。\n"
            "你可以看到任务的目标、当前进度、已注入消息和审查记录。\n"
            "请基于实际状态给出准确、有用的回答。如问题超出当前可见信息，请说明。\n\n"
            f"【任务实时状态】\n{summary}\n"
            f"【用户问题】\n{question}\n"
        )

    async def _invoke_lightweight_llm(self, task_id: str, prompt: str) -> str:
        """轻量 LLM 调用——按 task 关联的 agent_id 选 adapter（非 ReAct loop）

        P0-1.6 修复: 原硬编码 `self._adapters.get("hermes")` 违反铁律 #1（不硬编码 agent_id）。
        现从 orchestrator._store 读取 task 的 agent_id，按 agent_id 动态选 adapter。
        缺失/调用失败返回空字符串（best-effort）。
        """
        # P0-1.6: 按 task 的 agent_id 动态选 adapter（对标 line 203/266 已有模式）
        # H19 修复：硬编码 "hermes" → DEFAULT_AGENT_ID（不假设只有一个后端）
        try:
            state = await self._orchestrator._store.get_state(task_id)
            agent_id = state.get("agent_id", DEFAULT_AGENT_ID)
        except Exception:
            agent_id = DEFAULT_AGENT_ID
        adapter = self._adapters.get(agent_id)
        if adapter is None:
            # fallback: 若指定 adapter 不可用，尝试默认 agent（保持向后兼容）
            adapter = self._adapters.get(DEFAULT_AGENT_ID)
        if adapter is None:
            _log.warning("ask_supervisor: adapter '%s' missing for task %s", agent_id, task_id)
            return ""
        try:
            result = await adapter.invoke(prompt)
        except Exception:
            _log.exception("ask_supervisor: adapter.invoke failed for task %s", task_id)
            return ""
        # adapter.invoke 返回 dict，content 字段是模型输出
        if isinstance(result, dict):
            return str(result.get("content", ""))
        return str(result) if result else ""

    # ── shutdown ──

    async def shutdown(self) -> None:
        """lifespan shutdown 时调用——清理资源"""
        _log.info("TaskSupervisor shutting down")
        # EventBus 无 unsubscribe 所有 handler 的便捷方法，留待 EventBus 增强
        # 当前实现：monitor_loop cancel 后不再消费事件
