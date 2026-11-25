"""services/deliberation_discussion.py —— 三阶段并行讨论引擎。

从 deliberation_service.py 拆分而来，保留全部功能。
模块级函数以 self 为首参，由 DeliberationService 薄包装委托调用。
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid

from config import calc_max_parallel_roles, adaptive_truncate, calc_context_budget, DELIBERATION_ROLE_TIMEOUT

_log = logging.getLogger("yuncat.deliberation")

# 角色讨论专用 system prompt 模板
ROLE_SYSTEM_TEMPLATE = """## 讨论规则
你现在扮演「{role_name}」参与一个多角色技术讨论。其他参与者会同时分析同一个问题。

{role_prompt}

## 输出格式要求
1. 先一句话总结你的核心观点
2. 列出 2-5 条具体建议或发现
3. 每条前面加上 [提案] 标记

请开始分析。"""

# 交叉审查 prompt
REVIEW_PROMPT = """## 交叉审查阶段
你是「{role_name}」。以下是其他角色的提案，请从你的专业角度评审：

{others}

## 要求
1. 指出你同意哪些观点
2. 指出你可能不同意的观点及原因
3. 补充他们遗漏的内容

请用中文回复。"""

# 协调员汇总 prompt
CONSENSUS_PROMPT = """## 共识汇总阶段
你是讨论协调员。以下是所有角色的提案和评审意见：

{all_content}

请生成综合方案：
1. 各角色都同意的核心结论（如果有）
2. 有分歧的要点及各方理由
3. 推荐的最终方案（不是简单的多数意见，而是考虑了所有专业意见后最优的）
4. 标注仍存在的风险

