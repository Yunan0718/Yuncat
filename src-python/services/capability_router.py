"""services/capability_router.py —— Agent Capability Router (Phase 31)

Yuncat <-> Hermes <-> OpenClaw tripartite coordination:
  - Auto-select best agent by task type
  - Cross-agent tool discovery — merge all agent tools into unified list
  - Commander prompt delegation context — let LLM know who to delegate to
"""
import logging
from dataclasses import dataclass, field

from config import (
    DEFAULT_AGENT_ID, EXTERNAL_AGENT_IDS,
    SUB_AGENT_PROMPT_LOG_PREVIEW_CHARS, CAPABILITY_ROUTER_ARGUMENTS_PREVIEW_CHARS,
    CAPABILITY_STRENGTHS_MAX, CAPABILITY_TOOLS_MAX,
    CAPABILITY_DELEGATE_STRENGTHS_MAX, CAPABILITY_DELEGATE_TOOLS_MAX,
)
from services.infra.tool_policy import ToolPolicy, get_policy
import time

_log = logging.getLogger("yuncat.capability")


@dataclass
class AgentProfile:
    agent_id: str
    name: str
    strengths: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    supports_image: bool = False
    supports_browser: bool = False
    supports_shell: bool = False
    supports_search: bool = False


async def build_profiles(adapters: dict) -> list[AgentProfile]:
    """Build agent capability profiles from adapter registry.
    动态发现每个 Agent 的真实可用工具列表。

    2026-09-10: **外部 agent 一律不进入候选档案**（openclaw/hermes/codex/claude）——
    外部后端仅在用户于下拉框显式选择时调用（EXTERNAL_AGENT_AUTO_ROUTE=False）。
    此前保留 OpenClaw 档案导致：任务含「图片/截图/PDF/视觉」等关键字时能力路由
    自动选中未启动的 openclaw 网关 → 任务静默失败（用户感知为"被委派给别的 agent"）。
    """
    profiles = []

    # Yuncat 原生 profile —— 8/29「强原生」重构后的默认 agent。
    # 2026-08-31 drift 修复: 原实现把这份档案误标为 Hermes（agent_id=DEFAULT_AGENT_ID
    # + name="Hermes"）。DEFAULT_AGENT_ID 从 hermes 漂移为 yuncat 后，档案身份与
    # 工具列表双双错位——shell/code 关键词命中后路由到原生 agent 是对的，
    # 但档案名与工具集都是 hermes 时代的残留。
    nat = adapters.get(DEFAULT_AGENT_ID)
    if nat is not None:
        tools = []
        try:
            from tools.registry import list_tools as _builtin_tools
            yt = _builtin_tools(agent_id=DEFAULT_AGENT_ID)
            if yt:
                tools = get_policy().filter_tools(yt, provider=DEFAULT_AGENT_ID)
        except Exception:
            _log.warning("Yuncat built-in tool registry load failed", exc_info=True)
        profiles.append(AgentProfile(
            agent_id=DEFAULT_AGENT_ID, name="Yuncat",
            strengths=["shell command execution", "code execution (Python/JS/Bash sandbox)",
                       "LLM 直连 API (支持 thinking mode + 工具调用·模型无关)",
                       "流式输出 + 思考链 + 三级上下文压缩", "跨 Agent 委托 (via delegate_to_agent → OpenClaw/Hermes)"],
            keywords=["command", "execute", "run", "terminal", "shell", "code", "program",
                      "write file", "read file", "search files", "git"],
            tools=tools,
            supports_shell=True,
            supports_browser=True,
            supports_search=True,
        ))

    # 2026-09-10: Hermes 恢复为外部网关 agent，但**不加入自动路由候选**——
    # 外部 agent（openclaw/codex/claude/hermes）一律仅在用户显式选择 / @mention 时调用
    # （ws.py 提交路径的 EXTERNAL_AGENT_IDS 门控兜底）。能力路由只会在原生 yuncat
    # 与「确需外部网关能力」的 openclaw 视觉/搜索档之间选择。

    return profiles


