"""services/agent_context.py —— Agent 上下文构建模块

从 agent_service.py 拆分: _build_agent_context / _get_growth_stats /
_reattach_context_after_compact。采用委托模式: 模块级函数接收 self 作为首参。
"""
import logging

from services.memory.history_trim import drop_current_prompt

from config import (
    adaptive_truncate, DEFAULT_AGENT_ID, OPENCLAW_AGENT_ID, HERMES_AGENT_ID, calc_file_reattach_budget,
    resolve_context_window,
    DESIGN_STYLE_DEFAULT, COLOR_SCHEME_DEFAULT,
    AGENT_CONTEXT_FTS_MEMORIES_LIMIT,
    AGENT_CONTEXT_MEMORY_TREE_LIMIT,
    AGENT_CONTEXT_MEMORY_TOTAL_BUDGET_FRACTION,
    AGENT_CONTEXT_MEMORY_TOTAL_BUDGET_MIN_CHARS,
    AGENT_CONTEXT_MEMORY_TOTAL_BUDGET_MAX_CHARS,
    AGENT_CONTEXT_PERSONALIZATION_LIMIT,
    AGENT_CONTEXT_TABS_LIMIT,
    AGENT_CONTEXT_DIAGNOSTICS_LIMIT,
    AGENT_CONTEXT_REATTACH_FILES_LIMIT,
    AGENT_CONTEXT_REATTACH_FILE_MAX_BYTES,
    AGENT_CONTEXT_SKILL_PREVIEW_CHARS,
    AGENT_CONTEXT_ACTIVE_SKILLS_LIMIT,
    AGENT_CONTEXT_SKILL_TOTAL_BUDGET_CHARS,
    AGENT_CONTEXT_RULES_BUDGET_CHARS,
    AGENT_CONTEXT_RECENT_FILES_LIST_LIMIT,
    SUMMARY_EMPTY_SENTINEL,
    calc_context_budget,
    calc_list_limit,
    USER_TOOL_ROUNDS_MIN,
    USER_TOOL_ROUNDS_MAX,
)

_log = logging.getLogger("yuncat.agent")


# ==== M1: 项目规则文件自动加载（AGENTS.md / CLAUDE.md） ====
# 对标 Cursor .cursorrules / Claude Code CLAUDE.md 自动读取。
# workspace 根目录的 AGENTS.md / CLAUDE.md 自动注入 agent 上下文。

_PROJECT_RULE_FILES = ("AGENTS.md", "CLAUDE.md")  # AGENTS.md 优先（主流标准）


def _read_rule_file(path) -> str:
    """读取单个规则文件，失败/空返回 ''。"""
    try:
        from pathlib import Path as _Path
        p = _Path(path)
        if not p.is_file():
            return ""
        text = p.read_text(encoding="utf-8", errors="replace")
        return text.strip()
    except Exception:
        _log.debug("读取项目规则文件失败: %s", path, exc_info=True)
        return ""


def load_project_rules(workspace=None, budget: int = 8000) -> str:
    """M1: 自动加载 workspace 根目录的 AGENTS.md / CLAUDE.md。

    对标 Cursor .cursorrules / Claude Code CLAUDE.md 自动读取。
    AGENTS.md 优先（OpenAI/主流 Agent 标准），CLAUDE.md 次之。
    合并后按 budget 截断。无规则文件或读取失败返回 ""。

    Args:
        workspace: workspace 根目录 Path；None 时用 get_workspace() 默认值
        budget: 合并后最大字符数（截断时附提示）
    """
    if workspace is None:
        try:
            from security.sandbox import get_workspace
            workspace = get_workspace()
        except Exception:
            _log.debug("取不到工作区路径，项目规则上下文为空", exc_info=True)
            return ""
    parts: list[str] = []
    try:
        ws_dir = workspace
        for name in _PROJECT_RULE_FILES:
            text = _read_rule_file(ws_dir / name)
            if text:
                parts.append(f"# === 项目规则 ({name}) ===\n{text}")
    except Exception:
        _log.warning("读取项目规则失败——agent 将看不到项目约定（AGENTS.md 等）", exc_info=True)
        return ""
    if not parts:
        return ""
    combined = "\n\n".join(parts)
    if len(combined) > budget:
        notice = f"\n\n[...项目规则已截断: {len(combined)} → {budget} 字符]"
        content_budget = max(0, budget - len(notice))
        combined = combined[:content_budget] + notice
    return combined


