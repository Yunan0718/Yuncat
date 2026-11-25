"""lifespan_services.py —— 从 lifespan.py._init_services 提取的服务初始化逻辑

将原 ~990 行的 _init_services 按逻辑分组拆分为多个聚焦的小函数（每函数 <50 行，
个别含长闭包的略超），lifespan.py 仅保留 create_lifespan 编排层 + _init_services 薄调度。

拆分原则：
  - 不改变任何逻辑——仅提取函数 + 修 import
  - 不循环依赖——本模块不 import lifespan.py
  - 每函数按初始化阶段聚焦单一职责

函数调用顺序由 lifespan._init_services 编排，保持与原 _init_services 完全一致。
"""
import asyncio
import logging
import os
import time as _time

import aiohttp

from config import (
    DEFAULT_AGENT_ID, OPENCLAW_AGENT_ID,
    BACKGROUND_AGENT_INTERVAL_SECONDS,
    BACKGROUND_AGENT_MAX_PENDING,
    MAX_TOKENS_LIFESPAN_HEALTH,
    LIFESPAN_HEALTH_SUMMARY_INPUT_MAX,
    LOOP_INTERVAL_WORKFLOW_CRON_SEC,
    LOOP_INTERVAL_HEALER_SEC,
    LOOP_INTERVAL_MEMORY_TREE_SEC,
    LOOP_INTERVAL_AUTO_DREAM_SEC,
    LOOP_INTERVAL_EVOLUTION_SEC,
    LOOP_INTERVAL_MEMORY_SYNC_SEC,
    LOOP_INTERVAL_GC_AGENT_SEC,
    LOOP_INTERVAL_CLEANUP_SEC,
    LOOP_INTERVAL_APPROVAL_EXPIRE_SEC,
    LOOP_INTERVAL_HARNESS_RECOVERY_SEC,
    OC_AUTOSTART_DELAY_SEC,
    OC_AUTOSTART_TIMEOUT_SEC,
    EVENT_BUS_PERSISTED_TYPES,
)

# 复用已提取的辅助模块（不经过 lifespan.py，避免循环依赖）
from lifespan_helpers import (
    _periodic_cleanup,
)

_log = logging.getLogger("yuncat.lifespan")


# ════════════════════════════════════════════════════════════
# 核心服务
# ════════════════════════════════════════════════════════════

async def _sync_approval_mode(conn) -> None:
    """把审批方式（问我 / AI 审批 / 全放行）同步到 env——审批层与提示词读同一份"""
    try:
        from security.approval import MODE_ENV_VAR, resolve_approval_mode

        os.environ[MODE_ENV_VAR] = await resolve_approval_mode(conn)
    except Exception:
        # 回退：读不到就保持默认 ask（审批层本身也 fail-closed 到 ask）
        _log.debug("Failed to sync approval mode", exc_info=True)


async def _init_core_services(app, conn) -> None:
    """核心服务实例化：memory / tools / rules / settings / model_config / analytics / cache。"""
    from services.memory.memory_service import MemoryService
    from services.tools.tool_service import ToolService
    from services.rules_service import RulesService
    from services.settings_service import SettingsService
    from services.analytics_service import AnalyticsService
    from repositories.memory_repo import MemoryRepository

    app.state.memory_service = MemoryService(conn, memory_repo=MemoryRepository(conn))
    from security.profiles import get_current_profile_config
    app.state.security_profile = get_current_profile_config()
    app.state.tool_service = ToolService(rules_service=None)
    app.state.rules_service = RulesService(conn)
    app.state.settings_service = SettingsService(conn)

    # 工作区路径——从数据库同步到环境变量（设置页写入 DB → 此处读取并设置到 env）
    try:
        ws_from_db = await app.state.settings_service.get_raw("workspace")
        if ws_from_db:
            os.environ["YUNCAT_WORKSPACE"] = ws_from_db
            _log.info("Workspace set to %s (from saved setting)", ws_from_db)
    except Exception:
        _log.debug("Failed to sync workspace from settings", exc_info=True)

    # Phase 39: 从 settings 同步 security_profile 到环境变量
    try:
        profile_from_db = await app.state.settings_service.get_raw("security_profile")
        if profile_from_db:
            os.environ["YUNCAT_SECURITY_PROFILE"] = profile_from_db
            from security.profiles import get_current_profile_config
            app.state.security_profile = get_current_profile_config()
            _log.info("Security profile set to %s (from saved setting)", profile_from_db)
    except Exception:
        _log.debug("Failed to sync security profile", exc_info=True)

    await _sync_approval_mode(conn)

    from services.model_config_service import ModelConfigService
    app.state.model_config = ModelConfigService(settings_service=app.state.settings_service)
    if hasattr(app.state.memory_service, 'set_model_config_ref'):
        app.state.memory_service.set_model_config_ref(app.state.model_config)
    app.state.analytics_service = AnalyticsService(conn)
    # Batch 8.4: LLM 响应缓存（全局单例，挂到 app.state 便于路由查询统计）
    from services.llm_cache import get_global_cache
    app.state.llm_cache = get_global_cache()
    app.state.tool_service.rules_service = app.state.rules_service


