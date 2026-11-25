"""lifespan_postinit.py —— 从 lifespan_services.py 提取的后置初始化逻辑

将原 lifespan_services.py 中 _init_mcp_services 及其之后的函数（共 17 个）提取到此模块，
以满足项目规则"文件 <800 行"。lifespan.py._init_services 按原顺序分别从
lifespan_services（前 13 个）与本模块（后 17 个）导入。

拆分原则：
  - 不改变任何逻辑——仅提取函数 + 修 import
  - 不循环依赖——本模块不 import lifespan_services.py / lifespan.py
  - 每函数按初始化阶段聚焦单一职责

函数调用顺序由 lifespan._init_services 编排，保持与原 _init_services 完全一致。
"""
import asyncio
import logging
import os
import time as _time
import uuid as _uuid

from config import (
    DEFAULT_AGENT_ID, OPENCLAW_AGENT_ID,
    CI_ERROR_LOG_PREVIEW_MAX_CHARS,
    LOOP_INTERVAL_BACKUP_SCHEDULER_SEC,
)

# 复用已提取的辅助模块（不经过 lifespan.py，避免循环依赖）
from lifespan_helpers import (
    _insert_builtin_templates,
    _watch_prompts_with_watchdog,
)
from lifespan_event_handlers import register_event_handlers

_log = logging.getLogger("yuncat.lifespan")


# ════════════════════════════════════════════════════════════
# MCP 服务 + Loop 注册
# ════════════════════════════════════════════════════════════

async def _init_mcp_services(app) -> None:
    """MCP 服务器自动启动 + 工具注册 + 心跳监控 Loop。"""
    from services.loop_supervisor import LoopSpec, RestartStrategy

    # Phase U.2: MCP 服务器自动启动 + 工具注册
    async def _auto_start_mcp_servers():
        try:
            from services.mcp_bridge import register_mcp_tools
            await register_mcp_tools(app.state.db)
            _log.info("MCP tools registered from enabled servers")
        except Exception:
            _log.debug("MCP auto-start skipped", exc_info=True)
    asyncio.create_task(_auto_start_mcp_servers())

    # Phase U.2-A: MCP 服务器心跳监控——每 30s 探测一次，失败时自动清理以便重建
    async def _mcp_health_monitor(state: dict):
        try:
            from services.mcp_bridge import health_check_all
            await health_check_all()
        except Exception:
            _log.debug("MCP health monitor skipped", exc_info=True)

    await app.state.supervisor.register(LoopSpec(
        "mcp_health", lambda s: _mcp_health_monitor(s),
        interval=30, priority=-1, strategy=RestartStrategy.PERMANENT))


# ════════════════════════════════════════════════════════════
# Spec 触发器 + Cron 调度器 + 备份调度器
# ════════════════════════════════════════════════════════════

