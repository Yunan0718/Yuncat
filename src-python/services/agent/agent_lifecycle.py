"""services/agent_lifecycle.py —— Agent 任务生命周期模块

从 agent_service.py 拆分: _run_agent / spawn_subagent / execute_plan_steps。
采用委托模式: 模块级函数接收 self 作为首参，访问 AgentService 实例状态。
"""
import asyncio
import logging
import time as _time
import uuid as _uuid

from config import (
    DEFAULT_AGENT_ID,
    EXTERNAL_AGENT_IDS,
    SUB_AGENT_POLL_MAX_SECONDS,
    SUB_AGENT_OUTPUT_PREVIEW_CHARS,
    SUB_AGENT_PROMPT_PREVIEW_CHARS,
    SUB_AGENT_PROMPT_LOG_PREVIEW_CHARS,
    SUB_AGENT_GENERATED_RESPONSE_MAX_CHARS,
    SUB_AGENT_ERROR_PREVIEW_CHARS,
    SUB_AGENT_FULL_RESPONSE_LOG_PREVIEW_CHARS,
    SUB_AGENT_PLAN_STEP_OUTPUT_PREVIEW_CHARS,
    LOG_PREVIEW_TASK_ID_LEN,
    LOG_PREVIEW_CMD_LEN,
    LOG_PREVIEW_ERROR_LEN,
    LOG_PREVIEW_CHARS,
)
from retry_utils import MAX_RETRIES as _MAX_RETRIES, RETRY_BASE_DELAY as _RETRY_BASE_DELAY, retry_async as _retry_async
from services.agent.task_interactions import discard_task_interactions

_log = logging.getLogger("yuncat.agent")


def _extract_tool_calls(history: list[dict]) -> list[dict]:
    """从 history 中提取 assistant 消息里的 tool_calls 摘要。"""
    calls: list[dict] = []
    for msg in history:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls", []):
            if isinstance(tc, dict):
                calls.append({
                    "id": tc.get("id", ""),
                    "name": tc.get("function", {}).get("name", ""),
                })
    return calls