async def route_by_capability(prompt: str, profiles: list[AgentProfile],
                        default: str = DEFAULT_AGENT_ID,
                        learn_from: dict | None = None,
                        db=None) -> str:
    """Route prompt to best agent based on keyword matching + learned preferences.

    Priority:
      1. Learned preferences (delegation success history → knowledge_nodes score)
      2. image/vision → OpenClaw
      3. messaging/notify → OpenClaw
      4. terminal/command → Hermes
      5. default → Hermes (more general-purpose)

    learn_from: optional dict from knowledge_nodes query. 为 None 时自动查询 DB。

    Phase 48 T2-1: 改 async 以便 await _query_learned_from_db（原 sync 版在 async
    调用链中 loop.is_running() 永远 True → DB 查询不可达，反馈学习闭环断裂）。
    """
    prompt_lower = prompt.lower()

    # Phase 43/48 T2-1: learn_from 为空时自动从 knowledge_nodes 查询 delegation 历史
    if not learn_from:
        try:
            learn_from = await _query_learned_from_db(prompt_lower, profiles)
        except Exception:
            _log.warning("查询 delegation 学习历史失败", exc_info=True)

    best_learned = _get_learned_preference(prompt_lower, profiles, learn_from)
    if best_learned:
        _log.info("Learned route: '%s' -> %s (from delegation history)",
                  prompt[:SUB_AGENT_PROMPT_LOG_PREVIEW_CHARS], best_learned)
        return best_learned

    for profile in sorted(profiles, key=lambda p: len(p.keywords), reverse=True):
        for kw in profile.keywords:
            if kw.lower() in prompt_lower:
                _log.info("Capability route: '%s' keyword '%s' -> %s",
                          prompt[:SUB_AGENT_PROMPT_LOG_PREVIEW_CHARS], kw, profile.agent_id)
                return profile.agent_id

    return default


async def _query_learned_from_db(prompt_lower: str, profiles: list[AgentProfile]) -> dict | None:
    """Phase 43/48 T2-1: 从 knowledge_nodes 异步查询 delegation 成功历史。

    原 sync 版用 `asyncio.get_event_loop().is_running()` 判断，在 async 调用链中
    永远 True → 直接 return None → DB 查询不可达。改 async 后用 await db.execute，
    真正闭合反馈学习闭环（写入端 feedback_learning.py:79 tags=json.dumps([agent_id])）。

    P1-2 修复: 原代码 SELECT COUNT(*) 只统计委托次数，success_rate 硬编码 0.8，
    导致"用过 3 次就永久偏好"——假学习。改为读取 AVG(score) 反映真实反馈质量。
    score 语义（feedback_learning.py:57）: 接受 +0.10 / 拒绝 -0.15 / 初始 0.3。
    AVG(score) 直接作为 success_rate，与 _get_learned_preference 行 183 的 0.7 阈值可比。

    Task 7.1: 聚合两种 source 的反馈数据到同一个 delegation key：
      - agent_delegation: 工具委托反馈（feedback_learning.record_feedback 写入）
      - mixed_deliberation + title LIKE 'agent_perf:%': 讨论角色表现追踪
        （deliberation_discussion.py 写入，tags=json.dumps([agent_used])，
         feedback_learning.record_deliberation_feedback 调整 score）
    两者都通过 tags LIKE '%agent_id%' 关联到对应 Agent。
    """
    from context import get_db
    db = get_db()
    if db is None:
        return None
    result: dict[str, dict] = {}
    for p in profiles:
        key = f"delegation:{p.agent_id}"
        try:
            cursor = await db.execute(
                "SELECT COUNT(*), AVG(score) FROM knowledge_nodes "
                "WHERE (source='agent_delegation' "
                "       OR (source='mixed_deliberation' AND title LIKE 'agent_perf:%')) "
                "AND tags LIKE ?",
                (f"%{p.agent_id}%",),
            )
            row = await cursor.fetchone()
            if row and row[0] > 0:
                # P1-2: 用真实平均分替代硬编码 0.8。AVG(score) 可能为 NULL（理论不会，但防御）
                avg_score = row[1] if row[1] is not None else 0.3
                result[key] = {"success_rate": avg_score, "count": row[0]}
        except Exception:
            _log.warning("查询知识节点失败", exc_info=True)
    return result if result else None