def _register_spec_trigger(app, conn) -> None:
    """注册 Spec 事件触发器——处理 ci_failure / spec / 渠道消息。"""
    from services.loop_supervisor import LoopSpec, RestartStrategy

    # Phase 45 D.1: Spec 事件循环 — supervisor._tick_event_loop 轮询并调用此函数
    async def _spec_trigger_handler(payload: dict):
        """处理单个触发事件——由 supervisor._tick_event_loop 调用。
        payload 已从 loop_trigger_queue 提取，supervisor 负责认领/完成标记。
        """
        trigger_type = payload.get("trigger_type", payload.get("type", "unknown"))
        if trigger_type == "ci_failure":
            task_input = (
                f"CI 构建在 commit {payload.get('commit_sha','?')[:8]} 失败。\n\n"
                f"## Repository\n{payload.get('repo','unknown')}\n\n"
                f"## 错误日志\n{payload.get('error_log','')[:CI_ERROR_LOG_PREVIEW_MAX_CHARS]}\n\n"
                f"## 任务\n1. 分析错误根因\n2. 生成 Spec 描述修复方案\n3. 实现修复\n4. 自验证"
            )
            task_id = payload.get("task_id", str(_uuid.uuid4()))
            await app.state.deliberation_service.run_spec_loop(
                task_input=task_input, task_id=task_id,
                agent_id=DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透（supervisor 触发无动态 agent_id，用默认值）
            )
        elif trigger_type == "spec":
            # AUDIT #9: cron 触发已迁移到 scheduled_tasks 表 + _scheduler_tick，
            # 不再走 loop_trigger_queue。此分支仅处理 spec 触发。
            task_id = payload.get("task_id", str(_uuid.uuid4()))
            await app.state.deliberation_service.run_spec_loop(
                task_input=payload.get("task_input", payload.get("prompt", "")),
                task_id=task_id,
                agent_id=DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
            )
        # ── Phase 43 F2.2: 渠道消息消费 (30s 冷却防刷屏) ──
        elif trigger_type.endswith("_message"):
            channel = payload.get("channel", "unknown")
            now = _time.time()
            _cooldown = getattr(_spec_trigger_handler, "_channel_cooldown", {})
            if now - _cooldown.get(channel, 0) < 30:
                return  # 30 秒冷却——同一渠道不连续处理
            _cooldown[channel] = now
            _spec_trigger_handler._channel_cooldown = _cooldown

            task_id = payload.get("task_id", str(_uuid.uuid4()))
            task_input = payload.get("task_input", payload.get("text", ""))
            _log.info("Channel event: %s · %d chars · task=%s",
                     channel, len(task_input), task_id[:8])

            # 渠道消息 → Spec Loop 处理
            await app.state.deliberation_service.run_spec_loop(
                task_input=task_input, task_id=task_id,
                agent_id=DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透
            )
            # 发送回复到渠道
            oc = app.state.adapters.get(OPENCLAW_AGENT_ID)
            if oc and payload.get("chat_id"):
                try:
                    await oc.execute_tool(
                        f"{channel}_send",
                        {"chat_id": payload["chat_id"], "text": f"已处理 (task:{task_id[:8]})"},
                    )
                except Exception:
                    _log.debug("Channel reply failed for %s", channel)
        else:
            _log.info("Spec event ignored: type=%s", trigger_type)

    app.state.supervisor.register_sync(LoopSpec(
        "spec_events", _spec_trigger_handler,
        interval=30, priority=2, strategy=RestartStrategy.PERMANENT,
        loop_type="event"))


async def _reschedule_after_fire(conn, tid: str, cron_expr: str, now: int) -> None:
    """触发后的排期收尾：一次性 → completed；循环 cron → 重算 next_run_at。"""
    from repositories.scheduled_tasks_repo import complete_scheduled_task
    from services.cron_parser import next_run_at

    if not (cron_expr or "").strip():
        await complete_scheduled_task(conn, tid)
        _log.info("scheduled one-shot task %s fired and completed", tid[:8])
        return
    nxt = next_run_at(cron_expr, after=now)
    await complete_scheduled_task(conn, tid, next_run_at=nxt)
    _log.info("AUDIT #9: scheduled task %s fired, next run at %d", tid[:8], nxt)


def _register_scheduler_tick(app, conn) -> None:
    """注册 Cron 调度器 Loop——检索到期 scheduled_tasks 并触发执行。"""
    from services.loop_supervisor import LoopSpec, RestartStrategy

    # ── AUDIT #9: Cron 调度器 Loop——真正评估 cron 表达式 ──
    # 每 30s 检索 scheduled_tasks 表中 next_run_at <= now 的 pending 任务，
    # 原子认领（status → running），触发 Agent 执行，重新计算 next_run_at 恢复 pending。
    async def _scheduler_tick(state: dict):
        """AUDIT #9: cron 调度器 tick——检索到期任务并触发执行。

        fire-and-forget 模式：start_task 在后台跑 Agent，scheduler 立即
        重新计算 next_run_at 恢复 pending（不等待 Agent 完成）。这是标准
        cron 行为——调度器只负责按时触发，不跟踪任务执行结果。
        """
        from repositories.scheduled_tasks_repo import (
            claim_due_scheduled_tasks, complete_scheduled_task, fail_scheduled_task,
        )
        from services.cron_parser import next_run_at

        now = int(_time.time())
        try:
            due = await claim_due_scheduled_tasks(conn, now, limit=5)
        except Exception:
            _log.debug("scheduler_tick claim failed", exc_info=True)
            return
        for task in due:
            tid = task["id"]
            task_input = task["task_input"]
            agent_id = task.get("agent_id") or DEFAULT_AGENT_ID
            session_id = task.get("session_id") or str(_uuid.uuid4())
            cron_expr = task.get("cron_expr", "")
            max_budget = task.get("max_budget_tokens")  # AUDIT #10/#13
            try:
                # 触发 Agent 执行（fire-and-forget）
                # AUDIT #10/#13: 传入 max_budget_tokens 限制单次执行预算
                await app.state.agent_service.start_task(
                    agent_id, task_input, session_id, mode="agent",
                    max_budget_tokens=max_budget,
                )
                # 触发后的排期收尾（一次性 / 循环两种）
                await _reschedule_after_fire(conn, tid, cron_expr, now)
            except Exception as e:
                _log.warning("AUDIT #9: scheduled task %s fire failed: %s", tid[:8], e)
                await fail_scheduled_task(conn, tid, error=str(e))

    app.state.supervisor.register_sync(LoopSpec(
        "scheduler", _scheduler_tick,
        interval=30, priority=0, strategy=RestartStrategy.PERMANENT,
        loop_type="timer"))


