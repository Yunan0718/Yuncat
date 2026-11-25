"""hooks/registry.py —— Hook 拦截链注册与执行

v40 设计模式 6：PreToolUse 可在工具执行前阻止调用，
PostToolUse 用于审计记录，OnError 用于错误处理。

匹配器语法：按工具名正则匹配。
"""
from __future__ import annotations

import asyncio
import logging
import os as _os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Awaitable

_log = logging.getLogger("yuncat.hooks")


class HookType(Enum):
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    ON_ERROR = "on_error"
    # Phase 23: Claude Code 对标新增
    STOP = "stop"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    # Phase D W.2: 工作流/会话生命周期通知 + 预压缩钩子
    NOTIFICATION = "notification"
    PRE_COMPACT = "pre_compact"


@dataclass
class HookEntry:
    """Hook 条目——存储回调 + 匹配模式"""
    hook_type: HookType
    pattern: re.Pattern
    func: Callable[..., Awaitable[dict | None]]
    description: str = ""


# 全局 Hook 注册表
_hooks: dict[HookType, list[HookEntry]] = {t: [] for t in HookType}


def register_hook(hook_type: HookType, tool_pattern: str, description: str = ""):
    """装饰器：注册 Hook——按工具名正则匹配。回调签名如下：

    - PRE_TOOL_USE: async def fn(tool_name, arguments, agent_id) -> dict | None
      返回 {"block": True, "reason": "..."} 阻止执行
      返回 None 或 {"block": False} 放行

    - POST_TOOL_USE: async def fn(tool_name, arguments, result, agent_id) -> None
      用于审计日志/通知/缓存失效等

    - ON_ERROR: async def fn(tool_name, arguments, error, agent_id) -> None
      用于错误告警/降级处理
    """
    pattern = re.compile(tool_pattern)

    def decorator(func: Callable):
        _hooks[hook_type].append(HookEntry(
            hook_type=hook_type,
            pattern=pattern,
            func=func,
            description=description,
        ))
        return func
    return decorator


async def run_pre_tool_hooks(
    tool_name: str, arguments: dict, agent_id: str = "",
) -> dict | None:
    """执行所有匹配的 PreToolUse Hook。任一返回 block=True 即拦截。
    返回第一个阻止决策的 dict，或 None 表示全部放行。
    """
    for entry in _hooks[HookType.PRE_TOOL_USE]:
        if entry.pattern.search(tool_name):
            try:
                result = await entry.func(tool_name, arguments, agent_id)
                if result and result.get("block"):
                    _log.info(
                        f"Hook '{entry.description}' blocked {tool_name}: {result.get('reason', '')}"
                    )
                    return result
            except Exception:
                _log.exception(f"PreToolUse hook '{entry.description}' failed, allowing")
    return None


async def run_post_tool_hooks(
    tool_name: str, arguments: dict, result: dict, agent_id: str = "",
) -> None:
    """执行所有匹配的 PostToolUse Hook（fire-and-forget）。"""
    for entry in _hooks[HookType.POST_TOOL_USE]:
        if entry.pattern.search(tool_name):
            try:
                await entry.func(tool_name, arguments, result, agent_id)
            except Exception:
                _log.exception(f"PostToolUse hook '{entry.description}' failed")


async def run_error_hooks(
    tool_name: str, arguments: dict, error: Exception, agent_id: str = "",
) -> None:
    """执行所有匹配的 OnError Hook。"""
    for entry in _hooks[HookType.ON_ERROR]:
        if entry.pattern.search(tool_name):
            try:
                await entry.func(tool_name, arguments, error, agent_id)
            except Exception:
                _log.exception(f"OnError hook '{entry.description}' failed")


def list_hooks() -> list[dict]:
    """列出所有已注册的 Hook（调试用）"""
    result = []
    for hook_type, entries in _hooks.items():
        for entry in entries:
            result.append({
                "type": hook_type.value,
                "pattern": entry.pattern.pattern,
                "description": entry.description,
            })
    return result


