"""services/long_task/window_rollover.py —— 长任务窗口翻滚引擎 (长任务优化)

解决"长任务靠压缩→跑几小时必然跑偏"的根因:
- 当前漂移来自 (a) 每步子 agent 的 3 级有损压缩, 与 (b) plan 节点间多跳摘要继承
  (节点传递结构化摘要而非原始对话, 每跳渐进失真)。
- 本模块在进度 ~60% 的"翻滚点"开一个新 Worker 窗口承接剩余任务, 原对话翻转成
  Supervisor 监督者, 二者双向交叉验证, 并都与主 Yuncat Agent (agent_id=yuncat) 联动。

设计要点 (与已批准方案一致):
- 触发: AI 自判(~60% 区间) + token 占用兜底 (混合)。
- Worker 起始上下文: 目标摘要 + 近期窗口 (轻量, 不继承被压缩/污染的全量历史)。
- 闭环: 翻滚 + supervisor/worker 身份 (见 prompts/agent_identity.py) + 双向交叉验证 + 主 Agent 联动。

为避免循环依赖, 本模块不 import orchestrator 类, 而是把 orch 当作鸭子类型
(需实现 get_state / set_state / inject_constraint)。DB 访问走 repositories.conversations_repo,
便于在测试中 monkeypatch。
"""
import json as _json
import logging
import re
import time as _time
import uuid

from config import WORKER_AGENT_ID
from repositories.conversations_repo import get_conversation, insert_conversation

_log = logging.getLogger("yuncat.window_rollover")

# ── 翻滚触发参数 ──
ROLLOVER_TRIGGER_PROGRESS = 0.60     # AI 自判的目标翻滚区间中心
ROLLOVER_PROGRESS_FLOOR = 0.35       # AI 信号触发的最低进度 (避免过早翻滚)
ROLLOVER_PROGRESS_MIN = 0.55         # 纯进度触发下限 (区间内才触发, 防抖)
ROLLOVER_PROGRESS_MAX = 0.72         # 纯进度触发上限
ROLLOVER_TOKEN_FALLBACK = 0.85       # token 占用兜底阈值 (无 AI 信号时的代理指标)
ROLLOVER_RECENT_WINDOW = 12          # Worker 承接的近期消息条数
MAX_ROLLOVER_GENERATIONS = 6         # 单任务最多翻滚代数 (防无限翻滚)

# Worker 起始上下文里的"目标摘要"系统种子前缀
ROLLOVER_SEED_PREFIX = (
    "【窗口翻滚·目标摘要】你是本长任务的新执行窗口 (Worker)。以下是原始任务目标摘要, "
    "请据此继续完成尚未完成的剩余部分。不要因为局部上下文而偏离该原始意图:\n\n"
)


# ═══════════════════════════════════════════════════════════════
# 1. 翻滚判定 (纯函数, 可单测)
# ═══════════════════════════════════════════════════════════════