def _register_backup_scheduler(app, conn, data_dir: str) -> None:
    """注册备份调度器 Loop——定时增量备份。"""
    from services.loop_supervisor import LoopSpec, RestartStrategy

    # ── Batch 5.6: 备份调度器 Loop——定时增量备份 ──
    # 独立于 agent 任务的 cron 调度，读 settings 评估 cron 表达式，
    # 到点触发 backup_service.create_scheduled_backup()。
    # 异常吞掉 + 日志，LoopSupervisor PERMANENT 策略兜底重启。
    async def _backup_scheduler_tick(state: dict):
        from services.backup_scheduler import run_backup_tick
        try:
            await run_backup_tick(app.state.settings_service, data_dir)
        except Exception:
            _log.debug("backup scheduler tick failed", exc_info=True)

    app.state.supervisor.register_sync(LoopSpec(
        "backup_scheduler", _backup_scheduler_tick,
        interval=LOOP_INTERVAL_BACKUP_SCHEDULER_SEC, priority=3,
        strategy=RestartStrategy.PERMANENT, loop_type="timer"))


# ════════════════════════════════════════════════════════════
# Supervisor 启动 + 崩溃恢复
# ════════════════════════════════════════════════════════════

async def _start_supervisor_and_recover(app, conn) -> None:
    """启动 LoopSupervisor + 崩溃任务智能恢复。"""
    try:
        await app.state.supervisor.start_all()
        _restored = await app.state.supervisor.resume_after_restart()
        if _restored > 0:
            _log.info("Resumed %d loop(s) from previous session", _restored)

        # Phase 62: 崩溃任务智能恢复——替代 Phase OH.5 的"全部标 failed"
        # 查询 updated_at < now-60s 的 running 任务，注入恢复上下文后重启 _run_agent
        try:
            _resumed = await app.state.agent_service.resume_after_restart()
            if _resumed > 0:
                _log.info("Phase 62: resumed %d crashed task(s)", _resumed)
        except Exception:
            _log.warning("Phase 62: crash recovery failed (non-fatal)", exc_info=True)
    except Exception:
        _log.warning("Supervisor startup failed (non-fatal), backend continues")


# ════════════════════════════════════════════════════════════
# Prompt 服务 + 长任务 Agent
# ════════════════════════════════════════════════════════════

def _init_prompt_services(app, conn, background_tasks: list, data_dir: str) -> None:
    """PromptService + PromptLibrary + PromptAgent + watchdog 热加载。"""
    # ═══ 创建不在 supervisor 中的服务对象 ═══
    from services.prompt_service import PromptService
    app.state.prompt_service = PromptService(data_dir=data_dir)
    # Phase 56: prompt 热加载改为 watchdog 事件驱动，替代 5s 轮询
    background_tasks.append(asyncio.create_task(_watch_prompts_with_watchdog(app.state.prompt_service)))

    # Phase 72: 统一 PromptLibrary——聚合 DB roles + YAML roles/behaviors + workflow templates
    from services.prompt_library import PromptLibrary
    app.state.prompt_library = PromptLibrary(conn)

    # Phase 74: PromptAgent 后台服务——推荐角色/行为/工作流 + 渲染 prompt 片段
    # 复用 adapters（line 204）/ model_config（line 188）/ prompt_library（line 590）
    from services.prompt_agent import PromptAgent
    app.state.prompt_agent = PromptAgent(
        conn=conn,
        adapters=app.state.adapters,
        model_config=app.state.model_config,
        library=app.state.prompt_library,
    )
    # Phase 76-A: lazy 注入到 AgentService（prompt_agent 晚于 AgentService 创建）
    app.state.agent_service.set_prompt_agent(app.state.prompt_agent)
    # Phase 88: lazy 注入到 IntentService（同上——prompt_agent 晚于 IntentService 创建）
    app.state.intent_service.set_prompt_agent(app.state.prompt_agent)


