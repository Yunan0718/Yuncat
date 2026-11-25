"""services/long_task/executor.py — Phase 87 长任务执行策略与并发控制

拓扑排序 + 有限并行（MAX_CONCURRENT=3）+ 失败重试 + blocked 状态传播。
从 orchestrator._execute_steps 提取单节点逻辑，升级为并行执行器。

设计决策（见 .trae/documents/Yuncat-Master-Implementation-Plan.md Phase 87）:
- 决策 1: 保留 spawn_subagent（不改 agent_lifecycle.run_agent）——Simplicity First
- 决策 2: 信号量限并发（MAX_LONG_TASK_CONCURRENT_NODES=3）
- 决策 3: blocked 节点不 fail 整个任务——停止同分支，独立分支继续
- 决策 4: 可重试错误（网络/限流/文件锁）自动重试 2 次（指数退避）；
          不可重试错误调 StandardsLookup（best-effort）后标 blocked
"""
import asyncio
import json as _json
import logging
import re
import time as _time

from config import (
    LOG_PREVIEW_CHARS, LOG_PREVIEW_CMD_LEN, LOG_PREVIEW_ERROR_LEN,
    LONG_TASK_WINDOW_ROLLOVER_ENABLED, WORKER_AGENT_ID,
)

# 长任务窗口翻滚: 翻滚判定 + 监督闭环 (best-effort, 不阻塞执行)
from services.long_task import window_rollover as _wr
# Task 6.15: 截断溯源指针工具
from text_utils import truncate_with_pointer
# Task 6.16: 共享重试策略（统一 long_task 与 agent_lifecycle 两条 plan 执行路径）
from retry_utils import (
    MAX_RETRIES as _MAX_RETRIES,
    RETRY_BASE_DELAY as _RETRY_BASE_DELAY,
    is_retriable as _is_retriable,
)
from services.model_router import infer_step_tier, _resolve_step_tier_from_role

from repositories.plan_step_repo import (
    get_plan_tree_by_task,
    update_plan_node_status,
    create_plan_node,
)
# 长任务窗口翻滚 · 增强1: 翻滚后把 Worker 节点产出写回 worker_session_id 会话
# (conversations.agent_id 仅为标签, 不要求是可 spawn 的 adapter, 故写 WORKER_AGENT_ID 安全)。
from repositories.conversations_repo import insert_conversation

_log = logging.getLogger("yuncat.executor")

MAX_LONG_TASK_CONCURRENT_NODES = 3

# 模型不可用关键词（非瞬时网络错误，而是模型/配置级错误）
_MODEL_UNAVAILABLE_KEYWORDS = (
    "model not found", "unknown model", "invalid model", "unsupported model",
    "model is deprecated", "model unavailable", "model disabled",
    "bad gateway", "gateway timeout",
)

# tier 升级链（lite -> normal -> max；max 与 vision 不再升级）
_TIER_ESCALATION = {
    "lite": "normal",
    "normal": "max",
}

# blocked 步骤 AI 决策最大次数（防止死循环）
_BLOCKED_DECISION_MAX = 1


def _merge_dependency_outputs(node: dict, tree: list[dict]) -> str:
    """收集 node 的 dependencies 中已完成节点的结构化输出作为上下文

    Phase 87: 并行执行时每个节点需要自己的上下文（非线性的 prev_outputs）。
    Phase 108: 优先使用 step_output 结构化字段（文件、测试、关键决策、模型），
    回退到 output 文本摘要。
    """
    deps = node.get("dependencies", []) or []
    if not deps:
        return ""
    sections = []
    for n in tree:
        if n["id"] not in deps or n["status"] != "completed":
            continue
        step_out = n.get("step_output") or {}
        title = n.get("title", "")
        parts = [f"## {title}"]

        # 结构化字段
        files_created = step_out.get("files_created") or []
        files_modified = step_out.get("files_modified") or []
        tests_created = step_out.get("tests_created") or []
        key_decisions = step_out.get("key_decisions") or []
        model_used = step_out.get("model_used") or {}
        if files_created:
            parts.append(f"- 创建文件: {', '.join(files_created)}")
        if files_modified:
            parts.append(f"- 修改文件: {', '.join(files_modified)}")
        if tests_created:
            parts.append(f"- 创建测试: {', '.join(tests_created)}")
        if key_decisions:
            parts.append(f"- 关键决策: {'; '.join(key_decisions)}")
        if model_used:
            parts.append(f"- 使用模型: {model_used.get('provider', '')}/{model_used.get('model', '')}")

        # 输出摘要（兜底）——仅当 step_output 未提取到任何结构化内容时回退，
        # 否则 Phase 108 后 step_output 恒非空，此分支会成为死代码
        output = n.get("output", "")
        has_structured = any([
            step_out.get("files_created"),
            step_out.get("files_modified"),
            step_out.get("tests_created"),
            step_out.get("key_decisions"),
            step_out.get("model_used"),
        ])
        if output and not has_structured:
            preview = truncate_with_pointer(
                output, LOG_PREVIEW_ERROR_LEN * 2, n["id"],
            )
            parts.append(f"- 输出摘要:\n{preview}")

        sections.append("\n".join(parts))
    if not sections:
        return ""
    return "[前序依赖输出摘要]\n\n" + "\n\n".join(sections)