async def _init_arena_and_notifications(app, conn) -> None:
    """Arena 盲测服务 + NotificationCenter（Agent 异步推送）。"""
    # Batch 5.1: Arena 盲测 A/B 服务
    from services.arena_service import ArenaService
    app.state.arena_service = ArenaService(
        db=conn,
        model_config_service=app.state.model_config,
        analytics_service=app.state.analytics_service,
    )

    # Phase OH.7: NotificationCenter — Agent 异步推送
    from services.notify import NotificationCenter
    app.state.notification_center = NotificationCenter()
    _pending_notifs = await app.state.notification_center.load_from_disk()
    if _pending_notifs:
        _log.info("Loaded %d pending notifications from disk", len(_pending_notifs))


# ════════════════════════════════════════════════════════════
# Adapter 注册 + 预热
# ════════════════════════════════════════════════════════════

def _init_adapters(app, conn) -> None:
    """Adapter 发现 + 依赖注入 + Git/VCS 服务。"""
    app.state.startup_stage = "adapters"
    app.state._startup_t_adapters = _time.time()
    from adapters.registry import discover_adapters, set_global_adapters
    app.state.adapters = discover_adapters()
    set_global_adapters(app.state.adapters)

    if hasattr(app.state.memory_service, 'set_adapters'):
        app.state.memory_service.set_adapters(app.state.adapters)

    # 把统一模型配置注入所有声明了 set_model_config 的适配器。
    # 刻意不针对具体 agent：原先只给 openclaw 注入、漏了 hermes，是不对称的硬编码
    # （两者共用 ext_http 基类，那里是 noop 存储；真正使用配置的是 native 适配器）。
    _injected = 0
    for _aid, _adapter in app.state.adapters.items():
        if hasattr(_adapter, "set_model_config"):
            _adapter.set_model_config(app.state.model_config)
            _injected += 1
    _log.info("model config injected into %d adapter(s)", _injected)

    from services.git_service import GitService
    app.state.git_service = GitService(model_config_service=app.state.model_config)

    # Phase 104: VCS 服务（GitHub/GitLab PR 集成）
    from services.vcs_service import VCSService
    app.state.vcs_service = VCSService(conn)


def _schedule_adapter_autowarm(app, background_tasks: list) -> None:
    """原生 yuncat 就绪标记 + OpenClaw 网关预热。"""
    # ═══ 原生 yuncat：无子进程，start 仅标记就绪 ═══
    # 不显式标记的话，首条任务前 /agents 与顶栏都会显示 stopped（实测）。
    async def _mark_native_ready():
        nat = app.state.adapters.get(DEFAULT_AGENT_ID)
        if nat and not nat._ready.is_set():
            await nat.start()
            await app.state.agent_service.push_agent_status(nat.agent_id, "ready")
            _log.info("Yuncat native agent marked ready at startup")
    background_tasks.append(asyncio.create_task(_mark_native_ready()))

    # ═══ OpenClaw 预热 ═══
    async def _auto_start_openclaw():
        await asyncio.sleep(OC_AUTOSTART_DELAY_SEC)
        oc = app.state.adapters.get(OPENCLAW_AGENT_ID)
        if oc and not oc._ready.is_set():
            try:
                await asyncio.wait_for(oc.start(), timeout=OC_AUTOSTART_TIMEOUT_SEC)
                _log.info("OpenClaw auto-started on port %d", oc._port)
                await app.state.agent_service.push_agent_status(OPENCLAW_AGENT_ID, "ready")
                # Phase 43 E.1: EventBus 发布 AGENT_STARTED
                if hasattr(app.state, 'event_bus'):
                    await app.state.event_bus.publish("agent_started", {"agent_id": OPENCLAW_AGENT_ID, "port": oc._port})
            except asyncio.TimeoutError:
                _log.warning("OpenClaw auto-start timed out after 15s (will retry on first use)")
                await app.state.agent_service.push_agent_status(OPENCLAW_AGENT_ID, "error")
            except Exception as e:
                _log.warning("OpenClaw auto-start failed (will start on first use): %s", e)
                await app.state.agent_service.push_agent_status(OPENCLAW_AGENT_ID, "error")
    background_tasks.append(asyncio.create_task(_auto_start_openclaw()))

    # 2026-09-10: Hermes 自动预热块已删除——Hermes 下线（DISABLED_ADAPTERS），
    # 且原块误推 DEFAULT_AGENT_ID 的状态（潜在状态错位 bug，随删除一并消失）。


