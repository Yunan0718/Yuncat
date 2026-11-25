"""services/long_task/orchestrator.py — Phase 80 长任务编排器

串联 GoalClarifier(Phase 78) + AnalysisAgent(Task 6.10) + TaskDecomposer(Phase 79) + plan_steps 执行。
生命周期: clarifying → analyzing → decomposing → executing → completed/failed

设计决策（见 .trae/documents/phase-80-long-task-orchestrator.md）:
- 决策 1: 执行用 spawn_subagent(阻塞式) 而非 EventBus 事件驱动
- 决策 2: confirmed_goal 持久化到 tasks.task_state.long_task JSON 子节点
- 决策 3 (Task 6.10): analyzing phase best-effort——analysis_agent 未注入或失败时
  跳过分析直接进 decomposing，保持向后兼容

Phase 81 扩展（见 .trae/documents/phase-80-completion-and-phase-81-task-supervisor.md §B.2）:
- pause/resume 用 asyncio.Event per task（不打断当前 spawn_subagent）
- inject_constraint 用 asyncio.Queue per task + 写 plan_steps.review 持久化
"""
import asyncio
import json as _json
import logging
import time as _time

from config import LOG_PREVIEW_CHARS, LOG_PREVIEW_ERROR_LEN, calc_context_budget, get_default_model_config, DEFAULT_AGENT_ID
# Task 6.15: 截断溯源指针工具
from text_utils import truncate_with_pointer

from repositories.plan_step_repo import (
    get_plan_tree_by_task,
    update_plan_node_status,
)
# Phase 87: 拓扑 + 并行执行器（替代 _execute_steps 的顺序执行）
from services.long_task.executor import execute_plan_tree
from services.model_router import infer_step_tier
# 6b-3 修复: 9 个 review hook 方法已提取到 review_hooks.py（委托模式）
from services.long_task import review_hooks as _review_hooks

_log = logging.getLogger("yuncat.orchestrator")

# Batch 3b B2: restart_execution 后台任务的强引用集合——防 asyncio.Task 被 GC 回收
# （仅持弱引用时任务可能中途消失），完成后经 done_callback 自清理
_bg_resume_tasks: set[asyncio.Task] = set()

# 长任务状态机 phase 常量
PHASE_CLARIFYING = "clarifying"
PHASE_ANALYZING = "analyzing"  # Task 6.10: 5 维度分析（clarifying 和 decomposing 之间）
PHASE_DECOMPOSING = "decomposing"
PHASE_EXECUTING = "executing"
PHASE_COMPLETED = "completed"
PHASE_FAILED = "failed"

# P2-2.2: 前序步骤输出预览长度——动态派生（原硬编码 _PREV_OUTPUT_PREVIEW_CHARS = 500）
# 在 _build_step_context 内用 calc_context_budget(get_default_model_config(), 0.001, 200, 800) 派生

# P0-1.1: clarifying 阶段超时——5min 无用户回复则标记 failed
# 对标主流做法：VS Code "Resolve" 按钮无超时，但 Yuncat 长任务需自动回收资源
CLARIFYING_TIMEOUT_SECONDS = 300

# Task 6.11: auto_followup 不设上限——由 LLM confidence 自行判断是否需要继续追问。
# 安全网：clarifying_watchdog（5min 超时）确保用户不回复时不会永久阻塞。


class _LongTaskStateStore:
    """读写 tasks.task_state.long_task JSON 子节点

    不复用 _write_task_snapshot（整体覆盖会冲掉其他 snapshot key）。
    直接 SELECT → json.loads → 设 data["long_task"] → UPDATE。
    """

    def __init__(self, conn):
        self._conn = conn

    async def get_state(self, task_id: str) -> dict:
        """返回 task_state.long_task 子节点（不存在则空 dict）"""
        cur = await self._conn.execute(
            "SELECT task_state FROM tasks WHERE id=?", (task_id,),
        )
        row = await cur.fetchone()
        if not row or not row[0]:
            return {}
        try:
            data = _json.loads(row[0])
        except (_json.JSONDecodeError, TypeError):
            return {}
        return data.get("long_task", {}) or {}

    async def set_state(self, task_id: str, **fields) -> None:
        """merge 写入 task_state.long_task 子节点（不冲掉其他 key）"""
        cur = await self._conn.execute(
            "SELECT task_state FROM tasks WHERE id=?", (task_id,),
        )
        row = await cur.fetchone()
        data: dict = {}
        if row and row[0]:
            try:
                data = _json.loads(row[0])
            except (_json.JSONDecodeError, TypeError):
                data = {}
        lt = data.get("long_task", {}) or {}
        lt.update(fields)
        data["long_task"] = lt
        await self._conn.execute(
            "UPDATE tasks SET task_state=? WHERE id=?",
            (_json.dumps(data, ensure_ascii=False), task_id),
        )
        await self._conn.commit()

    async def is_long_task(self, task_id: str) -> bool:
        """判断 task 是否为长任务（task_state.long_task 存在）"""
        return bool(await self.get_state(task_id))


