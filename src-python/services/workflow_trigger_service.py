"""services/workflow_trigger_service.py —— Phase 101: 工作流事件触发器

三种触发类型:
  - event: 订阅 EventBus（task_completed / file_changed / session_started），
           匹配 config.event + config.filter 后自动执行工作流
  - cron:  由 LoopSupervisor timer 定期扫描 next_run_at，到期则执行，
           支持 5 字段 cron 表达式（复用 services/cron_parser.py）
  - manual: 仅通过 API 手动触发

审批拦截:
  config.require_approval=True 时，fire_trigger 不直接执行工作流，
  而是创建 runtime_states pending 令牌（Phase 107），用户在审批 Inbox
  批准后才由 dispatch_confirm → _handle_workflow_trigger 实际执行。
"""
import json
import logging
import time as _time
import uuid as _uuid

from typing import Any

_log = logging.getLogger("yuncat.workflow_trigger")


class WorkflowTriggerService:
    """Phase 101: 工作流触发器服务——event / cron / manual 三种触发类型。"""

    def __init__(self, db, workflow_service=None):
        self.db = db
        self.workflow_service = workflow_service

    # ============================================================
    # CRUD
    # ============================================================

    async def create_trigger(
        self, workflow_id: str, trigger_type: str,
        config: dict | None = None, enabled: bool = True,
    ) -> str:
        """创建触发器。

        config 结构因 trigger_type 而异:
          - event: {"event": "task_completed", "filter": {"agent_id": "hermes"},
                    "initial_input": "", "require_approval": false}
          - cron:  {"cron_expr": "0 9 * * *", "initial_input": "", "require_approval": false}
          - manual: {"initial_input": "", "require_approval": false}
        """
        trigger_id = str(_uuid.uuid4())
        config_json = json.dumps(config or {})
        next_run_at = None
        if trigger_type == "cron":
            from services.cron_parser import next_run_at as compute_next
            cron_expr = (config or {}).get("cron_expr", "")
            if cron_expr:
                next_run_at = compute_next(cron_expr)
        await self.db.execute(
            "INSERT INTO workflow_triggers("
            "id, workflow_id, trigger_type, config, enabled, next_run_at"
            ") VALUES(?,?,?,?,?,?)",
            (trigger_id, workflow_id, trigger_type, config_json,
             1 if enabled else 0, next_run_at),
        )
        await self.db.commit()
        return trigger_id

    async def list_triggers(self, workflow_id: str) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT id, workflow_id, trigger_type, config, enabled, "
            "last_run_at, next_run_at, run_count, created_at "
            "FROM workflow_triggers WHERE workflow_id=? ORDER BY created_at DESC",
            (workflow_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def delete_trigger(self, trigger_id: str) -> bool:
        cursor = await self.db.execute(
            "DELETE FROM workflow_triggers WHERE id=?", (trigger_id,)
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def get_trigger(self, trigger_id: str) -> dict | None:
        cursor = await self.db.execute(
            "SELECT id, workflow_id, trigger_type, config, enabled, "
            "last_run_at, next_run_at, run_count, created_at "
            "FROM workflow_triggers WHERE id=?",
            (trigger_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    # ============================================================
    # 事件匹配
    # ============================================================

    async def get_enabled_event_triggers(self) -> list[dict]:
        """获取所有 enabled 的 event 类型触发器。"""
        cursor = await self.db.execute(
            "SELECT id, workflow_id, trigger_type, config, enabled, "
            "last_run_at, next_run_at, run_count, created_at "
            "FROM workflow_triggers WHERE trigger_type='event' AND enabled=1"
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    def match_event(self, trigger_config: dict, event_type: str,
                    event_data: dict | None) -> bool:
        """检查触发器 config 是否匹配当前事件。

        config.event 必须等于 event_type；
        config.filter 中的键值对必须在 event_data 中匹配（子集匹配）。
        """
        cfg_event = trigger_config.get("event", "")
        if cfg_event != event_type:
            return False
        filter_spec = trigger_config.get("filter", {})
        if not filter_spec:
            return True
        data = event_data or {}
        for key, expected in filter_spec.items():
            actual = data.get(key)
            if isinstance(expected, str) and isinstance(actual, str):
                if expected not in actual:
                    return False
            elif actual != expected:
                return False
        return True

    # ============================================================
    # Cron 调度
    # ============================================================

    async def get_due_cron_triggers(self, now: int | None = None) -> list[dict]:
        """获取所有到期的 cron 触发器（只读，不原子认领）。

        注意：此方法保留用于查询/展示。cron_tick 应使用 claim_due_cron_triggers
        以避免 TOCTOU 双触发（A3）。
        """
        now = now or int(_time.time())
        cursor = await self.db.execute(
            "SELECT id, workflow_id, trigger_type, config, enabled, "
            "last_run_at, next_run_at, run_count, created_at "
            "FROM workflow_triggers "
            "WHERE trigger_type='cron' AND enabled=1 "
            "AND next_run_at IS NOT NULL AND next_run_at <= ?",
            (now,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def claim_due_cron_triggers(self, now: int | None = None, limit: int = 10) -> list[dict]:
        """A3: 原子认领到期的 cron 触发器——修复 cron_tick TOCTOU 双触发。

        原子操作：UPDATE ... SET next_run_at=NULL, last_run_at=now
        WHERE id IN (SELECT ... WHERE next_run_at<=now AND enabled=1) RETURNING *
        认领后 next_run_at=NULL，下个 tick（30s 后）不会重复选中同一 trigger。
        fire 后由 _update_after_cron_fire 重算并恢复 next_run_at。

        对标 scheduled_tasks_repo.claim_due_scheduled_tasks 的 UPDATE...RETURNING 模式。
        """
        now = now or int(_time.time())
        cursor = await self.db.execute(
            "UPDATE workflow_triggers SET next_run_at=NULL, last_run_at=? "
            "WHERE id IN ("
            "  SELECT id FROM workflow_triggers "
            "  WHERE trigger_type='cron' AND enabled=1 "
            "  AND next_run_at IS NOT NULL AND next_run_at <= ? "
            "  ORDER BY next_run_at ASC LIMIT ?"
            ") RETURNING id, workflow_id, trigger_type, config, enabled, "
            "last_run_at, next_run_at, run_count, created_at",
            (now, now, limit),
        )
        rows = await cursor.fetchall()
        await self.db.commit()
        return [self._row_to_dict(r) for r in rows]

    async def _update_after_cron_fire(self, trigger_id: str, cron_expr: str) -> None:
        """cron 触发器执行后更新 next_run_at + last_run_at。

        run_count 在 fire_trigger 调用 _execute_workflow 时已递增，此处不再重复。
        """
        from services.cron_parser import next_run_at as compute_next
        now = int(_time.time())
        nxt = compute_next(cron_expr, after=now)
        await self.db.execute(
            "UPDATE workflow_triggers SET last_run_at=?, next_run_at=? WHERE id=?",
            (now, nxt, trigger_id),
        )
        await self.db.commit()

    # ============================================================
    # 触发执行
    # ============================================================

    async def fire_trigger(
        self, trigger_id: str,
        initial_input: str = "",
        event_data: dict | None = None,
    ) -> dict:
        """触发工作流执行。

        - config.require_approval=True → 创建 pending_state 令牌，返回 pending_approval
        - 否则直接调用 workflow_service.run()，返回 run_id
        """
        trigger = await self.get_trigger(trigger_id)
        if not trigger:
            return {"success": False, "error": "trigger-not-found"}
        if not trigger["enabled"]:
            return {"success": False, "error": "trigger-disabled"}

        config = trigger.get("config", {})
        # initial_input 优先级：参数 > config.initial_input > event_data 序列化
        input_text = initial_input or config.get("initial_input", "")
        if not input_text and event_data:
            input_text = json.dumps(event_data, ensure_ascii=False)

        workflow_id = trigger["workflow_id"]

        # 审批拦截
        if config.get("require_approval", False):
            return await self._create_approval(trigger, input_text)

        # 直接执行
        return await self._execute_workflow(workflow_id, input_text, trigger_id)

    async def _execute_workflow(
        self, workflow_id: str, input_text: str, trigger_id: str = "",
    ) -> dict:
        """直接执行工作流。"""
        if not self.workflow_service:
            return {"success": False, "error": "workflow-service-not-available"}
        try:
            run_id = await self.workflow_service.run(workflow_id, input_text)
            if trigger_id:
                await self._increment_run_count(trigger_id)
            return {"success": True, "run_id": run_id, "trigger_id": trigger_id}
        except Exception as e:
            _log.exception("Trigger %s failed to execute workflow %s", trigger_id, workflow_id)
            return {"success": False, "error": str(e), "trigger_id": trigger_id}

    async def _create_approval(self, trigger: dict, input_text: str) -> dict:
        """创建审批令牌——用户在 Inbox 批准后由 dispatch_confirm 实际执行。"""
        from tools.builtin.system_tool_confirm import create_pending_state
        workflow_id = trigger["workflow_id"]
        trigger_id = trigger["id"]
        payload = {
            "workflow_id": workflow_id,
            "initial_input": input_text,
            "trigger_id": trigger_id,
        }
        title = f"工作流触发审批: {trigger.get('trigger_type', 'manual')}"
        token = await create_pending_state(
            self.db,
            state_type="pending_workflow_trigger",
            payload=payload,
            title=title,
            task_id=trigger_id,
        )
        return {
            "success": True,
            "status": "pending_approval",
            "token": token,
            "trigger_id": trigger_id,
            "workflow_id": workflow_id,
        }

    async def _increment_run_count(self, trigger_id: str) -> None:
        await self.db.execute(
            "UPDATE workflow_triggers SET run_count=run_count+1, last_run_at=? "
            "WHERE id=?",
            (int(_time.time()), trigger_id),
        )
        await self.db.commit()

    # ============================================================
    # EventBus 事件处理
    # ============================================================

    async def handle_event(self, event_type: str, event_data: dict) -> None:
        """EventBus 订阅回调——匹配所有 event 触发器并执行。"""
        try:
            triggers = await self.get_enabled_event_triggers()
            for trigger in triggers:
                config = trigger.get("config", {})
                if self.match_event(config, event_type, event_data):
                    input_text = config.get("initial_input", "")
                    if not input_text and event_data:
                        input_text = json.dumps(event_data, ensure_ascii=False)
                    _log.info(
                        "Event %s matched trigger %s for workflow %s",
                        event_type, trigger["id"], trigger["workflow_id"],
                    )
                    await self.fire_trigger(trigger["id"], input_text, event_data)
        except Exception:
            _log.exception("Error handling event %s in trigger service", event_type)

    async def cron_tick(self, now: int | None = None) -> int:
        """Cron 触发器定期扫描——由 LoopSupervisor 调用。返回触发的数量。

        A3: 使用 claim_due_cron_triggers 原子认领，避免工作流执行 >30s 时
        下个 tick 重复触发同一 trigger（TOCTOU 双触发修复）。
        """
        try:
            due = await self.claim_due_cron_triggers(now=now)
            for trigger in due:
                config = trigger.get("config", {})
                cron_expr = config.get("cron_expr", "")
                input_text = config.get("initial_input", "")
                _log.info(
                    "Cron trigger %s fired for workflow %s",
                    trigger["id"], trigger["workflow_id"],
                )
                try:
                    await self.fire_trigger(trigger["id"], input_text)
                except Exception:
                    _log.exception("Cron trigger %s fire failed", trigger["id"])
                # 无论 fire 成功/失败都恢复 next_run_at，否则触发器 next_run_at 停留 NULL 静默停摆；
                # _update_after_cron_fire 自身异常也不得中断后续 trigger 的处理。
                if cron_expr:
                    try:
                        await self._update_after_cron_fire(trigger["id"], cron_expr)
                    except Exception:
                        _log.exception("Failed to reschedule cron trigger %s", trigger["id"])
            return len(due)
        except Exception:
            _log.exception("Error in cron trigger tick")
            return 0

    # ============================================================
    # 工具
    # ============================================================

    @staticmethod
    def _row_to_dict(row) -> dict:
        if hasattr(row, "keys"):
            d = {k: row[k] for k in row.keys()}
        else:
            cols = ["id", "workflow_id", "trigger_type", "config", "enabled",
                    "last_run_at", "next_run_at", "run_count", "created_at"]
            d = {c: row[i] for i, c in enumerate(cols)}
        d["enabled"] = bool(d.get("enabled"))
        cfg = d.get("config", "{}")
        d["config"] = json.loads(cfg) if isinstance(cfg, str) else (cfg or {})
        return d
