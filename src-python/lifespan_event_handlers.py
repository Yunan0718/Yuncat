"""lifespan_event_handlers.py —— 从 lifespan.py 提取的 EventBus 订阅注册

6b-2 修复：将 _init_services 中的 6 个 event handler 闭包 + 订阅逻辑提取到本模块，
让 _init_services 聚焦于服务实例化，event handler 注册逻辑独立维护。

提取的 handler：
  - _on_task_notification: 任务完成/失败/取消 → NotificationCenter 通知
  - _on_long_task_phase: 长任务阶段切换 → NotificationCenter 通知
  - _on_tool_start / _on_tool_done: 工具调用开始/结束 → WS 转发
  - _on_tool_output_chunk: 工具实时输出 → WS 流式转发
  - _on_delegation_completed: 委托完成 → analytics_events 落盘

设计：register_event_handlers(app, conn) 函数内定义闭包并订阅，保持与原 _init_services
行为完全一致。闭包依赖 app.state，通过函数参数传入。
"""
import json as _json
import logging
import time as _time

_log = logging.getLogger("yuncat.lifespan")


async def register_event_handlers(app, conn) -> None:
    """注册所有 EventBus 订阅——从 lifespan._init_services 提取。

    Args:
        app: FastAPI app（需已初始化 event_bus, notification_center, agent_service）
        conn: aiosqlite 连接（用于 analytics_events 落盘）
    """
    from services.event_bus import Events

    # ═══ Phase 106: NotificationCenter handlers（任务生命周期 + 长任务阶段）═══
    if hasattr(app.state, "notification_center"):
        # 订阅任务生命周期事件，自动发送统一通知
        async def _on_task_notification(event_type: str, data: dict) -> None:
            task_id = data.get("task_id", "")
            agent_id = data.get("agent_id", "")
            error = data.get("error", "")
            if event_type == Events.TASK_COMPLETED:
                await app.state.notification_center.notify(
                    title="任务完成",
                    body=f"任务 {task_id[:8]} 已完成",
                    type="task_completed",
                    level="success",
                    task_id=task_id,
                    agent_id=agent_id,
                    link=f"/chat?task={task_id}",
                )
            elif event_type == Events.TASK_FAILED:
                await app.state.notification_center.notify(
                    title="任务失败",
                    body=error or f"任务 {task_id[:8]} 执行失败",
                    type="task_failed",
                    level="error",
                    task_id=task_id,
                    agent_id=agent_id,
                    link=f"/chat?task={task_id}",
                )
            elif event_type == Events.TASK_CANCELLED:
                await app.state.notification_center.notify(
                    title="任务已取消",
                    body=f"任务 {task_id[:8]} 已取消",
                    type="task_cancelled",
                    level="info",
                    task_id=task_id,
                    agent_id=agent_id,
                    link=f"/chat?task={task_id}",
                )

        app.state.event_bus.subscribe(Events.TASK_COMPLETED, _on_task_notification)
        app.state.event_bus.subscribe(Events.TASK_FAILED, _on_task_notification)
        app.state.event_bus.subscribe(Events.TASK_CANCELLED, _on_task_notification)

        # 长任务阶段切换通知
        async def _on_long_task_phase(event_type: str, data: dict) -> None:
            task_id = data.get("task_id", "")
            phase = data.get("phase", "")
            description = data.get("description", "")
            await app.state.notification_center.notify(
                title="长任务阶段切换",
                body=description or f"进入阶段 {phase}",
                type="long_task_phase",
                level="info",
                task_id=task_id,
                link=f"/chat?task={task_id}",
            )

        app.state.event_bus.subscribe(Events.LONG_TASK_PHASE, _on_long_task_phase)

    # ═══ Tool event handlers（tool_start / tool_done / tool_output_chunk → WS 转发）═══
    def _get_agent_service():
        return app.state.agent_service

    async def _on_tool_start(evt_type: str, data: dict):
        ag = _get_agent_service()
        task_id = data.get("task_id", "")
        if task_id and ag:
            await ag._safe_send(task_id, {
                "type": "tool_start", "task_id": task_id,
                "tool_name": data.get("tool_name", ""),
                "preview": data.get("arguments_preview", ""),
            })

    async def _on_tool_done(evt_type: str, data: dict):
        ag = _get_agent_service()
        task_id = data.get("task_id", "")
        if task_id and ag:
            await ag._safe_send(task_id, {
                "type": "tool_done", "task_id": task_id,
                "tool_name": data.get("tool_name", ""),
                "success": data.get("success", False),
            })
    app.state.event_bus.subscribe("tool_start", _on_tool_start)
    app.state.event_bus.subscribe("tool_done", _on_tool_done)

    # Phase 47: tool_output_chunk → 实时命令输出流
    async def _on_tool_output_chunk(evt_type: str, data: dict):
        ag = _get_agent_service()
        task_id = data.get("task_id", "")
        if task_id and ag:
            await ag._safe_send(task_id, {
                "type": "tool_output_chunk", "task_id": task_id,
                "tool_name": data.get("tool_name", "run_command"),
                "data": data.get("data", ""),
                "stream": data.get("stream", "stdout"),
            })
    app.state.event_bus.subscribe("tool_output_chunk", _on_tool_output_chunk)

    # ═══ Delegation handler（delegation_completed / CROSS_BACKEND_DELEGATION → analytics_events）═══
    # Phase 38: delegation_completed 事件 → analytics_events
    # Phase 43 E.1: 同时订阅新的 CROSS_BACKEND_DELEGATION 融合事件
    async def _on_delegation_completed(evt_type: str, data: dict):
        try:
            await conn.execute(
                "INSERT INTO analytics_events(event_type, event_data, created_at) "
                "VALUES(?,?,?)",
                (evt_type, _json.dumps(data), int(_time.time())),
            )
            await conn.commit()
        except Exception:
            _log.debug("delegation_completed logging failed", exc_info=True)
    app.state.event_bus.subscribe("delegation_completed", _on_delegation_completed)
    app.state.event_bus.subscribe(Events.CROSS_BACKEND_DELEGATION, _on_delegation_completed)

    # ═══ Batch 5.4: file_changed → CodeIndexer 增量更新 ═══
    async def _on_file_changed_for_indexer(evt_type: str, data: dict):
        indexer = getattr(app.state, "code_indexer", None)
        if not indexer:
            return
        paths = data.get("paths", [])
        py_paths = [p for p in paths if p.endswith(".py")]
        if not py_paths:
            return
        try:
            indexer.incremental_update(py_paths)
        except Exception:
            _log.debug("CodeIndexer incremental update failed", exc_info=True)
    app.state.event_bus.subscribe("file_changed", _on_file_changed_for_indexer)
