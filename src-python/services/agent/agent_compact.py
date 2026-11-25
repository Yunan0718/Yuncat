"""services/agent_compact.py —— Agent 上下文压缩静态方法模块

从 agent_service.py 拆分: _micro_dump_tool_output / _micro_compact_history /
_snip_low_value_turns / _collapse_inactive_context / _extract_recent_files /
_emergency_trim_history。

这些原本是 AgentService 的 @staticmethod，被 ContextCompactor / memory_service
等外部模块通过 AgentService._method() 调用。拆分后保留为模块级函数，
AgentService 中保留 @staticmethod 薄包装以维持向后兼容。
"""
import logging
import re as _re

from config import (
    COMPACT_KEEP_RECENT_EMERGENCY,
    MAX_CONTEXT_FALLBACK_CHARS,
    MICRO_COMPACT_TOOL_HEAD_BUDGET,
    MICRO_COMPACT_TOOL_TAIL_BUDGET,
    MICRO_COMPACT_THINKING_BUDGET,
    MICRO_COMPACT_REASONING_CONTENT_BUDGET,
    AGENT_COMPACT_DEDUP_CONTENT_PREVIEW_CHARS,
    AGENT_COMPACT_RECOVERABLE_FILES_LIST_LIMIT,
    AGENT_COMPACT_RECOVERABLE_FILES_REFS_LIMIT,
    AGENT_COMPACT_TOOL_TRUNCATE_THRESHOLD_CHARS,
    AGENT_COMPACT_ASSISTANT_STRIP_THRESHOLD_CHARS,
    EXTRACT_RECENT_FILES_LIMIT,
    _AVG_TURN_FALLBACK,
    adaptive_truncate,
    resolve_context_window,
    calc_context_budget,
    calc_micro_compact_threshold,
    calc_micro_compact_preview_budget,
    calc_list_limit,
)

_log = logging.getLogger("yuncat.agent")


def estimate_history_tokens(history: list[dict], model_id: str = "gpt-4") -> int:
    """G1: 估算当前上下文 history 的 token 数。

    修复 _check_compaction 依赖 adapter.last_usage.prompt_tokens（滞后）的问题——
    last_usage 是上一次 LLM 调用的 prompt 大小，工具结果追加后实际上下文已膨胀。
    此函数估算当前 token 数，使压缩能主动触发而非被动滞后。

    CodeWhale 借鉴: 优先用 tiktoken 精确计数（对标 Codex CLI / Claude Code 本地 tokenizer），
    tiktoken 不可用时 fallback 到 CJK 感知估算（estimate_tokens_cjk）。
    较旧的 chars/3.5 估算在中文场景下误差可达 4-8x，导致压缩触发滞后。

    计入: content + tool_calls JSON + reasoning_content + 每条消息 4 token 格式开销
    """
    from token_budget import estimate_tokens

    total_tokens = 0
    for msg in history:
        # 拼接本条消息的全部文本内容
        parts: list[str] = []
        content = msg.get("content")
        if content:
            parts.append(str(content))
        # tool_calls JSON 计入
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            import json as _json
            try:
                parts.append(_json.dumps(tool_calls, ensure_ascii=False))
            except Exception:
                parts.append(str(tool_calls))
        # reasoning_content 计入（DeepSeek 推理块）
        rc = msg.get("reasoning_content")
        if rc:
            parts.append(str(rc))
        if parts:
            combined = "\n".join(parts)
            total_tokens += estimate_tokens(combined, model_id)
        # 每条消息格式开销（OpenAI 风格约 4 token/条）
        total_tokens += 4
    return total_tokens


def micro_dump_tool_output(content: str) -> str:
    """Claude Code 风格：截断的完整内容 dump 到 workspace 文件，
    返回引用路径供 Agent 恢复。"""
    try:
        from security.sandbox import get_workspace
        import hashlib, os as _os
        from pathlib import Path
        ws = get_workspace()
        dump_dir = _os.path.join(str(ws), ".yuncat", "compactions", "micro")
        _os.makedirs(dump_dir, exist_ok=True)
        content_hash = hashlib.sha1(content.encode()).hexdigest()[:12]
        fname = f"tool_output_{content_hash}.txt"
        fpath = _os.path.join(dump_dir, fname)
        Path(fpath).write_text(content, encoding='utf-8')
        return f".yuncat/compactions/micro/{fname}"
    except Exception:
        return ""


