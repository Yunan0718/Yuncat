"""services/intent_service.py —— 意图路由引擎（IntentRouter）

Phase 33: LLM 分类器 + 正则保底。首次用 LLM 分析意图，LLM 不可用时回退正则。
"""
import hashlib
import logging
import re
import time as _time
from typing import Any

from config import (
    DEFAULT_AGENT_ID, OPENCLAW_AGENT_ID, EXTERNAL_AGENT_IDS,
    MAX_TOKENS_INTENT_CLASSIFY, ADAPTER_HEALTH_CHECK_TIMEOUT,
    INTENT_CLASSIFY_INPUT_MAX_CHARS, INTENT_REASONING_PREVIEW_CHARS,
    INTENT_LLM_CACHE_TTL_SEC,
)
from services.dynamic_orchestrator import _extract_json

_log = logging.getLogger("yuncat.intent")

# Batch 4.3: @-mention 识别——与前端 context-pills.ts 保持类型一致
# 供 analyze() 在 route_info 中透传，便于日志/审计/后续路由决策
MENTION_TYPES = ("agent", "memory", "file", "session", "folder", "web", "git")
# value 终止于空白或 CJK/全角标点，避免把后续中文句子吞入 value
MENTION_VALUE_PATTERN = r"[^\s\u3000-\u303F\uFF00-\uFFEF]+"
MENTION_REGEX = re.compile(rf"@({'|'.join(MENTION_TYPES)}):({MENTION_VALUE_PATTERN})")


def extract_mentions(prompt: str) -> list[dict[str, str]]:
    """从用户输入提取 @type:value 上下文引用。

    仅做格式验证，不调用文件/网络 IO；实际解析注入由前端 context-pills 完成。
    返回列表按出现顺序排列，重复项保留（便于前端去重）。
    """
    return [
        {"type": match.group(1), "value": match.group(2), "raw": match.group(0)}
        for match in MENTION_REGEX.finditer(prompt or "")
    ]


# v31: 正则预编译——LLM 不可用时作为保底方案
COMPILED_RULES = [
    (re.compile(r"(分析|处理|解析).*(CSV|csv|Excel|表格|数据)", re.IGNORECASE),
     {"type": "workflow", "template": "data_analysis", "agent_id": DEFAULT_AGENT_ID}),
    (re.compile(r"(代码审查|review|审查|检查.*代码)", re.IGNORECASE),
     {"type": "workflow", "template": "code_review", "agent_id": DEFAULT_AGENT_ID}),
    (re.compile(r"(生成文档|写文档|文档|README)", re.IGNORECASE),
     {"type": "workflow", "template": "doc_generation", "agent_id": DEFAULT_AGENT_ID}),
    (re.compile(r"(安全审查|安全审计|漏洞|安全检查)", re.IGNORECASE),
     {"type": "workflow", "template": "security_review", "agent_id": DEFAULT_AGENT_ID}),
    (re.compile(r"(写代码|编程|实现|开发|函数|脚本|bug|修复|重构)", re.IGNORECASE),
     {"type": "direct", "agent_id": DEFAULT_AGENT_ID}),
    (re.compile(r"(执行|运行|启动|重启|停止).*(命令|服务|脚本|程序)", re.IGNORECASE),
     {"type": "direct", "agent_id": DEFAULT_AGENT_ID}),
    (re.compile(r"(搜索|查找|查询|检索|上网)", re.IGNORECASE),
     {"type": "direct", "agent_id": DEFAULT_AGENT_ID}),
    (re.compile(r"(文件|读取|打开|编辑|写入|保存|创建)", re.IGNORECASE),
     {"type": "direct", "agent_id": DEFAULT_AGENT_ID}),
]

DEFAULT_ROUTE = {"type": "direct", "agent_id": DEFAULT_AGENT_ID}

DEFAULT_RULES = [
    {
        "id": "builtin_confirm_write",
        "description": "所有文件写入需要用户二次确认",
        "trigger_on": ["tool:write_file"],
        "action": "require_confirmation",
        "enabled": True,
    },
    {
        "id": "builtin_backup_before_edit",
        "description": "编辑文件前自动备份到 .yuncat_backup/",
        "trigger_on": ["tool:write_file"],
        "action": "auto_backup",
        "enabled": False,
    },
]

# Phase 33: LLM 分类 prompt（含工作流路由建议）
# 2026-09-10: 移除 "hermes"——Hermes 已下线，全部能力回归 Yuncat 原生。
# 自动路由只允许建议原生 Yuncat；视觉/搜索/通讯类需求通过 delegate_to_agent("openclaw") 委托，
# 不由意图分类器直接选外部 Agent（避免判给未启动的外部网关导致静默失败）。
INTENT_CLASSIFY_PROMPT = """Analyze this user request and output JSON:
{{
  "category": "code|data|review|doc|search|command|security|debug|refactor|test|general",
  "suggested_agent": "yuncat",
  "complexity": "low|medium|high",
  "suggest_workflow": "data_analysis|code_review|doc_generation|security_review|auto_fix|tdd_loop|diagnose_loop|null",
  "suggested_tools": ["tool1","tool2"],
  "reasoning": "one sentence"
}}

Agent guide: yuncat=原生主 Agent（代码+shell+files+推理，自身直连 Provider）.
Workflow guide: data_analysis=CSV/charts, code_review=review PRs, doc_generation=docs, security_review=audit, auto_fix=lint+test fix, tdd_loop=test-first, diagnose_loop=debug bugs.

Request: {prompt}"""


