"""routes/ws.py —— WebSocket 端点 + 消息分发

从 main.py 提取（原 ~275 行内联 handler），M1 拆分。
使用 register_ws_routes(app, token) 模式——因为 FastAPI @app.websocket
装饰器不能通过 APIRouter 注册，所以在 main.py 中调用此函数挂载。
"""
import asyncio
import hmac
import json as _json
import logging
import re
import time as _time
import uuid as _uuid
from collections import deque

from fastapi import WebSocket, WebSocketDisconnect

from config import (
    DEFAULT_AGENT_ID,  # Phase 48-G: 铁律 #1 穿透（spec 模式默认 agent_id）
    EXTERNAL_AGENT_IDS,  # 外部 Agent 禁止自动路由（用户未显式选择时一律回退原生 Yuncat）
    WS_MESSAGE_MAX_BYTES, WS_RATE_LIMIT_WINDOW_SEC, WS_RATE_LIMIT_MAX_MESSAGES,
    WS_PROMPT_MAX_CHARS,
    capability_flags_for_level,  # Phase 108+: 能力策略（功能开关）单一来源
)
from services.auth_service import validate_access_token
from services.attachment_store import format_image_reference, persist_image_attachments
from .ws_steer import handle_steer
from .ws_user_question import handle_user_question_response

_log = logging.getLogger("yuncat.ws")

# Phase 48 T3-4: 保存 spec task 引用防 GC + done_callback 记录异常
_spec_tasks: set[asyncio.Task] = set()

# Phase 105: 自动触发 Spec Loop 的 workflow 模板集合
_SPEC_REQUIRED_TEMPLATES = {"zh_architect_editor", "zh_speckit_pipeline"}
# Phase 105: 自动触发 Spec Loop 的关键词（小写匹配）
_SPEC_KEYWORDS = ("设计", "架构", "方案", "重构", "spec", "规格")


class _WsRateLimiter:
    """Batch C M18: 每连接 WS 消息速率限制——滑动窗口。

    防止恶意客户端刷消息耗尽服务端资源。对标 Discord/Guilded WS gateway
    的 per-connection rate limiting（识别 spam 后 close code 1008）。
    """

    def __init__(self):
        self._timestamps: deque[float] = deque()

    def is_allowed(self, now: float | None = None) -> bool:
        t = now if now is not None else _time.time()
        # 淘汰窗口外的时间戳
        cutoff = t - WS_RATE_LIMIT_WINDOW_SEC
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        if len(self._timestamps) >= WS_RATE_LIMIT_MAX_MESSAGES:
            return False
        self._timestamps.append(t)
        return True


# Phase 47 修复: WS Origin 白名单——防恶意网页接管 WS 连接
# 与 main.py CORS 配置对齐：放行 localhost/127.0.0.1 任意端口 + Electron file://
# Electron 生产环境 Origin 头可能为空或 file://，均视为可信本地来源
_ALLOWED_WS_ORIGIN_RE = re.compile(
    r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
    re.IGNORECASE,
)


def _gate_external_agent(explicit_agent: str | None, cap_level: int) -> str:
    """外部 Agent 的显式调用门控——**规则的唯一实现处**。

    外部后端（openclaw/hermes/codex/claude）仅在 MAX 档（cap_level >= 2）可被点名调用；
    非 MAX 档即使用户点名也回落原生 Yuncat：
    - 轻量/标准档的任务不需要拉起外部网关，避免无谓开销与超时；
    - 未启动的外部网关会静默失败，回落比报错更符合用户预期。
    """
    if not explicit_agent:
        return DEFAULT_AGENT_ID
    if explicit_agent in EXTERNAL_AGENT_IDS and not capability_flags_for_level(cap_level).get("external_agent", False):
        return DEFAULT_AGENT_ID
    return explicit_agent


def _is_origin_allowed(origin: str) -> bool:
    """校验 WS Origin 是否来自可信本地源。

    - 空 Origin：Electron 生产环境（file:// 协议），允许
    - file:// 前缀：Electron 加载本地资源，允许
    - http(s)://127.0.0.1 或 localhost 任意端口：开发服务器，允许
    - 其他：拒绝（防 evil.com 等恶意网页接管）

    Batch B H10: 空 Origin 仍放行（Electron 生产环境合法场景），但记录 debug 日志
    便于监控。真正的防御在 ws_endpoint 中的 hmac.compare_digest token 校验——
    即使 Origin 被绕过，无 token 的连接仍会被拒绝。
    """
    if not origin:
        _log.debug("WS empty origin accepted (Electron production or non-browser client)")
        return True
    if origin.startswith("file://"):
        return True
    return bool(_ALLOWED_WS_ORIGIN_RE.match(origin))