# ════════════════════════════════════════════════════════════
# AgentService + Workflow
# ════════════════════════════════════════════════════════════

def _init_agent_service(app, conn) -> None:
    """MemoryTree + FeedbackLearner + AgentService + summarizer 注入。"""
    # ═══ Memory Tree + 反馈学习 ═══
    from services.memory.memory_tree import MemoryTree
    from services.feedback_learning import FeedbackLearner
    app.state.memory_tree = MemoryTree(db=conn)
    app.state.feedback_learner = FeedbackLearner(db=conn)

    # ═══ AgentService ═══
    from services.agent.agent_service import AgentService
    app.state.agent_service = AgentService(
        conn, app.state.adapters, app.state.memory_service,
        settings_service=app.state.settings_service,
        model_config_service=app.state.model_config,
        memory_tree=app.state.memory_tree,
        feedback_learner=app.state.feedback_learner,
        tool_service=app.state.tool_service,
    )
    # 会话标题服务（对标 DSH session-title）——标题策略在 session_titles.py，
    # 这里只负责装配：DB + 模型配置 + WS 推送通道。
    from services.session_title_service import SessionTitleService
    app.state.session_title_service = SessionTitleService(
        conn, app.state.model_config)
    app.state.session_title_service.set_ws_manager(app.state.agent_service.ws_manager)
    # Task 7.2: 注入 LLM summarizer 到 MemoryTree——用于 RAPTOR 聚类 + index_from_conversations
    # MemoryTree 先于 AgentService 构造，故在此 setter 注入（对标 set_evolution_engine 模式）
    async def _memory_summarizer(prompt: str) -> str:
        return await app.state.agent_service._invoke_with_role(DEFAULT_AGENT_ID, "", prompt)
    app.state.memory_tree.set_summarizer(_memory_summarizer)


def _init_workflow_engine(app, conn) -> None:
    """WorkflowService + WorkflowEngine + 节点执行器注册。"""
    from services.workflow.workflow_service import WorkflowService
    from workflow.engine import WorkflowEngine
    from workflow.node_executor import (
        AgentNodeExecutor,
        ManagerNodeExecutor,
        MergeNodeExecutor,
        SummaryNodeExecutor,
        ToolExecuteNodeExecutor,
        HANativeNodeExecutor,
    )
    from workflow.hitl_executor import HumanNodeExecutor
    from workflow.events import EventLogger

    app.state.hitl_executor = HumanNodeExecutor(app.state.agent_service)
    app.state.workflow_service = WorkflowService(
        conn, app.state.agent_service, app.state.tool_service,
        notification_center=app.state.notification_center,
    )
    # Phase 100: 注册 manager / merge / summary 节点执行器
    app.state.workflow_service.engine = WorkflowEngine(
        repo=app.state.workflow_service,
        executors={
            "default": AgentNodeExecutor(app.state.agent_service),
            "human": app.state.hitl_executor,
            "manager": ManagerNodeExecutor(),
            "merge": MergeNodeExecutor(),
            "summary": SummaryNodeExecutor(
                agent_service=app.state.agent_service, db=conn,
            ),
            "tool_execute": ToolExecuteNodeExecutor(app.state.tool_service),
            # Phase 43 H6: HA 子进程原生节点——直接路由到 Hermes Agent 子进程
            "ha_native": HANativeNodeExecutor(app.state.agent_service),
        },
        event_logger=EventLogger(app.state.workflow_service),
        model_config=app.state.model_config,
        adapters=app.state.adapters,
    )


