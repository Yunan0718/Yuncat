"""services/agent_roles.py —— Agent 角色调用模块

从 agent_service.py 拆分: _invoke_with_role / _invoke_with_role_and_tools。
采用委托模式: 模块级函数接收 self 作为首参。
"""
import logging
import uuid as _uuid

from config import (
    calc_tool_result_limit, calc_list_limit, calc_context_budget, OPENCLAW_AGENT_ID,
    protect_critical_info, adaptive_truncate,
    AGENT_CONTEXT_PERSONALIZATION_LIMIT, AGENT_CONTEXT_MEMORY_TREE_LIMIT,
    AGENT_ROLES_TOOL_CALLS_SEEN_LIMIT,
)
from prompts.commander import OUTPUT_LANGUAGE_RULE_TEMPLATE

_log = logging.getLogger("yuncat.agent")


def _build_role_system_prompt(role_prompt: str, language: str = "zh") -> str:
    """在角色 prompt 末尾追加输出语言规则，保持与 Commander 一致。"""
    if language == "zh":
        return role_prompt
    return f"{role_prompt}\n\n{OUTPUT_LANGUAGE_RULE_TEMPLATE.format(language=language)}"


async def invoke_with_role(
    self, agent_id: str, role_prompt: str, task_input: str = "",
    model_key: str = "primary",
    provider_config: dict | None = None,
    language: str = "zh",
) -> str:
    """为指定角色调用 adapter——不走 Commander prompt，不走 DB 写入。

    Phase 23: model_key 按角色分级——Architect/Reviewer 用 flash，
    Editor/Builder 用 pro。由 DeliberationService._get_role_model() 计算。

    Phase 43 P1-SPEC D: provider_config 可选——当指定时替代 _get_model_config() 的返回值，
    实现多 Provider 角色路由（Architect→DeepSeek, Reviewer→Anthropic）。
    """
    adapter = self.adapters.get(agent_id)
    if not adapter:
        raise ValueError(f"Agent {agent_id} not found")

    if not adapter._ready.is_set():
        await adapter.start()
        await adapter.wait_ready()

    if provider_config and provider_config.get("api_key"):
        model_cfg = provider_config
    else:
        model_cfg = await self._get_model_config(model_key)

    # Phase 56-P2: 角色上下文注入预算按当前模型窗口动态计算
    _personal_limit = calc_list_limit(model_cfg, AGENT_CONTEXT_PERSONALIZATION_LIMIT, fraction_per_100k=1, max_limit=6)
    _memtree_limit = calc_list_limit(model_cfg, AGENT_CONTEXT_MEMORY_TREE_LIMIT, fraction_per_100k=1, max_limit=10)
    _tool_preview = calc_context_budget(model_cfg, fraction=0.003, min_chars=250, max_chars=1000)
    _delegation_preview = calc_context_budget(model_cfg, fraction=0.003, min_chars=250, max_chars=1000)

    # Phase 43 P3.3: 用真实 memory context 替代 "" —— 讨论赛角色需要个性化上下文
    memory_ctx = ""
    try:
        from prompts.memory_context import render_memory_context
        # 角色调用没有 session_id —— 只注入 MemoryTree 相关记忆 + 个性化
        memory_ctx = render_memory_context(
            session_context=[],
            semantic_results=[],
            summary="",
        )
        # 附加个性化偏好（FeedbackLearner）
        if self._feedback_learner:
            try:
                personalization = await self._feedback_learner.get_personalization_context(limit=_personal_limit)
                if personalization:
                    memory_ctx = memory_ctx + "\n\n" + personalization
            except Exception:
                _log.debug("获取个性化偏好失败", exc_info=True)
        # B: 用户明确表达的偏好注入（源隔离 source="user_preference"）
        # 角色调用无 session/workspace，仅注入全局偏好（project_id=None）。
        try:
            from context import get_app
            _app = get_app()
            _db = getattr(_app.state, "db", None) if _app and hasattr(_app, "state") else None
            if _db is not None:
                from services.user_preferences import build_preference_snippet
                _prefs = await build_preference_snippet(_db, project_id=None, limit=_personal_limit)
                if _prefs:
                    memory_ctx = memory_ctx + "\n\n" + _prefs
        except Exception:
            _log.debug("获取用户偏好失败", exc_info=True)
        # 附加 MemoryTree 检索
        if self._memory_tree:
            try:
                tree_ctx = await self._memory_tree.retrieve_context(task_input or role_prompt, limit=_memtree_limit)
                if tree_ctx:
                    memory_ctx = memory_ctx + "\n\n## 知识树\n" + tree_ctx
            except Exception:
                _log.debug("MemoryTree 检索失败", exc_info=True)
    except Exception:
        _log.debug("构建角色记忆上下文失败", exc_info=True)

    context = {
        "system": _build_role_system_prompt(role_prompt, language),
        "memory": memory_ctx,
        "model_config": model_cfg,
    }

    chunks: list[str] = []
    async for chunk in adapter.stream(task_input, context=context):
        if isinstance(chunk, str):
            chunk = {"type": "text", "data": chunk}
        if chunk.get("type") == "text":
            chunks.append(chunk["data"])
        # Phase 43 P3.1b: 处理 tool_call — OC/HA 后端可直接调工具
        elif chunk.get("type") == "tool_call":
            import json as _json
            for tc in chunk.get("data", []):
                fn = tc.get("function", {}).get("name", "")
                try:
                    raw_args = tc.get("function", {}).get("arguments", "{}")
                    args = _json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except (_json.JSONDecodeError, TypeError):
                    args = {}
                # 路由决定谁执行此工具
                if fn in getattr(self, '_openclaw_tool_names', set()):
                    # OC 原生工具 → 走 OC Gateway
                    oc = self.adapters.get(OPENCLAW_AGENT_ID)
                    if oc and hasattr(oc, "execute_tool"):
                        try:
                            result = await oc.execute_tool(fn, args)
                            chunks.append(f"\n[Tool: {fn} (OpenClaw)] {_json.dumps(result, ensure_ascii=False)[:_tool_preview]}\n")
                        except Exception as e:
                            chunks.append(f"\n[Tool: {fn} failed: {e}]\n")
                elif fn == "delegate_to_agent":
                    # 跨后端委托
                    from tools.builtin.system_tools import delegate_to_agent as _dta
                    result = await _dta(
                        target_agent=args.get("target_agent", "hermes"),
                        tool_name=args.get("tool_name", ""),
                        arguments=args.get("arguments"),
                    )
                    chunks.append(f"\n[委托结果] {_json.dumps(result, ensure_ascii=False)[:_delegation_preview]}\n")
                elif self._tool_service:
                    # Yuncat 统一工具
                    try:
                        result = await self._tool_service.execute(fn, args, agent_id=agent_id)
                        chunks.append(f"\n[Tool: {fn}] {_json.dumps(result, ensure_ascii=False)[:_tool_preview]}\n")
                    except Exception as e:
                        chunks.append(f"\n[Tool: {fn} failed: {e}]\n")
    return "".join(chunks)


