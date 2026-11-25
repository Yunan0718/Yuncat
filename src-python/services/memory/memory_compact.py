"""services/memory_compact.py —— 上下文压缩四阶段 pipeline（从 memory_service.py 拆分）

包含:
  - 触发判定: compact_now / maybe_compact_context
  - 分级分类: _is_tool_result / _is_user_instruction / _calc_tool_result_ratio / _split_messages
  - 结构化摘要: _build_structured_summary / _grade_messages / _format_graded_messages / _format_recent_context
  - 质量门控: _quality_gate / _extract_probes
  - 持久化: _save_compact_marker / _persist_compaction_snapshot / _apply_compacted_context
  - 熔断器: _record_compact_failure / get_circuit_breaker_status
  - 模块级常量与辅助: STRUCTURED_COMPACT_PROMPT / QUALITY_PROBE_PROMPT
                    _format_existing_summary / _parse_structured_summary / _merge_section
                    _fallback_structured_summary / _format_compact_boundary
                    _summary_size / _is_probe_passed

v55: 融合 Factory AI / ContextPilot / Claude Code
"""
import asyncio
import logging
import time as _time

from config import (
    MAX_TOKENS_MEMORY_COMPACT, MAX_CONTEXT_TOKENS_SAFETY_CAP,
    DEFAULT_AGENT_ID,
    SUMMARY_EMPTY_SENTINEL,
    COMPACT_MARKER_MESSAGE_PREVIEW_CHARS,
    COMPACT_MARKER_TOTAL_CHARS,
    COMPACT_RECENT_CONTEXT_MAX_MESSAGES,
    COMPACT_RECENT_CONTEXT_PREVIEW_CHARS,
    COMPACT_DUMP_CONTENT_CHARS,
    COMPACT_FALLBACK_INTENT_CHARS,
    COMPACT_FALLBACK_FILES_CHARS,
    COMPACT_PROBE_CONTENT_PREVIEW_CHARS,
    COMPACT_PROBE_MAX_COUNT,
    COMPACT_SECTION_MAX_CHARS,
)
from services.memory.compaction_txn import capture_snapshot
from services.memory.compaction_prune_stage import (
    collapse_older as _collapse_older,
    prune_for_pressure as _prune_for_pressure,
    tool_ratio_gate as _tool_ratio_gate,
)

_log = logging.getLogger("yuncat.memory")


# ============================================================
# 阶段 0: 触发判定（自动 + 手动双入口）
# ============================================================

async def compact_now(self, session_id: str, model_config: dict | None = None) -> dict | None:
    """手动触发上下文压缩——跳过阈值门控但保留质量验证（lenient 模式）。
    返回压缩后摘要或 None（无内容可压缩/质量不过关）。
    """
    conv = self._sessions.get(session_id, [])
    if not conv and self.db is not None:
        conv = await self.get_session_context(session_id)
    if len(conv) < 4:
        return None
    recent, older = self._split_messages(conv, model_config)
    if not older:
        return None
    snapshot = capture_snapshot(conv, len(older))
    now = _time.time()
    try:
        structured = await self._build_structured_summary(
            session_id, older, recent, 0.5,
            model_config=model_config,
        )
    except Exception:
        _log.exception("Manual compaction failed")
        return None
    if not structured:
        return None
    # 质量门控（手动触发 → lenient 模式）
    passed = await self._quality_gate(structured, older, conv, lenient=True)
    if not passed:
        _log.warning("Manual compaction for %s: quality gate failed, keeping full context", session_id[:8])
        return None
    # Phase OH.3: Condensation Marker — 保留压缩前原文
    await self._save_compact_marker(session_id, older)
    await self._persist_compaction_snapshot(session_id, older)
    if not await self._apply_compacted_context(session_id, structured, recent, snapshot):
        return None
    self._last_compact_time[session_id] = now
    self._structured_summaries[session_id] = structured
    _log.info("Manual compaction: %s — %d messages → %d chars summary",
              session_id, len(older), _summary_size(structured))
    return structured


