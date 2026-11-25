"""system_tool_config.py —— 配置/委托工具"""
import asyncio
import logging
import time as _time

from ..registry import tool
from config import (
    SYSTEM_TOOL_CONFIG_ERROR_PREVIEW_CHARS, ERROR_PREVIEW_CHARS,
    DELEGATE_CONCURRENCY_MAX,
    DELEGATE_RETRY_MAX, DELEGATE_RETRY_BACKOFF_SEC, DELEGATE_FALLBACK_MAP,
    MAX_SUBAGENT_DEPTH, DEFAULT_AGENT_ID, MAX_DELEGATION_DEPTH,
)

_log = logging.getLogger("yuncat.tools")

# Task 3.8: delegate_to_agent 全局并发信号量——防主 Agent 一轮发 50 个委托耗尽 OC 后端连接。
# Python 3.10+ 的 asyncio.Semaphore 可模块级创建（不绑定 event loop）。
# 模块级单例: 跨任务共享，限制全局并发委托数。测试用 _reset_delegate_sem() 重置状态。
_delegate_sems: dict[int, asyncio.Semaphore] = {}


def _delegate_semaphore() -> asyncio.Semaphore:
    """按当前能力档取委托并发信号量：默认 10 / MAX 20（services/capability_limits.py）"""
    from services.capability_limits import current_limits

    limit = current_limits().delegate_concurrency
    sem = _delegate_sems.get(limit)
    if sem is None:
        sem = asyncio.Semaphore(limit)
        _delegate_sems[limit] = sem
    return sem


def _reset_delegate_sem() -> None:
    """测试用：清空按档缓存的信号量"""
    _delegate_sems.clear()


# ---- Phase 34: Yuncat 自身配置工具 ----

@tool(
    name="yuncat_config", agents=["hermes", "openclaw"],
    description=(
        "Read or update Yuncat settings. The Agent can configure itself. "
        "Use action='get' to read a setting, action='set' to update it. "
        "Supported keys: auto_approve, watch_files_enabled, design_style, color_scheme, "
        "security_profile (strict/standard/developer)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["get", "set", "list"], "description": "get a single key, set a key=value, or list all"},
            "key": {"type": "string", "description": "Setting key (e.g. auto_approve, design_style)"},
            "value": {"type": "string", "description": "New value (only for action=set)"},
        },
        "required": ["action"],
    },
    is_concurrency_safe=True,
    groups=["config"],
)
async def yuncat_config(action: str, key: str = "", value: str = "") -> dict:
    """读/写 Yuncat 设置——Agent 可以自己配置运行时参数。"""
    from tools.result import ToolResult
    try:
        from context import get_app
        app = get_app()
        settings_svc = getattr(app.state, "settings_service", None) if app and hasattr(app, "state") else None
        if not settings_svc:
            return ToolResult(success=False, error="SettingsService not available",
                              meta={"tool_name": "yuncat_config"}).to_dict()

        if action == "list":
            all_s = await settings_svc.get_all()
            # 过滤掉敏感的 API key
            safe = {k: ("***" if k.startswith("api_key:") else v) for k, v in all_s.items()}
            return ToolResult(success=True, data=safe,
                              observation=f"当前有 {len(safe)} 项设置",
                              meta={"tool_name": "yuncat_config"}).to_dict()

        if action == "get":
            if not key:
                return ToolResult(success=False, error="key is required for get",
                                  meta={"tool_name": "yuncat_config"}).to_dict()
            all_s = await settings_svc.get_all()
            val = all_s.get(key, "")
            if key.startswith("api_key:"):
                val = "***"
            return ToolResult(success=True, data={key: val},
                              observation=f"{key} = {val}",
                              meta={"tool_name": "yuncat_config"}).to_dict()

        if action == "set":
            if not key or not value:
                return ToolResult(success=False, error="key and value are required for set",
                                  meta={"tool_name": "yuncat_config"}).to_dict()
            await settings_svc.save(key, value)
            return ToolResult(success=True, data={key: value},
                              observation=f"已设置 {key} = {value}（重启/下轮对话生效）",
                              meta={"tool_name": "yuncat_config"}).to_dict()
    except Exception as e:
        return ToolResult(success=False, error=str(e)[:SYSTEM_TOOL_CONFIG_ERROR_PREVIEW_CHARS],
                          meta={"tool_name": "yuncat_config"}).to_dict()


