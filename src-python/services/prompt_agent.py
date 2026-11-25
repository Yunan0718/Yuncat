"""services/prompt_agent.py —— Phase 74 PromptAgent 后台服务

职责：
1. recommend(task_input, agent_id, project_id) —— 调轻量 LLM 推荐角色/行为/工作流，失败时走规则兜底
2. render(role_ids, behavior_ids, workflow_template, ctx) —— 渲染最终注入 commander 的 prompt 片段
3. compose_behaviors(behavior_ids, project_lang, max_tokens) —— 按 priority 拼接 behavior，超预算截断

设计原则：
- 不直连 LLM（架构铁律 1）——通过注入的 adapter.invoke() 调用
- LLM 失败必有兜底（_rule_based_recommend）——保证 always returns a dict
- 5 分钟缓存——降低重复推荐成本
- confidence < 0.6 强制丢弃 workflow_template（避免低置信度误选）

依赖（lifespan 注入）：
- adapters: dict[agent_id, adapter] —— app.state.adapters
- model_config: ModelConfigService —— app.state.model_config
- library: PromptLibrary —— app.state.prompt_library（Phase 72）
"""
import json
import os
import time
from typing import Any

from config import (
    PROMPT_AGENT_CACHE_TTL_SEC,
    PROMPT_AGENT_CHARS_PER_TOKEN,
    PROMPT_AGENT_LOW_CONFIDENCE_THRESHOLD,
    PROMPT_AGENT_OPTIMIZE_CANDIDATE_COUNT,
    PROMPT_AGENT_OPTIMIZE_JSON_OVERHEAD_TOKENS,
    PROMPT_AGENT_OPTIMIZE_MAX_CHANGES,
    PROMPT_AGENT_OPTIMIZE_MAX_TOKENS_PER_CANDIDATE,
    PROMPT_AGENT_RECOMMEND_MAX_TOKENS,
    TEMPERATURE_PROMPT_AGENT_OPTIMIZE,
)

# ── 模块级常量 ──

_TASK_INPUT_PREFIX_LEN = 80  # 缓存 key 取 task_input 前 80 字符
_RECOMMEND_TEMPERATURE = 0.0
_PREF_KEY_BEHAVIOR_IDS = "behavior_ids"