def _init_long_task_agents(app, conn) -> None:
    """长任务 Agent 链：GoalClarifier + AnalysisAgent + StandardsLookup + TaskDecomposer + Orchestrator。"""
    # Phase 78: GoalClarifier——长任务目标澄清 Agent
    # 复用 adapters / model_config（与 PromptAgent 同源）
    from services.long_task.goal_clarifier import GoalClarifier
    app.state.goal_clarifier = GoalClarifier(
        adapters=app.state.adapters,
        model_config=app.state.model_config,
    )

    # Task 6.10: AnalysisAgent——长任务 5 维度分析 Agent
    # 在 clarifying 和 decomposing 之间生成 complexity/dependencies/risks/resources/acceptance 报告
    from services.long_task.analysis_agent import AnalysisAgent, set_analysis_agent
    app.state.analysis_agent = AnalysisAgent(
        adapters=app.state.adapters,
        model_config=app.state.model_config,
    )
    set_analysis_agent(app.state.analysis_agent)  # 全局单例供路由层用

    # Task 6.13: StandardsLookupAgent——decompose 前置查询最新文档
    # 复用 Phase 85 实现（原为失败后查询，此处提前创建供 orchestrator 前置调用）
    from services.long_task.standards_lookup_agent import (
        StandardsLookupAgent, set_standards_lookup_agent)
    app.state.standards_lookup_agent = StandardsLookupAgent(
        agent_service=app.state.agent_service,
        model_config=app.state.model_config,
    )
    set_standards_lookup_agent(app.state.standards_lookup_agent)  # 全局单例供路由层用

    # Phase 79: TaskDecomposer——长任务分解 Agent
    # 复用 adapters / model_config，conn 用全局 db（写 plan_steps）
    from services.long_task.task_decomposer import TaskDecomposer, set_decomposer
    app.state.task_decomposer = TaskDecomposer(
        adapters=app.state.adapters,
        model_config=app.state.model_config,
        conn=app.state.db,
    )
    set_decomposer(app.state.task_decomposer)  # 全局单例供路由层用

    # Phase 80: LongTaskOrchestrator——长任务生命周期主控器
    # 串联 GoalClarifier(78) + AnalysisAgent(6.10) + TaskDecomposer(79) + spawn_subagent 执行
    from services.long_task.orchestrator import LongTaskOrchestrator, set_orchestrator
    app.state.long_task_orchestrator = LongTaskOrchestrator(
        agent_service=app.state.agent_service,
        goal_clarifier=app.state.goal_clarifier,
        task_decomposer=app.state.task_decomposer,
        conn=app.state.db,
        ws_manager=app.state.agent_service.ws_manager,
        analysis_agent=app.state.analysis_agent,  # Task 6.10
        standards_lookup_agent=app.state.standards_lookup_agent,  # Task 6.13
    )
    set_orchestrator(app.state.long_task_orchestrator)  # 全局单例供路由层用


# ════════════════════════════════════════════════════════════
# 构建/审查/Spec + EventBus 注入 + 恢复
# ════════════════════════════════════════════════════════════

def _init_build_review_spec(app, conn) -> None:
    """GC Agent + MemorySyncBridge + BuildAnalyzer + ReviewPipeline + SpecOrchestrator。"""
    # Phase 39: GC Agent
    from services.gc_agent import GCAgent
    from security.sandbox import get_workspace as _get_ws
    _ws_dir = str(_get_ws())
    app.state.gc_agent = GCAgent(db=conn, workspace=_ws_dir)

    # Phase 41: MemorySyncBridge
    from services.memory.memory_sync import MemorySyncBridge
    app.state.memory_sync = MemorySyncBridge(
        db=conn, memory_tree=app.state.memory_tree, workspace=_ws_dir)

    # Phase 46 E.2+E.3: 构建分析器 + 审查流水线
    from services.build_analyzer import BuildAnalyzer
    app.state.build_analyzer = BuildAnalyzer(db=conn)
    from services.review_pipeline import ReviewPipeline
    app.state.review_pipeline = ReviewPipeline(
        db=conn, agent_service=app.state.agent_service,
        deliberation_service=app.state.deliberation_service)

    # Phase 45 D.3: Spec 编排器——多 Spec 依赖图拓扑排序
    from services.spec_orchestrator import SpecOrchestrator
    app.state.spec_orchestrator = SpecOrchestrator(
        deliberation_service=app.state.deliberation_service, db=conn)