class LongTaskOrchestrator:
    """Phase 80: 长任务生命周期主控器

    串联: GoalClarifier(澄清) → AnalysisAgent(分析) → TaskDecomposer(分解) → spawn_subagent(执行)
    状态机: clarifying → analyzing → decomposing → executing → completed/failed

    Task 6.10: analyzing phase 在 clarifying 和 decomposing 之间，生成 5 维度分析报告。
    analysis_agent=None 时跳过 analyzing（向后兼容，旧配置不破坏）。

    调用入口:
    - start(): ws.py Tier1/Tier3 触发 → 推 initial 澄清问题
    - handle_clarification_response(): ws.py goal_clarification_response 委托
    - replan(): API/WS 触发重新分解（保留 completed 节点）
    """

    def __init__(
        self,
        agent_service,
        goal_clarifier,
        task_decomposer,
        conn,
        ws_manager,
        analysis_agent=None,  # Task 6.10: 可选，None 时跳过 analyzing phase
        standards_lookup_agent=None,  # Task 6.13: 可选，None 时跳过 decompose 前置查询
    ):
        self._agent_service = agent_service
        self._goal_clarifier = goal_clarifier
        self._task_decomposer = task_decomposer
        self._conn = conn
        self._ws_manager = ws_manager
        self._analysis_agent = analysis_agent
        self._standards_lookup_agent = standards_lookup_agent
        self._store = _LongTaskStateStore(conn)
        # Phase 81: per-task pause 信号 + inject 队列
        # Event 默认 set（不阻塞）；pause 调 clear()，resume 调 set()
        # Queue 存注入的约束字符串，_execute_steps 每步前非阻塞取
        self._pause_events: dict[str, asyncio.Event] = {}
        # Batch 3b B2: 执行中的长任务 id 集合——_launch_execution 维护。
        # 用于 resume 判定"执行协程是否存活"（进程重启后集合为空 → 协程已死，
        # 需走 restart_execution 重入；集合非空 → 协程活着，set Event 即可）
        self._executing_tasks: set[str] = set()
        # Phase 87 审查 M1: 改为 list[str] 广播语义——并行执行时多个节点同时 drain，
        # 每个节点都应看到全部已注入约束（而非 Queue.get_nowait 随机分配给某一节点）
        self._inject_queues: dict[str, list[str]] = {}
        # Phase 84: 对抗验证 replan 重试计数——{task_id: {stage: count}}
        self._adversarial_retries: dict[str, dict[str, int]] = {}
        self._adversarial_retry_ceiling: dict[str, int] = {}
        # Phase 87 审查 C4: EventBus lazy 注入（EventBus 在 lifespan 中晚于 orchestrator 创建）
        # 注入后 _mark_failed/_mark_completed 发布 TASK_FAILED/TASK_COMPLETED，供 task_supervisor stall 检测
        self._event_bus = None
        # P0-1.1: clarifying 超时 watchdog——{task_id: asyncio.Task}
        # start() 创建，handle_clarification_response() / _cleanup_task_state() 取消
        self._clarifying_watchdogs: dict[str, asyncio.Task] = {}

    async def is_long_task(self, task_id: str) -> bool:
        """判断 task 是否为长任务（供 ws.py 路由判断是否委托编排器）"""
        return await self._store.is_long_task(task_id)

    # ── task_state.long_task 读写代理 (供 window_rollover 鸭子类型契约) ──
    # window_rollover 的监督/仲裁消费函数依赖 orch.get_state / orch.set_state /
    # orch.inject_constraint 这一契约 (见 services/long_task/window_rollover.py 顶部 docstring)。
    # 此前仅 inject_constraint 在 orchestrator 上, get_state/set_state 只存在于 _store,
    # 导致生产环境 orch.get_state 抛 AttributeError (被 executor 的 best-effort try/except 静默吞掉,
    # 监督/升级功能实际从未运行)。此处补齐契约, 把前两者代理到 _store。
    async def get_state(self, task_id: str) -> dict:
        """返回 task_state.long_task 子节点 (代理到 _store)。"""
        return await self._store.get_state(task_id)

    async def set_state(self, task_id: str, **fields) -> None:
        """merge 写入 task_state.long_task 子节点 (代理到 _store)。"""
        return await self._store.set_state(task_id, **fields)

    def set_event_bus(self, event_bus) -> None:
        """Phase 87 审查 C4: lazy 注入 EventBus 实例

        EventBus 在 lifespan 中晚于 orchestrator 创建，注入后
        _mark_failed/_mark_completed 发布 TASK_FAILED/TASK_COMPLETED，
        供 task_supervisor 的 stall 检测正常工作。
        """
        self._event_bus = event_bus

    # ── 公开入口 ──

    async def start(self, task_id: str, prompt: str, agent_id: str = DEFAULT_AGENT_ID, language: str = "zh", max_mode: bool = False, capability_level: int = 1, reasoning_effort: str = "") -> None:
        """启动长任务：持久化 phase=clarifying → 生成初始问题 → 推 WS

        不阻塞——等用户通过 WS goal_clarification_response 回复，
        回复后由 handle_clarification_response() 继续后续流程。

        P0-1.1: 启动后创建 clarifying watchdog——5min 无回复自动标记 failed
        capability_level: 0=轻量 / 1=标准 / 2=强力（取代原 MAX 布尔）。
        reasoning_effort: 思考强度（low/medium/high），透传至子 Agent 调用的模型。
        """
        await self._store.set_state(
            task_id,
            phase=PHASE_CLARIFYING,
            original_prompt=prompt,
            agent_id=agent_id,
            language=language,
            max_mode=max_mode,
            capability_level=capability_level,
            reasoning_effort=reasoning_effort,
            started_at=int(_time.time()),
        )
        # Phase 86: task_lifecycle.phase_start
        await self._push_phase_start(task_id, PHASE_CLARIFYING, "正在澄清任务目标")
        questions = await self._goal_clarifier.generate_questions(
            prompt, agent_id=agent_id,
        )
        await self._ws_manager.safe_send(task_id, {
            "type": "goal_clarification_request",
            "task_id": task_id,
            "stage": "initial",
            "questions": questions,
        })
        # P0-1.1: 启动 clarifying 超时 watchdog
        self._start_clarifying_watchdog(task_id)

    def _start_clarifying_watchdog(self, task_id: str) -> None:
        """P0-1.1: 启动/重启 clarifying 超时 watchdog

        - 取消已有 watchdog（如果存在——followup 轮重新计时）
        - 创建新 task：睡眠 CLARIFYING_TIMEOUT_SECONDS 后检查 phase
        """
        existing = self._clarifying_watchdogs.pop(task_id, None)
        if existing and not existing.done():
            existing.cancel()
        self._clarifying_watchdogs[task_id] = asyncio.create_task(
            self._clarifying_watchdog(task_id)
        )

    async def _clarifying_watchdog(self, task_id: str) -> None:
        """P0-1.1: clarifying 超时检测——5min 后若 phase 仍为 clarifying 则 _mark_failed

        对标 loop_supervisor 的 stall 检测模式：定时检查状态机是否卡住。
        超时原因：用户关闭窗口 / 忘记回复 / 网络断开——避免长任务永久占用资源。
        """
        try:
            await asyncio.sleep(CLARIFYING_TIMEOUT_SECONDS)
            state = await self._store.get_state(task_id)
            if state.get("phase") == PHASE_CLARIFYING:
                _log.warning(
                    "Long task %s clarifying timeout after %ds, marking failed",
                    task_id, CLARIFYING_TIMEOUT_SECONDS,
                )
                await self._mark_failed(
                    task_id,
                    f"目标澄清超时（{CLARIFYING_TIMEOUT_SECONDS}s 无回复），"
                    "请重新发起任务或在对话中明确目标",
                )
        except asyncio.CancelledError:
            # 正常取消（用户回复了 / 任务被 cleanup）——静默退出
            pass
        except Exception:
            _log.exception("clarifying watchdog error for task %s", task_id)
        finally:
            self._clarifying_watchdogs.pop(task_id, None)

    async def handle_clarification_response(
        self, task_id: str, answers: list[dict], need_more: bool = False,
    ) -> None:
        """处理用户澄清回复

        - need_more=True → 生成 followup 问题再推一轮
        - need_more=False → refine_goal → 持久化 → decompose → execute

        Task 6.11: refine_goal 后检查 needs_more_clarification。
        低 confidence 时自主追问（stage="auto_followup"，带 reason）。
        不设轮数上限——由 LLM confidence 自行判断，5min watchdog 兜底。
        """
        # P0-1.1: 用户回复了——取消 clarifying watchdog
        wd = self._clarifying_watchdogs.pop(task_id, None)
        if wd and not wd.done():
            wd.cancel()

        state = await self._store.get_state(task_id)
        agent_id = state.get("agent_id", DEFAULT_AGENT_ID)
        original_prompt = state.get("original_prompt", "")

        if need_more:
            questions = await self._goal_clarifier.generate_questions(
                original_prompt, previous_qa=answers, agent_id=agent_id,
            )
            await self._ws_manager.safe_send(task_id, {
                "type": "goal_clarification_request",
                "task_id": task_id,
                "stage": "followup",
                "questions": questions,
            })
            # P0-1.1: followup 轮重新启动 watchdog（用户可能再次不回复）
            self._start_clarifying_watchdog(task_id)
            return

        # refine → 检查 confidence
        result = await self._goal_clarifier.refine_goal(answers, agent_id=agent_id)

        # Task 6.11: 低 confidence 自主追问（双向讨论）
        # 由 LLM 自主判断 confidence，低于阈值时 orchestrator 自动生成 followup。
        # 不设轮数上限——5min watchdog 兜底防止用户不回复时永久阻塞。
        if result.get("needs_more_clarification"):
            auto_followup_count = int(state.get("auto_followup_count", 0))
            _log.info(
                "Task %s auto_followup #%d (confidence=%.2f, reason=%s)",
                task_id, auto_followup_count + 1,
                result.get("confidence", 0.5),
                result.get("clarification_reason", "")[:LOG_PREVIEW_CHARS],
            )
            # 自主追问——持久化计数 + 生成新问题 + 推 auto_followup WS
            await self._store.set_state(
                task_id, auto_followup_count=auto_followup_count + 1,
            )
            questions = await self._goal_clarifier.generate_questions(
                original_prompt, previous_qa=answers, agent_id=agent_id,
            )
            await self._ws_manager.safe_send(task_id, {
                "type": "goal_clarification_request",
                "task_id": task_id,
                "stage": "auto_followup",
                "reason": result.get("clarification_reason", ""),
                "questions": questions,
            })
            # auto_followup 轮也启动 watchdog（用户可能不回复 auto_followup）
            self._start_clarifying_watchdog(task_id)
            return

        # Task 6.10: 先进入 analyzing phase（而非直接 decomposing）
        await self._store.set_state(
            task_id,
            phase=PHASE_ANALYZING,
            confirmed_goal=result["confirmed_goal"],
            grading_levels=result.get("grading_levels", []),
            assumptions=result.get("assumptions", []),
        )
        # Phase 86: task_lifecycle.phase_start
        await self._push_phase_start(task_id, PHASE_ANALYZING, "正在分析任务")
        await self._ws_manager.safe_send(task_id, {
            "type": "goal_confirmed",
            "task_id": task_id,
            "confirmed_goal": result["confirmed_goal"],
            "grading_levels": result.get("grading_levels", []),
            "assumptions": result.get("assumptions", []),
            # Task 6.11: 透传 confidence 供前端展示
            "confidence": result.get("confidence", 0.5),
        })
        # Phase 84 (a): 目标确认后对抗验证——被反驳则回退 GoalClarifier 重新提问
        challenge = await self._adversarial_challenge(
            task_id, "goal_confirmed",
            result["confirmed_goal"], original_prompt, agent_id,
        )
        if challenge and challenge.get("action") == "replan":
            if self._bump_adversarial_retry(task_id, "goal_confirmed"):
                _log.warning(
                    "Goal refuted by adversarial review, re-clarifying: %s",
                    challenge.get("reason"),
                )
                questions = await self._goal_clarifier.generate_questions(
                    original_prompt, agent_id=agent_id,
                )
                await self._ws_manager.safe_send(task_id, {
                    "type": "goal_clarification_request",
                    "task_id": task_id,
                    "stage": "adversarial_replan",
                    "questions": questions,
                })
                return
            _log.warning(
                "Goal refuted but adversarial replan limit reached, proceeding: %s",
                challenge.get("reason"),
            )
        # Task 6.10: 运行 5 维度分析（best-effort——analysis_agent 未注入或失败时跳过）
        await self._run_analysis(task_id, result["confirmed_goal"], original_prompt, agent_id)
        # analyzing → decomposing
        await self._store.set_state(task_id, phase=PHASE_DECOMPOSING)
        await self._push_phase_start(task_id, PHASE_DECOMPOSING, "正在分解任务计划")
        await self._decompose_and_execute(task_id, agent_id)

    async def replan(self, task_id: str, new_goal: str, agent_id: str = DEFAULT_AGENT_ID) -> None:
        """重新分解：保留 completed 节点 → 删 pending/running → 重新 decompose → execute

        completed 节点保留作历史记录；pending 节点全部删除（新目标下已失效）。
        Phase 87 审查 M4: 同时清理 running 节点（上次崩溃残留），否则依赖 running
        的下游节点永远不 ready，replan 后仍卡死。
        """
        await self._conn.execute(
            "DELETE FROM plan_steps WHERE task_id=? AND status IN ('pending', 'running')",
            (task_id,),
        )
        await self._conn.commit()
        await self._store.set_state(
            task_id,
            phase=PHASE_DECOMPOSING,
            confirmed_goal=new_goal,
        )
        # Phase 86: task_lifecycle.phase_start（重新分解）
        await self._push_phase_start(task_id, PHASE_DECOMPOSING, "正在重新分解任务计划")
        await self._decompose_and_execute(task_id, agent_id)

    # ── Phase 81: pause/resume/inject（供 TaskSupervisor 委托）──

    async def pause(self, task_id: str) -> None:
        """暂停长任务——clear Event 使 _execute_steps 下一步前阻塞

        不打断当前正在执行的 spawn_subagent（无法取消正在跑的子任务）。
        """
        event = self._pause_events.setdefault(task_id, asyncio.Event())
        event.clear()
        _log.info("Orchestrator paused task %s", task_id)

    async def resume(self, task_id: str) -> dict:
        """恢复长任务——按执行协程存活状态分流，如实返回结果（不再假成功）

        Batch 3b B2 修复：
        - 协程存活且已 pause → set Event 释放阻塞（原行为）
        - 协程存活且未 pause → 幂等返回 running（无需恢复）
        - 协程不存在（进程重启 / 崩溃后残留，_executing_tasks 为内存态重启即空）
          → phase=executing 时走 restart_execution 从 pending 步骤重入；
            其他 phase（clarifying/decomposing 等无执行树可续）诚实返回失败

        Returns:
            {"ok": bool, "status": str, "error": str}
            status: resumed / running / restarted / not_long_task / phase_{x} / finished
        """
        if task_id in self._executing_tasks:
            event = self._pause_events.get(task_id)
            if event is not None and not event.is_set():
                event.set()
                _log.info("Orchestrator resumed task %s", task_id)
                return {"ok": True, "status": "resumed"}
            # 未 pause 过的运行中任务——resume 是幂等 no-op，如实上报
            return {"ok": True, "status": "running"}

        # 执行协程不存在——先校验任务是否可重入
        if not await self.is_long_task(task_id):
            return {
                "ok": False, "status": "not_long_task",
                "error": "任务不是长任务或不存在，无法恢复",
            }
        state = await self._store.get_state(task_id)
        phase = state.get("phase", "")
        if phase != PHASE_EXECUTING:
            # clarifying/decomposing 阶段的"断点"是等待用户回复/LLM 中间态，
            # 无执行树可续——诚实失败并提示正确入口（重新触发或 replan）
            return {
                "ok": False, "status": f"phase_{phase or 'unknown'}",
                "error": f"长任务处于 {phase or 'unknown'} 阶段（无执行树可续），"
                         f"请重新发起任务或调用 spec 接口重新分解",
            }
        return await self.restart_execution(task_id)

    async def restart_execution(self, task_id: str) -> dict:
        """Batch 3b B2: 执行协程丢失后从 pending 步骤重入执行

        execute_plan_tree 自带 stale running → pending 恢复（Phase 87 审查 M4），
        completed 节点天然跳过——从首个未完成步骤继续。后台启动（create_task），
        与 _decompose_and_execute 的执行路径共用 _launch_execution。
        """
        state = await self._store.get_state(task_id)
        agent_id = state.get("agent_id") or DEFAULT_AGENT_ID
        # 任务标 running（崩溃后可能残留 paused/interrupted 等状态）
        try:
            await self._conn.execute(
                "UPDATE tasks SET status='running', updated_at=? "
                "WHERE id=? AND status NOT IN ('completed', 'failed', 'cancelled')",
                (int(_time.time()), task_id),
            )
            await self._conn.commit()
        except Exception:
            _log.debug("restart_execution: mark running failed for %s",
                       task_id, exc_info=True)
        # 后台重入执行树——保存强引用防 GC，完成后自清理
        task = asyncio.get_running_loop().create_task(
            self._launch_execution(task_id, agent_id),
        )
        _bg_resume_tasks.add(task)
        task.add_done_callback(_bg_resume_tasks.discard)
        _log.info("Orchestrator restarted execution for task %s (resumed from pending steps)",
                  task_id)
        return {"ok": True, "status": "restarted"}

    async def _launch_execution(self, task_id: str, agent_id: str) -> None:
        """Batch 3b B2: execute_plan_tree 的统一包装——维护 _executing_tasks 存活标记

        _decompose_and_execute（正常路径）与 restart_execution（恢复路径）共用，
        保证 resume 的协程存活判定与实际执行一致。
        """
        self._executing_tasks.add(task_id)
        try:
            await execute_plan_tree(self, task_id, agent_id)
        finally:
            self._executing_tasks.discard(task_id)

    async def inject_constraint(self, task_id: str, constraint: str) -> None:
        """注入约束——写所有 running 节点的 review + 入队供后续节点消费

        Phase 87 审查 M1+M2: 广播语义——并行执行时有多个 running 节点，
        约束应同时应用到全部 running 节点的 review（持久化）+ 全部后续节点的
        prompt（运行时队列），而非随机分配给某一个。

        - 持久化：append 到所有 running 节点的 review.constraints 数组
        - 运行时：append 到 _inject_queues（list），_drain_inject_queue 广播读取
        """
        # 1. 写 plan_steps.review（所有 running 节点——广播语义，M2 修复）
        cur = await self._conn.execute(
            "SELECT id, review FROM plan_steps WHERE task_id=? AND status='running'",
            (task_id,),
        )
        rows = await cur.fetchall()
        ts = int(_time.time())
        for row in rows:
            node_id, review_str = row[0], row[1]
            try:
                review = _json.loads(review_str) if review_str else {}
            except (_json.JSONDecodeError, TypeError):
                review = {}
            review.setdefault("constraints", []).append({
                "constraint": constraint,
                "ts": ts,
            })
            await self._conn.execute(
                "UPDATE plan_steps SET review=?, updated_at=? WHERE id=?",
                (_json.dumps(review, ensure_ascii=False), ts, node_id),
            )
        if rows:
            await self._conn.commit()
        # 2. 入队供后续节点 prompt 消费（list 广播，M1 修复）
        self._inject_queues.setdefault(task_id, []).append(constraint)
        _log.info(
            "Constraint injected to task %s (broadcast to %d running nodes): %s",
            task_id, len(rows), constraint[:LOG_PREVIEW_CHARS],
        )

    # ── 内部流程 ──

    async def _run_analysis(
        self, task_id: str, confirmed_goal: str, original_prompt: str, agent_id: str,
    ) -> None:
        """Task 6.10: 运行 5 维度分析——best-effort，失败不阻塞主流程

        - analysis_agent 未注入（None）→ 跳过，直接进 decomposing（向后兼容）
        - LLM 调用失败 / 返回错误 → 跳过，用 warn 日志记录
        - 成功 → 持久化 analysis_report 到 task_state.long_task + 推 WS analysis_report

        WS 事件：
        - analysis_started：分析开始（前端可展示 loading 态）
        - analysis_report：分析完成（含 5 维度报告 dict）
        """
        if self._analysis_agent is None:
            return  # 未注入 analysis_agent，跳过 analyzing phase
        await self._ws_manager.safe_send(task_id, {
            "type": "analysis_started",
            "task_id": task_id,
        })
        try:
            report = await self._analysis_agent.analyze(
                confirmed_goal, original_prompt, agent_id=agent_id,
            )
        except Exception:
            _log.warning(
                "analysis_agent failed for task %s, skipping analysis", task_id, exc_info=True,
            )
            return
        # 持久化到 task_state.long_task.analysis_report
        await self._store.set_state(task_id, analysis_report=report)
        await self._ws_manager.safe_send(task_id, {
            "type": "analysis_report",
            "task_id": task_id,
            "report": report,
        })

    async def _run_standards_lookup(
        self, task_id: str, confirmed_goal: str, agent_id: str,
    ) -> str | None:
        """Task 6.13: decompose 前置查询最新文档——best-effort，失败不阻塞主流程

        复用 Phase 85 的 StandardsLookupAgent.lookup()（原为失败后查询，此处前置调用）。
        把 confirmed_goal 作为 problem_description 传入，让 4 步降级链（Context7→web_search→
        通用建议）查最新文档，结果拼成 standards_context 字符串注入 decompose prompt。

        - standards_lookup_agent 未注入（None）→ 返回 None，跳过（向后兼容）
        - lookup 抛异常 / 返回 ok=False → 返回 None，warn 日志记录
        - 成功 → 拼接 summary + recommendations 返回，并推 WS standards_lookup_report

        WS 事件：
        - standards_lookup_started：查询开始（前端可展示 loading 态）
        - standards_lookup_report：查询完成（含 sources/summary/recommendations）
        """
        if self._standards_lookup_agent is None:
            return None  # 未注入，跳过（向后兼容）
        await self._ws_manager.safe_send(task_id, {
            "type": "standards_lookup_started",
            "task_id": task_id,
        })
        try:
            result = await self._standards_lookup_agent.lookup(
                confirmed_goal, domain="general", force=False,
            )
        except Exception:
            _log.warning(
                "standards_lookup failed for task %s, skipping (best-effort)",
                task_id, exc_info=True,
            )
            return None
        if not result or not result.get("ok"):
            _log.warning(
                "standards_lookup returned not-ok for task %s: %s",
                task_id, result.get("source_type") if result else "None",
            )
            return None

        # 拼接 standards_context 字符串——summary + recommendations 列表
        summary = result.get("summary", "").strip()
        recs = result.get("recommendations", []) or []
        parts: list[str] = []
        if summary:
            parts.append(summary)
        for rec in recs:
            rec = rec.strip() if isinstance(rec, str) else str(rec).strip()
            if rec:
                parts.append(f"- {rec}")
        standards_context = "\n".join(parts) if parts else None

        # 持久化 + 推 WS（即使 standards_context 为空也推 report，让前端知道查询完成）
        await self._store.set_state(task_id, standards_report=result)
        await self._ws_manager.safe_send(task_id, {
            "type": "standards_lookup_report",
            "task_id": task_id,
            "report": {
                "sources": result.get("sources", []),
                "summary": summary,
                "recommendations": recs,
                "source_type": result.get("source_type", "fallback"),
            },
        })
        return standards_context

    async def _decompose_and_execute(self, task_id: str, agent_id: str) -> None:
        """调 TaskDecomposer.decompose → phase=executing → _execute_steps

        Task 6.13: decompose 前调 _run_standards_lookup 查最新文档，
        结果作为 standards_context 传给 decompose（best-effort，失败传 None）。
        同一轮中若对抗验证触发 re-decompose，复用同一 standards_context 避免重复查询。
        """
        state = await self._store.get_state(task_id)
        confirmed_goal = state.get("confirmed_goal", "")
        if not confirmed_goal:
            _log.error("decompose_and_execute: no confirmed_goal for task %s", task_id)
            await self._mark_failed(task_id, "no confirmed_goal")
            return

        # Task 6.13: decompose 前置查询最新文档（best-effort）
        standards_context = await self._run_standards_lookup(
            task_id, confirmed_goal, agent_id,
        )

        try:
            await self._task_decomposer.decompose(
                task_id, confirmed_goal, agent_id, standards_context=standards_context,
            )
        except Exception as e:
            _log.exception("decompose failed for task %s", task_id)
            await self._mark_failed(task_id, f"decompose failed: {e}")
            return

        # Phase 84 (b): 计划生成后对抗验证——被反驳则删 pending 重新分解
        # P1-7 修复: 对抗验证 + 状态设置段异常时标记 task failed，避免卡在 decomposing phase
        # 原代码行 327-361 未被 try/except 覆盖，DB 锁/get_plan_tree_by_task 失败等异常
        # 传播到调用方（handle_clarification_response/replan），不调 _mark_failed → task 卡死。
        try:
            tree = await get_plan_tree_by_task(self._conn, task_id)
            plan_text = self._plan_tree_to_text(tree)
            challenge = await self._adversarial_challenge(
                task_id, "plan_generated", plan_text, confirmed_goal, agent_id,
            )
            if challenge and challenge.get("action") == "replan":
                if self._bump_adversarial_retry(task_id, "plan_generated"):
                    _log.warning(
                        "Plan refuted by adversarial review, re-decomposing: %s",
                        challenge.get("reason"),
                    )
                    # Phase 87 审查 M4: 同步清理 running 节点（崩溃残留/本轮刚 running 的）
                    await self._conn.execute(
                        "DELETE FROM plan_steps WHERE task_id=? AND status IN ('pending', 'running')",
                        (task_id,),
                    )
                    await self._conn.commit()
                    try:
                        # Task 6.13: 复用同一轮的 standards_context（避免重复查询最新文档）
                        await self._task_decomposer.decompose(
                            task_id, confirmed_goal, agent_id,
                            standards_context=standards_context,
                        )
                    except Exception as e:
                        _log.exception("re-decompose failed for task %s", task_id)
                        await self._mark_failed(
                            task_id, f"re-decompose after adversarial replan failed: {e}",
                        )
                        return
                else:
                    _log.warning(
                        "Plan refuted but adversarial replan limit reached, proceeding: %s",
                        challenge.get("reason"),
                    )

            await self._store.set_state(task_id, phase=PHASE_EXECUTING)
            # Phase 86: task_lifecycle.phase_start
            await self._push_phase_start(task_id, PHASE_EXECUTING, "正在执行任务步骤")
        except Exception as e:
            _log.exception("_decompose_and_execute post-decompose failed for %s", task_id)
            await self._mark_failed(task_id, f"decompose_and_execute: {e}"[:LOG_PREVIEW_ERROR_LEN])
            return
        # Phase 87: 拓扑排序 + 有限并行执行（替代 _execute_steps 顺序执行）
        # execute_plan_tree 内部有 try/except（executor.py:75-100），异常会自动 _mark_failed
        # Batch 3b B2: 经 _launch_execution 包装——维护 _executing_tasks 协程存活标记
        await self._launch_execution(task_id, agent_id)

    async def _execute_steps(self, task_id: str, agent_id: str) -> None:
        """顺序执行 pending plan_steps（DFS 顺序）。失败即停。

        - 有子节点的树：跳过 root（level 0 是目标容器，不执行）
        - 无子节点（fallback 单节点）：执行 root
        - 每步用 spawn_subagent，前序步骤输出作为 context_summary
        """
        tree = await get_plan_tree_by_task(self._conn, task_id)
        has_children = any(n.get("parent_id") is not None for n in tree)
        pending = [n for n in tree if n["status"] == "pending"]
        if has_children:
            pending = [n for n in pending if n["level"] > 0]

        total = len(pending)
        if total == 0:
            await self._mark_completed(task_id)
            return

        # Phase 83: 取 confirmed_goal 供 pre/post_execution 终审上下文
        _state = await self._store.get_state(task_id)
        confirmed_goal = _state.get("confirmed_goal", "")
        language = _state.get("language", "zh")
        max_mode = bool(_state.get("max_mode", False))
        capability_level = int(_state.get("capability_level", 2 if max_mode else 1))
        reasoning_effort = _state.get("reasoning_effort", "")
        # Task 6.15: prev_outputs 携带 step_id 供截断溯源指针使用
        prev_outputs: list[tuple[str, str]] = []
        for idx, step in enumerate(pending):
            # Phase 81: 每步前检查 pause（Event 默认 set 不阻塞；pause 后 clear 阻塞）
            await self._check_pause(task_id)
            step_id = step["id"]
            await update_plan_node_status(self._conn, step_id, "running", expected_from="pending")
            await self._ws_manager.safe_send(task_id, {
                "type": "long_task_step_start",
                "task_id": task_id,
                "step_id": step_id,
                "step_index": idx,
                "total_steps": total,
                "step_title": step["title"],
            })

            prompt = self._build_step_prompt(step)
            # Phase 81: 取出注入的约束（非阻塞），拼到 prompt 末尾
            constraint = await self._drain_inject_queue(task_id)
            if constraint:
                prompt = f"{prompt}\n\n[注入约束]\n{constraint}"
            # Phase 83: pre_execution 终审——halt 则跳过该步（不 fail 整个任务）
            pre_review = await self._final_review_pre(task_id, step, prompt, confirmed_goal)
            if pre_review and pre_review.get("action") == "halt":
                _halt_reason = pre_review.get("reason", "halted by FinalReview")
                await update_plan_node_status(
                    self._conn, step_id, "skipped",
                    output=f"FinalReview halt: {_halt_reason}",
                    expected_from="running",
                )
                await self._ws_manager.safe_send(task_id, {
                    "type": "long_task_step_fail", "task_id": task_id, "step_id": step_id,
                    "step_index": idx, "total_steps": total,
                    "error": f"FinalReview halt: {_halt_reason}",
                })
                await self._write_step_review(step_id, "pre_execution", pre_review)
                continue  # 跳过该步，继续下一步
            context_summary = self._build_context_summary(prev_outputs)
            step_role = infer_step_tier(f"{step.get('title', '')} {step.get('description', '')}")
            try:
                result = await self._agent_service.spawn_subagent(
                    agent_id, prompt, context_summary=context_summary, auto_approve=True,
                    language=language,
                    max_mode=max_mode, long_task_step_role=step_role,
                    capability_level=capability_level,
                    reasoning_effort=reasoning_effort,
                )
            except Exception as e:
                result = {"ok": False, "error": f"spawn_subagent exception: {e}"}

            if result.get("ok"):
                output = result.get("output", "")
                # Phase 84 (c): 输出合并前对抗验证——被反驳则跳过该步不合并输出
                challenge = await self._adversarial_challenge(
                    task_id, "output_merge", output,
                    confirmed_goal, agent_id, step_id=step_id,
                )
                if challenge and challenge.get("action") == "replan":
                    _log.warning(
                        "Output refuted by adversarial review for step %s: %s",
                        step_id, challenge.get("reason"),
                    )
                    await update_plan_node_status(
                        self._conn, step_id, "failed",
                        output=f"Adversarial refuted: {challenge.get('reason', '')}",
                        expected_from="running",
                    )
                    await self._write_adversarial_review(step_id, "output_merge", challenge)
                    await self._ws_manager.safe_send(task_id, {
                        "type": "long_task_step_fail",
                        "task_id": task_id,
                        "step_id": step_id,
                        "step_index": idx,
                        "total_steps": total,
                        "error": f"Adversarial: {challenge.get('reason', '')}",
                    })
                    continue  # 不合并输出，继续下一步
                # Task 6.15: 携带 step_id 供 _build_context_summary 截断溯源
                prev_outputs.append((step_id, output))
                await update_plan_node_status(
                    self._conn, step_id, "completed", output=output,
                    expected_from="running",
                )
                # Phase 83: post_execution 终审（只读，结果写 plan_steps.review）
                await self._final_review_post(task_id, step, output, confirmed_goal)
                await self._ws_manager.safe_send(task_id, {
                    "type": "long_task_step_done",
                    "task_id": task_id,
                    "step_id": step_id,
                    "step_index": idx,
                    "total_steps": total,
                    "output": output,
                })
                # Phase 86: 派生 task_lifecycle.step_done（复用已有数据，output 截断 + 溯源指针）
                # Task 6.15: 截断附溯源指针，完整 output 在 plan_steps 节点 {step_id}
                await self._ws_manager.safe_send(task_id, {
                    "type": "task_lifecycle", "subtype": "step_done",
                    "task_id": task_id,
                    "step_index": idx, "total_steps": total,
                    "output_preview": truncate_with_pointer(output, LOG_PREVIEW_CHARS, step_id),
                })
            else:
                err = result.get("error", "unknown error")
                await update_plan_node_status(
                    self._conn, step_id, "failed", output=err,
                    expected_from="running",
                )
                await self._ws_manager.safe_send(task_id, {
                    "type": "long_task_step_fail",
                    "task_id": task_id,
                    "step_id": step_id,
                    "step_index": idx,
                    "total_steps": total,
                    "error": err,
                })
                # Phase 85: 节点失败时触发标准查询——best-effort，不阻塞 _mark_failed
                await self._standards_lookup_hook(task_id, step, err, agent_id)
                await self._mark_failed(task_id, f"step {step_id} failed: {err}")
                return

        await self._mark_completed(task_id)

    def _build_step_prompt(self, step: dict) -> str:
        """构建 plan_step 执行 prompt：title + description + acceptance_criteria"""
        parts = [step["title"]]
        if step.get("description"):
            parts.append(step["description"])
        criteria = step.get("acceptance_criteria", []) or []
        if criteria:
            parts.append("验收标准：")
            for c in criteria:
                parts.append(f"  - {c}")
        return "\n\n".join(parts)

    def _build_context_summary(self, prev_outputs: list[tuple[str, str]]) -> str:
        """前序步骤输出摘要作为子 Agent 上下文

        Task 6.15: prev_outputs 为 (step_id, output) 列表，截断时附溯源指针
        `... [see node {step_id} for full content]`，让 LLM 知道完整输出在哪个节点可查。
        """
        if not prev_outputs:
            return ""
        lines = ["[前序步骤输出摘要]"]
        for i, (step_id, out) in enumerate(prev_outputs):
            # P2-2.2: 动态派生前序步骤输出预览长度（原硬编码 _PREV_OUTPUT_PREVIEW_CHARS = 500）
            _prev_preview_chars = calc_context_budget(
                get_default_model_config(), 0.001, min_chars=200, max_chars=800,
            )
            # Task 6.15: 截断附溯源指针（step_id 指向 plan_steps 节点）
            preview = truncate_with_pointer(out, _prev_preview_chars, step_id)
            lines.append(f"步骤 {i + 1}: {preview}")
        return "\n".join(lines)

    # ── 6b-3 修复: 9 个 review hook 方法已提取到 review_hooks.py ──
    # 采用委托模式：薄方法包装 → 模块级函数（self 首参），保持调用方签名不变
    # 设计原则：所有 hook 均 best-effort，失败/缺单例返回 None，不阻塞主流程

    async def _final_review_pre(self, task_id, step, prompt, goal):
        return await _review_hooks._final_review_pre(self, task_id, step, prompt, goal)

    async def _final_review_post(self, task_id, step, output, goal):
        await _review_hooks._final_review_post(self, task_id, step, output, goal)

    async def _write_step_review(self, step_id, stage, review):
        await _review_hooks._write_step_review(self, step_id, stage, review)

    async def _adversarial_challenge(self, task_id, stage, text, task_input, agent_id, step_id=None):
        return await _review_hooks._adversarial_challenge(self, task_id, stage, text, task_input, agent_id, step_id)

    def _bump_adversarial_retry(self, task_id, stage):
        return _review_hooks._bump_adversarial_retry(self, task_id, stage)

    async def _write_adversarial_review(self, step_id, stage, review):
        await _review_hooks._write_adversarial_review(self, step_id, stage, review)

    async def _standards_lookup_hook(self, task_id, step, error, agent_id):
        await _review_hooks._standards_lookup_hook(self, task_id, step, error, agent_id)

    @staticmethod
    def _infer_domain(confirmed_goal):
        return _review_hooks._infer_domain(confirmed_goal)

    async def _write_standards_lookup(self, step_id, result):
        await _review_hooks._write_standards_lookup(self, step_id, result)

    @staticmethod
    def _plan_tree_to_text(tree: list[dict]) -> str:
        """plan 树转文本——供对抗验证审查（缩进表示层级）"""
        if not tree:
            return ""
        # 按 level 分组，每级缩进 2 空格
        lines: list[str] = []
        for node in sorted(tree, key=lambda n: (n.get("level", 0), n.get("order", 0))):
            level = node.get("level", 0)
            indent = "  " * level
            title = node.get("title", "")
            desc = node.get("description", "")
            status = node.get("status", "")
            line = f"{indent}- [{status}] {title}"
            if desc:
                line += f"：{desc}"
            lines.append(line)
        return "\n".join(lines)

    # ── Phase 81: pause/inject 内部辅助 ──

    async def _check_pause(self, task_id: str) -> None:
        """检查 pause 信号——Event 不存在（从未 pause）则立即返回；存在则 wait 到 set

        Event 默认 set=True（构造即不阻塞）；pause 调 clear() 使后续 wait 阻塞；
        resume 调 set() 释放阻塞。当前正在执行的 spawn_subagent 不会被取消。
        """
        event = self._pause_events.get(task_id)
        if event is not None:
            await event.wait()

    async def _drain_inject_queue(self, task_id: str) -> str:
        """读取所有已注入的约束，合并返回（无则空字符串）

        Phase 87 审查 M1: 广播语义——不删除已读约束，后续每个节点 drain 时
        都能看到全部历史约束。原 Queue.get_nowait 在并行执行时会导致约束
        随机分配给某一节点（先 drain 的赢），违反"用户约束应作用于全部后续
        工作"的直觉。改为 list + 只读，每个节点看到全部约束。

        _execute_node_core 每步前调用——FIFO 顺序拼接，多条约束用换行分隔。
        """
        queue = self._inject_queues.get(task_id)
        if not queue:
            return ""
        return "\n".join(queue)

    async def _push_phase_start(self, task_id: str, phase: str, description: str) -> None:
        """Phase 86: 推送 task_lifecycle.phase_start 事件

        Phase 106: 同时发布 LONG_TASK_PHASE 事件，由 NotificationCenter 统一发送通知。
        """
        ts = int(_time.time())
        await self._ws_manager.safe_send(task_id, {
            "type": "task_lifecycle", "subtype": "phase_start",
            "task_id": task_id,
            "phase": phase, "description": description,
            "started_at": ts,
        })
        if self._event_bus:
            try:
                from services.event_bus import Events
                await self._event_bus.publish(
                    Events.LONG_TASK_PHASE,
                    {"task_id": task_id, "phase": phase, "description": description, "started_at": ts},
                )
            except Exception:
                _log.debug("publish LONG_TASK_PHASE failed", exc_info=True)

    def _cleanup_task_state(self, task_id: str) -> None:
        """任务结束（completed/failed）时清理 per-task 内存状态，避免泄漏

        Phase 87 审查 C3: 补齐 _adversarial_retries 清理 + 调用 3 个 agent 的
        cleanup_task 清理 per-task 冷却/缓存（原仅清 _pause_events/_inject_queues）。
        """
        self._pause_events.pop(task_id, None)
        self._inject_queues.pop(task_id, None)
        self._adversarial_retries.pop(task_id, None)
        self._adversarial_retry_ceiling.pop(task_id, None)
        # P0-1.1: 取消 clarifying watchdog（若仍在运行）
        wd = self._clarifying_watchdogs.pop(task_id, None)
        if wd and not wd.done():
            wd.cancel()
        # best-effort 清理 3 个 agent 的 per-task 缓存（agent 是全局单例，按 task_id 前缀删）
        for _module, _getter in (
            ("final_review_agent", "get_final_review_agent"),
            ("adversarial_agent", "get_adversarial_agent"),
        ):
            try:
                _mod = __import__(
                    f"services.long_task.{_module}", fromlist=[_getter],
                )
                _agent = getattr(_mod, _getter)()
                if _agent is not None and hasattr(_agent, "cleanup_task"):
                    _agent.cleanup_task(task_id)
            except Exception:
                _log.debug("cleanup %s for task %s failed", _module, task_id, exc_info=True)

    async def _publish_lifecycle_event(self, event_type: str, task_id: str,
                                       extra: dict | None = None) -> None:
        """Phase 87 审查 C4: best-effort 发布 TASK_COMPLETED/TASK_FAILED 事件

        供 task_supervisor 的 stall 检测正常工作（原 orchestrator 不发事件，
        导致 stall 计数永不递增，stall 建议功能失效）。
        """
        if not self._event_bus:
            return
        try:
            payload = {"task_id": task_id, "source": "long_task"}
            if extra:
                payload.update(extra)
            await self._event_bus.publish(event_type, payload)
        except Exception:
            _log.debug("publish %s for task %s failed", event_type, task_id, exc_info=True)

    async def recover_stale_clarifying_tasks(self) -> int:
        """P0-1.2: 启动时恢复卡在 clarifying 的长任务

        服务重启后，phase=clarifying 的任务用户 WS 已断开，无法继续 clarifying 流程。
        标记为 failed，让用户重新发起——对标 loop_supervisor.resume_after_restart
        的恢复模式（崩溃任务不静默丢失）。

        P0-4.5 修复: 原查询 `status='running'` 永远返回空——Phase 47 清理
        (lifespan.py:92) 已将所有 running 改为 failed。改为查询 failed +
        error_message='Backend restarted' 的长任务。

        Returns:
            int: 恢复的任务数（标记为 failed 的数量）
        """
        try:
            cur = await self._conn.execute(
                "SELECT id FROM tasks WHERE status='failed' "
                "AND error_message='Backend restarted' "
                "AND task_state LIKE '%\"long_task\"%'",
            )
            rows = await cur.fetchall()
        except Exception:
            _log.warning("recover_stale_clarifying_tasks: query tasks failed",
                         exc_info=True)
            return 0

        recovered = 0
        for row in rows:
            task_id = row[0]
            try:
                state = await self._store.get_state(task_id)
                if state.get("phase") == PHASE_CLARIFYING:
                    _log.info(
                        "Recovering stale clarifying task %s (marking failed)",
                        task_id,
                    )
                    await self._mark_failed(
                        task_id,
                        "服务重启——目标澄清中断，请重新发起任务",
                    )
                    recovered += 1
            except Exception:
                _log.debug("recover task %s failed", task_id, exc_info=True)
        if recovered > 0:
            _log.info("Recovered %d stale clarifying task(s)", recovered)
        return recovered

    async def recover_stale_executing_tasks(self) -> int:
        """P0-4.5: 启动时恢复崩溃前在 executing 阶段的长任务

        Phase 47 已将所有 running 任务标 failed。此方法将长任务中 phase=executing 的
        从 failed 改为 interrupted（可 resume），保留 plan_steps 断点。

        对标 G4 优雅关闭的 interrupted 模式——崩溃恢复也应支持断点续传。
        clarifying 阶段不恢复（WS 已断，用户需重新发起）；executing 阶段恢复
        因为 plan_steps 已持久化，resume 可从断点继续。

        Returns:
            int: 恢复的任务数（标记为 interrupted 的数量）
        """
        try:
            cur = await self._conn.execute(
                "SELECT id FROM tasks WHERE status='failed' "
                "AND error_message='Backend restarted' "
                "AND task_state LIKE '%\"long_task\"%'",
            )
            rows = await cur.fetchall()
        except Exception:
            _log.warning("recover_stale_executing_tasks: query tasks failed",
                         exc_info=True)
            return 0

        recovered = 0
        for row in rows:
            task_id = row[0]
            try:
                state = await self._store.get_state(task_id)
                if state.get("phase") == PHASE_EXECUTING:
                    # 改为 interrupted（支持 resume_task）——保留 plan_steps 断点
                    await self._conn.execute(
                        "UPDATE tasks SET status='interrupted', "
                        "error_message='Backend restarted (recoverable - executing phase)' "
                        "WHERE id=?",
                        (task_id,),
                    )
                    await self._conn.commit()
                    _log.info(
                        "Recovered stale executing task %s (marked interrupted, resumable)",
                        task_id,
                    )
                    recovered += 1
            except Exception:
                _log.debug("recover executing task %s failed", task_id, exc_info=True)
        if recovered > 0:
            _log.info("Recovered %d stale executing task(s)", recovered)
        return recovered

    async def _mark_completed(self, task_id: str) -> None:
        await self._store.set_state(
            task_id, phase=PHASE_COMPLETED, completed_at=int(_time.time()),
        )
        # Phase 86: task_lifecycle.phase_start
        await self._push_phase_start(task_id, PHASE_COMPLETED, "任务已完成")
        await self._ws_manager.safe_send(task_id, {
            "type": "long_task_completed",
            "task_id": task_id,
        })
        # Phase 87 审查 C4: 发布 TASK_COMPLETED（供 task_supervisor 清理 stall 计数）
        # event_type 字符串对齐 services.event_bus.Events.TASK_COMPLETED
        await self._publish_lifecycle_event("task_completed", task_id)
        self._cleanup_task_state(task_id)

    async def _mark_failed(self, task_id: str, reason: str) -> None:
        await self._store.set_state(
            task_id, phase=PHASE_FAILED,
            failed_at=int(_time.time()), fail_reason=reason,
        )
        # Phase 86: task_lifecycle.phase_start
        await self._push_phase_start(task_id, PHASE_FAILED, "任务执行失败")
        await self._ws_manager.safe_send(task_id, {
            "type": "long_task_failed",
            "task_id": task_id,
            "reason": reason,
        })
        # Phase 87 审查 C4: 发布 TASK_FAILED（供 task_supervisor stall 计数递增）
        # event_type 字符串对齐 services.event_bus.Events.TASK_FAILED
        await self._publish_lifecycle_event("task_failed", task_id, {"reason": reason})
        self._cleanup_task_state(task_id)


# ── 全局单例（对齐 Phase 78/79 模式）──
_orchestrator: LongTaskOrchestrator | None = None


def get_orchestrator() -> LongTaskOrchestrator | None:
    """路由层用——获取全局 LongTaskOrchestrator 单例"""
    return _orchestrator


def set_orchestrator(orch: LongTaskOrchestrator) -> None:
    """lifespan 用——注入全局单例"""
    global _orchestrator
    _orchestrator = orch