def register_ws_routes(app, token: str):
    """在 FastAPI app 上注册 WebSocket 端点。

    为什么要用这种模式：FastAPI 的 APIRouter.websocket() 在一些版本中不稳定，
    且 WS 端点需要直接访问 app.state（无法通过 request.app.state）。
    因此将 handler 逻辑提取到此文件，但路由注册仍在 main.py 通过 app.websocket() 完成。
    """

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        # Phase 47 修复: accept 前校验 Origin 防恶意网页接管
        origin = ws.headers.get("origin", "")
        if not _is_origin_allowed(origin):
            _log.warning("WS rejected origin=%s", origin)
            try:
                await ws.close(code=1008)
            except RuntimeError:
                pass
            return

        await ws.accept()
        peer = origin or "?"
        try:
            msg = await ws.receive_json()
            # Batch C M17: 用 json.dumps + encode 精确计算字节数，替代不准确的 str(msg)
            if len(_json.dumps(msg, ensure_ascii=False).encode("utf-8")) > WS_MESSAGE_MAX_BYTES:
                await ws.close(code=1009, reason="Message too large")
                return
            msg_type = msg.get("type", "")
            msg_token = msg.get("token", "")
            # Phase 47 修复: hmac.compare_digest 防时序攻击
            # Batch 7.6: WS 同时接受静态 TOKEN 与有状态 access token，保持与 HTTP 认证一致
            static_match = bool(msg_token) and hmac.compare_digest(msg_token, token)
            access_match = bool(msg_token) and validate_access_token(msg_token)
            token_match = static_match or access_match
            if msg_type != "auth" or not token_match:
                _log.warning(
                    "WS auth rejected: type=%r have_token=%s token_match=%s from=%s",
                    msg_type, bool(msg_token), token_match, peer,
                )
                try:
                    await ws.close(code=1008)
                except RuntimeError:
                    pass
                return
        except WebSocketDisconnect:
            _log.info("WS disconnected before auth from=%s", peer)
            return
        except Exception as exc:
            _log.warning("WS auth error: %s from=%s", exc, peer)
            try:
                await ws.close(code=1008)
            except RuntimeError:
                pass
            return
        await ws.send_json({"type": "auth_ok"})
        _log.info("WS authenticated from=%s", peer)

        pending = getattr(app.state, "pending_skill_install_toast", None)
        if pending and pending.get("count", 0) > 0:
            try:
                await ws.send_json({
                    "type": "skill_install_complete",
                    "count": pending["count"],
                    "installed_ids": pending.get("installed_ids", []),
                })
            except Exception:
                _log.warning("Failed to send skill_install_complete toast", exc_info=True)
            finally:
                app.state.pending_skill_install_toast = None

        _rate_limiter = _WsRateLimiter()

        app.state.agent_service.register_ws("__broadcast__", ws)

        while True:
            # 预初始化避免 JSON 解析失败时 except 分支二次 NameError。
            msg: dict = {}
            _msg_type = ""
            try:
                msg = await ws.receive_json()
                # Batch C M17: 主消息循环也检查大小（auth 消息已查，后续消息同样需要）
                if len(_json.dumps(msg, ensure_ascii=False).encode("utf-8")) > WS_MESSAGE_MAX_BYTES:
                    await ws.close(code=1009, reason="Message too large")
                    app.state.agent_service.unregister_ws(ws)
                    break
                _msg_type = msg.get("type", "")
                if _msg_type not in ("ping", "pong") and not _rate_limiter.is_allowed():
                    _log.warning("WS rate limited from=%s type=%s", peer, _msg_type)
                    await ws.send_json({
                        "type": "error",
                        "error": "消息发送过于频繁，请稍后再试",
                        "code": "RATE_LIMITED",
                    })
                    continue
                if _msg_type == "submit_task":
                    await _handle_submit_task(app, ws, msg)
                elif _msg_type == "steer" and msg.get("task_id"):
                    await handle_steer(app, ws, msg)
                elif _msg_type == "user_question_response": await handle_user_question_response(ws, msg)
                elif _msg_type == "deep_review":
                    await _handle_deep_review(app, ws, msg)
                elif _msg_type == "compact_context":
                    await _handle_compact_context(app, ws, msg)
                elif _msg_type == "subscribe" and msg.get("task_id"):
                    await _handle_subscribe(app, ws, msg)
                elif _msg_type == "resume" and msg.get("task_id"):
                    # P0-1 修复: WS 重连后前端发 resume 重新订阅 running task 流式更新
                    # 复用 _handle_subscribe（register_ws + sync_state），避免消息被静默丢弃
                    await _handle_subscribe(app, ws, msg)
                elif _msg_type == "ping":
                    await ws.send_json({"type": "pong", "ts": msg.get("ts", 0)})
                elif _msg_type == "pong":
                    app.state.agent_service._ws_last_active[id(ws)] = _time.time()
                elif _msg_type == "tool_confirm":
                    # Phase 47: 工具确认弹窗——前端通过 WS 发回确认
                    await _handle_tool_confirm(app, ws, msg)
                elif _msg_type == "approval_response":
                    # Phase 107: 统一审批 Inbox——前端通过 WS 发回批准/拒绝
                    await _handle_approval_response(app, ws, msg)
                elif _msg_type == "goal_clarification_response":
                    # Phase 78: 客户端回传目标澄清答案 → 服务端提炼后推送 goal_confirmed
                    await _handle_goal_clarification_response(app, ws, msg)
                elif _msg_type == "long_task_confirm":
                    # Phase 80 Tier 3: 用户确认进入长任务 → orchestrator.start()
                    await _handle_long_task_confirm(app, ws, msg)
                elif _msg_type == "deliberation_user_message":
                    # Phase 49-D6: 用户作为讨论参与者发送消息
                    await _handle_deliberation_user_message(app, ws, msg)
                elif _msg_type == "deliberation_pause":
                    # Phase 49-D6: 暂停讨论——下一阶段间检查点阻塞
                    await _handle_deliberation_pause(app, ws, msg)
                elif _msg_type == "deliberation_resume":
                    # Phase 49-D6: 恢复讨论——解除阻塞
                    await _handle_deliberation_resume(app, ws, msg)
            except WebSocketDisconnect:
                app.state.agent_service.unregister_ws(ws)
                break
            except Exception as e:
                # P0-5 修复: handler 异常兜底——避免未预期错误冒泡导致 WS 连接断裂不清理
                # 场景: submit_task/orchestrator.start 等抛 DB 错误/配置缺失时，
                # 原代码会跳出 while 循环，WS 不 unregister（资源泄漏）+ task 流式更新丢失。
                # 现改为: 记录日志 + 向前端推送 error 消息 + 保持连接不断开。
                _log.warning("WS message handler failed (type=%s): %s", _msg_type, e, exc_info=True)
                # P1-6 修复: 兜底 error 补 task_id —— 前端 setTaskError 依赖 task_id
                # 路由错误到对应任务卡片；msg 可能不含 task_id（如 submit_task 在创建前抛异常），
                # 此时省略 task_id 字段，前端按"全局错误"处理。
                _err_payload = {
                    "type": "error",
                    "error": f"handler failed: {e}",
                    "msg_type": _msg_type,
                }
                if isinstance(msg, dict) and msg.get("task_id"):
                    _err_payload["task_id"] = msg["task_id"]
                try:
                    await ws.send_json(_err_payload)
                except Exception:
                    # send_json 也失败说明连接已坏，退出循环
                    app.state.agent_service.unregister_ws(ws)
                    break


# ════════════════════════════════════════════════════════════
# 消息子处理器
# ════════════════════════════════════════════════════════════

