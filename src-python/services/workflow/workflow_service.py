"""services/workflow_service.py —— 工作流服务（薄封装 DAG 引擎 + Repository 接口）

为 WorkflowEngine 提供 get_workflow / update_snapshot / get_snapshot 三个 repo 方法。
"""
import asyncio
import json
import logging
import time as _time
import uuid as _uuid

from hooks.registry import run_notification_hooks

_log = logging.getLogger("yuncat.workflow")


class WorkflowService:
    def __init__(self, db, agent_service=None, tool_service=None, notification_center=None):
        self.db = db
        self.agent_service = agent_service
        self.tool_service = tool_service
        self.notification_center = notification_center
        self.engine = None  # Phase 1 由 main.py lifespan 注入

    async def _notify(self, run_id: str, workflow_id: str, status: str,
                      context: dict | None = None) -> None:
        """Phase D W.2: 触发工作流生命周期通知 hook。"""
        try:
            await run_notification_hooks(run_id, workflow_id, status, context)
        except Exception:
            _log.debug("workflow notification hook failed", exc_info=True)

    # ============================================================
    # Workflow CRUD
    # ============================================================
    async def create(self, payload: dict) -> str:
        wid = payload.get("id") or str(_uuid.uuid4())
        graph = payload.get("graph", {})
        # 防御：前端可能传 JSON 字符串或已解析的对象
        if isinstance(graph, str):
            graph = json.loads(graph)
        # Phase 99: 作用域与分类（缺省值由 DB DEFAULT 兜底，这里仅传非空值）
        await self.db.execute(
            "INSERT INTO workflows(id, name, graph, description, scope, project_id, category, owner_id) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (wid, payload.get("name", "untitled"), json.dumps(graph),
             payload.get("description", "") or "",
             payload.get("scope", "global"),
             payload.get("project_id"),
             payload.get("category", "custom"),
             payload.get("owner_id")),
        )
        await self.db.commit()
        return wid

    async def list_all(
        self, scope: str | None = None, project_id: str | None = None,
        category: str | None = None, limit: int = 100, offset: int = 0,
    ) -> list[dict]:
        """Phase 99: 支持按 scope/project_id/category 过滤。

        - scope='project' 时通常需配合 project_id 过滤具体项目
        - scope='global' 时 project_id 通常为 NULL
        - category 独立过滤
        - Batch 7.10: 支持 limit/offset 分页
        """
        clauses: list[str] = []
        params: list = []
        if scope:
            clauses.append("scope=?")
            params.append(scope)
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        if category:
            clauses.append("category=?")
            params.append(category)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, limit)
        offset = max(0, offset)
        cursor = await self.db.execute(
            "SELECT id, name, description, is_template, created_at, scope, project_id, category, owner_id "
            "FROM workflows" + where + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = await cursor.fetchall()
        return [{"id": r[0], "name": r[1], "description": r[2] or r[1],
                 "display_name": r[2] or r[1], "is_template": r[3], "created_at": r[4],
                 "scope": r[5] or "global", "project_id": r[6],
                 "category": r[7] or "custom", "owner_id": r[8]}
                for r in rows]

    async def get(self, workflow_id: str) -> dict | None:
        cursor = await self.db.execute(
            "SELECT id, name, graph, is_template, description, scope, project_id, category, owner_id "
            "FROM workflows WHERE id=?", (workflow_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        graph = json.loads(row[2]) if isinstance(row[2], str) else row[2]
        return {
            "id": row[0], "name": row[1], "graph": graph, "is_template": bool(row[3]),
            "nodes": graph.get("nodes", []), "edges": graph.get("edges", []),
            "description": row[4] or "",
            "scope": row[5] or "global", "project_id": row[6],
            "category": row[7] or "custom", "owner_id": row[8],
        }

    async def delete(self, workflow_id: str) -> None:
        await self.db.execute("DELETE FROM workflows WHERE id=?", (workflow_id,))
        await self.db.commit()

    # ============================================================
    # WorkflowEngine Repository 接口（引擎必需的三个方法）
    # ============================================================
    async def get_workflow(self, workflow_id: str) -> dict:
        """WorkflowEngine 接口别名——返回 {nodes, edges} dict"""
        wf = await self.get(workflow_id)
        if not wf:
            raise ValueError(f"Workflow {workflow_id} not found")
        return wf

    async def update_snapshot(self, run_id: str, snapshot_json: str) -> None:
        """写入 checkpoint 快照到 workflow_runs.snapshot"""
        await self.db.execute(
            "UPDATE workflow_runs SET snapshot=? WHERE id=?", (snapshot_json, run_id)
        )
        await self.db.commit()

    async def update_pause_state(
        self, run_id: str, context_json: str,
        current_node_id: str | None, snapshot_json: str, status: str,
    ) -> None:
        """Phase 51-E: 暂停时持久化上下文、当前节点、快照和状态。"""
        await self.db.execute(
            "UPDATE workflow_runs SET context=?, current_node_id=?, snapshot=?, "
            "status=?, updated_at=? WHERE id=?",
            (context_json, current_node_id, snapshot_json, status,
             int(_time.time()), run_id),
        )
        await self.db.commit()

    async def get_snapshot(self, run_id: str) -> str | None:
        """读取最近快照"""
        cursor = await self.db.execute(
            "SELECT snapshot FROM workflow_runs WHERE id=?", (run_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    # v56: events_log 读写（供 EventLogger 使用）
    async def get_events_log(self, run_id: str) -> str | None:
        cursor = await self.db.execute(
            "SELECT events_log FROM workflow_runs WHERE id=?", (run_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    async def set_events_log(self, run_id: str, events_json: str) -> None:
        await self.db.execute(
            "UPDATE workflow_runs SET events_log=? WHERE id=?", (events_json, run_id)
        )
        await self.db.commit()

    # ============================================================
    # Workflow 运行
    # ============================================================
    async def run(self, workflow_id: str, initial_input: str) -> str:
        """启动工作流——插入 workflow_runs 行后异步执行"""
        if not self.engine:
            return "engine-not-initialized"
        wf = await self.get(workflow_id)
        if not wf:
            raise ValueError(f"Workflow {workflow_id} not found")
        run_id = str(_uuid.uuid4())
        # 插入 workflow_runs 行——引擎通过 update_snapshot 更新
        await self.db.execute(
            "INSERT INTO workflow_runs(id, workflow_id, status, started_at) VALUES(?,?,?,?)",
            (run_id, workflow_id, "running", int(_time.time())),
        )
        await self.db.commit()
        # Phase D W.2: 触发启动通知
        await self._notify(run_id, workflow_id, "started")
        # 异步执行——引擎完成后更新状态
        asyncio.create_task(self._run_and_finalize(run_id, workflow_id, initial_input))
        return run_id

    async def _run_and_finalize(self, run_id: str, workflow_id: str, initial_input: str) -> None:
        """后台执行工作流并在完成后更新状态。
        Phase 33: 运行结果自动写入 MemoryTree 知识沉淀。
        Phase 51-E: 支持暂停状态——暂停时不写入 MemoryTree。
        """
        try:
            result = await self.engine.run(workflow_id, initial_input, run_id=run_id)
            status = result.get("status", "partial_error")
            if status not in ("completed", "partial_error", "paused"):
                status = "partial_error"
        except Exception as e:
            _log.exception(f"Workflow run {run_id} failed")
            status = "failed"
            result = {"error": str(e)}

        await self.db.execute(
            "UPDATE workflow_runs SET status=?, node_results=?, finished_at=?, updated_at=? WHERE id=?",
            (status, json.dumps(result), int(_time.time()), int(_time.time()), run_id),
        )
        await self.db.commit()

        # Phase D W.2: 触发完成/失败/暂停通知
        notify_status = "completed" if status in ("completed", "partial_error") else status
        await self._notify(
            run_id, workflow_id, notify_status,
            {"error": result.get("error", "")} if status == "failed" else {},
        )

        # P4: 工作流运行结果自动写入 MemoryTree（暂停时不写入）
        if status in ("completed", "partial_error"):
            await self._write_workflow_result_to_memory_tree(workflow_id, result)

    async def _write_workflow_result_to_memory_tree(self, workflow_id: str, result: dict) -> None:
        """将工作流运行结果沉淀到 MemoryTree。"""
        try:
            wf = await self.get(workflow_id)
            wf_name = wf.get("name", workflow_id) if wf else workflow_id
            summary = f"工作流 '{wf_name}' 执行完成 ({result.get('status', 'unknown')})。"
            node_outputs = result.get("results", {}) if isinstance(result, dict) else {}
            for nid, nr in node_outputs.items():
                out = nr.get("output", "") if isinstance(nr, dict) else str(nr)
                if out:
                    summary += f" 节点 {nid}: {out[:200]}"
            import uuid as _uuid2
            await self.db.execute(
                "INSERT OR REPLACE INTO knowledge_nodes(id, title, content, source, score, tags, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (str(_uuid2.uuid4()), f"工作流结果: {wf_name}",
                 summary[:2000], "workflow_run", 0.7, "[]",
                 int(_time.time()), int(_time.time())),
            )
            await self.db.commit()
        except Exception:
            _log.exception("Failed to write workflow result to MemoryTree")

    async def pause_run(self, run_id: str) -> dict:
        """Phase 51-E: 请求暂停正在运行的工作流。"""
        if not self.engine:
            return {"status": "error", "error": "engine-not-initialized"}
        run = await self.get_run(run_id)
        if not run:
            raise ValueError(f"Workflow run {run_id} not found")
        if run.get("status") != "running":
            return {"status": "ignored", "run_id": run_id,
                    "message": f"Run status is {run.get('status')}, not running"}
        self.engine.pause(run_id)
        # Phase D W.2: 触发暂停通知
        await self._notify(run_id, run.get("workflow_id", ""), "paused")
        return {"status": "ok", "run_id": run_id, "message": "Pause requested"}

    async def resume_run(self, run_id: str) -> str:
        """Phase 51-E: 从暂停状态恢复工作流运行。

        D5: 原子 CAS——UPDATE 带 AND status='paused' 守卫，避免 read-check-write
        间隙的竞态（两个并发 resume 都通过 Python 检查后双执行）。
        affected_rows=0 时抛 ValueError，不 spawn 后台任务、不发通知。
        """
        if not self.engine:
            return "engine-not-initialized"
        run = await self.get_run(run_id)
        if not run:
            raise ValueError(f"Workflow run {run_id} not found")
        if run.get("status") != "paused":
            raise ValueError(f"Run {run_id} is not paused (status={run.get('status')})")
        workflow_id = run["workflow_id"]
        # D5: 原子 CAS——仅当 status 仍为 paused 时才转移为 running
        cursor = await self.db.execute(
            "UPDATE workflow_runs SET status='running', finished_at=NULL, updated_at=? "
            "WHERE id=? AND status='paused'",
            (int(_time.time()), run_id),
        )
        await self.db.commit()
        if cursor.rowcount == 0:
            # 竞态：read 到 paused 后、UPDATE 前状态已被其他进程改变
            raise ValueError(
                f"Run {run_id} status changed during resume (no longer paused)"
            )
        # Phase D W.2: 触发恢复通知
        await self._notify(run_id, workflow_id, "resumed")
        asyncio.create_task(self._resume_and_finalize(run_id, workflow_id))
        return run_id

    async def _resume_and_finalize(self, run_id: str, workflow_id: str) -> None:
        """后台恢复并执行工作流。"""
        try:
            result = await self.engine.resume(run_id)
            status = result.get("status", "partial_error")
            if status not in ("completed", "partial_error", "paused"):
                status = "partial_error"
        except Exception as e:
            _log.exception(f"Workflow run {run_id} resume failed")
            status = "failed"
            result = {"error": str(e)}

        await self.db.execute(
            "UPDATE workflow_runs SET status=?, node_results=?, finished_at=?, updated_at=? WHERE id=?",
            (status, json.dumps(result), int(_time.time()), int(_time.time()), run_id),
        )
        await self.db.commit()

        # Phase D W.2: 触发恢复后完成/失败通知
        notify_status = "completed" if status in ("completed", "partial_error") else status
        await self._notify(
            run_id, workflow_id, notify_status,
            {"error": result.get("error", "")} if status == "failed" else {},
        )

        if status in ("completed", "partial_error"):
            await self._write_workflow_result_to_memory_tree(workflow_id, result)

    async def get_run(self, run_id: str) -> dict | None:
        cursor = await self.db.execute(
            "SELECT id, workflow_id, status, current_node_id, context, snapshot, "
            "node_results, error_message, parent_run_id, parent_node_id, "
            "started_at, finished_at, created_at, updated_at "
            "FROM workflow_runs WHERE id=?", (run_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "workflow_id": row[1], "status": row[2],
            "current_node_id": row[3], "context": row[4],
            "snapshot": row[5], "node_results": row[6], "error_message": row[7],
            "parent_run_id": row[8], "parent_node_id": row[9],
            "started_at": row[10], "finished_at": row[11],
            "created_at": row[12], "updated_at": row[13],
        }

    async def insert_child_run(
        self, run_id: str, workflow_id: str,
        parent_run_id: str, parent_node_id: str,
    ) -> None:
        """Phase 53: 为子工作流节点创建运行记录。"""
        await self.db.execute(
            "INSERT OR IGNORE INTO workflow_runs("
            "id, workflow_id, status, parent_run_id, parent_node_id, started_at"
            ") VALUES(?,?,?,?,?,?)",
            (run_id, workflow_id, "running",
             parent_run_id, parent_node_id, int(_time.time())),
        )
        await self.db.commit()

    async def list_templates(self) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT id, name, description, graph FROM workflows WHERE is_template=1"
        )
        rows = await cursor.fetchall()
        return [
            {"id": r[0], "name": r[1],
             "description": r[2] or "",
             "display_name": r[2] or r[1],
             "graph": json.loads(r[3]) if isinstance(r[3], str) else r[3]}
            for r in rows
        ]

    async def run_template(self, template_name: str, initial_input: str) -> str:
        """按模板名称运行工作流"""
        cursor = await self.db.execute(
            "SELECT id FROM workflows WHERE name=? AND is_template=1", (template_name,)
        )
        row = await cursor.fetchone()
        if not row:
            return f"template-not-found: {template_name}"
        return await self.run(row[0], initial_input)