# ==== M3: 跨 session 记忆继承（对标 Claude Code --continue） ====
# 新会话首条消息时注入同 workspace 上次会话的结构化摘要。

# P2-2.2: 注入文本最大字符数——动态派生（原硬编码 _M3_INHERITED_SUMMARY_MAX_CHARS = 1500）
# 在 _format_inherited_summary 内用 calc_context_budget(get_default_model_config(), 0.01, 800, 2000) 派生
_M3_INHERITED_SUMMARY_MAX_CHARS_FALLBACK = 1500  # get_default_model_config 未就绪时的 fallback

_M3_SUMMARY_LABELS = {
    "session_intent": "目标",
    "key_decisions": "关键决策",
    "files_modified": "涉及文件",
    "pending_tasks": "待办",
    "errors_and_fixes": "错误与修复",
    "next_steps": "下一步",
}


def _format_inherited_summary(summary: dict) -> str:
    """M3: 格式化上次会话结构化摘要为注入文本。

    Args:
        summary: 6-section 结构化摘要 dict（session_intent / key_decisions / ...）

    Returns:
        格式化的 Markdown 文本，空摘要返回 ""。
    """
    if not summary or not isinstance(summary, dict):
        return ""
    lines = []
    for key, label in _M3_SUMMARY_LABELS.items():
        val = summary.get(key)
        # 哨兵值必须一并跳过：各节缺失时写入端填的是 SUMMARY_EMPTY_SENTINEL（"无"）
        # 而非空串，它是非空真值，只判 `not val` 会让「无」原样注入，
        # 每个新会话多出 7 行「- 目标: 无」，既浪费上下文，
        # 又让模型把「待办: 无」误当成既成事实而非「未记录」。
        # 与 memory_compact._format_compact_boundary 的判空口径保持一致。
        if not val or val == SUMMARY_EMPTY_SENTINEL:
            continue
        if isinstance(val, list):
            val = "; ".join(str(v) for v in val if v)
        val = str(val).strip()
        if val:
            lines.append(f"- {label}: {val}")
    if not lines:
        return ""
    text = "## 上次会话摘要（跨 session 继承，可加速上下文恢复）\n" + "\n".join(lines)
    # P2-2.2: 动态派生截断上限（原硬编码 _M3_INHERITED_SUMMARY_MAX_CHARS = 1500）
    try:
        from config import get_default_model_config
        _max_chars = calc_context_budget(
            get_default_model_config(), 0.01, min_chars=800, max_chars=2000,
        )
    except Exception:
        _max_chars = _M3_INHERITED_SUMMARY_MAX_CHARS_FALLBACK
    # 截断时预留后缀长度，保证最终 len(text) <= _max_chars（Task 7.3 回归修复）
    _trunc_suffix = "\n[...摘要已截断]"
    if len(text) > _max_chars:
        text = text[:_max_chars - len(_trunc_suffix)] + _trunc_suffix
    return text


async def _inject_inherited_summary(
    self, session_ctx: list, session_id: str, sys_prompt: str,
) -> str:
    """M3: 新会话注入同 workspace 上次会话摘要。

    判定新会话：session_ctx 中无 assistant 消息（用户刚发首条消息）。
    已有 assistant 回复 → 不注入（避免重复注入）。

    Args:
        self: AgentService 实例（需要 self.db）
        session_ctx: 当前会话的消息列表
        session_id: 当前会话 ID
        sys_prompt: 当前系统提示词

    Returns:
        可能注入了上次会话摘要的 sys_prompt
    """
    try:
        has_assistant = any(m.get("role") == "assistant" for m in session_ctx)
        if has_assistant:
            return sys_prompt
        from security.sandbox import get_workspace
        from repositories.session_summary_repo import get_last_session_summary
        ws = str(get_workspace())
        prev = await get_last_session_summary(self.db, ws, exclude_session_id=session_id)
        if not prev:
            return sys_prompt
        summary_text = _format_inherited_summary(prev["summary"])
        if summary_text:
            return summary_text + "\n\n" + sys_prompt
    except Exception:
        _log.debug("M3 跨 session 记忆继承注入失败", exc_info=True)
    return sys_prompt


