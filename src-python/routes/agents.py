"""routes/agents.py —— Agent 管理 + 多 Agent 讨论引擎 + 角色 CRUD + 版本检测"""

import asyncio
import logging
import uuid as _uuid

from fastapi import APIRouter, HTTPException, Request

from config import DEFAULT_AGENT_ID, VERSION_PROBE_TIMEOUT_SEC

router = APIRouter(prefix="/api/v1", tags=["agents"])
_log = logging.getLogger("yuncat.agents")


# ---- Agent 管理 ----

@router.get("/agents")
async def list_agents(request: Request) -> list[dict]:
    return await request.app.state.agent_service.list_agents()


@router.get("/agents/{agent_id}/status")
async def agent_status(agent_id: str, request: Request) -> dict:
    """返回单个 Agent 后端的运行时状态——供 Settings UI 使用"""
    adapter = request.app.state.adapters.get(agent_id)
    if not adapter:
        raise HTTPException(404, f"Agent {agent_id} not found")
    # Phase 48-F: 服务自描述——adapter 自带 _port/port_hint，无端口回退到 0（修复 hermes 错误返回 18789 的 Bug）
    port = getattr(adapter, '_port', 0) or getattr(adapter, 'port_hint', 0)
    # OC adapter 通过 __init__ 注入 OC_PORT_HINT，其他 adapter 无 port_hint 时返回 0（表示无 HTTP 端口）
    return {
        "status": "running" if adapter._ready.is_set() else "stopped",
        "port": port,
        "agent_id": agent_id,
    }


@router.post("/agents/{agent_id}/start")
async def start_agent(agent_id: str, request: Request) -> dict:
    await request.app.state.agent_service.start_agent(agent_id)
    return {"status": "ok"}


@router.post("/agents/{agent_id}/stop")
async def stop_agent(agent_id: str, request: Request) -> dict:
    await request.app.state.agent_service.stop_agent(agent_id)
    return {"status": "ok"}


@router.post("/agents/{agent_id}/restart")
async def restart_agent(agent_id: str, request: Request) -> dict:
    """Task 6.15: 重启 Agent——stop + start 原子序列。

    对标 Docker ``docker restart`` / systemd ``systemctl restart``：
    先 stop（终止子进程 + 清 _ready），再 start（重新 spawn + 等待就绪）。
    """
    svc = request.app.state.agent_service
    await svc.stop_agent(agent_id)
    await svc.start_agent(agent_id)
    return {"status": "ok"}


@router.post("/agents/{agent_id}/enable")
async def enable_agent(agent_id: str, request: Request) -> dict:
    """Phase X: 启用 Agent——写入 settings ``agent_enabled:{id}=true``。

    启用后该 Agent 才会出现在任务调用的可选列表中（默认仅原生 Yuncat 启用）。
    """
    await request.app.state.settings_service.save(f"agent_enabled:{agent_id}", "true")
    return {"status": "ok", "enabled": True}


@router.post("/agents/{agent_id}/disable")
async def disable_agent(agent_id: str, request: Request) -> dict:
    """Phase X: 禁用 Agent——写入 settings ``agent_enabled:{id}=false``。

    原生 Yuncat 不可禁用（始终启用）；禁用后该 Agent 不会出现在任务调用的可选列表。
    """
    if agent_id == DEFAULT_AGENT_ID:
        raise HTTPException(400, "原生 Yuncat Agent 不可禁用")
    await request.app.state.settings_service.save(f"agent_enabled:{agent_id}", "false")
    return {"status": "ok", "enabled": False}


@router.get("/agents/{agent_id}/profile")
async def get_agent_profile(agent_id: str, request: Request) -> dict:
    """Phase 51-F: 获取统一 AgentProfile（能力、MCP、Skill、记忆来源）。"""
    from services.agent_profile import load_agent_profile

    if agent_id not in ("yuncat", "openclaw", "codex", "hermes"):
        raise HTTPException(400, "Unsupported agent_id")
    profile = await load_agent_profile(agent_id, request.app.state)
    return profile.to_dict()


@router.get("/agents/check-versions")
async def check_agent_versions(request: Request = None) -> dict:
    """Phase 18: 检测已安装 Agent 的版本"""
    import subprocess

    results: dict[str, dict] = {}
    # Hermes
    try:
        r = await asyncio.to_thread(
            subprocess.run, ["hermes", "--version"], capture_output=True, text=True, timeout=VERSION_PROBE_TIMEOUT_SEC
        )
        v = r.stdout.strip().split()[-1] if r.stdout else ""
        results["hermes"] = {"installed": r.returncode == 0, "version": v or "unknown"}
    except Exception:
        results["hermes"] = {"installed": False, "version": ""}
    # OpenClaw
    try:
        r = await asyncio.to_thread(
            subprocess.run, ["openclaw", "--version"], capture_output=True, text=True, timeout=VERSION_PROBE_TIMEOUT_SEC
        )
        v = r.stdout.strip().split()[-1] if r.stdout else ""
        results["openclaw"] = {"installed": r.returncode == 0, "version": v or "unknown"}
    except Exception:
        results["openclaw"] = {"installed": False, "version": ""}
    return results