请用中文回复。"""

# 结构化输出解析
STRUCTURED_PARSE_PATTERNS = [
    (re.compile(r'\[提案\]\s*(.+?)(?=\[|$)', re.DOTALL), "proposal"),
    (re.compile(r'\[发现\]\s*(.+?)(?=\[|$)', re.DOTALL), "finding"),
    (re.compile(r'\[风险\]\s*(.+?)(?=\[|$)', re.DOTALL), "risk"),
    (re.compile(r'\[结论\]\s*(.+?)(?=\[|$)', re.DOTALL), "conclusion"),
]


def parse_structured_output(text: str) -> dict[str, list[str]]:
    """解析 Agent 输出中的结构化标签 → {proposal: [...], finding: [...], risk: [...], conclusion: [...]}"""
    result: dict[str, list[str]] = {"proposal": [], "finding": [], "risk": [], "conclusion": []}
    for pattern, key in STRUCTURED_PARSE_PATTERNS:
        for m in pattern.finditer(text):
            item = m.group(1).strip()
            if item:
                result[key].append(item)
    return result


def count_divergence(proposals: list[dict], reviews: list[dict]) -> int:
    """统计分歧项数——评审中明确表示"不同意"或"反对"的条目。"""
    count = 0
    for r in reviews:
        text = r.get("review", "")
        count += len(re.findall(r'(?:不同意|反对|不正确|有问题|需重新考虑)', text))
    return count


async def _inter_phase_checkpoint(
    self, task_id: str, task_input: str, push_callback, phase_label: str,
) -> str:
    """Phase 49-D6: 阶段间检查点——等待暂停恢复 + 注入用户消息到下一阶段。

    设计原则:
    1. **暂停不中断当前阶段**——只在阶段切换点检查，避免半成品状态。
       这是对标 VS Code 调试器的"pause between steps"模式，而非
       Thread.sleep 中断模式（后者会破坏数据一致性）。
    2. **用户消息批量注入**——排空队列后追加到 task_input，下一阶段所有
       角色都能看到用户补充意见。
    3. **WS 推送可见性**——通知前端"已注入用户意见"，让用户知道消息已生效。

    参数:
        phase_label: "reviews" 或 "consensus"——用于日志区分检查点位置。
    """
    # 1. 等待暂停恢复（如果用户按了暂停，event 处于 clear 状态，wait 会阻塞）
    event = self._pause_events.get(task_id)
    if event is not None:
        await event.wait()

    # 2. 排空用户消息队列并注入到 task_input
    messages = self._user_messages.get(task_id)
    if messages:
        user_block_parts = ["\n\n## 讨论参与者（用户）补充意见"]
        for m in messages:
            user_block_parts.append(f"- {m}")
        user_block = "\n".join(user_block_parts)
        msg_count = len(messages)
        messages.clear()
        task_input = task_input + user_block
        _log.info(
            "Task %s inter-phase(%s): injected %d user message(s) into next phase",
            task_id, phase_label, msg_count,
        )
        await self._push(push_callback, task_id, "deliberation_chunk", {
            "role_name": "协调员", "phase": "user_input",
            "data": f"已接收 {msg_count} 条用户补充意见，将注入到下一阶段讨论",
        })

    return task_input


def _build_spec_input(task_input: str, consensus: str, proposals: list[dict]) -> str:
    """Task 7.1 子任务1: 将讨论结果格式化为 Spec-ready 输入。

    将讨论共识 + 原始需求 + 结构化风险整合为一个可直接传入 run_spec_loop
    的 task_input 字符串，实现「讨论→Spec 自动衔接」。

    格式:
      ## 讨论共识
      {consensus}

      ## 原始需求
      {task_input}

      ## 识别的风险
      - {risk1}
      - {risk2}
      ...
    """
    sections: list[str] = []

    if consensus:
        sections.append(f"## 讨论共识\n{consensus}")

    if task_input:
        sections.append(f"## 原始需求\n{task_input}")

    # 从所有提案的 _structured.risk 中提取风险
    all_risks: list[str] = []
    for p in proposals:
        structured = p.get("_structured", {})
        risks = structured.get("risk", [])
        if isinstance(risks, list):
            all_risks.extend(r for r in risks if r)
    if all_risks:
        risk_lines = "\n".join(f"- {r}" for r in all_risks)
        sections.append(f"## 识别的风险\n{risk_lines}")

    return "\n\n".join(sections) if sections else task_input or consensus or ""


async def _limited_call(_sem: asyncio.Semaphore, fn, *args):
    """Semaphore 限流执行——替代原 _limited 闭包，便于跨子函数复用。"""
    async with _sem:
        return await fn(*args)


async def _run_proposals_phase(
    self, task_input: str, roles: list, task_id: str,
    push_callback, role_agent_map: dict, default_agent: str,
    _sem: asyncio.Semaphore,
    language: str = "zh",
) -> list:
    """阶段 1: 并行提案——每个角色独立分析 + 结构化标签提取。"""
    await self._push(push_callback, task_id, "deliberation_phase", {"phase": "proposals"})

    async def propose(role: dict) -> dict:
        try:
            role_prompt = ROLE_SYSTEM_TEMPLATE.format(
                role_name=role["name"],
                role_prompt=role.get("prompt_template", "").replace("{{task}}", task_input),
            )
            model_key = self._get_role_model(role.get("id", ""))
            # Phase 31: Per-role agent routing (vision→OpenClaw, shell→Hermes, etc.)
            role_agent = role_agent_map.get(role.get("id", ""), default_agent)
            output = await asyncio.wait_for(
                self.agent_service._invoke_with_role(role_agent, role_prompt, task_input, model_key, language=language),
                timeout=DELIBERATION_ROLE_TIMEOUT,
            )
            return {"role_id": role["id"], "role_name": role["name"], "output": output}
        except asyncio.TimeoutError:
            _log.warning(f"Role {role.get('name')} timed out after {DELIBERATION_ROLE_TIMEOUT}s")
            return {"role_id": role["id"], "role_name": role["name"],
                    "output": f"[超时] {role['name']} 在 {DELIBERATION_ROLE_TIMEOUT} 秒内未完成分析"}
        except Exception as e:
            _log.exception(f"Role {role.get('name')} proposal failed")
            return {"role_id": role["id"], "role_name": role["name"], "output": f"[错误] {e}"}

    proposals = await asyncio.gather(
        *[_limited_call(_sem, propose, r) for r in roles], return_exceptions=True,
    )
    proposals = [
        p if isinstance(p, dict) else {"output": f"[异常] {str(p)}"}
        for p in proposals
    ]
    # Phase 23: 结构化输出解析——提取 [提案]/[发现]/[风险] 标签
    for p in proposals:
        structured = parse_structured_output(p.get("output", ""))
        p["_structured"] = structured
        await self._push(push_callback, task_id, "deliberation_chunk", {
            "role_name": p.get("role_name", "?"),
            "phase": "proposals",
            "data": p.get("output", ""),
            "structured": structured,
            "agent_id": role_agent_map.get(p.get("role_id", ""), default_agent),
        })
    return proposals


async def _run_reviews_phase(
    self, task_input: str, roles: list, task_id: str,
    push_callback, role_agent_map: dict, default_agent: str,
    _sem: asyncio.Semaphore, proposals: list, agent_id: str,
    _proposal_review_budget: int,
    language: str = "zh",
) -> tuple[list, list, int]:
    """阶段 2+2.5: 交叉审查 + 二轮共识 + 对抗验证。Returns: (reviews, proposals, divergence)。"""
    await self._push(push_callback, task_id, "deliberation_phase", {"phase": "reviews"})

    from config import MAX_CONTEXT_FALLBACK_CHARS

    async def review(i: int) -> dict:
        role = roles[i]
        others = "\n---\n".join(
            # Phase 23: 全文传递 — Phase 56-P2: 自适应上限按模型窗口动态计算
            f"[{p.get('role_name', '?')}]: {adaptive_truncate(p.get('output', ''), MAX_CONTEXT_FALLBACK_CHARS, _proposal_review_budget, 0.6)}"
            for j, p in enumerate(proposals) if j != i
        )
        try:
            review_prompt = REVIEW_PROMPT.format(role_name=role["name"], others=others)
            model_key = self._get_role_model(role.get("id", ""))
            role_agent = role_agent_map.get(role.get("id", ""), default_agent)
            output = await self.agent_service._invoke_with_role(role_agent, review_prompt, task_input, model_key, language=language)
            return {"role_id": role["id"], "role_name": role["name"], "review": output}
        except Exception as e:
            _log.exception(f"Role {role.get('name')} review failed")
            return {"role_id": role["id"], "role_name": role["name"], "review": f"[错误] {e}"}

    reviews = await asyncio.gather(
        *[_limited_call(_sem, review, i) for i in range(len(roles))], return_exceptions=True,
    )
    reviews = [r if isinstance(r, dict) else {"review": f"[异常] {str(r)}"} for r in reviews]
    for r in reviews:
        await self._push(push_callback, task_id, "deliberation_chunk", {
            "role_name": r.get("role_name", "?"),
            "phase": "reviews",
            "data": r.get("review", ""),
            "agent_id": role_agent_map.get(r.get("role_id", ""), default_agent),
        })

    # Phase 23: 二轮共识——分歧 > 2 项时自动追加一轮
    divergence = count_divergence(proposals, reviews)
    if divergence > 2:
        await self._push(push_callback, task_id, "deliberation_phase",
                         {"phase": "round2", "divergence": divergence})
        _log.info("Divergence %d > 2, triggering second consensus round", divergence)
        round2_prompt = f"## 分歧焦点\n上一轮存在 {divergence} 项分歧。请重点讨论以下未解决点，尝试达成一致：\n\n{task_input}"

        async def round2_propose(role: dict) -> dict:
            try:
                rp = ROLE_SYSTEM_TEMPLATE.format(
                    role_name=role["name"],
                    role_prompt=role.get("prompt_template", "").replace("{{task}}", round2_prompt),
                )
                output = await asyncio.wait_for(
                    self.agent_service._invoke_with_role(agent_id, rp, round2_prompt, language=language),
                    timeout=DELIBERATION_ROLE_TIMEOUT,
                )
                return {"role_id": role["id"], "role_name": role["name"], "output": output}
            except Exception:
                return {"role_id": role["id"], "role_name": role["name"], "output": ""}

        r2 = await asyncio.gather(*[_limited_call(_sem, round2_propose, r) for r in roles], return_exceptions=True)
        r2 = [p if isinstance(p, dict) else {"output": ""} for p in r2]
        for p in r2:
            if p.get("output"):
                await self._push(push_callback, task_id, "deliberation_chunk", {
                    "role_name": p.get("role_name", "?"),
                    "phase": "round2",
                    "data": p.get("output", ""),
                    "agent_id": role_agent_map.get(p.get("role_id", ""), default_agent),
                })
        # 用第二轮结果更新 proposals
        for i, p in enumerate(r2):
            if i < len(proposals) and p.get("output"):
                proposals[i]["output"] = proposals[i]["output"] + "\n\n## 第二轮\n" + p["output"]

    # Phase 22: 阶段 2.5 — Adversarial Verification
    if len(roles) >= 3:
        await self._push(push_callback, task_id, "deliberation_phase", {"phase": "adversarial"})
        try:
            from services.dynamic_orchestrator import AdversarialReviewer
            if self._adversarial is None:
                self._adversarial = AdversarialReviewer(self.agent_service)
            surviving, refuted = await self._adversarial.review(proposals, task_input)
            if refuted:
                _log.info("Adversarial: refuted %d/%d proposals", len(refuted), len(proposals))
                await self._push(push_callback, task_id, "deliberation_chunk", {
                    "role_name": "Adversarial",
                    "phase": "adversarial",
                    "data": f"[REFUTED] {len(refuted)} 条提案被推翻，{len(surviving)} 条通过",
                    "agent_id": agent_id,  # Phase 48-G: 跟随任务 agent_id（原硬编码 "hermes"）
                })
            proposals = surviving or proposals
        except Exception:
            _log.exception("Adversarial review failed, continuing without it")

    return reviews, proposals, divergence


async def _finalize_consensus(
    self, task_input: str, roles: list, task_id: str,
    push_callback, proposals: list, reviews: list,
    divergence: int, agent_id: str, temp_role: dict | None,
    role_agent_map: dict, default_agent: str,
    _consensus_proposal_budget: int,
    _consensus_review_budget: int,
    _feedback_task_budget: int,
    _knowledge_task_budget: int,
    language: str = "zh",
) -> dict:
    """阶段 3: 协调员汇总 + 临时角色追踪 + Agent 表现追踪。"""
    await self._push(push_callback, task_id, "deliberation_phase", {"phase": "consensus"})
    # Phase 56-P2: 全文传递上限按模型窗口动态计算
    all_content = "\n\n".join(
        f"### {p.get('role_name', '?')} 的提案\n{p.get('output', '')[:_consensus_proposal_budget]}\n"
        f"### {p.get('role_name', '?')} 的评审\n"
        f"{reviews[i].get('review', '')[:_consensus_review_budget] if i < len(reviews) else ''}"
        for i, p in enumerate(proposals)
    )
    try:
        consensus_prompt = CONSENSUS_PROMPT.format(all_content=all_content)
        consensus = await self.agent_service._invoke_with_role(agent_id, consensus_prompt, task_input, language=language)
    except Exception as e:
        _log.exception("Consensus generation failed")
        consensus = f"[共识生成失败: {e}]"

    await self._push(push_callback, task_id, "deliberation_done", {
        "consensus": consensus,
        "roles": [{"name": r["name"], "id": r["id"]} for r in roles],
        "divergence": divergence,
    })

    # Phase 30: 追踪临时角色
    if temp_role and consensus and "错误" not in str(consensus)[:50]:
        try:
            fb = self.agent_service._feedback_learner if self.agent_service else None
            if fb and hasattr(fb, "track_temporary_role"):
                await fb.track_temporary_role(
                    role_name=temp_role["name"],
                    task_prompt=task_input[:_feedback_task_budget],
                    was_accepted=True,
                )
        except Exception:
            _log.debug("Unexpected error", exc_info=True)

    # Phase 31: 混合讨论 Agent 表现追踪——写入 knowledge_nodes
    for p in proposals:
        try:
            role_name = p.get("role_name", "?")
            rid = next((r.get("id") for r in roles if r.get("name") == role_name), "")
            agent_used = role_agent_map.get(rid, default_agent)
            if self.agent_service and self.agent_service._memory_tree:
                import json
                import time
                node_title = f"agent_perf:{role_name}→{agent_used}"
                node_content = (
                    f"Task: {task_input[:_knowledge_task_budget]}\n"
                    f"Role: {role_name}\nAgent: {agent_used}\n"
                    f"Output length: {len(p.get('output', ''))}\n"
                    f"Structured findings: {len(p.get('_structured', {}).get('proposal', [])) + len(p.get('_structured', {}).get('finding', []))}"
                )
                await self.agent_service._memory_tree.db.execute(
                    "INSERT INTO knowledge_nodes(id, title, content, source, score, tags, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4())[:12], node_title, node_content,
                     "mixed_deliberation", 0.5, json.dumps([agent_used]), int(time.time()), int(time.time())),
                )
                await self.agent_service._memory_tree.db.commit()
        except Exception:
            _log.debug("Unexpected error", exc_info=True)

    return {
        "status": "completed",
        "consensus": consensus,
        "spec_input": _build_spec_input(task_input, consensus, proposals),
        "proposals": [
            {"role_name": p.get("role_name"), "output": p.get("output"),
             "_structured": p.get("_structured", {})}
            for p in proposals
        ],
        "reviews": [
            {"role_name": r.get("role_name"), "review": r.get("review")}
            for r in reviews
        ],
        "divergence": divergence,
    }


async def run_deliberation(
    self,
    task_input: str,
    roles: list[dict],
    task_id: str,
    push_callback=None,
    language: str = "zh",
) -> dict:
    """三阶段讨论流程。

    Phase 48 T3-7: 拆分为 3 个子函数 + _limited_call 辅助，本函数仅负责编排。
    push_callback(task_id, msg_type, payload) 用于 WS 推送。
    """
    if not self.agent_service:
        return {"error": "agent_service not configured"}

    # Phase 107: 设置讨论语言上下文，供内部工具调用继承
    from context import set_current_language, reset_current_language
    lang_token = set_current_language(language)
    try:
        return await _run_deliberation_body(self, task_input, roles, task_id, push_callback, language)
    finally:
        try:
            reset_current_language(lang_token)
        except Exception:
            pass


async def _run_deliberation_body(
    self, task_input: str, roles: list[dict], task_id: str,
    push_callback, language: str,
) -> dict:
    """run_deliberation 实际编排体（抽离以便统一设置语言上下文）。"""
    if not self.agent_service:
        return {"error": "agent_service not configured"}

    all_agent_ids = list(self.agent_service.adapters.keys())
    if not all_agent_ids:
        return {"error": "no agents available"}

    # Phase 31: Multi-agent mixed deliberation — each role gets its best agent
    from services.capability_router import route_role_to_agent
    default_agent = all_agent_ids[0]
    role_agent_map: dict[str, str] = {}
    for r in roles:
        rid = r.get("id", "")
        role_agent_map[rid] = route_role_to_agent(rid, self.agent_service.adapters, default_agent)
    agent_id = default_agent

    # 前置检查 & Phase 23: Semaphore 可配置——从 provider config 读 max_concurrency
    cfg = {}
    try:
        cfg = await self.agent_service._get_model_config("primary")
        if not cfg.get("api_key"):
            return {"error": "未配置 API Key，请在设置 > 模型配置中填写"}
        max_concurrency = cfg.get("max_concurrency", 6)
    except Exception:
        # Phase 43 P0-DYN: semaphore 并发数从模型上下文窗口动态派生
        max_concurrency = calc_max_parallel_roles(cfg if cfg.get("api_key") else {"model": "default"})
    _sem = asyncio.Semaphore(max_concurrency)

    # Phase 56-P2: deliberation 预算按当前模型窗口动态计算
    _proposal_review_budget = calc_context_budget(cfg, fraction=0.02, min_chars=1500, max_chars=6000)
    _consensus_proposal_budget = calc_context_budget(cfg, fraction=0.012, min_chars=800, max_chars=4000)
    _consensus_review_budget = calc_context_budget(cfg, fraction=0.008, min_chars=600, max_chars=3000)
    _feedback_task_budget = calc_context_budget(cfg, fraction=0.002, min_chars=300, max_chars=800)
    _knowledge_task_budget = calc_context_budget(cfg, fraction=0.001, min_chars=150, max_chars=400)

    # Phase 30: 临时角色生成
    temp_role = None
    if len(roles) >= 2:
        try:
            temp_role = await self._maybe_generate_temp_role(task_input, roles)
            if temp_role:
                roles = list(roles) + [temp_role]
                await self._push(push_callback, task_id, "deliberation_chunk", {
                    "role_name": "协调员", "phase": "temp_role",
                    "data": f"生成临时角色: {temp_role['name']}",
                })
        except Exception:
            _log.debug("Temp role generation skipped")

    # 阶段 1: 并行提案
    proposals = await _run_proposals_phase(
        self, task_input, roles, task_id, push_callback,
        role_agent_map, default_agent, _sem, language=language,
    )
    # Phase 49-D6: 阶段间检查点 1——等待暂停恢复 + 注入用户消息到审查阶段
    task_input = await _inter_phase_checkpoint(self, task_id, task_input, push_callback, "reviews")
    # 阶段 2+2.5: 交叉审查 + 二轮共识 + 对抗验证
    reviews, proposals, divergence = await _run_reviews_phase(
        self, task_input, roles, task_id, push_callback,
        role_agent_map, default_agent, _sem, proposals, agent_id,
        _proposal_review_budget, language=language,
    )
    # Phase 49-D6: 阶段间检查点 2——等待暂停恢复 + 注入用户消息到共识阶段
    task_input = await _inter_phase_checkpoint(self, task_id, task_input, push_callback, "consensus")
    # 阶段 3: 协调员汇总 + 追踪
    return await _finalize_consensus(
        self, task_input, roles, task_id, push_callback,
        proposals, reviews, divergence, agent_id, temp_role,
        role_agent_map, default_agent,
        _consensus_proposal_budget, _consensus_review_budget,
        _feedback_task_budget, _knowledge_task_budget,
        language=language,
    )