# ============================================================
# 内置 Hook（Phase 2 默认注册）
# ============================================================

# Round 1 S6: 审计日志 DB 连接——lifespan 注入，fire-and-forget 写 audit_log 表
_audit_db = None


def set_audit_db(conn) -> None:
    """注入 DB 连接供审计 hook 写 audit_log 表。"""
    global _audit_db
    _audit_db = conn


# ============================================================
# CodeWhale 借鉴 #12: Hook 系统多 sink
# ============================================================
# 对标 CodeWhale hooks/lib.rs 的 4 种 sink:
#   - StdoutHookSink / JsonlHookSink（文件追加）/ WebhookHookSink / UnixSocketHookSink
# Yuncat 实现 JsonlHookSink（最实用）——追加 JSON 行到文件，提供独立于 DB 的审计轨迹。
# DB audit_log 表适合结构化查询；JsonlSink 适合 tail -f 实时监控 / 外部日志收集。

from typing import Protocol


class HookSink(Protocol):
    """Hook 事件 sink 协议——实现 write(event) 即可。"""

    def write(self, event: dict) -> None:
        ...


class JsonlHookSink:
    """JSONL 文件 sink——每条事件追加一行 JSON 到指定文件。

    线程安全：单进程内多次 write 串行（open(..., "a") + write 在 POSIX 上原子
    当 line < PIPE_BUF；Windows 上用 flock 兼容性差，依赖单进程串行调用保证）。
    失败仅记日志——sink 故障不影响主流程。

    日志轮转：文件超过 max_bytes 时重命名为 .1（覆盖旧备份），开始新文件。
    对标 Python logging.handlers.RotatingFileHandler 的 (maxBytes, backupCount=1)。
    """

    def __init__(self, path: str, max_bytes: int = 10 * 1024 * 1024):
        """Args:
            path: JSONL 文件路径
            max_bytes: 单文件大小上限（默认 10MB），超过时轮转
        """
        self._path = path
        self._max_bytes = max_bytes
        self._backup_path = path + ".1"
        # 确保目录存在
        import os as _os
        _parent = _os.path.dirname(path)
        if _parent:
            _os.makedirs(_parent, exist_ok=True)

    def write(self, event: dict) -> None:
        import json as _json
        import os as _os
        import time as _time
        try:
            line = _json.dumps(
                {"ts": _time.time(), **event}, ensure_ascii=False, default=str
            )
            # 轮转检查——文件超过上限时备份 + 新建
            try:
                if _os.path.exists(self._path) and _os.path.getsize(self._path) > self._max_bytes:
                    _os.replace(self._path, self._backup_path)  # 原子替换旧备份
            except OSError:
                pass  # 轮转失败不影响写入
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            _log.debug("JsonlHookSink write failed", exc_info=True)


# 全局 sink 列表——lifespan 注入
_hook_sinks: list[HookSink] = []


def set_hook_sinks(sinks: list[HookSink]) -> None:
    """设置全局 hook sink 列表（替换）。"""
    global _hook_sinks
    _hook_sinks = list(sinks)


def add_hook_sink(sink: HookSink) -> None:
    """追加一个 hook sink。"""
    _hook_sinks.append(sink)


def clear_hook_sinks() -> None:
    """清空所有 hook sink。"""
    _hook_sinks.clear()


def _emit_to_sinks(event: dict) -> None:
    """将事件发送到所有已注册的 sink（fire-and-forget）。"""
    if not _hook_sinks:
        return
    for sink in _hook_sinks:
        try:
            sink.write(event)
        except Exception:
            _log.debug("Hook sink %r failed", type(sink).__name__, exc_info=True)


# Round 1 S6: 审计正则扩展——覆盖所有副作用工具（不只 write_file/run_command）
_AUDIT_TOOL_PATTERN = r"write_file|run_command|delete_file|rename|edit|mkdir|move|copy"