def _init_evolution_and_approval(app, conn) -> None:
    """SelfEvolutionEngine + hook notification_center + ApprovalInboxService。"""
    # Batch 5.3: SelfEvolutionEngine 需 workflow_service 做 A/B baseline、notification_center 发通知
    from services.evolution.self_evolution import SelfEvolutionEngine
    app.state.evolution_engine = SelfEvolutionEngine(
        db=conn, memory_tree=app.state.memory_tree, model_config=app.state.model_config,
        adapters=app.state.adapters,
        notification_center=app.state.notification_center,
        workflow_service=app.state.workflow_service,
    )
    app.state.agent_service.set_evolution_engine(app.state.evolution_engine)

    # Phase D W.2: 将 NotificationCenter 注入 hook registry，供内置 notification hook 使用
    from hooks.registry import set_notification_center
    set_notification_center(app.state.notification_center)

    # Phase 107: 统一审批 Inbox——从 runtime_states 聚合审批项
    from services.approval_inbox import ApprovalInboxService
    app.state.approval_inbox = ApprovalInboxService(db=conn)


# ════════════════════════════════════════════════════════════
# EventBus + LoopSupervisor
# ════════════════════════════════════════════════════════════

async def _init_event_bus_and_supervisor(app, conn, background_tasks: list) -> None:
    """EventBus 实例化 + 订阅注册 + LoopSupervisor + workflow_cron 触发器。"""
    # Batch 7.4: EventBus 必须先实例化并注入 db，再注册任何订阅——
    # 原代码先用 get_event_bus() 拿到临时实例订阅，后续又新建 EventBus 替换全局单例，
    # 导致 workflow_trigger_service 等订阅丢失。
    from services.event_bus import EventBus, Events, _set_event_bus
    from config import EVENT_BUS_PERSISTED_TYPES

    app.state.event_bus = EventBus(
        db=conn,
        persisted_types=EVENT_BUS_PERSISTED_TYPES,
    )
    _set_event_bus(app.state.event_bus)
    await app.state.event_bus.start()
    background_tasks.append(app.state.event_bus._consumer_task)
    if app.state.event_bus._outbox_poll_task:
        background_tasks.append(app.state.event_bus._outbox_poll_task)

    # Phase 101: 工作流事件触发器
    from services.workflow_trigger_service import WorkflowTriggerService
    app.state.workflow_trigger_service = WorkflowTriggerService(
        db=conn, workflow_service=app.state.workflow_service,
    )

    # 订阅 EventBus 事件
    app.state.event_bus.subscribe("task_completed", app.state.workflow_trigger_service.handle_event)
    app.state.event_bus.subscribe("file_changed", app.state.workflow_trigger_service.handle_event)
    app.state.event_bus.subscribe("session_started", app.state.workflow_trigger_service.handle_event)

    # Phase 45-46: 先实例化 LoopSupervisor，后续所有 register/register_sync 才能使用
    from tools.builtin.system_tools import _cleanup_expired_states
    from services.loop_supervisor import LoopSupervisor, LoopSpec, RestartStrategy
    app.state.supervisor = LoopSupervisor(db=conn)

    # Cron 触发器 Loop：60s 扫描 workflow_triggers
    async def _workflow_cron_tick(_state: dict):
        await app.state.workflow_trigger_service.cron_tick()

    app.state.supervisor.register_sync(LoopSpec(
        "workflow_cron", _workflow_cron_tick,
        interval=LOOP_INTERVAL_WORKFLOW_CRON_SEC, priority=-1, strategy=RestartStrategy.PERMANENT,
        loop_type="timer",
    ))

    # Phase C: harness 崩溃恢复扫描——重启后重置残留 RUNNING/WAITING_PERMISSION
    # （与 _workflow_cron_tick 同构挂载；无 harness 表时 ensure_harness_schema 幂等建表）
    async def _harness_recovery_tick(_state: dict):
        try:
            from harness.runtime import scan_stale_running_tasks
            from repositories.harness_schema import ensure_harness_schema

            await ensure_harness_schema(conn)
            await scan_stale_running_tasks(conn, notifier=app.state.event_bus)
        except Exception:
            _log.warning("harness recovery tick failed", exc_info=True)

    app.state.supervisor.register_sync(LoopSpec(
        "harness_recovery", _harness_recovery_tick,
        interval=LOOP_INTERVAL_HARNESS_RECOVERY_SEC, priority=-1,
        strategy=RestartStrategy.PERMANENT, loop_type="timer",
    ))