# ---- Phase 31: Cross-Agent Delegation Tool ----


@tool(
    name="delegate_to_agent", agents=["hermes", "openclaw"],
    description=(
        "Delegate a task to another agent that has specialized capabilities. "
        "Use this when another agent has tools or expertise you lack. "
        "Example: delegate image analysis to OpenClaw (openclaw_vision), "
        "or delegate shell command execution to Hermes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "target_agent": {
                "type": "string", "enum": ["openclaw", "hermes"],
                "description": "Which agent to delegate to",
            },
            "tool_name": {
                "type": "string",
                "description": "The tool to invoke on the target agent (e.g. openclaw_vision, hermes_web_search)",
            },
            "arguments": {
                "type": "object",
                "description": "Arguments to pass to the tool (as key-value pairs)",
            },
        },
        "required": ["target_agent", "tool_name"],
    },
    is_concurrency_safe=True,
    groups=["config"],
    tool_timeout=600,  # P2-2.4: 跨后端委托超时（对端可能跑完整 ReAct 循环）
)
async def delegate_to_agent(target_agent: str, tool_name: str,
                            arguments: dict = None) -> dict:
    """Agent A 委托任务给 Agent B——真正跨 Agent 工具调用（Phase 38: 统一走 capability_router）"""
    from tools.result import ToolResult
    from adapters.registry import get_global_adapters

    # Phase 38: 防递归 — 不能委托 delegate_to_agent 自身
    if tool_name == "delegate_to_agent":
        return ToolResult(
            success=False,
            error="不能委托 delegate_to_agent 自身。请直接调用目标工具。",
            meta={"tool_name": "delegate_to_agent"},
        ).to_dict()

    # Phase 108: 委派递归深度硬门——防 delegate_to_agent 链失控（A→B→C→…）。
    from context import (
        get_delegation_depth, set_delegation_depth, reset_delegation_depth,
    )
    _depth = get_delegation_depth()
    if MAX_DELEGATION_DEPTH > 0 and _depth >= MAX_DELEGATION_DEPTH:
        return ToolResult(
            success=False,
            error=(
                f"Delegation depth limit reached ({_depth}/{MAX_DELEGATION_DEPTH}). "
                f"Cannot delegate further."
            ),
            meta={"tool_name": "delegate_to_agent", "depth": _depth},
        ).to_dict()
    _depth_token = set_delegation_depth(_depth + 1)
    try:
        # Phase 38: 参数大小限制 (防 OOM)
        if arguments:
            import json as _json
            args_str = _json.dumps(arguments, ensure_ascii=False)
            if len(args_str) > 100_000:
                return ToolResult(
                    success=False,
                    error="arguments 过大 (max 100KB)",
                    meta={"tool_name": "delegate_to_agent"},
                ).to_dict()

        adapters = get_global_adapters()
        if not adapters:
            return ToolResult(
                success=False, error="No agents available",
                meta={"tool_name": "delegate_to_agent"},
            ).to_dict()

        target = adapters.get(target_agent)
        if not target:
            return ToolResult(
                success=False,
                error=f"Agent '{target_agent}' not found. Available: {', '.join(adapters.keys())}",
                meta={"tool_name": "delegate_to_agent"},
            ).to_dict()

        if not hasattr(target, "execute_tool"):
            return ToolResult(
                success=False,
                error=f"Agent '{target_agent}' does not support direct tool execution",
                meta={"tool_name": "delegate_to_agent"},
            ).to_dict()

        # Phase 108: 能力硬门——目标 Agent 不具备所需能力时显式拒绝（防静默降级）。
        from services.capability_router import check_delegation_capability
        _cap_ok, _cap_err = check_delegation_capability(tool_name, target_agent, adapters)
        if not _cap_ok:
            return ToolResult(
                success=False, error=_cap_err,
                meta={"tool_name": "delegate_to_agent", "target_agent": target_agent},
            ).to_dict()

        args = arguments or {}
        # Task 3.8: 全局并发限流——防主 Agent 一轮发 50 个委托耗尽 OC 后端连接。
        # 信号量在通过基本校验后获取（快速失败的请求不占用槽位）。async with 确保异常时释放。
        async with _delegate_semaphore():
            from context import get_current_agent_id
            from_agent_id = get_current_agent_id() or "unknown"

            # Task 6.7 (F-053): 重试 + fallback——对标 Temporal 自动重试。
            # 流程: target(含重试) → 失败 → fallback Agent → 仍失败 → 降级返回错误。
            attempts: list[tuple[str, bool, str]] = []  # [(agent, success, error)]
            success = False
            result: dict = {"ok": False, "error": "未尝试"}
            fallback_used: str | None = None

            # 1. 尝试 target_agent（含重试）
            for attempt in range(1 + DELEGATE_RETRY_MAX):
                success, result = await _call_delegate(
                    target_agent, tool_name, args, adapters, from_agent_id)
                attempts.append((target_agent, success, result.get("error", "")))
                if success:
                    break
                if attempt < DELEGATE_RETRY_MAX:
                    await asyncio.sleep(DELEGATE_RETRY_BACKOFF_SEC)

            # 2. 仍失败 → 尝试备选 Agent（对标 Temporal fallback）
            if not success:
                fallback_agent = DELEGATE_FALLBACK_MAP.get(target_agent)
                if fallback_agent:
                    # 用 .get() 接口查询——不要求 adapters 实现 __contains__
                    fb_adapter = adapters.get(fallback_agent) if hasattr(adapters, "get") else None
                    if fb_adapter and hasattr(fb_adapter, "execute_tool"):
                        fb_success, fb_result = await _call_delegate(
                            fallback_agent, tool_name, args, adapters, from_agent_id)
                        attempts.append((fallback_agent, fb_success, fb_result.get("error", "")))
                        if fb_success:
                            success = True
                            result = fb_result
                            fallback_used = fallback_agent

            # 3. 发布事件（含重试/fallback 元信息）
            try:
                from services.event_bus import get_event_bus, Events
                eb = get_event_bus()
                if hasattr(eb, "publish"):
                    payload = {
                        "target_agent": target_agent, "tool_name": tool_name,
                        "success": success,
                        "args_summary": _safe_truncate(str(args), 200),
                        "timestamp": int(_time.time()),
                        "fallback": fallback_used,
                        "attempts": len(attempts),
                    }
                    await eb.publish("delegation_completed", payload)
                    await eb.publish(Events.CROSS_BACKEND_DELEGATION, payload)
            except Exception:
                _log.debug("发布委托完成事件失败", exc_info=True)

            # 4. 返回结果（含重试/fallback 元信息）
            final_agent = fallback_used or target_agent
            obs = f"委托给 {target_agent}: {tool_name}({_safe_truncate(str(args), 50)}) → "
            if success:
                obs += "成功"
                if fallback_used:
                    obs += f" (fallback→{fallback_used})"
                elif len(attempts) > 1:
                    obs += f" (重试 {len(attempts)-1} 次后成功)"
            else:
                obs += f"失败: {result.get('error', '')}"
                if len(attempts) > 1:
                    obs += f" (已尝试 {len(attempts)} 次)"

            return ToolResult(
                success=success,
                data={"delegated_to": final_agent, "tool": tool_name,
                      "result": result.get("result", result)},
                observation=obs,
                meta={"tool_name": "delegate_to_agent", "target_agent": target_agent,
                      "success": success, "fallback": fallback_used,
                      "retried": len(attempts) > 1},
            ).to_dict()
    finally:
        reset_delegation_depth(_depth_token)