async def _handle_tool_confirm(app, ws, msg: dict):
    """Phase 47: WS 工具确认——前端弹窗用户批准/拒绝后通过 WS 发回。
    与 HTTP POST /tools/confirm 功能等价，但走 WS 全双工——不额外建立连接。

    P1-6 修复: 原异常路径只 _log.debug 静默吞掉，前端不知道确认失败，
    confirm_future 永不被 resolve → agent_tool_loop 等 30 分钟超时。
    对标 WebSocket RPC 标准：请求-响应必须配对，失败要回 error + resolve Future。
    """
    token = msg.get("confirm_token", "")
    approved = msg.get("approved", False)
    if not token:
        return
    try:
        if approved:
            from tools.builtin.system_tools import dispatch_confirm
            result = await dispatch_confirm(app.state.db, token)
        else:
            result = {"success": False, "error": "用户拒绝执行", "action": "rejected"}
        # 解析 confirm Future——唤醒等待中的 agent_tool_loop 协程
        from services.tools.tool_service import resolve_confirmation
        await resolve_confirmation(token, result)
        # 修复 1.6 (F-031): 用户勾选 "Always allow" → 加入 session 级允许列表（本 session 立即可用），
        # 同时持久化一条 standing grant（跨进程重启仍有效，被 execute_confirmable 的
        # is_standing_granted 命中后跳过确认弹窗）。两者互补：内存态重启即失效，持久态可长期复用。
        if msg.get("remember") and result.get("success"):
            from services.tools.tool_service import add_session_allowed_tool
            from services.standing_grants import persist_remember
            tool_name = msg.get("tool_name", "")
            if tool_name:
                add_session_allowed_tool(tool_name)
                await persist_remember(app.state.db, tool_name)
    except Exception as e:
        # P1-6: 提升到 warning + 通知前端 + 尝试 resolve Future 避免悬挂 30 分钟
        _log.warning("tool_confirm handler failed: %s", e, exc_info=True)
        try:
            await ws.send_json({
                "type": "tool_confirm_error",
                "confirm_token": token,
                "error": f"确认处理失败: {e}",
            })
        except Exception:
            pass
        # 尝试用失败结果 resolve Future，避免 agent_tool_loop 悬挂 30 分钟
        try:
            from services.tools.tool_service import resolve_confirmation
            await resolve_confirmation(token, {
                "success": False, "error": f"confirm handler: {e}", "action": "error",
            })
        except Exception:
            _log.debug("tool_confirm fallback resolve failed", exc_info=True)


async def _handle_approval_response(app, ws, msg: dict):
    """Phase 107: 统一审批 Inbox——前端通过 WS 发回批准/拒绝。

    消息格式（客户端→服务端）::
        {"type": "approval_response", "token": "...", "action": "approve"|"reject",
         "reason": "..."}

    与 HTTP POST /api/v1/approvals/{token}/approve|reject 等价，但走 WS 全双工。
    批准路径调 ApprovalInboxService.resolve_approved（包装 dispatch_confirm），
    拒绝路径调 resolve_rejected。两者均通过 resolve_confirmation 唤醒等待中的协程。

    P1-6 修复: 同 _handle_tool_confirm——异常时通知前端 + resolve Future。
    """
    token = msg.get("token", "")
    action = msg.get("action", "")
    if not token or action not in ("approve", "reject"):
        return
    try:
        svc = getattr(app.state, "approval_inbox", None)
        if svc is None:
            return
        if action == "approve":
            try:
                result = await svc.resolve_approved(
                    token,
                    remember=bool(msg.get("remember", False)),
                    tool_name=str(msg.get("tool_name", "")),
                )
            except ValueError:
                # token 已处理/过期——通知前端
                result = {"success": False, "error": "token-expired-or-invalid",
                          "action": "approve"}
        else:  # reject
            result = await svc.resolve_rejected(token, msg.get("reason", ""))
        # 解析 confirm Future——唤醒等待中的 agent_tool_loop 协程（与 tool_confirm 协同）
        from services.tools.tool_service import resolve_confirmation
        await resolve_confirmation(token, result)
    except Exception as e:
        # P1-6: 提升到 warning + 通知前端 + resolve Future
        _log.warning("approval_response handler failed: %s", e, exc_info=True)
        try:
            await ws.send_json({
                "type": "approval_error",
                "token": token,
                "error": f"审批处理失败: {e}",
            })
        except Exception:
            pass
        try:
            from services.tools.tool_service import resolve_confirmation
            await resolve_confirmation(token, {
                "success": False, "error": f"approval handler: {e}", "action": "error",
            })
        except Exception:
            _log.debug("approval_response fallback resolve failed", exc_info=True)


# ════════════════════════════════════════════════════════════
# Phase 78: 目标澄清 WS 消息
# ════════════════════════════════════════════════════════════

async def _handle_goal_clarification_response(app, ws, msg: dict):
    """Phase 78: 客户端回传澄清答案 → 服务端 refine_goal → 推送 goal_confirmed

    消息格式（客户端→服务端）::
        {"type": "goal_clarification_response", "task_id": "...",
         "answers": [{"question": "...", "answer": "..."}],
         "need_more": bool  # 可选——true 表示用户想再答一轮（生成 followup 问题）

    服务端推送（服务端→客户端）::
        - need_more=false（默认）→ goal_confirmed
            {"type": "goal_confirmed", "task_id": "...",
             "confirmed_goal": "...", "grading_levels": [...], "assumptions": [...]}
        - need_more=true → goal_clarification_request (stage="followup")
            {"type": "goal_clarification_request", "task_id": "...",
             "stage": "followup", "questions": [...]}
    """
    task_id = msg.get("task_id", "")
    answers = msg.get("answers", [])
    need_more = bool(msg.get("need_more", False))
    if not task_id or not isinstance(answers, list):
        await ws.send_json({"type": "error", "message": "task_id 或 answers 缺失"})
        return

    # Phase 80: 长任务委托——若 task 是长任务，交给 orchestrator 处理
    # orchestrator 内部会调 clarifier 并串联 decompose → execute
    orchestrator = getattr(app.state, "long_task_orchestrator", None)
    if orchestrator and await orchestrator.is_long_task(task_id):
        try:
            await orchestrator.handle_clarification_response(
                task_id, answers, need_more=need_more,
            )
        except Exception as e:
            _log.debug("orchestrator handle_clarification_response failed", exc_info=True)
            await ws.send_json({"type": "error", "message": f"长任务处理失败: {e}"})
        return

    clarifier = getattr(app.state, "goal_clarifier", None)
    if clarifier is None:
        await ws.send_json({"type": "error", "message": "GoalClarifier 未初始化"})
        return

    agent_service = app.state.agent_service

    # ── 分支 A：need_more=true → 再生成 followup 问题 ──
    if need_more:
        # 从 tasks 表取 task_input（generate_questions 需要）
        task_input = ""
        try:
            cur = await app.state.db.execute(
                "SELECT input FROM tasks WHERE id=?", (task_id,),
            )
            row = await cur.fetchone()
            if row:
                task_input = str(row[0] or "")
        except Exception:
            _log.debug("need_more: fetch task_input failed", exc_info=True)

        try:
            questions = await clarifier.generate_questions(
                task_input, previous_qa=answers,
            )
        except Exception:
            _log.debug("need_more: generate_questions failed", exc_info=True)
            await ws.send_json({"type": "error", "message": "生成追问问题失败"})
            return

        await agent_service._safe_send(task_id, {
            "type": "goal_clarification_request",
            "task_id": task_id,
            "stage": "followup",
            "questions": questions,
        })
        return

    # ── 分支 B：默认 → refine_goal → 推 goal_confirmed ──
    try:
        result = await clarifier.refine_goal(answers)
    except Exception:
        _log.debug("goal_clarification_response refine failed", exc_info=True)
        await ws.send_json({"type": "error", "message": "目标提炼失败"})
        return

    await agent_service._safe_send(task_id, {
        "type": "goal_confirmed",
        "task_id": task_id,
        "confirmed_goal": result["confirmed_goal"],
        "grading_levels": result["grading_levels"],
        "assumptions": result.get("assumptions", []),
        # Task 6.11: 透传 confidence（非长任务路径，refine_goal 兜底返回 0.5）
        "confidence": result.get("confidence", 0.5),
    })


