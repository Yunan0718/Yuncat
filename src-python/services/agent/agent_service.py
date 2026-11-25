"""services/agent_service.py —— Agent 服务核心

WS 连接管理 + 流式推送 + 任务生命周期 + 并发控制 + 心跳/健康

H7 拆分: 大型方法体提取到子模块（agent_lifecycle / agent_context / agent_tool_loop /
agent_tool_helpers / agent_finalize / agent_roles / agent_compact / agent_healer），
本文件保留薄包装 + 短方法内联。
"""
import asyncio
import logging
import time as _time
import uuid as _uuid

from config import (COMPACT_TRIGGER_MICRO, COMPACT_TRIGGER_STANDARD,
                    COMPACT_TRIGGER_EMERGENCY, COMPACT_KEEP_RECENT_EMERGENCY,
                    calc_tool_result_limit, calc_max_tool_rounds,
                    protect_critical_info, adaptive_truncate, DEFAULT_AGENT_ID,
                    MAX_TASK_RESUME_COUNT, TASK_CRASH_GRACE_SECONDS,
                    TASK_RUNNING_CLEANUP_LIMIT,                     MAX_CONCURRENT_TASKS,
                    EXTERNAL_AGENT_IDS, AGENT_ENABLED_DEFAULTS)
from services.agent.ws_manager import WsManager; from services.agent.reasoning_trace import ReasoningTrace; from services.agent.task_interactions import discard_task_interactions
import socket

_log = logging.getLogger("yuncat.agent")


# ============================================================
# P2: 工具失败自动换方案映射（模块级——供 agent_tool_loop import）
# ============================================================

def _suggest_alternative_tool(failed_tool: str) -> str | None:
    """工具失败时推荐替代工具。"""
    ALTERNATIVES = {
        # 文件工具
        "read_file": "search_files", "search_files": "list_dir",
        "run_command": "search_files", "list_dir": "search_files",
        "write_file": "read_file", "delete_file": "list_dir",
        "mkdir": "list_dir", "rename_file": "list_dir",
        # Phase 38: OpenClaw 工具
        "openclaw_vision": "read_file",   # 视觉失败→尝试以文本读取
        "web_search": "web_fetch",        # 搜索失败→尝试直接抓取
        "web_fetch": "read_file",
    }
    return ALTERNATIVES.get(failed_tool)


def _map_args_for_alternative(from_tool: str, to_tool: str, args: dict) -> dict:
    """将原工具的参数映射到替代工具。"""
    if from_tool == "read_file" and to_tool == "search_files":
        path = args.get("path", "")
        return {"pattern": path.split("/")[-1].split(".")[0] if "/" in path else path, "path": path}
    if from_tool == "search_files" and to_tool == "list_dir":
        return {"path": args.get("path", "") if args.get("path") else "."}
    if from_tool == "run_command" and to_tool == "search_files":
        cmd = args.get("command", "")
        words = cmd.split()
        return {"pattern": words[-1] if words else cmd, "file_pattern": "*"}
    return args