# ---- Task 7.8: spawn_subagent 工具（对标 Claude Code Task tool）----

# CodeWhale 借鉴: 子 Agent 角色分类——不同角色注入不同的系统提示词前缀。
# 对标 CodeWhale subagent/mod.rs 的 7 种角色（explore/plan/review/implementer/
# verifier/general/custom），此处简化为 6 种（custom 合并到 general）。
# 每个角色引导子 Agent 聚焦特定任务类型，提升子任务执行质量。
_SUBAGENT_ROLE_PROMPTS: dict[str, str] = {
    "explore": (
        "[Role: Explorer] You are a code exploration specialist. Your goal is to "
        "thoroughly investigate the codebase and report findings. "
        "Focus on: reading files, searching patterns, tracing call chains, and "
        "mapping dependencies. Do NOT modify any files. "
        "Report: file paths, key functions, data flow, and architectural observations."
    ),
    "plan": (
        "[Role: Planner] You are an architectural planner. Analyze the task and "
        "produce a step-by-step implementation plan. "
        "Focus on: identifying affected files, ordering changes, potential risks, "
        "and testing strategy. Do NOT modify any files. "
        "Report: numbered steps with file paths, estimated complexity, and risk assessment."
    ),
    "review": (
        "[Role: Reviewer] You are a code reviewer. Analyze the provided code or "
        "changes for quality, correctness, and best practices. "
        "Focus on: security issues, performance problems, error handling gaps, "
        "and adherence to project conventions. "
        "Report: categorized findings (Critical/High/Medium/Low) with file:line references."
    ),
    "implement": (
        "[Role: Implementer] You are an implementation specialist. Write clean, "
        "minimal code to accomplish the task. "
        "Focus on: surgical changes, no unnecessary refactoring, following existing "
        "patterns and conventions. Test your changes if possible. "
        "Report: files modified, summary of changes, and verification results."
    ),
    "verify": (
        "[Role: Verifier] You are a verification specialist. Your goal is to "
        "confirm that the implementation meets the requirements. "
        "Focus on: running tests, checking edge cases, verifying type safety, "
        "and ensuring no regressions. Do NOT modify implementation files. "
        "Report: pass/fail status, test results, and any issues found."
    ),
    "general": "",  # 默认角色——不注入额外提示词，保持原有行为
}


