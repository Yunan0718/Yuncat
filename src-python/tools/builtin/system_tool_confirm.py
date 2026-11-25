"""system_tool_confirm.py —— 二阶段确认机制"""
import asyncio
import json as _json
import secrets
import time as _time
from pathlib import Path

from .system_tool_file_ops import _handle_delete, check_file_conflict
from .system_tool_command import _execute_command
from config import ERROR_PREVIEW_CHARS, AGENT_TOOL_HELPERS_ERROR_PREVIEW_CHARS
from config import CONFIRM_AUDIT_RETENTION_DAYS


# ============================================================
# 二阶段确认机制
# ============================================================

async def create_pending_state_with_expiry(
    db, state_type: str, payload: dict, ttl: int = 1800,
    title: str = "", task_id: str = "", project_id: str = "",
) -> tuple[str, float]:
    """原子操作：生成 token + 写入 SQLite。返回 (token, expires_at)；失败抛异常。

    Batch 4b: 拆出带 expires_at 返回值的版本——确认弹窗需要倒计时/过期过滤。
    """
    token = secrets.token_hex(16)
    now = _time.time()
    expires_at = now + ttl
    await db.execute(
        "INSERT INTO runtime_states(id, state_type, payload, created_at, expires_at, "
        "title, task_id, project_id, status) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (token, state_type, _json.dumps(payload), now, expires_at,
         title or None, task_id or None, project_id or None, "pending")
    )
    await db.commit()
    return token, expires_at


async def create_pending_state(
    db, state_type: str, payload: dict, ttl: int = 1800,
    title: str = "", task_id: str = "", project_id: str = "",
) -> str:
    """原子操作：生成 token + 写入 SQLite。返回 token；失败抛异常。

    Phase 107: 新增 title/task_id/project_id 可选参数，供统一审批 Inbox 查询/展示。
    """
    token, _ = await create_pending_state_with_expiry(
        db, state_type, payload, ttl, title, task_id, project_id,
    )
    return token


async def _pop_pending(db, token: str) -> dict | None:
    """Phase 107: 原子 UPDATE ... RETURNING——单条 SQL 完成 读取+标记 resolved。

    保留审批历史（status='resolved' + resolved_at），供 Inbox 查询。
    仅当 status='pending' 且未过期时返回记录；否则返回 None。
    """
    cursor = await db.execute(
        "UPDATE runtime_states SET status='resolved', resolved_at=? "
        "WHERE id=? AND expires_at > ? AND status='pending' "
        "RETURNING state_type, payload",
        (_time.time(), token, _time.time())
    )
    row = await cursor.fetchone()
    if not row:
        return None
    await db.commit()
    return {"state_type": row[0], "payload": _json.loads(row[1])}


async def _expire_pending_states(db) -> int:
    """Phase 107: 将过期但仍 pending 的记录标记为 expired——供 Inbox 展示过期历史。

    Batch 4b: 标记前先取 (id, task_id)，过期后向对应 task 推送 tool_confirm_expired，
    让前端确认弹窗队列即时移除（前端本地 expires_at 过滤兜底）。

    返回标记的行数。每分钟由 lifespan 后台任务调用。
    """
    cursor = await db.execute(
        "SELECT id, task_id FROM runtime_states WHERE status='pending' AND expires_at < ?",
        (_time.time(),)
    )
    expired_rows = await cursor.fetchall()
    if not expired_rows:
        return 0
    cursor = await db.execute(
        "UPDATE runtime_states SET status='expired' "
        "WHERE status='pending' AND expires_at < ?",
        (_time.time(),)
    )
    await db.commit()
    # Batch 4b: 定向推送过期事件——best-effort，失败不影响标记流程
    try:
        from context import get_app
        app = get_app()
        svc = getattr(app.state, "agent_service", None) if app else None
        if svc is not None:
            for token, task_id in expired_rows:
                if task_id:
                    await svc.ws_manager.safe_send(task_id, {
                        "type": "tool_confirm_expired",
                        "task_id": task_id,
                        "confirm_token": token,
                    })
    except Exception:
        pass
    return cursor.rowcount


