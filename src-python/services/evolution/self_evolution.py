"""services/self_evolution.py —— Phase 34: AI 自我进化引擎

观察对话 → 提取可复用模式 → 自动生成 Skill + Workflow → 更新记忆

闭环:
  1. 对话完成 → 提取工具组合模式 / 任务序列 / 用户偏好
  2. 模式累积 ≥ 阈值 → 自动生成 Skill (skill.yaml + prompt.md)
  3. 工具序列模式 → 自动生成 DAG Workflow 模板
  4. 用户接受/拒绝 → 反馈学习调整得分
  5. 定期巡查 → 低分模式衰减淘汰

借鉴:
  - Cursor "Self-Summarization" (Composer 从行为中学习)
  - OpenHuman "ambient personalization" (持续从使用中提取偏好)
  - Claude Code "auto-memory" (对话结束自动提炼)
"""
import asyncio
import logging
import time as _time
import uuid as _uuid
import os as _os
import json as _json
import re as _re

from config import (
    MAX_TOKENS_SELF_EVOLUTION, ADAPTER_HEALTH_CHECK_TIMEOUT, DEFAULT_AGENT_ID,
    SELF_EVOLUTION_TOOL_SEQUENCE_MAX_LEN,
    SELF_EVOLUTION_PATTERN_EXAMPLE_PREVIEW_CHARS,
    SELF_EVOLUTION_PATTERN_TITLE_MAX_CHARS,
    SELF_EVOLUTION_PATTERN_ID_MAX_CHARS,
    SELF_EVOLUTION_PATTERN_CONTENT_PREVIEW_CHARS,
    SELF_EVOLUTION_OC_INTERACTION_PREVIEW_CHARS,
    SELF_EVOLUTION_QUALITY_GATE_ENABLED,
    SELF_EVOLUTION_DRY_RUN_SAMPLE_COUNT,
)
from services.evolution.evolution_quality_gate import EvolutionQualityGate


def _resolve_skill_export_dir(skill_registry, task_type: str) -> str:
    """算出自动生成技能的导出目录。

    注册表可能是测试注入的替身（MagicMock 等）。此时 getattr(..., '_install_root', 默认)
    取到的是替身对象本身而非默认值，Path(str(...)) 会在当前工作目录真的建出目录——
    测试跑完就在仓库根留下 MagicMock/，并顺着打包白名单混进安装包（2026-09-24 实测）。
    只有真实的 str / PathLike 才当作安装根，否则回退到默认位置。
    """
    from pathlib import Path as _Path

    root = getattr(skill_registry, "_install_root", None)
    if not isinstance(root, (str, _os.PathLike)) or not str(root):
        root = _Path(__file__).parent.parent / "skills"
    return str(_Path(root) / "auto" / task_type)

_log = logging.getLogger("yuncat.evolution")


