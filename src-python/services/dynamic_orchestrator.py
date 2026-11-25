"""services/dynamic_orchestrator.py —— 动态工作流编排器（Phase 22）

借鉴 Claude Code Dynamic Workflows + Mozzie LLM 编排器：
  - LLM 自写 DAG JSON → engine 执行（不写 JS 脚本引擎）
  - 4 级 effort 映射（quick/standard/deep/ultracode）
  - Adversarial Verification + Convergence loop-until-dry
  - Agent Teams 层级模型（Lead → Workers）
"""
import asyncio
import json as _json
import logging
import re
import uuid as _uuid
from config import (
    DEFAULT_AGENT_ID, adaptive_truncate, ADVERSARIAL_REVIEW_BUDGET_CHARS,
    DYNAMIC_ORCHESTRATOR_LAST_RESULT_PREVIEW_CHARS,
    LOG_PREVIEW_ERROR_LEN,
)

_log = logging.getLogger("yuncat.orchestrator")

# ---- 工作流生成器提示词 ----

WORKFLOW_GENERATOR_PROMPT = """## 角色：工作流编排器
你是 DAG 工作流生成器。根据用户任务，生成一个 JSON 格式的工作流定义。

## effort 级别：{effort}

## 输出格式——严格 JSON
{{
  "nodes": [
    {{"id": "唯一标识", "type": "default", "role": "角色名（如:安全专家/性能专家/架构师）",
      "prompt": "节点 prompt——具体到本节点的任务是什么"}}
  ],
  "edges": [
    {{"from": "源节点id", "to": "目标节点id"}}
  ]
}}

## effort 级别指南
- **quick**: 1-2 个并行探索节点，无 verify，单轮直接输出
- **standard**: 2-4 个并行节点，1 层交叉审查（每个 review 节点审查对应的 explore 节点），最多 1 个 synthesize 节点
- **deep**: 4-6 个并行节点，2 层审查（cross-review → adversary verify），最后 synthesize
- **ultracode**: 8-16 个并行节点，3 层（cross → adversary → consensus），convergence 循环\\n  - Team 层级: Lead 节点 (`agent: "hermes"`) + Worker 节点 (`agent: "openclaw"` 负责 vision/search)·结果汇总到 Lead

## 规则
1. 并行节点用相同的前置依赖（edges 中 from 同一个上游或都 from start）
2. verify 节点按角色配对——review_security 审查 security，review_perf 审查 perf
3. synthesize 节点是所有 verify 的下游
4. Human 节点（type="human"）只用在需要用户决策的关键位置，不超过 1 个
5. 输出**只包含 JSON**，不要任何解释文本"""