# 常见文件操作描述模式（中英双语），用于从子 Agent 输出中提取文件变更
_FILE_CREATED_PATTERNS = [
    re.compile(r"created file[`\s]+([\w\-/\\.]+)", re.IGNORECASE),
    re.compile(r"创建文件[`\s]+([\w\-/\\.]+)", re.IGNORECASE),
    re.compile(r"wrote [`\s]+([\w\-/\\.]+)", re.IGNORECASE),
    re.compile(r"写入 [`\s]+([\w\-/\\.]+)", re.IGNORECASE),
]
_FILE_MODIFIED_PATTERNS = [
    re.compile(r"modified file[`\s]+([\w\-/\\.]+)", re.IGNORECASE),
    re.compile(r"修改文件[`\s]+([\w\-/\\.]+)", re.IGNORECASE),
    re.compile(r"updated [`\s]+([\w\-/\\.]+)", re.IGNORECASE),
    re.compile(r"更新 [`\s]+([\w\-/\\.]+)", re.IGNORECASE),
]
_TEST_CREATED_PATTERNS = [
    re.compile(r"test file[`\s]+([\w\-/\\.]+)", re.IGNORECASE),
    re.compile(r"测试文件[`\s]+([\w\-/\\.]+)", re.IGNORECASE),
    re.compile(r"created test[`\s]+([\w\-/\\.]+)", re.IGNORECASE),
]


def _extract_paths(text: str, patterns: list) -> list[str]:
    """从文本中提取文件路径列表（去重、过滤空值）。"""
    found: list[str] = []
    for pat in patterns:
        for m in pat.finditer(text):
            path = m.group(1).strip()
            if path and path not in found:
                found.append(path)
    return found


def _is_model_unavailable(error: str) -> bool:
    """错误是否属于模型/配置级不可用（非瞬时网络错误）。"""
    err_lower = (error or "").lower()
    return any(kw in err_lower for kw in _MODEL_UNAVAILABLE_KEYWORDS)


def _classify_failure(error: str) -> str:
    """将失败原因分类为 retriable / model_unavailable / business。"""
    if _is_retriable(error):
        return "retriable"
    if _is_model_unavailable(error):
        return "model_unavailable"
    return "business"


def _next_tier(tier: str, max_mode: bool = False, capability_level: int = 1) -> str | None:
    """返回下一个更高 tier；MAX 模式/轻量档或已是 max/vision 时不升级。

    capability_level==0(轻量) 时 tier 封顶在 normal，不再升到 max。
    """
    if max_mode or tier in ("max", "vision"):
        return None
    if capability_level == 0 and tier == "normal":
        return None
    return _TIER_ESCALATION.get(tier)


def _fix_hint(category: str, error: str, language: str) -> str:
    """基于失败类别生成简短修复提示（跟随 UI 语言）。"""
    hints = {
        "zh": {
            "retriable": "这是瞬时错误，等待后重试；若持续失败请检查网络或模型服务状态。",
            "model_unavailable": "当前模型不可用，将尝试切换到 fallback 模型或更高 tier。",
            "business": "这是业务/逻辑错误，请分析错误信息修复输入或实现，避免重复相同请求。",
        },
        "en": {
            "retriable": "This is a transient error; retry after a delay. If it persists, check network or model service status.",
            "model_unavailable": "The current model is unavailable; switching to fallback or a higher tier.",
            "business": "This is a business/logic error; analyze the error and fix the input or implementation before retrying.",
        },
        "ja": {
            "retriable": "一時的なエラーです。少し待ってから再試行してください。継続する場合はネットワークまたはモデルサービスを確認してください。",
            "model_unavailable": "現在のモデルは利用できません。フォールバックまたは上位 tier に切り替えます。",
            "business": "これはビジネス/ロジックエラーです。エラーを分析して入力または実装を修正してください。",
        },
        "ko": {
            "retriable": "일시적인 오류입니다. 잠시 후 다시 시도하세요. 지속되면 네트워크 또는 모델 서비스 상태를 확인하세요.",
            "model_unavailable": "현재 모델을 사용할 수 없습니다. fallback 또는 상위 tier로 전환합니다.",
            "business": "비즈니스/논리 오류입니다. 오류를 분석하여 입력 또는 구현을 수정하세요.",
        },
    }
    return hints.get(language, hints["zh"]).get(category, hints["zh"]["business"])


def _format_retry_context(
    attempt: int,
    max_retries: int,
    previous_error: str,
    previous_model: dict,
    previous_tier: str,
    fix_hint: str,
    language: str,
) -> str:
    """将重试上下文序列化为可读文本，追加到子 Agent prompt。"""
    ctx = {
        "attempt": attempt,
        "max_retries": max_retries,
        "previous_error": previous_error,
        "previous_model": previous_model,
        "previous_tier": previous_tier,
        "fix_hint": fix_hint,
    }
    header = {
        "zh": "[重试上下文] 上一次执行失败，请参考以下信息调整方案后继续：",
        "en": "[Retry context] Previous execution failed; adjust and continue based on the following:",
        "ja": "[再試行コンテキスト] 前回の実行が失敗しました。以下を参考に調整して続行してください。",
        "ko": "[재시도 컨텍스트] 이전 실행이 실패했습니다. 다음 정보를 참고하여 조정 후 계속하세요.",
    }.get(language, "[Retry context]")
    return f"{header}\n{_json.dumps(ctx, ensure_ascii=False, indent=2)}"