def should_rollover(
    *,
    progress_fraction: float,
    token_occupancy: float | None = None,
    ai_signal: bool = False,
    already_rolled_in_window: int = 0,
    max_generations: int | None = None,
) -> tuple[bool, str, str]:
    """混合判定是否到达翻滚点。

    Args:
        progress_fraction: 已完成节点 / 总节点 的进度 (0~1)。
        token_occupancy: 当前上下文 token 占用 / 窗口上限 (0~1), 可选。
        ai_signal: 执行子 agent 自报"上下文将饱和, 建议翻滚"。
        already_rolled_in_window: 本窗口已翻滚代数 (链式翻滚累加)。
        max_generations: 单任务最大翻滚代数上限。

    Returns:
        (should, reason, signal) — signal ∈ {cap, ai, progress, token, none}。
    """
    # 1. 上限保护: 链式翻滚不能超过代数上限
    if max_generations is None:
        # 2026-09-17: 上限按能力档取（默认 6 / MAX 12）
        from services.capability_limits import current_limits

        max_generations = current_limits().rollover_generations
    if already_rolled_in_window >= max_generations:
        return (False, f"已达最大翻滚代数 {max_generations}", "cap")

    # 2. AI 自判: 子 agent 主动报告上下文饱和, 且进度已过最低地板
    if ai_signal and progress_fraction >= ROLLOVER_PROGRESS_FLOOR:
        return (True, "执行窗口自报上下文将饱和, 建议翻滚", "ai")

    # 3. 进度区间: 落在 ~60% 翻滚带内 (带滞回, 防抖)
    if ROLLOVER_PROGRESS_MIN <= progress_fraction <= ROLLOVER_PROGRESS_MAX:
        return (True, f"进度 {progress_fraction:.0%} 落入翻滚带", "progress")

    # 4. token 兜底: 无 AI 信号时, 用 token 占用做代理指标
    if (
        token_occupancy is not None
        and token_occupancy >= ROLLOVER_TOKEN_FALLBACK
        and progress_fraction >= ROLLOVER_PROGRESS_FLOOR
    ):
        return (True, f"token 占用 {token_occupancy:.0%} 触发兜底翻滚", "token")

    # 5. 未达任何阈值
    return (False, "未达翻滚阈值", "none")


# ═══════════════════════════════════════════════════════════════
# 2. 构建 Worker 种子 (目标摘要 + 近期窗口)
# ═══════════════════════════════════════════════════════════════

def build_worker_seed_message(goal_summary: str, generation: int) -> str:
    """构建注入 Worker 会话的"目标摘要"系统种子文案。"""
    return f"{ROLLOVER_SEED_PREFIX}【第 {generation} 代执行窗口】\n{goal_summary or '(目标摘要缺失)'}"


async def compute_rollover_seed(
    conn, supervisor_session_id: str, *, recent_window_n: int = ROLLOVER_RECENT_WINDOW,
) -> dict:
    """从 Supervisor(原)会话抽取近期窗口, 作为 Worker 的起始上下文。

    Returns: {"recent_messages": [...], "recent_text": str}
    """
    recent = await get_conversation(conn, supervisor_session_id, limit=recent_window_n)
    recent_text = "\n".join(
        f"[{m.get('role', '?')}]: {m.get('content', '')}" for m in recent
    )
    return {"recent_messages": recent, "recent_text": recent_text}


# ═══════════════════════════════════════════════════════════════
# 3. 执行翻滚: 开新 Worker 会话 + 翻转 Supervisor + 记录状态
# ═══════════════════════════════════════════════════════════════

