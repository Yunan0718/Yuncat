"""services/approval_inbox.py —— Phase 107: 统一审批 Inbox 服务

聚合分散的审批入口（confirmable 工具确认、Bridge 审批等）到统一 Inbox。

数据源：`runtime_states` 表（不新建表），通过 status 字段区分：
- 'pending'  — 待审批
- 'resolved' — 已批准（_pop_pending 原子 UPDATE 标记）
- 'rejected' — 已拒绝
- 'expired'  — 已过期（_expire_pending_states 标记）

核心 API：
- `get_pending(project_id, task_id)` — 查询待审批项
- `get_history(project_id, limit)` — 查询已处理历史
- `resolve_approved(token)` — 批准并执行（包装 dispatch_confirm）
- `resolve_rejected(token, reason)` — 拒绝（不执行）
"""
import json as _json
import logging
import time as _time

_log = logging.getLogger("yuncat.approval_inbox")


class ApprovalInboxService:
    """Phase 107: 统一审批 Inbox——从 runtime_states 聚合审批项。"""

    def __init__(self, db):
        self.db = db

    async def get_pending(
        self, project_id: str | None = None, task_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """查询待审批项——status='pending' 且未过期。"""
        clauses = ["status='pending'", "expires_at > ?"]
        params: list = [_time.time()]
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        where = " WHERE " + " AND ".join(clauses)
        params.append(limit)
        cursor = await self.db.execute(
            "SELECT id, state_type, payload, created_at, expires_at, session_id, "
            "project_id, title, task_id "
            "FROM runtime_states" + where + " ORDER BY created_at DESC LIMIT ?",
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_history(
        self, project_id: str | None = None, limit: int = 50,
    ) -> list[dict]:
        """查询已处理历史——status IN ('resolved', 'rejected', 'expired')。"""
        clauses = ["status IN ('resolved', 'rejected', 'expired')"]
        params: list = []
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        where = " WHERE " + " AND ".join(clauses)
        params.append(limit)
        cursor = await self.db.execute(
            "SELECT id, state_type, payload, created_at, expires_at, session_id, "
            "project_id, status, resolved_at, resolution, title, task_id "
            "FROM runtime_states" + where + " ORDER BY resolved_at DESC LIMIT ?",
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def resolve_approved(
        self, token: str, remember: bool = False, tool_name: str = "",
    ) -> dict:
        """批准并执行——包装 dispatch_confirm（内部 _pop_pending 原子 UPDATE）。

        Args:
            token: 审批令牌。
            remember: 是否将本工具加入 session 级允许列表，后续同 session 调用跳过确认。
            tool_name: 工具名；若为空，从 state_type 推导（如 pending_write → write_file）。

        返回 handler 执行结果。若 token 已处理/过期/不存在，抛 ValueError。
        """
        from tools.builtin.system_tool_confirm import dispatch_confirm
        from services.tools.tool_service import resolve_confirmation
        try:
            result = await dispatch_confirm(self.db, token)
        except Exception as e:
            # 二阶段确认原子性兜底：handler 抛异常时 token 已被 _pop_pending 消费
            # （status='resolved'），但 future 未唤醒。这里唤醒并回传错误，避免 agent
            # 循环悬挂 30min；resolve_confirmation 对已消失的 future 是幂等 no-op。
            _log.warning("resolve_approved: dispatch_confirm failed for %s: %s", token[:8], e)
            await resolve_confirmation(token, {
                "success": False, "error": str(e), "action": "approved", "token": token,
            })
            raise
        # 记录执行结果摘要到 resolution 字段（便于 Inbox 展示）
        summary = self._summarize_result(result)
        await self.db.execute(
            "UPDATE runtime_states SET resolution=? WHERE id=? AND status='resolved'",
            (summary, token),
        )
        await self.db.commit()
        # A1 竞态修复：唤醒等待的 future——让 agent_tool_loop 立即收到执行结果。
        # 原代码只执行副作用不唤醒 Future，导致 approve 路径 await 到 30min 超时、
        # 把已成功的操作误判失败并可能重复执行。与 resolve_rejected 对齐。
        await resolve_confirmation(token, result)
        # 修复 1.6 (F-031): Inbox 批准路径也支持 "Always allow in this session"
        # standing grants: 除内存态 session 允许列表外，再持久化一条带 TTL 的授权，
        # 使 cron / 重复任务在进程重启后仍能免确认复用（对标 OpenClaw）。
        # 注：批量批准路径（resolve_approved_batch）保持 session 语义——批量场景下
        # 每项的项目归属不明，持久化带通配 project 的授权会过度放宽，故不落库。
        if remember and result.get("success"):
            item = await self.get_by_token(token)
            resolved_tool = tool_name
            if not resolved_tool:
                state_type = item.get("state_type") if item else ""
                resolved_tool = self._derive_tool_name_from_state(state_type)
            if resolved_tool:
                from services.tools.tool_service import add_session_allowed_tool
                add_session_allowed_tool(resolved_tool)
                await self._persist_standing_grant(
                    resolved_tool, (item or {}).get("project_id") or "",
                )
        return result

    async def _persist_standing_grant(self, tool_name: str, project_id: str) -> None:
        """持久化一条 standing grant——失败不影响审批结果（副作用已执行成功）。"""
        try:
            from services.standing_grants import grant
            res = await grant(
                self.db, tool_name, agent_id="", project_id=project_id,
                source="inbox_approval", reason="用户勾选始终允许",
            )
            if not res.get("ok"):
                _log.warning("Standing grant persist failed: %s", res.get("error"))
        except Exception:
            _log.warning("Standing grant persist error (non-fatal)", exc_info=True)

    async def resolve_rejected(self, token: str, reason: str = "") -> dict:
        """拒绝——UPDATE status='rejected'，不执行 handler。

        A1 竞态 3 修复：同步唤醒等待的 future，避免 agent_tool_loop 卡 30 分钟。
        原 code 仅更新 DB 不调 resolve_confirmation，HTTP /approvals/reject 路径
        的 agent 循环会一直 await future 到超时。

        返回 {success: False, action: 'rejected', reason: ...}。
        若 token 已处理（非 pending），返回 {success: False, error: 'already-resolved'}。
        """
        cursor = await self.db.execute(
            "UPDATE runtime_states SET status='rejected', resolved_at=?, resolution=? "
            "WHERE id=? AND status='pending' AND expires_at > ?",
            (_time.time(), reason or "user rejected", token, _time.time()),
        )
        await self.db.commit()
        if cursor.rowcount == 0:
            return {"success": False, "error": "already-resolved",
                    "action": "rejected", "token": token}
        # A1: 唤醒等待的 future——让 agent_tool_loop 立即收到拒绝结果
        from services.tools.tool_service import resolve_confirmation
        reject_result = {"success": False, "error": "用户拒绝执行",
                         "action": "rejected", "reason": reason or "user rejected"}
        await resolve_confirmation(token, reject_result)
        return {"success": False, "action": "rejected",
                "reason": reason or "user rejected", "token": token}

    async def resolve_approved_batch(
        self, tokens: list[str], remember: bool = False,
    ) -> list[dict]:
        """Batch 3.2: 批量批准并执行。

        对每个 token 原子 pop 后执行 handler；已处理/过期 token 返回
        already-resolved 但不阻断其他 token。remember=True 时，优先从 handler
        返回结果的 meta.tool_name 取工具名，fallback 到 state_type 映射，加入
        session 允许列表。
        """
        from tools.builtin.system_tool_confirm import _pop_pending, CONFIRM_HANDLERS
        from services.tools.tool_service import add_session_allowed_tool

        results: list[dict] = []
        for token in tokens:
            pending = await _pop_pending(self.db, token)
            if not pending:
                results.append({
                    "token": token, "success": False,
                    "error": "already-resolved", "action": "rejected",
                })
                continue
            state_type = pending["state_type"]
            handler = CONFIRM_HANDLERS.get(state_type)
            if not handler:
                results.append({
                    "token": token, "success": False,
                    "error": f"Unknown state_type: {state_type}",
                })
                continue
            try:
                result = await handler(self.db, pending["payload"])
            except Exception as e:
                results.append({"token": token, "success": False, "error": str(e)})
                continue
            summary = self._summarize_result(result)
            await self.db.execute(
                "UPDATE runtime_states SET resolution=? WHERE id=? AND status='resolved'",
                (summary, token),
            )
            await self.db.commit()
            if remember and isinstance(result, dict) and result.get("success"):
                tool = ""
                meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
                if meta:
                    tool = meta.get("tool_name", "")
                if not tool:
                    tool = self._derive_tool_name_from_state(state_type)
                if tool:
                    add_session_allowed_tool(tool)
            results.append({"token": token, "success": True, "result": result})
        return results

    async def resolve_rejected_batch(
        self, tokens: list[str], reason: str = "",
    ) -> list[dict]:
        """Batch 3.2: 批量拒绝。

        逐个 UPDATE runtime_states 为 rejected 并 resolve_confirmation；已处理 token
        返回 already-resolved 但不阻断其他 token。
        """
        from services.tools.tool_service import resolve_confirmation

        results: list[dict] = []
        reject_result = {
            "success": False, "error": "用户拒绝执行",
            "action": "rejected", "reason": reason or "user rejected",
        }
        for token in tokens:
            cursor = await self.db.execute(
                "UPDATE runtime_states SET status='rejected', resolved_at=?, resolution=? "
                "WHERE id=? AND status='pending' AND expires_at > ?",
                (_time.time(), reason or "user rejected", token, _time.time()),
            )
            await self.db.commit()
            if cursor.rowcount == 0:
                results.append({
                    "token": token, "success": False,
                    "error": "already-resolved", "action": "rejected",
                })
                continue
            await resolve_confirmation(token, reject_result)
            results.append({
                "token": token, "success": True,
                "action": "rejected", "reason": reason or "user rejected",
            })
        return results

    async def get_by_token(self, token: str) -> dict | None:
        """按 token 查询单条记录（含 status）——供前端 Inbox 详情查看。"""
        cursor = await self.db.execute(
            "SELECT id, state_type, payload, created_at, expires_at, session_id, "
            "project_id, status, resolved_at, resolution, title, task_id "
            "FROM runtime_states WHERE id=?",
            (token,),
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    # ============================================================
    # 内部辅助
    # ============================================================
    def _row_to_dict(self, row) -> dict:
        """将 aiosqlite.Row 转为 dict，安全解析 payload JSON。"""
        if hasattr(row, "keys"):
            d = {k: row[k] for k in row.keys()}
        else:
            cols = ["id", "state_type", "payload", "created_at", "expires_at",
                    "session_id", "project_id", "status", "resolved_at", "resolution",
                    "title", "task_id"]
            d = {c: row[i] for i, c in enumerate(cols)}
        # 解析 payload JSON
        raw = d.get("payload")
        if isinstance(raw, str):
            try:
                d["payload"] = _json.loads(raw)
            except Exception:
                pass  # 回退：解析失败保留原始字符串
        return d

    def _summarize_result(self, result: dict) -> str:
        """从 handler 执行结果生成摘要——用于 resolution 字段。"""
        if not isinstance(result, dict):
            return str(result)[:200]
        success = result.get("success", False)
        meta = result.get("meta", {}) or {}
        tool = meta.get("tool_name", "unknown")
        if success:
            data = result.get("data", {}) or {}
            return f"approved: {tool} executed — {str(data)[:150]}"
        err = result.get("error", "unknown error")
        return f"approved: {tool} failed — {str(err)[:150]}"

    def _derive_tool_name_from_state(self, state_type: str) -> str:
        """从 runtime_states 的 state_type 推导 tool_name（如 pending_write → write_file）。"""
        state_type_map = {
            "pending_write": "write_file",
            "pending_command": "run_command",
            "pending_delete_file": "delete_file",
            "pending_rename_file": "rename_file",
            "pending_delete_dir": "delete_dir",
            "pending_edit_file": "edit_file",
            "pending_workflow_trigger": "workflow_trigger",
        }
        return state_type_map.get(state_type, "")