class DynamicOrchestrator:
    """LLM 自生成工作流 → DAG 执行。

    BA.2 冲突 2 修复: generate_and_run 在 create 前调用 engine._validate_graph()。
    Phase 22 Convergence: generate_and_run 支持 loop-until-dry——最多 N 轮，连续 dry 即停止。
    """

    MAX_ROUNDS = {"quick": 2, "standard": 5, "deep": 10, "ultracode": 20}  # Phase 48-H: 放宽上限对标主流

    def __init__(self, agent_service, workflow_service, engine=None):
        self._agent = agent_service
        self._wf = workflow_service
        self._engine = engine

    async def generate_and_run(
        self, prompt: str, effort: str = "auto",
    ) -> dict:
        """1. LLM 生成 DAG JSON → 2. 验证 → 3. DAG 执行 → 4. 收敛检查 → 5. 返回结果

        Phase 22 Convergence: 达到 max_rounds 或连续 dry_rounds 轮无新发现即停止。
        """
        max_rounds = self.MAX_ROUNDS.get(effort, 2)
        dry_rounds_needed = 2  # 连续 2 轮无新发现即收敛

        all_results = []
        prev_findings_count = 0
        dry_rounds = 0

        for round_num in range(1, max_rounds + 1):
            # Step 1: LLM 自写编排计划（第一轮完整生成，后续轮注入上轮上下文）
            round_prompt = prompt
            if round_num > 1 and all_results:
                prev_summary = _json.dumps({
                    "rounds_completed": round_num - 1,
                    "last_findings_count": prev_findings_count,
                    "last_result": str(all_results[-1])[:DYNAMIC_ORCHESTRATOR_LAST_RESULT_PREVIEW_CHARS],
                }, ensure_ascii=False)
                round_prompt = (
                    f"## 上一轮结果\n{prev_summary}\n\n"
                    f"## 原始任务\n{prompt}\n\n"
                    f"请检查是否有遗漏的新发现。如果没有实质性新发现，返回空结果。"
                )

            plan = await self._generate_plan(round_prompt, effort)
            if not plan:
                if round_num == 1:
                    return {"error": "Failed to generate workflow plan", "status": "failed"}
                # 非首轮生成失败 → 已收敛
                break

            nodes = plan.get("nodes", [])
            edges = plan.get("edges", [])

            # Step 2: 验证
            if self._engine:
                try:
                    self._engine._validate_graph(nodes, edges)
                except ValueError as e:
                    _log.warning("LLM generated invalid DAG (round %d), retrying: %s", round_num, e)
                    plan = await self._generate_plan(round_prompt, effort)
                    if not plan:
                        break
                    try:
                        self._engine._validate_graph(plan["nodes"], plan["edges"])
                    except ValueError as e2:
                        return {"error": f"Generated DAG has cycle: {e2}", "status": "failed"}

            # Step 3: 保存 + 执行
            wf_name = f"dynamic-{str(_uuid.uuid4())[:8]}-r{round_num}"
            wf_id = await self._wf.create({"name": wf_name, "graph": plan})
            run_id = await self._wf.run(wf_id, prompt)
            round_result = await self._wf.get_run(run_id)
            all_results.append(round_result)

            # Step 4: 收敛检查
            curr_findings = ConvergenceDetector.count_findings(round_result)
            if curr_findings <= prev_findings_count and curr_findings <= 1:
                dry_rounds += 1
            else:
                dry_rounds = 0
            prev_findings_count = max(prev_findings_count, curr_findings)

            if dry_rounds >= dry_rounds_needed:
                _log.info("Converged after %d rounds (dry=%d, findings=%d)",
                          round_num, dry_rounds, curr_findings)
                break

        return {
            "status": "completed",
            "rounds": len(all_results),
            "converged": dry_rounds >= dry_rounds_needed,
            "effort": effort,
            "findings_count": prev_findings_count,
            "results": all_results,
        }

    async def _generate_plan(self, prompt: str, effort: str) -> dict | None:
        """用 LLM（Flash 模型）生成 DAG JSON——成本极低

        Phase 43 H.1: 不再硬编码 agent_id。通过 route_task 自动选择后端——
        涉及视觉的节点→OC，代码分析→Hermes。
        """
        system = WORKFLOW_GENERATOR_PROMPT.format(effort=effort)
        # H.1: 自动路由——不再硬编码 "hermes"
        agent_id = DEFAULT_AGENT_ID
        try:
            adapters = self._agent.adapters if hasattr(self._agent, "adapters") else {}
            if len(adapters) > 1:
                from services.task_router import route_task
                route = await route_task(
                    prompt, adapters,
                    default_agent=DEFAULT_AGENT_ID,
                    allow_external=False,
                )
                if route.confidence >= 0.70:
                    agent_id = route.agent_id
                    _log.info("DynamicOrchestrator routed DAG gen to %s (%.2f)",
                             agent_id, route.confidence)
        except Exception:
            _log.debug("动态任务路由失败", exc_info=True)
        try:
            output = await self._agent._invoke_with_role(
                agent_id, system, prompt,
            )
            return _extract_json(output)
        except Exception:
            _log.exception("Workflow generation failed")
            return None


# ---- Adversarial Reviewer ----