# ---- Phase 9: 多 Agent 讨论引擎 ----

@router.post("/deliberation/start")
def _require_max_for_deliberation(payload: dict) -> None:
    """多角色讨论属于 MAX 能力（多模型并行 + 多轮交叉审查，成本乘数很高）"""
    from config import is_max_level

    if not is_max_level(payload.get("capability_level", 0)):
        raise HTTPException(403, "多角色讨论属于 MAX 能力：请先在输入区打开 MAX 后再发起")


async def deliberation_start(payload: dict, request: Request) -> dict:
    """启动多 Agent 讨论。body: {prompt, roles, session_id?}"""
    _require_max_for_deliberation(payload)
    prompt = payload.get("prompt")
    roles = payload.get("roles", [])
    if not prompt or not roles:
        raise HTTPException(400, "Missing 'prompt' or 'roles'")
    from repositories import create_deliberation_task, complete_task, fail_task

    session_id = payload.get("session_id", str(_uuid.uuid4()))
    language = payload.get("language", "zh")
    adapters = request.app.state.agent_service.adapters
    agent_ids = [aid for aid, a in adapters.items() if a._ready.is_set()]
    agent_id = agent_ids[0] if agent_ids else (list(adapters.keys())[0] if adapters else "hermes")
    task_id = await create_deliberation_task(
        request.app.state.db, session_id, prompt, agent_id,
    )

    # Phase 49-D6: 注册用户参与状态——必须在 run_deliberation 启动前完成，
    # 否则 WS handler 收到 user_message/pause 时找不到对应 task 状态。
    deliberation_service = request.app.state.deliberation_service
    deliberation_service.register_task(task_id)

    async def _run_deliberation():
        try:
            result = await deliberation_service.run_deliberation(
                task_input=prompt, roles=roles, task_id=task_id,
                push_callback=_push_deliberation,
                language=language,
            )
            await complete_task(request.app.state.db, task_id,
                                result.get("consensus", ""))
        except asyncio.CancelledError:
            # Phase 49-R1: 用户取消——CancelledError 是 BaseException 子类，
            # 不会被上面的 except Exception 捕获。DB 状态由 cancel 接口单独更新。
            _log.info("Deliberation task %s cancelled by user", task_id)
            raise
        except Exception as e:
            _log.exception("Deliberation failed for task %s", task_id)
            await fail_task(request.app.state.db, task_id, str(e))
        finally:
            # Phase 49-D6: 无论成功/失败/取消/异常都清理状态——避免内存泄漏。
            deliberation_service.cleanup_task(task_id)

    async def _push_deliberation(tid: str, msg_type: str, payload: dict):
        for task_conns in request.app.state.agent_service._ws_connections.values():
            for ws_conn in task_conns:
                try:
                    await ws_conn.send_json({"type": msg_type, "task_id": tid, **payload})
                except Exception:
                    _log.debug("推送审议 WS 通知失败", exc_info=True)

    import asyncio

    t = asyncio.create_task(_run_deliberation())
    # Phase 49-R1: 保存 task 引用——供 cancel 接口取消
    deliberation_service.register_running_task(task_id, t)
    t.add_done_callback(lambda _t: _log.exception("deliberation failed", exc_info=_t.exception()) if _t.exception() else None)
    return {"task_id": task_id}


@router.post("/deliberation/{task_id}/cancel")
async def deliberation_cancel(task_id: str, request: Request) -> dict:
    """取消正在运行的讨论任务。

    用户点"返回对话"时调用——避免后端继续消耗 token 运行讨论。
    取消后 _run_deliberation 的 await 点抛出 CancelledError，
    finally 块清理 _user_messages / _pause_events / _running_tasks。
    """
    svc = getattr(request.app.state, "deliberation_service", None)
    if svc is None:
        raise HTTPException(503, "deliberation_service 未初始化")
    cancelled = svc.cancel_task(task_id)
    if not cancelled:
        # 任务可能已结束或不在此 service 中——幂等返回 ok
        _log.debug("deliberation_cancel: task %s not found or already done", task_id)
    # 标记 DB 任务状态为 cancelled（如果存在）
    try:
        from repositories import fail_task
        await fail_task(request.app.state.db, task_id, "cancelled by user")
    except Exception:
        _log.debug("deliberation_cancel: failed to update DB status for %s", task_id, exc_info=True)
    # 清理 service 状态（即使 task 已结束，也要确保 _user_messages 等被清理）
    svc.cleanup_task(task_id)
    return {"ok": True, "cancelled": cancelled}