async def _cleanup_expired_states(db):
    """Batch 5a C6: 分状态清理——resolved/rejected 审计留存，硬上限只统计非 resolved 行。

    - expired: 即时清（已由 _expire_pending_states 标记，Inbox 已展示过）
    - pending: 超 TTL 由 _expire_pending_states 先标记为 expired（保留现有流程，不直接删）
    - resolved/rejected: 保留 CONFIRM_AUDIT_RETENTION_DAYS 天后清（审计留存）
    - 硬上限：只统计非 resolved 行，避免把 resolved 审计从最老开始删掉
    """
    now = _time.time()
    # 1. expired 即时清
    await db.execute("DELETE FROM runtime_states WHERE status='expired'")
    # 2. resolved/rejected 审计留存期满后清（resolved_at 为空时回退 created_at）
    audit_cutoff = now - CONFIRM_AUDIT_RETENTION_DAYS * 86400
    await db.execute(
        "DELETE FROM runtime_states WHERE status IN ('resolved','rejected') "
        "AND COALESCE(resolved_at, created_at) < ?",
        (audit_cutoff,),
    )
    # 3. 硬上限——只统计非 resolved 行
    cursor = await db.execute(
        "SELECT COUNT(*) FROM runtime_states WHERE status != 'resolved'"
    )
    count = (await cursor.fetchone())[0]
    if count > 5000:
        await db.execute(
            "DELETE FROM runtime_states WHERE id IN ("
            "SELECT id FROM runtime_states WHERE status != 'resolved' "
            "ORDER BY created_at ASC LIMIT ?)",
            (count - 5000,),
        )
    await db.commit()
    from services.attachment_store import cleanup_attachments
    cleanup_attachments()


# ============================================================
# 确认处理器注册表（惰性注册，不依赖定义顺序）
# ============================================================

CONFIRM_HANDLERS = {}

async def _handle_write(db, payload: dict) -> dict:
    """执行文件写入——支持 overwrite 和 append 模式。

    Task 6.4 (F-043): 写入前校验 expected_hash / expected_mtime——检测 preview→confirm
    间隙的外部修改。不匹配则拒绝写入，保护用户/外部 IDE 的修改不被静默覆盖。
    对标 Claude Code content hash 检查。新文件（不存在）跳过校验。
    """
    from security.sandbox import validate_path, get_workspace
    from tools.result import ToolResult, gen_write_observation, gen_write_error_observation
    full = Path(payload["path"])
    try:
        validate_path(str(full.relative_to(Path(get_workspace()) / "shared")))
    except (ValueError, PermissionError):
        return ToolResult(success=False, error="路径验证失败", observation="路径验证失败，操作已拒绝", meta={"tool_name": "write_file"}).to_dict()
    full.parent.mkdir(parents=True, exist_ok=True)
    mode = payload.get("mode", "overwrite")
    content = payload.get("content", "")

    # Task 6.4: 外部修改冲突检测——仅对已存在文件校验（新文件无冲突可能）
    expected_hash = payload.get("expected_hash", "")
    expected_mtime = payload.get("expected_mtime", 0.0)
    if (expected_hash or expected_mtime) and full.exists():
        try:
            existing = full.read_text(encoding="utf-8", errors="replace")
            current_mtime = full.stat().st_mtime
        except Exception as e:
            return ToolResult(success=False, error=f"读取文件失败: {e}", meta={"tool_name": "write_file"}).to_dict()
        ok, err, conflict = check_file_conflict(
            existing, current_mtime, expected_hash, expected_mtime,
        )
        if not ok:
            return ToolResult(
                success=False, error=err, observation=err,
                meta={"tool_name": "write_file", "conflict": conflict, "phase": "executed"},
            ).to_dict()

    try:
        if mode == "append" and full.exists():
            existing = full.read_text(encoding="utf-8", errors="replace")
            content = existing + "\n" + content
        full.write_text(content, encoding="utf-8")
    except PermissionError:
        return ToolResult(success=False, error=f"文件占用: {full.name}", observation=gen_write_error_observation(full.name, "占用"), meta={"tool_name": "write_file", "recoverable": True}).to_dict()
    except OSError as e:
        return ToolResult(success=False, error=str(e), observation=gen_write_error_observation(full.name, str(e)), meta={"tool_name": "write_file"}).to_dict()
    return ToolResult(success=True, data={"path": payload["path"], "bytes_written": len(content), "mode": mode}, observation=gen_write_observation(payload["path"], len(content)), meta={"tool_name": "write_file", "phase": "executed"}).to_dict()