async def run_agent(
    self, task_id: str, agent_id: str, prompt: str, session_id: str,
    rules_context: str = "", mode: str = "agent",
    extra_context: dict | None = None,
    language: str = "zh",
):
    """后台协程——Claude Code 风格：每轮完整构建上下文 + 全量历史。

    API prompt caching 自动去重系统提示词，重复发送不产生 token 成本。

    H7 重构：拆分为 _build_agent_context / _execute_tool_loop / _finalize_response。
    _run_agent 现在是薄编排层（~80 行，原 579 行）。
    """
    adapter = self.adapters.get(agent_id)
    if not adapter:
        await self.push_error(task_id, f"Agent {agent_id} not found")
        await self._mark_task(task_id, "failed", f"Agent {agent_id} not found",
                              expected_from="running")
        return

    full_response = ""  # 预初始化——防 except 块引用未定义变量（L114 之前异常时）
    lang_token = None
    try:
        from context import set_current_language, reset_current_language
        lang_token = set_current_language(language)
        # ── 启动 Adapter ──
        if not adapter._ready.is_set():
            await adapter.start()
            if hasattr(adapter, "wait_ready"):
                await adapter.wait_ready()
            else:
                await adapter._ready.wait()

        # ── Phase 1: 保存用户消息 + 分配序号 ──
        user_conv_id = ""
        user_msg_id_str = ""
        if self._memory_service:
            async with self._msg_counter_lock:
                self._msg_counter += 1
                msg_seq = self._msg_counter
            user_msg = await self._memory_service.add_message(
                session_id, "user", prompt, agent_id=agent_id,
            )
            user_conv_id = user_msg["id"]
            user_msg_id_str = user_conv_id
            async with self._msg_counter_lock:
                self._msg_index[user_conv_id] = {
                    "seq": msg_seq, "role": "user", "session_id": session_id,
                    "content_preview": prompt[:SUB_AGENT_PROMPT_PREVIEW_CHARS], "refs": [],
                }
                self._evict_msg_index()
            await self._safe_send(task_id, {
                "type": "msg_numbered", "task_id": task_id,
                "msg_id": user_conv_id, "seq": msg_seq, "role": "user",
            })
            await self._write_task_snapshot(task_id, {
                "status": "running", "agent_id": agent_id, "session_id": session_id,
                "messages": [{"role": "user", "content": prompt}],
            })
            # Phase 49-C/D: 用户消息必须立即可见（版本链 / 任务状态查询依赖）
            await self.db.commit()

        # ── 获取模型配置 + API Key 校验 ──
        # Phase 108: 统一模型选择入口——支持分级路由 + MAX 模式
        _max_mode = bool((extra_context or {}).get("max_mode"))
        _cap_level = int((extra_context or {}).get("capability_level", 2 if _max_mode else 1))
        _step_role = (extra_context or {}).get("long_task_step_role", "")
        _model_tier = (extra_context or {}).get("model_tier", "")
        model_cfg = await self._resolve_model_for_request(
            mode, prompt,
            max_mode_enabled=_max_mode,
            long_task_step_role=_step_role,
            model_tier=_model_tier,
            conversation_context={"attachments": (extra_context or {}).get("attachments", [])},
            capability_level=_cap_level,
        )
        # Phase 108: 记录实际使用的模型，供子 Agent 结果追溯 / 长任务 step_output 使用
        if model_cfg:
            _existing = self._task_snapshots.get(task_id, {})
            await self._write_task_snapshot(task_id, {
                **_existing,
                "model_used": {
                    "provider": model_cfg.get("provider", ""),
                    "model": model_cfg.get("model", ""),
                },
            })
        if not model_cfg.get("api_key"):
            await self.push_error(task_id, "未配置 API Key，请在 设置 > 模型配置 中填写")
            await self._mark_task(task_id, "failed", "API Key not configured",
                                  expected_from="running")
            return

        # ── Phase 43 R1-R2: 任务感知路由 —— 根据 prompt 自动选择后端 ──
        route_reason = ""
        if agent_id == DEFAULT_AGENT_ID or not agent_id:
            try:
                default_agent = DEFAULT_AGENT_ID
                if self._settings_service:
                    configured = await self._settings_service.get_raw("default_agent")
                    # Phase 9-X: 外部 Agent 不能作为「默认」自动接管——只接受原生 Yuncat。
                    if configured and configured in self.adapters and configured not in EXTERNAL_AGENT_IDS:
                        default_agent = configured
                from services.task_router import route_task as _route_task
                route = await _route_task(
                    prompt, self.adapters, db=self.db,
                    default_agent=default_agent,
                    allow_external=False,
                )
                # 仅当置信度高且用户未手动指定时切换
                if route.confidence >= 0.70 and route.agent_id != agent_id:
                    _log.info(
                        "Auto-route: '%s' -> %s (confidence=%.2f, reason=%s)",
                        prompt[:SUB_AGENT_PROMPT_LOG_PREVIEW_CHARS], route.agent_id, route.confidence, route.reasoning,
                    )
                    agent_id = route.agent_id
                    adapter = self.adapters.get(agent_id)
                    if not adapter:
                        agent_id = DEFAULT_AGENT_ID
                        adapter = self.adapters.get(DEFAULT_AGENT_ID)
                    elif not adapter._ready.is_set():
                        await adapter.start()
                        await adapter.wait_ready()
                    route_reason = route.reasoning
                    # Phase 43 R1-R2: 推送到前端——用户看到路由决策
                    # Task 7.6: 附带 model_tier——前端可显示难度分级
                    await self._safe_send(task_id, {
                        "type": "task_routed", "task_id": task_id,
                        "agent_id": agent_id,
                        "route_reason": route.reasoning,
                        "confidence": route.confidence,
                        "task_type": route.task_type,
                        "model_tier": route.model_tier,
                        "previous_agent": agent_id or DEFAULT_AGENT_ID,
                    })
                # 路由提示注入到 prompt 前面
                if route.pre_inject_prompt:
                    prompt = route.pre_inject_prompt + "\n\n" + prompt
            except Exception:
                _log.debug("Task routing skipped", exc_info=True)

        # ── Phase 2: 组装 Agent 上下文 ──
        context, ctx_summary = await self._build_agent_context(
            task_id, session_id, prompt, mode, rules_context, extra_context, model_cfg, agent_id,
            language=language,
        )

        # ── Phase 3: Tool Call 执行循环 ──
        t0 = _time.time()
        full_response = await self._execute_tool_loop(
            adapter, context, task_id, agent_id, session_id, model_cfg, prompt,
        )

        await self.push_stream_done(task_id)
        elapsed = _time.time() - t0

        await self._write_task_snapshot(task_id, {
            "status": "running", "agent_id": agent_id, "session_id": session_id,
            "generated_response": full_response[:SUB_AGENT_GENERATED_RESPONSE_MAX_CHARS],
            "tool_calls": _extract_tool_calls(context.get("history", [])),
        })

        # ═══════════════════════════════════════════════════════════
        # Phase 47: Stop Hooks — 对标 Claude Code: exit code ≠ 0 → 回循环
        #
        # After Agent reports "done", run tsc/pytest/lint automatically.
        # Pass → proceed. Fail → inject failure as system message + re-loop
        # (max 1 extra loop to prevent infinite fix-attempt cycles).
        # ═══════════════════════════════════════════════════════════
        try:
            _hook_results = await self._run_stop_hooks(task_id, session_id, agent_id, model_cfg)
            if not _hook_results.get("all_passed"):
                _fail_summary = _hook_results.get("summary", "")
                _fail_detail = "\n".join(
                    r.get("detail", str(r))
                    for r in _hook_results.get("results", [])
                    if not r.get("passed", True)
                )[:SUB_AGENT_GENERATED_RESPONSE_MAX_CHARS]
                await self._safe_send(task_id, {
                    "type": "stop_hooks_failed",
                    "task_id": task_id, "session_id": session_id,
                    "results": _hook_results.get("results", []),
                    "summary": _fail_summary,
                })
                _log.warning("Stop hooks failed for task %s, re-looping: %s", task_id[:LOG_PREVIEW_TASK_ID_LEN], _fail_summary)
                # 注入失败信息→重新进入工具循环（最多 1 次额外循环）
                context["history"].append({
                    "role": "system",
                    "content": (
                        f"[自动验证失败] {_fail_summary}\n\n"
                        f"### 失败详情\n{_fail_detail}\n\n"
                        f"请修复上述问题后重新提交。不要做其他改动。"
                    ),
                })
                full_response = await self._execute_tool_loop(
                    adapter, context, task_id, agent_id, session_id, model_cfg, prompt,
                )
                await self.push_stream_done(task_id)
        except Exception:
            _log.debug("Stop hooks execution failed", exc_info=True)

        # ── Phase 4: 收尾——Token统计 · 持久化 · Plan解析 · 进化 · 压缩 · 标记完成 ──
        await self._finalize_response(
            task_id, agent_id, session_id, prompt, full_response, mode,
            model_cfg, adapter, user_conv_id, user_msg_id_str, elapsed,
        )

        # ── Phase 76-A: 项目级 behavior 偏好持久化（best-effort）──
        # 若 extra_context 含 selected_behaviors 且 project_id 非空，调用
        # PromptAgent.save_project_behavior_preference 保存到 project_preferences 表，
        # 下次同项目自动复用。失败不阻断主流程。
        try:
            _ctx = extra_context or {}
            _selected_behaviors = _ctx.get("selected_behaviors")
            _project_id = _ctx.get("project_id")
            if (
                _selected_behaviors
                and _project_id
                and isinstance(_selected_behaviors, list)
                and self._prompt_agent is not None
            ):
                await self._prompt_agent.save_project_behavior_preference(
                    _project_id, _selected_behaviors,
                )
        except Exception:
            _log.debug("Phase 76-A: save project behavior preference failed", exc_info=True)

        await self._write_task_snapshot(task_id, {
            "status": "completed", "agent_id": agent_id, "session_id": session_id,
            "output": full_response[:SUB_AGENT_GENERATED_RESPONSE_MAX_CHARS],
        })

        # Phase 80: 发布 TASK_COMPLETED 事件（best-effort，为 Supervisor 监控铺路）
        if self._event_bus:
            try:
                from services.event_bus import Events
                await self._event_bus.publish(
                    Events.TASK_COMPLETED,
                    {"task_id": task_id, "agent_id": agent_id},
                )
            except Exception:
                _log.debug("publish TASK_COMPLETED failed", exc_info=True)

    except asyncio.CancelledError:
        _log.info(f"Agent {agent_id} task {task_id} cancelled")
        await self._write_task_snapshot(task_id, {
            "status": "cancelled", "agent_id": agent_id, "session_id": session_id,
        })
        await self._mark_task(task_id, "cancelled", "Task cancelled by user",
                              expected_from="running")
        raise  # 重新抛出让 asyncio 正确传播取消
    except Exception as e:
        _log.exception(f"Agent {agent_id} task {task_id} failed")
        await self.push_error(task_id, str(e))
        await self._write_task_snapshot(task_id, {
            "status": "failed", "agent_id": agent_id, "session_id": session_id,
            "error": str(e)[:SUB_AGENT_ERROR_PREVIEW_CHARS],
        })
        # Phase 47: 保存部分响应（不在此处改 status——交由 _mark_task 统一走乐观锁）
        try:
            if full_response:
                cursor = await self.db.execute(
                    "UPDATE tasks SET output=? WHERE id=? AND status='running'",
                    (full_response[:SUB_AGENT_FULL_RESPONSE_LOG_PREVIEW_CHARS], task_id),
                )
                await self.db.commit()
        except Exception as _de:
            # Phase 48 T3-15: DB 写失败必上报（原 except: pass 静默吞咽 → 任务状态丢失无日志）
            _log.error("Failed to persist partial response for task %s: %s", task_id, _de)
        # D3: 乐观锁——仅 running→failed 生效，避免覆盖竞态中已被取消的任务
        await self._mark_task(task_id, "failed", str(e), expected_from="running")

        # Phase 80: 发布 TASK_FAILED 事件（best-effort，为 Supervisor 监控铺路）
        if self._event_bus:
            try:
                from services.event_bus import Events
                await self._event_bus.publish(
                    Events.TASK_FAILED,
                    {"task_id": task_id, "agent_id": agent_id, "error": str(e)},
                )
            except Exception:
                _log.debug("publish TASK_FAILED failed", exc_info=True)
    finally:
        await discard_task_interactions(self, task_id)
        if lang_token is not None:
            try:
                reset_current_language(lang_token)
            except Exception:
                pass  # 回退：语言上下文重置失败不影响后续流程
        self._task_handles.pop(task_id, None)