# ════════════════════════════════════════════════════════════
# Deliberation + Vector + Indexer
# ════════════════════════════════════════════════════════════

async def _init_deliberation_vector_indexer(app, conn) -> None:
    """Deliberation + Intent + VectorRepo + Reranker + MemoryIndexerWorker。"""
    # ═══ Deliberation + Intent ═══
    from services.deliberation.deliberation_service import DeliberationService
    from services.intent_service import IntentService
    app.state.deliberation_service = DeliberationService(
        agent_service=app.state.agent_service, model_config=app.state.model_config,
    )
    app.state.intent_service = IntentService(
        app.state.agent_service, app.state.workflow_service, app.state.rules_service,
        deliberation_service=app.state.deliberation_service,
        model_config=app.state.model_config,
        adapters=app.state.adapters,
    )

    # ═══ Vector Repo（sqlite-vec 替代 chromadb）═══
    # 传入 db 连接，不再需要懒加载 import chromadb
    from repositories.vector_repo import VectorRepository
    app.state.vector_repo = VectorRepository(db=conn)
    await app.state.vector_repo.initialize()
    app.state.memory_service._vector_repo = app.state.vector_repo
    # Phase 47: MemoryTree 接入 chromadb 混合检索（语义+关键词）
    if hasattr(app.state, "memory_tree") and app.state.memory_tree:
        app.state.memory_tree._vector_repo = app.state.vector_repo

    # Task 6.14: BGE cross-encoder 重排器——懒加载模型，best-effort（未安装/下载失败时跳过）
    from services.memory.reranker import Reranker
    app.state.reranker = Reranker()
    app.state.memory_service.set_reranker(app.state.reranker)
    if hasattr(app.state, "memory_tree") and app.state.memory_tree:
        app.state.memory_tree.set_reranker(app.state.reranker)
    _log.info("Task 6.14: Reranker injected (model=%s, lazy-load on first use)",
              app.state.reranker._model_name)

    # Phase 49-H: 懒加载下乐观假设可用——首次 ensure_initialized() 失败时由 search/index 降级
    # （_search_collection / _flush_collection_batch 已有 if not collection 保护，不会崩溃）
    app.state.vector_storage_available = True

    # Task 4.4: RAG 索引一致性——MemoryIndexerWorker 兜底补索引（Outbox pattern）
    from services.memory_indexer_worker import MemoryIndexerWorker
    app.state.memory_indexer = MemoryIndexerWorker(
        db=conn, vector_repo=app.state.vector_repo,
    )
    await app.state.memory_indexer.start()


# ════════════════════════════════════════════════════════════
# Summarizer
# ════════════════════════════════════════════════════════════

def _init_summarizer(app) -> None:
    """LLM Summarizer 闭包——复用 aiohttp.ClientSession，注入 memory_service/memory_tree。"""
    # ═══ Summarizer（LLM 内联定义） ═══
    # Batch C M12: 复用 aiohttp.ClientSession——原每次调用新建 session，
    # 对标 aiohttp 官方建议 "don't create a session per request"。
    # session 存储在 app.state 上，shutdown 时由 lifespan 关闭。
    app.state._summarizer_session = None

    async def _get_summarizer_session() -> aiohttp.ClientSession:
        s = app.state._summarizer_session
        if s is None or s.closed:
            s = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
            app.state._summarizer_session = s
        return s

    async def _summarize(text: str) -> str:
        cfg = await app.state.model_config.get_active_config()
        api_key = cfg.get("api_key", "")
        # 端点与鉴权全部走厂商协议声明——这里不假设 Bearer，也不假设固定厂商默认模型
        from services.provider_protocol import auth_headers, chat_url, requires_api_key, resolve_protocol

        protocol = resolve_protocol(cfg)
        model = cfg.get("model", "")
        if not cfg.get("base_url") or not model or (not api_key and requires_api_key(protocol)):
            return ""
        try:
            s = await _get_summarizer_session()
            headers = auth_headers(protocol, api_key)
            headers.setdefault("Content-Type", "application/json")
            async with s.post(
                chat_url(cfg, protocol),
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "你是一个文本摘要助手。请简洁摘要以下内容，保留关键事实和决策，控制在 200 字以内。"},
                        {"role": "user", "content": text[:LIFESPAN_HEALTH_SUMMARY_INPUT_MAX]},
                    ],
                    "max_tokens": MAX_TOKENS_LIFESPAN_HEALTH,
                },
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    msg = data["choices"][0]["message"]
                    content = msg.get("content", "")
                    if not content:
                        content = msg.get("reasoning_content", "")
                    return content
                _log.warning("summarizer: LLM returned %d", resp.status)
                return ""
        except Exception as e:
            _log.warning("summarizer: request failed: %s", type(e).__name__)
            return ""

    app.state.memory_service._summarizer = _summarize
    app.state.memory_tree._summarizer = _summarize


