"""services/agent_tool_helpers.py —— Agent 工具辅助模块

从 agent_service.py 拆分: _stream_with_fallback / _run_stop_hooks /
_analyze_tool_failure_and_send / _extract_memory_from_tool_result。
采用委托模式: 模块级函数接收 self 作为首参。
"""
import asyncio
import logging
import time as _time

from config import (protect_critical_info, adaptive_truncate,
                    calc_tool_result_limit, calc_context_budget, DEFAULT_AGENT_ID,
                    MAX_TOKENS_AGENT_TOOL_RESULT, ADAPTER_HEALTH_CHECK_TIMEOUT,
                    AGENT_TOOL_HELPERS_FALLBACK_CHAIN_LIMIT,
                    AGENT_TOOL_HELPERS_ERROR_PREVIEW_CHARS,
                    AGENT_TOOL_HELPERS_STDOUT_PREVIEW_CHARS,
                    AGENT_TOOL_HELPERS_STDOUT_TYPECHECK_PREVIEW_CHARS,
                    AGENT_TOOL_HELPERS_PROTECTED_RESULT_PREVIEW_CHARS,
                    AGENT_TOOL_HELPERS_BUG_ID_MAX_LEN,
                    build_stop_hook_commands, STOP_HOOK_TIMEOUT_SEC)

_log = logging.getLogger("yuncat.agent")


def _install_tool_progress_sink(self, agent_id: str, task_id: str, context: dict) -> None:
    """把服务端工具进度（hermes.tool.progress）转成 Yuncat 的 tool_start / result 事件。

    外部 Agent 在服务端执行工具，Yuncat 收不到 tool_call_chunk；只有把这些进度
    桥接成既有 WS 事件，前端“行动链”才不是空的。
    """
    if not task_id or "_tool_progress_sink" in context:
        return

    async def _emit_tool_progress(payload: dict) -> None:
        status = str(payload.get("status") or "")
        tool = str(payload.get("tool") or payload.get("toolName") or "tool")
        label = str(payload.get("label") or "")
        if status in ("running", "started"):
            await self._safe_send(task_id, {
                "type": "tool_start", "task_id": task_id,
                "tool_name": tool, "arguments_preview": label,
                "agent_source": agent_id,
            })
        elif status:
            ok = status not in ("failed", "error")
            await self._safe_send(task_id, {
                "type": "tool_result_preview", "task_id": task_id,
                "tool_name": tool, "success": ok,
                "preview": label or ("已完成" if ok else status),
                "full_result": "",
            })
            await self._safe_send(task_id, {
                "type": "tool_done", "task_id": task_id, "tool_name": tool,
            })

    context["_tool_progress_sink"] = _emit_tool_progress


def _fallback_chain(agent_id: str, primary_provider: str, configured: list[str]) -> list[str]:
    """构造 Provider 降级链：外部 Agent 只调用一次，原生 Agent 才降级。"""
    from config import EXTERNAL_AGENT_IDS

    if agent_id in EXTERNAL_AGENT_IDS:
        return [primary_provider]
    return [primary_provider, *configured[:AGENT_TOOL_HELPERS_FALLBACK_CHAIN_LIMIT]]