async def _get_model_used(self, task_id: str) -> dict:
    """从任务 task_state 读取实际使用的模型信息（best-effort）。"""
    try:
        state = await self.get_task_state(task_id)
        return (state or {}).get("task_state", {}).get("model_used", {})
    except Exception:
        return {}  # 回退：读不到任务状态时按"没有模型信息"处理


async def spawn_subagent(
    self, agent_id: str, prompt: str,
    context_summary: str = "", auto_approve: bool = False,
    language: str = "zh",
    max_mode: bool = False,
    long_task_step_role: str = "",
    model_tier: str = "",
    capability_level: int = 1,
    reasoning_effort: str = "",
) -> dict:
    """Phase 43 W.4: 独立上下文子 Agent。

    与 delegation 的区别: 子 Agent 有自己的 session_id（独立上下文窗口），
    不污染当前对话的上下文。结果以 summary 形式返回。
    对标 Claude Code AgentTool / Cursor Cloud Agents。

    P0-4.3: 通过 _subagent_sem 限制并发子 Agent 数（MAX_CONCURRENT_TASKS=20）。
    超过限流的调用会排队等待，防止 Agent 失控 spawn 海量子任务耗尽资源。

    修复 1.5 (FUNCTIONAL P0-1): auto_approve=True 时子 Agent 在预审批范围内
    自动运行（长任务用），不弹确认弹窗。对标 Claude Code spawn sub-agent 预审批。

    能力策略门控（Phase 108+）: 由 capability_level 推导功能开关集合
    （capability_flags_for_level）——轻量档（0）禁用子 Agent 调用，
    这是「能力强度 = 功能策略而非模型选择」的体现。
    """
    from config import capability_flags_for_level

    # 轻量档（MINIMAL）禁用子 Agent 调用——返回明确错误而非崩溃，
    # 长任务窗口翻滚（level>=1 才启用）不受影响。
    if not capability_flags_for_level(capability_level).get("subagent_spawn"):
        return {
            "ok": False,
            "error": "当前能力策略（轻量）已禁用子 Agent 调用；请切换到「标准」或「全功能」。",
        }
    # P0-4.3: 并行限流——async with 在获取 semaphore 前不创建任务，
    # 排队中的调用不占 DB 行/内存（仅占调用方 coroutine 栈）
    async with self._subagent_sem:
        sub_session_id = str(_uuid.uuid4())
        sub_task_id = str(_uuid.uuid4())

        # 构建独立上下文——不含当前对话历史
        sub_context = ""
        if context_summary:
            sub_context = f"[子Agent独立上下文]\n{context_summary}\n\n"

    # P0 修复: 不再预 INSERT 任务行——原实现先插 sub_task_id 行再由
    # start_task 插第二行，首行无人更新成永久 running 孤儿，重启后被
    # resume_after_restart 当崩溃任务重跑。现通过显式 task_id 复用单行。
        try:
            # Phase 108: 真实子任务树——子 Agent 的 parent_task_id 指向
            # 调用方（当前正在执行的）任务。spawn_subagent 在父任务工具循环内
            # 被调用，此时 ContextVar 已设为父 task_id。无父（独立触发）则为 None。
            try:
                from context import get_current_task_id
                _parent_task_id = get_current_task_id() or None
            except LookupError:
                _parent_task_id = None
            extra_context = {
                "max_mode": max_mode,
                "long_task_step_role": long_task_step_role,
                "model_tier": model_tier,
                "capability_level": capability_level,
                "reasoning_effort": reasoning_effort,
            }
            task_id = await self.start_task(
                agent_id, sub_context + prompt, sub_session_id,
                extra_context=extra_context, language=language,
                task_id=sub_task_id, parent_task_id=_parent_task_id,
            )
            # 修复 1.5 (FUNCTIONAL P0-1): 预审批标志——子 Agent 的 agent_tool_loop
            # 读此跳过确认弹窗。start_task 返回的 task_id 是实际执行 agent loop 的任务。
            # 设在 start_task 之后：bg task 先调 LLM（网络 I/O 耗时秒级），
            # 标志在首个工具调用前就已就位，无竞态。
            if auto_approve:
                self._task_auto_approve[task_id] = True
            # Phase 56: 事件驱动等待替代固定轮询
            event = asyncio.Event()
            self._task_completion_events[task_id] = event
            try:
                # 先查一次，防止任务在注册事件前已完成
                cursor = await self.db.execute(
                    "SELECT status, output FROM tasks WHERE id=?", (task_id,),
                )
                row = await cursor.fetchone()
                if row and row[0] in ("completed", "failed"):
                    return {
                        "ok": row[0] == "completed",
                        "task_id": task_id,
                        "output": (row[1] or "")[:SUB_AGENT_OUTPUT_PREVIEW_CHARS],
                        "model_used": await _get_model_used(self, task_id),
                    }
                await asyncio.wait_for(
                    event.wait(),
                    timeout=SUB_AGENT_POLL_MAX_SECONDS,
                )
                cursor = await self.db.execute(
                    "SELECT status, output FROM tasks WHERE id=?", (task_id,),
                )
                row = await cursor.fetchone()
                if row and row[0] in ("completed", "failed"):
                    return {
                        "ok": row[0] == "completed",
                        "task_id": task_id,
                        "output": (row[1] or "")[:SUB_AGENT_OUTPUT_PREVIEW_CHARS],
                        "model_used": await _get_model_used(self, task_id),
                    }
                return {"ok": False, "error": "Sub-agent state inconsistent after event"}
            except asyncio.TimeoutError:
                # P0-1.3: 超时后必须 cancel 子 Agent asyncio.Task——
                # 原代码仅返回错误但未 cancel，留下孤儿任务持续消耗资源（LLM 调用/内存）
                # 对标 Claude Code AgentTool 超时后自动 abort 子 agent
                _log.warning(
                    "Sub-agent %s timed out after %ds, cancelling",
                    task_id, SUB_AGENT_POLL_MAX_SECONDS,
                )
                try:
                    await self.cancel_task(task_id)
                except Exception as cancel_exc:
                    _log.error(
                        "Failed to cancel timed-out sub-agent %s: %s",
                        task_id, cancel_exc,
                    )
                return {"ok": False, "error": f"Sub-agent timeout ({SUB_AGENT_POLL_MAX_SECONDS}s)"}
            finally:
                self._task_completion_events.pop(task_id, None)
                # 修复 1.5: 清理 per-task auto_approve 标志避免内存泄漏
                self._task_auto_approve.pop(task_id, None)
        except Exception as e:
            return {"ok": False, "error": str(e)}