async def _inject_event_bus_and_recover(app, conn) -> None:
    """EventBus 注入各服务 + register_event_handlers + 崩溃任务恢复。"""
    # Phase 80: 注入 EventBus 到 AgentService（run_agent 完成/失败时 publish TASK_COMPLETED/FAILED）
    app.state.agent_service.set_event_bus(app.state.event_bus)
    # Task 6.6: MemoryTree 订阅 ASSISTANT_TURN_COMPLETE 事件——事件驱动即时提取
    # （debounce 30s + 15min background_loop 兜底，对标 Claude Code 每轮即时提取）
    if hasattr(app.state, "memory_tree") and app.state.memory_tree:
        app.state.memory_tree.start_event_subscription()
    # 会话标题订阅同一事件——assistant 回复落库后异步生成标题（DSH session-title）
    if getattr(app.state, "session_title_service", None):
        app.state.session_title_service.start_event_subscription()
    # Phase 87 审查 C4: 注入 EventBus 到 LongTaskOrchestrator（_mark_failed/_mark_completed
    # 发布 TASK_FAILED/TASK_COMPLETED，供 task_supervisor stall 检测正常工作）
    if hasattr(app.state, "long_task_orchestrator"):
        app.state.long_task_orchestrator.set_event_bus(app.state.event_bus)

        # Batch B M10: 先完成 notification_center 的 db/ws_manager 注入 + 注册 event handlers，
        # 再执行 stale task 恢复。原顺序：恢复在前(line 990/1000)、NC 注入 + handler 注册在后
        # (line 1040/1044)——恢复时 publish 的 TASK_FAILED 事件无人订阅，通知丢失。
        # 现提前到恢复之前，确保恢复产生的事件能被 NC handler 捕获并落盘。
        if hasattr(app.state, "notification_center"):
            app.state.notification_center.set_db(conn)
            app.state.notification_center.set_ws_manager(app.state.agent_service.ws_manager)
        await register_event_handlers(app, conn)

        # P0-1.2: 启动时恢复卡在 clarifying 的长任务——
        # 服务重启后用户 WS 已断开，phase=clarifying 的任务无法继续，标记为 failed
        try:
            _recovered = await app.state.long_task_orchestrator.recover_stale_clarifying_tasks()
            if _recovered > 0:
                _log.info("P0-1.2: recovered %d stale clarifying task(s)", _recovered)
        except Exception:
            _log.warning("P0-1.2: stale clarifying recovery failed (non-fatal)",
                         exc_info=True)
        # P0-4.5: 启动时恢复崩溃前在 executing 阶段的长任务——
        # Phase 47 已将 running 改为 failed，此方法将 phase=executing 的从 failed
        # 改为 interrupted（可 resume），保留 plan_steps 断点支持断点续传
        try:
            _recovered_exec = await app.state.long_task_orchestrator.recover_stale_executing_tasks()
            if _recovered_exec > 0:
                _log.info("P0-4.5: recovered %d stale executing task(s)", _recovered_exec)
        except Exception:
            _log.warning("P0-4.5: stale executing recovery failed (non-fatal)",
                         exc_info=True)


def _init_task_supervisor_review(app, conn, background_tasks: list) -> None:
    """TaskSupervisor + FinalReviewAgent + AdversarialAgent 单例注入。"""
    # ═══ Phase 80/83/84/85: 长任务审查链 + 监督舱单例注入 ═══
    # 修复 1.2（v2 第一批）：4 个完整实现的类此前从未实例化——
    #   - TaskSupervisor: 6 个 supervisor 端点全 503、stall 检测死代码
    #   - FinalReviewAgent: Phase 80 长任务审查流水线失效
    #   - AdversarialAgent: Phase 84 对抗验证失效
    #   - StandardsLookupAgent: Phase 85 标准查询失效
    # 均因 lifespan 漏写实例化语句（"定义即遗忘"模式）。
    from services.task_supervisor import TaskSupervisor
    app.state.task_supervisor = TaskSupervisor(
        conn=conn,
        orchestrator=app.state.long_task_orchestrator,
        adapters=app.state.adapters,
        model_config=app.state.model_config,
        ws_manager=app.state.agent_service.ws_manager,
        event_bus=app.state.event_bus,
    )
    # 启动 monitor_loop 后台任务（订阅 EventBus + 轮询 running 长任务 + stall 检测）
    _supervisor_task = asyncio.create_task(app.state.task_supervisor.monitor_loop())
    background_tasks.append(_supervisor_task)

    from services.long_task.final_review_agent import (
        FinalReviewAgent, set_final_review_agent)
    from services.long_task.adversarial_agent import (
        AdversarialAgent, set_adversarial_agent)
    set_final_review_agent(FinalReviewAgent(
        adapters=app.state.adapters, model_config=app.state.model_config))
    set_adversarial_agent(AdversarialAgent(
        agent_service=app.state.agent_service, model_config=app.state.model_config))
    # Task 6.13: StandardsLookupAgent 已在 orchestrator 之前创建（供前置查询 + 路由层共用）
    _log.info("Phase 80/83/84/85: TaskSupervisor + 3 review agents injected")

    # Batch B M10: notification_center.set_db/set_ws_manager + register_event_handlers
    # 已提前到 recover_stale_* 之前（见上方 long_task_orchestrator 分支内），
    # 确保恢复期间 publish 的 TASK_FAILED 事件能被 NC handler 捕获。