def _get_learned_preference(prompt_lower: str, profiles: list[AgentProfile],
                             learn_from: dict | None = None) -> str | None:
    """Check if past delegation history suggests a specific agent for this task type.

    Returns agent_id or None if no learned preference.
    """
    if not learn_from:
        return None

    # Check if any profile's keywords have high-success delegation history
    for profile in sorted(profiles, key=lambda p: len(p.keywords), reverse=True):
        for kw in profile.keywords:
            if kw.lower() in prompt_lower:
                # Check learned success rate for this agent+tool combination
                key = f"delegation:{profile.agent_id}"
                perf = learn_from.get(key, {})
                if perf.get("success_rate", 0) > 0.7 and perf.get("count", 0) >= 3:
                    return profile.agent_id
    return None


def get_delegation_context(profiles: list[AgentProfile]) -> str:
    """Generate cross-agent delegation context for Commander prompt.

    Lets the LLM know:
      1. Which agent it is currently talking to (default: Hermes)
      2. What other agents are available and their capabilities
      3. How to delegate — describe the need, Yuncat auto-routes
    """
    if len(profiles) <= 1:
        return ""

    lines = ["## Cross-Agent Collaboration"]
    lines.append("You are operating in a multi-agent environment. Other agents are available:")

    for p in profiles:
        skills = ", ".join(p.strengths[:CAPABILITY_STRENGTHS_MAX])
        lines.append(f"\n### {p.name} (agent_id: `{p.agent_id}`)")
        lines.append(f"- Specializes in: {skills}")
        if p.supports_image:
            lines.append("- Can analyze images/screenshots")
        if p.supports_browser:
            lines.append("- Can control a browser")
        if p.supports_shell:
            lines.append("- Can execute terminal commands")
        if p.tools:
            tool_names = ", ".join(f"`{t.get('name', '?')}`" for t in p.tools[:CAPABILITY_TOOLS_MAX])
            lines.append(f"- Available tools: {tool_names}")

    lines.append("\n### Delegation Rules")
    lines.append("- When you need another Agent's expertise, suggest delegating: say 'I recommend delegating this to [Agent Name]'")
    lines.append("- Users don't need to switch manually — just describe what you need, Yuncat auto-routes")
    lines.append("- All Agent outputs appear in the same conversation thread")

    return "\n".join(lines)


def merge_all_tools(profiles: list[AgentProfile]) -> list[dict]:
    """Merge all agents' tools into a unified list with source agent labels"""
    all_tools = []
    for p in profiles:
        for tool in p.tools:
            all_tools.append({
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "agent_id": p.agent_id,
                "agent_name": p.name,
                "category": tool.get("category", "general"),
            })
    return all_tools


# ============================================================
# Phase 108: 能力硬门（capability hard-gate）
# ============================================================
# 工具语义 → 所需 AgentCapability 字段。仅覆盖适配器真实暴露的能力
# (supports_image / supports_browser)，避免对 shell/search 等 profile 级启发式做
# 误判式硬拒（那是硬编码风险点）。
_CAPABILITY_KEYWORDS: dict[str, list[str]] = {
    "supports_image": [
        "vision", "image", "img", "ocr", "screenshot", "photo", "picture",
        "图片", "图像", "截图", "视觉",
    ],
    "supports_browser": [
        "browser", "web", "navigate", "click", "scroll", "网页", "浏览器", "点击",
    ],
}