async def send_goal_clarification_request(
    app, task_id: str, questions: list[dict], stage: str = "initial",
) -> None:
    """Phase 78: 服务端推送目标澄清请求给前端（供 Phase 80 编排器调用）

    消息格式（服务端→客户端）::
        {"type": "goal_clarification_request", "task_id": "...",
         "stage": "initial|followup",
         "questions": [{"id":..., "type":..., "question":...}]}
    """
    agent_service = app.state.agent_service
    await agent_service._safe_send(task_id, {
        "type": "goal_clarification_request",
        "task_id": task_id,
        "stage": stage,
        "questions": questions,
    })


async def _handle_long_task_confirm(app, ws, msg: dict):
    """Phase 80 Tier 3: 用户确认进入长任务 → 创建新 task → orchestrator.start()

    消息格式（客户端→服务端）::
        {"type": "long_task_confirm", "prompt": "...", "agent_id": "...可选...}

    与 Tier 1 的区别：Tier 1 是用户主动开启 long_task_mode 提交；
    Tier 3 是系统发 long_task_suggested 后用户点确认触发。
    两者都走 orchestrator.start()，但 Tier 3 创建新 task（不复用原 task）。
    """
    prompt = msg.get("prompt", "")
    if not prompt:
        await ws.send_json({"type": "error", "message": "prompt 缺失"})
        return
    orchestrator = getattr(app.state, "long_task_orchestrator", None)
    if orchestrator is None:
        await ws.send_json({"type": "error", "message": "LongTaskOrchestrator 未初始化"})
        return

    session_id = msg.get("session_id", str(_uuid.uuid4()))
    _cap_level = int(msg.get("capability_level", 2 if msg.get("max_mode") else 1))
    agent_id = _gate_external_agent(msg.get("agent_id"), _cap_level)
    import time as _t
    task_id = str(_uuid.uuid4())
    await app.state.agent_service.db.execute(
        "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (task_id, prompt, "running", agent_id, session_id, int(_t.time())),
    )
    await app.state.agent_service.db.commit()
    app.state.agent_service.register_ws(task_id, ws)
    await ws.send_json({
        "type": "task_created",
        "task_id": task_id,
        "route_info": {"agent_id": agent_id, "reason": "长任务确认"},
        "prompt": prompt,
        "long_task": True,
    })
    await orchestrator.start(
        task_id, prompt, agent_id=agent_id,
        language=msg.get("language", "zh"),
        max_mode=_cap_level >= 2,
        capability_level=_cap_level,
        reasoning_effort=msg.get("reasoning_effort")
        or ("high" if _cap_level >= 2 else "medium" if _cap_level == 1 else "none"),
    )


# ════════════════════════════════════════════════════════════
# Phase 49-D6: 多角色讨论用户参与——消息注入 + 暂停/恢复
# ════════════════════════════════════════════════════════════

async def _handle_deliberation_user_message(app, ws, msg: dict):
    """用户作为讨论参与者发送消息——追加到 task 的用户消息队列。

    消息格式（客户端→服务端）::
        {"type": "deliberation_user_message", "task_id": "...", "message": "..."}

    消息不会立即影响当前阶段——会在下一阶段间检查点排空并注入到 task_input。
    设计原因: 当前阶段角色可能已在并行执行，注入会造成部分角色看到、部分看不到，
    破坏讨论一致性。阶段间检查点是天然的同步点。

    返回 WS 确认（服务端→客户端）::
        {"type": "deliberation_user_message_ack", "task_id": "...",
         "accepted": bool, "queued_count": int}
    """
    task_id = msg.get("task_id", "")
    message = msg.get("message", "")
    if not task_id or not message:
        await ws.send_json({
            "type": "deliberation_user_message_ack",
            "task_id": task_id, "accepted": False, "queued_count": 0,
            "error": "task_id 或 message 缺失",
        })
        return

    svc = getattr(app.state, "deliberation_service", None)
    if svc is None:
        await ws.send_json({
            "type": "deliberation_user_message_ack",
            "task_id": task_id, "accepted": False, "queued_count": 0,
            "error": "deliberation_service 未初始化",
        })
        return

    accepted = svc.add_user_message(task_id, message)
    queued_count = len(svc._user_messages.get(task_id, []))
    await ws.send_json({
        "type": "deliberation_user_message_ack",
        "task_id": task_id, "accepted": accepted, "queued_count": queued_count,
    })


async def _handle_deliberation_pause(app, ws, msg: dict):
    """暂停讨论——下一阶段间检查点会阻塞等待 resume。

    消息格式（客户端→服务端）::
        {"type": "deliberation_pause", "task_id": "..."}

    注意: 暂停不会中断当前正在执行的阶段（避免半成品状态）。
    """
    task_id = msg.get("task_id", "")
    if not task_id:
        return
    svc = getattr(app.state, "deliberation_service", None)
    if svc is None:
        return
    paused = svc.pause_task(task_id)
    if paused:
        _log.info("Deliberation task %s paused by user", task_id)


async def _handle_deliberation_resume(app, ws, msg: dict):
    """恢复讨论——解除阶段间检查点阻塞。

    消息格式（客户端→服务端）::
        {"type": "deliberation_resume", "task_id": "..."}
    """
    task_id = msg.get("task_id", "")
    if not task_id:
        return
    svc = getattr(app.state, "deliberation_service", None)
    if svc is None:
        return
    resumed = svc.resume_task(task_id)
    if resumed:
        _log.info("Deliberation task %s resumed by user", task_id)


