"""lifespan.py —— FastAPI lifespan 管理（薄编排层）

从 main.py 提取（原 ~370行 lifespan + ~70行 helper），M1 拆分。
create_lifespan 是唯一对外入口，服务初始化逻辑在 lifespan_services.py。
辅助函数在 lifespan_helpers.py / lifespan_event_handlers.py。
"""
import asyncio
import logging
import os
import sys
import time as _time

from contextlib import asynccontextmanager

# 6b-2 修复: 独立辅助函数 + EventBus 订阅已提取到独立模块（re-export 保持向后兼容）
# lifespan_helpers: _insert_builtin_templates / _periodic_cleanup / _PromptReloadHandler / _watch_prompts_with_watchdog
# lifespan_event_handlers: register_event_handlers (6 个 event handler 闭包)
from lifespan_helpers import (  # noqa: F401
    _emit_stage,
    _timed_stage,
    _insert_builtin_templates,
    _periodic_cleanup,
    _PromptReloadHandler,
    _watch_prompts_with_watchdog,
)
from lifespan_event_handlers import register_event_handlers  # noqa: F401

_log = logging.getLogger("yuncat.lifespan")


def create_lifespan(db_path: str, data_dir: str, token: str, background_tasks: list):
    """创建 lifespan 上下文管理器——工厂函数，注入路径配置。

    返回的 lifespan 与 FastAPI(app, lifespan=...) 直接兼容。
    所有服务初始化委托给内部 init_services()，保持编排层轻薄。
    """

    @asynccontextmanager
    async def lifespan(app):
        """初始化 DB → 服务注入 → 后台任务 → 等待请求 → 关闭"""
        _log.info("Starting Yuncat backend (data: %s)", data_dir)
        os.makedirs(data_dir, exist_ok=True)

        if not token:
            _log.error("YUNCAT_TOKEN not set! Electron should set this via spawnBackend env.")
            raise RuntimeError("YUNCAT_TOKEN not set")

        app.state.startup_stage = "init"
        # Phase 97: 记录启动各阶段时间戳——ready 时上报 analytics_events
        app.state._startup_t0 = _time.time()

        # Phase 47: 注入全局 context 单例——对标 Flask current_app / Django registry
        from context import set_app
        set_app(app)

        conn = None
        try:
            app.state.startup_stage = "db-init"
            app.state._startup_t_db_init = _time.time()
            # Phase 96: 应用待恢复的备份——若上次 session 执行了 stage_restore，
            # 此时 DB 未打开，用 staging 文件替换真实文件再初始化
            try:
                from services.backup_service import apply_pending_restore
                if apply_pending_restore(data_dir):
                    _log.info("Applied pending config restore — DB replaced with backup")
            except Exception as e:
                _log.warning("apply_pending_restore failed: %s", e)

            from repositories import init_db, run_migrations
            conn = await init_db(db_path)
            await run_migrations(conn)
            app.state.db = conn
            _log.info("Database initialized: %s", db_path)

            # Phase 47: 标记上次异常退出残留的 task → failed
            try:
                cursor = await conn.execute(
                    "UPDATE tasks SET status='failed', error_message='Backend restarted' "
                    "WHERE status IN ('running','pending')"
                )
                if cursor.rowcount > 0:
                    _log.info("Marked %d stale tasks as failed", cursor.rowcount)
                await conn.execute(
                    "UPDATE scheduled_tasks SET status='failed' "
                    "WHERE status='running'"
                )
                # 清理上次会话残留的通知文件
                import shutil as _sh, os as _os
                nd = _os.path.join(data_dir, "notifications")
                if _os.path.exists(nd):
                    _sh.rmtree(nd)
                    _os.makedirs(nd)
                await conn.commit()
            except Exception:
                _log.debug("Failed to mark stale tasks", exc_info=True)

            # Phase 47 收尾: 清理过期 / 已撤销的持久化授权（standing_grants），
            # 避免「始终允许」类授权无限堆积。保留窗口内（STANDING_GRANT_MAX_TTL_SEC）
            # 的记录仍可审计，之后由本步物理删除。失败不影响启动。
            try:
                from services.standing_grants import purge_expired
                n = await purge_expired(conn)
                if n:
                    _log.info("Purged %d expired/revoked standing grants", n)
            except Exception:
                _log.debug("purge_expired on startup failed", exc_info=True)

            # ── 服务注入图 ──
            await _init_services(app, conn, data_dir, background_tasks)

            # Sidecar 就绪信号（通过 stdout 与 Electron 父进程通信，不能用 logging）
            _port = os.environ.get("_YUNCAT_PORT", "")
            if _port:
                import json as _json
                _ready = _json.dumps({"port": int(_port)})
                sys.stdout.write(f"YUNCAT_READY {_ready}\n")
                sys.stdout.flush()

            yield
        finally:
            # ── G4: 优雅关闭——标记 running task 为 interrupted（可恢复） ──
            # 必须在 conn.commit() 之前、adapter.stop() 之前执行：
            #   1. adapter 仍存活 → adapter.cancel() 可送达
            #   2. DB 写入在 conn.commit() 持久化
            #   3. 下次启动 Phase 47 清理只匹配 running/pending → interrupted 任务存活
            #      用户可手动 resume（resume_task 已扩展接受 interrupted）
            # 对标 Cursor 断点续传 / Claude Code crash recovery
            if conn is not None and hasattr(app.state, "agent_service"):
                try:
                    agent_svc = app.state.agent_service
                    cursor = await conn.execute("SELECT id FROM tasks WHERE status='running'")
                    running_rows = await cursor.fetchall()
                    interrupted_count = 0
                    for row in running_rows:
                        try:
                            await agent_svc.interrupt_task(row[0])
                            interrupted_count += 1
                        except Exception:
                            _log.debug("G4: interrupt_task %s failed on shutdown", row[0], exc_info=True)
                    if interrupted_count > 0:
                        _log.info("G4: Interrupted %d running tasks on graceful shutdown", interrupted_count)
                except Exception:
                    _log.debug("G4 shutdown interrupt sweep failed", exc_info=True)
            # ── 优雅关闭 ──
            if hasattr(app.state, "memory_service"):
                # D1: 统一走 flush_pending()——含 commit 失败的 rollback + 计数器重置
                pending = getattr(app.state.memory_service, '_pending_writes', 0)
                if pending > 0:
                    try:
                        await app.state.memory_service.flush_pending()
                        _log.info("Flushed %d pending conversation writes on shutdown", pending)
                    except Exception as e:
                        _log.warning("flush_pending on shutdown failed: %s", e)
                # P0-3.3: 触发 SESSION_END hook——为活跃会话生成结构化摘要（跨 session 继承）
                # 必须在 adapter.stop() 前执行（LLM 调用需要 adapter 存活）
                try:
                    from hooks.registry import HookType, run_session_hooks
                    mem_svc = app.state.memory_service
                    active_sessions = list(getattr(mem_svc, '_sessions', {}).keys())
                    model_cfg = {}
                    try:
                        model_cfg = await app.state.model_config_service.get_active_config()
                    except Exception:
                        # M8 修复：关闭阶段配置获取失败不应阻塞 SESSION_END——记录后用空配置降级
                        _log.debug("model_config get_active_config failed on shutdown, using empty cfg", exc_info=True)
                    for sid in active_sessions:
                        try:
                            await run_session_hooks(
                                HookType.SESSION_END, sid,
                                {"memory_service": mem_svc, "model_config": model_cfg},
                            )
                        except Exception:
                            _log.debug("SESSION_END hook failed for %s", sid[:8], exc_info=True)
                    if active_sessions:
                        _log.info("Triggered SESSION_END for %d active sessions", len(active_sessions))
                except Exception:
                    _log.debug("SESSION_END trigger sweep failed", exc_info=True)
            # Batch 3b B3: 后半段关闭链每步独立 try/except——前一步异常不再跳过
            # 后续清理（对标 FastAPI 官方 shutdown 模式：逐容错、全执行）
            if hasattr(app.state, "analytics_service"):
                try:
                    await app.state.analytics_service.flush_all()
                except Exception:
                    _log.exception("analytics flush_all on shutdown failed")
            if conn is not None:
                try:
                    await conn.commit()
                except Exception:
                    _log.debug("final commit on shutdown failed", exc_info=True)
            # Phase 45: Supervisor 优先关闭——先 supervisor 后 background_tasks
            if hasattr(app.state, "supervisor"):
                try:
                    await app.state.supervisor.shutdown_all()
                except Exception:
                    _log.exception("supervisor shutdown_all on shutdown failed")
            # Phase: 关闭两个后端子进程
            for agent_id, adapter in (getattr(app.state, "adapters", None) or {}).items():
                try:
                    if hasattr(adapter, "stop"):
                        await adapter.stop()
                        _log.info("Adapter %s stopped", agent_id)
                except Exception:
                    _log.debug("Adapter %s stop failed", agent_id)
            # Task 4.2: CodeGraph 单例关闭——与 adapter 同级，在 background_tasks cancel 之前
            if getattr(app.state, "codegraph", None):
                try:
                    await app.state.codegraph.stop()
                    _log.info("CodeGraph stopped")
                except Exception:
                    _log.debug("CodeGraph stop failed", exc_info=True)
            # Task 4.4: MemoryIndexerWorker 关闭——在 background_tasks cancel 之前，
            # 保证 worker 内的 flush 能完成（避免进程退出时 buffer 里的索引丢失）
            if getattr(app.state, "memory_indexer", None):
                try:
                    await app.state.memory_indexer.stop()
                except Exception:
                    _log.debug("MemoryIndexerWorker stop failed", exc_info=True)
            for t in background_tasks:
                t.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*background_tasks, return_exceptions=True),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                _log.warning("Background task cleanup timed out after 10s")
            # P2-6 修复: file_watcher.stop() 必须在 event_bus.stop() 之前调用
            # 原代码从未调 file_watcher.stop()——observer 线程和 _flush_task 整个生命周期泄漏。
            # 在 event_bus.stop() 前调用保证 in-flight flush 能 publish 最后一波事件。
            if hasattr(app.state, "file_watcher") and app.state.file_watcher:
                try:
                    await app.state.file_watcher.stop()
                except Exception:
                    _log.debug("file_watcher stop failed", exc_info=True)
            # Batch C M12: 关闭 summarizer session
            _summarizer_sess = getattr(app.state, "_summarizer_session", None)
            if _summarizer_sess and not _summarizer_sess.closed:
                try:
                    await _summarizer_sess.close()
                except Exception:
                    _log.debug("summarizer session close failed", exc_info=True)
            if hasattr(app.state, "event_bus"):
                try:
                    await app.state.event_bus.stop()
                except Exception:
                    _log.exception("event_bus stop on shutdown failed")
            if conn is not None:
                # Batch 3b B3: conn.close() 放 finally 确保执行——即使过期状态
                # 清理失败，DB 连接也必须关闭（否则 WAL 文件锁残留阻塞下次启动）
                try:
                    from tools.builtin.system_tools import _cleanup_expired_states
                    await _cleanup_expired_states(conn)
                except Exception:
                    _log.debug("_cleanup_expired_states on shutdown failed", exc_info=True)
                finally:
                    try:
                        await conn.close()
                    except Exception:
                        _log.debug("db conn close on shutdown failed", exc_info=True)
                    _log.info("Backend stopped.")

    return lifespan