async def stream_with_fallback(
    self, agent_id: str, prompt: str, context: dict,
    primary_provider: str, task_id: str = "",
):
    """尝试主 Provider → 失败自动降级到用户已配置的备选 Provider。

    与旧硬编码 FALLBACK_CHAIN 的区别:
      - 动态读取用户已配置的 Provider（_model_config.list_providers()）
      - 知情的——每级切换通过 _safe_send 推送到前端
      - 指数退避 + jitter——2s→4s→8s（cap 60s，Task 7.4）
      - 冷却期——降级后 5 分钟内不切回
      - 错误分类——401/403 直接报错（不降级），timeout/429 降级

    Task 7.4（重试退避加 jitter + Retry-After）:
      - 同一 provider 重试 1 次后再切换（仅在 stream 未产出 chunk 时重试，
        避免重复 chunk——对标 OpenAI/Anthropic SDK streaming retry 策略）
      - 退避加 jitter 防 thundering herd，backoff cap 16→60s
      - 异常含 retry_after 属性时优先使用（服务器指定，不加 jitter）
    """
    import time as _t
    import random as _random
    from config import resolve_context_window
    from retry_utils import MAX_BACKOFF, RETRY_JITTER_FACTOR

    adapter = self.adapters.get(agent_id); _install_tool_progress_sink(self, agent_id, task_id, context)
    if not adapter:
        _log.error("No adapter for agent %s", agent_id)
        if prompt:
            async for _ in []:
                yield {}
        return

    # 动态构建降级链——从用户已配置的 Provider 中读取
    fallback_chain: list[str] = [primary_provider]
    try:
        all_providers = await self._model_config.list_providers()
        configured = [
            p.get("provider", "") for p in all_providers
            if p.get("api_key_configured") and p.get("provider") != primary_provider
        ]
        fallback_chain = _fallback_chain(adapter, primary_provider, configured)
    except Exception:
        _log.debug("读取 Provider 列表失败，只用主 Provider", exc_info=True)

    last_error: str = ""
    backoff = 2  # provider 间切换的初始退避秒数
    degradation_cooldown: dict[str, float] = {}
    DEGRADE_COOLDOWN_SEC = 300  # 5 分钟冷却

    for idx, provider_id in enumerate(fallback_chain):
        # 冷却检查——刚降级的 Provider 不立刻尝试
        if idx > 0 and provider_id in degradation_cooldown:
            if _t.time() - degradation_cooldown[provider_id] < DEGRADE_COOLDOWN_SEC:
                continue

        # Task 7.4: 同一 provider 重试 1 次后再切换
        # 策略：provider 失败 → 若 stream 未产出 chunk 则退避重试 1 次 → 仍失败才切换
        # （stream 已产出 chunk 后不重试，避免重复 chunk——对标 OpenAI/Anthropic SDK streaming retry）
        chunk_yielded = False
        exc_obj: Exception | None = None

        for inner_attempt in range(2):  # 1 次初始 + 1 次重试
            if idx == 0:
                cfg = context.get("model_config", {})
            else:
                # 降级: 获取备选 Provider 配置
                try:
                    cfg = await self._model_config.get_provider_config(provider_id)
                    if not cfg or not cfg.get("api_key"):
                        break  # 配置失败，跳过此 provider
                except Exception:
                    break

                # WS 通知前端——用户需知道切换发生（仅首次尝试通知，避免重复刷屏）
                if hasattr(self, '_ws_connections') and task_id and inner_attempt == 0:
                    await self._safe_send(task_id, {
                        "type": "model_degraded",
                        "task_id": task_id,
                        "from_provider": primary_provider if idx == 1 else fallback_chain[idx - 1],
                        "to_provider": provider_id,
                        "reason": last_error[:AGENT_TOOL_HELPERS_ERROR_PREVIEW_CHARS] if last_error else "主 Provider 不可用",
                    })

            context["model_config"] = cfg
            try:
                async for chunk in adapter.stream(
                    prompt if idx == 0 else None, context=context,
                ):
                    chunk_yielded = True
                    yield chunk
                return  # 成功——降级链在这里终止

            except asyncio.TimeoutError as te:
                exc_obj = te
                last_error = f"{provider_id} 超时"
                _log.warning("Provider %s timed out (attempt %d/2)", provider_id, inner_attempt + 1)
            except Exception as e:
                exc_obj = e
                error_str = str(e)
                # 401/403 → 不可降级（Key 本身无效），直接报错
                if "401" in error_str or "403" in error_str:
                    _log.error("Provider %s returned auth error, NOT falling back", provider_id)
                    if task_id:
                        await self._safe_send(task_id, {
                            "type": "error", "task_id": task_id,
                            "message": f"Provider '{provider_id}' 认证失败——API Key 无效。请检查设置。",
                        })
                    return
                last_error = error_str[:AGENT_TOOL_HELPERS_ERROR_PREVIEW_CHARS]
                _log.warning("Provider %s failed (attempt %d/2): %s", provider_id, inner_attempt + 1, last_error)

            # 已 yield chunk 则不重试（避免重复 chunk）
            if chunk_yielded:
                break
            # 还有重试机会 → 退避后重试同一 provider
            if inner_attempt == 0:
                # Task 7.4: Retry-After 优先，否则指数退避 + jitter
                retry_after = getattr(exc_obj, "retry_after", None)
                if retry_after is not None:
                    inner_delay = min(float(retry_after), MAX_BACKOFF)
                else:
                    inner_backoff = min(2.0 * (2 ** inner_attempt), MAX_BACKOFF)
                    inner_delay = inner_backoff + _random.uniform(0, inner_backoff * RETRY_JITTER_FACTOR)
                await asyncio.sleep(inner_delay)

        # 指数退避（切换到下一个 provider 前）
        if idx < len(fallback_chain) - 1:
            degradation_cooldown[provider_id] = _t.time()
            # Task 7.4: 加 jitter + cap 60（原 cap 16）
            switch_backoff = min(backoff, MAX_BACKOFF)
            switch_delay = switch_backoff + _random.uniform(0, switch_backoff * RETRY_JITTER_FACTOR)
            await asyncio.sleep(switch_delay)
            backoff = min(backoff * 2, MAX_BACKOFF)

    # 所有降级选项耗尽
    _log.error("All %d providers exhausted. Last error: %s", len(fallback_chain), last_error)
    if task_id:
        await self._safe_send(task_id, {
            "type": "error", "task_id": task_id,
            "message": f"所有模型 Provider 不可用（已尝试 {len(fallback_chain)} 个）。最后错误: {last_error}",
        })