class SelfEvolutionEngine:
    """AI 自我进化引擎——从使用中学习并自动创建资产。"""

    # 阈值
    PATTERN_CONFIDENCE_MIN = 3       # 同一模式出现 ≥3 次才考虑提取
    AUTO_SKILL_THRESHOLD = 5          # ≥5 次自动生成 Skill
    AUTO_WORKFLOW_THRESHOLD = 4       # ≥4 次自动生成 Workflow

    def __init__(
        self, db, skill_registry=None, model_config=None, memory_tree=None, adapters=None,
        notification_center=None, workflow_service=None,
    ):
        self.db = db
        self._skill_registry = skill_registry
        self._model_config = model_config
        self._memory_tree = memory_tree
        self._adapters = adapters or {}
        # Batch 5.3: 注入通知中心与 workflow 服务（用于 pending 通知与 A/B baseline）
        self._notification_center = notification_center
        self._workflow_service = workflow_service
        self._quality_gate = EvolutionQualityGate()
        # 模式计数器: {pattern_signature: {count, examples[], last_seen}}
        # Batch 2.6: 从 evolution_patterns 表加载持久化计数，避免重启丢计数
        self._patterns: dict[str, dict] = {}
        self._tool_sequences: list[tuple[str, ...]] = []
        # 异步加载持久化 pattern——调用方可在创建后立即 await engine._load_patterns()
        # 若未显式加载，observe_conversation 内部也会按需加载（幂等）
        self._patterns_loaded: bool = False

    async def _load_patterns(self) -> None:
        """Batch 2.6: 从数据库加载已持久化的 patterns（幂等）。"""
        if self._patterns_loaded:
            return
        try:
            from repositories.evolution_repo import load_patterns
            self._patterns = await load_patterns(self.db)
            self._patterns_loaded = True
        except Exception:
            _log.debug("Failed to load evolution patterns", exc_info=True)
            self._patterns = {}
            self._patterns_loaded = True

    async def _persist_pattern_state(self, sig: str) -> None:
        """Batch 2.6: 将单个 pattern 的内存状态写回数据库（用于 flag 变更时）。"""
        entry = self._patterns.get(sig)
        if not entry:
            return
        try:
            from repositories.evolution_repo import upsert_pattern
            await upsert_pattern(
                self.db, sig,
                count=entry["count"],
                examples=entry.get("examples", []),
                last_seen=entry.get("last_seen", 0.0),
                workflow_generated=int(entry.get("_workflow_generated", False)),
                skill_generated=int(entry.get("_skill_generated", False)),
                skill_exported=int(entry.get("_skill_exported", False)),
            )
        except Exception:
            _log.debug("Failed to persist pattern state %s", sig[:40], exc_info=True)

    # ============================================================
    # 1. 观察 —— 对话完成后提取模式
    # ============================================================

    async def observe_conversation(self, session_id: str, task_prompt: str,
                                     agent_output: str, tool_calls: list[dict],
                                     accepted: bool = True) -> dict:
        """对话完成后分析——提取可复用的模式。

        返回: {"new_patterns": N, "promoted_skills": [], "promoted_workflows": []}
        """
        await self._load_patterns()
        result = {"new_patterns": 0, "promoted_skills": [], "promoted_workflows": []}

        # 1) 提取工具组合模式
        if tool_calls:
            tool_names = tuple(sorted(set(tc.get("function", {}).get("name", "")
                                          for tc in tool_calls if tc.get("function"))))
            if len(tool_names) >= 2:
                sig = f"tools:{'→'.join(tool_names)}"
                result["new_patterns"] += await self._count_pattern(sig, task_prompt, agent_output)

            # 工具序列
            seq = tuple(tc.get("function", {}).get("name", "")
                       for tc in tool_calls if tc.get("function"))
            if len(seq) >= 3:
                sig = f"sequence:{'→'.join(seq[:SELF_EVOLUTION_TOOL_SEQUENCE_MAX_LEN])}"
                result["new_patterns"] += await self._count_pattern(sig, task_prompt, agent_output)

        # 2) 提取任务类型模式
        task_type = self._classify_task(task_prompt)
        sig = f"task_type:{task_type}"
        result["new_patterns"] += await self._count_pattern(sig, task_prompt, agent_output)

        # 3) 检查是否达到生成阈值
        if task_type and self._patterns.get(sig, {}).get("count", 0) >= self.AUTO_WORKFLOW_THRESHOLD:
            wf = await self._try_generate_workflow(task_type)
            if wf:
                result["promoted_workflows"].append(wf)

        return result

    async def _count_pattern(self, sig: str, prompt: str, output: str) -> int:
        """累积模式计数，并持久化到 evolution_patterns 表（Batch 2.6）。"""
        entry = self._patterns.setdefault(sig, {"count": 0, "examples": [], "last_seen": 0})
        entry["count"] += 1
        entry["examples"].append(prompt[:SELF_EVOLUTION_PATTERN_EXAMPLE_PREVIEW_CHARS])
        if len(entry["examples"]) > 10:
            entry["examples"] = entry["examples"][-10:]
        entry["last_seen"] = _time.time()

        # Batch 2.6: 计数/示例/状态实时持久化，解决重启丢计数问题
        try:
            from repositories.evolution_repo import upsert_pattern
            await upsert_pattern(
                self.db, sig,
                count=entry["count"],
                examples=entry["examples"],
                last_seen=entry["last_seen"],
                workflow_generated=int(entry.get("_workflow_generated", False)),
                skill_generated=int(entry.get("_skill_generated", False)),
                skill_exported=int(entry.get("_skill_exported", False)),
            )
        except Exception:
            _log.debug("Failed to persist evolution pattern %s", sig[:40], exc_info=True)

        # 持久化到 knowledge_nodes（模式=知识）供人类可读展示
        if entry["count"] >= self.PATTERN_CONFIDENCE_MIN:
            await self._persist_pattern(sig, entry)
        return 1

    async def _persist_pattern(self, sig: str, entry: dict) -> None:
        """将高频模式持久化到 knowledge_nodes。"""
        try:
            title = f"pattern:{sig[:SELF_EVOLUTION_PATTERN_TITLE_MAX_CHARS]}"
            content = f"出现 {entry['count']} 次。示例: {'; '.join(entry['examples'][-3:])}"
            await self.db.execute(
                "INSERT OR REPLACE INTO knowledge_nodes(id, title, content, source, score, tags, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (sig[:SELF_EVOLUTION_PATTERN_ID_MAX_CHARS], title, content[:SELF_EVOLUTION_PATTERN_CONTENT_PREVIEW_CHARS], "self_evolution",
                 min(1.0, entry["count"] / self.AUTO_SKILL_THRESHOLD),
                 _json.dumps(["auto_pattern"]),
                 int(_time.time()), int(_time.time())),
            )
            await self.db.commit()
        except Exception:
            _log.debug("Unexpected error", exc_info=True)

    async def observe_openclaw_interaction(
        self, tool_name: str, args: dict | None = None,
        result: dict | None = None,
    ) -> None:
        """Phase 43 H.4: 观察 OpenClaw 工具交互——跨后端模式识别。

        使用已有 _count_pattern() API。
        跨后端模式（hermes+openclaw 协作）→ 自动生成 agent=openclaw 标记的 Skill。
        """
        await self._load_patterns()
        sig = f"oc:{tool_name}"
        args_str = _json.dumps(args or {}, ensure_ascii=False)[:SELF_EVOLUTION_OC_INTERACTION_PREVIEW_CHARS]
        result_str = _json.dumps(result or {}, ensure_ascii=False)[:SELF_EVOLUTION_OC_INTERACTION_PREVIEW_CHARS]
        await self._count_pattern(sig, args_str, result_str)

        # 跨后端模式: hermes 委托 OC → 标识为 cross_backend
        if tool_name in ("web_search", "openclaw_vision", "web_fetch", "pdf_extract"):
            cross_sig = f"cross_backend:hermes→oc:{tool_name}"
            await self._count_pattern(cross_sig, args_str, result_str)

    @staticmethod
    def _classify_task(prompt: str) -> str:
        """快速任务分类——用于模式聚类。"""
        p = prompt.lower()
        if any(w in p for w in ["review", "审查", "review"]): return "code_review"
        if any(w in p for w in ["test", "测试", "coverage"]): return "testing"
        if any(w in p for w in ["fix", "修复", "bug", "debug", "调试"]): return "bug_fix"
        if any(w in p for w in ["doc", "文档", "readme"]): return "documentation"
        if any(w in p for w in ["refactor", "重构"]): return "refactor"
        if any(w in p for w in ["deploy", "部署", "build", "构建"]): return "deploy"
        if any(w in p for w in ["api", "接口", "endpoint"]): return "api_dev"
        if any(w in p for w in ["security", "安全", "漏洞"]): return "security"
        if any(w in p for w in ["optimize", "优化", "性能", "performance"]): return "optimization"
        return "general"

    # ============================================================
    # Batch 5.3: 质量门控辅助方法
    # ============================================================

    async def _find_baseline_graph(self, task_type: str, current_workflow_id: str) -> dict | None:
        """查找同 task_type 已生成的其他 workflow 作为 A/B baseline。"""
        if not self._workflow_service:
            return None
        try:
            cursor = await self.db.execute(
                "SELECT id, graph FROM workflows WHERE name=? AND id!=? ORDER BY created_at DESC LIMIT 1",
                (f"自动生成: {task_type}", current_workflow_id),
            )
            row = await cursor.fetchone()
            if row:
                graph = _json.loads(row[1]) if isinstance(row[1], str) else row[1]
                return graph if isinstance(graph, dict) else None
        except Exception:
            _log.debug("Failed to find baseline graph for %s", task_type, exc_info=True)
        return None

    async def _run_quality_gate(
        self,
        task_type: str,
        graph: dict,
        examples: list[str],
        skill_dir: str,
        workflow_id: str,
    ) -> dict:
        """对生成的 workflow 跑 dry-run + A/B 验证，创建 pending install 记录。"""
        pattern_signature = f"task_type:{task_type}"

        if not SELF_EVOLUTION_QUALITY_GATE_ENABLED:
            evaluation = {"passed": True, "score": 1.0, "reason": "quality_gate_disabled"}
            sandbox_result: dict = {}
            ab_result: dict = {}
            status = "pending"
        else:
            sample_inputs = [
                ex[:SELF_EVOLUTION_PATTERN_EXAMPLE_PREVIEW_CHARS]
                for ex in examples[-SELF_EVOLUTION_DRY_RUN_SAMPLE_COUNT:]
            ]
            if not sample_inputs:
                sample_inputs = [f"示例输入: {task_type}"]

            baseline_graph = await self._find_baseline_graph(task_type, workflow_id)

            try:
                sandbox_result = await self._quality_gate.dry_run(graph, sample_inputs)
                ab_result = await self._quality_gate.ab_validate(graph, baseline_graph, sample_inputs)
                evaluation = self._quality_gate.evaluate(sandbox_result, ab_result, graph)
            except Exception:
                _log.exception("Quality gate evaluation failed for %s", task_type)
                sandbox_result = {
                    "completion_rate": 0.0, "node_success_rate": 0.0,
                    "total_nodes": 0, "total_error_nodes": 0, "runs": [],
                }
                ab_result = {"wins": 0, "losses": 0, "ties": 0, "win_rate": 0.0}
                evaluation = {"passed": False, "score": 0.0, "reason": "quality_gate_evaluation_failed"}

            status = "pending" if evaluation.get("passed") else "auto_rejected"

        pending_id = str(_uuid.uuid4())
        try:
            from repositories.evolution_repo import create_pending_install
            await create_pending_install(
                self.db,
                pending_id=pending_id,
                pattern_signature=pattern_signature,
                task_type=task_type,
                workflow_id=workflow_id,
                skill_dir=skill_dir,
                examples=examples,
                sandbox_result=sandbox_result,
                ab_result=ab_result,
                evaluation=evaluation,
                status=status,
            )
        except Exception:
            _log.exception("Failed to create pending install record for %s", task_type)
            return {**evaluation, "pending_id": None}

        if status == "pending" and self._notification_center is not None:
            try:
                await self._notification_center.notify(
                    title=f"Self-Evolution 待审核: {task_type}",
                    body=(
                        f"AI 自动生成 workflow「{workflow_id}」评分 {evaluation.get('score', 0)}，"
                        "请在「待审核进化」面板审批。"
                    ),
                    type="evolution_pending",
                    level="info",
                    link="/evolution-pending",
                )
            except Exception:
                _log.debug("Failed to send evolution pending notification", exc_info=True)

        _log.info(
            "Self-evolution: workflow %s quality gate result: %s (status=%s)",
            workflow_id, evaluation.get("reason"), status,
        )
        return {**evaluation, "pending_id": pending_id}

    # ============================================================
    # 2. 提取 —— 模式 → Skill / Workflow
    # ============================================================

    async def _try_generate_workflow(self, task_type: str) -> dict | None:
        """当同一任务类型出现 ≥4 次，LLM 自动生成 DAG 模板。"""
        entry = self._patterns.get(f"task_type:{task_type}", {})
        if entry.get("count", 0) < self.AUTO_WORKFLOW_THRESHOLD:
            return None
        if entry.get("_workflow_generated"):
            return None

        examples = entry.get("examples", [])[-3:]
        if not examples or not self._model_config:
            return None

        try:
            adapter = self._adapters.get(DEFAULT_AGENT_ID)
            if not adapter:
                return None
            cfg = await self._model_config.get_active_config()
            if not cfg.get("api_key"):
                return None

            prompt = (
                f"用户反复执行「{task_type}」类型的任务。以下是最近 3 个示例:\n"
                + "\n".join(f"- {e}" for e in examples)
                + f"\n\n请生成一个 DAG 工作流 JSON。3-5 个节点，如果有需要用户审批的环节加 type:human 节点。只输出 JSON。"
            )
            result = await adapter.invoke(
                prompt,
                context={
                    "system": "Output a DAG workflow graph as JSON with nodes and edges. Use type:human for approval nodes.",
                    "model_config": cfg,
                    "max_tokens": MAX_TOKENS_SELF_EVOLUTION,
                    "temperature": 0.3,
                },
            )
            if result.get("error"):
                return None
            content = result.get("content", "")
            j = content.find("{")
            graph = _json.loads(content[j:content.rfind("}")+1]) if j >= 0 else None

            if graph and "nodes" in graph:
                import uuid
                wid = str(uuid.uuid4())[:12]
                name = f"自动生成: {task_type}"
                desc = f"AI 自动生成（{task_type} 类型任务出现 {entry['count']} 次后触发）"
                await self.db.execute(
                    "INSERT INTO workflows(id, name, description, graph, is_template, created_at, updated_at) "
                    "VALUES(?,?,?,?,1,?,?)",
                    (wid, name, desc, _json.dumps(graph, ensure_ascii=False),
                     int(_time.time()), int(_time.time())),
                )
                await self.db.commit()
                entry["_workflow_generated"] = True
                await self._persist_pattern_state(f"task_type:{task_type}")
                _log.info("Self-evolution: auto-generated workflow '%s' (%d nodes)", name, len(graph["nodes"]))

                # Phase 41: 同时导出 SKILL.md + skill.yaml（供后续人工审批后安装）
                skill_dir = ""
                try:
                    from pathlib import Path as _Path
                    skill_dir = _resolve_skill_export_dir(
                        self._skill_registry, task_type)
                    _Path(skill_dir).mkdir(parents=True, exist_ok=True)
                    (_Path(skill_dir) / "SKILL.md").write_text(
                        f"---\nname: {task_type}\ndescription: AI自动生成 — {task_type}"
                        f" 类型任务出现 {entry['count']} 次后触发\n---\n"
                        f"# {task_type}\n\n自动生成的工作流。包含 {len(graph['nodes'])} 个步骤。\n",
                        encoding="utf-8")
                    import yaml as _yaml
                    with open(_Path(skill_dir) / "skill.yaml", "w", encoding="utf-8") as f:
                        # 主键一律用 name：skill_registry 以 name 为准并做 SKILL_ID_PATTERN
                        # 白名单校验，写 id 会被拒收（技能永远装不上）。同时对齐
                        # agentskills.io 开放标准（该标准主键亦为 name）。
                        _yaml.dump({"name": f"auto-{task_type}", "version": "1.0.0",
                                    "title": f"自动生成: {task_type}",
                                    "description": f"AI自动生成 — {task_type} 类型任务模式",
                                    "category": "auto_generated",
                                    "tags": [task_type, "auto"],
                                    "workflows": [{"name": name, "dag_id": wid}]}, f,
                                   allow_unicode=True)
                    _log.info("Self-evolution: exported SKILL.md + skill.yaml for '%s'", task_type)
                    entry["_skill_exported"] = True
                    await self._persist_pattern_state(f"task_type:{task_type}")
                except Exception:
                    _log.debug("Skill export failed for %s", task_type, exc_info=True)

                # Batch 5.3: 质量门控——不再直接安装，改为 pending / auto_rejected
                quality_result = await self._run_quality_gate(
                    task_type, graph, examples, skill_dir, wid,
                )

                return {
                    "workflow_id": wid,
                    "name": name,
                    "nodes": len(graph["nodes"]),
                    "quality_gate": quality_result,
                }
        except Exception:
            _log.exception("Self-evolution: workflow generation failed for %s", task_type)
        return None

    # ============================================================
    # 3. 更新 —— 现有 Skill / Workflow 基于反馈优化
    # ============================================================

    async def update_skill_from_feedback(self, skill_id: str, usage_count: int) -> bool:
        """Skill 被使用后调整优先级。"""
        if not self._skill_registry:
            return False
        skill = self._skill_registry.get(skill_id)
        if not skill:
            return False
        # 提高 priority（更多使用 = 更高优先级）
        if skill.prompts:
            for sp in skill.prompts:
                sp.priority = min(95, sp.priority + max(1, usage_count // 5))
            _log.info("Skill '%s' priority boosted to %d (used %d times)", skill_id,
                      skill.prompts[0].priority if skill.prompts else 0, usage_count)
        return True

    # ============================================================
    # 4. 记忆 —— 长期知识沉淀
    # ============================================================

    async def periodic_pattern_cleanup(self) -> dict:
        """定期清理低分模式 + 重置 _workflow_generated 标记。"""
        await self._load_patterns()
        pruned = 0
        now = _time.time()
        stale_keys = []
        for sig, entry in self._patterns.items():
            # 30 天未出现 → 清除
            if now - entry.get("last_seen", 0) > 2_592_000:
                stale_keys.append(sig)

        # Batch 2.6: 同时从 DB 删除 stale patterns
        if stale_keys:
            try:
                from repositories.evolution_repo import delete_stale_patterns
                cutoff = now - 2_592_000
                db_pruned = await delete_stale_patterns(self.db, cutoff)
                pruned = db_pruned
            except Exception:
                _log.debug("Failed to delete stale patterns from DB", exc_info=True)
        for k in stale_keys:
            del self._patterns[k]

        # 重置 _workflow_generated 标记（允许重新生成更新版）并持久化
        for sig, entry in list(self._patterns.items()):
            if entry.get("_workflow_generated") and entry.get("count", 0) > self.AUTO_WORKFLOW_THRESHOLD * 2:
                entry["_workflow_generated"] = False  # 模式持续 → 生成更新版
                await self._persist_pattern_state(sig)

        if pruned:
            _log.info("Pattern cleanup: pruned %d stale patterns", pruned)
        return {"pruned_patterns": pruned}

    # ============================================================
    # 后台进化循环
    # ============================================================

    async def evolution_loop(self, interval: int = 600) -> None:
        """后台进化循环——每 10 分钟检查一次模式积累。"""
        while True:
            await asyncio.sleep(interval)
            try:
                # 定期清理
                await self.periodic_pattern_cleanup()

                # 检查是否有达到阈值可生成资产的模式
                for sig, entry in list(self._patterns.items()):
                    if entry.get("count", 0) >= self.AUTO_SKILL_THRESHOLD and not entry.get("_skill_generated"):
                        if sig.startswith("task_type:"):
                            # Workflow 已通过 _try_generate_workflow 覆盖
                            pass
                        elif sig.startswith("tools:") or sig.startswith("sequence:"):
                            # 工具序列模式 → 生成 workflow 模板
                            entry["_skill_generated"] = True
                            await self._persist_pattern_state(sig)
                            _log.info("Pattern '%s' reached threshold (%d occurrences), workflow already covered",
                                     sig, entry["count"])
            except Exception:
                _log.exception("Evolution loop iteration failed")