@register_hook(HookType.PRE_TOOL_USE, _AUDIT_TOOL_PATTERN, "审计日志：写操作前记录")
async def audit_before_write(tool_name: str, arguments: dict, agent_id: str = "") -> dict | None:
    """所有写操作执行前记录审计日志"""
    _log.info(f"[AUDIT] {agent_id} → {tool_name}({_safe_args_summary(arguments)})")
    # Round 1 S6: 同步写 audit_log 表（preview 阶段，success=None）
    if _audit_db is not None:
        from repositories.audit_repo import log_audit
        task_id, session_id = await _audit_identity(arguments)
        await log_audit(
            _audit_db, agent_id, tool_name, arguments,
            success=None, phase="preview",
            session_id=session_id, task_id=task_id,
        )
    # CodeWhale 借鉴 #12: 同时发送到 hook sinks（JsonlHookSink 等）
    _emit_to_sinks({
        "type": "audit", "phase": "preview", "agent_id": agent_id,
        "tool": tool_name, "args_summary": _safe_args_summary(arguments),
    })
    return None  # 不阻止


@register_hook(HookType.PRE_TOOL_USE, r"write_file", "敏感路径防护：阻止系统目录写入")
async def block_system_path_write(tool_name: str, arguments: dict, agent_id: str = "") -> dict | None:
    """阻止对系统目录的写入"""
    path = arguments.get("path", "")
    if not path:
        return None
    dangerous_prefixes = ("C:\\Windows", "C:\\Windows\\System32", "/etc", "/usr", "/bin", "/boot")
    for prefix in dangerous_prefixes:
        if path.startswith(prefix):
            return {"block": True, "reason": f"禁止写入系统目录: {path}"}
    return None


@register_hook(HookType.POST_TOOL_USE, _AUDIT_TOOL_PATTERN, "写操作后审计记录")
async def audit_after_write(tool_name: str, arguments: dict, result: dict, agent_id: str = "") -> None:
    """写操作完成后记录结果——v56: 适配 ToolResult 格式"""
    success = result.get("success", False)
    meta = result.get("meta", {})
    phase = meta.get("phase", "execute")
    tool = meta.get("tool_name", tool_name)
    blocked = meta.get("blocked", False)
    _log.info(f"[AUDIT-RESULT] {agent_id} → {tool}: {'success' if success else 'failed'} (phase={phase})")
    # Round 1 S6: 同步写 audit_log 表（execute 阶段）
    if _audit_db is not None:
        from repositories.audit_repo import log_audit
        task_id, session_id = await _audit_identity(arguments)
        await log_audit(
            _audit_db, agent_id, tool, arguments,
            success=success,
            phase="blocked" if blocked else "execute",
            session_id=session_id, task_id=task_id,
        )
    # CodeWhale 借鉴 #12: 同时发送到 hook sinks
    _emit_to_sinks({
        "type": "audit", "phase": "blocked" if blocked else "execute",
        "agent_id": agent_id, "tool": tool, "success": success,
    })


# ============================================================
# Phase 19: Side-git 自动快照 Hook
# ============================================================
# 全局引用——lifespan 中注入
_snapshot_service = None
from config import HOOKS_MAX_PENDING_DIAG, HOOKS_MAX_REACTIVATIONS

# 快照覆盖面来自服务自身（同一份知识不写两处）。此前只匹配 write_file|run_command，
# 导致 edit/delete/rename 完全没有快照、撤销不了。
from services.snapshot_service import snapshot_hook_pattern as _snapshot_hook_pattern

_SNAPSHOT_TOOL_PATTERN = _snapshot_hook_pattern()

_pending_diagnostics: dict[str, list[dict]] = {}
_MAX_PENDING_DIAG = HOOKS_MAX_PENDING_DIAG  # 防止内存无限增长


def set_snapshot_service(svc):
    global _snapshot_service
    _snapshot_service = svc