@router.post("/deliberation/chain-collaborate")
async def chain_collaborate(payload: dict, request: Request) -> dict:
    """Phase 43 F3.2: 链式多后端协作 — vision→Architect→Developer→Reviewer"""
    task_input = payload.get("prompt", "")
    vision_inputs = payload.get("vision_inputs")
    if not task_input:
        raise HTTPException(400, "Missing 'prompt'")
    ds = request.app.state.deliberation_service
    result = await ds.chain_collaborate(task_input=task_input, vision_inputs=vision_inputs)
    return {"ok": True, **result}


# ---- Phase 9: 角色 CRUD ----

@router.get("/roles")
async def list_roles_endpoint(category: str | None = None, request: Request = None):
    from repositories import list_roles

    return await list_roles(request.app.state.db, category)


@router.post("/roles")
async def create_role_endpoint(payload: dict, request: Request) -> dict:
    from repositories import create_role

    rid = payload.get("id") or str(_uuid.uuid4())
    name = payload.get("name")
    prompt_template = payload.get("prompt_template")
    if not name or not prompt_template:
        raise HTTPException(400, "Missing 'name' or 'prompt_template'")
    await create_role(
        request.app.state.db, rid, name, prompt_template,
        description=payload.get("description", ""),
        category=payload.get("category", "custom"),
    )
    return {"role_id": rid, "status": "ok"}


@router.put("/roles/{role_id}")
async def update_role_endpoint(role_id: str, payload: dict, request: Request) -> dict:
    from repositories import update_role

    ok = await update_role(request.app.state.db, role_id, **payload)
    if not ok:
        raise HTTPException(404, "Role not found or no fields to update")
    return {"status": "ok"}


@router.delete("/roles/{role_id}")
async def delete_role_endpoint(role_id: str, request: Request) -> dict:
    from repositories import delete_role

    ok = await delete_role(request.app.state.db, role_id)
    if not ok:
        raise HTTPException(404, "Role not found or is built-in (cannot delete)")
    return {"status": "ok"}


# ---- Phase 31: Agent 工具桥接 API ----
# 从 main.py 提取（M1 拆分）

@router.get("/agents/{agent_id}/tools")
async def list_agent_tools(agent_id: str, request: Request) -> dict:
    """列出 Agent 可用工具：适配器动态列表 → Yuncat 注册表 → 空列表 + 说明

    2026-09-16: 原来适配器不支持列表就 400——原生 yuncat 是进程内 Agent，
    它的工具就在 Yuncat 注册表里，却因此拿不到自己的工具清单；外部 CLI（claude/codex）
    确实不提供清单，但应该返回空列表而不是 400——400 会让前端把它当成
    “操作失败”，用户看到报错而不是“这个 Agent 不提供”。
    """
    adapter = request.app.state.adapters.get(agent_id)
    if not adapter:
        raise HTTPException(404, f"Agent {agent_id} not found")
    if hasattr(adapter, "list_available_tools"):
        tools = await adapter.list_available_tools()
        return {"ok": True, "agent_id": agent_id, "tools": tools, "source": "adapter"}

    from tools.registry import list_tools

    registry_tools = list_tools(agent_id)
    if registry_tools:
        return {"ok": True, "agent_id": agent_id, "tools": registry_tools, "source": "registry"}
    return {
        "ok": True, "agent_id": agent_id, "tools": [], "source": "none",
        "note": f"{agent_id} 是外部 CLI，不提供工具清单",
    }


@router.post("/agents/{agent_id}/tools/{tool_name}/invoke")
async def invoke_agent_tool(agent_id: str, tool_name: str, request: Request) -> dict:
    """通过 Agent 调用工具。

    Phase 48 T1-6: 闭合架构铁律 #1 绕过——
    Yuncat-registered 工具必须走 ToolService.execute()（获得审计/串行锁/
    确认流/权限分类）；agent-native 工具（OpenClaw ClawHub 5700+ / HA 原生
    47+，不在 Yuncat registry）fallback 到 adapter.execute_tool()，由后端
    自身沙箱负责安全。这是多后端主流做法（LangChain 各 agent 自管原生工具，
    orchestrator 不强加审计）。
    """
    adapter = request.app.state.adapters.get(agent_id)
    if not adapter:
        raise HTTPException(404, f"Agent {agent_id} not found")
    try:
        body = await request.json()
    except Exception:
        body = {}

    # Phase 48 T1-6: Yuncat-registered 工具走 ToolService（闭合绕过）
    from tools.registry import get_all_tools
    if tool_name in get_all_tools():
        try:
            return await request.app.state.tool_service.execute(
                tool_name, body, agent_id=agent_id,
            )
        except PermissionError as e:
            raise HTTPException(403, f"Permission denied: {e}")
        except ValueError as e:
            # 注册表竞态（list 后被卸载）——降级到 fallback
            _log.warning("ToolService execute missed for %s: %s", tool_name, e)

    # Agent-native 工具 fallback（后端自管沙箱）
    if hasattr(adapter, "execute_tool"):
        _log.info("invoke_agent_tool: agent-native fallback %s/%s", agent_id, tool_name)
        result = await adapter.execute_tool(tool_name, body)
        return result
    raise HTTPException(400, f"Agent {agent_id} does not support tool execution")