@tool(
    name="spawn_subagent", agents=["hermes", "openclaw"],
    description=(
        "Spawn a sub-agent with its own independent context window to handle a "
        "self-contained subtask. The sub-agent runs autonomously and returns a "
        "summary of its work. Use this for complex tasks that benefit from "
        "isolation (e.g. deep code analysis, multi-step research, independent "
        "verification). Recursion depth is limited to prevent runaway spawning. "
        "Example: spawn_subagent(prompt='Analyze the authentication flow in "
        "src/auth/ and list all security concerns', role='explore')"
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The task description for the sub-agent. Should be self-contained—"
                               "the sub-agent does NOT inherit the current conversation history.",
            },
            "agent_id": {
                "type": "string",
                "description": "Which agent to use for the sub-task (default: hermes).",
                "default": "hermes",
            },
            "context_summary": {
                "type": "string",
                "description": "Optional context summary to pass to the sub-agent. "
                               "Use this to provide essential background without the full history.",
            },
            "role": {
                "type": "string",
                "enum": ["explore", "plan", "review", "implement", "verify", "general"],
                "description": "CodeWhale 借鉴: 子 Agent 角色分类。不同角色注入不同的系统提示词，"
                               "引导子 Agent 聚焦特定任务类型。"
                               "explore=代码探索/研究, plan=架构规划, review=代码审查, "
                               "implement=实现编码, verify=验证测试, general=通用(默认).",
                "default": "general",
            },
            "model_tier": {
                "type": "string",
                "enum": ["lite", "normal", "max", "vision"],
                "description": "Optional: force a specific model tier for this sub-agent. "
                               "Overrides automatic complexity routing. Use 'max' for architecture/security, "
                               "'lite' for read-only/documentation, 'vision' for image analysis.",
            },
            "max_mode": {
                "type": "boolean",
                "description": "Optional: force MAX tier for this sub-agent (overrides model_tier).",
                "default": False,
            },
            "capability_level": {
                "type": "integer",
                "description": "Optional: capability level 0=轻量/1=标准/2=强力 for this sub-agent. "
                               "Defaults to 1. Only affects model tier selection when no explicit model_tier is set.",
                "default": 1,
            },
            "long_task_step_role": {
                "type": "string",
                "description": "Optional: hint the step role (e.g. scaffold, key_implementation, security) "
                               "for tier inference when model_tier is not explicitly set.",
            },
        },
        "required": ["prompt"],
    },
    confirmable=True,  # 启动子 Agent 是有副作用的操作（消耗资源/时间），需用户确认
    is_concurrency_safe=True,  # spawn_subagent 内部已有 Semaphore 限流
    groups=["config"],
    tool_timeout=300,  # 5min 超时（SUB_AGENT_POLL_MAX_SECONDS=120，留余量）
)
async def spawn_subagent_tool(
    prompt: str, agent_id: str = "",
    context_summary: str = "",
    role: str = "general",
    model_tier: str = "",
    max_mode: bool = False,
    long_task_step_role: str = "",
    capability_level: int = 1,
) -> dict:
    """Task 7.8: 暴露 spawn_subagent 为 LLM 可调用的工具。

    对标 Claude Code Task tool——LLM 可主动 spawn 子 Agent 处理独立子任务。
    子 Agent 有独立上下文窗口（不污染父对话），结果以 summary 形式返回。

    递归深度限制（MAX_SUBAGENT_DEPTH=3）：
      - depth 0 = 主 Agent（可 spawn）
      - depth 1 = 子 Agent（可 spawn）
      - depth 2 = 孙 Agent（可 spawn）
      - depth 3 = 曾孙 Agent（不可再 spawn，拒绝）
    深度通过 ContextVar 传播，asyncio.create_task 自动继承。
    """
    from tools.result import ToolResult
    from context import (
        get_agent_service, get_subagent_depth,
        set_subagent_depth, reset_subagent_depth,
        set_isolation_mode, reset_isolation_mode,
    )

    # 1. 递归深度检查——防止失控 spawn
    current_depth = get_subagent_depth()
    if current_depth >= MAX_SUBAGENT_DEPTH:
        return ToolResult(
            success=False,
            error=(
                f"Sub-agent recursion depth limit reached "
                f"({current_depth}/{MAX_SUBAGENT_DEPTH}). "
                f"Cannot spawn further sub-agents."
            ),
            meta={"tool_name": "spawn_subagent", "depth": current_depth},
        ).to_dict()

    # 2. 获取 AgentService
    agent_svc = get_agent_service()
    if agent_svc is None:
        return ToolResult(
            success=False,
            error="AgentService not available. Cannot spawn sub-agent.",
            meta={"tool_name": "spawn_subagent"},
        ).to_dict()

    # 3. 设置 depth+1——子 Agent 后台任务通过 asyncio.create_task 继承此值
    target_agent = agent_id or DEFAULT_AGENT_ID
    # CodeWhale 借鉴: 角色分类——不同角色注入不同的系统提示词前缀
    _role_prefix = _SUBAGENT_ROLE_PROMPTS.get(role, "")
    effective_prompt = f"{_role_prefix}\n\n{prompt}" if _role_prefix else prompt
    # Phase 107: 继承父任务 UI 语言（若上下文未设置则默认 zh）
    from context import get_current_language
    _language = get_current_language()
    token = set_subagent_depth(current_depth + 1)
    # Phase 108: 子 Agent 隔离——置隔离标记，子 Agent 后台任务经 create_task 继承。
    # 隔离子 Agent 的非只读工具被 classify_permission 钉死为 ALWAYS_DENY
    # （永不需用户确认、永不执行破坏性工具），对标 dsh child isolation。
    iso_token = set_isolation_mode(True)
    try:
        result = await agent_svc.spawn_subagent(
            agent_id=target_agent,
            prompt=effective_prompt,
            context_summary=context_summary,
            language=_language,
            model_tier=model_tier or "",
            max_mode=max_mode,
            long_task_step_role=long_task_step_role or "",
            capability_level=capability_level,
        )
        # 统一为 ToolResult 格式（AgentService 返回 {"ok", "task_id", "output"}）
        if result.get("ok"):
            return ToolResult(
                success=True,
                data={
                    "task_id": result.get("task_id", ""),
                    "output": result.get("output", ""),
                },
                observation=f"Sub-agent completed (agent={target_agent}).",
                meta={"tool_name": "spawn_subagent", "depth": current_depth + 1},
            ).to_dict()
        return ToolResult(
            success=False,
            error=result.get("error", "Unknown sub-agent failure"),
            meta={"tool_name": "spawn_subagent"},
        ).to_dict()
    except Exception as e:
        _log.error("spawn_subagent tool failed: %s", e, exc_info=True)
        return ToolResult(
            success=False,
            error=f"Sub-agent spawn failed: {e}",
            meta={"tool_name": "spawn_subagent"},
        ).to_dict()
    finally:
        reset_subagent_depth(token)
        reset_isolation_mode(iso_token)