def current_task_identity() -> str:
    """当前工具调用所属 task_id——由 ToolService.execute 用 contextvar 传播进来。

    历史上这里读 `arguments["_task_id"]`，但工具循环从不写这个键，于是每行快照都拿到
    随机 uuid，撤销永远查不到。contextvar 是唯一真源。
    """
    from context import get_current_task_id
    return str(get_current_task_id() or "")


async def _audit_identity(arguments: dict) -> tuple[str, str]:
    """审计用 (task_id, session_id)：contextvar 优先，缺 session 时按 task 反查。"""
    task_id = current_task_identity() or str(arguments.get("_task_id", "") or "")
    session_id = str(arguments.get("_session_id", "") or "")
    if task_id and not session_id and _audit_db is not None:
        try:
            cursor = await _audit_db.execute(
                "SELECT session_id FROM tasks WHERE id=?", (task_id,))
            row = await cursor.fetchone()
            session_id = str(row[0] or "") if row else ""
        except Exception:
            # 回退：审计仍写入，只是 session 维度留空（不让审计因反查失败而丢记录）
            _log.debug("audit: session lookup failed for %s", task_id, exc_info=True)
    return task_id, session_id


def get_pending_diagnostics() -> dict[str, list[dict]]:
    """获取所有待注入的诊断结果（LSP lint hook 写入）"""
    return _pending_diagnostics


@register_hook(HookType.PRE_TOOL_USE, _SNAPSHOT_TOOL_PATTERN, "文件快照：操作前备份受影响文件")
async def snapshot_pre_tool(tool_name: str, arguments: dict, agent_id: str = "") -> dict | None:
    """操作前备份受影响文件；快照 id 放私有键（ToolService 调工具时会剔除）。"""
    task_id = current_task_identity()
    if _snapshot_service and task_id:
        sid = await _snapshot_service.pre_tool(
            tool_name, arguments, task_id, arguments.get("_turn_seq", 0))
        if sid:
            arguments["_snapshot_pre_id"] = sid
    return None


@register_hook(HookType.POST_TOOL_USE, _SNAPSHOT_TOOL_PATTERN, "操作后：LSP 诊断 + 终端回传")
async def snapshot_and_lint_post_tool(tool_name: str, arguments: dict, result: dict, agent_id: str = "") -> None:
    """操作后做 LSP 诊断 / Agent 命令回传。

    刻意不在这里拍 post 快照：write_file / edit_file / delete_file / rename_file
    都是「先预览、确认后才真正落盘」，post 拍在预览之后内容并未改变，只会白占磁盘。
    交付物行数统计以**当前磁盘状态**作新侧（services/deliverables.py）。
    """
    task_id = current_task_identity()

    # LSP 诊断（仅 write_file）
    if tool_name == "write_file" and result.get("success"):
        # E3: 用 result["data"]["path"]（相对 ws 根，如 "shared/foo.txt"）而非 arguments["path"]
        # （相对 shared，如 "foo.txt"）——前者是 write_file 规范化后的路径，正确解析为绝对路径
        _data = result.get("data", {})
        _rel = _data.get("path", "") if isinstance(_data, dict) else ""
        if _rel:
            asyncio.create_task(_run_lint_diagnostic(_rel, task_id))

    # Phase 21: Agent 命令回传终端
    if tool_name == "run_command" and result.get("success"):
        try:
            from services.terminal_manager import get_terminal_manager
            tm = get_terminal_manager()
            cmd = arguments.get("command", "")
            data = result.get("data", {})
            output = data.get("output", "") if isinstance(data, dict) else str(data)
            await tm.broadcast_agent_block(
                agent_id=agent_id or "unknown",
                command=cmd,
                output_snippet=output,
                exit_code=0,
                task_id=task_id,
            )
        except Exception:
            _log.debug("广播 Agent 命令块失败", exc_info=True)