# ════════════════════════════════════════════════════════════
# Loop 注册（health / cleanup / monitor / background / mcp）
# ════════════════════════════════════════════════════════════

async def _register_health_cleanup_loops(app, conn) -> None:
    """注册 healer / memory_tree / auto_dream / evolution / memory_sync / gc_agent / cleanup / approval_expire Loop。"""
    from services.loop_supervisor import LoopSpec, RestartStrategy
    from tools.builtin.system_tools import _cleanup_expired_states

    # Phase 45-C: EventBus 包装器（适配 supervisor 的 coro_fn 签名）
    async def _event_bus_wrapper(_state: dict):
        pass  # EventBus 有自己独立的 _consumer_task，不需要额外 tick

    # 注册 11 个核心定时 Loop
    # 迁移后旧 create_task 可逐步移除
    await app.state.supervisor.register(LoopSpec(
        "healer", lambda _s: app.state.agent_service._agent_healer(),
        interval=LOOP_INTERVAL_HEALER_SEC, priority=-1, strategy=RestartStrategy.PERMANENT))
    await app.state.supervisor.register(LoopSpec(
        "memory_tree", lambda _s: app.state.memory_tree.background_loop(),
        interval=LOOP_INTERVAL_MEMORY_TREE_SEC, priority=0, strategy=RestartStrategy.PERMANENT))
    await app.state.supervisor.register(LoopSpec(
        "auto_dream", lambda _s: app.state.memory_service.auto_dream(),
        interval=LOOP_INTERVAL_AUTO_DREAM_SEC, priority=0, strategy=RestartStrategy.PERMANENT))
    await app.state.supervisor.register(LoopSpec(
        "evolution", lambda _s: app.state.evolution_engine.evolution_loop(),
        interval=LOOP_INTERVAL_EVOLUTION_SEC, priority=-1, strategy=RestartStrategy.PERMANENT))
    await app.state.supervisor.register(LoopSpec(
        "memory_sync", lambda _s: app.state.memory_sync.background_loop(),
        interval=LOOP_INTERVAL_MEMORY_SYNC_SEC, priority=-1, strategy=RestartStrategy.PERMANENT))
    await app.state.supervisor.register(LoopSpec(
        "gc_agent", lambda _s: app.state.gc_agent.background_loop(),
        interval=LOOP_INTERVAL_GC_AGENT_SEC, priority=-2, strategy=RestartStrategy.TRANSIENT))
    await app.state.supervisor.register(LoopSpec(
        "cleanup", lambda _s: _periodic_cleanup(conn, _cleanup_expired_states),
        interval=LOOP_INTERVAL_CLEANUP_SEC, priority=-2, strategy=RestartStrategy.PERMANENT))
    # Phase 107: 审批过期标记——每分钟将过期 pending 记录标记为 expired（供 Inbox 展示历史）
    from tools.builtin.system_tool_confirm import _expire_pending_states
    async def _periodic_expire_pending(_state: dict):
        await _expire_pending_states(conn)
    await app.state.supervisor.register(LoopSpec(
        "approval_expire", lambda _s: _periodic_expire_pending(_s),
        interval=LOOP_INTERVAL_APPROVAL_EXPIRE_SEC, priority=-2, strategy=RestartStrategy.PERMANENT))
    # EventBus consumer — 事件驱动，非定时
    await app.state.supervisor.register(LoopSpec(
        "event_bus", lambda _s: _event_bus_wrapper(app.state.event_bus),
        interval=0, priority=-1, strategy=RestartStrategy.PERMANENT,
        loop_type="event"))