def infer_required_capability(tool_name: str) -> str | None:
    """从工具名推断该委托所需的 AgentCapability 字段名。

    Returns:
        能力键 (如 "supports_image") 或 None（无特定能力要求）。
    """
    t = (tool_name or "").lower()
    for cap, kws in _CAPABILITY_KEYWORDS.items():
        if any(k in t for k in kws):
            return cap
    return None


def check_delegation_capability(
    tool_name: str, to_agent_id: str, adapters: dict,
) -> tuple[bool, str]:
    """能力硬门：目标 Agent 若不具备委托所需能力，返回 (False, 含建议 Agent 的错误信息)。

    对标 dsh 的 capability negotiation（非静默降级）——缺能力时显式拒绝并建议改选，
    而不是把任务塞给一个不支持该能力的 Agent 导致静默失败。

    fail-open: 目标适配器不可知（无 capabilities()）时不拦截，避免误杀。
    """
    required = infer_required_capability(tool_name)
    if not required:
        return True, ""
    target = adapters.get(to_agent_id)
    if target is None or not hasattr(target, "capabilities"):
        return True, ""  # 能力未知 → 不拦
    caps = target.capabilities()
    if getattr(caps, required, True):
        return True, ""  # 目标具备所需能力

    # 找一个具备该能力的备选 Agent
    alt = None
    for aid, ad in adapters.items():
        if aid == to_agent_id:
            continue
        if hasattr(ad, "capabilities") and getattr(ad.capabilities(), required, False):
            alt = aid
            break
    msg = (
        f"Agent '{to_agent_id}' 不支持所需能力 ({required})，"
        f"委托已被拒绝（防止静默降级）。"
    )
    if alt:
        msg += f" 建议委托给支持该能力的 Agent: {alt}。"
    else:
        msg += " 当前没有其他 Agent 支持该能力。"
    return False, msg



# ============================================================
# Phase 31: Cross-Agent Tool Delegation (ACP-style)
# ============================================================

async def delegate_tool(
    from_agent_id: str, to_agent_id: str,
    tool_name: str, arguments: dict,
    adapters: dict,
) -> dict:
    """Agent A delegates a tool call to Agent B.

    Example: Hermes needs vision → delegate_tool("hermes", "openclaw", "openclaw_vision", {...})
    Example: OpenClaw needs shell → delegate_tool("openclaw", "hermes", "run_command", {...})

    Phase 51-F: 支持 HE↔OC 双向委托，并更新反馈学习。
    Returns the tool execution result or error.
    """
    if from_agent_id == to_agent_id:
        return {"ok": False, "error": "Cannot delegate to the same agent"}

    adapter = adapters.get(to_agent_id)
    if not adapter:
        return {"ok": False, "error": f"Agent {to_agent_id} not found"}

    if not hasattr(adapter, "execute_tool"):
        return {"ok": False,
                "error": f"Agent {to_agent_id} does not support direct tool execution"}

    _log.info("Cross-agent delegate: %s -> %s.%s(%s)",
              from_agent_id, to_agent_id, tool_name,
              str(arguments)[:CAPABILITY_ROUTER_ARGUMENTS_PREVIEW_CHARS])

    try:
        result = await adapter.execute_tool(tool_name, arguments)
        ok = bool(result.get("ok") or result.get("success"))
    except Exception as e:
        result = {"ok": False, "error": f"Cross-agent delegation failed: {e}"}
        ok = False

    await _record_delegation_feedback(from_agent_id, to_agent_id, tool_name, ok)
    return result