class AgentService:
    def __init__(self, db, adapters: dict | None = None, memory_service=None, settings_service=None, model_config_service=None, memory_tree=None, feedback_learner=None, tool_service=None):
        self.db = db
        self.adapters = adapters or {}
        self._memory_service = memory_service
        self._settings_service = settings_service  # Phase 6.6: 模型配置
        self._model_config = model_config_service  # v64: models.yaml SSOT
        self._memory_tree = memory_tree  # v66: Memory Tree (借鉴 OpenHuman)
        self._feedback_learner = feedback_learner  # v66: 反馈学习引擎
        self._tool_service = tool_service  # 注入持久 ToolService 实例
        # Phase 41: ContextCompactor — 三级压缩委托
        from services.context_compactor import ContextCompactor
        self.compactor = ContextCompactor(self)
        # Phase 42: PlanExecutor + ToolRecoveryEngine
        from services.plan_executor import PlanExecutor
        self.plan_executor = PlanExecutor(self)
        from services.tools.tool_recovery import ToolRecoveryEngine
        self.tool_recovery = ToolRecoveryEngine(self)
        # Phase 52-N: WS 连接管理拆分到 ws_manager，保留别名保证兼容
        self.ws_manager = WsManager()
        self._ws_connections = self.ws_manager._connections
        self._ws_last_active = self.ws_manager._last_active
        self._task_handles: dict[str, asyncio.Task] = {}  # Phase 6: task_id → asyncio.Task 句柄
        # Phase 56: 子 Agent / 任务完成事件通知 —— 替代轮询 DB
        self._task_completion_events: dict[str, asyncio.Event] = {}
        # 修复 1.5 (FUNCTIONAL P0-1): per-task auto_approve 标志——长任务子 Agent
        # 在预审批范围内自动运行，不弹确认弹窗。对标 Claude Code spawn sub-agent 预审批。
        self._task_auto_approve: dict[str, bool] = {}
        self._start_time = _time.time()
        self._evolution_engine = None
        self._prompt_agent = None  # Phase 76-A: lazy setter 注入（prompt_agent 在 lifespan 中晚于 AgentService 创建）
        self._event_bus = None  # Phase 80: lazy setter 注入（EventBus 在 lifespan 中晚于 AgentService 创建）
        # C4: 消息序号 + 依赖图谱（asyncio.Lock 保护并发安全）
        self._msg_counter_lock = asyncio.Lock()
        self._msg_counter: int = 0
        self._msg_index: dict[str, dict] = {}  # msg_id → {seq, role, session_id, content_preview, refs[]}
        # P3: 初始化 _needs_full_compact 防止跨方法 getattr 竞态
        self._needs_full_compact: set[str] = set()
        self._micro_compact_last_applied: dict[str, float] = {}
        # Phase 38: 失败分析注入下轮
        self._pending_failure_analysis: str | None = None
        # Phase 48 §22.2.7: Bridge 工具审批 pending——TTL 自动清理（created_at 由 register_bridge_pending 写入）
        self._bridge_pending: dict[str, dict] = {}
        # Phase 49-D: 任务运行时内存快照（断线恢复）
        self._task_snapshots: dict[str, dict] = {} ; self._reasoning_trace = ReasoningTrace()
        # P0-4.3: spawn_subagent 并行限流——激活 config.MAX_CONCURRENT_TASKS（原零引用）
        # 对标 Claude Code AgentTool 并发上限 / Cursor background agents 限流
        self._subagent_sem = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

    def set_evolution_engine(self, engine) -> None:
        self._evolution_engine = engine

    def set_prompt_agent(self, prompt_agent) -> None:
        """Phase 76-A: lazy 注入 PromptAgent 实例

        prompt_agent 在 lifespan 中晚于 AgentService 创建（依赖 prompt_library），
        故用 lazy setter 模式（对齐 set_evolution_engine）。
        注入后 agent_lifecycle.run_agent 任务完成钩子可调用其
        save_project_behavior_preference 实现项目级 behavior 偏好持久化。
        """
        self._prompt_agent = prompt_agent

    def set_event_bus(self, event_bus) -> None:
        """Phase 80: lazy 注入 EventBus 实例

        EventBus 在 lifespan 中晚于 AgentService 创建，
        注入后 agent_lifecycle.run_agent 完成/失败时 publish TASK_COMPLETED/FAILED，
        为 Phase 81 Supervisor 监控铺路。
        """
        self._event_bus = event_bus

    # C3: 安全后台 task 创建 — 异常回调避免静默丢失
    @staticmethod
    def _create_bg_task(coro, label: str = "") -> asyncio.Task:
        t = asyncio.create_task(coro)
        t.add_done_callback(lambda _t: (
            _log.exception(f"Background task '{label}' failed", exc_info=_t.exception())
            if _t.exception() else None
        ))
        return t

    from config import AGENT_SERVICE_MSG_INDEX_MAX

    # H1: _msg_index LRU 驱逐 — 防止长时间运行 OOM
    _MSG_INDEX_MAX = AGENT_SERVICE_MSG_INDEX_MAX

    def _evict_msg_index(self) -> None:
        """按 session 粒度驱动——每个 session 最多保留 200 条索引，总计 ≤ 5000"""
        if len(self._msg_index) <= self._MSG_INDEX_MAX:
            return
        # 按 session 归组，老 session 先驱逐
        by_session: dict[str, list] = {}
        for mid, info in self._msg_index.items():
            by_session.setdefault(info.get("session_id", "?"), []).append(mid)
        # 驱逐最老的 session（按消息数）直到不超限
        sorted_sessions = sorted(by_session.items(), key=lambda x: min(
            self._msg_index.get(m, {}).get("seq", 0) for m in x[1] if m in self._msg_index
        ))
        for sid, mids in sorted_sessions:
            if len(self._msg_index) <= self._MSG_INDEX_MAX * 0.8:
                break
            for mid in mids:
                self._msg_index.pop(mid, None)
                if len(self._msg_index) <= self._MSG_INDEX_MAX * 0.8:
                    break

    # ============================================================
    # WS 连接管理（委托给 ws_manager，保持 API 兼容）
    # ============================================================
    def register_ws(self, task_id: str, ws):
        self.ws_manager.register_ws(task_id, ws)

    def unregister_ws(self, ws):
        self.ws_manager.unregister_ws(ws)

    def _all_ws_connections(self) -> list:
        return self.ws_manager.all_connections()

    async def ws_heartbeat_tick(self):
        await self.ws_manager.heartbeat_tick()

    # ============================================================
    # 任务生命周期
    # ============================================================
    async def start_task(
        self, agent_id: str, prompt: str, session_id: str, ws=None,
        rules_context: str = "", mode: str = "agent",
        extra_context: dict | None = None,
        max_budget_tokens: int | None = None,
        language: str = "zh",
        task_id: str | None = None,
        parent_task_id: str | None = None,
    ) -> str:
        """v66: 创建 task → 注册 WS → 启动 Agent。mode: agent/ask/plan

        AUDIT #13: max_budget_tokens 设置该任务的 Token 预算上限。
        agent_tool_loop 每轮累加 used_tokens，超限优雅暂停 (status='paused_budget')。

        P0 修复: task_id 可选显式传入——调用方（如 spawn_subagent）已预建
        任务行时复用同一 id，避免产生第二行永久 running 的孤儿任务
        （重启后被 resume_after_restart 误判为崩溃任务重跑）。

        Phase 108: parent_task_id 支撑真实子任务树——spawn_subagent / Plan 子步骤 /
        retry 版本链 通过此字段建立任务层级（frontend 据此渲染子任务树）。
        """
        if not task_id:
            task_id = str(_uuid.uuid4())
        # parent_task_id 可选：有则建立层级，无则顶层任务。
        if parent_task_id:
            await self.db.execute(
                "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at, "
                "max_budget_tokens, parent_task_id) VALUES(?,?,?,?,?,?,?,?)",
                (task_id, prompt, "running", agent_id, session_id, int(_time.time()),
                 max_budget_tokens, parent_task_id),
            )
        else:
            await self.db.execute(
                "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at, "
                "max_budget_tokens) VALUES(?,?,?,?,?,?,?)",
                (task_id, prompt, "running", agent_id, session_id, int(_time.time()),
                 max_budget_tokens),
            )
        await self.db.commit()
        if ws:
            self.register_ws(task_id, ws)
        handle = self._create_bg_task(
            self._run_agent(task_id, agent_id, prompt, session_id, rules_context, mode,
                            extra_context, language=language),
            label=f"agent_{agent_id}_task_{task_id[:8]}",
        )
        self._task_handles[task_id] = handle
        return task_id

    async def _run_agent(
        self, task_id: str, agent_id: str, prompt: str, session_id: str,
        rules_context: str = "", mode: str = "agent",
        extra_context: dict | None = None,
        language: str = "zh",
    ):
        """后台协程——委托到 agent_lifecycle.run_agent。"""
        from services.agent.agent_lifecycle import run_agent
        return await run_agent(self, task_id, agent_id, prompt, session_id,
                               rules_context, mode, extra_context, language=language)

    async def _build_agent_context(
        self, task_id: str, session_id: str, prompt: str, mode: str,
        rules_context: str, extra_context: dict | None, model_cfg: dict,
        agent_id: str = DEFAULT_AGENT_ID,
        language: str = "zh",
    ) -> tuple[dict, str]:
        """组装 Agent 上下文——委托到 agent_context.build_agent_context。"""
        from services.agent.agent_context import build_agent_context
        return await build_agent_context(
            self, task_id, session_id, prompt, mode, rules_context,
            extra_context, model_cfg, agent_id, language=language,
        )

    async def _execute_tool_loop(
        self, adapter, context: dict, task_id: str, agent_id: str,
        session_id: str, model_cfg: dict, prompt: str,
    ) -> str:
        """Tool call 执行循环——委托到 agent_tool_loop.execute_tool_loop。"""
        from services.agent.agent_tool_loop import execute_tool_loop
        return await execute_tool_loop(
            self, adapter, context, task_id, agent_id, session_id, model_cfg, prompt,
        )

    async def _finalize_response(
        self, task_id: str, agent_id: str, session_id: str, prompt: str,
        full_response: str, mode: str, model_cfg: dict, adapter,
        user_conv_id: str, user_msg_id_str: str, elapsed: float,
    ) -> None:
        """对话完成收尾——委托到 agent_finalize.finalize_response。"""
        from services.agent.agent_finalize import finalize_response
        return await finalize_response(
            self, task_id, agent_id, session_id, prompt, full_response,
            mode, model_cfg, adapter, user_conv_id, user_msg_id_str, elapsed,
        )

    async def _mark_task(self, task_id: str, status: str, error: str | None = None,
                        output: str | None = None, expected_from: str | None = None) -> bool:
        """D3: 原子状态转移——expected_from 提供时走乐观锁，状态不匹配则跳过。

        expected_from=None（默认）：无条件 UPDATE，保持向后兼容。
        expected_from="running"：仅当当前状态为 running 时才转移，避免竞态覆盖。
        返回 True 表示转移成功，False 表示因乐观锁跳过（affected_rows=0）。
        跳过时不触发 completion event，调用方可据此决定是否发 WS 通知。
        """
        if expected_from:
            where_clause = "WHERE id=? AND status=?"
            params_prefix = (task_id, expected_from)
        else:
            where_clause = "WHERE id=?"
            params_prefix = (task_id,)

        if error:
            cursor = await self.db.execute(
                f"UPDATE tasks SET status=?, error_message=?, completed_at=? {where_clause}",
                (status, error, int(_time.time()), *params_prefix),
            )
        elif output:
            cursor = await self.db.execute(
                f"UPDATE tasks SET status=?, output=?, completed_at=? {where_clause}",
                (status, output, int(_time.time()), *params_prefix),
            )
        else:
            cursor = await self.db.execute(
                f"UPDATE tasks SET status=?, completed_at=? {where_clause}",
                (status, int(_time.time()), *params_prefix),
            )
        await self.db.commit()

        # D3: 乐观锁——affected_rows=0 表示状态已被其他进程改变，跳过后续操作
        if expected_from and cursor.rowcount == 0:
            _log.warning(
                "Task %s state transition %s→%s skipped: status already changed",
                task_id[:8], expected_from, status,
            )
            return False

        # Phase 56: 通知等待者（子 Agent / 审批 / 任何轮询等待方）
        event = self._task_completion_events.pop(task_id, None)
        if event:
            event.set()
        return True

    # ---- v64: 模型配置统一走 ModelConfigService (models.yaml SSOT) ----

    async def _get_model_config(self, model_key: str = "primary") -> dict:
        """v64: 从 models.yaml 读取模型配置。Phase 38: model_key 真正生效。
        model_key="flash" → 设置 _prefer_low_cost=True，Agent 可据此选择更便宜的小模型。

        Phase 108: 若开启分级路由且未显式走 _resolve_model_for_request，
        按 model_key 取对应 tier：flash->lite, primary->normal。
        """
        if not self._model_config:
            return {}
        if await self._model_config.is_tiering_enabled():
            tier = "lite" if model_key == "flash" else "normal"
            cfg = await self._model_config.resolve_model_for_task(tier)
            if cfg:
                if model_key == "flash":
                    cfg["_prefer_low_cost"] = True
                return cfg
        cfg = await self._model_config.get_active_config()
        if model_key == "flash":
            cfg["_prefer_low_cost"] = True
        return cfg

    async def _resolve_model_for_request(
        self,
        mode: str,
        prompt: str,
        *,
        max_mode_enabled: bool = False,
        long_task_step_role: str = "",
        model_tier: str = "",
        conversation_context: dict | None = None,
        capability_level: int = 1,
    ) -> dict:
        """Phase 108: 统一模型选择入口——委托到 services/agent/model_selection。

        规则（唯一模型全模式通用 / MAX 选最强 / 未开分级用 active / 开了按 tier）见
        该模块 docstring。拆出去的原因之一是本文件已在尺寸基线之上。
        """
        from services.agent.model_selection import resolve_model_for_request

        return await resolve_model_for_request(
            self._model_config,
            mode, prompt,
            max_mode_enabled=max_mode_enabled,
            long_task_step_role=long_task_step_role,
            model_tier=model_tier,
            conversation_context=conversation_context,
            capability_level=capability_level,
        )

    # ═══════════════════════════════════════════════════════════
    # Phase 46 E.5: 模型降级链 — 委托到 agent_tool_helpers
    # ═══════════════════════════════════════════════════════════

    async def _stream_with_fallback(
        self, agent_id: str, prompt: str, context: dict,
        primary_provider: str, task_id: str = "",
    ):
        """尝试主 Provider → 失败自动降级；同时记录 request/attempt 日志。"""
        from services.llm_request_log import logged_stream_with_fallback
        async for chunk in logged_stream_with_fallback(
            self, agent_id, prompt, context, primary_provider, task_id,
        ):
            yield chunk

    async def cancel_task(self, task_id: str):
        """Phase 6: cancel agent task with proper cleanup"""
        await discard_task_interactions(self, task_id)
        handle = self._task_handles.pop(task_id, None)
        if handle and not handle.done(): handle.cancel()

        task = await self.get_task(task_id)
        from services.agent.pending_tool_calls import persist_cancelled_results; await persist_cancelled_results(self, task_id, task)  # call/result 成对
        ws_conns = self.ws_manager.pop_task_connections(task_id)

        if not task:
            return
        adapter = self.adapters.get(task["agent_id"])
        if adapter and hasattr(adapter, "cancel"):
            await adapter.cancel(task_id)

        # Don't overwrite already-terminal statuses
        if task.get("status") in ("completed", "failed", "cancelled"):
            return

        # D3: 乐观锁——用读到的状态作为 expected_from，避免 read-check-write 间隙竞态
        # 若状态已被其他进程改变（如 agent 恰好完成），_mark_task 返回 False，跳过 WS 通知
        cancelled_ok = await self._mark_task(
            task_id, "cancelled", expected_from=task.get("status"),
        )
        if not cancelled_ok:
            _log.info("Task %s cancel skipped: status changed during cancel",
                      task_id[:8])
            return

        for ws_conn in ws_conns:
            try:
                await ws_conn.send_json({"type": "task_cancelled", "task_id": task_id})
            except Exception:
                _log.warning("WS send failed (disconnected)", exc_info=False)

        # Phase 87: 发布 TASK_CANCELLED，让 Supervisor 清理取消任务的计数器。
        if self._event_bus:
            try:
                from services.event_bus import Events
                await self._event_bus.publish(
                    Events.TASK_CANCELLED,
                    {"task_id": task_id, "agent_id": task.get("agent_id", "")},
                )
            except Exception:
                _log.debug("publish TASK_CANCELLED failed", exc_info=True)

    async def interrupt_task(self, task_id: str):
        """G4: 中断任务——running → interrupted（可恢复，区别于 cancelled 终态）。

        对标 Cursor 断点续传 / Claude Code crash recovery：
        - interrupted = 非用户主动的、可恢复的中断（优雅关闭/强杀）
        - cancelled = 用户明确放弃（终态，不可恢复）
        - paused = 用户主动暂停

        与 cancel_task 的区别：
        - 写 interrupted 而非 cancelled（可被 resume_task 恢复）
        - 不发 TASK_CANCELLED 事件（不是取消，是中断）
        - resume_after_restart 不自动恢复 interrupted 任务（用户需手动 resume）

        典型场景：lifespan 优雅关闭时对所有 running 任务调 interrupt_task。
        """
        await discard_task_interactions(self, task_id)
        handle = self._task_handles.pop(task_id, None)
        if handle and not handle.done(): handle.cancel()

        task = await self.get_task(task_id)
        from services.agent.pending_tool_calls import persist_cancelled_results; await persist_cancelled_results(self, task_id, task)  # call/result 成对
        ws_conns = self.ws_manager.pop_task_connections(task_id)

        if not task:
            return

        adapter = self.adapters.get(task["agent_id"])
        if adapter and hasattr(adapter, "cancel"):
            await adapter.cancel(task_id)

        # 跳过终态 + 已中断（不重复 interrupt）
        if task.get("status") in ("completed", "failed", "cancelled", "interrupted"):
            return

        # D3: 乐观锁——用读到的状态作为 expected_from
        interrupted_ok = await self._mark_task(
            task_id, "interrupted", expected_from=task.get("status"),
        )
        if not interrupted_ok:
            _log.info("Task %s interrupt skipped: status changed during interrupt",
                      task_id[:8])
            return

        for ws_conn in ws_conns:
            try:
                await ws_conn.send_json({"type": "task_interrupted", "task_id": task_id})
            except Exception:
                _log.warning("WS send failed (disconnected)", exc_info=False)

    def _get_long_task_orchestrator(self):
        """Batch 3b B2: lazy 获取长任务编排器（lifespan 中晚于 AgentService 创建）"""
        try:
            from context import get_app
            app = get_app()
            return getattr(app.state, "long_task_orchestrator", None) if app else None
        except Exception:
            return None  # 回退：拿不到 app 时按"没有长任务编排器"处理

    async def resume_task(self, task_id: str) -> bool:
        """Phase 48-H 维度 6: 恢复已暂停的任务——重新启动 agent loop。

        对标 Claude Code Pause/Resume：DB 状态 paused→running，重跑 _run_agent。
        上下文由 conversations 表历史自然恢复（session_id 不变）。
        mode/rules_context/extra_context 不持久化，恢复时用安全默认值。

        AUDIT #13: 也支持恢复 paused_budget 状态的任务（Token 预算耗尽导致的暂停）。
        恢复时 used_tokens 不重置——若预算仍超限，agent_tool_loop 会再次暂停。

        G4: 也支持恢复 interrupted 状态的任务（优雅关闭/强杀后的手动恢复）。

        Batch 3b B2: 长任务（phase=executing）分流到 orchestrator.restart_execution
        从 plan_steps 断点重入——原实现重跑 _run_agent 会完全忽略执行树进度。
        """
        task = await self.get_task(task_id)
        if not task:
            return False
        if task.get("status") not in ("paused", "paused_budget", "interrupted"):
            return False
        agent_id = task.get("agent_id") or DEFAULT_AGENT_ID
        prompt = task.get("input", "")
        session_id = task.get("session_id") or str(_uuid.uuid4())

        # Batch 3b B2: 长任务分流——断点在 plan_steps 树，重跑原始 prompt 无意义
        orch = self._get_long_task_orchestrator()
        if orch is not None and await orch.is_long_task(task_id):
            lt_state = await orch._store.get_state(task_id)
            if lt_state.get("phase") == "executing":
                await self._mark_task(task_id, "running", expected_from=task.get("status"))
                result = await orch.restart_execution(task_id)
                if result.get("ok"):
                    return True
            # 非 executing 阶段（clarifying/decomposing）无执行树可续——
            # 诚实失败（调用方据此提示用户重新发起或 replan），不重跑原始 prompt
            _log.warning(
                "resume_task: long task %s in phase=%s has no executable tree to resume",
                task_id[:8], lt_state.get("phase", "unknown"),
            )
            return False

        # D3: 乐观锁——用读到的状态作为 expected_from，避免 read-check-write 间隙被取消
        resumed = await self._mark_task(task_id, "running", expected_from=task.get("status"))
        if not resumed:
            # 乐观锁失败：状态已被并发修改（如 cancel/interrupt），不再 spawn 后台任务避免双执行
            _log.warning("resume_task: optimistic lock failed for %s, skip resume", task_id[:8])
            return False
        handle = self._create_bg_task(
            self._run_agent(task_id, agent_id, prompt, session_id,
                            "", "agent", None),
            label=f"agent_{agent_id}_resume_{task_id[:8]}",
        )
        self._task_handles[task_id] = handle
        return True

    # ============================================================
    # Phase 62: Task 崩溃恢复——重启后自动恢复 running 任务
    # 对标 Claude Code crash recovery / VS Code workspace restore
    # ============================================================
    async def resume_after_restart(self) -> int:
        """Phase 62: 启动时恢复崩溃的 running 任务（替代直接标 failed）。

        查询 status='running' 且 updated_at < now - TASK_CRASH_GRACE_SECONDS 的任务，
        对每个任务调用 _resume_task 智能恢复。返回成功恢复的任务数。
        """
        now = int(_time.time())
        cutoff = now - TASK_CRASH_GRACE_SECONDS
        cursor = await self.db.execute(
            "SELECT id, input, agent_id, session_id, task_state, "
            "COALESCE(resume_count, 0) FROM tasks "
            "WHERE status='running' AND (updated_at IS NULL OR updated_at < ?) "
            "LIMIT ?",
            (cutoff, TASK_RUNNING_CLEANUP_LIMIT),
        )
        crashed = await cursor.fetchall()
        resumed = 0
        for row in crashed:
            task_id = row[0]
            try:
                if await self._resume_task(
                    task_id, row[1] or "", row[2], row[3], row[4], row[5],
                ):
                    resumed += 1
            except Exception:
                _log.warning("resume task %s failed", task_id[:8], exc_info=True)
        if crashed:
            _log.info("Phase 62: processed %d crashed tasks, resumed %d",
                      len(crashed), resumed)
        return resumed

    async def _resume_task(self, task_id: str, input_text: str,
                            agent_id: str | None, session_id: str | None,
                            task_state_raw: str | None, resume_count: int) -> bool:
        """Phase 62: 恢复单个崩溃任务。返回是否成功恢复。"""
        import json as _json
        # 1. resume_count 超限 → 标 failed
        if resume_count >= MAX_TASK_RESUME_COUNT:
            await self._mark_task(task_id, "failed",
                error=f"任务多次崩溃恢复失败（resume_count={resume_count}）",
                expected_from="running")
            await self._safe_send(task_id, {"type": "task_failed", "task_id": task_id,
                "error": f"任务多次崩溃恢复失败（已达上限 {MAX_TASK_RESUME_COUNT} 次）"})
            _log.warning("Phase 62: task %s failed (resume_count=%d)", task_id[:8], resume_count)
            return False
        # 2. 关键字段缺失 → 标 failed
        aid = agent_id or DEFAULT_AGENT_ID
        sid = session_id
        if not sid:
            await self._mark_task(task_id, "failed",
                error="崩溃恢复失败：缺少 session_id，无法恢复上下文",
                expected_from="running")
            _log.warning("Phase 62: task %s failed (no session_id)", task_id[:8])
            return False
        # 3. 解析 task_state，构建恢复上下文
        state = {}
        if task_state_raw:
            try:
                state = _json.loads(task_state_raw)
            except (_json.JSONDecodeError, TypeError):
                state = {}
        # Batch 3b B2: 长任务崩溃恢复分流——phase=executing 时从 plan_steps 断点
        # 重入执行树（restart_execution）。原实现重跑 _run_agent 原始 prompt，
        # 完全忽略执行树进度，导致已完成步骤全部重做。
        lt_state = state.get("long_task") or {}
        if lt_state.get("phase") == "executing":
            orch = self._get_long_task_orchestrator()
            if orch is not None:
                await self._reinject_pending_tokens(task_id, sid)
                now_ts = int(_time.time())
                await self.db.execute(
                    "UPDATE tasks SET resume_count=resume_count+1, updated_at=?, heartbeat_at=? WHERE id=?",
                    (now_ts, _time.time(), task_id),
                )
                await self.db.commit()
                await self._safe_send(task_id, {"type": "task_resumed", "task_id": task_id,
                    "session_id": sid, "resume_count": resume_count + 1,
                    "message": f"长任务已从崩溃中恢复，从断点步骤继续（第 {resume_count + 1} 次恢复）"})
                result = await orch.restart_execution(task_id)
                _log.info("Phase 62: long task %s resumed via orchestrator (resume_count=%d)",
                          task_id[:8], resume_count + 1)
                return bool(result.get("ok"))
            # orchestrator 不可达（极端场景）——落到下方 _run_agent 路径兜底
        await self._build_and_inject_recovery(task_id, sid, aid, input_text, state)
        # 4. 重新推送 pending token
        await self._reinject_pending_tokens(task_id, sid)
        # 5. resume_count += 1，状态保持 running
        now_ts = int(_time.time())
        await self.db.execute(
            "UPDATE tasks SET resume_count=resume_count+1, updated_at=?, heartbeat_at=? WHERE id=?",
            (now_ts, _time.time(), task_id),
        )
        await self.db.commit()
        # 6. WS 推送 + 重启 _run_agent
        await self._safe_send(task_id, {"type": "task_resumed", "task_id": task_id,
            "session_id": sid, "resume_count": resume_count + 1,
            "message": f"任务已从崩溃中恢复（第 {resume_count + 1} 次恢复）"})
        handle = self._create_bg_task(
            self._run_agent(task_id, aid, input_text, sid, "", "agent", None),
            label=f"agent_{aid}_crashresume_{task_id[:8]}",
        )
        self._task_handles[task_id] = handle
        _log.info("Phase 62: task %s resumed (resume_count=%d)", task_id[:8], resume_count + 1)
        return True

    async def _build_and_inject_recovery(self, task_id: str, session_id: str,
                                          agent_id: str, input_text: str,
                                          state: dict) -> None:
        """Phase 62: 构建恢复上下文并注入 conversations 表（作为 system 消息）。"""
        tool_round = state.get("tool_round", state.get("round", 0))
        plan_step = state.get("plan_step", "?")
        plan_total = state.get("plan_total", "?")
        last_tool = state.get("current_tool", state.get("last_tool", "无"))
        confirmed_goal = state.get("confirmed_goal",
                                    input_text[:200] if input_text else "未知")
        # 中断工具处理：current_tool 存在但 last_tool_result 为空
        interrupted_hint = ""
        current_tool = state.get("current_tool")
        if current_tool and not state.get("last_tool_result"):
            interrupted_hint = (
                f"\n\n[中断工具] 崩溃前正在执行工具 `{current_tool}` 但未收到结果，"
                f"已标记为 interrupted。请重新决定是否需要重试该工具或跳过。"
            )
            state["interrupted_tool"] = current_tool
            await self._write_task_snapshot(task_id, state)
        recovery_msg = (
            f"[任务恢复] 本任务此前已运行到第 {tool_round} 轮、"
            f"计划步骤 {plan_step}/{plan_total}、最近工具 {last_tool}。"
            f"请继续推进，不要重复已完成的工作。核心目标：{confirmed_goal}"
            f"{interrupted_hint}"
        )
        from repositories.conversations_repo import insert_conversation
        await insert_conversation(self.db, session_id, "system", recovery_msg, agent_id=agent_id)
        try:
            await self.db.commit()
        except Exception:
            # D2: commit 失败时必须 rollback，避免 SQLite 连接进入错误状态导致后续操作连锁失败
            try:
                await self.db.rollback()
            except Exception as rb_err:
                _log.warning("rollback after recovery commit failure also failed: %s", rb_err)
            raise

    async def _reinject_pending_tokens(self, task_id: str, session_id: str) -> None:
        """Phase 62: 重新推送崩溃前未确认的 pending token。

        扫描 runtime_states 中 state_type LIKE 'pending_%' 的记录，
        解析 payload 中的 task_id 匹配，通过 WS 重新推送给前端。清理过期记录。
        """
        import json as _json
        now = _time.time()
        await self.db.execute("DELETE FROM runtime_states WHERE expires_at < ?", (now,))
        cursor = await self.db.execute(
            "SELECT id, state_type, payload FROM runtime_states "
            "WHERE state_type LIKE 'pending_%' AND expires_at > ?",
            (now,),
        )
        rows = await cursor.fetchall()
        for row in rows:
            token, state_type, payload_raw = row
            try:
                payload = _json.loads(payload_raw) if payload_raw else {}
            except (_json.JSONDecodeError, TypeError):
                continue
            if payload.get("task_id") != task_id:
                continue
            await self._safe_send(task_id, {
                "type": "pending_approval_reinject", "task_id": task_id,
                "session_id": session_id, "token": token,
                "state_type": state_type, "payload": payload,
            })
            _log.info("Phase 62: reinjected pending token %s for task %s",
                      token[:8], task_id[:8])

    # ============================================================
    # 流式推送（委托给 ws_manager，保持 API 兼容）
    # ============================================================
    async def _safe_send(self, task_id: str, payload: dict) -> None:
        await self.ws_manager.safe_send(task_id, payload)

    async def _streaming_execute_tool(
        self, acc: dict, agent_id: str, task_id: str, model_cfg: dict,
    ) -> str | None:
        """Phase 45 D.5: 流式执行单个 safe 工具——在 stream 进行中立即执行。

        对标 Claude Code: model 还在生成第3个 tool_call，第1个已在执行。
        仅处理 safe 级别工具（read_file/search_files/list_dir 等只读操作）。
        """
        import json as _json
        fn_name = acc["function"]["name"]
        try:
            fn_args = _json.loads(acc["function"]["arguments"]) if acc["function"]["arguments"].strip() else {}
        except (_json.JSONDecodeError, ValueError):
            return None
        ts = self._tool_service
        if ts is None:
            from services.tools.tool_service import ToolService
            ts = ToolService()
        try:
            result = await ts.execute(fn_name, fn_args, agent_id=agent_id, task_id=task_id)
            return _json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            return None  # 回退：流式执行失败不中断，改由后续顺序执行重试

    async def push_stream_chunk(self, task_id: str, data: str, index: int):
        await self.ws_manager.push_stream_chunk(task_id, data, index)

    async def push_stream_done(self, task_id: str):
        await self.ws_manager.push_stream_done(task_id)

    async def push_hitl_request(self, run_id: str, node_id: str, prompt: str, timeout: int,
                                  task_id: str = ""):
        await self.ws_manager.push_hitl_request(run_id, node_id, prompt, timeout, task_id)

    async def _task_prompt_summary(self, task_id: str) -> str:
        """Batch 4b: 取任务 input 前 80 字符——确认弹窗展示来源任务摘要。"""
        try:
            cursor = await self.db.execute(
                "SELECT input FROM tasks WHERE id=?", (task_id,)
            )
            row = await cursor.fetchone()
            return (row[0] or "")[:80] if row else ""
        except Exception:
            return ""  # 回退：读不到 agent 名时用空串占位

    async def push_tool_confirm(self, task_id: str, tool_name: str, preview: dict,
                                confirm_token: str, expires_at: float = 0.0):
        task_prompt = await self._task_prompt_summary(task_id)
        await self.ws_manager.push_tool_confirm(
            task_id, tool_name, preview, confirm_token, expires_at, task_prompt,
        )

    async def push_tool_confirm_batch(self, task_id: str, items: list[dict]):
        task_prompt = await self._task_prompt_summary(task_id)
        await self.ws_manager.push_tool_confirm_batch(task_id, items, task_prompt)

    async def push_error(self, task_id: str, error: str):
        await self.ws_manager.push_error(task_id, error)

    async def push_agent_status(self, agent_id: str, status: str, reason: str = "") -> None:
        await self.ws_manager.push_agent_status(agent_id, status, reason)

    def register_bridge_pending(self, approval_id: str, tool_name: str,
                                arguments: dict, future: "asyncio.Future") -> None:
        """Phase 48 §22.2.7: 注册 Bridge 工具审批 pending——带 created_at 供 TTL 清理。

        唯一写入入口——确保每个条目都带 created_at 时间戳。
        """
        self._bridge_pending[approval_id] = {
            "tool_name": tool_name,
            "arguments": arguments,
            "future": future,
            "created_at": _time.time(),
        }

    async def cleanup_bridge_pending(self, ttl_sec: int = 300) -> int:
        """Phase 48 §22.2.7: 清理超过 ttl_sec 未处理的 Bridge 审批。

        释放 pending future（设为超时拒绝）以避免调用协程永久阻塞。
        返回清理的条目数。
        """
        now = _time.time()
        expired = [
            aid for aid, info in self._bridge_pending.items()
            if now - info.get("created_at", now) > ttl_sec
        ]
        for aid in expired:
            info = self._bridge_pending.pop(aid, None)
            fut = info.get("future") if info else None
            if fut and not fut.done():
                try:
                    fut.set_result({"success": False, "error": "审批超时自动拒绝"})
                except Exception:
                    _log.debug("cleanup_bridge_pending: future resolve failed", exc_info=False)
        if expired:
            _log.info("Cleaned up %d expired bridge approvals", len(expired))
        return len(expired)

    # ═══════════════════════════════════════════════════════════
    # Phase 44 C.4: Stop Hooks — 委托到 agent_tool_helpers
    # ═══════════════════════════════════════════════════════════

    async def _run_stop_hooks(self, task_id: str, session_id: str,
                             agent_id: str = DEFAULT_AGENT_ID,
                             model_cfg: dict | None = None) -> dict:
        """Agent 声明 done 后运行验证钩子——委托到 agent_tool_helpers.run_stop_hooks。

        Phase 48-F: agent_id 穿透——OC 后端任务以 openclaw 身份调工具。
        Phase 56-P2: 传入 model_cfg 使验证钩子预览预算动态化。
        """
        from services.agent.agent_tool_helpers import run_stop_hooks
        return await run_stop_hooks(self, task_id, session_id, agent_id, model_cfg)

    async def _ensure_tool_service(self):
        """懒加载 ToolService——确保 _tool_service 存在。"""
        if self._tool_service is None:
            from services.tools.tool_service import ToolService
            self._tool_service = ToolService()
        return self._tool_service

    # ============================================================
    # 任务查询
    # ============================================================
    async def get_task(self, task_id: str) -> dict | None:
        cursor = await self.db.execute(
            "SELECT id, input, output, status, agent_id, session_id, error_message, "
            "created_at, completed_at, parent_task_id, workflow_id "
            "FROM tasks WHERE id=?", (task_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "input": row[1], "output": row[2], "status": row[3],
            "agent_id": row[4], "session_id": row[5], "error_message": row[6],
            "created_at": row[7], "completed_at": row[8],
            "parent_task_id": row[9], "workflow_id": row[10],
            # Phase 108: 真实子任务树——children 由 parent_task_id 反查得到。
            "children": await self._get_child_tasks(row[0]),
        }

    async def _get_child_tasks(self, parent_task_id: str) -> list[dict]:
        """Phase 108: 反查某任务的子任务（spawn_subagent / plan 子步骤 / 版本链）。"""
        from repositories import tasks_repo
        try:
            return await tasks_repo.get_child_tasks(self.db, parent_task_id)
        except Exception as exc:
            _log.debug("get_child_tasks failed for %s: %s", parent_task_id[:8], exc)
            return []

    async def _write_task_snapshot(self, task_id: str, snapshot: dict) -> None:
        """Phase 49-D: 将任务状态写入 tasks.task_state 并缓存到内存。

        Phase 61: update_task_state 已同时更新 heartbeat_at + updated_at，
        故完整 snapshot 写入即自带心跳——本方法无需额外操作。
        """
        from repositories import update_task_state

        full = {"task_id": task_id, "timestamp": int(_time.time()), **snapshot}
        self._task_snapshots[task_id] = full
        await update_task_state(self.db, task_id, full)
        await self.db.commit()

    async def update_task_heartbeat(self, task_id: str) -> None:
        """Phase 61: 长耗时工具执行期间周期性心跳——只更新 heartbeat_at + updated_at。

        与 _write_task_snapshot 互不干扰：本方法不覆盖 task_state JSON，
        仅供 run_command / spawn_subagent 等长耗时工具在执行循环内调用。
        失败静默（心跳不应中断工具执行）。
        """
        from repositories import update_task_heartbeat
        try:
            await update_task_heartbeat(self.db, task_id)
            await self.db.commit()
        except Exception:
            _log.debug("heartbeat update failed for task %s", task_id[:8], exc_info=True)

    async def get_task_state(self, task_id: str) -> dict | None:
        """Phase 49-D: 读取任务状态；运行中任务合并内存快照。"""
        import json as _json

        cursor = await self.db.execute(
            "SELECT status, task_state FROM tasks WHERE id=?", (task_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        status, raw = row
        try:
            state = _json.loads(raw) if raw else {}
        except (_json.JSONDecodeError, TypeError):
            state = {}
        if status == "running" and task_id in self._task_snapshots:
            state = {**state, **self._task_snapshots[task_id]}
        return {"status": status, "task_state": state}

    async def list_tasks(
        self, status: str = None, limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        offset = max(0, offset)
        if status:
            cursor = await self.db.execute(
                "SELECT id, input, status, agent_id, created_at FROM tasks "
                "WHERE status=? ORDER BY pinned DESC, created_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            )
        else:
            cursor = await self.db.execute(
                "SELECT id, input, status, agent_id, created_at "
                "FROM tasks ORDER BY pinned DESC, created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        rows = await cursor.fetchall()
        return [
            {"id": r[0], "input": r[1], "status": r[2], "agent_id": r[3], "created_at": r[4]}
            for r in rows
        ]

    async def invoke(self, agent_id: str, prompt: str, context: dict | None = None, language: str = "zh") -> str:
        session_id = str(_uuid.uuid4())
        ctx = context or {}
        rules = ctx.get("rules", "")
        mode = ctx.get("mode", "agent")
        # Phase 88: 从 context 提取 extra_context（含 prompt_recommendation 等）透传给 start_task
        extra_context = ctx.get("extra_context")
        return await self.start_task(
            agent_id, prompt, session_id, rules_context=rules, mode=mode,
            extra_context=extra_context, language=language,
        )

    # ---- Phase 9: 多 Agent 讨论引擎 ----

    async def _invoke_with_role(self, agent_id: str, role_prompt: str, task_input: str = "",
                                model_key: str = "primary",
                                provider_config: dict | None = None,
                                language: str = "zh") -> str:
        """为指定角色调用 adapter——委托到 agent_roles.invoke_with_role。"""
        from services.agent.agent_roles import invoke_with_role
        return await invoke_with_role(
            self, agent_id, role_prompt, task_input, model_key, provider_config,
            language=language,
        )

    # ═══════════════════════════════════════════════════════════
    # Phase 43 P1-SPEC B.1: 角色+工具循环 — 委托到 agent_roles
    # ═══════════════════════════════════════════════════════════

    async def _invoke_with_role_and_tools(
        self, agent_id: str, role_prompt: str, task_input: str,
        model_key: str = "primary", max_tool_rounds: int = 30,  # Phase 48-H: 3→30 对标主流无低上限
        task_id: str = "", override_prompt: str = None,
        provider_config: dict | None = None,
        language: str = "zh",
    ) -> str:
        """为指定角色调用 adapter（含工具循环）——委托到 agent_roles.invoke_with_role_and_tools。"""
        from services.agent.agent_roles import invoke_with_role_and_tools
        return await invoke_with_role_and_tools(
            self, agent_id, role_prompt, task_input, model_key,
            max_tool_rounds, task_id, override_prompt, provider_config,
            language=language,
        )

    async def _get_growth_stats(self) -> dict:
        """Phase 43 F4.3: 平台成长统计——委托到 agent_context.get_growth_stats。"""
        from services.agent.agent_context import get_growth_stats
        return await get_growth_stats(self)

    async def spawn_subagent(
        self, agent_id: str, prompt: str,
        context_summary: str = "", auto_approve: bool = False,
        language: str = "zh",
        max_mode: bool = False,
        long_task_step_role: str = "",
        model_tier: str = "",
        capability_level: int = 1,
        reasoning_effort: str = "",
    ) -> dict:
        """Phase 43 W.4: 独立上下文子 Agent——委托到 agent_lifecycle.spawn_subagent。

        修复 1.5: auto_approve=True 时子 Agent 在预审批范围内自动运行（长任务用）。
        Phase 108: 透传 max_mode + model_tier + 长任务子步骤 role，供分级路由选择模型 tier。
        capability_level: 0/1/2 能力等级（取代原 MAX 布尔）。
        """
        from services.agent.agent_lifecycle import spawn_subagent
        return await spawn_subagent(
            self, agent_id, prompt, context_summary,
            auto_approve=auto_approve, language=language,
            max_mode=max_mode, long_task_step_role=long_task_step_role,
            model_tier=model_tier, capability_level=capability_level,
            reasoning_effort=reasoning_effort,
        )

    async def spawn_subagent_team(
        self, specs: list[dict], aggregate: bool = False,
        language: str = "zh",
    ) -> dict:
        """Phase 9-X: 并行 fan-out 多个内部子 Agent——委托到 agent_lifecycle.spawn_subagent_team。

        单个用户请求拆分为多个独立子任务并行执行并聚合（非外部 Agent）。
        """
        from services.agent.agent_lifecycle import spawn_subagent_team
        return await spawn_subagent_team(self, specs, aggregate, language)

    async def list_agents(self) -> list[dict]:
        """Phase D: 返回更丰富的 Agent 元数据，供 Agent 管理面板 / 下拉框使用。

        Phase X: 新增两个字段：
        - detected: 真实探测该 Agent 当前是否可用（外部 Agent 做 TCP 端口探测；
          CLI Agent 用 shutil.which 探测二进制；原生 yuncat 恒为 True）。
        - enabled: 该 Agent 是否「已启用」（用户需在面板显式开启，外部 Agent 默认关闭，
          原生 yuncat 默认启用且不可禁用）。只有 enabled 的 Agent 才能在任务中被调用。
        """
        import shutil

        def _tcp_probe(host: str, port: int, timeout: float = 0.6) -> bool:
            """TCP 端口探测——外部 Agent 网关是否可达（不依赖特定 HTTP 响应）。"""
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    return True
            except OSError:
                return False

        async def _read_enabled(agent_id: str, is_external: bool) -> bool:
            """从 settings 读取启用状态；缺失则回退到默认值。"""
            default = AGENT_ENABLED_DEFAULTS.get(agent_id, not is_external)
            try:
                cursor = await self.db.execute(
                    "SELECT value FROM settings WHERE key=?", (f"agent_enabled:{agent_id}",)
                )
                row = await cursor.fetchone()
                if row is None or row[0] is None:
                    return default
                return str(row[0]).lower() == "true"
            except Exception:
                return default

        result = []
        probe_tasks: list[tuple[str, "asyncio.Future[bool]"]] = []
        for adapter in self.adapters.values():
            caps = adapter.capabilities()
            port = getattr(adapter, "_port", 0) or getattr(adapter, "port_hint", 0)
            cli_name = getattr(adapter, "cli_name", "") or ""
            mode = "subprocess"
            if hasattr(adapter, "_ha_available"):
                mode = "subprocess" if getattr(adapter, "_ha_available", False) else "direct"
            version = ""
            if hasattr(adapter, "MODEL"):
                version = str(getattr(adapter, "MODEL", ""))
            aid = adapter.agent_id
            is_external = aid in EXTERNAL_AGENT_IDS
            # 外部 Agent 探测（异步并行，避免串行阻塞事件循环）：
            #  - 有端口的网关（openclaw）做 TCP 端口探测；
            #  - CLI 型外部 Agent（codex：cli_name="codex"）用 shutil.which 探测二进制是否安装；
            #  - 原生无端口、无 cli_name → 占位，稍后统一覆盖为 True。
            if port:
                probe = asyncio.to_thread(_tcp_probe, "127.0.0.1", port)
            elif is_external and cli_name:
                probe = asyncio.to_thread(shutil.which, cli_name)
            else:
                probe = asyncio.sleep(0, result=False)
            result.append({
                "id": aid,
                "name": getattr(adapter, "display_name", "") or aid.capitalize(),
                "kind": "external" if is_external else "native",
                "external": is_external,
                "auto_route": not is_external,
                "status": "running" if adapter._ready.is_set() else "stopped",
                "port": port,
                "mode": mode,
                "version": version,
                "installed": True,
                "detected": True,  # 占位，稍后由探测结果覆盖（原生恒 True）
                "capabilities": {
                    "supports_stream": caps.supports_stream,
                    "supports_tools": caps.supports_tools,
                    "supports_browser": caps.supports_browser,
                    "supports_interrupt": caps.supports_interrupt,
                    "supports_image": caps.supports_image,
                    "supports_memory": caps.supports_memory,
                },
            })
            probe_tasks.append((aid, probe))

        # CLI Agent（无 adapter）——探测二进制是否存在，供「检测有哪些」使用
        cli_agents = {
            "codex": "codex",
            "claude": "claude",
        }
        known_ids = {a["id"] for a in result}
        for cid, bin_name in cli_agents.items():
            if cid in known_ids:
                continue
            installed = shutil.which(bin_name) is not None
            result.append({
                "id": cid,
                "name": cid.capitalize(),
                "kind": "cli",
                "external": True,
                "auto_route": False,
                "status": "stopped",
                "port": 0,
                "mode": "cli",
                "version": "",
                "installed": installed,
                "detected": installed,
                "capabilities": {},
            })

        # 真实探测外部 Agent 端口（并行）+ 读取各 Agent 启用状态
        detected_map: dict[str, bool] = {}
        if probe_tasks:
            probes = await asyncio.gather(*[p for _, p in probe_tasks], return_exceptions=True)
            for (aid, _), ok in zip(probe_tasks, probes):
                # 原生 Agent 端口为 0，探测恒 False——下面统一覆盖为 True
                detected_map[aid] = bool(ok) if not isinstance(ok, Exception) else False
        enabled_map = await asyncio.gather(*[
            _read_enabled(a["id"], a["external"]) for a in result
        ])

        # 回填 detected / installed / enabled
        for idx, a in enumerate(result):
            if a["kind"] == "native":
                a["detected"] = True
                a["installed"] = True
            elif a["kind"] == "external":
                det = detected_map.get(a["id"], False)
                a["detected"] = det
                a["installed"] = det  # 外部 Agent 仅当网关可达时才视为可用
                # 网关型外部 Agent（有端口）：status 以探测结果为准——
                # ext_http.start() 会无条件置 _ready（适配器自身就绪≠网关在线），
                # 不覆盖的话「CLI 装了但网关没启动」会虚标 running（实测）。
                if a["port"] and (a["status"] == "running") != det:
                    a["status"] = "running" if det else "stopped"
            # cli 的 detected/installed 已在上面设置
            a["enabled"] = enabled_map[idx]

        # 按 stable 顺序排序：原生优先，其次外部，最后 cli
        order = {"native": 0, "external": 1, "cli": 2}
        result.sort(key=lambda a: (order.get(a["kind"], 3), a["id"]))
        return result

    async def is_running(self, agent_id: str) -> bool:
        adapter = self.adapters.get(agent_id)
        if not adapter:
            return False
        return adapter._ready.is_set()

    async def start_agent(self, agent_id: str):
        adapter = self.adapters.get(agent_id)
        if adapter and not adapter._ready.is_set():
            await adapter.start()
            await adapter.wait_ready()
            await self.push_agent_status(agent_id, "ready")

    async def stop_agent(self, agent_id: str):
        adapter = self.adapters.get(agent_id)
        if adapter and hasattr(adapter, "stop"):
            await adapter.stop()
        await self.push_agent_status(agent_id, "stopped")

    async def proxy_llm_request(self, agent_id: str, messages: list[dict], model: str = "") -> dict:
        adapter = self.adapters.get(agent_id) or self.adapters.get(DEFAULT_AGENT_ID)
        if not adapter:
            return {"error": "No adapter available"}
        cfg = await self._get_model_config("primary")
        if not cfg.get("api_key"):
            return {"error": "API key not configured"}
        # 将 messages 列表转换为 adapter.invoke 可接受的 history + prompt
        history = list(messages)
        prompt = ""
        if history and history[-1].get("role") == "user":
            prompt = history[-1].get("content", "")
            history = history[:-1]
        model_cfg = dict(cfg)
        if model:
            model_cfg["model"] = model
        return await adapter.invoke(
            prompt,
            context={"history": history, "model_config": model_cfg},
        )

    # ============================================================
    # P0: 微型压缩 — 委托到 agent_compact（保留 @staticmethod 包装以维持向后兼容）
    # ============================================================

    @staticmethod
    def _micro_dump_tool_output(content: str) -> str:
        """Claude Code 风格：截断的完整内容 dump 到 workspace 文件——委托到 agent_compact。"""
        from services.agent.agent_compact import micro_dump_tool_output
        return micro_dump_tool_output(content)

    @staticmethod
    def _micro_compact_history(history: list[dict], model_cfg: dict | None = None) -> int:
        """微型压缩——委托到 agent_compact.micro_compact_history。"""
        from services.agent.agent_compact import micro_compact_history
        return micro_compact_history(history, model_cfg=model_cfg)

    # ═══════════════════════════════════════════════════════════
    # Phase 44 C.5: Snip 层 — 委托到 agent_compact
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _snip_low_value_turns(history: list[dict]) -> list[dict]:
        """删除低信息量轮次——委托到 agent_compact.snip_low_value_turns。"""
        from services.agent.agent_compact import snip_low_value_turns
        return snip_low_value_turns(history)

    # ═══════════════════════════════════════════════════════════
    # Phase 44 C.5: Collapse 层 — 委托到 agent_compact
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _collapse_inactive_context(history: list[dict],
                                    active_window: int = 20) -> list[dict]:
        """非活跃区折叠——委托到 agent_compact.collapse_inactive_context。"""
        from services.agent.agent_compact import collapse_inactive_context
        return collapse_inactive_context(history, active_window)

    # ============================================================
    # P1: 压缩后重新附着核心上下文——委托到 agent_context
    # ============================================================

    async def _reattach_context_after_compact(
        self, session_id: str, context: dict, model_cfg: dict,
    ) -> None:
        """Claude Code 风格：压缩后将核心上下文重新注入——委托到 agent_context。"""
        from services.agent.agent_context import reattach_context_after_compact
        return await reattach_context_after_compact(self, session_id, context, model_cfg)

    @staticmethod
    def _extract_recent_files(history: list[dict]) -> list[str]:
        """从对话历史中提取最近操作的文件路径——委托到 agent_compact。"""
        from services.agent.agent_compact import extract_recent_files
        return extract_recent_files(history)

    # ============================================================
    # P2: 工具失败自愈 — 委托到 agent_tool_helpers
    # ============================================================

    async def _analyze_tool_failure_and_send(self, task_id: str, tool_name: str,
                                               args: dict, result_str: str,
                                               agent_id: str, model_cfg: dict) -> None:
        """工具失败后 LLM 分析原因 + 建议替代方案——委托到 agent_tool_helpers。"""
        from services.agent.agent_tool_helpers import analyze_tool_failure_and_send
        return await analyze_tool_failure_and_send(
            self, task_id, tool_name, args, result_str, agent_id, model_cfg,
        )

    # ============================================================
    # P6: 工具调用结果提取关键信息 → 长期记忆——委托到 agent_tool_helpers
    # ============================================================

    async def _extract_memory_from_tool_result(self, tool_name: str, args: dict,
                                                  result_str: str) -> str | None:
        """从成功的工具调用结果中提取可记忆的关键信息——委托到 agent_tool_helpers。"""
        from services.agent.agent_tool_helpers import extract_memory_from_tool_result
        return await extract_memory_from_tool_result(self, tool_name, args, result_str)

    # ============================================================
    # 上下文压缩辅助方法——委托到 agent_compact
    # ============================================================

    @staticmethod
    def _emergency_trim_history(
        history: list[dict],
        keep_recent: int = COMPACT_KEEP_RECENT_EMERGENCY,
        model_config: dict | None = None,
    ) -> list[dict]:
        """99.99% 紧急修剪——委托到 agent_compact.emergency_trim_history。"""
        from services.agent.agent_compact import emergency_trim_history
        return emergency_trim_history(history, keep_recent, model_config=model_config)

    # ============================================================
    # 消息索引查询（右键引用用）
    # ============================================================

    async def get_msg_by_seq(self, session_id: str, seq: int) -> dict | None:
        """按序号查找消息。"""
        for msg_id, info in self._msg_index.items():
            if info.get("session_id") == session_id and info.get("seq") == seq:
                return {"msg_id": msg_id, **info}
        return None

    async def _should_auto_approve(self, task_id: str = "") -> bool:
        """对标 Claude Code: 决定是否自动批准危险操作。

        修复 1.5 (FUNCTIONAL P0-1): 优先读 task 级 auto_approve（长任务子 Agent
        在预审批范围内自动运行），再读 DB 全局设置。
        """
        # 1. task 级标志——spawn_subagent(auto_approve=True) 时设置
        if task_id and self._task_auto_approve.get(task_id):
            return True
        # 2. DB 全局设置
        try:
            cursor = await self.db.execute("SELECT value FROM settings WHERE key='auto_approve'")
            row = await cursor.fetchone()
            return row is not None and row[0] == "true"
        except Exception:
            _log.warning("读取 auto_approve 设置失败，默认不自动批准", exc_info=False)
            return False

    async def get_session_messages_indexed(self, session_id: str) -> list[dict]:
        """获取带序号的会话消息列表。"""
        return [
            {"msg_id": mid, **info}
            for mid, info in self._msg_index.items()
            if info.get("session_id") == session_id
        ]

    # ============================================================
    # 健康 / 监控——委托到 agent_healer
    # ============================================================
    async def _agent_healer(self):
        """Phase 3: Agent 僵死自愈——委托到 agent_healer.agent_healer。"""
        from services.agent.agent_healer import agent_healer
        return await agent_healer(self)

    async def get_monitoring_data(self) -> dict:
        """Phase 7b: 监控 API——委托到 agent_healer.get_monitoring_data。"""
        from services.agent.agent_healer import get_monitoring_data
        return await get_monitoring_data(self)

    # ---- Phase 14: Plan 步骤执行引擎——委托到 agent_lifecycle ----

    async def execute_plan_steps(self, parent_task_id: str, agent_id: str) -> None:
        """按顺序执行 Plan 的所有步骤——委托到 agent_lifecycle.execute_plan_steps。"""
        from services.agent.agent_lifecycle import execute_plan_steps
        return await execute_plan_steps(self, parent_task_id, agent_id)