async def _run_lint_diagnostic(rel_path: str, task_id: str) -> None:
    """异步执行 lint：缓存到 _pending_diagnostics（给 agent）+ 发布 WS 事件（给前端）。

    E3: 同时推送诊断到前端（通过 EventBus → WS 广播），供 Monaco inline hint 显示。
    CodeWhale 借鉴: 支持 Python(ruff) + TypeScript(tsc) 双语言 lint；
    诊断严重度过滤（默认只注入 error）+ 按文件截断（max 20 条）。
    rel_path: 相对 workspace 根的路径（如 "shared/foo.txt"）——与 write_file data["path"] 一致。
    """
    from services.lint_service import (
        lint_python_file, lint_typescript_file, detect_language,
        filter_and_truncate_diagnostics,
    )
    from services.event_bus import get_event_bus
    from security.sandbox import get_workspace

    lang = detect_language(rel_path)
    if not lang:
        return  # 不支持的文件类型

    ws = get_workspace()
    full = ws / rel_path
    if not full.is_file():
        return

    # 按语言分发 lint
    if lang == "python":
        diags = await lint_python_file(str(full), cwd=str(ws))
    elif lang == "typescript":
        diags = await lint_typescript_file(str(full), cwd=str(ws))
    else:
        return

    # CodeWhale 借鉴: 严重度过滤 + 按文件截断（error 优先，warning 可配置）
    diags = filter_and_truncate_diagnostics(diags)

    # 1. 缓存给 agent（下一轮 system prompt 注入）——仅当有诊断时（保持原行为）
    # E3: 升级字段补 line/col/severity（原仅 file/message/code），消费者 agent_context.py 仍兼容
    if diags:
        if len(_pending_diagnostics) >= _MAX_PENDING_DIAG:
            oldest = next(iter(_pending_diagnostics))
            del _pending_diagnostics[oldest]
        _pending_diagnostics[task_id] = [
            {"file": rel_path, "line": d["line"], "col": d["col"],
             "message": d["message"], "severity": d["severity"], "code": d["code"]}
            for d in diags
        ]

    # 2. E3: 始终推送到前端——有诊断时画 marker，无诊断时清 marker（空列表）
    try:
        await get_event_bus().publish("diagnostics_updated", {
            "file": rel_path.replace("\\", "/"),  # 跨平台统一正斜杠，匹配前端 filePath
            "language": lang,
            "diagnostics": diags,
        })
    except Exception:
        _log.debug("Failed to publish diagnostics_updated event", exc_info=True)


def _safe_args_summary(arguments: dict) -> str:
    """安全摘要参数——截断长内容，防日志膨胀。

    Task 3.1: 对 str 值先脱敏再截断——防止 audit log 里泄露 sk-/password= 等敏感串。
    """
    from security.redact import redact_secrets
    summary = {}
    for k, v in arguments.items():
        if isinstance(v, str):
            v = redact_secrets(v)
            if len(v) > 80:
                v = v[:80] + "..."
        summary[k] = v
    return str(summary)


# ============================================================
# Phase 23: Stop Hook + SessionStart/SessionEnd
# ============================================================
_MAX_REACTIVATIONS = HOOKS_MAX_REACTIVATIONS
_reactivation_counts: dict[str, int] = {}  # session_id → count


async def run_stop_hooks(session_id: str, transcript: str) -> dict:
    """Stop hook——在 Agent 完成前执行 SOP 检查。

    返回: {"continue": True/False, "reactivate": True/False, "message": "..."}
      - continue=True: 正常结束
      - reactivate=True: 将 message 注入为新 prompt → Agent 重新执行
    """
    rc = _reactivation_counts.get(session_id, 0)
    if rc >= _MAX_REACTIVATIONS:
        _log.warning("Stop hook: session %s reached max reactivations (%d)", session_id, rc)
        return {"continue": True, "reactivate": False}

    for entry in _hooks.get(HookType.STOP, []):
        try:
            result = await entry.func(session_id, transcript)
            if result and result.get("reactivate"):
                _reactivation_counts[session_id] = rc + 1
                _log.info("Stop hook reactivation %d/%d for session %s: %s",
                          rc + 1, _MAX_REACTIVATIONS, session_id, result.get("message", ""))
                return result
        except Exception:
            _log.exception("Stop hook '%s' failed", entry.description)
    return {"continue": True, "reactivate": False}