def _append_retry_context(
    prompt: str,
    attempt: int,
    max_retries: int,
    previous_error: str,
    previous_model: dict,
    previous_tier: str,
    language: str,
) -> str:
    """在原 prompt 后追加重试上下文。"""
    category = _classify_failure(previous_error)
    hint = _fix_hint(category, previous_error, language)
    ctx_text = _format_retry_context(
        attempt, max_retries, previous_error, previous_model,
        previous_tier, hint, language,
    )
    return f"{prompt}\n\n{ctx_text}"


def _parse_blocked_decision(output: str) -> dict:
    """从子 Agent 输出解析 blocked 决策 JSON。"""
    text = output or ""
    # 优先取 markdown 代码块
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if m:
        text = m.group(1)
    text = text.strip()
    try:
        data = _json.loads(text)
    except (_json.JSONDecodeError, TypeError):
        return {"action": "blocked", "reason": "decision output is not valid JSON"}
    action = data.get("action", "blocked")
    if action not in ("skip", "retry", "split", "blocked"):
        action = "blocked"
    return {
        "action": action,
        "reason": data.get("reason", ""),
        "new_sub_steps": data.get("new_sub_steps") or [],
    }


def _build_blocked_decision_prompt(node: dict, error: str, goal: str, language: str) -> str:
    """构造 blocked 步骤 AI 决策 prompt（跟随 UI 语言）。"""
    title = node.get("title", "")
    description = node.get("description", "")
    texts = {
        "zh": (
            "你是一位任务监督者。某计划步骤在多次重试后仍然失败，请决定下一步动作。\n"
            f"目标：{goal}\n"
            f"步骤标题：{title}\n"
            f"步骤描述：{description}\n"
            f"错误信息：{error}\n"
            "可选动作：skip（跳过该步骤，视为非关键）、retry（重试）、split（拆分为更小子步骤）。\n"
            "请只返回 JSON：{\"action\": \"skip|retry|split\", \"reason\": \"...\", \"new_sub_steps\": [{\"title\": \"...\", \"description\": \"...\"}]}"
        ),
        "en": (
            "You are a task supervisor. A plan step failed after retries. Decide the next action.\n"
            f"Goal: {goal}\n"
            f"Step title: {title}\n"
            f"Step description: {description}\n"
            f"Error: {error}\n"
            "Actions: skip (step is non-critical), retry (retry with adjustments), split (break into smaller sub-steps).\n"
            "Return only JSON: {\"action\": \"skip|retry|split\", \"reason\": \"...\", \"new_sub_steps\": [{\"title\": \"...\", \"description\": \"...\"}]}"
        ),
        "ja": (
            "タスク監督者として、何度も再試行した計画ステップが失敗した場合の次の行動を決定してください。\n"
            f"目標：{goal}\n"
            f"ステップタイトル：{title}\n"
            f"ステップ説明：{description}\n"
            f"エラー：{error}\n"
            "選択肢：skip（重要でないためスキップ）、retry（調整して再試行）、split（より小さいサブステップに分割）\n"
            "JSON のみ返してください：{\"action\": \"skip|retry|split\", \"reason\": \"...\", \"new_sub_steps\": [{\"title\": \"...\", \"description\": \"...\"}]}"
        ),
        "ko": (
            "태스크 감독자로서 여러 번 재시도한 후에도 실패한 계획 단계의 다음 작업을 결정하세요.\n"
            f"목표：{goal}\n"
            f"단계 제목：{title}\n"
            f"단계 설명：{description}\n"
            f"오류：{error}\n"
            "선택: skip(중요하지 않음), retry(조정 후 재시도), split(더 작은 하위 단계로 분할)\n"
            "JSON만 반환하세요: {\"action\": \"skip|retry|split\", \"reason\": \"...\", \"new_sub_steps\": [{\"title\": \"...\", \"description\": \"...\"}]}"
        ),
    }
    return texts.get(language, texts["zh"])


async def _update_node_review(conn, node_id: str, patch: dict) -> None:
    """merge 更新 plan_steps.review JSON 字段。"""
    cur = await conn.execute("SELECT review FROM plan_steps WHERE id=?", (node_id,))
    row = await cur.fetchone()
    review: dict = {}
    if row and row[0]:
        try:
            review = _json.loads(row[0])
        except (_json.JSONDecodeError, TypeError):
            review = {}
    review.update(patch)
    await conn.execute(
        "UPDATE plan_steps SET review=?, updated_at=? WHERE id=?",
        (_json.dumps(review, ensure_ascii=False), int(_time.time()), node_id),
    )
    await conn.commit()