async def _resolve_max_tool_rounds(settings_service) -> int | None:
    """Batch 3.1: 从用户设置读取 max_tool_rounds，非法/缺失返回 None。

    返回 None 表示让工具循环使用默认值（DEFAULT_MAX_TOOL_ROUNDS_HARD_LIMIT）。
    返回值会被 clamp 到 [USER_TOOL_ROUNDS_MIN, USER_TOOL_ROUNDS_MAX]。

    2026-09-10 修复: get_raw 是 async（SettingsService.get_raw 返回协程），原 sync 调用
    未 await → 拿到的是协程对象，int(协程) 抛 TypeError 被 except 吞掉 → 用户配置的
    max_tool_rounds 永远不生效、静默回退默认值。改为 async + await。
    """
    if settings_service is None:
        return None
    try:
        raw = await settings_service.get_raw("max_tool_rounds")
        if raw is None or raw == "":
            return None
        value = int(raw)
    except Exception:
        _log.debug("max_tool_rounds 设置解析失败，使用默认值", exc_info=True)
        return None
    if value < USER_TOOL_ROUNDS_MIN:
        return USER_TOOL_ROUNDS_MIN
    if value > USER_TOOL_ROUNDS_MAX:
        return USER_TOOL_ROUNDS_MAX
    return value


def _build_tools_schema(agent_id: str) -> list:
    """按注册表生成 OpenAI function calling 的 tools 列表（注册表是唯一来源）。

    2026-09-22 实测：这个列表以前从不注入 context，模型收不到任何工具 schema，
    于是把工具调用当普通文本吐出来（形如 <｜|DSML|｜invoke name="list_dir">），
    工具一次都执行不了——所有工具调用能力因此形同虚设。

    注册表读不到时返回空列表并记 warning：便于区分"读表失败"与"该 agent 无工具"。
    """
    try:
        from prompts.tool_descriptions import list_tools_for_agent

        return [{"type": "function", "function": f}
                for f in list_tools_for_agent(agent_id)]
    except Exception:
        _log.warning("生成工具 schema 失败（本轮将不带工具下发）", exc_info=True)
        return []


def _apply_capability_hint(sys_prompt: str, extra_context: dict | None) -> str:
    """MAX 档的增强行为（先出计划、做完自检）随能力档注入；非 MAX 原样返回"""
    # 本函数是模块级的，看不到 build_agent_context 里的局部导入；
    from prompts.commander import render_capability_hint
    hint = render_capability_hint((extra_context or {}).get("capability_flags"))
    return sys_prompt + "\n" + hint if hint else sys_prompt