async def _record_delegation_feedback(
    from_agent_id: str, to_agent_id: str, tool_name: str, success: bool,
) -> None:
    """Phase 51-F: 将跨 Agent 委托结果写入反馈学习。"""
    try:
        from context import get_app
        app = get_app()
        fb = getattr(app.state, "feedback_learner", None) if app and hasattr(app, "state") else None
        if fb and hasattr(fb, "record_feedback"):
            await fb.record_feedback(
                "human_accepted" if success else "human_rejected",
                task_id=str(int(time.time())),
                context=f"delegate:{to_agent_id}.{tool_name}",
                agent_id=to_agent_id,
            )
    except Exception:
        _log.warning("记录委托反馈失败", exc_info=True)


def get_delegate_tool_prompt(profiles: list[AgentProfile]) -> str:
    """Generate Commander prompt segment telling the agent how to delegate to other agents.

    The agent gets a "delegate_to" conceptual tool it can suggest using.
    """
    if len(profiles) <= 1:
        return ""

    lines = ["\n## Cross-Agent Delegation (Phase 31)"]
    lines.append("You can delegate tasks to other agents when they have expertise you lack:")

    for p in profiles:
        skills = ", ".join(p.strengths[:CAPABILITY_DELEGATE_STRENGTHS_MAX])
        lines.append(f"- **{p.name}** ({p.agent_id}): {skills}")
        if p.tools:
            tool_names = ", ".join(t.get("name", "?") for t in p.tools[:CAPABILITY_DELEGATE_TOOLS_MAX])
            lines.append(f"  Tools: {tool_names}")

    lines.append("\nTo delegate: tell the user 'I'll delegate this to [Agent Name]' "
                  "and Yuncat will route the request automatically.")
    return "\n".join(lines)


# ============================================================
# Phase 31: Role-to-Agent Mapping (Mixed Agent Deliberation)
# ============================================================

# Which agent best handles each deliberation role type
# 2026-09-10: Hermes 已下线，原 "hermes" 角色全部回归原生 Yuncat（DEFAULT_AGENT_ID）。
# 仅视觉/无障碍审计类角色保留 OpenClaw（外部视觉后端）。
ROLE_AGENT_MAP: dict[str, str] = {
    # Vision/image analysis → OpenClaw（外部视觉后端）
    "ux": "openclaw",           # UX needs visual analysis
    "frontend": "openclaw",     # Frontend benefits from vision
    "a11y-architect": "openclaw",  # Accessibility audit needs vision
    "accessibility-auditor": "openclaw",
    # Shell/browser/build automation → 原生 Yuncat（Hermes 已下线，能力已回归）
    "devops": DEFAULT_AGENT_ID,
    "sre": DEFAULT_AGENT_ID,
    "tester": DEFAULT_AGENT_ID,
    "build-error-resolver": DEFAULT_AGENT_ID,
    "dependency-scanner": DEFAULT_AGENT_ID,
    "performance-optimizer": DEFAULT_AGENT_ID,
    "silent-failure-hunter": DEFAULT_AGENT_ID,
    # Everyone else → default (原生 Yuncat)
}


def route_role_to_agent(role_id: str, adapters: dict, default: str = DEFAULT_AGENT_ID) -> str:
    """Map a deliberation role to the best agent.

    - Vision-heavy roles → OpenClaw (if available)
    - Shell/code-heavy roles → 原生 Yuncat（Hermes 已下线，能力已回归）
    - Others → default
    """
    mapped = ROLE_AGENT_MAP.get(role_id, default)
    if mapped in adapters:
        # 2026-09-10: 外部 agent（openclaw 视觉档）仅在**真实就绪**（网关可达）时才用——
        # 否则角色会被派给一个没启动的网关，讨论直接失败。未就绪一律回落原生 Yuncat。
        if mapped in EXTERNAL_AGENT_IDS:
            _ready = getattr(adapters[mapped], "_ready", None)
            if _ready is None or not _ready.is_set():
                return default if default in adapters else mapped
        return mapped
    # If mapped agent not available, fall back to any available
    if default in adapters:
        return default
    return next(iter(adapters)) if adapters else default