async def rollover_window(
    conn,
    orch,
    task_id: str,
    *,
    supervisor_session_id: str,
    progress_fraction: float,
    goal_summary: str | None = None,
    recent_window_n: int = ROLLOVER_RECENT_WINDOW,
) -> tuple[str, dict]:
    """开一个新 Worker 会话承接剩余任务, 原会话翻转成 Supervisor。

    - 不继承被压缩/污染的全量历史, Worker 只拿"目标摘要 + 近期窗口"(轻量)。
    - 翻滚关系写入 task_state.long_task.rollover, 供 executor / 主 Agent 读取。

    Args:
        conn: aiosqlite 连接。
        orch: 鸭子类型 LongTaskOrchestrator (get_state / set_state / inject_constraint)。
        task_id: 长任务 id。
        supervisor_session_id: 原(即将翻转成监督者)会话 id。
        progress_fraction: 翻滚时的进度。
        goal_summary: 原始目标摘要; 缺省时从 task_state 取 confirmed_goal/original_prompt。
        recent_window_n: Worker 承接的近期消息条数。

    Returns:
        (worker_session_id, rollover_meta)
    """
    # 1. 解析目标摘要 (优先入参, 否则从任务状态取)
    if not goal_summary:
        state = await orch.get_state(task_id)
        goal_summary = state.get("confirmed_goal") or state.get("original_prompt") or ""

    # 2. 建新 Worker 会话 + 注入目标摘要种子
    worker_session_id = str(uuid.uuid4())
    await insert_conversation(
        conn, worker_session_id, "system",
        build_worker_seed_message(goal_summary, generation=1),
        agent_id=WORKER_AGENT_ID,
    )

    # 3. 复制近期窗口到 Worker (轻量上下文, 不含被污染的早期历史)
    seed = await compute_rollover_seed(
        conn, supervisor_session_id, recent_window_n=recent_window_n,
    )
    for m in seed["recent_messages"]:
        await insert_conversation(
            conn, worker_session_id, m.get("role", "user"), m.get("content", ""),
            agent_id=m.get("agent_id"),
        )
    await conn.commit()

    # 4. 读取既有翻滚状态, 计算新一代索引
    state = await orch.get_state(task_id)
    prev = state.get("rollover", {}) or {}
    history = prev.get("history", []) or []
    generation = len(history) + 1

    # 5. 记录翻滚关系 (supervisor=原会话, worker=新会话)
    rollover_meta = {
        "current_generation": generation,
        "supervisor_session_id": supervisor_session_id,
        "worker_session_id": worker_session_id,
        "goal_summary": goal_summary,
        "progress_at_rollover": round(progress_fraction, 4),
        "ts": int(_time.time()),
        "history": history + [{
            "generation": generation,
            "supervisor_session_id": supervisor_session_id,
            "worker_session_id": worker_session_id,
            "progress_at_rollover": round(progress_fraction, 4),
            "ts": int(_time.time()),
        }],
    }
    await orch.set_state(task_id, rollover=rollover_meta)
    _log.info(
        "Window rollover gen=%d for task %s: supervisor=%s -> worker=%s (progress=%.0f%%)",
        generation, task_id, supervisor_session_id, worker_session_id,
        progress_fraction * 100,
    )
    return worker_session_id, rollover_meta


# ═══════════════════════════════════════════════════════════════
# 4. Supervisor 评审 + 双向交叉验证 (LLM 注入式, 可单测)
# ═══════════════════════════════════════════════════════════════

def build_supervisor_critique_prompt(*, worker_artifact: str, original_intent: str) -> str:
    """构建 Supervisor 评审 Worker 产物的 prompt。"""
    return (
        "你是长任务监督者(Supervisor)。请严格对照【原始任务意图】评审【Worker 产物】,\n"
        "判断 Worker 是否偏离原始意图, 并给出纠偏约束。\n\n"
        f"【原始任务意图】\n{original_intent}\n\n"
        f"【Worker 产物】\n{worker_artifact}\n\n"
        "只输出 JSON, 不要解释:\n"
        '{"pass": true|false, "score": 0-100, "issues": ["..."], '
        '"corrective_constraint": "若偏离, 写一条要回写 Worker 的约束; 否则空字符串"}'
    )


def build_cross_validate_prompt(
    *, supervisor_summary: str, worker_summary: str, original_intent: str,
) -> str:
    """构建双向交叉验证 prompt: 监督者复述的目标 vs Worker 理解 vs 原始意图。"""
    return (
        "你是长任务双向交叉验证仲裁。请比对三方是否一致:\n"
        "1)【原始意图】 2)【Supervisor 复述的目标】 3)【Worker 理解的目标】。\n"
        "双向校验: 既查 Worker 是否偏离, 也查 Supervisor 自身复述是否已偏离原始意图。\n\n"
        f"【原始意图】\n{original_intent}\n\n"
        f"【Supervisor 复述的目标】\n{supervisor_summary}\n\n"
        f"【Worker 理解的目标】\n{worker_summary}\n\n"
        "只输出 JSON, 不要解释:\n"
        '{"aligned": true|false, "divergences": ["..."], '
        '"verdict": "ok|worker_drift|supervisor_drift|both_drift"}'
    )