async def run_session_hooks(hook_type: HookType, session_id: str, context: dict = None) -> None:
    """SessionStart/SessionEnd hook——fire-and-forget，不阻塞主流程"""
    for entry in _hooks.get(hook_type, []):
        try:
            await entry.func(session_id, context or {})
        except Exception:
            _log.exception("%s hook '%s' failed", hook_type.value, entry.description)


# ============================================================
# Phase D W.2: Notification / PreCompact hooks
# ============================================================
_notification_center = None


def set_notification_center(svc) -> None:
    """注入 NotificationCenter，供内置 notification hook 使用。"""
    global _notification_center
    _notification_center = svc


async def run_notification_hooks(
    run_id: str, workflow_id: str, status: str, context: dict | None = None,
) -> None:
    """触发工作流/会话生命周期通知 hook。

    status: started | completed | failed | paused | resumed
    context: 附加字段，如 node_count / error / duration_ms
    """
    payload = {
        "run_id": run_id,
        "workflow_id": workflow_id,
        "status": status,
        "context": context or {},
    }
    for entry in _hooks.get(HookType.NOTIFICATION, []):
        try:
            await entry.func(payload)
        except Exception:
            _log.exception("Notification hook '%s' failed", entry.description)


async def run_pre_compact_hooks(
    run_id: str, workflow_id: str, snapshot: dict,
) -> None:
    """触发 PreCompact hook，允许 hook 修改 snapshot 字典（原地）。

    工作流引擎在持久化快照前调用，hook 可压缩节点输出、清理临时字段等。
    """
    payload = {
        "run_id": run_id,
        "workflow_id": workflow_id,
        "snapshot": snapshot,
    }
    for entry in _hooks.get(HookType.PRE_COMPACT, []):
        try:
            await entry.func(payload)
        except Exception:
            _log.exception("PreCompact hook '%s' failed", entry.description)


@register_hook(HookType.NOTIFICATION, r".*", "工作流生命周期通知：统一通知中心")
async def workflow_notification_hook(payload: dict) -> None:
    """将工作流生命周期事件发送到 NotificationCenter。"""
    if _notification_center is None:
        return
    status = payload.get("status", "info")
    run_id = payload.get("run_id", "")
    workflow_id = payload.get("workflow_id", "")
    ctx = payload.get("context", {})
    title_map = {
        "started": "工作流启动",
        "completed": "工作流完成",
        "failed": "工作流失败",
        "paused": "工作流已暂停",
        "resumed": "工作流已恢复",
    }
    body_map = {
        "started": f"工作流 {workflow_id[:8]} 开始运行",
        "completed": f"工作流 {workflow_id[:8]} 已完成（运行 {run_id[:8]}）",
        "failed": f"工作流 {workflow_id[:8]} 失败：{ctx.get('error', '')[:120]}",
        "paused": f"工作流 {workflow_id[:8]} 已暂停，可稍后恢复",
        "resumed": f"工作流 {workflow_id[:8]} 已恢复运行",
    }
    try:
        await _notification_center.notify(
            title=title_map.get(status, f"工作流 {status}"),
            body=body_map.get(status, f"run={run_id[:8]} workflow={workflow_id[:8]}"),
            type=f"workflow_{status}",
            level="info" if status in ("started", "completed", "resumed") else "warning",
            task_id=run_id,
        )
    except Exception:
        _log.debug("workflow notification hook failed", exc_info=True)