async def _handle_submit_task(app, ws, msg):
    """submit_task —— 任务创建 + 路由 + 附件 + 复杂度评估"""
    # Batch C M20: 用 .get() 替代 msg["prompt"] 防 KeyError
    prompt = msg.get("prompt", "")
    # Batch C M19: prompt 长度校验——防止超长 prompt 耗尽内存/上下文
    if not prompt or not prompt.strip():
        await ws.send_json({"type": "error", "message": "prompt 不能为空"})
        return
    if len(prompt) > WS_PROMPT_MAX_CHARS:
        await ws.send_json({
            "type": "error",
            "message": f"prompt 过长（{len(prompt)} 字符，上限 {WS_PROMPT_MAX_CHARS}）",
        })
        return

    # Phase 23: #记住 快捷指令 → 直接写长期记忆
    if prompt.strip().startswith("#记住"):
        memory_content = prompt.strip()[3:].strip()
        if memory_content:
            try:
                from repositories import insert_memory
                await insert_memory(
                    app.state.db, content=memory_content, source="quick_remember",
                    memory_type="fact", confidence=0.8, owner=str(_uuid.uuid4())[:8],
                )
                await ws.send_json({
                    "type": "memory_saved",
                    "message": f"已记住: {memory_content[:100]}",
                })
            except Exception as e:
                await ws.send_json({
                    "type": "error",
                    "message": f"记忆保存失败: {e}",
                })
        else:
            await ws.send_json({
                "type": "error", "message": "请提供记忆内容。格式: #记住 内容",
            })
        return

    explicit_agent = msg.get("agent_id")
    session_id = msg.get("session_id", str(_uuid.uuid4()))
    mode = msg.get("mode", "agent")
    language = msg.get("language", "zh")

    # 能力等级 + 思考强度（reasoning_effort）统一推导：
    # - 能力等级 0=轻量 / 1=标准 / 2=强力(MAX)；max_mode 由等级推导（L2 ⇒ MAX）。
    # - 思考强度默认随能力等级联动：0→low(不思考) / 1→medium(标准) / 2→high(深度)，
    #   用户可在前端单独覆盖（reasoning_effort 透传至 adapter，模型不支持时自动忽略）。
    _cap_level = int(msg.get("capability_level", 2 if msg.get("max_mode") else 1))
    _reasoning = msg.get("reasoning_effort") or (
        "high" if _cap_level >= 2 else "medium" if _cap_level == 1 else "none"
    )

    # ═══════════════════════════════════════════════════════════
    # Phase 43 Stage E.1: Spec 模式路由
    # ═══════════════════════════════════════════════════════════
    if mode == "spec":
        await _handle_spec_mode(app, ws, msg, prompt, session_id, language=language)
        return

    # 快捷指令：/compact 手动压缩上下文
    if prompt.strip().startswith("/compact"):
        await _handle_compact_inline(app, ws, msg)
        return
    # /context /tokens /context-stats 查看上下文占用（主流 CLI 常见命令）
    if prompt.strip().startswith(("/context", "/tokens", "/context-stats")):
        await _handle_context_stats_inline(app, ws, msg)
        return

    # Phase 27: Slash Command 匹配
    sc = None
    if prompt.strip().startswith("/"):
        sc = app.state.slash_commands.match(prompt)
    if sc:
        await _handle_slash_command(app, ws, msg, prompt, sc, session_id, mode, language=language)
        return

    if explicit_agent:
        # Phase X (2026-09-10): 外部 Agent 仅当 MAX（强力档）才可被显式调用。
        # 判定收敛到 _gate_external_agent——它有两个入口（submit_task / long_task_confirm），
        # 各写一份必然漂移（实测 confirm 路径曾漏掉门控，同一诉求在两条入口行为不同）。
        _gated = _gate_external_agent(explicit_agent, _cap_level)
        agent_id = _gated
        route_info = {
            "agent_id": agent_id,
            "reason": "用户指定" if _gated == explicit_agent
            else "外部 Agent 仅在 MAX 模式可用，已回退至 Yuncat",
        }
    else:
        # Phase 31: 意图路由 + 能力路由 双保险
        intent_svc = app.state.intent_service
        route = await intent_svc.classify_route(prompt)
        agent_id = route["agent_id"]
        route_info = {"agent_id": agent_id, "reason": route["reason"]}

        # Phase X (2026-09-10): 外部 Agent 禁止自动路由（EXTERNAL_AGENT_AUTO_ROUTE=False）。
        # 即使用户在 Agent 管理页启用了外部 Agent（codex/openclaw），自动路由也只走原生
        # Yuncat；外部 Agent 仅在用户于下拉框显式选择 / @mention 时才调用。否则意图分类器
        # 一旦把任务判给未启动的外部网关（如 openclaw），任务会静默失败。
        if agent_id in EXTERNAL_AGENT_IDS or not agent_id or agent_id not in app.state.adapters:
            from services.capability_router import build_profiles, route_by_capability
            # Phase 48 T2-1: build_profiles/route_by_capability 均为 async，加 await
            # （原 build_profiles 未 await 是 pre-existing bug，会传 coroutine 进 route_by_capability）
            profiles = await build_profiles(app.state.adapters)
            agent_id = await route_by_capability(prompt, profiles, default=DEFAULT_AGENT_ID)
            # 兜底（防御纵深）：即便能力路由返回外部 agent（build_profiles 已排除，
            # 但防未来回归），也一律回落原生——外部 agent 只能由用户显式选择调用。
            if agent_id in EXTERNAL_AGENT_IDS:
                agent_id = DEFAULT_AGENT_ID
            route_info = {"agent_id": agent_id, "reason": f"能力路由：{agent_id} 擅此任务"}

    attachments = msg.get("attachments", [])
    attachments = persist_image_attachments(session_id, attachments)
    extra_ctx = {
        "max_mode": _cap_level >= 2,
        "capability_level": _cap_level,
        # Phase 108+: 能力策略——功能开关集合（非模型选择），供任务执行/工具路由前置拦截
        "capability_flags": capability_flags_for_level(_cap_level),
        # Phase X (2026-09-10): 思考强度——透传至 adapter，由模型决定是否实际生效
        "reasoning_effort": _reasoning,
    }
    if attachments:
        extra_ctx["attachments"] = attachments
        img_count = sum(1 for a in attachments if a.get("type", "").startswith("image/"))
        if img_count > 0:
            extra_ctx["image_count"] = img_count
            prompt = f"{format_image_reference(attachments)} {prompt}"

    # Phase 105: Auto Spec 触发——非 plan/ask 模式下根据关键词/workflow/effort 自动进入 Spec Loop
    # 同时评估任务复杂度，供 Tier 2 建议复用，避免重复调用 assess_complexity。
    complexity = None
    if mode not in ("plan", "ask") and app.state.deliberation_service:
        try:
            complexity = app.state.deliberation_service.assess_complexity(prompt)
        except Exception:
            _log.debug("assess_complexity failed before auto spec check", exc_info=True)

    # Phase 49-D3: complexity=high 且无 spec 关键词 → AI 自动触发多角色讨论
    # 设计原则: spec 关键词（设计/架构/方案/重构/spec/规格）命中的任务走 spec loop
    # （Architect→Developer→Auditor 实施流程）；无 spec 关键词但高复杂度的分析类任务
    # （如评估/权衡/审计/选型）走 deliberation（多角色讨论→共识）。
    # 顺序在 auto-spec 之前，避免 _maybe_auto_spec 因 effort=deep 抢占。
    if (
        complexity
        and complexity.get("level") == "high"
        and app.state.deliberation_service
        and mode not in ("plan", "ask", "long_task")
        and not any(kw in prompt.lower() for kw in _SPEC_KEYWORDS)
    ):
        _log.info("Auto-deliberation triggered for prompt: %s...", prompt[:60])
        await ws.send_json({
            "type": "deliberation_auto_start",
            "prompt": prompt,
            "complexity": complexity,
            "session_id": session_id,
        })
        return

    if mode not in ("plan", "ask"):
        try:
            if await _maybe_auto_spec(app, prompt, agent_id, extra_ctx, complexity=complexity):
                _log.info("Auto spec triggered for prompt: %s...", prompt[:60])
                await _handle_spec_mode(app, ws, msg, prompt, session_id)
                return
        except Exception:
            _log.debug("Auto spec check failed, falling back to normal task", exc_info=True)

    # Phase 80 Tier 1: 显式长任务模式——用户在 ChatToolbar 开启 long_task_mode
    # 创建 task 后走 orchestrator.start()（澄清→分解→执行），不走普通 start_task
    if msg.get("long_task_mode"):
        orchestrator = getattr(app.state, "long_task_orchestrator", None)
        if orchestrator is None:
            await ws.send_json({"type": "error", "message": "LongTaskOrchestrator 未初始化"})
            return
        import time as _t
        task_id = str(_uuid.uuid4())
        await app.state.agent_service.db.execute(
            "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (task_id, prompt, "running", agent_id, session_id, int(_t.time())),
        )
        await app.state.agent_service.db.commit()
        app.state.agent_service.register_ws(task_id, ws)
        await ws.send_json({
            "type": "task_created",
            "task_id": task_id,
            "route_info": route_info,
            "prompt": prompt,
            "long_task": True,
        })
        await orchestrator.start(
            task_id, prompt, agent_id=agent_id, language=language,
            max_mode=_cap_level >= 2,
            capability_level=_cap_level,
            reasoning_effort=_reasoning,
        )
        return

    task_id = await app.state.agent_service.start_task(
        agent_id, prompt, session_id, ws, mode=mode,
        extra_context=extra_ctx,
        language=language,
    )
    # plan/ask 模式下上面未评估复杂度，此处补一次；非 plan/ask 模式复用已有结果。
    if complexity is None and app.state.deliberation_service:
        try:
            complexity = app.state.deliberation_service.assess_complexity(prompt)
        except Exception:
            _log.debug("assess_complexity failed for task_created", exc_info=True)
    # dev 模式 Vite HMR 重载会导致 WS 在 submit_task 处理期间断开，task_created
    # 确认消息 send 抛 "after close"。此时任务已成功创建并在后台运行，不应因
    # 确认消息发送失败而让 _handle_submit_task 抛异常（前端会误报"发送超时"）。
    # 容错：send 失败仅记 warning，任务照常执行；前端后续通过 HTTP 对账恢复。
    try:
        await ws.send_json({
            "type": "task_created",
            "task_id": task_id,
            "route_info": route_info,
            "prompt": prompt,
            "complexity": complexity,
        })
    except Exception as e:
        _log.warning("task_created send failed (task %s still running): %s",
                     task_id[:8], e)

    # Phase 80 Tier 2: complexity=extreme → 发 long_task_suggested（不阻塞正常执行）
    # 用户可点确认进入长任务（Tier 3），不自动切换
    if complexity and complexity.get("level") == "extreme":
        try:
            await ws.send_json({
                "type": "long_task_suggested",
                "task_id": task_id,
                "reason": f"complexity={complexity.get('level')}",
                "complexity": complexity,
            })
        except Exception:
            _log.debug("long_task_suggested send failed", exc_info=True)