async def run_stop_hooks(self, task_id: str, session_id: str,
                        agent_id: str = DEFAULT_AGENT_ID,
                        model_cfg: dict | None = None) -> dict:
    """Agent 声明 done 后运行验证钩子。

    对标 Claude Code: exit code ≠ 0 → 回循环。
    只在 Agent 实际修改了代码时才运行——纯问答跳过。

    Phase 48-F: agent_id 参数穿透——修复原硬编码 "hermes" 违反铁律 #1（OC 后端任务也以 hermes 身份调工具）。
    Phase 56-P2: 验证钩子预览预算按当前模型上下文窗口动态计算。
    Phase 56-P4: 验证命令按工作区技术栈动态选择（不再对所有项目跑 tsc/pytest）。
    """
    results: list[dict] = []
    all_passed = True

    # Phase 56-P2: 动态预算（无 model_cfg 时回退到原常量）
    _typecheck_budget = calc_context_budget(
        model_cfg, fraction=0.006, min_chars=500,
        max_chars=AGENT_TOOL_HELPERS_STDOUT_PREVIEW_CHARS,
    ) if model_cfg else AGENT_TOOL_HELPERS_STDOUT_PREVIEW_CHARS
    _test_budget = calc_context_budget(
        model_cfg, fraction=0.004, min_chars=300,
        max_chars=AGENT_TOOL_HELPERS_STDOUT_TYPECHECK_PREVIEW_CHARS,
    ) if model_cfg else AGENT_TOOL_HELPERS_STDOUT_TYPECHECK_PREVIEW_CHARS
    _error_budget = calc_context_budget(
        model_cfg, fraction=0.002, min_chars=150,
        max_chars=AGENT_TOOL_HELPERS_ERROR_PREVIEW_CHARS,
    ) if model_cfg else AGENT_TOOL_HELPERS_ERROR_PREVIEW_CHARS

    # 跳过纯问答——Agent 没写代码就不跑验证
    try:
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM conversations "
            "WHERE session_id=? AND role='assistant' AND "
            "content LIKE '%write_file%'",
            (session_id,),
        )
        row = await cursor.fetchone()
        if not row or row[0] == 0:
            return {"all_passed": True, "results": [], "summary": "skipped (no code written)"}
    except Exception:
        _log.debug("检查 Agent 是否写过代码失败", exc_info=True)

    # Phase 56-P4: 按工作区技术栈动态生成验证命令
    import os as _os
    workspace = ""
    try:
        from security.sandbox import get_workspace
        workspace = str(get_workspace())
    except Exception:
        _log.debug("获取 workspace 失败，使用当前目录", exc_info=True)
        workspace = _os.getcwd()

    commands = build_stop_hook_commands(workspace)
    if not commands:
        return {"all_passed": True, "results": [], "summary": "skipped (no recognizable tech stack)"}

    ts = self._tool_service
    if ts is None:
        from services.tools.tool_service import ToolService
        ts = ToolService()

    for cmd_info in commands:
        hook_name = cmd_info["hook"]
        cmd = cmd_info["command"]
        cwd = cmd_info.get("cwd", workspace)
        try:
            # 在子 shell 中切到目标目录执行命令
            full_cmd = f"cd {cwd} && {cmd}"
            result = await ts.execute(
                "run_command",
                {"command": full_cmd, "timeout": STOP_HOOK_TIMEOUT_SEC},
                agent_id=agent_id, task_id=task_id,
            )
            ok = result.get("success", False) if isinstance(result, dict) else False
            data = result.get("data", {}) if isinstance(result, dict) else {}
            stdout = str(data.get("stdout", "") if isinstance(data, dict) else "")
            stderr = str(data.get("stderr", "") if isinstance(data, dict) else "")
            detail = (stdout + "\n" + stderr).strip()
            # 按钩子类型选择预览预算
            budget = _typecheck_budget if hook_name in ("typecheck", "compile", "lint") else _test_budget
            results.append({"hook": hook_name, "passed": ok, "detail": detail[:budget]})
            if not ok:
                all_passed = False
        except Exception as e:
            results.append({"hook": hook_name, "passed": False, "detail": str(e)[:_error_budget]})
            all_passed = False

    return {
        "all_passed": all_passed,
        "results": results,
        "summary": f"{sum(1 for r in results if r['passed'])}/{len(results)} hooks passed",
    }