ADVERSARIAL_PROMPT = """## 角色：持怀疑态度的高级工程师
你的任务不是改进这些发现，而是**找出其中的错误**。
对于以下每一条提案，逐一回答：
1. 这个发现是否有事实支撑？如果引用了代码，代码确实存在吗？
2. 是否有逻辑漏洞？是否忽略了替代方案或边缘情况？
3. 问题的严重程度是否被夸大？
4. 这条发现是否真的需要修改，还是可以接受为技术债？

**默认立场**：每条发现都有可能是错的。只报告你**无法确认正确性**的发现。
标记格式：[REFUTED] 开头 = 该发现被推翻，将从最终报告中移除。
未标记 [REFUTED] 的发现视为通过验证。"""


class AdversarialReviewer:
    """对抗验证器——试图推翻审查发现，只保留无法被反驳的（Phase 22）。

    借鉴 Claude Code Adversarial Verification——这是 SWE-bench 80.9% 的核心贡献因素。
    """

    def __init__(self, agent_service):
        self._agent = agent_service

    async def review(self, proposals: list[dict], task_input: str) -> tuple[list[dict], list[dict]]:
        """对抗验证——双后端双视角 (Phase 43 H.2)。

        Reviewer A (hermes): 检查逻辑漏洞·代码引用准确性·论证完整性。
        Reviewer B (openclaw): 检查视觉/UI相关问题·搜索准确性·多渠道逻辑。
        如果只有单后端——回退到原始单视角行为。

        Returns (surviving_proposals, refuted_proposals)。
        """
        if not proposals or not self._agent:
            return proposals, []

        adapters = self._agent.adapters
        all_agent_ids = list(adapters.keys())
        if not all_agent_ids:
            return proposals, []

        proposals_text = "\n---\n".join(
            f"[{p.get('role_name', '?')}]: {p.get('output', p.get('content', ''))}"
            for p in proposals
        )

        from config import MAX_CONTEXT_FALLBACK_CHARS
        base_prompt = f"{ADVERSARIAL_PROMPT}\n\n## 待审查提案\n{adaptive_truncate(proposals_text, MAX_CONTEXT_FALLBACK_CHARS, ADVERSARIAL_REVIEW_BUDGET_CHARS, 0.6)}"

        # H.2: 双后端并行审查
        import asyncio as _asyncio

        async def _single_review(agent_id: str, perspective: str) -> str:
            try:
                prompt = base_prompt + f"\n\n### 审查视角\n{perspective}"
                return await self._agent._invoke_with_role(agent_id, prompt, task_input)
            except Exception:
                _log.debug("Adversarial review failed for %s", agent_id)
                return ""

        tasks = []
        # Reviewer A: Hermes — 逻辑/代码审查
        if "hermes" in adapters:
            tasks.append(_single_review("hermes", "你是代码逻辑审查者。重点检查: 代码引用是否真实存在·逻辑是否有漏洞·论证是否完整·严重度是否被夸大。"))
        # Reviewer B: OpenClaw — 视觉/UI/搜索审查
        if "openclaw" in adapters:
            tasks.append(_single_review("openclaw", "你是视觉/搜索/UI审查者。重点检查: 涉及视觉/UI的分析是否有截图支撑·搜索结果是否过时·多渠道逻辑是否正确。"))

        outputs = await _asyncio.gather(*tasks, return_exceptions=True)
        combined = "\n---\n".join(o if isinstance(o, str) else "" for o in outputs)

        if not combined.strip():
            return proposals, []

        # 解析合并结果
        surviving = []
        refuted = []
        for p in proposals:
            role_name = re.escape(p.get("role_name", "?"))
            refuted_pat = re.compile(
                rf'\[REFUTED\]\s+{role_name}', re.IGNORECASE,
            )
            if refuted_pat.search(combined):
                refuted.append(p)
            else:
                surviving.append(p)

        _log.info("Adversarial: %d backend(s) · %d survived · %d refuted",
                 len(tasks), len(surviving), len(refuted))
        return surviving, refuted


# ---- Convergence Detector ----