async def _maybe_auto_spec(
    app, prompt: str, agent_id: str, extra_ctx: dict | None,
    complexity: dict | None = None,
) -> bool:
    """Phase 105: 根据任务特征判断是否需要自动进入 Spec Loop。

    触发条件（满足任一即返回 True）：
    1. prompt 包含 spec 相关关键词（设计/架构/方案/重构/spec/规格）。
    2. PromptAgent.recommend 返回的 workflow_template 属于需要 spec 的模板。
    3. extra_ctx 显式 effort=deep/ultracode，或 deliberation 评估 effort=deep/ultracode。

    参数:
        complexity: 可选的预评估复杂度。传入时复用，避免重复调用 assess_complexity。

    所有外部调用均包裹 try/except，失败时降级为 False，不阻断主流程。
    """
    prompt_lower = prompt.lower()
    if any(kw in prompt_lower for kw in _SPEC_KEYWORDS):
        return True

    prompt_agent = getattr(app.state, "prompt_agent", None)
    if prompt_agent is not None:
        try:
            rec = await prompt_agent.recommend(prompt, agent_id=agent_id)
            if rec.get("workflow_template") in _SPEC_REQUIRED_TEMPLATES:
                return True
        except Exception:
            _log.debug("PromptAgent recommend failed in auto spec check", exc_info=True)

    effort = (extra_ctx or {}).get("effort")
    if effort in ("deep", "ultracode"):
        return True

    deliberation = getattr(app.state, "deliberation_service", None)
    if deliberation is not None:
        try:
            if complexity is None:
                complexity = deliberation.assess_complexity(prompt)
            if complexity.get("effort") in ("deep", "ultracode"):
                return True
        except Exception:
            _log.debug("assess_complexity failed in auto spec check", exc_info=True)

    return False