async def _replace_node_with_children(
    conn, task_id: str, node: dict, sub_steps: list[dict],
) -> bool:
    """将 blocked 节点拆分为子节点，并更新下游依赖指向最后一个子节点。"""
    if not sub_steps:
        return False
    parent_id = node["id"]
    level = int(node.get("level", 0)) + 1
    base_deps = node.get("dependencies") or []
    child_ids: list[str] = []
    for step in sub_steps:
        title = (step.get("title") or "").strip()
        if not title:
            continue
        nid = await create_plan_node(conn, {
            "task_id": task_id,
            "parent_id": parent_id,
            "level": level,
            "title": title,
            "description": step.get("description", ""),
            "dependencies": list(base_deps),
            "acceptance_criteria": step.get("acceptance_criteria", []),
            "status": "pending",
        })
        child_ids.append(nid)
    if not child_ids:
        return False
    tree = await get_plan_tree_by_task(conn, task_id)
    for n in tree:
        if n["id"] == parent_id:
            continue
        deps = n.get("dependencies") or []
        if parent_id in deps:
            new_deps = [child_ids[-1] if d == parent_id else d for d in deps]
            await conn.execute(
                "UPDATE plan_steps SET dependencies=? WHERE id=?",
                (_json.dumps(new_deps, ensure_ascii=False), n["id"]),
            )
    await conn.commit()
    return True


async def _decide_blocked_step_action(
    orch, task_id: str, node: dict, error: str, agent_id: str, language: str,
) -> dict:
    """blocked 步骤 AI 决策：skip / retry / split（best-effort）。"""
    review = node.get("review") or {}
    decisions = review.get("blocked_decisions") or []
    if len(decisions) >= _BLOCKED_DECISION_MAX:
        return {"action": "blocked", "reason": "blocked decision limit reached"}

    state = await orch._store.get_state(task_id)
    goal = state.get("confirmed_goal", "")
    prompt = _build_blocked_decision_prompt(node, error, goal, language)
    try:
        result = await orch._agent_service.spawn_subagent(
            agent_id, prompt, context_summary="", auto_approve=True,
            language=language, max_mode=False, model_tier="normal",
        )
        if not result.get("ok"):
            return {"action": "blocked", "reason": f"decision subagent failed: {result.get('error', '')}"}
        decision = _parse_blocked_decision(result.get("output", ""))
        decisions.append({
            "ts": int(_time.time()),
            "error": error,
            "action": decision.get("action"),
            "reason": decision.get("reason"),
        })
        await _update_node_review(orch._conn, node["id"], {"blocked_decisions": decisions})
        return decision
    except Exception as e:
        _log.debug("blocked step decision failed for %s: %s", node.get("id"), e, exc_info=True)
        return {"action": "blocked", "reason": str(e)}


def _build_step_output(
    node: dict,
    output: str,
    model_used: dict,
    step_role: str,
) -> dict:
    """构建结构化 step_output，供后续步骤继承与失败追溯。

    当前 v1 从子 Agent 文本输出中启发式提取文件/测试变更；
    后续可让子 Agent 直接返回结构化字段提升准确度。
    """
    return {
        "title": node.get("title", ""),
        "description": node.get("description", ""),
        "step_role": step_role,
        "output_preview": output[:LOG_PREVIEW_ERROR_LEN * 2],
        "files_created": _extract_paths(output, _FILE_CREATED_PATTERNS),
        "files_modified": _extract_paths(output, _FILE_MODIFIED_PATTERNS),
        "tests_created": _extract_paths(output, _TEST_CREATED_PATTERNS),
        "tests_passing": [],
        "tests_failing": [],
        "errors_encountered": [],
        "key_decisions": [],
        "model_used": model_used or {},
    }


def _ready_nodes_from_tree(tree: list[dict]) -> list[dict]:
    """从已加载的 tree 中提取 ready 节点（pending + dependencies 全 completed）

    避免每轮重复查 DB——execute_plan_tree 每轮只查一次 tree。
    """
    completed_ids = {n["id"] for n in tree if n["status"] == "completed"}
    ready: list[dict] = []
    for node in tree:
        if node["status"] != "pending":
            continue
        deps = node.get("dependencies", []) or []
        if all(dep_id in completed_ids for dep_id in deps):
            ready.append(node)
    return ready


async def execute_plan_tree(orch, task_id: str, agent_id: str) -> None:
    """拓扑排序 + 有限并行执行 plan_steps 树

    调度循环：
    1. 读 tree → 找 ready 节点（依赖全完成的 pending）
    2. 过滤掉 root（level 0 是目标容器，不执行；无子节点 fallback 例外）
    3. asyncio.gather + Semaphore(3) 并行执行
    4. 本轮全完成后 → 下一轮 ready 检测（新完成的节点可能解锁后续）
    5. 无更多 ready → 退出
    6. 判定最终状态：全 completed → _mark_completed；有 blocked/failed/pending → _mark_failed

    Args:
        orch: LongTaskOrchestrator 实例（复用其单节点执行逻辑 + ws_manager + conn）
        task_id: 长任务根 task id
        agent_id: 执行 agent
    """
    # Phase 87 审查 M3: 顶层 try/except 兜底——异常时标记任务 failed，避免卡 executing
    try:
        await _execute_plan_tree_impl(orch, task_id, agent_id)
    except Exception as exc:
        _log.exception("execute_plan_tree raised for task %s: %s", task_id, exc)
        try:
            await orch._mark_failed(task_id, f"executor exception: {exc}"[:200])
        except Exception:
            _log.debug("Failed to mark task %s failed after executor exception",
                       task_id, exc_info=True)