class ConvergenceDetector:
    """收敛检测器——判断多轮编排是否已收敛。

    收敛条件:
      - 新发现 ≤ 0: 已收敛
      - 新发现 ≤ 2 且与上轮重复 ≥ 80%: 已收敛
      - 连续 dry_rounds_needed 轮无变化: 已收敛
    """

    @staticmethod
    def count_findings(result: dict) -> int:
        """统计结果中的发现/提案条数。从 DAG run 结果的各种格式中提取。"""
        if not result:
            return 0

        # 尝试从 node_results 统计
        node_results = result.get("node_results", {})
        total = 0
        for nr in (node_results.values() if isinstance(node_results, dict) else []):
            output = ""
            if isinstance(nr, dict):
                output = nr.get("output", "")
            elif isinstance(nr, str):
                output = nr
            # 统计 [提案]/[发现] 标记数
            total += len(re.findall(r'\[(?:提案|发现|BUG|ISSUE|FINDING)\]', output))
            # 统计列表项
            total += len(re.findall(r'^\s*[-*]\s', output, re.MULTILINE))
        return total

    @staticmethod
    def overlap_pct(prev_findings: list[str], curr_findings: list[str]) -> float:
        """计算两轮发现的重复率（基于文本相似度）"""
        if not curr_findings:
            return 0.0
        overlaps = 0
        for cf in curr_findings:
            for pf in prev_findings:
                # 简单 Jaccard——如果 50% 的词重叠视为重复
                cf_words = set(cf.lower().split())
                pf_words = set(pf.lower().split())
                if not cf_words or not pf_words:
                    continue
                jaccard = len(cf_words & pf_words) / len(cf_words | pf_words)
                if jaccard > 0.5:
                    overlaps += 1
                    break
        return overlaps / len(curr_findings) if curr_findings else 0.0

    @staticmethod
    def is_cross_backend_findings(
        findings: list[str], backend_sources: list[str] | None = None,
    ) -> bool:
        """Phase 43 H.3: 跨后端收敛判定。

        如果两个不同后端发现了**不同类别**的问题，不算收敛——
        不同后端有不同专长视角，互补发现问题说明编排有效。
        OC 发现视觉问题 · Hermes 发现代码问题 = 两个独立视角，不必合并。
        """
        if not findings or not backend_sources or len(set(backend_sources)) <= 1:
            return False
        # 简化为: 只要有多个后端参与，就认为可能还有跨后端新发现
        return len(backend_sources) >= 2 and len(findings) > 0

    @staticmethod
    def cross_backend_reason(
        prev_findings: list[str], curr_findings: list[str],
        prev_sources: list[str] | None = None,
        curr_sources: list[str] | None = None,
    ) -> str:
        """返回跨后端收敛的详细原因——用于日志和 Harness 决策。"""
        if curr_sources and prev_sources and set(curr_sources) != set(prev_sources):
            new_backends = set(curr_sources) - set(prev_sources)
            if new_backends:
                return f"新后端参与: {new_backends} — 可能有跨视角新发现"
        if ConvergenceDetector.is_cross_backend_findings(
            curr_findings, curr_sources,
        ):
            return "跨后端发现: 不同后端发现不同类型问题"
        return ""


# ---- JSON 提取工具 ----

def _extract_json(text: str) -> dict | None:
    """从 LLM 输出中提取 JSON——支持 ```json 代码块和裸 JSON"""
    # 尝试 ```json 代码块
    m = re.search(r'```(?:json)?\s*\n(.*?)```', text, re.DOTALL)
    if m:
        try:
            return _json.loads(m.group(1))
        except _json.JSONDecodeError:
            pass

    # 尝试裸 JSON
    try:
        return _json.loads(text)
    except _json.JSONDecodeError:
        pass

    # 尝试提取 { ... } 块
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return _json.loads(m.group(0))
        except _json.JSONDecodeError:
            pass

    _log.warning("Failed to extract JSON from: %s", text[:LOG_PREVIEW_ERROR_LEN])
    return None


# ---- Effort 映射 ----

EFFORT_MAP = {
    "extreme": "ultracode",
    "high": "deep",
    "medium": "standard",
    "low": "quick",
}


def complexity_to_effort(level: str) -> str:
    """复杂度级别 → effort 级别"""
    return EFFORT_MAP.get(level, "standard")