class IntentService:
    """意图分析 + 路由分发。Phase 33: LLM 优先，正则保底。"""

    def __init__(self, agent_service=None, workflow_service=None, rules_repo=None, llm_classifier=None, deliberation_service=None, model_config=None, adapters=None, prompt_agent=None):
        self.agent_service = agent_service
        self.workflow_service = workflow_service
        self.rules_repo = rules_repo
        self._llm_classifier = llm_classifier  # v41: 可选 LLM 分类器回调
        self._deliberation = deliberation_service  # v58: Phase 9 讨论引擎
        self._model_config = model_config  # Phase 33: 模型配置（供 LLM 分类用）
        self._adapters = adapters or {}  # Phase 47: 默认 Agent Adapter 路由
        self._prompt_agent = prompt_agent  # Phase 88: PromptAgent 串联（lazy 注入）
        # Batch 5a C7: LLM 意图分类结果缓存（prompt 前 128 字符 hash → (时间戳, 结果)）
        self._llm_cache: dict[str, tuple[float, dict[str, str]]] = {}

    def set_prompt_agent(self, prompt_agent) -> None:
        """Phase 88: lazy 注入 PromptAgent

        lifespan 中 PromptAgent 创建晚于 IntentService，需在创建后回填。
        """
        self._prompt_agent = prompt_agent

    async def get_active_rules(self) -> list[dict]:
        if self.rules_repo:
            user_rules = await self.rules_repo.get_enabled_rules()
            return DEFAULT_RULES + user_rules
        return DEFAULT_RULES

    async def analyze(self, prompt: str, session_id: str | None = None) -> dict[str, Any]:
        active_rules = await self.get_active_rules()
        rules_context = "\n".join(
            f"[规则] {r['description']}" for r in active_rules if r.get("enabled", True)
        )
        route = await self._classify(prompt)
        reason = route.get("_reason", "默认路由")
        complexity = None
        should_deliberate = False
        if self._deliberation:
            complexity = self._deliberation.assess_complexity(prompt)
            should_deliberate = complexity.get("level") in ("high", "extreme")
        # Phase 88: PromptAgent 推荐——best-effort，在 _dispatch 之前调
        # recommend() 结果通过 _dispatch → invoke → start_task → build_agent_context 透传
        prompt_recommendation = None
        if self._prompt_agent:
            try:
                agent_id = route.get("agent_id", DEFAULT_AGENT_ID)
                prompt_recommendation = await self._prompt_agent.recommend(prompt, agent_id=agent_id)
            except Exception:
                _log.debug("PromptAgent recommend failed", exc_info=True)
        result = await self._dispatch(
            route, prompt, rules_context, reason=reason,
            prompt_recommendation=prompt_recommendation,
        )
        if complexity:
            result["complexity"] = complexity
        if should_deliberate:
            result["should_deliberate"] = True
        return result

    async def _classify(self, prompt: str) -> dict[str, str]:
        """Batch 5a C7: 正则/关键词优先（零首 token 延迟），仅无命中时回退 LLM。

        壳不调 LLM 铁律下，意图分类默认走本地规则；规则未命中且 LLM 可用时才分类，
        并以 TTL 缓存降低重复调用。
        """
        # 1) 正则/关键词分类——命中即高置信，直接返回
        for compiled_re, route in COMPILED_RULES:
            if compiled_re.search(prompt):
                agent_id = route.get("agent_id")
                if agent_id and self.agent_service:
                    running = await self.agent_service.is_running(agent_id)
                    if running:
                        result = dict(route)
                        result["_reason"] = "规则匹配"
                        return result
                result = dict(DEFAULT_ROUTE)
                result["_reason"] = f"{agent_id.capitalize()} 未就绪，降级到 {DEFAULT_AGENT_ID.capitalize()}"
                return result
        # 2) 规则未命中 → LLM（带 TTL 缓存）
        llm_result = await self._llm_classify_cached(prompt)
        if llm_result:
            llm_result["_reason"] = f"LLM: {llm_result.get('reasoning', 'classified')}"
            return llm_result
        # 3) LLM 不可用/失败 → 默认路由
        result = dict(DEFAULT_ROUTE)
        result["_reason"] = "默认路由"
        return result

    async def _llm_classify(self, prompt: str) -> dict[str, str] | None:
        """用当前激活模型做轻量意图分类（~200 tokens prompt）。"""
        try:
            adapter = self._adapters.get(DEFAULT_AGENT_ID)
            if not adapter:
                return None
            cfg = {}
            if self._model_config:
                cfg = await self._model_config.get_active_config()
            if not cfg.get("api_key"):
                return None

            result = await adapter.invoke(
                INTENT_CLASSIFY_PROMPT.format(prompt=prompt[:INTENT_CLASSIFY_INPUT_MAX_CHARS]),
                context={
                    "system": "You are an intent classifier. Output valid JSON only.",
                    "model_config": cfg,
                    "max_tokens": MAX_TOKENS_INTENT_CLASSIFY,
                    "temperature": 0.0,
                },
            )
            if result.get("error"):
                return None
            content = result.get("content", "")
            # Extract JSON（复用 dynamic_orchestrator 的解析器，避免两套实现）
            parsed = _extract_json(content) or {}
            # LLM 可能建议走工作流
            wf = parsed.get("suggest_workflow")
            # 外部 Agent 禁止自动路由：意图分类只建议原生 Yuncat，视觉/搜索/通讯需求
            # 由 Yuncat 经 delegate_to_agent("openclaw") 委托。LLM 若返回外部 Agent ID，
            # 一律降级为原生 Yuncat，避免判给未启动的外部网关导致静默失败。
            suggested = parsed.get("suggested_agent", DEFAULT_AGENT_ID)
            if suggested in EXTERNAL_AGENT_IDS:
                suggested = DEFAULT_AGENT_ID
            if wf and wf in ("data_analysis", "code_review", "doc_generation",
                             "security_review", "auto_fix", "tdd_loop", "diagnose_loop"):
                return {
                    "type": "workflow",
                    "template": wf,
                    "agent_id": suggested,
                    "category": parsed.get("category", "general"),
                    "reasoning": parsed.get("reasoning", "")[:INTENT_REASONING_PREVIEW_CHARS],
                }
            return {
                "type": "direct",
                "agent_id": suggested,
                "category": parsed.get("category", "general"),
                "confidence": parsed.get("complexity", "medium"),
                "reasoning": parsed.get("reasoning", "")[:200],
            }
        except Exception:
            _log.warning("LLM intent classify unavailable, using regex fallback", exc_info=True)
        return None

    async def _llm_classify_cached(self, prompt: str) -> dict[str, str] | None:
        """Batch 5a C7: 带 TTL 内存缓存的 LLM 分类——按 prompt 前 128 字符 hash 去重。

        仅在规则未命中时调用，避免重复/相似 prompt 反复调 LLM。
        """
        key = hashlib.md5(prompt[:128].encode("utf-8", errors="replace")).hexdigest()
        now = _time.monotonic()
        cached = self._llm_cache.get(key)
        if cached is not None:
            ts, result = cached
            if now - ts < INTENT_LLM_CACHE_TTL_SEC:
                return dict(result)
            self._llm_cache.pop(key, None)
        result = await self._llm_classify(prompt)
        if result:
            self._llm_cache[key] = (now, dict(result))
        return result

    async def classify_route(self, prompt: str) -> dict[str, str]:
        route = await self._classify(prompt)
        reason = route.pop("_reason", "默认路由")
        return {"agent_id": route.get("agent_id", DEFAULT_AGENT_ID), "reason": reason}

    async def _dispatch(self, route: dict, prompt: str, rules_context: str, reason: str,
                        prompt_recommendation: dict | None = None) -> dict[str, Any]:
        route_info = {"route": route, "reason": reason, "rules_applied": rules_context}
        # P2 修复: 顶层 agent_id —— 与 WS 路径 classify_route() 返回的扁平结构对齐。
        # 前端 taskHandlers.ts 读 route_info?.agent_id；原嵌套结构 route.route.agent_id 导致 undefined。
        # 保留嵌套 route 字段供审计/调试，仅补充顶层快捷访问字段。
        route_info["agent_id"] = route.get("agent_id", DEFAULT_AGENT_ID)
        # Batch 4.3: 透传 @-mention 引用，便于审计与后续路由决策
        mentions = extract_mentions(prompt)
        if mentions:
            route_info["mentions"] = mentions
        if prompt_recommendation:
            route_info["prompt_recommendation"] = prompt_recommendation
        context = {"rules": rules_context} if rules_context else {}
        # Phase 88: 把推荐结果放入 extra_context 透传给 agent_service.invoke
        if prompt_recommendation:
            context["extra_context"] = {"prompt_recommendation": prompt_recommendation}
        task_id = None
        if route["type"] == "direct":
            agent_id = route.get("agent_id", DEFAULT_AGENT_ID)
            if self.agent_service:
                task_id = await self.agent_service.invoke(agent_id=agent_id, prompt=prompt, context=context)
        else:
            if self.workflow_service:
                task_id = await self.workflow_service.run_template(template_name=route["template"], initial_input=prompt)
        return {"task_id": task_id, "route_info": route_info}