async def build_agent_context(
    self, task_id: str, session_id: str, prompt: str, mode: str,
    rules_context: str, extra_context: dict | None, model_cfg: dict,
    agent_id: str = DEFAULT_AGENT_ID,
    language: str = "zh",
) -> tuple[dict, str]:
    """组装 Agent 上下文——Commander prompt + 记忆 + Delegation + Skill + 诊断 + 历史。

    线性流程：记忆 → 系统提示词 → 会话块 → 成长/诊断块 → 历史/context dict。
    Returns: (context_dict, ctx_summary_str)
    """
    # Phase 56-P2: 所有注入预算按当前模型窗口动态计算
    _fts_limit = calc_list_limit(model_cfg, AGENT_CONTEXT_FTS_MEMORIES_LIMIT, fraction_per_100k=1, max_limit=10)
    _memtree_limit = calc_list_limit(model_cfg, AGENT_CONTEXT_MEMORY_TREE_LIMIT, fraction_per_100k=1, max_limit=10)
    _personal_limit = calc_list_limit(model_cfg, AGENT_CONTEXT_PERSONALIZATION_LIMIT, fraction_per_100k=1, max_limit=6)
    _tabs_limit = calc_list_limit(model_cfg, AGENT_CONTEXT_TABS_LIMIT, fraction_per_100k=2, max_limit=12)
    _diags_limit = calc_list_limit(model_cfg, AGENT_CONTEXT_DIAGNOSTICS_LIMIT, fraction_per_100k=2, max_limit=12)
    _skill_preview = calc_context_budget(model_cfg, fraction=0.004, min_chars=300, max_chars=1500)
    _skill_total_budget = calc_context_budget(model_cfg, fraction=0.04, min_chars=3000, max_chars=12000)
    _rules_budget = calc_context_budget(model_cfg, fraction=0.015, min_chars=1500, max_chars=6000)
    _recent_files_limit = calc_list_limit(model_cfg, AGENT_CONTEXT_RECENT_FILES_LIST_LIMIT, fraction_per_100k=3, max_limit=25)

    # ==== 1. 组装记忆上下文 ====
    from prompts.memory_context import render_memory_context

    session_ctx: list = []
    long_term_memories: list = []
    knowledge_results: list[dict] = []
    workspace_project_id: str | None = None
    if self._memory_service:
        session_ctx = await self._memory_service.get_session_context(session_id)
        try:
            from security.sandbox import get_workspace
            import os
            workspace = get_workspace()
            workspace_project_id = os.path.normcase(str(workspace))
            filters = {
                "source": "memory",
                "project_id": workspace_project_id,
            }
            long_term_memories = await self._memory_service.multi_level_search(
                prompt,
                limit=_fts_limit,
                filters=filters,
                query_expansion=True,
                diversify=True,
            )
        except Exception:
            _log.debug('Memory hybrid search failed', exc_info=True)
    if self._memory_tree:
        try:
            raw = await self._memory_tree.retrieve_context(
                prompt, limit=_memtree_limit,
                project_id=workspace_project_id,
                return_raw=True,
            )
            knowledge_results = raw if isinstance(raw, list) else []
        except Exception:
            _log.debug('Memory tree retrieval failed', exc_info=True)

    # Batch 2.7: 三条检索路径统一预算——memories(向量+FTS) + knowledge tree 合并去重/截断
    _memory_total_budget = calc_context_budget(
        model_cfg,
        fraction=AGENT_CONTEXT_MEMORY_TOTAL_BUDGET_FRACTION,
        min_chars=AGENT_CONTEXT_MEMORY_TOTAL_BUDGET_MIN_CHARS,
        max_chars=AGENT_CONTEXT_MEMORY_TOTAL_BUDGET_MAX_CHARS,
    )
    memory_ctx = render_memory_context(
        session_ctx, long_term_memories, "",
        knowledge_results=knowledge_results,
        total_budget=_memory_total_budget,
    )
    if self._feedback_learner:
        try:
            personalization = await self._feedback_learner.get_personalization_context(limit=_personal_limit)
            if personalization:
                memory_ctx = memory_ctx + "\n\n" + personalization
        except Exception:
            _log.debug("Unhandled exception", exc_info=True)

    # B: 用户明确表达的偏好注入（源隔离 source="user_preference"）
    # 复用 get_app().state.db 取连接；失败静默降级，不影响主流程。
    try:
        from context import get_app
        _app = get_app()
        _db = getattr(_app.state, "db", None) if _app and hasattr(_app, "state") else None
        if _db is not None:
            from services.user_preferences import build_preference_snippet
            _prefs = await build_preference_snippet(_db, project_id=workspace_project_id, limit=_personal_limit)
            if _prefs:
                memory_ctx = memory_ctx + "\n\n" + _prefs
    except Exception:
        _log.debug("Unhandled exception", exc_info=True)

    # ==== 2. 组装系统提示词 ====
    from prompts.commander import (
        render_commander_prompt, ASK_MODE_GUARD, PLAN_MODE_GUARD,
    )
    from tools.builtin.system_tools import SAFE_COMMAND_WHITELIST
    from services.capability_router import build_profiles, get_delegation_context, get_delegate_tool_prompt

    # 动态 Delegation 上下文
    profiles = await build_profiles(self.adapters)
    delegation_text = get_delegation_context(profiles)
    delegate_prompt = get_delegate_tool_prompt(profiles)

    # 设计偏好（Phase 48-F: 默认值从 config.DESIGN_STYLE_DEFAULT / COLOR_SCHEME_DEFAULT 取）
    ds = DESIGN_STYLE_DEFAULT
    cs = COLOR_SCHEME_DEFAULT
    if self._settings_service:
        try:
            s = await self._settings_service.get_all()
            ds = s.get("design_style", ds)
            cs = s.get("color_scheme", cs)
        except Exception:
            _log.debug("Unhandled exception", exc_info=True)

    # Phase 88: PromptAgent.render() 生成角色/行为/工作流 prompt 段
    # 有推荐时 selected_behaviors 传 None 避免与 render_commander_prompt 双重注入 behavior
    prompt_agent_section = ""
    recommendation = (extra_context or {}).get("prompt_recommendation") if extra_context else None
    if recommendation and getattr(self, "_prompt_agent", None):
        try:
            prompt_agent_section = self._prompt_agent.render(
                role_ids=recommendation.get("role_ids", []),
                behavior_ids=recommendation.get("behavior_ids", []),
                workflow_template=recommendation.get("workflow_template"),
            )
        except Exception:
            _log.debug("PromptAgent render failed", exc_info=True)

    selected_behaviors = None if prompt_agent_section else (
        (extra_context or {}).get("selected_behaviors") if extra_context else None
    )
    # M1: 自动加载 workspace 根目录 AGENTS.md / CLAUDE.md，拼到 rules_context 前
    # 对标 Cursor .cursorrules / Claude Code CLAUDE.md 自动读取
    try:
        _project_rules = load_project_rules(budget=_rules_budget)
        if _project_rules:
            rules_context = _project_rules + ("\n\n" + rules_context if rules_context else "")
    except Exception:
        _log.debug("M1 项目规则自动加载失败", exc_info=True)
    sys_prompt = render_commander_prompt(
        agent_id=agent_id, whitelist=SAFE_COMMAND_WHITELIST,
        rules_context=rules_context, design_style=ds, color_scheme=cs,
        selected_behaviors=selected_behaviors, model_config=model_cfg,
        language=language,
    )
    sys_prompt = _apply_capability_hint(sys_prompt, extra_context)
    if agent_id != OPENCLAW_AGENT_ID:
        if delegation_text:
            sys_prompt = sys_prompt + "\n\n" + delegation_text
        if delegate_prompt:
            sys_prompt = sys_prompt + "\n\n" + delegate_prompt
        # Phase 43 W.1: Lazy Skill Loading
        try:
            from services.evolution.skill_registry import get_skill_registry
            sr = get_skill_registry()
            meta = sr.get_active_metadata(task_type="agent", mode=mode)
            if meta:
                sys_prompt = sys_prompt + "\n\n" + meta
        except Exception:
            _log.debug("Unhandled exception", exc_info=True)
    # Phase 47 P5.1: 模式守卫
    if mode == "ask":
        sys_prompt = sys_prompt + "\n\n" + ASK_MODE_GUARD
    elif mode == "plan":
        sys_prompt = sys_prompt + "\n\n" + PLAN_MODE_GUARD

    # ==== 3. 追加会话块 ====
    sys_prompt += (
        f"\n\n## 当前会话\n"
        f"- 会话ID: {session_id}，已进行 {len(session_ctx)} 轮\n"
    )
    if long_term_memories:
        sys_prompt += f"- 本次对话已积累 {len(long_term_memories)} 条长期记忆\n"
    if self._memory_tree:
        try:
            stats = await self._get_growth_stats()
            if stats.get("total_nodes", 0) > 0:
                sys_prompt += f"- 平台共有 {stats['total_nodes']} 个知识节点可供检索\n"
        except Exception:
            _log.debug("获取成长统计失败", exc_info=True)
    recent_files = sum(
        str(r.get("content", "")).count("read_file") + str(r.get("content", "")).count("write_file")
        for r in knowledge_results
    )
    if recent_files > 0:
        sys_prompt += f"- 本次对话中操作了 {recent_files} 个文件\n"
    # Phase P.0b: 检测工作区已有 plan.md → 提醒Agent重读
    try:
        import os as _os
        from security.sandbox import get_workspace
        ws = str(get_workspace()) if get_workspace() else ""
        if ws:
            plan_path = _os.path.join(ws, "shared", "plan.md")
            if _os.path.exists(plan_path):
                sys_prompt += (f"\n⚠ **工作区已有 plan.md**——先 read_file('plan.md') 了解当前进度和上次阻塞项。"
                          f"如果工作区还有 findings.md，也一并读取。")
            findings_path = _os.path.join(ws, "shared", "findings.md")
            if _os.path.exists(findings_path):
                sys_prompt += f"\n📋 工作区已有 findings.md——read_file 查看已有研究成果。"
    except Exception:
        _log.debug("检测工作区 plan.md 失败", exc_info=True)
    # Phase 43 H3.1: 注入当前打开的编辑器 tabs
    if extra_context and extra_context.get("open_tabs"):
        tabs = extra_context["open_tabs"]
        if isinstance(tabs, list) and tabs:
            tabs_text = " · ".join(
                f"{t.get('path','')}" + (f":{t.get('line', '')}L" if t.get("line") else "")
                for t in tabs[:_tabs_limit]
            )
            sys_prompt += f"- 用户当前打开的文件: {tabs_text}\n"

    # Task 4.1: 注入项目符号索引摘要——code_indexer 每 30min 后台刷新，Agent 知道"哪个类在哪个文件"。
    # 原 load_summary() 是死代码（build() 持久化但从不读取注入），30min 索引 CPU 消耗无效。
    # 对标 Aider --map / Cursor @workspace：符号索引注入 prompt 提升代码导航准确度。
    # Task 4.7: load_summary(model_cfg) 按当前模型上下文窗口动态派生注入数量。
    try:
        from services.code_indexer import CodeIndexer
        _code_summary = CodeIndexer.load_summary(model_cfg=model_cfg)
        if _code_summary:
            sys_prompt += "\n\n" + _code_summary
    except Exception:
        _log.debug("code_index 摘要注入失败", exc_info=True)

    # ==== 4. 追加成长 + 诊断块 ====
    # Phase 43 F4.1+F4.2: PLATFORM_GROWTH + OC 自感知
    try:
        stats = await self._get_growth_stats()
        if stats["total_nodes"] > 0 or stats["bug_lessons"] > 0:
            growth = (
                f"\n\n## 平台的成长\n"
                f"Yuncat 在后台持续运行着——这些是你在长期对话中积累的知识:\n"
                f"- MemoryTree: {stats['total_nodes']} 个知识节点\n"
                f"- Bug 经验库: {stats['bug_lessons']} 条教训\n"
                f"- Dream 固化: {stats['dreams']} 条事实\n"
                f"- SelfEvolution: {stats['evolutions']} 个 Workflow 模板\n"
            )
            if agent_id == OPENCLAW_AGENT_ID:
                growth += ('你是 OpenClaw，你的兄弟后端 Hermes Agent 也在 Yuncat 中运行。\n'
                           'Hermes 擅长深度代码生成和多步推理——需要时用 delegate_to_agent("hermes", ...) 委托给他。\n')
            elif agent_id == HERMES_AGENT_ID:
                growth += ('你是 Hermes Agent，你的兄弟后端 OpenClaw 也在 Yuncat 中运行。\n'
                           'OpenClaw 擅长视觉分析、网页搜索和 30+ 消息渠道——需要时用 delegate_to_agent("openclaw", ...) 委托给他。\n')
            sys_prompt += growth
    except Exception:
        _log.debug("PLATFORM_GROWTH injection failed", exc_info=True)
    # 诊断注入
    from hooks.registry import get_pending_diagnostics
    pending_diag = get_pending_diagnostics()
    if task_id and task_id in pending_diag:
        diags = pending_diag.pop(task_id)
        if diags:
            # CodeWhale 借鉴: 按文件分组 + severity 排序（error 优先）+ 公平分配
            from collections import OrderedDict
            from config import LINT_MAX_DIAGS_PER_FILE
            by_file: dict[str, list] = OrderedDict()
            for d in diags:
                by_file.setdefault(d["file"], []).append(d)
            diag_lines = ["## 最新诊断（自动，来自你上次的编辑）"]
            # 公平分配: 每文件预算 = max(1, 全局上限 / 文件数)
            # 确保多文件编辑时每个文件都有诊断代表，而非第一个文件占满全部配额
            num_files = len(by_file)
            per_file_budget = min(
                LINT_MAX_DIAGS_PER_FILE,
                max(1, _diags_limit // num_files) if num_files > 0 else _diags_limit,
            )
            shown = 0
            for file_path, file_diags in by_file.items():
                if shown >= _diags_limit:
                    break
                # 每文件按 severity 排序
                sev_order = {"error": 0, "warning": 1, "info": 2, "hint": 3}
                file_diags.sort(key=lambda d: sev_order.get(d.get("severity", "error"), 0))
                file_shown = 0
                for d in file_diags:
                    if shown >= _diags_limit or file_shown >= per_file_budget:
                        if file_shown >= per_file_budget and len(file_diags) > per_file_budget:
                            diag_lines.append(
                                f"- ... ({file_path} 还有 {len(file_diags) - file_shown} 条诊断已截断)"
                            )
                        elif shown >= _diags_limit:
                            diag_lines.append(f"- ... (更多诊断已截断，全局上限 {_diags_limit})")
                        break
                    sev_tag = d.get("severity", "error")
                    diag_lines.append(f"- {file_path}:{d['line']} [{sev_tag}] {d['code']} {d['message']}")
                    shown += 1
                    file_shown += 1
            diag_lines.append("请在本次回复中修复上述问题。")
            sys_prompt += "\n\n" + "\n".join(diag_lines)

    # ==== 5. 组装会话历史 + context dict + WS 通知 ====
    ctx_summary = f"会话 {len(session_ctx)}轮 · 长期记忆 {len(long_term_memories)}条"
    if knowledge_results:
        ctx_summary += " · 知识树"
    # 模型配置注入 + 历史限制计算
    if self._memory_service and hasattr(self._memory_service, "set_model_config_for_sizing"):
        self._memory_service.set_model_config_for_sizing(model_cfg)
    # 会话历史（先获取——Task 7.3: 用于计算真实历史均值替代固定 fallback）
    session_history: list = []
    if self._memory_service:
        try:
            session_history = drop_current_prompt(await self._memory_service.get_session_context(session_id), prompt)
        except Exception:
            _log.debug('Session history retrieval failed', exc_info=True)
    from config import calc_history_limit, estimate_avg_turn_tokens
    # Task 7.3: 用真实历史均值替代固定 _AVG_TURN_FALLBACK=1200
    _avg_turn = estimate_avg_turn_tokens(session_history)
    _history_max_turns = calc_history_limit(model_cfg, avg_turn_tokens=_avg_turn)
    # 组装 context dict
    # Task 3.2: 存 mode 到 context——供 adapter 和 execute_tool_loop 程序级拦截使用。
    # 对标 Claude Code disallowed-tools: mode 由 sys_prompt 软约束升级为程序级硬约束。
    # adapter 读取 context["mode"] 后在 Ask/Plan 模式清空 payload["tools"]（工具定义层），
    # ToolService.execute 也读取 mode 拒绝执行任何工具（执行层双保险）。
    context = {
        "system": sys_prompt,
        "memory": memory_ctx,
        "model_config": model_cfg,
        "history": session_history,
        "history_limit": _history_max_turns,
        "mode": mode, "session_id": session_id,  # session_id: 外部网关的会话连续性
    }
    if extra_context and extra_context.get("attachments"):
        context["attachments"] = extra_context["attachments"]
    context["tools"] = _build_tools_schema(agent_id)  # 工具 schema 必须随 context 下发，见该函数说明
    # Phase 108+: 能力策略开关集合随 extra_context 透传到工具循环（ToolService 据此在分发前
    # fail-closed 拦截被禁用的功能）；max_mode / capability_level 一并透传供 prompt 与路由复用。
    if extra_context:
        if extra_context.get("capability_flags") is not None:
            context["capability_flags"] = extra_context["capability_flags"]
        if extra_context.get("capability_level") is not None:
            context["capability_level"] = extra_context["capability_level"]
        if extra_context.get("max_mode") is not None:
            context["max_mode"] = extra_context["max_mode"]
        # Phase X (2026-09-10): 思考强度——透传至 adapter.invoke/stream，
        # 由具体模型决定是否生效（如 DeepSeek reasoning_effort / thinking 开关）。
        if extra_context.get("reasoning_effort") is not None:
            context["reasoning_effort"] = extra_context["reasoning_effort"]
    # Batch 3.1: 注入用户配置的最大工具轮次上限
    _user_max_rounds = await _resolve_max_tool_rounds(self._settings_service)
    if _user_max_rounds is not None:
        context["max_rounds"] = _user_max_rounds
    # WS 通知前端上下文已注入
    await self._safe_send(task_id, {
        "type": "context_injected", "task_id": task_id,
        "summary": ctx_summary,
        "context_window": model_cfg.get("context", "?"),
        "history_limit": _history_max_turns,
        "max_rounds": context.get("max_rounds"),
    })
    return context, ctx_summary


async def get_growth_stats(self) -> dict:
    """Phase 43 F4.3: 查询平台成长统计数据——注入到 Agent system prompt。

    返回 MemoryTree 节点数·Bug 经验·Dream 固化·Workflow 生成 4 项指标。
    Agent 知道平台在后台持续学习中——增强"长期数字搭档"的认知。
    """
    stats = {"total_nodes": 0, "bug_lessons": 0, "dreams": 0, "evolutions": 0}
    try:
        cursor = await self.db.execute("SELECT COUNT(*) FROM knowledge_nodes")
        row = await cursor.fetchone()
        if row:
            stats["total_nodes"] = row[0]
    except Exception:
        _log.debug("统计知识节点数失败", exc_info=True)
    try:
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM knowledge_nodes WHERE tags LIKE '%bug%'"
        )
        row = await cursor.fetchone()
        if row:
            stats["bug_lessons"] = row[0]
    except Exception:
        _log.debug("统计 Bug 经验数失败", exc_info=True)
    try:
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM knowledge_nodes WHERE source='dream'"
        )
        row = await cursor.fetchone()
        if row:
            stats["dreams"] = row[0]
    except Exception:
        _log.debug("统计 Dream 固化数失败", exc_info=True)
    try:
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM workflows WHERE is_template=1"
        )
        row = await cursor.fetchone()
        if row:
            stats["evolutions"] = row[0]
    except Exception:
        _log.debug("统计 Workflow 模板数失败", exc_info=True)
    return stats


