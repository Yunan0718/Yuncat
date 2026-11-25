"""services/tool_service.py —— 工具执行服务（三级硬约束 + Hook 拦截链 + 串行锁 + ToolResult）

架构铁律 #1: 所有文件读写、命令执行走 ToolService.execute()。
架构铁律 #4: 非并发工具串行——is_concurrency_safe=False 走 _serial_locks。
v40: Hook 拦截链集成。v56: ToolResult 统一返回格式。
"""
import asyncio
import json as _json
import logging
import time as _time

from config import (
    CONFIRM_TIMEOUT, PENDING_CONFIRM_MAX, TOOL_DEFAULT_TIMEOUT_SEC,
    YUNCAT_AGENT_ID,
)
from tools.registry import get_all_tools, resolve_tool_name

_log = logging.getLogger("yuncat.tool_service")

# Phase 47: 确认流——asyncio.Future 机制
# confirm_token → Future，工具循环 await future，用户确认后 resolve
# Phase 48 T3-1: 改 OrderedDict LRU + 固定上限（原裸 dict 无 TTL，用户不确认时
# future 永久泄漏）。cachetools 不在依赖中，用标准库 OrderedDict 实现（不引入新依赖）。
# Phase 48-F: _CONFIRM_TIMEOUT 从 config.CONFIRM_TIMEOUT 导入（与 agent_tool_loop 共用单一来源）
from collections import OrderedDict as _OrderedDict
# M29 设计说明：_pending_confirmations 依赖 asyncio 协作式调度保证原子性——
# move_to_end/popitem 之间无 await 点，不会被协程切换打断。如未来改为多线程
# 或在操作间引入 await 点，需加 asyncio.Lock 保护。
_pending_confirmations: _OrderedDict[str, asyncio.Future] = _OrderedDict()
# Task 3.5: 上限改为 config 化（原硬编码 1000）
_PENDING_CONFIRM_MAX = PENDING_CONFIRM_MAX
_CONFIRM_TIMEOUT = CONFIRM_TIMEOUT  # 向后兼容别名（模块内已有引用）

# Batch B M6: 工具名 → pending state_type 映射——从 execute_confirmable 内提取到模块级，
# 消除两处重复定义（原 line 432 + line 461 完全相同）。
_STATE_TYPE_MAP = {
    "write_file": "pending_write",
    "run_command": "pending_command",
    "delete_file": "pending_delete_file",
    "rename_file": "pending_rename_file",
    "delete_dir": "pending_delete_dir",
    "edit_file": "pending_edit_file",
}

# 修复 1.6 (F-031): session 级工具权限缓存——用户勾选 "Always allow in this session"
# 后，同进程内该工具后续调用跳过确认弹窗。对标 Cursor "Always allow in this workspace" /
# Claude Code per-tool 记住。桌面应用 session = 进程生命周期，重启后清空（符合 "session" 语义）。
_session_allowed_tools: set[str] = set()


def add_session_allowed_tool(tool_name: str) -> None:
    """记录用户已批准 "Always allow" 的工具——后续同 session 内跳过确认。"""
    if tool_name:
        _session_allowed_tools.add(tool_name)
        _log.info("Tool '%s' added to session allow-list (skip confirm this session)", tool_name)


def is_session_allowed(tool_name: str) -> bool:
    """检查工具是否在 session 级允许列表中。"""
    return tool_name in _session_allowed_tools


def clear_session_allowed_tools() -> None:
    """清空 session 级允许列表（供测试 / session 重置使用）。"""
    _session_allowed_tools.clear()