# ════════════════════════════════════════════════════════════
# 扩展服务
# ════════════════════════════════════════════════════════════

async def _init_security_hooks(app, conn, data_dir: str) -> None:
    """Snapshot + audit hook + network_guard 模式加载。"""
    # ═══ 扩展服务 ═══
    from services.snapshot_service import SnapshotService, HarnessRecorder
    app.state.snapshot_service = SnapshotService(db=conn)
    app.state.harness_recorder = HarnessRecorder(db=conn)
    from hooks.registry import set_snapshot_service
    set_snapshot_service(app.state.snapshot_service)

    # Round 1 S6: 注入 DB 连接给审计 hook——写 audit_log 表
    from hooks.registry import set_audit_db
    set_audit_db(conn)

    # CodeWhale 借鉴 #12: 默认启用 JsonlHookSink——追加审计事件到 audit.jsonl
    # 提供独立于 DB 的文件审计轨迹，可用 tail -f 实时监控或外部日志收集
    try:
        import os as _os
        from hooks.registry import JsonlHookSink, add_hook_sink
        _audit_jsonl = _os.path.join(data_dir, "audit.jsonl")
        add_hook_sink(JsonlHookSink(_audit_jsonl))
        _log.info("JsonlHookSink enabled: %s", _audit_jsonl)
    except Exception:
        _log.debug("JsonlHookSink setup failed (non-critical)", exc_info=True)

    # Round 1 S7: 从 settings 读取网络隔离模式注入 network_guard
    try:
        from security.network_guard import set_network_mode
        net_mode_raw = await app.state.settings_service.get_raw("security.network_mode")
        set_network_mode(net_mode_raw or "off")
    except Exception:
        _log.debug("network_guard mode load failed, defaulting to off", exc_info=True)


async def _init_orchestrator_skills(app, conn, data_dir: str) -> None:
    """GitWorktree + Terminal + DynamicOrchestrator + slash commands + skill_registry。"""
    from services.git_worktree_service import GitWorktreeService
    app.state.worktree_service = GitWorktreeService(git_service=app.state.git_service)

    from services.terminal_manager import get_terminal_manager
    app.state.terminal_manager = get_terminal_manager()

    # Phase 38: AgentPlatformBridge 类保留，不再创建孤立实例（无人调用）
    # from adapters.platform_bridge import AgentPlatformBridge
    # app.state.platform_bridge = AgentPlatformBridge()

    from services.dynamic_orchestrator import DynamicOrchestrator
    app.state.dynamic_orchestrator = DynamicOrchestrator(
        agent_service=app.state.agent_service,
        workflow_service=app.state.workflow_service,
        engine=app.state.workflow_service.engine,
    )

    from commands.registry import get_registry
    app.state.slash_commands = get_registry()

    from services.evolution.skill_registry import get_skill_registry
    app.state.skill_registry = get_skill_registry(data_dir=data_dir, adapters=app.state.adapters)
    skill_install_result = await app.state.skill_registry.install_system_skills()
    if skill_install_result.get("count", 0) > 0:
        app.state.pending_skill_install_toast = {
            "count": skill_install_result["count"],
            "installed_ids": skill_install_result.get("installed_ids", []),
        }
    app.state.evolution_engine._skill_registry = app.state.skill_registry

    from workflow.node_executor import AgentNodeExecutor
    app.state.workflow_service.engine.executors["default"] = AgentNodeExecutor(
        app.state.agent_service, worktree_service=app.state.worktree_service,
    )