async def reattach_context_after_compact(
    self, session_id: str, context: dict, model_cfg: dict,
) -> None:
    """Claude Code 风格：压缩后将核心上下文重新注入。

    1. 重读最近修改的文件实际内容
    2. 重新注入 Skill 定义
    3. 重新注入 project rules
    4. 最近操作文件路径列表

    Phase 56-P2: 所有预算按当前模型窗口动态计算。
    """
    try:
        # 动态预算
        _reattach_files_limit = calc_list_limit(model_cfg, AGENT_CONTEXT_REATTACH_FILES_LIMIT, fraction_per_100k=2, max_limit=10)
        _reattach_file_max_bytes = calc_context_budget(model_cfg, fraction=0.04, min_chars=30_000, max_chars=120_000)
        _skill_preview = calc_context_budget(model_cfg, fraction=0.004, min_chars=300, max_chars=1500)
        _active_skills_limit = calc_list_limit(model_cfg, AGENT_CONTEXT_ACTIVE_SKILLS_LIMIT, fraction_per_100k=2, max_limit=8)
        _skill_total_budget = calc_context_budget(model_cfg, fraction=0.04, min_chars=3000, max_chars=12000)
        _rules_budget = calc_context_budget(model_cfg, fraction=0.015, min_chars=1500, max_chars=6000)
        _recent_files_limit = calc_list_limit(model_cfg, AGENT_CONTEXT_RECENT_FILES_LIST_LIMIT, fraction_per_100k=3, max_limit=25)

        # 1) Claude Code 风格: 重读最近操作文件的实际内容
        recent_files = self.compactor.extract_recent_files(context["history"])
        if recent_files:
            from security.sandbox import get_workspace
            import os as _os
            ws = str(get_workspace())
            reattached = []
            for fname in recent_files[:_reattach_files_limit]:
                try:
                    search_path = _os.path.join(ws, fname.lstrip("/\\"))
                    if not _os.path.exists(search_path):
                        for root, _, files in _os.walk(ws):
                            if fname in files:
                                search_path = _os.path.join(root, fname)
                                break
                    if _os.path.exists(search_path) and _os.path.getsize(search_path) < _reattach_file_max_bytes:
                        _raw = open(search_path, encoding="utf-8", errors="replace").read()
                        _window = resolve_context_window(model_cfg)
                        _budget = calc_file_reattach_budget(model_cfg)
                        content = adaptive_truncate(_raw, _window, _budget, 0.5)
                        if content.strip():
                            reattached.append(f"### {fname}\n```\n{content}\n```")
                except Exception:
                    _log.warning("Reattach file failed: %s", fname, exc_info=True)
            if reattached:
                context["history"].append({
                    "role": "assistant",
                    "content": "[上下文恢复] 压缩后恢复：最近操作文件的当前内容\n\n" + "\n\n".join(reattached),
                })

        # 2) Skill 定义重新注入
        from services.evolution.skill_registry import get_skill_registry
        sr = get_skill_registry()
        skill_prompts = sr.get_active_prompts(task_type="agent", mode="agent")
        if skill_prompts:
            skill_text = "\n\n---\n## 已激活的 Skill（压缩后恢复）\n" + "\n\n".join(
                sp.text[:_skill_preview]
                for sp in sorted(skill_prompts, key=lambda x: -x.priority)[:_active_skills_limit]
            )
            context["history"].append({
                "role": "system",
                "content": skill_text[:_skill_total_budget],
            })

        # 3) Project rules 从 disk 重读
        if self._settings_service:
            try:
                s = await self._settings_service.get_all()
                rules_text = s.get("project_rules", "")
                if rules_text:
                    context["history"].append({
                        "role": "system",
                        "content": f"## Project Rules（压缩后恢复）\n{rules_text[:_rules_budget]}",
                    })
            except Exception:
                _log.debug("Unhandled exception", exc_info=True)

        # 4) 路径列表（供 Agent 自行 read）
        if recent_files:
            file_list = "\n".join(f"- `{f}`" for f in recent_files[:_recent_files_limit])
            context["history"].append({
                "role": "system",
                "content": f"## 最近操作的文件（压缩后恢复）\n{file_list}",
            })

        _log.info("Post-compact reattachment: %d files+skills+rules for session %s",
                  len(recent_files), session_id[:8])

    except Exception:
        _log.exception("Post-compact reattachment failed")