async def spawn_subagent_team(
    self,
    specs: list[dict],
    aggregate: bool = False,
    language: str = "zh",
) -> dict:
    """Phase 9-X: 并行 fan-out 多个内部子 Agent（非外部）执行并聚合结果。

    对标 Claude Code 的多并行子任务 / Agent 团队。允许单个用户请求拆成多个
    独立子任务，并行拉起、并发等待、统一回收。

    specs: list[dict]，每个元素：
      - agent_id: str            目标内部 Agent（默认 DEFAULT_AGENT_ID）
      - prompt: str              自包含任务描述（子 Agent 不继承当前对话历史）
      - context_summary: str     可选背景摘要
      - model_tier: str          可选（lite/normal/max/vision）
      - max_mode: bool           可选，强制 MAX tier
      - long_task_step_role: str 可选，分级路由 hint

    外部 Agent 不进入 fan-out（遵守 Phase 9-X 的 opt-in 规则——必须由用户
    显式选择才调用），会被静默跳过并计入 skipped_external。

    aggregate=True 时，将所有子 Agent 的输出做结构化合并（按 agent 分段），
    生成一个统一的 aggregated_output 文本，方便主 Agent 直接消费。
    （注：当前为结构化合并不调用额外 LLM，避免引入新的模型调用脆弱点；
     后续如需 LLM 综合可在此接入。）
    """
    if not specs:
        return {"ok": False, "error": "specs 不能为空（至少需要一个子 Agent 规格）"}

    clean_specs: list[dict] = []
    skipped_external: list[str] = []
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        aid = spec.get("agent_id") or DEFAULT_AGENT_ID
        if aid in EXTERNAL_AGENT_IDS:
            # 外部 Agent 不参与内部 fan-out——保持 opt-in 规则
            skipped_external.append(aid)
            continue
        clean_specs.append(spec)

    if not clean_specs:
        return {
            "ok": False,
            "error": "没有可用的内部子 Agent（外部 Agent 不进入 team）",
            "skipped_external": skipped_external,
        }

    async def _run_one(spec: dict):
        aid = spec.get("agent_id") or DEFAULT_AGENT_ID
        res = await spawn_subagent(
            self, aid, spec.get("prompt", ""),
            context_summary=spec.get("context_summary", ""),
            auto_approve=True,
            language=language,
            max_mode=spec.get("max_mode", False),
            long_task_step_role=spec.get("long_task_step_role", ""),
            model_tier=spec.get("model_tier", ""),
        )
        return aid, res

    gathered = await asyncio.gather(
        *[_run_one(s) for s in clean_specs],
        return_exceptions=True,
    )

    results: dict[str, dict] = {}
    for item in gathered:
        if isinstance(item, Exception):
            key = f"_error_{_uuid.uuid4().hex[:8]}"
            results[key] = {"ok": False, "error": str(item)}
            continue
        aid, res = item
        # 同 agent 多次出现时去重 key（保留 agent_id 可读性）
        base = aid
        suffix = 1
        while base in results:
            suffix += 1
            base = f"{aid}_{suffix}"
        results[base] = res

    team_result: dict = {
        "ok": True,
        "agent_count": len(clean_specs),
        "results": results,
        "skipped_external": skipped_external,
    }

    if aggregate:
        parts = []
        for aid, res in results.items():
            out = (res or {}).get("output", "")
            if (res or {}).get("ok") and out:
                parts.append(f"### 子 Agent: {aid}\n{out}")
            else:
                err = (res or {}).get("error", "无输出")
                parts.append(f"### 子 Agent: {aid}\n(失败: {err})")
        team_result["aggregated_output"] = "\n\n".join(parts)

    return team_result