async def _init_codegraph(app) -> None:
    """CodeGraph 单例——启动时 start()，工具层复用。"""
    # Task 4.2: CodeGraph 单例——启动时 start()，工具层复用，避免每次调用 fork Node 子进程。
    # 修正 workspace_path: 原指向 data_dir/workspace/shared（Yuncat 数据目录，非用户代码），
    # 改为 get_workspace()（用户项目根，与 system_tool_search.codegraph_context 一致）。
    # 对标 Aider repo-map 单例 / Cursor codebase index 复用——启动建索引，运行时复用。
    from services.codegraph_client import CodeGraphClient
    from security.sandbox import get_workspace
    app.state.codegraph = CodeGraphClient(workspace_path=str(get_workspace()))
    try:
        await app.state.codegraph.start()
        _log.info("CodeGraph started (workspace=%s)", app.state.codegraph._ws)
    except Exception:
        # start 失败不阻塞启动——codegraph_context 工具有 search_files fallback
        _log.warning("CodeGraph start failed (non-fatal — codegraph_context will fallback to search_files)", exc_info=True)
        app.state.codegraph = None


# ════════════════════════════════════════════════════════════
# 模板 + 会话上下文 + 文件监控
# ════════════════════════════════════════════════════════════

async def _init_templates_yaml(app, conn) -> None:
    """内置模板注册 + YAML roles 种子到 DB。"""
    # ═══ 内置模板注册 ═══
    await _insert_builtin_templates(conn)

    # Phase 72: YAML roles 种子到 DB（幂等——同 id 跳过）
    # 让 YAML 新增角色自动入库（source='builtin'），允许用户后续编辑覆盖
    try:
        from services.prompt_library import seed_yaml_roles_to_db
        _seeded = await seed_yaml_roles_to_db(conn)
        if _seeded > 0:
            _log.info("Phase 72: seeded %d YAML roles to DB", _seeded)
    except Exception:
        _log.warning("Phase 72: YAML roles seeding failed (non-fatal)", exc_info=True)


async def _has_start_context_data(app, tree, feedback) -> bool:
    """判断是否**真有必要**在启动期做那次语义检索。

    2026-09-15 启动剖析：`retrieve_context` 会触发本地 embedding 模型加载
    （含 transformers 1.4s），而全新安装/空记忆库时它必然查无结果——
    为一个注定为空的结果付 1.4s 不值得。这里先看有没有数据，没有就跳过。
    """
    try:
        cursor = await app.state.db.execute("SELECT 1 FROM knowledge_nodes LIMIT 1")
        if await cursor.fetchone():
            return True
    except Exception:
        _log.debug("start-context: knowledge_nodes probe failed", exc_info=True)
    return False


async def _load_session_start_context(app) -> None:
    """加载上次会话上下文（MemoryTree + FeedbackLearner）。

    只在**第一个任务**被读一次（`agent_context.py` 读后即清空），因此启动期
    不需要同步等它——调用方现在通过 `_schedule_session_start_context` 放到后台，
    不阻塞 YUNCAT_READY。
    """
    # Phase 43 T1 (SessionStart): 加载上次会话上下文
    try:
        agent_svc = app.state.agent_service
        tree = getattr(agent_svc, '_memory_tree', None)
        feedback = getattr(agent_svc, '_feedback_learner', None)
        if tree or feedback:
            # 空记忆库 → 语义检索必然空手而归，直接跳过（省掉 embedding 模型加载）
            if tree and not await _has_start_context_data(app, tree, feedback):
                _log.debug("SessionStart: no knowledge nodes yet, skip retrieval")
                return
            ctx_parts = []
            if tree:
                try:
                    tctx = await tree.retrieve_context("user profile preferences project", limit=5)
                    if tctx:
                        ctx_parts.append(f"## 上次会话的记忆\n{tctx}")
                except Exception:
                    _log.debug("Memory tree context retrieval failed", exc_info=True)
            if feedback:
                try:
                    pctx = await feedback.get_personalization_context(limit=5)
                    if pctx:
                        ctx_parts.append(f"## 个性化偏好\n{pctx}")
                except Exception:
                    _log.debug("Feedback personalization context failed", exc_info=True)
            if ctx_parts:
                app.state._session_start_context = "\n\n".join(ctx_parts)
                _log.info("SessionStart: loaded context (%d parts)", len(ctx_parts))
    except Exception:
        _log.debug("SessionStart context load skipped", exc_info=True)

    _log.info("Adapters: %s", list(app.state.adapters.keys()))