async def invoke_with_role_and_tools(
    self, agent_id: str, role_prompt: str, task_input: str,
    model_key: str = "primary", max_tool_rounds: int = 30,  # Phase 48-H: 3→30 对标主流无低上限
    task_id: str = "", override_prompt: str = None,
    provider_config: dict | None = None,
    language: str = "zh",
) -> str:
    """为指定角色调用 adapter——含工具循环，但保持 clean context。

    与 _invoke_with_role 的区别: 有 tool_call → ToolService.execute() → 结果回注。
    与 _run_agent 的区别: 无 Commander prompt、无 MemoryTree、无对话历史、不写 DB。

    Phase 44 预留: override_prompt 参数（Phase 43 暂不使用，但签名预留避免后续改接口）。
    provider_config: 多 Provider 路由时传入，替代 _get_model_config() 返回值。
    """
    import json as _json
    adapter = self.adapters.get(agent_id)
    if not adapter:
        raise ValueError(f"Agent {agent_id} not found")
    if not adapter._ready.is_set():
        await adapter.start()
        await adapter.wait_ready()

    if provider_config and provider_config.get("api_key"):
        model_cfg = provider_config
    else:
        model_cfg = await self._get_model_config(model_key)
    from config import resolve_context_window
    _window = resolve_context_window(model_cfg)
    context = {
        "system": _build_role_system_prompt(role_prompt, language),
        "memory": "",
        "model_config": model_cfg,
        "history": [],
    }

    _task_id = task_id or str(_uuid.uuid4())
    _prompt = override_prompt or task_input
    full_output = ""

    for _round in range(max_tool_rounds):
        tool_calls_seen: list[dict] = []
        _round_text = ""
        async for chunk in adapter.stream(
            _prompt if not full_output else None, context=context,
        ):
            if isinstance(chunk, str):
                chunk = {"type": "text", "data": chunk}
            if chunk.get("type") == "text":
                _round_text += chunk["data"]
                full_output += chunk["data"]
            elif chunk.get("type") == "tool_call":
                data = chunk["data"]
                if isinstance(data, list):
                    tool_calls_seen.extend(data)
                else:
                    tool_calls_seen.append(data)

        if not tool_calls_seen:
            break

        # 执行工具 — 每轮最多 8 个（Spec 实现场景：controlled scope，防 Token 爆炸）
        ts = self._tool_service
        if ts is None:
            _log.warning("_tool_service is None in _invoke_with_role_and_tools — 工具调用将跳过")
            from services.tools.tool_service import ToolService
            ts = ToolService()
        for tc in tool_calls_seen[:AGENT_ROLES_TOOL_CALLS_SEEN_LIMIT]:
            if not isinstance(tc, dict):
                continue
            # Phase 48 T3-18: 统一 OpenAI Function Calling 格式
            # （原 tc.get("name")/tc.get("args") 与 invoke_with_role L85 的
            #   tc.get("function",{}).get("name") 格式不一致 → 后端返回的
            #   delta.tool_calls 永远取不到 name/args → 工具调用静默失败）
            fn_name = tc.get("function", {}).get("name", "")
            if not fn_name:
                continue
            try:
                raw_args = tc.get("function", {}).get("arguments", "{}")
                fn_args = _json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except (_json.JSONDecodeError, TypeError):
                fn_args = {}
            try:
                result = await ts.execute(
                    fn_name, fn_args,
                    agent_id=agent_id, task_id=_task_id,
                )
                result_str = _json.dumps(result, ensure_ascii=False)
            except Exception as exc:
                result_str = _json.dumps({"error": str(exc)})

            # Phase 43 P0-DYN: 自适应截断 (与 _execute_tool_loop 统一)
            _limit = calc_tool_result_limit(model_cfg, len(tool_calls_seen[:AGENT_ROLES_TOOL_CALLS_SEEN_LIMIT]))
            if len(result_str) > _limit:
                _critical = protect_critical_info(result_str)
                _dump_ref = self._micro_dump_tool_output(result_str)
                _truncated = (
                    adaptive_truncate(result_str, _window, _limit, 0.5)
                    + f"\n\n[... 已截断 {len(result_str)} 字符。"
                )
                if _critical:
                    _truncated += f"\n关键信息:\n{_critical}"
                if _dump_ref:
                    _truncated += f"\n完整内容: {_dump_ref}]"
                _truncated += result_str[-int(_limit * 0.3):]
                result_str = _truncated

            context["history"].append({
                "role": "tool",
                "content": result_str,
            })

        _prompt = ""

    return full_output
