"""routes/tools.py —— 工具二阶段确认 + HITL 用户决策 + 工具确认模式状态"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/v1", tags=["tools"])
_log = logging.getLogger("yuncat.tools")


@router.post("/tools/preview")
async def preview_tool(payload: dict, request: Request) -> dict:
    """触发工具二阶段确认——执行预览 + 生成 token + WS 推送确认弹窗"""
    task_id = payload.get("task_id")
    tool_name = payload.get("tool_name")
    arguments = payload.get("arguments", {})
    if not task_id or not tool_name:
        raise HTTPException(400, "Missing task_id or tool_name")

    result = await request.app.state.tool_service.execute_confirmable(
        request.app.state.db, tool_name, arguments,
    )
    # execute_confirmable 会把内部对象（_confirm_future 是 asyncio.Future）一并放进返回值，
    # 供进程内工具循环 await；HTTP 响应必须先把这类下划线前缀的内部字段剥掉，
    # 否则 Pydantic 序列化 Future 直接 500（2026-09-23 实测）。
    if isinstance(result, dict):
        result = {k: v for k, v in result.items() if not k.startswith("_")}
    token = result.get("confirm_token")
    if token and result.get("success"):
        preview = result.get("data", {})
        if result.get("meta", {}).get("size"):
            preview["size"] = result["meta"]["size"]
        await request.app.state.agent_service.push_tool_confirm(
            task_id, tool_name, preview, token,
        )
    return result


@router.post("/tools/confirm")
async def confirm_tool(payload: dict, request: Request) -> dict:
    from tools.builtin.system_tools import dispatch_confirm

    token = payload.get("confirm_token")
    if not token:
        raise HTTPException(400, "Missing confirm_token")
    try:
        result = await dispatch_confirm(request.app.state.db, token)
        # Phase 47: 解析 confirm Future——唤醒 agent_tool_loop 中等待的协程
        from services.tools.tool_service import resolve_confirmation
        await resolve_confirmation(token, result)
        # 修复 1.6 (F-031): 用户勾选 "Always allow" → 加入 session 级允许列表（本 session 立即可用），
        # 同时持久化一条 standing grant（跨进程重启仍有效，被 execute_confirmable 的
        # is_standing_granted 命中后跳过确认弹窗）。两者互补：内存态重启即失效，持久态可长期复用。
        if payload.get("remember") and result.get("success"):
            from services.tools.tool_service import add_session_allowed_tool
            from services.standing_grants import persist_remember
            tool_name = payload.get("tool_name", "")
            if tool_name:
                add_session_allowed_tool(tool_name)
                await persist_remember(request.app.state.db, tool_name)
        if hasattr(request.app.state, "feedback_learner"):
            tool_name = payload.get("tool_name", "")
            asyncio.create_task(
                request.app.state.feedback_learner.record_feedback(
                    "human_accepted", payload.get("task_id", ""),
                    context=tool_name,
                )
            ).add_done_callback(
                lambda _t: _log.warning("record_feedback failed: %s", _t.exception())
                if _t.exception() else None
            )
        return result
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception:
        _log.exception("dispatch_confirm failed for token %s", token[:8])
        raise HTTPException(500, "Internal error processing confirmation")


@router.post("/hitl/respond")
async def hitl_respond(payload: dict, request: Request) -> dict:
    """用户对 HITL 审批请求的响应——唤醒等待中的工作流节点"""
    run_id = payload.get("run_id")
    node_id = payload.get("node_id")
    action = payload.get("action", "reject")
    if action not in ("approve", "reject", "retry"):
        raise HTTPException(400, "Invalid action: must be approve/reject/retry")
    note = payload.get("note", "")
    if not run_id or not node_id:
        raise HTTPException(400, "Missing run_id or node_id")

    hitl_executor = request.app.state.hitl_executor
    ok = hitl_executor.submit_decision(run_id, node_id, {
        "action": action, "note": note,
    })
    if not ok:
        raise HTTPException(404, "Decision not found or already processed")
    return {"status": "ok"}


@router.get("/security/preset")
async def get_security_preset(request: Request) -> dict:
    """当前权限档位（权限 + 审批打包）以及全部可选预设"""
    from security.presets import describe_current

    return describe_current()


@router.post("/security/preset")
async def update_security_preset(payload: dict, request: Request) -> dict:
    """切到某个预设；权限档与审批方式一次写好，不允许上层只改一个"""
    from security.presets import apply_preset

    try:
        return await apply_preset(request.app.state.settings_service, payload.get("preset"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/tool-confirm-status")
async def tool_confirm_status(request: Request = None) -> dict:
    """返回当前工具确认模式——帮助用户理解 Yuncat vs Hermes 两条确认路径"""
    from security.profiles import describe_active_profile
    settings = await request.app.state.settings_service.get_all()
    return {
        "path": "yuncat",
        "explanation": (
            "Yuncat 工具确认路径："
            "文件写入/命令执行需经 ToolService → 弹窗确认 → 用户点击后执行。"
            "Hermes 内置沙箱操作不经过此路径——Agent 运行时自行处理文件变更。"
        ),
        "security_profile": describe_active_profile(),
        "auto_approve": settings.get("auto_approve") == "true",
        "note": (
            "若 auto_approve=true，Yuncat 工具调用将跳过确认弹窗自动执行。"
            "Hermes 内置工具调用不受此开关影响——Agent 自己在沙箱中操作。"
        ) if settings.get("auto_approve") == "true" else "",
    }


# ---- Bridge 桥接审批 ----

@router.post("/bridge/approval-request/{approval_id}/decide")
async def bridge_approval_decide(approval_id: str, payload: dict, request: Request = None) -> dict:
    """前端用户对 Hermes Bridge 发起的 OC 工具审批做出决策。

    Bridge 通过 WS bridge_tool_approval 消息触发前端弹窗，
    用户点击批准/拒绝后调用此端点。审批通过后 Yuncat 代理执行工具。
    """
    approved = payload.get("approved", False)
    _log.info("Bridge approval %s: approved=%s", approval_id, approved)

    # 通知 Hermes Bridge（如果存在 pending 审批）
    agent_svc = request.app.state.agent_service
    if hasattr(agent_svc, "_bridge_pending"):
        pending = agent_svc._bridge_pending.pop(approval_id, None)
        if pending and approved:
            # 通过 ToolService 执行工具
            ts = request.app.state.tool_service
            try:
                result = await ts.execute(
                    pending["tool_name"], pending["arguments"],
                    task_id=approval_id,
                )
                pending["future"].set_result(result)
            except Exception as e:
                pending["future"].set_exception(e)
        elif pending:
            pending["future"].set_result({"success": False, "error": "用户拒绝"})

    return {"status": "ok", "approved": approved}


# ---- 工具策略 (Phase 51-B) ----

@router.get("/tools/policy")
async def get_tool_policy() -> dict:
    """返回当前工具策略配置。"""
    from services.infra.tool_policy import get_policy
    policy = get_policy()
    return {
        "profile": policy.profile,
        "allow": policy.allow,
        "deny": policy.deny,
        "byProvider": {
            provider: {"profile": p.profile, "allow": p.allow, "deny": p.deny}
            for provider, p in policy.by_provider.items()
        },
        "groups": policy.group_map,
    }


@router.post("/tools/policy/reload")
async def reload_tool_policy() -> dict:
    """重新加载工具策略 YAML。"""
    from services.infra.tool_policy import reload_policy
    policy = reload_policy()
    return {
        "profile": policy.profile,
        "allow": policy.allow,
        "deny": policy.deny,
        "byProvider": list(policy.by_provider.keys()),
    }


# ---- 检查点恢复 ----

@router.post("/tools/checkpoint_restore")
async def http_checkpoint_restore(payload: dict, request: Request = None) -> dict:
    """HTTP 端点: 从检查点恢复文件——前端 FileTree 历史恢复"""
    from services.infra.file_checkpoint import FileCheckpoint
    from security.sandbox import validate_path  # Phase 48 T1-10: 路径校验防穿越
    path = payload.get("path", "")
    checkpoint_id = payload.get("checkpoint_id", "")
    if not path or not checkpoint_id:
        raise HTTPException(400, "Missing 'path' or 'checkpoint_id'")
    try:
        validate_path(path)
    except PermissionError:
        raise HTTPException(403, "Path outside workspace")
    ck = FileCheckpoint()
    ok = ck.restore(path, checkpoint_id)
    if not ok:
        raise HTTPException(404, f"Checkpoint '{checkpoint_id}' not found for '{path}'")
    return {"status": "ok"}