async def _save_compact_marker(self, session_id: str, older: list[dict]) -> None:
    """Phase OH.3: 压缩前保留原始段到 task_logs + 写 marker。

    对标 OpenHands Condensation: 标记为"可展开"——不删除原始数据。
    """
    try:
        from services.event_log import EventLog
        raw_text = "\n".join(
            f"{m.get('role','?')}: {str(m.get('content',''))[:COMPACT_MARKER_MESSAGE_PREVIEW_CHARS]}"
            for m in older[-COMPACT_RECENT_CONTEXT_MAX_MESSAGES:]
        )
        # task_logs 的列是 (task_id, chunk_index, content) 且 task_id 外键指向 tasks(id)，
        # 装不下会话级留档——之前那版 INSERT 会**静默失败**，压缩 marker 实际从没落过盘。
        await self.db.execute(
            "INSERT INTO compaction_artifacts(session_id, kind, ref_id, content, created_at) "
            "VALUES(?,?,?,?,?)",
            (session_id, "compact_snapshot", "", raw_text[:COMPACT_MARKER_TOTAL_CHARS], int(_time.time())),
        )
        await self.db.commit()
    except Exception:
        _log.debug("Compact marker save failed", exc_info=True)


async def maybe_compact_context(
    self, session_id: str, current_tokens: int, max_tokens: int | None = None,
    system_prompt_tokens: int = 0,
) -> dict | None:
    """四阶段上下文压缩。返回压缩后的结构化摘要 dict，或 None（未触发/失败）。

    门控顺序: 阈值 → 冷却期 → 消息数 → tool 结果占比 → 执行压缩 → 质量验证

    M4: system_prompt_tokens — system prompt 已占用的 token，从 recent budget 中扣除。
    """
    if not max_tokens:
        from config import resolve_context_window
        max_tokens = resolve_context_window({})  # 无模型配置时 fallback 128K
    max_tokens = min(max_tokens, MAX_CONTEXT_TOKENS_SAFETY_CAP)  # 安全硬上限

    # Phase 44 C.3: Circuit Breaker — 熔断的 session 跳过压缩
    # Phase 48 C4: 加冷却期自动恢复（原为永久禁用，无恢复机制）
    if session_id in self._compact_circuit_broken:
        broken_at = self._circuit_broken_at.get(session_id, 0)
        if _time.time() - broken_at < self.CIRCUIT_BREAKER_RECOVERY_SEC:
            _log.warning("Circuit breaker OPEN for session %s — skipping auto-compact",
                        session_id[:8])
            return None
        # 冷却期已过——重置熔断状态，允许重试
        self._compact_circuit_broken.discard(session_id)
        self._circuit_broken_at.pop(session_id, None)
        self._compact_failure_streak.pop(session_id, None)
        _log.info("Circuit breaker RECOVERED for session %s after %ds cooldown — retrying",
                  session_id[:8], self.CIRCUIT_BREAKER_RECOVERY_SEC)

    # 门控 1: 使用率阈值（98% —— 由外部传入的 max_tokens 控制）
    usage_ratio = current_tokens / max_tokens
    if usage_ratio < self.COMPACT_THRESHOLD:
        return None
    _log.info("Compaction triggered: usage=%.1f%% (%d/%d tokens)", usage_ratio * 100, current_tokens, max_tokens)

    # 门控 2: 冷却期——防止频繁压缩导致震荡
    now = _time.time()
    last = self._last_compact_time.get(session_id, 0)
    if now - last < self.COMPACT_COOLDOWN_SEC:
        return None

    # 门控 3: 最少消息数
    conv = self._sessions.get(session_id, [])
    if len(conv) < self.COMPACT_MIN_MESSAGES:
        return None

    # DSH compaction-tool-result-pruner：先裁剪超预算的 tool 结果。
    # 裁剪本身不调模型——压力解除了就不必再做一次摘要（省一次 LLM 调用）。
    conv, relieved = await _prune_for_pressure(
        self, session_id, conv, current_tokens, max_tokens)
    if relieved:
        _log.info("Tool-result pruning relieved the pressure — skipping summarization")
        self._last_compact_time[session_id] = now
        return None

    # 门控 4: tool 结果占比——高负载时放宽容忍度
    tool_ratio = self._calc_tool_result_ratio(conv)
    if _tool_ratio_gate(self, session_id, conv, usage_ratio):
        return None

    # ---- 阶段 1: 分级分类 ----
    recent, older = self._split_messages(conv, {"context": max_tokens}, system_prompt_tokens=system_prompt_tokens)
    snapshot = capture_snapshot(conv, len(older))

    older = _collapse_older(self, older)

    # ---- 阶段 2: 结构化摘要（增量合并） ----
    try:
        structured = await self._build_structured_summary(
            session_id, older, recent, tool_ratio,
            model_config={"context": max_tokens},
        )
    except Exception:
        _log.exception("Structured compaction failed, keeping full context")
        return None

    # ---- 阶段 3: 质量门控（探针失败时重试不同采样区间） ----
    passed = False
    probes = _extract_probes(older)
    from config import COMPACT_QUALITY_MAX_ATTEMPTS
    for attempt in range(COMPACT_QUALITY_MAX_ATTEMPTS):
        passed = await self._quality_gate(structured, older, conv, probes)
        if passed:
            break
        if attempt < 2 and len(older) > 10:
            # 换一个 older 子集重新采样探针
            offset = (attempt + 1) * (len(older) // 4)
            probes = _extract_probes(older[offset:])
            _log.warning(f"Session {session_id}: probe retry {attempt+1}/3 with offset={offset}")
        elif attempt == 2:
            # 最后尝试：放宽容忍度（<2/3 变为 ≥1/3）
            passed = await self._quality_gate(structured, older, conv, probes, lenient=True)
    if not passed:
        _log.warning(f"Session {session_id}: quality gate failed after 3 attempts, keeping full context")
        return None

    # ---- 阶段 4: 磁盘持久化 + 内存替换 ----
    await self._save_compact_marker(session_id, older)
    await self._persist_compaction_snapshot(session_id, older)
    if not await self._apply_compacted_context(session_id, structured, recent, snapshot):
        return None

    self._last_compact_time[session_id] = now
    self._structured_summaries[session_id] = structured
    # Phase 44 C.3: 压缩成功 → 重置失败计数器 + 清除熔断状态（Phase 48 C4 双重保险）
    self._compact_failure_streak.pop(session_id, None)
    self._compact_circuit_broken.discard(session_id)
    self._circuit_broken_at.pop(session_id, None)
    _log.info(
        f"Context compacted for {session_id}: "
        f"{len(older)} messages → {_summary_size(structured)} chars, "
        f"quality passed, {len(recent)} messages retained"
    )
    return structured


def _record_compact_failure(self, session_id: str):
    """Phase 44 C.3: 记录压缩失败并检查是否需要触发熔断器。

    对标 Claude Code: 连续 3 次 auto-compact 失败 → 永久禁用该 session 的 auto-compact。
    熔断通知通过 WS 推送（如果 agent_service 已注入）。
    """
    self._compact_failure_streak[session_id] = \
        self._compact_failure_streak.get(session_id, 0) + 1
    streak = self._compact_failure_streak[session_id]
    _log.warning("Compact failure %d/%d for session %s",
                streak, self.CIRCUIT_BREAKER_THRESHOLD, session_id[:8])
    if streak >= self.CIRCUIT_BREAKER_THRESHOLD:
        self._compact_circuit_broken.add(session_id)
        self._circuit_broken_at[session_id] = _time.time()  # Phase 48 C4: 记录熔断时间供冷却期恢复
        _log.error("Circuit breaker TRIPPED for session %s after %d consecutive failures",
                  session_id[:8], streak)
        if hasattr(self, '_agent_service') and self._agent_service:
            try:
                asyncio.create_task(
                    self._agent_service._safe_send(session_id, {
                        "type": "circuit_breaker_tripped",
                        "session_id": session_id,
                        "failures": streak,
                        "message": ("上下文自动压缩已熔断——连续 3 次失败。"
                                   f"将在 {self.CIRCUIT_BREAKER_RECOVERY_SEC // 60} 分钟后自动恢复重试，"
                                   "或使用 /compact 手动触发压缩以立即重置熔断器。"),
                    })
                )
            except Exception:
                _log.debug("发送熔断器 WS 通知失败", exc_info=True)


def get_circuit_breaker_status(self) -> dict:
    """Phase 44 C.3: 获取所有 session 的熔断器状态（供 get_own_metrics 工具使用）"""
    return {
        "broken_sessions": len(self._compact_circuit_broken),
        "broken_ids": [s[:8] for s in self._compact_circuit_broken],
        "failure_streaks": {
            k[:8]: v for k, v in self._compact_failure_streak.items()
        },
    }


# ============================================================
# 阶段 1 辅助: 分级分类
# ============================================================

def _is_tool_result(msg: dict) -> bool:
    """判断消息是否为工具调用结果（检查 role + tool_calls 标记）"""
    role = msg.get("role", "")
    # system 角色通常是注入的工具结果摘要
    if role == "system":
        return True
    # 真正的 tool 角色
    if role == "tool":
        return True
    # assistant 消息包含 tool_calls 标记时是工具调用响应
    if role == "assistant" and msg.get("tool_calls"):
        return True
    return False


def _is_user_instruction(msg: dict) -> bool:
    """判断是否为用户指令——压缩时应完整保留"""
    return msg.get("role") == "user"


def _calc_tool_result_ratio(self, conv: list[dict]) -> float:
    """计算 tool 结果在总消息中的占比"""
    if not conv:
        return 0.0
    tool_count = sum(1 for m in conv if _is_tool_result(m))
    return tool_count / len(conv)


def _split_messages(self, conv: list[dict], model_config: dict | None = None, system_prompt_tokens: int = 0) -> tuple[list[dict], list[dict]]:
    """拆分消息：按 token 预算保留最近消息，其余送压缩。

    Phase 56-P4: 从固定"最近 5 轮/10 条"改为按模型上下文窗口的 20% token 预算保留。
    主流做法：Claude Code / Codex CLI 保留最近上下文，旧历史才压缩。

    M4: system_prompt_tokens 参数——system prompt 已占用的 token 从 recent budget 中扣除，
    避免 system + recent 总和超窗口。最低下限 2K tokens。

    分级策略:
      - 用户指令: 优先完整保留在 recent 中
      - Tool 结果:  超阈值截断后送 older，原始内容已在 DB
      - Agent 推理: LLM 摘要压缩
    """
    from config import calc_recent_context_budget, estimate_tokens_for_messages

    if not conv:
        return [], []

    # 至少保留最后一条用户消息及其后的所有消息（保证任务连续性）
    user_indices = [i for i, m in enumerate(conv) if _is_user_instruction(m)]
    min_split_at = user_indices[-1] if user_indices else max(0, len(conv) - 1)

    # 按 token 预算从后往前累积，找到 split_at
    # M4: 扣除 system prompt 占用的 token，最低 2K
    budget_tokens = calc_recent_context_budget(model_config or {})
    if system_prompt_tokens > 0:
        budget_tokens = max(2_000, budget_tokens - system_prompt_tokens)
    accumulated = 0
    split_at = min_split_at
    for i in range(len(conv) - 1, -1, -1):
        msg = conv[i]
        content = msg.get("content", "")
        # 单条消息 token 估算：内容 + 格式开销
        msg_tokens = estimate_tokens_for_messages([msg], model_config.get("model", "gpt-4") if model_config else "gpt-4")
        if accumulated + msg_tokens > budget_tokens and i < min_split_at:
            split_at = i + 1
            break
        accumulated += msg_tokens
    else:
        # 全部消息在预算内——仍按 last user boundary 拆分，旧历史送压缩
        split_at = min_split_at

    # 保证最少保留最后 2 条，避免压缩过早切掉关键上下文
    split_at = min(split_at, max(0, len(conv) - 2))
    return conv[split_at:], conv[:split_at]


# ============================================================
# 阶段 2: 结构化摘要（借鉴 Factory AI 6 节格式）
# ============================================================

async def _build_structured_summary(
    self, session_id: str, older: list[dict], recent: list[dict], tool_ratio: float,
    model_config: dict | None = None,
) -> dict:
    """构建 6 节结构化摘要，增量合并已有摘要。

    借鉴 Factory AI: 每节强制输出，不依赖 LLM 记忆。
    借鉴 Claude Code: tool 结果先截断降级再送 LLM。
    """
    # 提取已有结构化摘要（增量合并基础）
    existing = self._structured_summaries.get(session_id, {})

    # 分级处理 older 消息
    graded = self._grade_messages(older, model_config)

    # 增量模式：只送新消息（不在已有摘要中的）
    new_messages_text = _format_graded_messages(graded)

    if not new_messages_text.strip():
        return existing  # 无新内容，返回已有摘要

    # 构建结构化摘要 prompt
    existing_text = _format_existing_summary(existing) if existing else "（首次压缩，无历史摘要）"
    prompt = STRUCTURED_COMPACT_PROMPT.format(
        existing_summary=existing_text,
        new_messages=new_messages_text,
        recent_context=_format_recent_context(recent),
        section_max_chars=COMPACT_SECTION_MAX_CHARS,
    )

    if self._summarizer:
        try:
            raw = await self._summarizer(prompt)
            return _parse_structured_summary(raw, existing)
        except Exception:
            _log.exception("LLM compaction call failed")
            raise

    # 无 _summarizer 时降级：手动提取
    return _fallback_structured_summary(older, existing)


def _grade_messages(self, messages: list[dict], model_config: dict | None = None) -> list[dict]:
    """分级处理：tool 结果截断，用户指令保留关键词，Agent 推理完整送。

    Phase 56-P2: 截断阈值按模型上下文窗口动态计算（fallback 到原固定值）。
    """
    from config import calc_context_budget
    _tool_threshold = 2000
    _tool_keep = 200
    _user_keep = 500
    _other_keep = 1000
    if model_config:
        _tool_threshold = calc_context_budget(model_config, fraction=0.03, min_chars=1500, max_chars=8000)
        _tool_keep = calc_context_budget(model_config, fraction=0.002, min_chars=150, max_chars=600)
        _user_keep = calc_context_budget(model_config, fraction=0.005, min_chars=300, max_chars=1200)
        _other_keep = calc_context_budget(model_config, fraction=0.01, min_chars=600, max_chars=2500)

    graded = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if _is_tool_result(m) and len(content) > _tool_threshold:
            # Tool 结果: 只保留前 N 字摘要 + 截断标记
            graded.append({
                "role": role, "grade": "tool_truncated",
                "content": content[:_tool_keep] + f"\n... [tool output truncated, {len(content)} total chars]",
            })
        elif _is_user_instruction(m):
            graded.append({
                "role": role, "grade": "user_full",
                "content": content[:_user_keep],
            })
        else:
            graded.append({
                "role": role, "grade": "full",
                "content": content[:_other_keep],
            })
    return graded


def _format_graded_messages(graded: list[dict]) -> str:
    """格式化为 LLM 可理解的分级文本"""
    lines = []
    for m in graded:
        tag = f"[{m['grade']}]" if m['grade'] != 'full' else ""
        role_label = (
            "用户" if m['role'] == 'user'
            else "Agent" if m['role'] == 'assistant'
            else f"[{m['role']}]"
        )
        lines.append(f"{tag} {role_label}: {m['content']}")
    return "\n".join(lines)


def _format_recent_context(recent: list[dict]) -> str:
    """格式化最近上下文（3 行摘要）"""
    if not recent:
        return "（无）"
    lines = []
    for m in recent[-COMPACT_RECENT_CONTEXT_MAX_MESSAGES:]:
        role = "用户" if m['role'] == 'user' else "Agent"
        lines.append(f"{role}: {m['content'][:COMPACT_RECENT_CONTEXT_PREVIEW_CHARS]}")
    return "\n".join(lines)


# ============================================================
# 阶段 3: 质量门控（借鉴 ContextPilot < 72 分回退）
# ============================================================

async def _quality_gate(
    self, structured: dict, older: list[dict], full_conv: list[dict],
    probes: list[str] | None = None, lenient: bool = False,
) -> bool:
    """3 探针问题验证压缩质量。lenient=True 时 ≥1/3 即可通过。

    探针 1: 事实留存——压缩后的摘要能否回答具体细节？
    探针 2: 文件追踪——是否记住了修改过的文件？
    探针 3: 任务连续性——是否知道下一步该做什么？
    """
    if probes is None:
        probes = _extract_probes(older)
    if not probes:
        return True  # 无探针可提取，信任压缩

    min_pass = 1 if lenient else max(1, len(probes) - 1)
    summary_text = _format_existing_summary(structured)

    # 验证：优先用 summarizer 回调；无 summarizer 时走默认 Agent Adapter（FUSION：Yuncat 不调 LLM）
    verify_fn = self._summarizer
    if not verify_fn and hasattr(self, '_adapters') and self._adapters:
        adapter = self._adapters.get(DEFAULT_AGENT_ID)
        if adapter:
            async def _adapter_verify(q: str) -> str:
                result = await adapter.invoke(
                    QUALITY_PROBE_PROMPT.format(summary=summary_text, question=q),
                    context={
                        "system": "Answer 是 or 否 plus ≤10 chars explanation.",
                        "max_tokens": MAX_TOKENS_MEMORY_COMPACT,
                        "temperature": 0.0,
                    },
                )
                if result.get("error"):
                    _log.warning("Quality gate adapter error: %s", result["error"])
                    return ""
                return result.get("content", "")

            verify_fn = _adapter_verify

    if not verify_fn:
        # 无任何验证能力 → 安全回退：手动触发时信任，自动触发时不信任
        _log.warning("Quality gate skipped: no summarizer and no adapter available")
        return False  # 宁可不压缩，也别出烂摘要

    score = 0
    async def _check_one(probe: str) -> bool:
        try:
            answer = await verify_fn(
                QUALITY_PROBE_PROMPT.format(summary=summary_text, question=probe)
            )
            return _is_probe_passed(answer)
        except Exception:
            _log.exception("Quality probe failed: %s...", probe[:COMPACT_PROBE_CONTENT_PREVIEW_CHARS])
            return False

    # Phase 33: 并行调 3 探针 — asyncio.gather 替代串行 for 循环
    results = await asyncio.gather(*[_check_one(p) for p in probes], return_exceptions=True)
    score = sum(1 for r in results if r is True)

    _log.info(f"Quality gate: {score}/{len(probes)} probes passed (min={min_pass}, lenient={lenient})")
    return score >= min_pass


def _extract_probes(older: list[dict]) -> list[str]:
    """从旧消息中提取探针问题。

    策略: 找最后一条 user 消息作为「任务意图」探针，
    找含文件路径的消息作为「文件追踪」探针，
    找含"错误/报错/error/fix"的消息作为「错误修复」探针。
    """
    probes = []
    # 探针 1: 任务意图
    user_msgs = [m for m in older if m.get("role") == "user"]
    if user_msgs:
        probes.append(
            f"用户最近在做什么任务？用户的原话是: {user_msgs[-1]['content'][:COMPACT_PROBE_CONTENT_PREVIEW_CHARS]}"
        )

    # 探针 2: 文件追踪
    file_msgs = [
        m for m in older
        if any(kw in m.get("content", "").lower() for kw in ["文件", "file", ".py", ".ts", ".rs", ".js", ".yaml"])
    ]
    if file_msgs:
        probes.append(
            f"涉及过哪些文件？请列出文件名。原始对话中提到过: {file_msgs[-1]['content'][:COMPACT_PROBE_CONTENT_PREVIEW_CHARS]}"
        )

    # 探针 3: 待办/决策
    probes.append("当前的待办事项和下一步计划是什么？")

    return probes[:COMPACT_PROBE_MAX_COUNT]


# ============================================================
# 阶段 4: 磁盘持久化 + 内存替换
# ============================================================

async def _persist_compaction_snapshot(self, session_id: str, older: list[dict]) -> None:
    """压缩前快照——workspace 文件 dump（Agent 可 grep 溯源）。

    DB 的压缩真源由 compaction_checkpoints 保存；文件快照保留原文供检索。
    """
    try:
        from security.sandbox import get_workspace
        import json as _json2
        ws = get_workspace()
        dump_dir = ws / ".yuncat" / "compactions"
        dump_dir.mkdir(parents=True, exist_ok=True)
        dump_path = dump_dir / f"{session_id[:12]}_{int(_time.time())}.json"
        dump_lines = []
        for m in older:
            role = m.get("role", "?")
            content = m.get("content", "")[:COMPACT_DUMP_CONTENT_CHARS]
            dump_lines.append(f"[{role}] {content}")
        dump_path.write_text("\n---\n".join(dump_lines), encoding="utf-8")
        _log.info("Compaction snapshot dumped to %s (%d messages)", dump_path, len(older))
    except Exception:
        _log.debug("Unexpected error", exc_info=True)  # dump 失败不阻塞压缩


async def _apply_compacted_context(
    self, session_id: str, structured: dict, recent: list[dict], snapshot,
) -> bool:
    """稳定提交摘要；成功后保留摘要期间追加的新消息。"""
    summary_text = _format_compact_boundary(structured)
    from services.memory.compaction_txn import apply_compaction_transaction
    return await apply_compaction_transaction(
        self, session_id, snapshot, structured, summary_text, recent,
    )


# ============================================================
# v55: 结构化摘要提示词与辅助函数（借鉴 Factory AI + ContextPilot）
# ============================================================

STRUCTURED_COMPACT_PROMPT = """从以下对话中**提取并照抄**关键信息。已有摘要供增量合并，**不要重复已有内容，只追加新发现**。

## 已有摘要（增量合并基础）
{existing_summary}

## 新对话内容（需要提取新信息）
{new_messages}

## 最近上下文（保留完整，帮你理解当前状态）
{recent_context}

## 输出格式（严格按以下 6 节输出。每节输出用方括号标记开始）:

[session_intent]
照抄用户原话中最能概括当前任务的 1 个句子。若无法确定，写"无"。

[files_modified]
逐行列出每个被提及/修改的文件。每行格式: `文件名` — 做了什么。**必须包含文件扩展名**。
例:
`app.py` — 新增 POST /users 路由
`models.py` — 新增 User 数据模型
若无则写"无"。

[key_decisions]
逐行列出每个决策。每行格式: 决策内容 — 为什么这样选择。
若无则写"无"。

[pending_tasks]
逐行列出未完成的待办事项。只列明确提到名字且还未做的事。
若无则写"无"。

[errors_and_fixes]
逐行列出遇到的错误。每行格式: 错误原文（照抄）— 修复方法。
若无则写"无"。

[next_steps]
基于上下文推断下一步应该做什么。一句话。
若无法推断，写"无"。

## 规则
- 宁可不写，不要编造。不确定就写"无"
- 文件路径必须包含扩展名。不要写 "app" 而要写 "app.py"
- 错误信息必须照抄原文，不要用自己的话改写
- 每节总内容不超过 {section_max_chars} 字
- 用中文输出
- 只输出上述 6 节标记和内容，不要加额外解释"""


QUALITY_PROBE_PROMPT = """基于以下上下文摘要回答探针问题。只回答"是"或"否"加最多 10 个字解释。

## 上下文摘要
{summary}

## 探针问题
{question}

## 回答格式
[是/否] <简短解释>"""


def _format_existing_summary(structured: dict) -> str:
    """格式化为已有的摘要文本"""
    if not structured:
        return "（无）"
    sections = [
        ("session_intent", "任务意图"),
        ("files_modified", "已修改文件"),
        ("key_decisions", "关键决策"),
        ("pending_tasks", "待办事项"),
        ("errors_and_fixes", "错误与修复"),
        ("next_steps", "下一步"),
    ]
    lines = []
    for key, label in sections:
        val = structured.get(key, "")
        if val and val != SUMMARY_EMPTY_SENTINEL:
            lines.append(f"[{label}] {val}")
    return "\n".join(lines) if lines else "（无）"


def _parse_structured_summary(raw: str, existing: dict | None = None) -> dict:
    """解析 LLM 输出的 6 节摘要，增量合并到已有摘要"""
    result = dict(existing) if existing else {}
    current_key = None
    keys_map = {
        "[session_intent]": "session_intent",
        "[files_modified]": "files_modified",
        "[key_decisions]": "key_decisions",
        "[pending_tasks]": "pending_tasks",
        "[errors_and_fixes]": "errors_and_fixes",
        "[next_steps]": "next_steps",
    }
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        matched = False
        for tag, key in keys_map.items():
            if tag in line.lower():
                current_key = key
                idx = line.lower().index(tag)
                content = line[idx + len(tag):].strip(": ：").strip()
                result[current_key] = _merge_section(result.get(current_key, ""), content)
                matched = True
                break
        if not matched and current_key:
            # 续行追加到当前节
            result[current_key] = _merge_section(result.get(current_key, ""), line)

    # 填充缺失的节
    for key in keys_map.values():
        result.setdefault(key, SUMMARY_EMPTY_SENTINEL)

    return result


def _merge_section(existing_val: str, new_val: str) -> str:
    """增量合并：新内容追加到已有内容后面"""
    if not existing_val or existing_val == SUMMARY_EMPTY_SENTINEL:
        return new_val.strip()
    if existing_val.endswith(new_val.strip()):
        return existing_val  # 已在其中
    return f"{existing_val}；{new_val.strip()}"


def _fallback_structured_summary(older: list[dict], existing: dict | None = None) -> dict:
    """无 LLM 时的降级结构化摘要——纯规则提取"""
    result = dict(existing) if existing else {}
    # 提取用户意图（保留已有摘要中的意图）
    if "session_intent" not in result or result["session_intent"] == SUMMARY_EMPTY_SENTINEL:
        user_msgs = [m for m in older if m.get("role") == "user"]
        if user_msgs:
            result["session_intent"] = user_msgs[-1]["content"][:COMPACT_FALLBACK_INTENT_CHARS]
    # 提取文件路径
    import re
    file_patterns = re.findall(r'[\w\-/\\]+\.(py|ts|js|rs|yaml|json|toml|md|txt)', "\n".join(m.get("content","") for m in older))
    if file_patterns:
        result["files_modified"] = ", ".join(set(file_patterns))[:COMPACT_FALLBACK_FILES_CHARS]

    result.setdefault("session_intent", SUMMARY_EMPTY_SENTINEL)
    result.setdefault("files_modified", SUMMARY_EMPTY_SENTINEL)
    result.setdefault("key_decisions", SUMMARY_EMPTY_SENTINEL)
    result.setdefault("pending_tasks", SUMMARY_EMPTY_SENTINEL)
    result.setdefault("errors_and_fixes", SUMMARY_EMPTY_SENTINEL)
    result.setdefault("next_steps", SUMMARY_EMPTY_SENTINEL)
    return result


def _format_compact_boundary(structured: dict) -> str:
    """格式化为 compact boundary 消息（Claude Code 风格）"""
    sections = [
        ("session_intent", "📋 任务", True),
        ("files_modified", "📁 文件", False),
        ("key_decisions", "🔑 决策", False),
        ("pending_tasks", "⏳ 待办", False),
        ("errors_and_fixes", "🔧 修复", False),
        ("next_steps", "▶️ 下一步", True),
    ]
    lines = ["[上下文已压缩]", ""]
    for key, label, required in sections:
        val = structured.get(key, "")
        if val and val != SUMMARY_EMPTY_SENTINEL:
            lines.append(f"**{label}**: {val}")
        elif required and not val:
            lines.append(f"**{label}**: （未知）")
    return "\n".join(lines)


def _summary_size(structured: dict) -> int:
    """估算结构化摘要的字符数"""
    return sum(len(v) for v in structured.values() if v and v != SUMMARY_EMPTY_SENTINEL)


def _is_probe_passed(answer: str) -> bool:
    """判断探针回答是否通过——回答以"是"开头即为通过"""
    return answer.strip().startswith("是") if answer else False