async def _handle_spec_mode(app, ws, msg, prompt, session_id, language: str = "zh"):
    """Phase 43 Stage E.1: Spec 模式 → run_spec_loop + WS 实时推送。

    对标 OpenAI Ghost Library: 前端触发 Spec Loop，后端三阶段循环，
    每阶段进度通过 WS spec_phase/spec_loop_done 事件实时推送到前端。

    与 run_deliberation 的区别:
      - deliberation: POST /api/v1/deliberation/start → HTTP 端点
      - spec_mode:    WS submit_task mode="spec" → WS 全双工推送
    """
    import time as _t

    agent_service = app.state.agent_service
    deliberation_service = app.state.deliberation_service
    if not deliberation_service:
        await ws.send_json({"type": "error", "message": "DeliberationService 未初始化"})
        return

    # 生成 task_id + 写入 DB
    task_id = str(_uuid.uuid4())
    agent_id = DEFAULT_AGENT_ID  # Phase 48-G: 改用常量替代硬编码 "hermes"
    await agent_service.db.execute(
        "INSERT INTO tasks(id, input, status, agent_id, session_id, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (task_id, prompt, "running", agent_id, session_id, int(_t.time())),
    )
    await agent_service.db.commit()

    # 注册 WS 连接 — Agent 在循环中可通过 _safe_send 推送到前端
    agent_service.register_ws(task_id, ws)

    await ws.send_json({
        "type": "task_created",
        "task_id": task_id,
        "route_info": {"agent_id": agent_id, "reason": "Spec 模式——三阶段循环"},  # Phase 48-G: 用变量替代硬编码
        "mode": "spec",
        "prompt": prompt,
    })

    # push_callback — 将 WS 事件转发到 agent_service._safe_send
    async def _push_spec(tid: str, msg_type: str, payload: dict):
        await agent_service._safe_send(tid, {"type": msg_type, "task_id": tid, **payload})

    # 后台执行 Spec Loop
    async def _run_spec():
        try:
            result = await deliberation_service.run_spec_loop(
                task_input=prompt,
                task_id=task_id,
                push_callback=_push_spec,
                agent_id=agent_id,  # Phase 48-G: 穿透到 spec verification
                language=language,
            )
            # 更新 DB → completed/failed
            # P0-2 修复: 原代码两分支都返回 "completed"，未收敛的 Spec 任务被误标为成功
            status = "completed" if result.get("converged") else "failed"
            output = (
                f"Spec: {result.get('spec_dict', {}).get('spec', {}).get('name', '')}\n"
                f"收敛: {'✅' if result.get('converged') else '❌'}\n"
                f"轮次: {result.get('rounds', 0)}\n"
                f"审计: MATCH={result.get('audit_report', {}).get('matches', 0)} "
                f"MISMATCH={result.get('audit_report', {}).get('mismatches', 0)} "
                f"MISSING={result.get('audit_report', {}).get('missing', 0)}"
            )
            await agent_service.db.execute(
                "UPDATE tasks SET output=?, status=?, completed_at=? WHERE id=?",
                (output, status, int(_t.time()), task_id),
            )
            await agent_service.db.commit()
            await agent_service._safe_send(task_id, {
                "type": "task_status", "task_id": task_id, "status": status,
            })
        except Exception as e:
            _log.exception("Spec loop failed for task %s", task_id)
            await agent_service.db.execute(
                "UPDATE tasks SET status='failed', error_message=?, completed_at=? WHERE id=?",
                (str(e), int(_t.time()), task_id),
            )
            await agent_service.db.commit()
            await agent_service._safe_send(task_id, {
                "type": "error", "task_id": task_id,
                "message": f"Spec Loop 失败: {e}",
            })

    # Phase 48 T3-4: 保存 task 引用防 GC + done_callback 记录异常（原裸 create_task 异常被静默吞咽）
    _spec_task = asyncio.create_task(_run_spec())
    _spec_tasks.add(_spec_task)

    def _spec_done_cb(t: asyncio.Task):
        _spec_tasks.discard(t)
        if t.exception():
            _log.error("Spec task crashed (unhandled): %s", t.exception())

    _spec_task.add_done_callback(_spec_done_cb)


async def _handle_slash_command(app, ws, msg, prompt, sc, session_id, mode, language: str = "zh"):
    """Phase 27: Slash Command → DAG/模式/工具 三路路由"""
    args = sc.extract_args(prompt)
    if sc.workflow_template:
        await app.state.workflow_service.create({  # Phase 48 T3: 去掉未使用的 wf_id 赋值
            "name": f"slash-{sc.name}-{str(_uuid.uuid4())[:8]}",
            "graph": {"nodes": [], "edges": []},
        })
        await ws.send_json({
            "type": "slash_command_matched",
            "command": sc.slash_command, "name": sc.name, "args": args,
        })
        task_id = await app.state.agent_service.start_task(
            DEFAULT_AGENT_ID, f"{sc.description}\n\n用户输入: {prompt}", session_id, ws, mode="agent",
            language=language,
        )
    elif sc.agent_routing.get("mode"):
        mode = sc.agent_routing["mode"]
        task_id = await app.state.agent_service.start_task(
            DEFAULT_AGENT_ID, prompt, session_id, ws, mode=mode,
            language=language,
        )
    elif sc.agent_routing.get("tool"):
        task_id = await app.state.agent_service.start_task(
            DEFAULT_AGENT_ID, prompt, session_id, ws, mode="agent",
            language=language,
        )
        await ws.send_json({
            "type": "slash_command_matched",
            "command": sc.slash_command, "name": sc.name,
            "tool": sc.agent_routing["tool"], "args": args,
        })
    else:
        task_id = await app.state.agent_service.start_task(
            DEFAULT_AGENT_ID, prompt, session_id, ws, mode="agent",
            language=language,
        )
    await ws.send_json({
        "type": "task_created",
        "task_id": task_id,
        "route_info": {"agent_id": DEFAULT_AGENT_ID, "reason": f"命令: {sc.slash_command}"},
        "prompt": prompt,
    })


async def _handle_compact_inline(app, ws, msg):
    """内联 /compact 指令"""
    session_id = msg.get("session_id", "")
    if session_id and hasattr(app.state, "memory_service"):
        model_cfg = {}
        if hasattr(app.state, "model_config"):
            try:
                model_cfg = await app.state.model_config.get_active_config()
            except Exception:
                _log.debug("获取活跃模型配置失败", exc_info=True)
        result = await app.state.memory_service.compact_now(session_id, model_cfg)
        await ws.send_json({
            "type": "compact_result",
            "session_id": session_id,
            "compacted": result is not None,
            "summary": result.get("session_intent", "") if result else "",
            "message": "上下文已压缩" if result else "无可压缩内容",
        })
    else:
        await ws.send_json({
            "type": "compact_result",
            "compacted": False,
            "error": "无活跃会话或 memory_service 不可用",
        })