async def _record_shell_changes(db, payload: dict) -> None:
    """把这条 shell 命令改动的文件记成交付物（失败不影响命令结果）。"""
    task_id = str(payload.get("task_id") or "")
    if not task_id or db is None:
        return
    from services.deliverables import session_id_for_task

    session_id = await session_id_for_task(db, task_id)
    if not session_id:
        return
    from services import shell_changes

    await shell_changes.record_command_changes(db, task_id, session_id)


async def _handle_command(db, payload: dict) -> dict:
    """执行命令——v56: 统一返回 ToolResult 格式。
    Phase 47 P4.1: 支持 shell_id → 在持久 Shell 中执行（对标 Claude Code 会话级 Bash）。
    """
    from tools.result import ToolResult, gen_run_executed_observation, gen_run_error_observation
    from .system_tool_command import _execute_command, _run_in_shell
    from services import shell_changes
    shell_id = payload.get("shell_id", "")
    task_id = payload.get("task_id", "")
    # DSH workspace-changes 语义：命令前后比一次 git 脏文件集合，
    # 得到"这条命令改动了哪些文件"——工具层改的文件由快照服务负责，shell 这半靠这里。
    await shell_changes.begin_command(task_id, payload.get("command", ""))
    try:
        if shell_id:
            result = await _run_in_shell(
                shell_id, payload["command"],
                payload.get("timeout_sec", 60),
                task_id=payload.get("task_id"),
            )
        else:
            result = await _execute_command(
                payload["command"], payload.get("timeout_sec", 60),
                task_id=payload.get("task_id"),
            )
    except asyncio.TimeoutError:
        await _record_shell_changes(db, payload)
        return ToolResult(success=False, error=f"超时（{payload.get('timeout_sec', 60)}秒）", observation=gen_run_error_observation(payload["command"], "超时"), meta={"tool_name": "run_command"}).to_dict()
    except Exception as e:
        await _record_shell_changes(db, payload)
        return ToolResult(success=False, error=str(e)[:ERROR_PREVIEW_CHARS], observation=gen_run_error_observation(payload["command"], str(e)[:AGENT_TOOL_HELPERS_ERROR_PREVIEW_CHARS]), meta={"tool_name": "run_command"}).to_dict()
    output, exit_code = result.output, result.exit_code
    await _record_shell_changes(db, payload)
    data = {"command": payload["command"], "output": output, "exit_code": exit_code}
    if exit_code != 0:
        # 2026-10-22：命令失败必须让 agent 看见——此前无论退出码是什么都返回
        # success=True，编译错误/测试不通过在模型眼里全是"成功"。
        tail = output[-800:] if output else ""
        cmd = payload["command"]
        return ToolResult(
            success=False,
            error=f"命令退出码 {exit_code}",
            observation=f"命令 {cmd} 失败（退出码 {exit_code}）。输出尾部：{tail}",
            data=data,
            meta={"tool_name": "run_command", "phase": "executed", "exit_code": exit_code},
        ).to_dict()
    return ToolResult(success=True, data=data, observation=gen_run_executed_observation(payload["command"], len(output)), meta={"tool_name": "run_command", "phase": "executed", "exit_code": exit_code}).to_dict()