async def execute_plan_steps(self, parent_task_id: str, agent_id: str) -> None:
    """按顺序执行 Plan 的所有步骤。每步完成后用上一步结果作为下一步上下文。

    Plan 完成时 task.status=completed 且 task.output 为结构化 Plan JSON。
    步骤为 parent_task_id 指向此 Plan 的子任务（status=pending）。

    审查 C-1: 顶层 try/except 兜底——get_plan_steps DB 异常 / 后处理异常时
    标记 parent_task failed，避免永久卡 running（fire-and-forget 调用，
    add_done_callback 只记日志不回写状态）。
    """
    try:
        # 委托模式：_execute_plan_steps_impl 是本模块的函数（接收 self 首参），
        # 而非 AgentService 绑定方法——必须用 _execute_plan_steps_impl(self, ...) 调用。
        await _execute_plan_steps_impl(self, parent_task_id, agent_id)
    except Exception as exc:
        _log.exception("execute_plan_steps raised for task %s: %s", parent_task_id, exc)
        # best-effort 回写 failed（避免回写本身抛异常）
        try:
            await self.db.execute(
                "UPDATE tasks SET status='failed', error_message=? WHERE id=?",
                (f"execute_plan_steps exception: {exc}"[:LOG_PREVIEW_ERROR_LEN * 2], parent_task_id),  # 400 chars
            )
            await self.db.commit()
        except Exception:
            _log.debug("Failed to mark task %s failed after execute_plan_steps exception",
                       parent_task_id, exc_info=True)
        try:
            await self.push_error(
                parent_task_id, f"execute_plan_steps exception: {exc}"[:LOG_PREVIEW_ERROR_LEN],
            )
        except Exception:
            pass