def _safe_json_loads(text: str) -> dict:
    """尽力从 LLM 文本中解析 JSON (容错: 去 ```json 围栏 / 取首个 {...})。"""
    if not text:
        return {}
    s = text.strip()
    # 去除 markdown 代码围栏
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return _json.loads(s)
    except (_json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            try:
                return _json.loads(m.group(0))
            except (_json.JSONDecodeError, TypeError):
                return {}
    return {}


def _parse_critique_response(text: str, *, task_id: str = "") -> dict:
    """解析 Supervisor 评审响应为结构化 dict (best-effort 容错)。"""
    data = _safe_json_loads(text)
    if not data:
        return {
            "pass": True, "score": 70, "issues": [],
            "corrective_constraint": "", "note": "解析失败, 保守放行",
            "task_id": task_id, "ts": int(_time.time()),
        }
    return {
        "pass": bool(data.get("pass", True)),
        "score": int(data.get("score", 70)),
        "issues": list(data.get("issues", [])),
        "corrective_constraint": str(data.get("corrective_constraint", "") or ""),
        "task_id": task_id, "ts": int(_time.time()),
    }


def _parse_crossval_response(text: str, *, task_id: str = "") -> dict:
    """解析双向交叉验证响应 (best-effort 容错)。"""
    data = _safe_json_loads(text)
    if not data:
        return {
            "aligned": True, "divergences": [], "verdict": "ok",
            "note": "解析失败, 保守判为对齐", "task_id": task_id, "ts": int(_time.time()),
        }
    verdict = str(data.get("verdict", "ok"))
    if "aligned" not in data:
        data["aligned"] = verdict == "ok"
    return {
        "aligned": bool(data.get("aligned", True)),
        "divergences": list(data.get("divergences", [])),
        "verdict": verdict,
        "task_id": task_id, "ts": int(_time.time()),
    }


async def supervisor_critique(
    llm_call, *, worker_artifact: str, original_intent: str, task_id: str = "",
) -> dict:
    """Supervisor 评审 Worker 产物, 返回结构化判定 (含纠偏约束)。

    llm_call: 注入式 Callable[[str], Awaitable[str]] —— #52 接入真实 adapter.invoke。
    无 LLM 时返回保守放行结构。
    """
    prompt = build_supervisor_critique_prompt(
        worker_artifact=worker_artifact, original_intent=original_intent,
    )
    try:
        text = await llm_call(prompt) if llm_call else ""
    except Exception:  # best-effort, 不阻塞主流程
        _log.exception("supervisor_critique: llm_call failed for task %s", task_id)
        text = ""
    return _parse_critique_response(text, task_id=task_id)


async def cross_validate(
    llm_call,
    *,
    supervisor_summary: str,
    worker_summary: str,
    original_intent: str,
    task_id: str = "",
) -> dict:
    """双向交叉验证: Supervisor 复述 vs Worker 理解 vs 原始意图。

    llm_call: 注入式 Callable[[str], Awaitable[str]]。
    """
    prompt = build_cross_validate_prompt(
        supervisor_summary=supervisor_summary,
        worker_summary=worker_summary,
        original_intent=original_intent,
    )
    try:
        text = await llm_call(prompt) if llm_call else ""
    except Exception:
        _log.exception("cross_validate: llm_call failed for task %s", task_id)
        text = ""
    return _parse_crossval_response(text, task_id=task_id)


async def write_back_constraint(orch, task_id: str, constraint: str) -> None:
    """把 Supervisor 的纠偏约束回写到当前执行上下文 (广播给 running 节点)。"""
    if constraint:
        await orch.inject_constraint(task_id, constraint)
        _log.info("Supervisor constraint written back to task %s: %s",
                  task_id, constraint[:120])


# ═══════════════════════════════════════════════════════════════
# 5. 编排集成辅助 (executor 调用) — DB/WS/LLM 适配 + 每轮监督入口
# ═══════════════════════════════════════════════════════════════

async def _fetch_task_session_id(orch, task_id: str) -> str | None:
    """取任务所属的主会话 id (即首个翻滚前的 Supervisor 会话)。"""
    try:
        cur = await orch._conn.execute(
            "SELECT session_id FROM tasks WHERE id=?", (task_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None


async def _fetch_latest_completed_output(orch, task_id: str) -> str:
    """取最近一个 completed 节点的 output, 作为监督的 Worker 产物样本。"""
    try:
        cur = await orch._conn.execute(
            "SELECT output FROM plan_steps WHERE task_id=? AND status='completed' "
            "AND output IS NOT NULL AND output != '' ORDER BY updated_at DESC LIMIT 1",
            (task_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


async def _get_llm_call():
    """复用终审 Agent 的 hermes adapter 作为监督 LLM (best-effort)。"""
    try:
        from services.long_task.final_review_agent import get_final_review_agent
        agent = get_final_review_agent()
        return agent._invoke_llm if agent else None
    except Exception:
        return None


async def _safe_ws(orch, task_id: str, payload: dict) -> None:
    """安全推送 WS (best-effort, 异常忽略)。"""
    try:
        await orch._ws_manager.safe_send(task_id, payload)
    except Exception:
        _log.debug("ws send failed for task %s: %s", task_id, payload.get("type"))


async def supervise_round(
    orch, task_id: str, *, total: int, completed_count: int,
    ai_signal: bool = False, supervision_granularity: float = 0.15,
) -> dict:
    """每轮调度后调用: 混合判定翻滚 + 节流双向监督。

    这是 #52 在 executor 主循环里的唯一入口, 全程 best-effort, 永不抛异常阻断执行。

    - 进度落入翻滚带 / AI 自报饱和 / token 兜底 → 开新 Worker 窗口 + 翻转 Supervisor。
    - 否则按 supervision_granularity 节流跑一轮监督 (critique → 约束回写 → 交叉验证)。
    - 升级(escalate)经 WS 上报主 Yuncat Agent 仲裁。
    """
    try:
        state = await orch.get_state(task_id)
    except Exception:
        return {"ok": False, "reason": "get_state failed"}
    rollover = state.get("rollover") or {}
    gen = rollover.get("current_generation", 0)
    progress = (completed_count / total) if total else 0.0
    goal_summary = (
        rollover.get("goal_summary")
        or state.get("confirmed_goal")
        or state.get("original_prompt")
        or ""
    )

    should, reason, sig = should_rollover(
        progress_fraction=progress, ai_signal=ai_signal,
        already_rolled_in_window=gen,
    )

    if should:
        supervisor_session_id = (
            rollover.get("supervisor_session_id")
            or await _fetch_task_session_id(orch, task_id)
        )
        worker_session_id, meta = await rollover_window(
            orch._conn, orch, task_id,
            supervisor_session_id=supervisor_session_id,
            progress_fraction=progress, goal_summary=goal_summary,
        )
        await _safe_ws(orch, task_id, {
            "type": "long_task_window_rollover",
            "task_id": task_id,
            "generation": meta["current_generation"],
            "supervisor_session_id": supervisor_session_id,
            "worker_session_id": worker_session_id,
            "progress": round(progress, 4),
            "reason": reason,
        })
        # 翻滚后立即监督一轮 (以最新产出为 Worker 产物)
        latest = await _fetch_latest_completed_output(orch, task_id)
        await _supervise_now(orch, task_id, latest, goal_summary)
        return {
            "ok": True, "rolled": True, "progress": progress,
            "signal": sig, "worker_session_id": worker_session_id,
        }

    # 未翻滚: 节流监督
    last = rollover.get("last_supervision_progress", 0.0)
    if (progress - last) >= supervision_granularity:
        latest = await _fetch_latest_completed_output(orch, task_id)
        await _supervise_now(orch, task_id, latest, goal_summary)
        rollover["last_supervision_progress"] = progress
        try:
            await orch.set_state(task_id, rollover=rollover)
        except Exception:
            _log.warning("persist supervision progress failed for %s", task_id, exc_info=True)
    return {"ok": True, "rolled": False, "progress": progress, "signal": sig}


async def _supervise_now(orch, task_id: str, latest_output: str, goal_summary: str) -> dict | None:
    """跑一轮监督闭环, 并把结果 (含升级) 经 WS 上报主 Agent。"""
    if not latest_output:
        return None
    llm_call = await _get_llm_call()
    if not llm_call:
        return None
    try:
        report = await run_supervision_cycle(
            llm_call, orch, task_id,
            worker_artifact=latest_output,
            original_intent=goal_summary or "(原始目标缺失)",
            supervisor_summary=goal_summary or None,
            worker_summary=latest_output or None,
        )
    except Exception:
        _log.exception("supervise_round: supervision cycle failed for %s", task_id)
        return None

    await _safe_ws(orch, task_id, {
        "type": "long_task_supervision",
        "task_id": task_id,
        "action": report["action"],
        "score": report["critique"]["score"],
        "constraint": report["constraint"],
        "verdict": (report["crossval"] or {}).get("verdict"),
        "ts": report["ts"],
    })

    if report["action"] == "escalate":
        # 主 Agent 联动: 监督者/Worker 偏离 → 上报主 Yuncat Agent 仲裁
        try:
            st = await orch.get_state(task_id)
            rl = st.get("rollover", {}) or {}
            rl["pending_arbitration"] = True
            rl["last_escalation"] = {
                "ts": report["ts"],
                "verdict": (report["crossval"] or {}).get("verdict"),
                "issues": report["critique"].get("issues", []),
            }
            await orch.set_state(task_id, rollover=rl)
        except Exception:
            _log.warning("persist pending_arbitration failed for %s", task_id, exc_info=True)
        await _safe_ws(orch, task_id, {
            "type": "long_task_supervisor_escalation",
            "task_id": task_id,
            "reason": "监督者或 Worker 偏离原始意图, 需主 Agent 仲裁/重新锚定",
            "critique": report["critique"],
            "crossval": report["crossval"],
        })
    return report


async def run_supervision_cycle(
    llm_call, orch, task_id: str, *,
    worker_artifact: str,
    original_intent: str,
    supervisor_summary: str | None = None,
    worker_summary: str | None = None,
    escalate_threshold: int = 50,
) -> dict:
    """监督闭环: Supervisor 评审 → 约束回写 + 双向交叉验证 → 行动建议。

    这是 #51 的"闭环"核心。给定 Worker 的一个里程碑产物:

    1. supervisor_critique: 对照原始意图评审, 给出是否通过 + 纠偏约束。
    2. 若不通过且含约束 → write_back_constraint 把约束广播回 Worker 的 running 节点。
    3. cross_validate (双向): 既查 Worker 是否偏离, 也查 Supervisor 自身复述是否
       已偏离原始意图。若监督者自己跑偏 (supervisor_drift/both_drift) → 升级上报主 Agent。
    4. 汇总为 action: proceed(继续) / constrain(已回写约束, Worker 下一轮纠正) /
       escalate(需主 Yuncat Agent 仲裁/重新锚定)。

    Args:
        llm_call: 注入式 LLM Callable[[str], Awaitable[str]]。
        orch: 鸭子类型 orchestrator (inject_constraint 等)。
        worker_artifact: Worker 本里程碑的产物 / 阶段摘要。
        original_intent: 原始任务目标 (来自 rollover.goal_summary)。
        supervisor_summary / worker_summary: 双向验证所需的三方目标复述 (可选)。
        escalate_threshold: 评审分数低于此值直接升级上报。
    """
    critique = await supervisor_critique(
        llm_call, worker_artifact=worker_artifact,
        original_intent=original_intent, task_id=task_id,
    )

    # 1+2. 约束回写
    constraint = ""
    if (not critique["pass"]) and critique.get("corrective_constraint"):
        constraint = critique["corrective_constraint"]
        await write_back_constraint(orch, task_id, constraint)

    # 3. 双向交叉验证
    cross = None
    action = "proceed"
    if not critique["pass"]:
        # 评审未通过 → 已回写约束, 要求 Worker 下一轮纠正
        action = "constrain"
    if supervisor_summary is not None and worker_summary is not None:
        cross = await cross_validate(
            llm_call, supervisor_summary=supervisor_summary,
            worker_summary=worker_summary, original_intent=original_intent,
            task_id=task_id,
        )
        if not cross["aligned"]:
            if cross["verdict"] in ("supervisor_drift", "both_drift"):
                # 监督者自身复述已偏离 → 必须主 Agent 重新锚定 (覆盖 constrain)
                action = "escalate"
            # 否则保持 constrain (Worker 偏离, 约束已回写)
    else:
        if critique["score"] < escalate_threshold:
            action = "escalate"

    _log.info(
        "Supervision cycle task %s: pass=%s score=%s action=%s",
        task_id, critique["pass"], critique["score"], action,
    )
    return {
        "critique": critique,
        "crossval": cross,
        "action": action,
        "constraint": constraint,
        "task_id": task_id,
        "ts": int(_time.time()),
    }


# ═══════════════════════════════════════════════════════════════
# 4. 主 Yuncat Agent 仲裁消费 (闭环的"联动"落点)
# ═══════════════════════════════════════════════════════════════
#
# supervise_round 在发现 supervisor/worker 偏离时会:
#   - 置位 rollover.pending_arbitration = True
#   - 广播 WS 事件 long_task_supervisor_escalation
# 本节能让主 Yuncat Agent (agent_id=yuncat) 真正"消费"这次升级——
# 拉取待仲裁任务、给出决策 (纠偏约束 / 重新锚定 / 中止 / 忽略) 并回灌运行中长任务。
# 全部 best-effort, 不阻塞执行。

ARBITRATION_DECISIONS = ("adjust", "reanchor", "dismiss", "abort")


async def _fetch_pending_arbitrations(orch, *, limit: int = 50) -> list[tuple[str, dict]]:
    """扫 tasks 表, 取所有 pending_arbitration=True 的长任务。

    返回 [(task_id, rollover_dict), ...]。rollover 取自 task_state.long_task.rollover。
    """
    try:
        cur = await orch._conn.execute(
            "SELECT id, task_state FROM tasks "
            "WHERE json_extract(task_state, '$.long_task.rollover.pending_arbitration') = 1 "
            "LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
    except Exception:
        return []
    out: list[tuple[str, dict]] = []
    for task_id, ts_json in rows:
        try:
            data = _json.loads(ts_json) if ts_json else {}
        except (_json.JSONDecodeError, TypeError):
            data = {}
        rl = (data.get("long_task", {}) or {}).get("rollover", {}) or {}
        out.append((task_id, rl))
    return out


async def get_pending_arbitrations(orch, *, limit: int = 50) -> list[dict]:
    """供主 Agent / UI 拉取待仲裁清单。

    Returns:
        [{task_id, pending_arbitration, current_generation, goal_summary,
          worker_session_id, supervisor_session_id, last_escalation}, ...]
    """
    rows = await _fetch_pending_arbitrations(orch, limit=limit)
    items = []
    for task_id, rl in rows:
        # _fetch_pending_arbitrations 已用 WHERE pending_arbitration=1 过滤, 返回项必 pending
        items.append({
            "task_id": task_id,
            "pending_arbitration": True,
            "current_generation": rl.get("current_generation"),
            "goal_summary": rl.get("goal_summary"),
            "worker_session_id": rl.get("worker_session_id"),
            "supervisor_session_id": rl.get("supervisor_session_id"),
            "last_escalation": rl.get("last_escalation"),
        })
    return items


async def is_task_aborted(orch, task_id: str) -> bool:
    """执行器热循环用——判定该任务是否已被主 Agent 仲裁中止。"""
    try:
        st = await orch.get_state(task_id)
        return bool((st.get("rollover", {}) or {}).get("aborted"))
    except Exception:
        return False


async def apply_abort(orch, task_id: str, reason: str) -> None:
    """把任务置为 interrupted (保留断点, 可 resume) 并广播, 清除待仲裁标志。

    与 orchestrator 既有 interrupted 语义一致 (backend 重启恢复同款状态)。
    """
    try:
        await orch._conn.execute(
            "UPDATE tasks SET status='interrupted', error_message=? WHERE id=?",
            (reason, task_id),
        )
        await orch._conn.commit()
    except Exception:
        _log.debug("apply_abort: status update failed for %s", task_id, exc_info=True)
    try:
        st = await orch.get_state(task_id)
        rl = (st.get("rollover", {}) or {}).copy()
        rl["aborted"] = True
        rl["pending_arbitration"] = False
        rl["last_resolution"] = {"ts": int(_time.time()), "decision": "abort", "directive": reason}
        await orch.set_state(task_id, rollover=rl)
    except Exception:
        _log.debug("apply_abort: state update failed for %s", task_id, exc_info=True)
    await _safe_ws(orch, task_id, {
        "type": "long_task_arbitration_resolved",
        "task_id": task_id,
        "action": "abort",
        "decision": "abort",
        "directive": reason,
        "ts": int(_time.time()),
    })


async def resolve_arbitration(
    orch, task_id: str, *,
    decision: str,
    directive: str | None = None,
) -> dict:
    """主 Yuncat Agent 对一次监督升级做出仲裁决策并回灌运行中的长任务。

    decision ∈ ARBITRATION_DECISIONS:
      - adjust:   把 directive 作为纠偏约束广播回 running 节点 (inject_constraint)。
      - reanchor: 用 directive 重新锚定目标 (更新 rollover.goal_summary, Worker 下一窗口据此对齐)。
      - dismiss:  仅清除待仲裁标志, 不改动执行。
      - abort:    置任务 interrupted 并停止执行 (见 apply_abort)。

    全程 best-effort; 返回应用结果摘要。
    """
    if decision not in ARBITRATION_DECISIONS:
        return {"ok": False, "resolved": False, "error": f"未知决策: {decision}"}
    if decision in ("adjust", "reanchor") and not directive:
        return {"ok": False, "resolved": False, "error": f"{decision} 需要 directive"}

    try:
        st = await orch.get_state(task_id)
    except Exception:
        return {"ok": False, "resolved": False, "error": "读取任务状态失败"}

    rl = (st.get("rollover", {}) or {}).copy()
    applied = None

    if decision == "adjust":
        try:
            await orch.inject_constraint(task_id, directive)
            applied = "constraint_injected"
        except Exception:
            _log.debug("resolve_arbitration: inject_constraint failed for %s", task_id, exc_info=True)
            return {"ok": False, "resolved": False, "error": "约束回写失败"}
    elif decision == "reanchor":
        rl["goal_summary"] = directive
        applied = "goal_reanchored"
    elif decision == "abort":
        await apply_abort(orch, task_id, directive or "主 Agent 仲裁中止")
        return {"ok": True, "resolved": True, "decision": "abort",
                "action": "abort", "task_id": task_id}
    # dismiss: applied 保持 None, 仅清标志

    rl["pending_arbitration"] = False
    rl["last_resolution"] = {
        "ts": int(_time.time()), "decision": decision, "directive": directive,
    }
    try:
        await orch.set_state(task_id, rollover=rl)
    except Exception:
        _log.debug("resolve_arbitration: set_state failed for %s", task_id, exc_info=True)
        return {"ok": False, "resolved": False, "error": "状态写回失败"}

    await _safe_ws(orch, task_id, {
        "type": "long_task_arbitration_resolved",
        "task_id": task_id,
        "action": "resolve",
        "decision": decision,
        "directive": directive,
        "applied": applied,
        "ts": int(_time.time()),
    })
    return {
        "ok": True, "resolved": True, "decision": decision,
        "applied": applied, "task_id": task_id,
    }