# ════════════════════════════════════════════════════════════
# 服务注入工厂（薄编排层——实际逻辑在 lifespan_services.py）
# ════════════════════════════════════════════════════════════

async def _init_services(app, conn, data_dir: str, background_tasks: list) -> None:
    """全量服务实例化 + 依赖注入——薄编排层，按顺序调用 lifespan_services 中的聚焦函数。

    警告：初始化顺序敏感——修改前确认依赖关系。
    background_tasks 由 create_lifespan 参数传入，init 和 shutdown 共享同一列表。
    """
    from lifespan_services import (
        _init_core_services,
        _init_arena_and_notifications,
        _init_adapters,
        _schedule_adapter_autowarm,
        _init_agent_service,
        _init_workflow_engine,
        _init_evolution_and_approval,
        _init_event_bus_and_supervisor,
        _init_deliberation_vector_indexer,
        _init_summarizer,
        _register_health_cleanup_loops,
        _register_monitor_loops,
        _register_background_agent_loop,
    )
    from lifespan_postinit import (
        _init_mcp_services,
        _register_spec_trigger,
        _register_scheduler_tick,
        _register_backup_scheduler,
        _start_supervisor_and_recover,
        _init_prompt_services,
        _init_long_task_agents,
        _init_build_review_spec,
        _inject_event_bus_and_recover,
        _init_task_supervisor_review,
        _init_security_hooks,
        _init_orchestrator_skills,
        _init_codegraph,
        _init_templates_yaml,
        _load_session_start_context,
        _schedule_session_start_context,
        _init_file_watcher,
        _report_startup_metrics,
    )

    # ═══ 核心服务 ═══
    # 注：本阶段耗时含「import main 的模块图」约 1.3s（实测）——细分标记用于区分。
    _emit_stage("core-services")
    await _init_core_services(app, conn)
    _emit_stage("core-services-done")
    await _init_arena_and_notifications(app, conn)

    # ═══ Adapter 注册 + 预热 ═══
    _emit_stage("adapters")
    _init_adapters(app, conn)
    _schedule_adapter_autowarm(app, background_tasks)

    # ═══ AgentService + Workflow ═══
    _emit_stage("agent-service")
    _init_agent_service(app, conn)
    _init_workflow_engine(app, conn)
    _init_evolution_and_approval(app, conn)

    # ═══ EventBus + LoopSupervisor ═══
    _emit_stage("event-bus")
    await _init_event_bus_and_supervisor(app, conn, background_tasks)

    # ═══ Deliberation + Vector + Indexer + Summarizer ═══
    _emit_stage("deliberation")
    await _init_deliberation_vector_indexer(app, conn)
    _init_summarizer(app)

    # ═══ Loop 注册 ═══
    _emit_stage("loops")
    await _register_health_cleanup_loops(app, conn)
    await _register_monitor_loops(app)
    await _register_background_agent_loop(app, conn, background_tasks)
    await _init_mcp_services(app)

    # ═══ Spec 触发器 + Cron 调度器 + 备份调度器 ═══
    _emit_stage("scheduler")
    _register_spec_trigger(app, conn)
    _register_scheduler_tick(app, conn)
    _register_backup_scheduler(app, conn, data_dir)

    # ═══ Supervisor 启动 + 崩溃恢复 ═══
    _emit_stage("supervisor")
    await _start_supervisor_and_recover(app, conn)

    # ═══ Prompt 服务 + 长任务 Agent ═══
    _emit_stage("services")
    _init_prompt_services(app, conn, background_tasks, data_dir)
    _init_long_task_agents(app, conn)
    _emit_stage("services-prompt-longtask")

    # ═══ 构建/审查/Spec + EventBus 注入 + 恢复 ═══
    _init_build_review_spec(app, conn)
    _emit_stage("services-build-review")
    await _inject_event_bus_and_recover(app, conn)
    _init_task_supervisor_review(app, conn, background_tasks)
    _emit_stage("services-eventbus-inject")

    # ═══ 扩展服务 ═══
    # Phase 49-H: 并行初始化三个独立服务（依赖已就绪，修改 app.state 不同属性，无共享可变状态竞争）
    # _init_security_hooks: 设置 snapshot_service/harness_recorder/hooks.registry/network_guard
    # _init_orchestrator_skills: 设置 worktree/terminal/orchestrator/skill_registry/engine.executors
    # _init_codegraph: 设置 codegraph 单例
    await asyncio.gather(
        _timed_stage("init-security-hooks", _init_security_hooks(app, conn, data_dir)),
        _timed_stage("init-orchestrator-skills", _init_orchestrator_skills(app, conn, data_dir)),
        _timed_stage("init-codegraph", _init_codegraph(app)),
    )
    _emit_stage("services-ext-gather")

    # ═══ 模板 + 会话上下文 + 文件监控 ═══
    await _timed_stage("init-templates-yaml", _init_templates_yaml(app, conn))
    # 移到后台：该值只被首个任务读取一次，启动期不再为它等待（实测省 1.4s）
    _schedule_session_start_context(app, background_tasks)
    await _timed_stage("init-file-watcher", _init_file_watcher(app))
    _emit_stage("services-tail")

    # ═══ 启动指标上报 ═══
    _emit_stage("ready")
    await _report_startup_metrics(app)