async def _execute_plan_steps_impl(self, parent_task_id: str, agent_id: str) -> None:
    """execute_plan_steps 的实现（被 try/except 包裹）"""
    from repositories import get_plan_steps

    steps = await get_plan_steps(self.db, parent_task_id)
    if not steps:
        await self.push_error(parent_task_id, "Plan has no steps to execute")
        return

    total = len(steps)
    completed_outputs: list[str] = []

    for idx, step in enumerate(steps):
        step_id = step["id"]
        step_input = step.get("input", "")

        # 重试时跳过已完成步骤——收集 output 作为后续上下文
        if step.get("status") == "completed" and step.get("output"):
            completed_outputs.append(step["output"])
            await self._safe_send(parent_task_id, {
                "type": "plan_step_done",
                "task_id": parent_task_id,
                "step_id": step_id,
                "step_index": idx,
                "step_task": step["input"],
                "total_steps": total,
                "output": f"(跳过——已完成) {step['output'][:SUB_AGENT_PLAN_STEP_OUTPUT_PREVIEW_CHARS]}",
            })
            continue

        # 更新步骤状态为 running
        await self.db.execute(
            "UPDATE tasks SET status='running' WHERE id=?", (step_id,),
        )
        await self.db.commit()

        await self._safe_send(parent_task_id, {
            "type": "plan_step_start",
            "task_id": parent_task_id,
            "step_id": step_id,
            "step_index": idx,
            "step_task": step["input"],
            "total_steps": total,
        })

        try:
            # 构建上下文：前面步骤的简要输出
            context_prompt = step["input"]
            if completed_outputs:
                prev_summary = "\n".join(
                    f"步骤 {i+1} 完成: {out[:SUB_AGENT_PLAN_STEP_OUTPUT_PREVIEW_CHARS]}"
                    for i, out in enumerate(completed_outputs)
                )
                context_prompt = (
                    f"## 上下文（前面已完成的步骤）\n{prev_summary}\n\n"
                    f"## 当前任务\n{step['input']}"
                )

            # 调用 _run_agent（不走 Commander——Plan 执行用轻量上下文）
            adapter = self.adapters.get(agent_id)
            if not adapter:
                await self.db.execute(
                    "UPDATE tasks SET status='failed', error_message=? WHERE id=?",
                    (f"Agent {agent_id} not found", step_id),
                )
                await self.db.commit()
                _evt = self._task_completion_events.pop(step_id, None)
                if _evt:
                    _evt.set()
                continue

            if not adapter._ready.is_set():
                await adapter.start()
                await adapter.wait_ready()

            from prompts.commander import COMMANDER_ADAPTER_PROMPT
            sys_prompt = COMMANDER_ADAPTER_PROMPT.format(
                memory_context=f"你是 Plan 执行者。当前是第 {idx+1}/{total} 步。完成此步骤后报告结果。"
            )
            model_cfg = await self._get_model_config("primary")
            context = {"system": sys_prompt, "memory": "", "model_config": model_cfg}

            # Task 6.16: 共享重试策略——adapter.stream() 是网络调用，瞬时错误
            # （超时/限流/连接重置）自动重试 _MAX_RETRIES 次（指数退避）。
            # 不可重试错误（如配置错误）直接抛出 → 外层 except 标 failed → break。
            async def _collect_stream_output() -> str:
                chunks: list[str] = []
                async for chunk in adapter.stream(context_prompt, context=context):
                    # Phase 20: 向后兼容 — 只取 text 类型 chunk
                    if isinstance(chunk, str):
                        chunk = {"type": "text", "data": chunk}
                    ctype = chunk.get("type")
                    if ctype == "text":
                        chunks.append(chunk["data"])
                    elif ctype == "stream_error":
                        # 2026-09-10: 原实现静默吞掉 stream_error（只收集 text）→ 限流/5xx
                        # 时产出空结果被当成功。现抛结构化异常：可重试码交 retry_async
                        # （带服务器 Retry-After 退避），永久性错误直接冒泡给上层标失败。
                        from services.llm_errors import LlmCallError

                        raise LlmCallError.from_stream_error(chunk)
                return "".join(chunks)

            output = await _retry_async(
                _collect_stream_output,
                max_retries=_MAX_RETRIES,
                base_delay=_RETRY_BASE_DELAY,
                label=f"plan step {idx+1}/{total} stream",
            )
            completed_outputs.append(output)

            # Phase 18: 真执行——解析输出中的文件操作并实际写入
            # Batch 3b B1: 副作用统一走 ToolService（架构铁律 #1——审计事件 + 文件检查点
            # + 串行锁）。write_file/run_command 均为 confirmable 工具，直接 execute() 只
            # 返回 preview 不落盘，必须走 execute_confirmable(auto_approve=True) 的
            # preview→dispatch_confirm 全链。plan 步骤是后台自动执行（无用户交互轮），
            # 与原直接 write_text/subprocess.run 的"自动执行"语义一致。
            from services.plan_parser import extract_file_operations, extract_commands, detect_file_changes
            from security.sandbox import get_workspace
            from pathlib import Path as _Path

            file_ops = extract_file_operations(output, model_cfg)
            files_actually_written: list[str] = []
            _ts = await self._ensure_tool_service()
            for fop in file_ops:
                try:
                    ws = get_workspace()
                    # 传绝对路径——write_file preview 以 shared 为 base 解析相对路径，
                    # 绝对路径经 validate_path 边界校验后与原 ws/fop["path"] 落点等价
                    full = _Path(ws) / fop["path"]
                    result = await _ts.execute_confirmable(
                        self.db, "write_file",
                        {"path": str(full), "content": fop["content"], "mode": "overwrite"},
                        agent_id=agent_id, auto_approve=True, task_id=parent_task_id,
                    )
                    if result.get("success"):
                        files_actually_written.append(fop["path"])
                    else:
                        _log.warning(
                            f"Plan step write_file rejected for {fop['path']}: "
                            f"{result.get('error', '')}"
                        )
                except Exception as we:
                    _log.warning(f"Plan step file write failed for {fop['path']}: {we}")

            commands = extract_commands(output, model_cfg)
            for cmd in commands[:2]:
                try:
                    # Batch 3b B1: run_command 全链——白名单 + LLM 安全分析在
                    # _validate_and_prepare，实际执行走异步 _execute_command
                    # （timeout_sec=30 保持原 subprocess timeout=30 等价，且不再
                    # 阻塞事件循环；cwd 由其内部统一为 workspace 根）
                    result = await _ts.execute_confirmable(
                        self.db, "run_command",
                        {"command": cmd, "timeout_sec": 30},
                        agent_id=agent_id, auto_approve=True, task_id=parent_task_id,
                    )
                    if not result.get("success"):
                        _log.warning(
                            f"Plan step command rejected: {cmd[:LOG_PREVIEW_CMD_LEN]} - "
                            f"{result.get('error', '')}"
                        )
                except Exception as ce:
                    _log.warning(f"Plan step command failed: {cmd[:LOG_PREVIEW_CMD_LEN]} - {ce}")

            files_changed = detect_file_changes(output, model_cfg)
            if files_actually_written:
                files_changed = files_actually_written

            await self.db.execute(
                "UPDATE tasks SET status='completed', output=? WHERE id=?",
                (output, step_id),
            )
            await self.db.commit()
            # Phase 56: 通知可能正在等待此任务完成的协程
            _evt = self._task_completion_events.pop(step_id, None)
            if _evt:
                _evt.set()

            await self._safe_send(parent_task_id, {
                "type": "plan_step_done",
                "task_id": parent_task_id,
                "step_id": step_id,
                "step_index": idx,
                "step_task": step["input"],
                "total_steps": total,
                "output": output[:SUB_AGENT_PLAN_STEP_OUTPUT_PREVIEW_CHARS],
                "files_changed": files_changed,
                "files_written": files_actually_written,
            })

        except Exception as e:
            _log.exception(f"Plan step {idx+1}/{total} failed")
            await self.db.execute(
                "UPDATE tasks SET status='failed', error_message=? WHERE id=?",
                (str(e), step_id),
            )
            await self.db.commit()
            # Phase 56: 通知可能正在等待此任务失败的协程
            _evt = self._task_completion_events.pop(step_id, None)
            if _evt:
                _evt.set()

            await self._safe_send(parent_task_id, {
                "type": "plan_step_fail",
                "task_id": parent_task_id,
                "step_id": step_id,
                "step_index": idx,
                "step_task": step["input"],
                "total_steps": total,
                "error": str(e),
            })
            # 失败不继续——后续步骤保持 pending
            break

    # 只有全部成功才发 plan_all_done（失败时已 break，不发此消息）
    all_done = all(
        s.get("status") == "completed"
        for s in await get_plan_steps(self.db, parent_task_id)
    )
    if all_done:
        await self._safe_send(parent_task_id, {
            "type": "plan_all_done",
            "task_id": parent_task_id,
        })