def _grant_subject(arguments: dict | None) -> str:
    """从工具参数提取待匹配主体（命令串 / 路径 / URL），供参数模式匹配使用。

    对标主流 `Tool(specifier)`：如 `run_command` 取 command 串，才可支持
    `run_command('git diff *')` 这类前缀规则。取不到时返回空串（回退为裸工具名语义）。
    """
    if not isinstance(arguments, dict):
        return ""
    for key in ("command", "cmd", "path", "file_path", "filePath", "url", "target"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


async def is_standing_granted(
    db, tool_name: str, agent_id: str | None = None, project_id: str = "",
    arguments: dict | None = None,
) -> bool:
    """检查该调用是否被**持久化**规则放行（scope = 工具 / 参数模式 / agent / 项目）。

    对标 OpenClaw 的 scoped standing grants + 主流 `Tool(specifier)` 参数模式匹配。
    与内存态 `is_session_allowed` 互补——后者进程重启即失效，本函数查库因而可跨重启。
    判定顺序 deny → allow，无命中即拒绝（fail-closed）。
    """
    if db is None or not tool_name:
        return False
    from services.standing_grants import is_granted
    return await is_granted(
        db, tool_name, agent_id or "", project_id or "",
        _grant_subject(arguments),
    )


def _add_pending_confirmation(token: str, future: asyncio.Future, db=None) -> None:
    """Phase 48 T3-1 / Task 3.5: 添加确认 future，超过上限时淘汰最老的（LRU）。

    淘汰时 cancel 未完成的 future，使等待的 agent_tool_loop 及时释放。
    Task 3.5: 同步标记 DB runtime_states 状态为 expired——原实现只 cancel future，
    DB 行仍为 pending，导致用户仍可通过 Inbox 批准已淘汰的确认（竞态窗口）。
    用 asyncio.create_task 异步更新 DB（本函数是同步的，不能 await）。
    db=None 时仅 cancel future（向后兼容 / 测试场景）。
    """
    _pending_confirmations[token] = future
    _pending_confirmations.move_to_end(token)
    while len(_pending_confirmations) > _PENDING_CONFIRM_MAX:
        _old_token, _old_future = _pending_confirmations.popitem(last=False)
        if not _old_future.done():
            _old_future.cancel()
        _log.warning(
            "LRU evicted pending confirmation: %s (future cancelled, marking DB expired)",
            _old_token,
        )
        # Task 3.5: 异步标记 DB 状态为 expired——关闭竞态窗口
        # db=None 跳过（测试 / 无 DB 场景）；create_task 需 event loop，无 loop 时跳过
        if db is not None:
            try:
                asyncio.create_task(_mark_pending_expired(_old_token, db))
            except RuntimeError:
                # 无 event loop（极端场景如测试同步调用）——跳过 DB 更新
                _log.debug("No event loop, skip DB expired mark for %s", _old_token)


async def _mark_pending_expired(token: str, db) -> None:
    """Task 3.5: 标记 runtime_states 中 pending 状态为 expired——LRU 淘汰时调用。

    对标 cancel_pending_state 的 DB 更新逻辑（status='rejected'），但用 'expired'
    区分淘汰（系统主动）vs 超时/取消（用户或超时）。
    失败仅记日志（best-effort）——DB 行即使未更新，future 已 cancel，用户确认时
    resolve_confirmation 会返回 False（token 不存在）。
    """
    try:
        await db.execute(
            "UPDATE runtime_states SET status='expired', resolved_at=?, resolution=? "
            "WHERE id=? AND status='pending'",
            (_time.time(), "lru_evicted", token),
        )
        await db.commit()
    except Exception:
        _log.debug("Failed to mark pending state %s as expired on LRU evict", token, exc_info=True)


def _safe_args_preview(args: dict, tool_name: str) -> str:
    """Safe argument preview: path for file tools, first 100 chars for others"""
    if tool_name in ("read_file", "write_file"):
        return f"path={args.get('path', '?')}"
    if tool_name == "run_command":
        return f"cmd={str(args.get('command', ''))[:80]}"
    return str(args)[:100]


def _resolve_pending_path(ws, data: dict) -> str | None:
    """从 preview 结果解析待确认操作的完整路径（统一 auto-approve / manual 分支）。

    preview 工具（write_file / delete_file / rename_file / delete_dir）返回的
    data["path"] 为相对 workspace 根的路径（如 "shared/xxx"，见
    system_tool_file_ops.py:219,358 的 `full.relative_to(get_workspace())`）。

    而 dispatch_confirm 的 handler（system_tool_confirm.py:65-67 等）期望
    payload["path"] 是绝对路径字符串，再用 `relative_to(get_workspace()/"shared")`
    取相对 shared 部分做安全校验。因此正确拼接是 `ws / data["path"]`——
    若误加 "shared" 前缀会得到 `ws/shared/shared/xxx` 双重 shared，handler 校验失败。

    返回 None 表示 data 无 path 字段（非文件类 confirmable 工具如 run_command）。
    """
    if not data or "path" not in data:
        return None
    return str((ws / data["path"]).resolve())


def _pre_execution_block(tool_name: str, tool, perm) -> dict | None:
    """执行前的两道拒绝：越权路径 + 权限档位。返回 None 表示放行。

    原本越权拒绝内联在 _validate_and_prepare 里，权限档位又要加一道；
    两道都是“拿到结果就直接返回”，收在这里后主流程只剩一个判断。
    权限档位的定义在 security/profiles.py，判决逻辑在 security/policy.py。
    """
    from security.permissions import PermissionLevel
    from tools.result import ToolResult

    if perm >= PermissionLevel.DIRECTORY_SCOPED:
        return ToolResult(
            success=False,
            error="操作超出授权范围",
            observation="操作被安全策略拒绝：目标路径超出工作区授权范围。",
            meta={"tool_name": tool_name, "permission_level": int(perm), "blocked": True},
        ).to_dict()

    from security.policy import decide_tool_call

    decision = decide_tool_call(
        tool_name,
        is_read_only=tool.is_read_only,
        groups=list(tool.groups or []),
        confirmable=tool.confirmable,
    )
    if not decision.allowed:
        return ToolResult(
            success=False,
            error=decision.reason,
            observation=decision.reason,
            meta={
                "tool_name": tool_name, "policy_blocked": True,
                "policy_code": decision.code, "policy_kind": decision.kind,
            },
        ).to_dict()
    return None


def _auto_file_checkpoint(tool_name: str, arguments: dict) -> str | None:
    """写/删/改名前存一份副本；失败不阻塞操作，但返回可展示的告警文案。

    P0-3 (2026-07-04): 捕获 CheckpointError 而非吞掉所有异常——失败时记录 warning
    并在 result meta 中标注 checkpoint_warning，前端展示红色 toast 通知用户"无法撤销"。
    """
    file_path = arguments.get("path", "")
    if tool_name not in ("write_file", "delete_file", "rename_file") or not file_path:
        return None
    try:
        from services.infra.file_checkpoint import FileCheckpoint
        FileCheckpoint().save(file_path, label=f"before_{tool_name}")
        return None
    except Exception as exc:
        # 检查点失败不阻塞写操作（用户意图是写），但记录 warning 供前端展示
        _log.warning("Checkpoint failed for %s on %s: %s", tool_name, str(file_path)[:60], exc)
        return f"checkpoint failed: {exc}"


async def _validate_and_prepare(
    self, tool_name: str, arguments: dict, agent_id: str | None,
) -> tuple[dict | None, dict | None]:
    """Phase 48 T3-9: 工具查找 + 权限分类 + 白名单 + LLM 安全 + Rules + PreToolUse Hook。

    Returns: (tool, block_result)——block_result 为 None 表示通过，否则为 ToolResult dict。
    """
    from hooks.registry import run_pre_tool_hooks
    from tools.result import ToolResult
    from security.permissions import classify_permission, PermissionLevel
    from tools.registry import record_tool_usage

    all_tools = get_all_tools()
    tool = all_tools.get(tool_name)
    if not tool:
        # CodeWhale 借鉴: 5 级工具名模糊解析
        resolved = resolve_tool_name(tool_name)
        if resolved and resolved != tool_name:
            tool = all_tools.get(resolved)
            if tool:
                _log.info("Tool name resolved: %r → %r", tool_name, resolved)
                tool_name = resolved  # 更新为解析后的名字供后续使用
    if not tool:
        raise ValueError(f"Tool {tool_name} not found")
    # Phase 33: 记录工具使用频率
    record_tool_usage(tool_name)

    # ---- v56: 中央权限分类器（七级权限模型） ----
    perm = classify_permission(
        tool_name, tool.is_read_only, tool.is_concurrency_safe, arguments, agent_id or "",
    )
    blocked = _pre_execution_block(tool_name, tool, perm)
    if blocked is not None:
        return None, blocked

    # ---- 硬约束 1: Agent 权限检查 ----
    # Yuncat 原生 agent 是受信宿主，不受这张表约束：内置工具的 agents 列表成文于
    # hermes 为主 agent 的时代，未随 8/29「强原生」重构更新——若原生也按表校验，
    # 其全部内置工具调用都会 PermissionError（实测 list_dir/read_file/run_command
    # 全灭，错误被 loop 的 except 吞成 {"error": ...} 静默失败）。原生 agent 的
    # 真实安全边界是上方权限分类器 + confirmable 审批 + 沙箱，而非这张表。
    # 外部 agent（hermes/openclaw/codex/claude）仍按各自 agents 列表严格把关。
    if agent_id and agent_id != YUNCAT_AGENT_ID and agent_id not in tool.agents:
        raise PermissionError(f"Agent {agent_id} not allowed: {tool_name}")

    # ---- 硬约束 2: 工具白名单检查 ----
    if tool_name == "run_command":
        from tools.builtin.system_tools import is_safe_command
        cmd = arguments.get("command", "")
        if not is_safe_command(cmd):
            return None, ToolResult(
                success=False, error="命令不在白名单中或包含禁用字符",
                observation="命令被安全策略拒绝。请使用 write_file 生成脚本后执行。",
                meta={"tool_name": tool_name, "blocked": True},
            ).to_dict()
        # Phase OH.2: LLM Security Analyzer — flash 模型对命令参数做语义安全评分
        try:
            from services.llm_security import analyze_command_safety
            risk = await analyze_command_safety(cmd)
            if risk.get("level") == "HIGH":
                return None, ToolResult(
                    success=False,
                    error=f"LLM 安全分析: 高风险 — {risk.get('reason', '命令可能造成不可逆影响')}",
                    observation=f"命令被 LLM 安全分析拦截 (风险级别: HIGH)。理由: {risk.get('reason', '')}",
                    meta={"tool_name": tool_name, "llm_security_blocked": True, "risk": risk},
                ).to_dict()
            elif risk.get("level") == "MEDIUM":
                _log.warning("LLM security: MEDIUM risk for command '%s': %s", cmd[:80], risk.get("reason", ""))
        except Exception:
            _log.debug("LLM security analysis skipped", exc_info=True)

    # ---- 硬约束 3: Rules 拦截 ----
    if self.rules_service:
        rules = await self.rules_service.get_enabled_rules()
        trigger_key = f"tool:{tool_name}"
        for rule in rules:
            triggers = rule.get("trigger_on") or []
            if trigger_key in triggers:
                if rule["action"] == "require_confirmation" and not tool.confirmable:
                    raise PermissionError(
                        f"Rule '{rule['description']}' requires confirmation for {tool_name}"
                    )

    # ---- v40: PreToolUse Hook 拦截链 ----
    hook_block = await run_pre_tool_hooks(tool_name, arguments, agent_id or "")
    if hook_block:
        return None, ToolResult(
            success=False,
            error=hook_block.get("reason", "操作被 Hook 拦截"),
            observation=hook_block.get("reason", "操作被安全策略拦截"),
            meta={"tool_name": tool_name, "hook_blocked": True},
        ).to_dict()

    return tool, None


async def _run_tool_execution(
    self, tool, tool_name: str, arguments: dict,
    agent_id: str | None, task_id: str | None, _t0: float,
) -> tuple[dict, bool]:
    """Phase 48 T3-9: 事件发布 + 检查点 + 串行锁 + 超时执行。

    Returns: (result, ok)——ok=False 表示超时，应直接返回 result 跳过 post-hooks。
    异常时运行 error_hooks 后 re-raise（保持原行为）。
    """
    from hooks.registry import run_error_hooks
    from tools.result import ToolResult
    from context import set_current_agent_id, reset_current_agent_id

    # P0 fix: push tool_start event for real-time visibility
    if task_id:
        try:
            from services.event_bus import get_event_bus
            await get_event_bus().publish("tool_start", {
                "task_id": task_id, "agent_id": agent_id or "",
                "tool_name": tool_name,
                "arguments_preview": _safe_args_preview(arguments, tool_name),
            })
        except Exception:
            _log.debug("Unexpected error", exc_info=True)

    # Phase F: 自动文件检查点 — write/delete/rename 前保存副本
    # P0-3 (2026-07-04): 失败不阻塞写操作，但返回可展示的告警文案（前端红 toast）。
    _checkpoint_warning = _auto_file_checkpoint(tool_name, arguments)

    # Phase 47 P7.1: per-tool 超时（对标 Claude Code Bash 120s）
    # P1-1: tool_timeout 为 None 时使用全局默认兜底——防止无超时工具永久阻塞 agent 循环
    _timeout = tool.tool_timeout if tool.tool_timeout is not None else TOOL_DEFAULT_TIMEOUT_SEC
    _agent_token = set_current_agent_id(agent_id)
    try:
        # Phase C: 预算硬计数——harness attempt 上下文内生效，非 harness 场景 no-op。
        # 超限返回与超时同构的失败 ToolResult（ok=False 跳过 post-hooks）。
        try:
            from harness.budget_manager import BudgetExceeded as _BudgetExceeded
            from harness.budget_manager import count_tool_invocation

            count_tool_invocation()
        except _BudgetExceeded as _be:
            _budget_meta = {
                "tool_name": tool_name, "budget_exceeded": True,
                "budget_type": _be.budget_type, "limit": _be.limit, "current": _be.current,
            }
            if _checkpoint_warning:
                _budget_meta["checkpoint_warning"] = _checkpoint_warning
            return ToolResult(
                success=False,
                error=(
                    f"工具 '{tool_name}' 触发预算上限"
                    f"（{_be.budget_type}: {_be.current}/{_be.limit}）"
                ),
                observation="预算超限，本次 attempt 已停止——Supervisor 可重派或放宽预算。",
                meta=_budget_meta,
            ).to_dict(), False
        # 私有元数据键（_snapshot_pre_id 等）只给 hook 用，不能混进工具函数签名
        call_args = {k: v for k, v in arguments.items() if not k.startswith("_")}
        if not tool.is_concurrency_safe:
            lock = self._get_serial_lock(tool_name)
            async with lock:
                result = await asyncio.wait_for(tool.func(**call_args), timeout=_timeout)
        else:
            result = await asyncio.wait_for(tool.func(**call_args), timeout=_timeout)
    except asyncio.TimeoutError:
        _timeout_meta = {"tool_name": tool_name, "timeout": True, "timeout_sec": _timeout}
        if _checkpoint_warning:
            _timeout_meta["checkpoint_warning"] = _checkpoint_warning
        return ToolResult(
            success=False,
            error=f"工具 '{tool_name}' 执行超时（{_timeout}s）",
            observation=f"工具 {tool_name} 超时。请简化参数或分步执行。",
            meta=_timeout_meta,
        ).to_dict(), False
    except Exception as e:
        await run_error_hooks(tool_name, arguments, e, agent_id or "")
        raise
    finally:
        reset_current_agent_id(_agent_token)

    # P0-3: 将检查点失败 warning 注入 result meta 供前端展示红色 toast
    if _checkpoint_warning and isinstance(result, dict):
        _result_meta = result.get("meta")
        if not isinstance(_result_meta, dict):
            _result_meta = {}
            result["meta"] = _result_meta
        _result_meta["checkpoint_warning"] = _checkpoint_warning

    return result, True


async def _record_harness_turn(
    task_id: str, tool_name: str, arguments: dict, result,
) -> None:
    """把这次工具调用录制下来（供回放对比）——只有 YUNCAT_HARNESS_RECORD=1 时才落盘。

    录制是诊断辅助：失败只记日志，绝不影响工具结果。
    """
    try:
        from context import get_app
        app = get_app()
        recorder = getattr(app.state, "harness_recorder", None) if app else None
        if recorder is None:
            return
        await recorder.record_turn(task_id, {
            "tool_name": tool_name,
            "arguments": {k: v for k, v in (arguments or {}).items()
                          if not k.startswith("_")},
            "result": result,
        })
    except Exception as exc:
        _log.warning("harness 录制失败（不影响工具结果）: %s", exc)


async def _finalize_execution(
    self, tool_name: str, arguments: dict, result,
    agent_id: str | None, task_id: str | None, _t0: float,
) -> None:
    """Phase 48 T3-9: PostToolUse Hook + tool_done 事件 + analytics 持久化。"""
    from hooks.registry import run_post_tool_hooks
    # ---- v40: PostToolUse Hook 审计 ----
    await run_post_tool_hooks(tool_name, arguments, result, agent_id or "")
    if task_id:  # Harness 录制（未启用时 record_turn 直接返回 None，零成本）
        await _record_harness_turn(task_id, tool_name, arguments, result)

    # P0 fix: tool_done event for real-time visibility
    if not task_id:
        return
    try:
        from services.event_bus import get_event_bus
        from tools.registry import get_concurrency
        success = result.get("success", True) if isinstance(result, dict) else True
        # 2026-10-07：_t0 是 monotonic，这里原用 wall-clock 相减会得到 epoch 毫秒（实测 17 亿），耗时统计全错。
        _duration_ms = int((_time.monotonic() - _t0) * 1000) if _t0 > 0 else 0
        _in_size = len(_json.dumps(arguments, ensure_ascii=False)) if arguments else 0
        _out_size = len(_json.dumps(result, ensure_ascii=False))
        _concurrency = get_concurrency(tool_name)
        # Phase 45 D.8: 标准化 8 字段
        await get_event_bus().publish("tool_done", {
            "tool_name": tool_name,
            "duration_ms": _duration_ms,
            "success": success,
            "input_size_chars": _in_size,
            "output_size_chars": _out_size,
            "concurrency_mode": _concurrency,
            "retry_count": 0,  # Phase 45 D.8: 当前未实现自动重试计数，预留字段
            "cache_hit": None,  # 无缓存机制，预留字段
            "task_id": task_id, "agent_id": agent_id or "",
        })
        # Phase 46 fix: persist to analytics_events
        from context import get_db
        _db = get_db()
        if _db:
            await _db.execute(
                "INSERT INTO analytics_events(event_type, event_data, created_at) "
                "VALUES(?,?,?)",
                ("tool_done", _json.dumps({
                    "tool_name": tool_name, "duration_ms": _duration_ms,
                    "success": success, "concurrency_mode": _concurrency,
                }), int(_now)),
            )
            await _db.commit()
    except Exception:
        _log.debug("Unexpected error", exc_info=True)


def _auto_approve_reason(auto_approve: bool, session_allowed: bool) -> str:
    """自动放行的原因（写进审计日志）"""
    if auto_approve:
        return "auto_approve"
    return "session_allow" if session_allowed else "standing_grant"


async def _audit_auto_approval(
    db, agent_id, tool_name: str, arguments: dict, executed: dict, reason: str, task_id,
) -> None:
    """自动放行留审计记录（phase=auto:*）——best-effort，写失败不影响主流程"""
    try:
        from repositories.audit_repo import log_audit

        await log_audit(
            db, agent_id or "", tool_name, arguments,
            success=bool(executed.get("success", True)),
            phase=f"auto:{reason}", task_id=task_id or "",
        )
    except Exception:
        _log.debug("auto-approve audit log failed", exc_info=True)


class ToolService:
    """副作用唯一入口——所有工具调用必须经过 execute()"""

    def __init__(self, rules_service=None):
        self.rules_service = rules_service
        self._serial_locks: dict[str, asyncio.Lock] = {}

    def _get_serial_lock(self, tool_name: str) -> asyncio.Lock:
        """获取工具串行锁（懒初始化）"""
        if tool_name not in self._serial_locks:
            self._serial_locks[tool_name] = asyncio.Lock()
        return self._serial_locks[tool_name]

    async def execute(
        self, tool_name: str, arguments: dict, agent_id: str | None = None,
        task_id: str | None = None, mode: str = "agent",
    ) -> dict:
        """v56: classify_permission → 三级硬约束 → Hook 拦截链 → 串行锁 → 执行 → ToolResult

        Phase 48 T3-9: 拆分为 3 个模块级子函数（_validate_and_prepare /
        _run_tool_execution / _finalize_execution），本方法负责模式守卫 + 编排。
        2026-10-21: 执行期挂 task_id 上下文——此前 hook/审计/快照拿不到任务身份，
        快照表 task_id 落成随机 uuid，「撤销文件变更」整体失效（详见 hooks/registry.py）。
        """
        from context import reset_current_task_id, set_current_task_id
        from services.tools.tool_guards import capability_guard, mode_guard

        # 执行层硬约束（模式 + 能力策略）——都在工具真正执行前 fail-closed 拦截
        blocked = mode_guard(tool_name, mode) or capability_guard(tool_name)
        if blocked:
            return blocked

        # 任务身份传播：hook / 审计 / 快照从这里取 task_id（不再各自造随机 id）
        _task_token = set_current_task_id(task_id or "")
        try:
            # 权限分类 + 白名单 + LLM 安全 + Rules + PreToolUse Hook
            tool, blocked = await _validate_and_prepare(self, tool_name, arguments, agent_id)
            if blocked:
                return blocked

            # 事件发布 + 检查点 + 串行锁 + 超时执行（异常时 run_error_hooks 后 re-raise）
            _t0 = _time.monotonic()
            result, ok = await _run_tool_execution(
                self, tool, tool_name, arguments, agent_id, task_id, _t0,
            )
            if not ok:
                return result  # 超时——跳过 post-hooks（与原行为一致）

            # PostToolUse Hook + tool_done 事件 + analytics 持久化
            await _finalize_execution(
                self, tool_name, arguments, result, agent_id, task_id, _t0,
            )
            return result
        finally:
            reset_current_task_id(_task_token)

    async def execute_confirmable(
        self, db, tool_name: str, arguments: dict, agent_id: str | None = None,
        auto_approve: bool = False, task_id: str = "", project_id: str = "",
    ) -> dict:
        """执行 confirmable 工具的预览阶段 + 生成确认令牌。

        auto_approve=True → 跳过确认直接执行（开发者模式 / auto-approve 设置）。
        """
        result = await self.execute(tool_name, arguments, agent_id, task_id=task_id or None)  # 透传任务号：快照/审计靠它认任务

        meta = result.get("meta", {})
        if meta.get("phase") != "preview" or not result.get("success"):
            return result

        # 修复 1.6 (F-031): session 级 "Always allow" 也跳过确认——用户已在本 session
        # 批准过该工具，无需重复弹窗。对标 Cursor "Always allow in this workspace"。
        # standing grant：持久化授权（跨进程重启仍有效），用于 cron / 重复任务一次审批
        # 后长期复用；过期或撤销立即失效（fail-closed）。对标 OpenClaw scoped standing grants。
        _auto_reason = _auto_approve_reason(auto_approve, is_session_allowed(tool_name))
        if (
            auto_approve
            or is_session_allowed(tool_name)
            or await is_standing_granted(
                db, tool_name, agent_id, project_id, arguments)
        ):
            from tools.builtin.system_tools import create_pending_state, dispatch_confirm
            state_type = _STATE_TYPE_MAP.get(tool_name, f"pending_{tool_name}")
            payload = dict(arguments)
            data = result.get("data", {})
            if data and "path" in data:
                from security.sandbox import get_workspace
                ws = get_workspace()
                resolved = _resolve_pending_path(ws, data)
                if resolved:
                    payload["path"] = resolved
            token = await create_pending_state(db, state_type, payload)
            try:
                executed = await dispatch_confirm(db, token)
                executed["_auto_approved"] = True
                await _audit_auto_approval(db, agent_id, tool_name, arguments, executed, _auto_reason, task_id)
                return executed
            except Exception as e:
                # P0-3 修复: 原代码 return result（preview result，success=True），
                # 误导 agent 认为副作用已执行。改为明确标记失败，让 agent 知道写入未发生。
                _log.warning("auto_approve dispatch_confirm failed: %s", e, exc_info=True)
                return {
                    **result,
                    "success": False,
                    "error": f"auto_approve execution failed: {e}",
                    "auto_approved": True,
                    "phase": "execute",
                }
        else:
            from tools.builtin.system_tools import create_pending_state_with_expiry
        # 映射 tool_name → handler state_type（Batch B M6: 使用模块级 _STATE_TYPE_MAP）
        state_type = _STATE_TYPE_MAP.get(tool_name, f"pending_{tool_name}")
        payload: dict = {}
        for k, v in arguments.items():
            payload[k] = v
        if task_id:
            payload["task_id"] = task_id
        # 附加完整路径
        data = result.get("data", {})
        if data and "path" in data:
            from security.sandbox import get_workspace
            ws = get_workspace()
            resolved = _resolve_pending_path(ws, data)
            if resolved:
                payload["path"] = resolved

        title = f"{tool_name}: {arguments.get('path') or arguments.get('command', '')[:60]}"
        token, confirm_expires_at = await create_pending_state_with_expiry(
            db, state_type, payload,
            title=title,
            task_id=task_id or "",
        )
        # Phase 106: 发送统一待审批通知
        try:
            from context import get_app
            app = get_app()
            nc = getattr(app.state, "notification_center", None) if app else None
            if nc is not None:
                await nc.notify(
                    title="待审批",
                    body=title,
                    type="approval_pending",
                    level="warning",
                    task_id=task_id or "",
                    link=f"/chat?task={task_id or ''}",
                )
        except Exception:
            _log.debug("approval notification failed", exc_info=True)
        result["confirm_token"] = token
        result["_awaiting_confirmation"] = True
        # Batch 4b: 确认 token 的过期时间（epoch 秒）——confirmable 聚合层取出后随 WS 推送
        result["_confirm_expires_at"] = confirm_expires_at
        # Phase 47: 创建 Future——工具循环阻塞等待用户确认
        # Phase 48 T3-16: asyncio.get_event_loop() Python 3.12 弃用 → get_running_loop()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        _add_pending_confirmation(token, future, db=db)  # Phase 48 T3-1: LRU + 上限 / Task 3.5: 传 db 同步淘汰时更新
        # 挂载 future 到 result，让 agent_tool_loop 知道需要 await
        result["_confirm_future"] = future
        return result


async def resolve_confirmation(token: str, executed_result: dict) -> bool:
    """解析确认 Future——由 HTTP/WS 确认端点调用。
    返回 True 表示成功解析，False 表示 token 不存在（已超时或已处理）。
    """
    future = _pending_confirmations.pop(token, None)
    if future and not future.done():
        future.set_result(executed_result)
        return True
    return False


def cancel_pending_confirmation(token: str) -> None:
    """Phase 48 T3-3: 取消待确认 future（超时/异常时清理，防泄漏）。

    agent_tool_loop 的 wait_for 超时后调用，从 _pending_confirmations pop 并 cancel
    future（否则 future 留在 LRU 中占用槽位直到被淘汰，且 await 的协程未释放）。
    """
    future = _pending_confirmations.pop(token, None)
    if future and not future.done():
        future.cancel()


async def cancel_pending_state(token: str, db=None) -> None:
    """A1: 取消待确认状态——同时 cancel future + 原子标记 DB 为 rejected。

    竞态 1 修复：原 cancel_pending_confirmation 只 cancel future，DB 行仍为 pending，
    导致超时后 60s 窗口内用户仍可通过 Inbox 批准并执行副作用。本函数同步将 DB
    行标记为 rejected，关闭窗口。

    db=None 时仅 cancel future（向后兼容）。
    """
    cancel_pending_confirmation(token)
    if db is None:
        return
    try:
        import time as _t
        await db.execute(
            "UPDATE runtime_states SET status='rejected', resolved_at=?, resolution=? "
            "WHERE id=? AND status='pending'",
            (_t.time(), "timeout/cancelled", token),
        )
        await db.commit()
    except Exception:
        _log.debug("Failed to mark pending state %s as rejected on cancel", token, exc_info=True)


async def resolve_confirmation_batch(
    tokens: list[str], result_factory: callable,
) -> list[tuple[str, bool]]:
    """Batch 3.2: 批量解析确认 Future。

    对 tokens 逐个调用 resolve_confirmation；result_factory(token) 生成每个 token 对应
    的结果。返回 [(token, resolved), ...] 列表，便于调用方追踪哪些 token 已唤醒。
    """
    resolved: list[tuple[str, bool]] = []
    for token in tokens:
        result = result_factory(token)
        ok = await resolve_confirmation(token, result)
        resolved.append((token, ok))
    return resolved


async def cancel_pending_confirmation_batch(tokens: list[str], db=None) -> None:
    """Batch 3.2: 批量取消待确认状态。

    逐个调用 cancel_pending_state，确保每个 token 的 future 和 DB 行都被清理。
    """
    for token in tokens:
        await cancel_pending_state(token, db=db)