async def _handle_rename(db, payload: dict) -> dict:
    """执行文件重命名/移动——确认后二次验证路径安全。"""
    from pathlib import Path as _Path
    from security.sandbox import validate_path, get_workspace
    from tools.result import ToolResult
    old = _Path(payload["old_path"])
    new = _Path(payload["new_path"])
    try:
        validate_path(str(old.relative_to(Path(get_workspace()) / "shared")))
        validate_path(str(new.relative_to(Path(get_workspace()) / "shared")))
    except (ValueError, PermissionError):
        return ToolResult(success=False, error="路径验证失败", meta={"tool_name": "rename_file"}).to_dict()
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
        return ToolResult(success=True, data={"old": payload["old_path"], "new": payload["new_path"]},
                          observation=f"已重命名: {old.name} → {new.name}",
                          meta={"tool_name": "rename_file", "phase": "executed"}).to_dict()
    except Exception as e:
        return ToolResult(success=False, error=str(e), meta={"tool_name": "rename_file"}).to_dict()


async def _handle_delete_dir(db, payload: dict) -> dict:
    """执行目录删除——确认后二次验证。"""
    from pathlib import Path as _Path
    from security.sandbox import validate_path, get_workspace
    from tools.result import ToolResult
    d = _Path(payload["path"])
    try:
        validate_path(str(d.relative_to(Path(get_workspace()) / "shared")))
    except (ValueError, PermissionError):
        return ToolResult(success=False, error="路径验证失败", meta={"tool_name": "delete_dir"}).to_dict()
    try:
        d.rmdir()
        return ToolResult(success=True, data={"path": str(d)},
                          observation=f"已删除空目录: {d.name}",
                          meta={"tool_name": "delete_dir", "phase": "executed"}).to_dict()
    except Exception as e:
        return ToolResult(success=False, error=str(e), meta={"tool_name": "delete_dir"}).to_dict()


async def dispatch_confirm(db, confirm_token: str) -> dict:
    """v35: 策略模式分发——执行确认的操作"""
    pending = await _pop_pending(db, confirm_token)
    if not pending:
        raise ValueError("Token expired or invalid. Please re-run the preview.")
    state_type = pending["state_type"]
    payload = pending["payload"]

    handler = CONFIRM_HANDLERS.get(state_type)
    if not handler:
        raise ValueError(f"Unknown state_type: {state_type}")
    return await handler(db, payload)

# Phase 34: 惰性注册 — Flask 风格，不依赖定义顺序，永不 NameError
CONFIRM_HANDLERS["pending_write"] = _handle_write
CONFIRM_HANDLERS["pending_command"] = _handle_command
CONFIRM_HANDLERS["pending_delete_file"] = _handle_delete
CONFIRM_HANDLERS["pending_rename_file"] = _handle_rename
CONFIRM_HANDLERS["pending_delete_dir"] = _handle_delete_dir