# ---- Phase 9-X: spawn_subagent_team 工具（并行多内部子 Agent fan-out）----

@tool(
    name="spawn_subagent_team", agents=["hermes", "openclaw"],
    description=(
        "Spawn a TEAM of multiple INTERNAL sub-agents (NOT external agents) in "
        "parallel to handle several independent subtasks at once, then collect "
        "all their results. Each sub-agent has its own isolated context window. "
        "Use this to fan out a complex request into parallel work (e.g. analyze "
        "3 modules independently, or research several topics at once). "
        "External agents (Hermes/OpenClaw) are NOT allowed in a team — they must "
        "be called explicitly by the user. "
        "Set aggregate=true to merge all outputs into one combined summary. "
        "Example: spawn_subagent_team(specs=[{agent_id:'hermes', prompt:'...', "
        "role:'explore'}, {agent_id:'hermes', prompt:'...', role:'review'}], "
        "aggregate=true)"
    ),
    parameters={
        "type": "object",
        "properties": {
            "specs": {
                "type": "array",
                "description": (
                    "List of sub-agent specifications. Each element: "
                    "{agent_id?, prompt, context_summary?, role?, model_tier?, "
                    "max_mode?, long_task_step_role?}. The prompt must be "
                    "self-contained — the sub-agent does NOT inherit the current "
                    "conversation history."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": "Target internal agent (default: hermes).",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Self-contained task description for this sub-agent.",
                        },
                        "context_summary": {
                            "type": "string",
                            "description": "Optional background summary.",
                        },
                        "role": {
                            "type": "string",
                            "enum": ["explore", "plan", "review", "implement", "verify", "general"],
                            "description": "CodeWhale 借鉴: 子 Agent 角色分类，注入不同的系统提示词。",
                            "default": "general",
                        },
                        "model_tier": {
                            "type": "string",
                            "enum": ["lite", "normal", "max", "vision"],
                            "description": "Optional forced model tier.",
                        },
                        "max_mode": {
                            "type": "boolean",
                            "description": "Optional: force MAX tier.",
                            "default": False,
                        },
                        "long_task_step_role": {
                            "type": "string",
                            "description": "Optional tier-routing hint.",
                        },
                    },
                    "required": ["prompt"],
                },
            },
            "aggregate": {
                "type": "boolean",
                "description": "If true, merge all sub-agent outputs into one combined summary.",
                "default": True,
            },
        },
        "required": ["specs"],
    },
    confirmable=True,  # 启动多个子 Agent 是有副作用的操作，需用户确认
    is_concurrency_safe=True,  # spawn_subagent 内部已有 Semaphore 限流
    groups=["config"],
    tool_timeout=600,  # 多子 Agent 并行——留足超时余量
)
async def spawn_subagent_team_tool(
    specs: list[dict],
    aggregate: bool = True,
) -> dict:
    """Phase 9-X: 暴露并行多内部子 Agent 为 LLM 可调工具。

    对标 Claude Code 的 Agent 团队 / 多并行子任务。允许单个用户请求 fan-out
    到多个内部子 Agent 并行执行并聚合。
    """
    from tools.result import ToolResult
    from context import (
        get_agent_service, get_subagent_depth,
        set_subagent_depth, reset_subagent_depth,
        set_isolation_mode, reset_isolation_mode,
    )

    # 1. 递归深度检查——防止失控 spawn（team 本身算一层）
    current_depth = get_subagent_depth()
    if current_depth >= MAX_SUBAGENT_DEPTH:
        return ToolResult(
            success=False,
            error=(
                f"Sub-agent recursion depth limit reached "
                f"({current_depth}/{MAX_SUBAGENT_DEPTH}). "
                f"Cannot spawn further sub-agents."
            ),
            meta={"tool_name": "spawn_subagent_team", "depth": current_depth},
        ).to_dict()

    agent_svc = get_agent_service()
    if agent_svc is None:
        return ToolResult(
            success=False,
            error="AgentService not available. Cannot spawn sub-agent team.",
            meta={"tool_name": "spawn_subagent_team"},
        ).to_dict()

    # 2. 解析每个 spec 的 role 前缀（与 spawn_subagent 工具一致）
    resolved: list[dict] = []
    for spec in specs or []:
        if not isinstance(spec, dict):
            continue
        role = spec.get("role", "general")
        _role_prefix = _SUBAGENT_ROLE_PROMPTS.get(role, "")
        prompt = spec.get("prompt", "")
        effective_prompt = f"{_role_prefix}\n\n{prompt}" if _role_prefix else prompt
        resolved.append({
            "agent_id": spec.get("agent_id", ""),
            "prompt": effective_prompt,
            "context_summary": spec.get("context_summary", ""),
            "model_tier": spec.get("model_tier", ""),
            "max_mode": spec.get("max_mode", False),
            "long_task_step_role": spec.get("long_task_step_role", ""),
        })

    from context import get_current_language
    _language = get_current_language()
    token = set_subagent_depth(current_depth + 1)
    iso_token = set_isolation_mode(True)
    try:
        result = await agent_svc.spawn_subagent_team(
            specs=resolved,
            aggregate=aggregate,
            language=_language,
        )
        if result.get("ok"):
            data: dict = {
                "agent_count": result.get("agent_count", 0),
                "results": result.get("results", {}),
            }
            if aggregate:
                data["aggregated_output"] = result.get("aggregated_output", "")
            if result.get("skipped_external"):
                data["skipped_external"] = result.get("skipped_external")
            return ToolResult(
                success=True,
                data=data,
                observation=f"Team of {result.get('agent_count', 0)} sub-agent(s) completed.",
                meta={"tool_name": "spawn_subagent_team"},
            ).to_dict()
        return ToolResult(
            success=False,
            error=result.get("error", "Unknown sub-agent team failure"),
            meta={"tool_name": "spawn_subagent_team"},
        ).to_dict()
    except Exception as e:
        _log.error("spawn_subagent_team tool failed: %s", e, exc_info=True)
        return ToolResult(
            success=False,
            error=f"Sub-agent team spawn failed: {e}",
            meta={"tool_name": "spawn_subagent_team"},
        ).to_dict()
    finally:
        reset_subagent_depth(token)
        reset_isolation_mode(iso_token)


async def _call_delegate(target_agent: str, tool_name: str, args: dict,
                         adapters: dict, from_agent_id: str) -> tuple[bool, dict]:
    """Task 6.7 (F-053): 单次委托调用——返回 (success, result_dict)。不发布事件。

    封装 capability_router.delegate_tool 调用 + 异常处理，
    供 delegate_to_agent 的重试/fallback 循环复用。

    对标 Temporal Activity：单次执行单元，重试策略由调用方控制。
    """
    try:
        from services.capability_router import delegate_tool as _dt
        result = await _dt(
            from_agent_id=from_agent_id, to_agent_id=target_agent,
            tool_name=tool_name, arguments=args, adapters=adapters,
        )
        # 对齐 capability_router.delegate_tool 内部的成功判定（ok 或 success 任一为真）
        success = bool(result.get("ok") or result.get("success"))
        return success, result
    except Exception as e:
        return False, {"ok": False, "error": str(e)}


def _safe_truncate(s: str, max_len: int) -> str:
    return s[:max_len] + "..." if len(s) > max_len else s