async def _execute_plan_tree_impl(orch, task_id: str, agent_id: str) -> None:
    """execute_plan_tree 的实现（被 try/except 包裹）"""
    # Phase 87 审查 M4: 启动时恢复 stale running 节点——上次崩溃/进程重启残留的
    # "running" 节点永远不会再被执行（_ready_nodes_from_tree 只挑 pending），
    # 且其下游节点因依赖未 completed 永远不 ready，导致任务卡死。重置为 pending
    # 让本轮调度重新执行它们。
    try:
        cur = await orch._conn.execute(
            "UPDATE plan_steps SET status='pending' "
            "WHERE task_id=? AND status='running'",
            (task_id,),
        )
        if cur.rowcount > 0:
            _log.info(
                "Recovered %d stale running nodes to pending for task %s",
                cur.rowcount, task_id,
            )
        await orch._conn.commit()
    except Exception:
        _log.debug("Failed to recover stale running nodes for task %s",
                   task_id, exc_info=True)

    tree = await get_plan_tree_by_task(orch._conn, task_id)
    has_children = any(n.get("parent_id") is not None for n in tree)

    # 可执行节点 = level > 0（有子树时）或全部（无子树 fallback）
    executable = [n for n in tree if n["level"] > 0 or not has_children]
    total = len(executable)
    if total == 0:
        await orch._mark_completed(task_id)
        return

    # 稳定的 step_index 映射（基于初始 tree 位置，供 WS 消息引用）
    step_indices = {n["id"]: i for i, n in enumerate(executable)}

    semaphore = asyncio.Semaphore(MAX_LONG_TASK_CONCURRENT_NODES)
    blocked_count = 0
    failed_count = 0

    while True:
        # 每轮重读 tree（状态在变）
        tree = await get_plan_tree_by_task(orch._conn, task_id)
        ready = _ready_nodes_from_tree(tree)
        if has_children:
            ready = [n for n in ready if n["level"] > 0]
        if not ready:
            break  # 无更多可执行节点

        # Phase 81: 每轮前检查 pause
        await orch._check_pause(task_id)

        # Phase 窗口翻滚: 每轮前检查主 Agent 仲裁中止 (best-effort, 不阻塞执行)
        if LONG_TASK_WINDOW_ROLLOVER_ENABLED:
            try:
                if await _wr.is_task_aborted(orch, task_id):
                    _log.info("Task %s aborted via main-agent arbitration", task_id)
                    await _wr.apply_abort(orch, task_id, "主 Agent 仲裁中止")
                    return
            except Exception:
                _log.debug("arbitration abort check failed (best-effort)", exc_info=True)

        # 并行执行本轮 ready 节点
        tasks = [
            _execute_node(orch, task_id, node, agent_id, semaphore, total, step_indices, tree)
            for node in ready
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for node, result in zip(ready, results):
            if isinstance(result, Exception):
                _log.error("Node %s execution raised: %s", node["id"], result)
                failed_count += 1
            elif result == "blocked":
                blocked_count += 1
            elif result == "failed":
                failed_count += 1
            elif result == "retry_scheduled":
                pass  # 节点已重置为 pending，下轮调度重新执行
            # completed / skipped 不计数（正常流程）

        # 长任务窗口翻滚: 每轮后混合判定翻滚 + 节流双向监督 (best-effort, 不阻塞)
        if LONG_TASK_WINDOW_ROLLOVER_ENABLED:
            try:
                completed_now = sum(
                    1 for n in tree
                    if n["status"] == "completed"
                    and (n["level"] > 0 or not has_children)
                )
                await _wr.supervise_round(
                    orch, task_id, total=total, completed_count=completed_now,
                )
            except Exception:
                _log.debug("window_rollover.supervise_round failed (best-effort)", exc_info=True)

    # 判定最终状态：检查是否有未完成的可执行节点
    final_tree = await get_plan_tree_by_task(orch._conn, task_id)
    remaining_pending = [
        n for n in final_tree
        if n["status"] == "pending" and (n["level"] > 0 or not has_children)
    ]
    # Phase 87 审查 M6: 若零 completed（全 skipped/blocked/failed），任务应 failed
    # 原逻辑只检查 blocked/failed/remaining_pending，全 skipped 时误标 completed
    completed_count = sum(
        1 for n in final_tree
        if n["status"] == "completed" and (n["level"] > 0 or not has_children)
    )

    if blocked_count or failed_count or remaining_pending or completed_count == 0:
        reasons = []
        if blocked_count:
            reasons.append(f"{blocked_count} blocked")
        if failed_count:
            reasons.append(f"{failed_count} failed")
        if remaining_pending:
            reasons.append(f"{len(remaining_pending)} pending (dependency blocked)")
        if completed_count == 0 and not (blocked_count or failed_count or remaining_pending):
            # 全 skipped（零 completed 且无其他失败）——零工作完成
            reasons.append("all steps skipped by FinalReview")
        await orch._mark_failed(task_id, f"Steps: {', '.join(reasons)}")
    else:
        await orch._mark_completed(task_id)


async def _execute_node(
    orch, task_id, node, agent_id, semaphore, total, step_indices, tree,
) -> str:
    """单节点执行（信号量限并发）

    Phase 87 审查 M5: standards_lookup（含 LLM 调用，慢）移出信号量——
    原在 _execute_node_core 内调用，导致 blocked 节点的查询期间仍占用
    并发槽位，阻塞其他 ready 节点。现改为信号量释放后再 best-effort 触发。

    Returns:
        "completed" / "blocked" / "failed" / "skipped" / "retry_scheduled"
    """
    async with semaphore:
        status, err = await _execute_node_inner(
            orch, task_id, node, agent_id, total, step_indices, tree,
        )
    # M5: standards_lookup 移出信号量——blocked 节点的查询不再占用并发槽位
    if status == "blocked" and err:
        try:
            await orch._standards_lookup_hook(task_id, node, err, agent_id)
        except Exception:
            _log.debug(
                "standards_lookup_hook failed for node %s (best-effort)",
                node.get("id"), exc_info=True,
            )
        # Phase 108: blocked 步骤 AI 决策（skip / retry / split）
        state = await orch._store.get_state(task_id)
        language = state.get("language", "zh")
        decision = await _decide_blocked_step_action(
            orch, task_id, node, err, agent_id, language,
        )
        action = decision.get("action")
        step_id = node["id"]
        step_index = step_indices.get(step_id, 0)
        if action == "skip":
            note = f"AI decision skip: {decision.get('reason', '')}"
            await update_plan_node_status(
                orch._conn, step_id, "completed", output=note,
                expected_from="failed",
            )
            await orch._ws_manager.safe_send(task_id, {
                "type": "long_task_step_done",
                "task_id": task_id, "step_id": step_id,
                "step_index": step_index, "total_steps": total,
                "output": note,
            })
            return "completed"
        if action == "retry":
            await update_plan_node_status(
                orch._conn, step_id, "pending", expected_from="failed",
            )
            return "retry_scheduled"
        if action == "split":
            sub_steps = decision.get("new_sub_steps") or []
            if await _replace_node_with_children(orch._conn, task_id, node, sub_steps):
                await update_plan_node_status(
                    orch._conn, step_id, "skipped", expected_from="failed",
                )
                return "retry_scheduled"
    return status


async def _execute_node_inner(
    orch, task_id, node, agent_id, total, step_indices, tree,
) -> tuple[str, str]:
    """实际单节点执行逻辑——从 orchestrator._execute_steps 提取并升级

    含：pause 检查 / 注入约束 / pre_execution review / spawn_subagent（带重试）/
        adversarial challenge / post_execution review / WS 通知

    Returns:
        (status, err) —— status: "completed"/"blocked"/"failed"/"skipped"
        err: blocked 时的错误字符串（供 _execute_node 在信号量外调 standards_lookup）
    """
    step_id = node["id"]
    step_index = step_indices.get(step_id, 0)

    await orch._check_pause(task_id)
    await update_plan_node_status(orch._conn, step_id, "running", expected_from="pending")
    await orch._ws_manager.safe_send(task_id, {
        "type": "long_task_step_start",
        "task_id": task_id,
        "step_id": step_id,
        "step_index": step_index,
        "total_steps": total,
        "step_title": node["title"],
    })

    # Phase 87 审查 C2: 顶层 try/except 兜底——核心逻辑异常时回写 failed，
    # 避免节点永久卡 "running"（DB 写失败/_build_step_prompt KeyError 等）
    try:
        return await _execute_node_core(
            orch, task_id, node, agent_id, total, step_indices, tree,
            step_id, step_index,
        )
    except Exception as exc:
        _log.exception("Node %s raised unhandled exception: %s", step_id, exc)
        # best-effort 回写 failed（避免回写本身抛异常）
        try:
            await update_plan_node_status(
                orch._conn, step_id, "failed",
                output=f"Unhandled exception: {exc}"[:500],
                expected_from="running",
            )
        except Exception:
            _log.debug("Failed to mark node %s failed after exception",
                       step_id, exc_info=True)
        try:
            await orch._ws_manager.safe_send(task_id, {
                "type": "long_task_step_fail",
                "task_id": task_id, "step_id": step_id,
                "step_index": step_index, "total_steps": total,
                "error": f"Unhandled exception: {exc}"[:200],
            })
        except Exception:
            pass
        return "failed", ""


async def _execute_node_core(
    orch, task_id, node, agent_id, total, step_indices, tree,
    step_id, step_index,
) -> tuple[str, str]:
    """节点核心执行逻辑（_execute_node_inner 的核心，被 try/except 包裹）

    含：prompt 构建 / 注入约束 / pre_execution review / spawn_subagent（带重试）/
        adversarial challenge / post_execution review / WS 通知

    Phase 87 审查 M5: standards_lookup 移出此函数（移到 _execute_node 信号量外），
    避免 LLM 调用占用并发槽位阻塞其他 ready 节点。

    Returns:
        (status, err) —— status: "completed"/"blocked"/"failed"/"skipped"
        err: blocked 时的错误字符串（供信号量外 standards_lookup 使用）
    """
    prompt = orch._build_step_prompt(node)
    # Phase 81: 取出注入的消息（非阻塞），拼到 prompt 末尾
    # 消息可能是约束、修改要求、讨论问题或其他指令——通用化提示
    constraint = await orch._drain_inject_queue(task_id)
    if constraint:
        prompt = (
            f"{prompt}\n\n"
            "═══ 用户在任务执行期间插入的消息 ═══\n"
            f"{constraint}\n"
            "═══ 说明：以上是用户在你执行任务过程中插入的消息，可能是约束、修改要求、"
            "讨论问题或其他指令。请在完成当前步骤的基础上处理这条消息——不要因为"
            "这条消息而中断或放弃当前正在进行的任务，但也不要忽略这条消息。如消息"
            "与当前步骤冲突，请说明冲突并给出处理建议。 ═══"
        )

    # Phase 83: pre_execution 终审——halt 则跳过该步
    _state = await orch._store.get_state(task_id)
    confirmed_goal = _state.get("confirmed_goal", "")
    language = _state.get("language", "zh")
    pre_review = await orch._final_review_pre(task_id, node, prompt, confirmed_goal)
    if pre_review and pre_review.get("action") == "halt":
        _halt_reason = pre_review.get("reason", "halted by FinalReview")
        await update_plan_node_status(
            orch._conn, step_id, "skipped",
            output=f"FinalReview halt: {_halt_reason}",
            expected_from="running",
        )
        await orch._ws_manager.safe_send(task_id, {
            "type": "long_task_step_fail", "task_id": task_id, "step_id": step_id,
            "step_index": step_index, "total_steps": total,
            "error": f"FinalReview halt: {_halt_reason}",
        })
        await orch._write_step_review(step_id, "pre_execution", pre_review)
        return "skipped", ""

    # Phase 87: 依赖输出作为上下文（替代线性的 prev_outputs）
    context_summary = _merge_dependency_outputs(node, tree)

    # 长任务窗口翻滚 · 增强2(安全版): 翻滚后把每个 Worker 节点锚定回原始目标摘要,
    # 并显式声明 Worker 身份(执行窗口承接者)。
    # 关键约束: 仅声明身份、绝不替换 spawn_subagent 的 agent_id——"worker" 不是可 spawn 的
    # adapter(见 agent_lifecycle.run_agent: adapters.get("worker") 为 None → "Agent worker
    # not found" 让每个节点失败)。执行体保持默认/可路由 agent; 身份只落在对话上下文里。
    _rollover_state = (_state or {}).get("rollover") or {}
    if _rollover_state.get("current_generation") and _rollover_state.get("goal_summary"):
        context_summary = (
            f"[窗口翻滚·Worker 锚定] 你正以 Worker(执行者) 身份在第 "
            f"{_rollover_state['current_generation']} 代执行窗口承接任务; 原始会话已翻转成 "
            f"Supervisor(监督者) 与其双向交叉验证。始终以原始目标为锚, 不要因局部上下文偏离:\n"
            f"{_rollover_state['goal_summary']}\n\n"
            + context_summary
        )

    # Phase 108+: 长任务子步骤 tier 推断 + MAX 模式 + 能力等级透传
    step_role = infer_step_tier(f"{node.get('title', '')} {node.get('description', '')}")
    max_mode = bool(_state.get("max_mode", False))
    capability_level = int(_state.get("capability_level", 2 if max_mode else 1))

    # Phase 87: 带重试的 spawn_subagent
    result = await _retry_spawn(
        orch, agent_id, prompt, context_summary, step_id, language,
        max_mode=max_mode, long_task_step_role=step_role,
        capability_level=capability_level,
    )

    if result.get("ok"):
        output = result.get("output", "")
        # Phase 108: 构建结构化 step_output（用于后续步骤继承 + 失败追溯）
        step_output = _build_step_output(
            node, output, result.get("model_used", {}), step_role,
        )

        # Phase 84 (c): 输出合并前对抗验证——被反驳则跳过不合并
        challenge = await orch._adversarial_challenge(
            task_id, "output_merge", output,
            confirmed_goal, agent_id, step_id=step_id,
        )
        if challenge and challenge.get("action") == "replan":
            _log.warning(
                "Output refuted by adversarial review for step %s: %s",
                step_id, challenge.get("reason"),
            )
            await update_plan_node_status(
                orch._conn, step_id, "failed",
                output=f"Adversarial refuted: {challenge.get('reason', '')}",
                step_output=step_output,
                expected_from="running",
            )
            await orch._write_adversarial_review(step_id, "output_merge", challenge)
            await orch._ws_manager.safe_send(task_id, {
                "type": "long_task_step_fail",
                "task_id": task_id, "step_id": step_id,
                "step_index": step_index, "total_steps": total,
                "error": f"Adversarial: {challenge.get('reason', '')}",
            })
            # Phase 87 审查 C1: 对抗反驳时 DB 已标 failed，返回值须一致为 "failed"
            # （原返回 "completed" 导致 failed_count 漏计，叶子节点反驳后任务误标 completed）
            return "failed", ""

        await update_plan_node_status(
            orch._conn, step_id, "completed", output=output,
            step_output=step_output,
            expected_from="running",
        )

        # 长任务窗口翻滚 · 增强1: 翻滚后把 Worker 节点产出写回 worker_session_id 会话,
        # 使其成为"活"的执行产物(审计/可视化), 不影响执行主链路。
        # conversations.agent_id 仅为标签, 不要求是可 spawn 的 adapter, 写 WORKER_AGENT_ID 安全。
        # (注意: "worker" 非可 spawn adapter, 故身份仅作标签写入, 不替换 spawn 的 agent_id。)
        if _rollover_state.get("current_generation"):
            _worker_sid = _rollover_state.get("worker_session_id")
            if _worker_sid:
                try:
                    await insert_conversation(
                        orch._conn, _worker_sid, "assistant", output,
                        agent_id=WORKER_AGENT_ID,
                    )
                    await orch._conn.commit()
                except Exception:
                    _log.debug(
                        "worker_session write-back failed for node %s (best-effort)",
                        step_id, exc_info=True,
                    )

        # Phase 83: post_execution 终审（只读）
        await orch._final_review_post(task_id, node, output, confirmed_goal)
        await orch._ws_manager.safe_send(task_id, {
            "type": "long_task_step_done",
            "task_id": task_id, "step_id": step_id,
            "step_index": step_index, "total_steps": total,
            "output": output,
        })
        # Phase 86: 派生 task_lifecycle.step_done
        # Task 6.15: 截断附溯源指针，完整 output 在 plan_steps 节点 {step_id}
        await orch._ws_manager.safe_send(task_id, {
            "type": "task_lifecycle", "subtype": "step_done",
            "task_id": task_id,
            "step_index": step_index, "total_steps": total,
            "output_preview": truncate_with_pointer(output, LOG_PREVIEW_CHARS, step_id),
        })
        return "completed", ""
    else:
        err = result.get("error", "unknown error")
        await update_plan_node_status(
            orch._conn, step_id, "failed", output=err,
            expected_from="running",
        )
        await orch._ws_manager.safe_send(task_id, {
            "type": "long_task_step_fail",
            "task_id": task_id, "step_id": step_id,
            "step_index": step_index, "total_steps": total,
            "error": err,
        })
        # Phase 87 审查 M5: standards_lookup 移至 _execute_node（信号量外）
        # Phase 87: 不可重试错误（重试已耗尽）→ blocked
        return "blocked", err


async def _retry_spawn(
    orch, agent_id, prompt, context_summary, step_id, language: str = "zh",
    max_mode: bool = False, long_task_step_role: str = "",
    capability_level: int = 1,
) -> dict:
    """带重试 + 模型/tier 自动升级的 spawn_subagent 调用。

    - 可重试错误：同 tier 指数退避重试 _MAX_RETRIES 次，耗尽后尝试升级 tier
    - 模型不可用：立即升级到更高 tier
    - 业务错误：直接返回失败
    - 每次重试注入 previous_error / model / tier / fix_hint 上下文
    """
    # 起始 tier：L2=max；L0=轻量(角色映射 lite 保持 lite，其余压到 normal)；L1=按角色默认
    if max_mode:
        current_tier = "max"
    elif capability_level == 0:
        role_tier = _resolve_step_tier_from_role(long_task_step_role)
        current_tier = "lite" if role_tier == "lite" else "normal"
    else:
        current_tier = _resolve_step_tier_from_role(long_task_step_role)
    last_result: dict = {"ok": False, "error": "no attempt made"}
    previous_model: dict = {}
    prompt_to_send = prompt
    max_attempts = _MAX_RETRIES + 1 + 2  # 初始 + 重试 + 最多 2 次 tier 升级

    for attempt in range(max_attempts):
        if attempt > 0:
            prompt_to_send = _append_retry_context(
                prompt, attempt, _MAX_RETRIES,
                last_result.get("error", ""), previous_model,
                current_tier, language,
            )
        try:
            result = await orch._agent_service.spawn_subagent(
                agent_id, prompt_to_send, context_summary=context_summary,
                auto_approve=True, language=language,
                max_mode=max_mode, long_task_step_role=long_task_step_role,
                model_tier=current_tier,
            )
        except Exception as e:
            result = {"ok": False, "error": f"spawn_subagent exception: {e}"}

        if result.get("ok"):
            return result

        last_result = result
        err = result.get("error", "")
        previous_model = result.get("model_used") or previous_model
        category = _classify_failure(err)

        if category == "retriable" and attempt < _MAX_RETRIES:
            delay = _RETRY_BASE_DELAY * (2 ** attempt)
            _log.warning(
                "Step %s attempt %d failed (retriable: %s), retrying in %.1fs",
                step_id, attempt + 1, err[:LOG_PREVIEW_CMD_LEN], delay,
            )
            await asyncio.sleep(delay)
            continue
        if category == "business":
            return last_result

        # 可重试次数耗尽 或 模型不可用 -> 尝试升级 tier
        next_tier = _next_tier(current_tier, max_mode, capability_level)
        if next_tier:
            _log.warning(
                "Step %s escalating tier %s -> %s after %s error: %s",
                step_id, current_tier, next_tier, category, err[:LOG_PREVIEW_CMD_LEN],
            )
            current_tier = next_tier
            continue
        return last_result

    return last_result