async def _handle_edit(db, payload: dict) -> dict:
    """E1: 执行 SEARCH/REPLACE 精确编辑——确认后重新应用编辑并写入文件。

    重新应用（而非使用 preview 阶段的缓存内容）避免 preview→confirm 间的竞态。

    Task 6.4 (F-043): 重新应用前校验 expected_hash / expected_mtime——检测
    preview→confirm 间隙的外部修改。不匹配则拒绝写入，保护用户/外部 IDE 的修改。
    对标 Claude Code content hash 检查。
    """
    from security.sandbox import validate_path, get_workspace
    from tools.result import ToolResult
    full = Path(payload["path"])
    try:
        validate_path(str(full.relative_to(Path(get_workspace()) / "shared")))
    except (ValueError, PermissionError):
        return ToolResult(success=False, error="路径验证失败",
                          meta={"tool_name": "edit_file"}).to_dict()
    if not full.exists():
        return ToolResult(success=False, error=f"文件不存在: {payload.get('path', '')}",
                          meta={"tool_name": "edit_file"}).to_dict()
    try:
        original = full.read_text(encoding="utf-8")
    except Exception as e:
        return ToolResult(success=False, error=f"读取文件失败: {e}",
                          meta={"tool_name": "edit_file"}).to_dict()

    # Task 6.4: 外部修改冲突检测——校验 expected_hash / expected_mtime
    # 对标 Claude Code content hash 检查：文件在 confirm 间隙被外部修改时拒绝写入
    expected_hash = payload.get("expected_hash", "")
    expected_mtime = payload.get("expected_mtime", 0.0)
    if expected_hash or expected_mtime:
        current_mtime = full.stat().st_mtime
        ok, err, conflict = check_file_conflict(
            original, current_mtime, expected_hash, expected_mtime,
        )
        if not ok:
            return ToolResult(
                success=False, error=err, observation=err,
                meta={"tool_name": "edit_file", "conflict": conflict, "phase": "executed"},
            ).to_dict()

    # 重新应用 SEARCH/REPLACE
    # Task 6.12 (F-042): 支持 edit 项 replace_all=True 替换所有匹配
    current = original
    matches = 0
    for edit in payload.get("edits", []):
        search = edit.get("search", "")
        replace = edit.get("replace", "")
        if not search:
            continue
        if bool(edit.get("replace_all", False)):
            count = current.count(search)
            if count == 0:
                continue
            current = current.replace(search, replace)
            matches += count
        else:
            idx = current.find(search)
            if idx == -1:
                continue
            current = current[:idx] + replace + current[idx + len(search):]
            matches += 1
    if matches == 0:
        return ToolResult(success=False, error="确认后重新应用编辑失败——文件可能已被修改",
                          meta={"tool_name": "edit_file"}).to_dict()
    try:
        full.write_text(current, encoding="utf-8")
    except PermissionError:
        return ToolResult(success=False, error=f"文件占用: {full.name}",
                          meta={"tool_name": "edit_file", "recoverable": True}).to_dict()
    except OSError as e:
        return ToolResult(success=False, error=str(e),
                          meta={"tool_name": "edit_file"}).to_dict()
    return ToolResult(
        success=True,
        data={"path": payload.get("path", ""), "matches": matches,
              "old_size": len(original), "new_size": len(current)},
        observation=f"已精确替换 {matches} 处（共 {len(original)}→{len(current)} 字符）",
        meta={"tool_name": "edit_file", "phase": "executed"},
    ).to_dict()


CONFIRM_HANDLERS["pending_edit_file"] = _handle_edit


# Phase 101: 工作流触发器审批 handler——用户在 Inbox 批准后执行工作流
async def _handle_workflow_trigger(db, payload: dict) -> dict:
    """审批通过后执行工作流。payload 含 workflow_id / initial_input / trigger_id。"""
    from context import get_app
    app = get_app()
    if app is None:
        return {"success": False, "error": "app-not-available",
                "tool_name": "workflow_trigger"}
    ws = getattr(app.state, "workflow_service", None)
    if ws is None:
        return {"success": False, "error": "workflow-service-not-available",
                "tool_name": "workflow_trigger"}
    workflow_id = payload.get("workflow_id", "")
    initial_input = payload.get("initial_input", "")
    trigger_id = payload.get("trigger_id", "")
    try:
        run_id = await ws.run(workflow_id, initial_input)
        ts = getattr(app.state, "workflow_trigger_service", None)
        if ts and trigger_id:
            await ts._increment_run_count(trigger_id)
        return {"success": True, "run_id": run_id,
                "tool_name": "workflow_trigger"}
    except Exception as e:
        return {"success": False, "error": str(e),
                "tool_name": "workflow_trigger"}

CONFIRM_HANDLERS["pending_workflow_trigger"] = _handle_workflow_trigger