async def _handle_context_stats_inline(app, ws, msg):
    """内联 /context /tokens /context-stats 指令。

    暴露当前会话的上下文用量、模型窗口、各注入预算、压缩阈值等，
    对标 Claude Code /tokens、Codex CLI /context。
    """
    session_id = msg.get("session_id", "")
    if not session_id or not hasattr(app.state, "memory_service"):
        await ws.send_json({
            "type": "context_stats",
            "session_id": session_id,
            "error": "无活跃会话或 memory_service 不可用",
        })
        return

    ms = app.state.memory_service
    conv = ms._sessions.get(session_id, [])

    # 获取当前模型配置（用于厂商 tokenizer API 和窗口计算）
    from config import MAX_CONTEXT_FALLBACK_CHARS, resolve_context_window
    model_cfg = {}
    context_window = MAX_CONTEXT_FALLBACK_CHARS
    if hasattr(app.state, "model_config"):
        try:
            model_cfg = await app.state.model_config.get_active_config()
            context_window = resolve_context_window(model_cfg)
        except Exception:
            _log.debug("解析上下文窗口失败", exc_info=True)

    # Phase 56-P3: 优先使用当前 Adapter 的 last_usage（厂商 API 实时返回）
    adapter = None
    try:
        cursor = await app.state.db.execute(
            "SELECT agent_id FROM tasks WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            adapter = app.state.adapters.get(row[0])
    except Exception:
        _log.debug("查询 session 对应 adapter 失败", exc_info=True)
    # 兜底：取任意一个有 last_usage 的 adapter
    if adapter is None:
        for a in (app.state.adapters or {}).values():
            if getattr(a, "last_usage", {}).get("prompt_tokens", 0) > 0:
                adapter = a
                break

    # Phase 38 / Phase 56-P3: Adapter last_usage 优先 → DB → 厂商 tokenizer API → tiktoken → CJK
    from services.token_tracker import estimate_context_usage
    usage = await estimate_context_usage(
        app.state.db, session_id, conv,
        adapter=adapter, model_id=model_cfg.get("model", "gpt-4"),
        model_cfg=model_cfg, use_online=True,
    )

    from config import (
        calc_context_budget,
        calc_list_limit,
        calc_tool_result_limit,
        calc_history_limit,
        calc_micro_compact_threshold,
        COMPACT_TRIGGER_MICRO,
        COMPACT_TRIGGER_STANDARD,
        COMPACT_TRIGGER_EMERGENCY,
    )

    estimated = usage["estimated_tokens"]
    usage_pct = round(estimated / context_window * 100, 1) if context_window else 0

    # Phase 56-P2: 动态预算可见性
    budgets = {
        "tool_result_limit": calc_tool_result_limit(model_cfg),
        "history_limit": calc_history_limit(model_cfg),
        "fts_memories": calc_list_limit(model_cfg, 5, fraction_per_100k=1, max_limit=10),
        "vector_memories": calc_list_limit(model_cfg, 5, fraction_per_100k=1, max_limit=6),
        "skill_preview_chars": calc_context_budget(model_cfg, fraction=0.004, min_chars=300, max_chars=1500),
        "skill_total_chars": calc_context_budget(model_cfg, fraction=0.04, min_chars=3000, max_chars=12000),
        "micro_compact_threshold": calc_micro_compact_threshold(model_cfg),
    }
    thresholds = {
        "micro_compact": round(COMPACT_TRIGGER_MICRO * 100, 1),
        "standard_compact": round(COMPACT_TRIGGER_STANDARD * 100, 1),
        "emergency_trim": round(COMPACT_TRIGGER_EMERGENCY * 100, 1),
    }

    await ws.send_json({
        "type": "context_stats",
        "session_id": session_id,
        "message_count": len(conv),
        "estimated_tokens": estimated,
        "source": usage.get("source", "cjk_estimate"),
        "last_real_prompt_tokens": usage.get("last_real_prompt_tokens"),
        "last_real_completion_tokens": usage.get("last_real_completion_tokens"),
        "context_window": context_window,
        "usage_percent": usage_pct,
        "model": model_cfg.get("model", "unknown"),
        "budgets": budgets,
        "thresholds": thresholds,
    })


async def _handle_deep_review(app, ws, msg):
    """Phase 22: Dynamic Orchestration — LLM 自生成工作流"""
    # Batch C M20: 用 .get() 替代 msg["prompt"] 防 KeyError
    prompt = msg.get("prompt", "")
    # Batch C M19: prompt 长度校验
    if not prompt or not prompt.strip():
        await ws.send_json({"type": "error", "message": "prompt 不能为空"})
        return
    if len(prompt) > WS_PROMPT_MAX_CHARS:
        await ws.send_json({
            "type": "error",
            "message": f"prompt 过长（{len(prompt)} 字符，上限 {WS_PROMPT_MAX_CHARS}）",
        })
        return
    effort = msg.get("effort", "auto")
    if effort == "auto" and app.state.deliberation_service:
        c = app.state.deliberation_service.assess_complexity(prompt)
        effort = c.get("effort", "standard")
    await ws.send_json({
        "type": "orchestration_started",
        "prompt": prompt,
        "effort": effort,
    })
    try:
        result = await app.state.dynamic_orchestrator.generate_and_run(prompt, effort)
        await ws.send_json({
            "type": "orchestration_done",
            "result": result,
            "effort": effort,
        })
    except Exception as e:
        await ws.send_json({
            "type": "orchestration_error",
            "error": str(e),
        })


async def _handle_compact_context(app, ws, msg):
    """手动触发上下文压缩"""
    session_id = msg.get("session_id", "")
    if session_id and hasattr(app.state, "memory_service"):
        model_cfg = {}
        if hasattr(app.state, "model_config"):
            try:
                model_cfg = await app.state.model_config.get_active_config()
            except Exception:
                _log.debug("获取活跃模型配置失败", exc_info=True)
        result = await app.state.memory_service.compact_now(session_id, model_cfg)
        await ws.send_json({
            "type": "compact_result",
            "session_id": session_id,
            "compacted": result is not None,
            "summary": result.get("session_intent", "") if result else "",
        })
    else:
        await ws.send_json({
            "type": "compact_result",
            "session_id": session_id,
            "compacted": False,
            "error": "No session_id or memory_service unavailable",
        })


async def _handle_subscribe(app, ws, msg):
    """订阅 task 流——重连时推送 sync_state + 最近对话历史（Task 6.9）。

    Task 6.9: 重连后从 conversations 表恢复最近 N 轮历史，前端 diff 渲染
    （已渲染的不重复）。对标 Claude Code 重连后全量状态同步。
    """
    task_id = msg["task_id"]
    app.state.agent_service.register_ws(task_id, ws)
    task = await app.state.agent_service.get_task(task_id)
    if task:
        await ws.send_json({
            "type": "sync_state",
            "task_id": task_id,
            "status": task["status"],
        })
        # Task 6.9: 重连后恢复流式上下文——推送最近对话历史供前端 diff 渲染
        session_id = task.get("session_id")
        if session_id and hasattr(app.state, "memory_service") and app.state.memory_service:
            try:
                messages = await app.state.memory_service.get_conversation(session_id)
                if messages:
                    await ws.send_json({
                        "type": "replay_messages",
                        "task_id": task_id,
                        "messages": [
                            {
                                "id": m["id"],
                                "role": m["role"],
                                "content": m["content"],
                                "reasoning_content": m.get("reasoning_content", ""),
                            }
                            for m in messages
                        ],
                    })
            except Exception:
                _log.debug("Failed to replay messages for task %s",
                           task_id[:8], exc_info=True)