def _schedule_session_start_context(app, background_tasks: list) -> None:
    """把「上次会话上下文」的检索放到后台，不阻塞启动。

    依据：该值只被第一个任务读取一次（读后即清空），启动期同步等待纯属浪费——
    实测它占启动总耗时的 1.4s（其中大头是本地 embedding 模型加载）。
    放到后台后，用户发第一条消息前通常已完成；即便没完成，也只是少一段
    "上次会话记忆"前缀，与"本来就没有记忆"表现一致。
    """
    async def _load():
        try:
            await _load_session_start_context(app)
        except Exception:
            _log.debug("background session-start context load failed", exc_info=True)
    background_tasks.append(asyncio.create_task(_load()))


async def _init_file_watcher(app) -> None:
    """文件监控——对标 Claude Code 自动检测外部文件修改。"""
    # Phase 47 P3.2: 文件监控——对标 Claude Code 自动检测外部文件修改
    try:
        from services.infra.file_watcher import FileWatcherService
        app.state.file_watcher = FileWatcherService()
        st = app.state.settings_service
        watch_enabled = True
        if st:
            try:
                s = await st.get_all()
                watch_enabled = s.get("watch_files_enabled", "true") != "false"
            except Exception as e:
                # Phase 48 T3-15: settings 读取失败必上报（原 except: pass 静默吞咽 → 配置异常无日志）
                _log.warning("Failed to read watch_files_enabled setting: %s", e)
        if watch_enabled:
            from security.sandbox import get_workspace
            ws = str(get_workspace()) if get_workspace() else ""
            if ws:
                ok = await app.state.file_watcher.start(ws, app.state.event_bus)
                if ok:
                    # 订阅 file_changed → WS 推送前端刷新 FileTree
                    async def _on_file_changed(evt_type: str, data: dict):
                        ag = app.state.agent_service
                        if ag:
                            await ag._safe_send("__broadcast__", {
                                "type": "file_changed", "paths": data.get("paths", []),
                                "count": data.get("count", 0),
                            })
                    app.state.event_bus.subscribe("file_changed", _on_file_changed)

                    # E3: 订阅 diagnostics_updated → WS 推送前端 Monaco inline hint
                    async def _on_diagnostics_updated(evt_type: str, data: dict):
                        ag = app.state.agent_service
                        if ag:
                            await ag._safe_send("__broadcast__", {
                                "type": "diagnostics",
                                "file": data.get("file", ""),
                                "language": data.get("language", ""),
                                "diagnostics": data.get("diagnostics", []),
                            })
                    app.state.event_bus.subscribe("diagnostics_updated", _on_diagnostics_updated)
    except Exception:
        _log.debug("File watcher setup skipped", exc_info=True)


# ════════════════════════════════════════════════════════════
# 启动指标上报
# ════════════════════════════════════════════════════════════

async def _report_startup_metrics(app) -> None:
    """上报启动各阶段耗时到 analytics_events。"""
    app.state.startup_stage = "ready"

    # Phase 97: 上报启动各阶段耗时到 analytics_events
    try:
        _t_ready = _time.time()
        _t0 = getattr(app.state, "_startup_t0", _t_ready)
        _t_db = getattr(app.state, "_startup_t_db_init", _t0)
        _t_adp = getattr(app.state, "_startup_t_adapters", _t_db)
        phases = {
            "init_ms": int((_t_db - _t0) * 1000),
            "db_init_ms": int((_t_adp - _t_db) * 1000),
            "adapters_ms": int((_t_ready - _t_adp) * 1000),
        }
        total_ms = int((_t_ready - _t0) * 1000)
        import platform as _platform
        await app.state.analytics_service.startup_metrics(
            phases=phases,
            total_ms=total_ms,
            extra={
                "version": os.environ.get("YUNCAT_VERSION", "dev"),
                "platform": _platform.platform(),
                "python_version": _platform.python_version(),
                "is_first_run": bool(getattr(app.state, "_is_first_run", False)),
            },
        )
        _log.info("Startup metrics reported: total=%dms phases=%s", total_ms, phases)
    except Exception:
        _log.debug("Failed to report startup metrics", exc_info=True)