async def analyze_tool_failure_and_send(self, task_id: str, tool_name: str,
                                           args: dict, result_str: str,
                                           agent_id: str, model_cfg: dict) -> None:
    """工具失败后 LLM 分析原因 + 建议替代方案（异步 fire-and-forget）。
    完成后通过 WS 推送到前端，Agent 下一轮自然看到建议。
    """
    try:
        adapter = self.adapters.get(agent_id) or self.adapters.get(DEFAULT_AGENT_ID)
        if not adapter:
            return
        if not model_cfg.get("api_key") or not model_cfg.get("base_url") or not model_cfg.get("model"):
            return
        # Phase 43 P0-DYN: 工具失败分析用 protect_critical_info 保护关键信息
        # Phase 56-P2: 失败分析提示预算按模型窗口动态计算
        _fail_budget = calc_context_budget(
            model_cfg, fraction=0.004, min_chars=200,
            max_chars=AGENT_TOOL_HELPERS_PROTECTED_RESULT_PREVIEW_CHARS,
        )
        _arg_budget = calc_context_budget(
            model_cfg, fraction=0.002, min_chars=150,
            max_chars=AGENT_TOOL_HELPERS_ERROR_PREVIEW_CHARS,
        )
        _fail_critical = protect_critical_info(result_str, _fail_budget) or result_str[:_fail_budget]
        prompt = (
            f"Tool '{tool_name}' failed: {_fail_critical}\n"
            f"Args: {protect_critical_info(str(args), _arg_budget) or str(args)[:_arg_budget]}\n"
            "Suggest a concrete alternative approach in one sentence."
        )
        result = await adapter.invoke(
            prompt,
            context={
                "system": "Output one sentence suggestion for fixing a failed tool call.",
                "model_config": model_cfg,
                "max_tokens": MAX_TOKENS_AGENT_TOOL_RESULT,
                "temperature": 0.1,
            },
        )
        if result.get("error"):
            return
        suggestion = (result.get("content", "") or "").strip()
        if suggestion:
            await self._safe_send(task_id, {
                "type": "tool_failure_analysis",
                "task_id": task_id,
                "tool_name": tool_name,
                "suggestion": suggestion,
            })
            # Phase 38: 注入下轮 context — Agent 下次可以看到失败分析
            self._pending_failure_analysis = (
                f"工具 '{tool_name}' 执行失败。建议: {suggestion}"
            )
            # Phase 41: Bug→知识闭环 — 写入 knowledge_nodes
            try:
                await self.db.execute(
                    "INSERT OR REPLACE INTO knowledge_nodes(id, title, content, "
                    "source, score, tags, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (f"bug_{tool_name}_{int(_time.time())}"[:AGENT_TOOL_HELPERS_BUG_ID_MAX_LEN],
                     f"工具失败: {tool_name}",
                     # Phase 43 P0-DYN: Bug 知识节点不截断关键信息
                     f"工具 '{tool_name}' 执行失败。Args: {protect_critical_info(str(args), _arg_budget) or str(args)[:_arg_budget]}\n"
                     f"Error: {protect_critical_info(result_str, _fail_budget) or result_str[:_fail_budget]}\n"
                     f"Suggestion: {suggestion}",
                     "bug_fix_lesson", 0.5, '["lesson_learned","auto","bug"]',
                     int(_time.time()), int(_time.time())),
                )
                await self.db.commit()
            except Exception:
                _log.debug("写入 Bug 经验到知识库失败", exc_info=True)
    except Exception:
        _log.debug("Tool failure analysis send failed", exc_info=True)


async def extract_memory_from_tool_result(self, tool_name: str, args: dict,
                                              result_str: str) -> str | None:
    """从成功的工具调用结果中提取可记忆的关键信息。
    例如 read_file→记住文件路径和主要功能，search_files→记住搜索结果模式。
    """
    try:
        import json
        data = json.loads(result_str) if isinstance(result_str, str) else result_str
        if isinstance(data, dict):
            if tool_name == "read_file":
                path = args.get("path", "")
                content = data.get("data", {}).get("content", "")
                if path and content:
                    return f"文件 {path} 的内容已读取（{len(content)} chars）"
            elif tool_name == "write_file":
                path = args.get("path", "")
                if path:
                    return f"文件 {path} 已被修改"
            elif tool_name == "search_files":
                pattern = args.get("pattern", "")
                matches = data.get("data", {}).get("total_matches", 0)
                if matches > 0:
                    return f"搜索 '{pattern}' 找到 {matches} 处匹配"
    except Exception:
        _log.debug("Memory extraction failed", exc_info=True)
    return None