class PromptAgent:
    """Prompt 推荐与渲染服务——Phase 74"""

    def __init__(self, conn, adapters: dict, model_config, library):
        """
        Args:
            conn: aiosqlite 连接（保留用于 Phase 76 项目偏好持久化）
            adapters: dict[agent_id, adapter]——hermes/openclaw adapter 实例
            model_config: ModelConfigService——获取激活模型配置
            library: PromptLibrary——聚合 roles/behaviors/workflows 目录（Phase 72）
        """
        self._conn = conn
        self._adapters = adapters
        self._model_config = model_config
        self._library = library
        # 缓存: (task_input[:80], agent_id, project_id) -> (result_dict, timestamp)
        self._cache: dict[tuple[str, str, str], tuple[dict, float]] = {}
        self._rules: dict = self._load_rules()
        self._recommend_template: str = self._load_recommend_template()
        self._optimize_template: str = self._load_optimize_template()

    # ═══════════════════════════════════════════════════════════
    # recommend —— LLM 推荐 + 缓存 + 兜底
    # ═══════════════════════════════════════════════════════════

    async def recommend(
        self,
        task_input: str,
        agent_id: str = "hermes",
        project_id: str = "",
    ) -> dict[str, Any]:
        """根据任务输入推荐 role/behavior/workflow

        Returns:
            {
                "role_ids": [...],
                "behavior_ids": [...],
                "workflow_template": str | None,
                "reason": str,
                "confidence": float,
                "source": "llm" | "rule"
            }
        """
        # 1. 缓存命中检查
        cache_key = (task_input[:_TASK_INPUT_PREFIX_LEN], agent_id, project_id)
        cached = self._cache.get(cache_key)
        if cached is not None:
            result, ts = cached
            if (time.time() - ts) < PROMPT_AGENT_CACHE_TTL_SEC:
                return result

        # 2. 取 adapter
        adapter = self._adapters.get(agent_id)
        if adapter is None:
            return self._rule_based_with_source(task_input)

        # 3. 取 model_config（async）
        try:
            cfg = await self._model_config.get_active_config(agent_id)
        except Exception:
            return self._rule_based_with_source(task_input)

        # 4. 预取可选项（roles=async, behaviors/workflows=sync）
        try:
            roles = await self._library.list_roles()
        except Exception:
            roles = []
        try:
            behaviors = self._library.list_behaviors()
        except Exception:
            behaviors = []
        try:
            workflows = self._library.list_workflows()
        except Exception:
            workflows = []

        # 5. 构建 prompt
        prompt = self._build_recommend_prompt(task_input, roles, behaviors, workflows)

        # 6. 调 LLM
        try:
            result = await adapter.invoke(
                prompt,
                context={
                    "model_config": cfg,
                    "max_tokens": PROMPT_AGENT_RECOMMEND_MAX_TOKENS,
                    "temperature": _RECOMMEND_TEMPERATURE,
                },
            )
        except Exception:
            return self._rule_based_with_source(task_input)

        # 7. 检查 adapter 错误返回
        if "error" in result:
            return self._rule_based_with_source(task_input)

        # 8. 解析 JSON
        content = result.get("content", "")
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError, TypeError):
            return self._rule_based_with_source(task_input)

        # 9. 规范化 + 低置信度处理
        normalized = self._normalize_llm_result(parsed)

        # 10. 写缓存
        self._cache[cache_key] = (normalized, time.time())

        return normalized

    # ═══════════════════════════════════════════════════════════
    # render —— 渲染最终 prompt 片段
    # ═══════════════════════════════════════════════════════════

    def render(
        self,
        role_ids: list[str],
        behavior_ids: list[str],
        workflow_template: str | None = None,
        ctx: dict | None = None,
    ) -> str:
        """渲染最终注入 commander prompt 的片段：role + behavior + workflow

        Args:
            role_ids: 角色 id 列表（从 PromptLibrary.get_role 取 prompt_template）
            behavior_ids: 行为约束 id 列表（compose_behaviors 拼接）
            workflow_template: 工作流模板 name（可选）
            ctx: 保留参数（Phase 75+ 用于注入 extra_context）
        """
        parts: list[str] = []

        # role prompt_template
        for rid in role_ids:
            role = self._safe_get_role(rid)
            if role:
                tmpl = role.get("prompt_template", "")
                name = role.get("name", rid)
                if tmpl:
                    parts.append(f"# Role: {name}\n{tmpl}")

        # behavior
        behavior_text = self.compose_behaviors(behavior_ids)
        if behavior_text:
            parts.append(f"# Behaviors\n{behavior_text}")

        # workflow
        if workflow_template:
            wf = self._safe_get_workflow(workflow_template)
            if wf:
                desc = wf.get("description") or wf.get("display_name") or workflow_template
                parts.append(f"# Workflow: {workflow_template}\n{desc}")

        return "\n\n---\n\n".join(parts) if parts else ""

    # ═══════════════════════════════════════════════════════════
    # optimize —— Phase 73-A 输入框 ✨ 优化
    # ═══════════════════════════════════════════════════════════

    async def optimize(self, raw_prompt: str, agent_id: str = "hermes") -> dict[str, Any]:
        """优化用户原始 prompt——生成多候选并按质量评分

        Returns:
            成功: {
                "optimized": str,
                "changes": list[str],
                "candidates": [
                    {"optimized": str, "changes": list[str], "score": float},
                    ...
                ]
            }
            失败: {"error": str, "optimized": raw_prompt, "changes": [], "candidates": []}
            空 prompt: {"optimized": "", "changes": [], "candidates": []}
        """
        if not raw_prompt or not raw_prompt.strip():
            return {"optimized": "", "changes": [], "candidates": []}

        adapter = self._adapters.get(agent_id)
        if adapter is None:
            return {"error": "Agent 未就绪", "optimized": raw_prompt, "changes": [], "candidates": []}

        try:
            cfg = await self._model_config.get_active_config(agent_id)
        except Exception:
            return {"error": "模型配置获取失败", "optimized": raw_prompt, "changes": [], "candidates": []}

        prompt = self._build_optimize_prompt(raw_prompt)
        max_tokens = _calc_optimize_max_tokens(PROMPT_AGENT_OPTIMIZE_CANDIDATE_COUNT)

        try:
            result = await adapter.invoke(
                prompt,
                context={
                    "model_config": cfg,
                    "max_tokens": max_tokens,
                    "temperature": TEMPERATURE_PROMPT_AGENT_OPTIMIZE,
                },
            )
        except Exception:
            return {"error": "LLM 调用失败", "optimized": raw_prompt, "changes": [], "candidates": []}

        if "error" in result:
            return {"error": "LLM 返回错误", "optimized": raw_prompt, "changes": [], "candidates": []}

        parsed = _parse_optimize_json(result.get("content", ""))
        if parsed is None:
            return {"error": "LLM 响应解析失败", "optimized": raw_prompt, "changes": [], "candidates": []}

        candidates = _normalize_candidates(parsed, PROMPT_AGENT_OPTIMIZE_MAX_CHANGES)
        if not candidates:
            return {"error": "LLM 返回空结果", "optimized": raw_prompt, "changes": [], "candidates": []}

        best = candidates[0]
        return {
            "optimized": best["optimized"],
            "changes": best["changes"],
            "candidates": candidates,
        }

    def _build_optimize_prompt(self, raw_prompt: str) -> str:
        """填充优化 prompt 模板"""
        template = self._optimize_template or ""
        if not template:
            return f"优化以下 prompt，返回 JSON {{\"optimized\": \"...\", \"changes\": [...], \"candidates\": [{{\"optimized\": \"...\", \"changes\": [...], \"score\": 0.9}}]}}：\n\n{raw_prompt}"
        return template.format(raw_prompt=raw_prompt, candidate_count=PROMPT_AGENT_OPTIMIZE_CANDIDATE_COUNT)


    # ═══════════════════════════════════════════════════════════
    # compose_behaviors —— 拼接 + 截断
    # ═══════════════════════════════════════════════════════════

    def compose_behaviors(
        self,
        behavior_ids: list[str],
        project_lang: str | None = None,
        max_tokens: int = 800,
    ) -> str:
        """按顺序拼接 behavior content，超 max_tokens 截断

        Args:
            behavior_ids: behavior id 列表
            project_lang: 项目语言（Phase 75 引入 priority 后用于过滤 applies_to，此处保留参数）
            max_tokens: token 预算上限（粗略估算 len//3）
        """
        if not behavior_ids:
            return ""

        behavior_map = self._load_behavior_map()
        if not behavior_map:
            return ""

        parts: list[str] = []
        total_tokens = 0
        for bid in behavior_ids:
            content = behavior_map.get(bid)
            if content is None:
                continue
            est_tokens = len(content) // PROMPT_AGENT_CHARS_PER_TOKEN
            if total_tokens + est_tokens > max_tokens:
                # 截断到剩余预算
                remaining_tokens = max_tokens - total_tokens
                if remaining_tokens > 20:  # 至少 20 token 才值得加截断块
                    char_budget = remaining_tokens * PROMPT_AGENT_CHARS_PER_TOKEN
                    parts.append(content[:char_budget])
                break
            parts.append(content)
            total_tokens += est_tokens

        return "\n\n".join(parts)

    # ═══════════════════════════════════════════════════════════
    # Phase 76-A: 项目级 behavior 偏好持久化
    #
    # 优先级：current_override > project_preferences > role default
    # - current_override：本次显式选择（用户手动覆盖），最高优先级
    # - project_preferences：项目级记忆（同项目下次自动复用）
    # - role default：角色种子默认值（architect→["simplicity","naming"] 等）
    # ═══════════════════════════════════════════════════════════

    async def resolve_behavior_ids(
        self,
        role_id: str | None = None,
        project_id: str = "",
        current_override: list[str] | None = None,
    ) -> list[str]:
        """解析最终生效的 behavior id 列表

        优先级：current_override > project_preferences > role default

        Args:
            role_id: 角色 id（取 default_behaviors 兜底）
            project_id: 项目 id（查 project_preferences）
            current_override: 本次显式选择，最高优先级

        Returns:
            behavior id 列表（可能为空——三层都无配置时）
        """
        # 1. current_override 非空 → 直接返回（去重保序）
        if current_override:
            seen: set[str] = set()
            result: list[str] = []
            for bid in current_override:
                if bid and bid not in seen:
                    seen.add(bid)
                    result.append(bid)
            return result

        # 2. project_preferences
        if project_id:
            try:
                cursor = await self._conn.execute(
                    "SELECT value FROM project_preferences WHERE project_id=? AND key=?",
                    (project_id, _PREF_KEY_BEHAVIOR_IDS),
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    try:
                        ids = json.loads(row[0])
                        if isinstance(ids, list) and ids:
                            return [str(x) for x in ids if x]
                    except (json.JSONDecodeError, TypeError):
                        pass
            except Exception:
                pass  # best-effort——失败回退到 role default

        # 3. role default_behaviors
        if role_id:
            try:
                cursor = await self._conn.execute(
                    "SELECT default_behaviors FROM roles WHERE id=?", (role_id,)
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    try:
                        ids = json.loads(row[0])
                        if isinstance(ids, list):
                            return [str(x) for x in ids if x]
                    except (json.JSONDecodeError, TypeError):
                        pass
            except Exception:
                pass

        return []

    async def save_project_behavior_preference(
        self,
        project_id: str,
        behavior_ids: list[str],
    ) -> None:
        """保存项目级 behavior 偏好（UPSERT 幂等）

        Args:
            project_id: 项目 id（空串则跳过——不保存）
            behavior_ids: behavior id 列表
        """
        if not project_id:
            return
        # 去重保序后存储
        seen: set[str] = set()
        unique_ids: list[str] = []
        for bid in behavior_ids:
            if bid and bid not in seen:
                seen.add(bid)
                unique_ids.append(bid)
        value = json.dumps(unique_ids)
        await self._conn.execute(
            "INSERT INTO project_preferences(project_id, key, value, updated_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(project_id, key) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at",
            (project_id, _PREF_KEY_BEHAVIOR_IDS, value, int(time.time())),
        )
        await self._conn.commit()

    # ═══════════════════════════════════════════════════════════
    # _rule_based_recommend —— 关键词匹配兜底
    # ═══════════════════════════════════════════════════════════

    def _rule_based_recommend(self, task_input: str) -> dict[str, Any]:
        """关键词匹配兜底——按 rules 列表顺序首个命中胜出"""
        task_lower = task_input.lower()
        for rule in self._rules.get("rules", []) or []:
            for kw in rule.get("keywords", []) or []:
                if kw.lower() in task_lower:
                    return {
                        "role_ids": list(rule.get("role_ids", []) or []),
                        "behavior_ids": list(rule.get("behavior_ids", []) or []),
                        "workflow_template": rule.get("workflow_template"),
                        "reason": rule.get("reason", ""),
                        "confidence": float(rule.get("confidence", 0.5)),
                    }
        # default
        d = self._rules.get("default", {}) or {}
        return {
            "role_ids": list(d.get("role_ids", []) or []),
            "behavior_ids": list(d.get("behavior_ids", []) or []),
            "workflow_template": d.get("workflow_template"),
            "reason": d.get("reason", ""),
            "confidence": float(d.get("confidence", 0.4)),
        }

    def _rule_based_with_source(self, task_input: str) -> dict[str, Any]:
        """兜底 + source='rule' 标记"""
        result = self._rule_based_recommend(task_input)
        result["source"] = "rule"
        return result

    # ═══════════════════════════════════════════════════════════
    # 内部 helper
    # ═══════════════════════════════════════════════════════════

    def _normalize_llm_result(self, parsed: dict) -> dict[str, Any]:
        """规范化 LLM 返回 + 低置信度丢弃 workflow"""
        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        workflow = parsed.get("workflow_template")
        if confidence < PROMPT_AGENT_LOW_CONFIDENCE_THRESHOLD:
            workflow = None
        return {
            "role_ids": list(parsed.get("role_ids", []) or []),
            "behavior_ids": list(parsed.get("behavior_ids", []) or []),
            "workflow_template": workflow,
            "reason": str(parsed.get("reason", "")),
            "confidence": confidence,
            "source": "llm",
        }

    def _build_recommend_prompt(
        self,
        task_input: str,
        roles: list[dict],
        behaviors: list[dict],
        workflows: list[dict],
    ) -> str:
        """填充推荐 prompt 模板"""
        roles_list = "\n".join(f"- {r.get('id', '?')}: {r.get('name', '')}" for r in roles) or "(无)"
        behaviors_list = "\n".join(f"- {b.get('id', '?')}: {b.get('description', '')}" for b in behaviors) or "(无)"
        workflows_list = "\n".join(f"- {w.get('id', '?')}: {w.get('name', '')}" for w in workflows) or "(无)"

        template = self._recommend_template or ""
        if not template:
            # 模板缺失时的最小兜底
            return (
                f"任务: {task_input}\n\n可选角色:\n{roles_list}\n\n"
                f"可选行为:\n{behaviors_list}\n\n可选工作流:\n{workflows_list}\n\n返回 JSON。"
            )

        # 模板用 {var} 占位（注意 {{ }} 是字面大括号）
        return template.format(
            roles_list=roles_list,
            behaviors_list=behaviors_list,
            workflows_list=workflows_list,
            task_input=task_input,
        )

    def _load_behavior_map(self) -> dict[str, str]:
        """加载 behavior id → content 映射"""
        try:
            all_behaviors = self._library.list_behaviors()
        except Exception:
            return {}
        return {b["id"]: b.get("content", "") for b in all_behaviors if b.get("id")}

    def _safe_get_role(self, role_id: str) -> dict | None:
        """同步安全取 role——list_roles 是 async，这里从 YAML 直接取（同步路径）"""
        # PromptLibrary.get_role 是 async，这里 render 是同步的
        # 改用 list_roles 预取不可行（render 可能被独立调用），所以直接从 YAML 加载
        try:
            from services.prompt_library import PromptLibrary
            # PromptLibrary._load_yaml_roles 是静态方法，可直接调用
            yaml_roles = PromptLibrary._load_yaml_roles()
            for r in yaml_roles:
                if r.get("id") == role_id:
                    return r
        except Exception:
            pass
        return None

    def _safe_get_workflow(self, name: str) -> dict | None:
        """同步安全取 workflow_template"""
        try:
            return self._library.get_workflow_template(name)
        except Exception:
            return None

    def _load_rules(self) -> dict:
        """加载 prompt_agent_rules.yaml"""
        import yaml
        rules_path = os.path.join(os.path.dirname(__file__), "prompt_agent_rules.yaml")
        try:
            with open(rules_path, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {"rules": [], "default": {}}

    def _load_recommend_template(self) -> str:
        """加载 prompts/prompt_agent_recommend.txt"""
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "prompts",
            "prompt_agent_recommend.txt",
        )
        try:
            with open(template_path, encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""

    def _load_optimize_template(self) -> str:
        """加载 prompts/prompt_optimize.txt（Phase 73-A）"""
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "prompts",
            "prompt_optimize.txt",
        )
        try:
            with open(template_path, encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""


# ═══════════════════════════════════════════════════════════
# Prompt optimize 辅助函数（模块级，便于独立测试）
# ═══════════════════════════════════════════════════════════


def _calc_optimize_max_tokens(candidate_count: int) -> int:
    """按候选数动态计算总 token 预算"""
    return (
        candidate_count * PROMPT_AGENT_OPTIMIZE_MAX_TOKENS_PER_CANDIDATE
        + PROMPT_AGENT_OPTIMIZE_JSON_OVERHEAD_TOKENS
    )


def _parse_optimize_json(content: str) -> dict | None:
    """安全解析 LLM 返回的 JSON"""
    if not content or not isinstance(content, str):
        return None
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_candidates(parsed: Any, max_changes: int) -> list[dict]:
    """解析并规范化候选列表，按 score 降序返回"""
    raw_candidates = parsed.get("candidates") if isinstance(parsed, dict) else None
    candidates: list[dict] = []

    if isinstance(raw_candidates, list):
        for item in raw_candidates:
            normalized = _normalize_candidate(item, max_changes)
            if normalized:
                candidates.append(normalized)

    # 兼容旧格式：没有 candidates 时把根字段包装成单候选
    if not candidates and isinstance(parsed, dict):
        optimized = str(parsed.get("optimized", "")).strip()
        changes = parsed.get("changes", [])
        if optimized:
            normalized = _normalize_candidate(
                {"optimized": optimized, "changes": changes, "score": 1.0},
                max_changes,
            )
            if normalized:
                candidates.append(normalized)

    return _sort_candidates(candidates)


def _normalize_candidate(item: Any, max_changes: int) -> dict | None:
    """把单个候选条目规范化；无效条目返回 None"""
    if not isinstance(item, dict):
        return None
    optimized = str(item.get("optimized", "")).strip()
    if not optimized:
        return None

    changes = item.get("changes", [])
    if not isinstance(changes, list):
        changes = []
    changes = [str(c) for c in changes if c][:max_changes]

    return {
        "optimized": optimized,
        "changes": changes,
        "score": _clamp_score(item.get("score", 0.0)),
    }


def _clamp_score(score: Any) -> float:
    """把 score 限制在 [0.0, 1.0] 并转为 float"""
    try:
        value = float(score)
    except (TypeError, ValueError):
        value = 0.0
    return max(0.0, min(1.0, value))


def _sort_candidates(candidates: list[dict]) -> list[dict]:
    """按 score 降序排列候选"""
    return sorted(candidates, key=lambda c: c.get("score", 0.0), reverse=True)