@register_hook(HookType.PRE_COMPACT, r".*", "工作流快照预压缩：过大时截断节点输出")
async def workflow_pre_compact_hook(payload: dict) -> None:
    """当工作流快照 JSON 超过阈值时，截断 node_results 中过长的 output。"""
    import json
    from config import WORKFLOW_SNAPSHOT_PRECOMPACT_THRESHOLD_BYTES

    snapshot = payload.get("snapshot", {})
    node_results = snapshot.get("node_results", {})
    if not isinstance(node_results, dict):
        return

    rough = len(json.dumps(snapshot, ensure_ascii=False))
    if rough <= WORKFLOW_SNAPSHOT_PRECOMPACT_THRESHOLD_BYTES:
        return

    _log.info("PreCompact hook triggered for run %s (snapshot %d bytes)",
              payload.get("run_id", "?")[:8], rough)
    for nid, result in list(node_results.items()):
        if isinstance(result, dict) and "output" in result:
            out = result["output"]
            if isinstance(out, str) and len(out) > 2000:
                result["output"] = out[:1000] + "\n\n...[预压缩截断]\n\n" + out[-500:]
                result["_pre_compact"] = True


@register_hook(HookType.STOP, r".*", "示例: 检测是否有未提交变更")
async def stop_check_uncommitted(session_id: str, transcript: str) -> dict | None:
    """内置 Stop hook 示例——检测工作区是否有未提交的 git 变更。
    默认不激活（仅在 settings 中显式配置 hooks.stop 后启用）。
    """
    return None  # 默认不激活


@register_hook(HookType.SESSION_END, r".*", "会话结束摘要：生成并持久化结构化摘要")
async def session_end_summary(session_id: str, context: dict | None = None) -> None:
    """内置 SESSION_END hook——会话结束时生成结构化摘要并持久化。

    P0-3.3: 激活原本死接线的 SESSION_END 机制。复用 memory_compact.compact_now
    的 6 节摘要生成逻辑（lenient 模式）。短会话（<4 消息）自动跳过。
    从 context 获取 memory_service + model_config。
    """
    ctx = context or {}
    mem_svc = ctx.get("memory_service")
    if not mem_svc:
        return
    try:
        model_cfg = ctx.get("model_config") or {}
        result = await mem_svc.compact_now(session_id, model_cfg)
        if result:
            _log.info("SESSION_END summary generated for %s", session_id[:8])
    except Exception:
        _log.debug("SESSION_END summary failed for %s", session_id[:8], exc_info=True)


# ── Phase 43 W.2: Hook 生命周期 Profile ──

# 对标 ECC: HOOK_PROFILES = {minimal/standard/strict}
# 对标 Claude Code: YUNCAT_HOOK_PROFILE 环境变量运行时切换

_HOOK_PROFILE = _os.environ.get("YUNCAT_HOOK_PROFILE", "standard")


def get_hook_profile() -> str:
    """返回当前生效的 Hook Profile 名称"""
    return _HOOK_PROFILE


def set_hook_profile(profile: str) -> None:
    """运行时切换 Hook Profile (供 Settings 调用)"""
    global _HOOK_PROFILE
    valid = {"minimal", "standard", "strict"}
    if profile in valid:
        _HOOK_PROFILE = profile
        _log.info("Hook profile switched to: %s", profile)
    else:
        _log.warning("Invalid hook profile '%s', keeping '%s'", profile, _HOOK_PROFILE)


HOOK_PROFILES = {
    "minimal": {
        "description": "最小: 仅安全扫描",
        "hooks": ["security_scan"],
    },
    "standard": {
        "description": "标准: 安全+格式化+lint",
        "hooks": ["security_scan", "format_check", "lint_quick"],
    },
    "strict": {
        "description": "严格: 安全+格式化+lint+测试+bandit",
        "hooks": ["security_scan", "format_check", "lint_full", "pytest_quick", "bandit_scan"],
    },
}