def micro_compact_history(history: list[dict], model_cfg: dict | None = None) -> int:
    """微型压缩——截断 Tool 输出 + 剥离 Thinking + 去重。0 次 API 调用。
    返回节省的字符数。

    借鉴 Claude Code microcompact：
      1. 最近 N 条 tool 结果始终保护（不截断），N 按模型窗口动态
      2. 其余 tool 结果：超大时落盘 + 头尾 preview；较大时动态截断
    借鉴 gptme phase-3：
      3. 连续相同 content 合并去重
    自身优化：
      4. 剥离 think/reasoning 块（DeepSeek V4 占 30-60% 输出）

    Phase 56-P2: 传入 model_cfg 时所有预算按 resolve_context_window 动态计算，
    否则回退到 MAX_CONTEXT_FALLBACK_CHARS / 原常量。
    """
    _has_cfg = bool(model_cfg)
    _window = resolve_context_window(model_cfg) if _has_cfg else MAX_CONTEXT_FALLBACK_CHARS

    # 动态预算（有 model_cfg）或回退常量（无 model_cfg，如测试路径）
    if _has_cfg:
        _tool_threshold = calc_micro_compact_threshold(model_cfg)
        _preview_budget = calc_micro_compact_preview_budget(model_cfg)
        _head_budget = max(MICRO_COMPACT_TOOL_HEAD_BUDGET, _preview_budget // 2)
        _tail_budget = max(MICRO_COMPACT_TOOL_TAIL_BUDGET, _preview_budget // 2)
        _dedup_preview = max(AGENT_COMPACT_DEDUP_CONTENT_PREVIEW_CHARS, _preview_budget // 4)
        _assistant_threshold = max(
            AGENT_COMPACT_ASSISTANT_STRIP_THRESHOLD_CHARS,
            calc_context_budget(model_cfg, fraction=0.015, min_chars=2000, max_chars=8000),
        )
        _thinking_budget = max(
            MICRO_COMPACT_THINKING_BUDGET,
            calc_context_budget(model_cfg, fraction=0.014, min_chars=1800, max_chars=8000),
        )
        _reasoning_budget = max(
            MICRO_COMPACT_REASONING_CONTENT_BUDGET,
            calc_context_budget(model_cfg, fraction=0.016, min_chars=2000, max_chars=10000),
        )
        _protected_count = calc_list_limit(model_cfg, base_limit=3, fraction_per_100k=2, max_limit=10)
    else:
        _tool_threshold = AGENT_COMPACT_TOOL_TRUNCATE_THRESHOLD_CHARS
        _preview_budget = MICRO_COMPACT_TOOL_HEAD_BUDGET
        _head_budget = MICRO_COMPACT_TOOL_HEAD_BUDGET
        _tail_budget = MICRO_COMPACT_TOOL_TAIL_BUDGET
        _dedup_preview = AGENT_COMPACT_DEDUP_CONTENT_PREVIEW_CHARS
        _assistant_threshold = AGENT_COMPACT_ASSISTANT_STRIP_THRESHOLD_CHARS
        _thinking_budget = MICRO_COMPACT_THINKING_BUDGET
        _reasoning_budget = MICRO_COMPACT_REASONING_CONTENT_BUDGET
        _protected_count = 3

    saved = 0
    # Find last N tool message indices (protected from truncation)
    tool_indices = [
        i for i, h in enumerate(history)
        if h.get("role") == "tool" or (
            h.get("role") == "assistant" and h.get("tool_calls")
        )
    ]
    protected = set(tool_indices[-_protected_count:]) if len(tool_indices) >= _protected_count else set()

    # Sort tool outputs by size — truncate largest first (gptme phase-2 style)
    tool_candidates = []
    for i, h in enumerate(history):
        if i in protected: continue
        content = str(h.get("content", ""))
        if h.get("role") in ("tool",) and len(content) > _tool_threshold:
            tool_candidates.append((i, len(content)))
    tool_candidates.sort(key=lambda x: -x[1])
    for i, orig_len in tool_candidates:
        content = str(history[i].get("content", ""))
        if len(content) <= _tool_threshold: break
        # Claude Code style: save full content to workspace file, replace with pointer
        dump_ref = micro_dump_tool_output(content)
        truncated = (
            adaptive_truncate(
                content, _window, _head_budget, 0.5,
            )
            + f"\n...[microcompact_trimmed, {len(content)} total chars]...\n"
            + (f"完整内容: {dump_ref}\n" if dump_ref else "")
            + content[-_tail_budget:]
        )
        saved += len(content) - len(truncated)
        history[i]["content"] = truncated

    prev_content = None

    for i, h in enumerate(history):
        if i in protected: prev_content = None; continue
        role = h.get("role", "")
        content = str(h.get("content", ""))

        # 1) 去重：连续相同内容 → 截断为提示
        if content == prev_content and len(content) > _dedup_preview:
            saved += len(content) - _dedup_preview
            history[i]["content"] = f"[同上，内容相同] {content[:_dedup_preview]}"
            continue
        prev_content = content

        if role == "assistant" and i not in protected and len(content) > _assistant_threshold:
            # 4) 剥离 think / reasoning 块
            # DeepSeek: think...end 或 end-of-thinking 块
            stripped = _re.sub(r'<\s*think\s*>.*?<\s*/\s*think\s*>', '', content,
                               flags=_re.DOTALL | _re.IGNORECASE)
            stripped = _re.sub(r'\n+', '\n', stripped).strip()
            if len(stripped) < len(content) and stripped:
                saved += len(content) - len(stripped)
                history[i]["content"] = (
                    adaptive_truncate(
                        stripped, _window, _thinking_budget, 0.5,
                    )
                    + f"\n\n[thinking block removed, "
                    f"saved ~{len(content) - len(stripped)} chars]"
                )
            # 5) 截断 reasoning_content 字段（DeepSeek V4 每轮注入 ~32K 推理 token）
            # 保留开头 N 字符满足多轮契约（字段必须非空），大量推理占用上下文空间
            rc = h.get("reasoning_content", "")
            if rc and len(rc) > _reasoning_budget:
                saved += len(rc) - _reasoning_budget
                h["reasoning_content"] = rc[:_reasoning_budget]

    # Phase 44 C.5: Snip 层 — 微压缩前先剪掉低价值轮次
    _len_before_snip = sum(len(str(h.get("content", ""))) for h in history)
    _snipped = snip_low_value_turns(history)
    history[:] = _snipped
    saved += _len_before_snip - sum(len(str(h.get("content", ""))) for h in _snipped)

    # Phase 44 C.7: 结构化 recoverable_file_refs
    _recover_limit = calc_list_limit(model_cfg, AGENT_COMPACT_RECOVERABLE_FILES_LIST_LIMIT, fraction_per_100k=3, max_limit=20) if _has_cfg else AGENT_COMPACT_RECOVERABLE_FILES_LIST_LIMIT
    _refs_limit = calc_list_limit(model_cfg, AGENT_COMPACT_RECOVERABLE_FILES_REFS_LIMIT, fraction_per_100k=5, max_limit=50) if _has_cfg else AGENT_COMPACT_RECOVERABLE_FILES_REFS_LIMIT
    recent_files = extract_recent_files(history)
    if recent_files:
        history.append({
            "role": "system",
            "content": f"[微型压缩完成。可恢复文件引用: {', '.join(recent_files[:_recover_limit])}]",
            "recoverable_file_refs": recent_files[:_refs_limit],
        })

    return saved


def snip_low_value_turns(history: list[dict]) -> list[dict]:
    """删除满足以下任一条件的轮次:
    - 空工具结果 (content 长度 < 10)
    - 被用户拒绝的工具调用 (content 含 "rejected" / "denied")
    - 连续 3 条相同的 system 消息
    """
    snipped = []
    prev_system_count = 0
    for h in history:
        role = h.get("role", "")
        content = str(h.get("content", ""))

        if role == "tool" and len(content) < 10:
            continue  # 空工具结果 → 删除
        if role == "tool" and any(kw in content.lower()
                                   for kw in ["rejected", "denied", "user cancelled"]):
            continue  # 被拒工具调用 → 删除
        if role == "system":
            prev_system_count += 1
            if prev_system_count > 3 and snipped and content == snipped[-1].get("content", ""):
                continue  # 连续重复 system 消息 → 只保留第一条
        else:
            prev_system_count = 0
        snipped.append(h)
    return snipped


def collapse_inactive_context(history: list[dict],
                                active_window: int = 20) -> list[dict]:
    """保留最近 active_window 条消息完整，其余折叠为一句话摘要。
    不对 LLM 调用来生成摘要——纯规则: 保留 system 消息 + 第一条 user 消息 + 最近 N 条。
    """
    if len(history) <= active_window:
        return history
    active = history[-active_window:]
    stale = history[:-active_window]
    # 保留 system 消息 + 第一条 user 消息
    first_user = next((h for h in stale if h.get("role") == "user"), None)
    essentials = [h for h in stale if h.get("role") == "system"]
    if first_user and first_user not in essentials:
        essentials.insert(0, first_user)
    # 增加 collapse marker
    collapse_marker = {
        "role": "system",
        "content": f"[{len(stale)} 条历史消息已折叠为上下文摘要，"
                   f"保留 {len(essentials)} 条关键消息]",
    }
    return essentials + [collapse_marker] + active


def extract_recent_files(history: list[dict]) -> list[str]:
    """从对话历史中提取最近操作的文件路径（去重、保持顺序）。"""
    seen = set()
    files = []
    # 倒序遍历（最近的优先）
    for h in reversed(history):
        content = str(h.get("content", ""))
        # 匹配反引号中的文件路径
        matches = _re.findall(
            r'`([^`\s]+\.(?:py|tsx?|jsx?|json|yaml|yml|md|html|css|rs|go|java|sql|txt|toml|ini|cfg|env))`',
            content,
        )
        for m in matches:
            if m not in seen and not m.startswith(("http", "./", "../")):
                    seen.add(m)
                    files.append(m)
                    if len(files) >= EXTRACT_RECENT_FILES_LIMIT:
                        return files
    return files


# ============================================================
# CodeWhale 借鉴: 工作集提取 + 钉住启发式
# ============================================================

# 47 种源码扩展名白名单（对齐 CodeWhale compaction.rs WORKING_SET_EXTENSIONS）
_WORKING_SET_EXTENSIONS = frozenset({
    ".py", ".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".md", ".rst", ".txt",
    ".html", ".css", ".scss", ".sass", ".less",
    ".rs", ".go", ".java", ".kt", ".scala", ".groovy",
    ".c", ".h", ".cpp", ".cc", ".hpp", ".cxx",
    ".sql", ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".dockerfile", ".makefile", ".cmake",
    ".vue", ".svelte", ".astro",
    ".graphql", ".proto",
})

# 排除目录（依赖/构建产物——不进工作集）
_EXCLUDED_DIRS = frozenset({
    "node_modules", "target", "vendor", "dist", "build",
    "__pycache__", ".git", ".venv", "venv", "env",
    ".next", ".nuxt", ".output",
})

# 工具调用参数中可能包含文件路径的字段名
_PATH_ARG_KEYS = frozenset({
    "path", "file", "target", "cwd", "paths", "files", "targets",
    "file_path", "filename", "dest", "destination", "source",
})

# 错误标记——含这些词的消息强制保留（对标 CodeWhale should_pin_message）
_ERROR_MARKERS = frozenset({
    "error", "failed", "panic", "traceback", "exception",
    "errno", "fatal", "crash", "segfault",
})

# Patch 标记——含这些模式的消息强制保留
_PATCH_MARKERS = frozenset({
    "diff --git", "apply_patch", "--- a/", "+++ b/", "@@ ",
})


def derive_working_set_paths(
    history: list[dict],
    window: int = 12,
    max_paths: int = 24,
) -> set[str]:
    """CodeWhale 借鉴: 从最近 N 条消息的工具调用中提取工作集文件路径。

    - 扫描 tool_calls 参数中的 path/file/target/cwd 等字段
    - 扫描 tool 角色消息的 content 中的文件路径
    - 过滤: 只保留源码扩展名，排除 node_modules/target/vendor 等依赖目录
    """
    recent = history[-window:] if len(history) > window else history
    paths: set[str] = set()
    for msg in recent:
        # 1. 从 tool_calls 参数提取
        tool_calls = msg.get("tool_calls")
        if tool_calls and isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                args = tc.get("function", {}).get("arguments", {})
                if isinstance(args, str):
                    import json as _json
                    try:
                        args = _json.loads(args)
                    except Exception:
                        args = {}
                if isinstance(args, dict):
                    for key in _PATH_ARG_KEYS:
                        val = args.get(key)
                        if isinstance(val, str) and val:
                            _add_path_if_valid(paths, val, max_paths)
                        elif isinstance(val, list):
                            for v in val:
                                if isinstance(v, str) and v:
                                    _add_path_if_valid(paths, v, max_paths)
        # 2. 从 tool 消息 content 提取（如 read_file 返回的路径）
        if msg.get("role") == "tool":
            content = str(msg.get("content", ""))
            for m in _re.finditer(r'`([^`\s]+\.\w+)`', content):
                _add_path_if_valid(paths, m.group(1), max_paths)
        if len(paths) >= max_paths:
            break
    return paths


def _add_path_if_valid(paths: set[str], path: str, max_paths: int) -> None:
    """添加路径到工作集——校验扩展名 + 排除依赖目录。"""
    if len(paths) >= max_paths:
        return
    path_lower = path.lower().replace("\\", "/")
    # 排除依赖目录
    for excluded in _EXCLUDED_DIRS:
        if f"/{excluded}/" in path_lower:
            return
    # 校验扩展名
    ext = "." + path_lower.rsplit(".", 1)[-1] if "." in path_lower else ""
    if ext in _WORKING_SET_EXTENSIONS:
        paths.add(path.replace("\\", "/"))


def should_pin_message(
    msg: dict,
    working_set: set[str],
    msg_index: int = -1,
    recent_count: int = 4,
    total_messages: int = 0,
) -> bool:
    """CodeWhale 借鉴: 启发式钉住消息——保留不压缩。

    钉住条件（满足任一）:
    1. 最近 N 条消息（尾部保留）
    2. 消息内容提及工作集路径
    3. 含错误标记（error/failed/panic/traceback）
    4. 含 patch 标记（diff --git/apply_patch）
    """
    # 1. 最近 N 条始终钉住
    if total_messages > 0 and msg_index >= total_messages - recent_count:
        return True
    content = str(msg.get("content", "")).lower()
    # 2. 提及工作集路径
    for path in working_set:
        if path.lower() in content:
            return True
    # 3. 含错误标记
    for marker in _ERROR_MARKERS:
        if marker in content:
            return True
    # 4. 含 patch 标记
    for marker in _PATCH_MARKERS:
        if marker in content:
            return True
    return False


# ============================================================
# CodeWhale 借鉴: Agent 驱动的上下文清除（purge）
# ============================================================

def execute_purge_operations(
    history: list[dict],
    indices_to_remove: set[int],
) -> dict:
    """CodeWhale 借鉴: 执行 purge 操作——配对级联删除（fixpoint 迭代）。

    ToolUse ↔ ToolResult 必须成对删除，否则 API 报错。
    用不动点迭代直到 remove_set 闭合。

    Args:
        history: 会话历史列表（将被原地修改）
        indices_to_remove: 要删除的消息索引集合（0-based）

    Returns:
        {"removed_count": int, "cascade_count": int}
    """
    if not indices_to_remove:
        return {"removed_count": 0, "cascade_count": 0}

    original_count = len(indices_to_remove)
    # 阶段 1: 计算完整删除集（含级联）
    full_indices = _compute_purge_indices(history, indices_to_remove)
    cascade_count = len(full_indices) - original_count

    # 阶段 2: 降序删除——保持低索引稳定
    for idx in sorted(full_indices, reverse=True):
        if 0 <= idx < len(history):
            del history[idx]

    return {"removed_count": len(full_indices), "cascade_count": cascade_count}


def _compute_purge_indices(
    history: list[dict],
    indices_to_remove: set[int],
) -> set[int]:
    """计算 purge 的完整删除集——含级联配对，不修改 history。

    供 purge_context 在删除前收集 DB conv_id 用——先算出哪些消息要删，
    再从 DB 删对应行，最后从内存删。
    """
    if not indices_to_remove:
        return set()
    result = set(indices_to_remove)

    # fixpoint 迭代——直到 remove_set 不再增长
    changed = True
    while changed:
        changed = False
        new_removals = set()
        for idx in result:
            if idx < 0 or idx >= len(history):
                continue
            msg = history[idx]
            # Case 1: assistant 消息含 tool_calls → 级联删除对应 tool 消息
            tool_calls = msg.get("tool_calls")
            if tool_calls and isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    tc_id = tc.get("id", "")
                    if not tc_id:
                        continue
                    for j, other in enumerate(history):
                        if j in result:
                            continue
                        if other.get("tool_call_id") == tc_id:
                            new_removals.add(j)
                            break
            # Case 2: tool 消息（有 tool_call_id）→ 级联删除对应 assistant 消息
            tc_id = msg.get("tool_call_id")
            if tc_id:
                for j, other in enumerate(history):
                    if j in result:
                        continue
                    other_tcs = other.get("tool_calls")
                    if not other_tcs:
                        continue
                    for tc in other_tcs:
                        if isinstance(tc, dict) and tc.get("id") == tc_id:
                            new_removals.add(j)
                            break
        if new_removals:
            result |= new_removals
            changed = True

    return result


def emergency_trim_history(
    history: list[dict],
    keep_recent: int = COMPACT_KEEP_RECENT_EMERGENCY,
    model_config: dict | None = None,
) -> list[dict]:
    """99.99% 紧急修剪——保留最近 N 条 + 当前 task 相关的消息。
    不影响当前任务执行，完成后自动触发全量压缩。

    Task 7.3: model_config 非空时按模型窗口动态计算 keep_recent。
    大窗口模型可保留更多近期消息（对标 Claude Code 按窗口比例修剪）。
    显式传 keep_recent（非默认值）时优先于 model_config——调用方控制。
    """
    # Task 7.3: keep_recent 感知模型窗口——仅在用默认值且提供 model_config 时动态计算
    if model_config is not None and keep_recent == COMPACT_KEEP_RECENT_EMERGENCY:
        window = resolve_context_window(model_config)
        # 每条消息约 _AVG_TURN_FALLBACK tokens，保留窗口 20% 的近期消息
        # 128K → 21（但下限保护到 30）, 256K → 42, 1M → 100（上限）
        dynamic_keep = max(COMPACT_KEEP_RECENT_EMERGENCY, int(window * 0.2 / _AVG_TURN_FALLBACK))
        keep_recent = min(dynamic_keep, 100)
    if len(history) <= keep_recent:
        return history
    # 保留最旧的 system 角色消息（记忆注入）+ 最近 N 条
    system_msgs = [h for h in history if h.get("role") == "system"]
    recent = history[-keep_recent:]
    # 去重（按消息内容而非 Python 内存地址）
    seen_ids: set[str] = set()
    trimmed = []
    for h in system_msgs + recent:
        hid = h.get("id", "") or h.get("message_id", "") or str(h.get("content", ""))[:40]
        if hid and hid not in seen_ids:
            trimmed.append(h)
            seen_ids.add(hid)
    return trimmed