async def _register_monitor_loops(app) -> None:
    """注册 code_indexer / backend_health / ws_heartbeat / bridge_cleanup Loop。"""
    from services.loop_supervisor import LoopSpec, RestartStrategy

    # Phase 43 V.2: 代码索引定时刷新
    from services.code_indexer import CodeIndexer
    _code_indexer = CodeIndexer(os.environ.get("YUNCAT_WORKSPACE", ""))
    app.state.code_indexer = _code_indexer
    await app.state.supervisor.register(LoopSpec(
        "code_indexer", lambda _s: _code_indexer.build(),
        interval=1800, priority=-2, strategy=RestartStrategy.TRANSIENT))

    # Phase 43 L.1: OC Gateway + HA 健康监控
    async def _backend_health_monitor(state: dict):
        """每 60s 检查两个后端的健康状态 → 发布 EventBus 事件。"""
        from services.event_bus import Events
        for agent_id, adapter in app.state.adapters.items():
            try:
                alive = await adapter.health() if hasattr(adapter, "health") else False
                state[f"{agent_id}_alive"] = alive
                if hasattr(app.state, "event_bus"):
                    await app.state.event_bus.publish(
                        Events.BACKEND_HEALTH_CHECK,
                        {"agent_id": agent_id, "alive": alive, "timestamp": int(_time.time())},
                    )
            except Exception:
                # M8 修复：健康检查异常可能是适配器 bug 而非仅"不健康"——记录便于诊断
                _log.debug("backend_health_monitor: %s health check threw", agent_id, exc_info=True)
                state[f"{agent_id}_alive"] = False

    await app.state.supervisor.register(LoopSpec(
        "backend_health", lambda s: _backend_health_monitor(s),
        interval=60, priority=-1, strategy=RestartStrategy.PERMANENT))

    # WS 心跳——每 30s ping + 清理死连接
    await app.state.supervisor.register(LoopSpec(
        "ws_heartbeat", lambda _s: app.state.agent_service.ws_heartbeat_tick(),
        interval=30, priority=0, strategy=RestartStrategy.PERMANENT))

    # Phase 48 §22.2.7: Bridge 审批 TTL 清理——每 60s 扫描，删除 >300s 未处理条目
    await app.state.supervisor.register(LoopSpec(
        "bridge_cleanup", lambda _s: app.state.agent_service.cleanup_bridge_pending(),
        interval=60, priority=-2, strategy=RestartStrategy.PERMANENT))


async def _register_background_agent_loop(app, conn, background_tasks: list) -> None:
    """注册后台 Agent Loop——用户不在时继续执行 pending 任务。"""
    from services.loop_supervisor import LoopSpec, RestartStrategy

    # Phase 43 W.3: 后台 Agent Loop — 用户不在时继续执行 pending 任务
    async def _background_agent_loop(state: dict):
        """扫描 tasks 表中 status='pending' 的任务 → 后台执行 → 完成通知。

        Phase 56: 移除硬编码 LIMIT 3，改为可配置常量；启动时立即执行一次，
        避免 pending 任务最多等 60s 才开始恢复。
        """
        try:
            cursor = await conn.execute(
                "SELECT id, input, agent_id, session_id FROM tasks "
                "WHERE status='pending' ORDER BY created_at ASC LIMIT ?",
                (BACKGROUND_AGENT_MAX_PENDING,),
            )
            rows = await cursor.fetchall()
            for row in rows:
                task_id, prompt, agent_id, session_id = row[0], row[1], row[2] or DEFAULT_AGENT_ID, row[3]
                _log.info("Background agent: resuming task %s", task_id[:8])
                await app.state.agent_service.start_task(
                    agent_id, prompt, session_id, mode="agent",
                )
                # Phase OH.7: 通知用户后台任务已启动
                if hasattr(app.state, "notification_center"):
                    await app.state.notification_center.send_to_user(
                        agent_id, f"后台任务已启动: {prompt[:80]}...",
                        level="info", task_id=task_id,
                    )
        except Exception:
            _log.debug("Background agent loop skipped", exc_info=True)

    await app.state.supervisor.register(LoopSpec(
        "background_agent", lambda s: _background_agent_loop(s),
        interval=BACKGROUND_AGENT_INTERVAL_SECONDS, priority=-1, strategy=RestartStrategy.PERMANENT))
    # 启动后立即执行一次，避免首次 60s 空等
    background_tasks.append(asyncio.create_task(_background_agent_loop({})))
