"""services/long_task/standards_lookup_agent.py —— Phase 85 标准/文档查询 Agent

在长任务执行失败/工具连续失败/Agent 表达不确定时，主动查询权威文档，
避免 Agent 用训练数据猜过期 API（对标 Claude Code web_search + Context7）。

4 步降级链（lookup）:
  1. adapter.invoke() 生成英文查询关键词 + 候选库名
  2. services.context7_client.Context7Client 查询库文档（命中取前 3 条）
  3. Context7 未命中/超时 → OpenClaw openclaw_web_search
  4. OpenClaw 不可用 → adapter.invoke() 让模型给通用建议（明确告知不可编造 API）

触发条件（should_lookup）:
  - error 含 ModuleNotFoundError/SyntaxError/ImportError/TypeError/编译失败/测试失败
  - 同工具连续失败 ≥ 2 次（由调用方传入 failure_streak）
  - 输出含「我不确定」「可能」等不确定短语
  - 冷却 10 分钟/问题（按 problem_description hash 去重）

返回结构:
  {
    "ok": bool, "sources": [str], "summary": str, "recommendations": [str],
    "source_type": "context7"|"web_search"|"generic_advice"|"fallback",
    "ts": int, "cached"?: bool,
  }

设计参考:
  - final_review_agent.py —— 冷却窗口 + best-effort fallback + 全局单例
  - adversarial_agent.py —— 薄包装 + 缓存 + 单例
  - context7_client.py —— async lookup(library, query)
"""
from __future__ import annotations

import hashlib
import json as _json
import logging
import re
import time as _time

from config import DEFAULT_AGENT_ID, OPENCLAW_AGENT_ID

_log = logging.getLogger("yuncat.standards")

# ── 冷却窗口（秒）── per problem 10 分钟
_COOLDOWN_SECONDS = 600

# Phase 87 审查 C3: LRU 上限——_cooldowns/_cache 永不淘汰会内存泄漏
_MAX_CACHE_SIZE = 100

# ── should_lookup 触发关键词 ──
_ERROR_KEYWORDS = (
    "ModuleNotFoundError", "SyntaxError", "ImportError", "TypeError",
    "AttributeError", "KeyError", "编译失败", "测试失败", "build failed",
    "test failed", "compilation error",
)
_UNCERTAIN_PHRASES = (
    "我不确定", "可能", "或许", "大概", "应该", "不确定", "I'm not sure",
    "maybe", "perhaps", "might",
)
# ── 触发阈值 ──
_FAILURE_STREAK_THRESHOLD = 2  # 同工具连续失败 ≥ 2 次触发

# ── token 预算 ──
_MAX_SNIPPETS = 3
_SNIPPET_CHARS = 800
_TOTAL_SNIPPETS_CHARS = 2400


class StandardsLookupAgent:
    """Phase 85: 标准/文档查询 Agent——4 步降级链查询权威文档。

    生命周期：lifespan 创建单例并注入 app.state.standards_lookup_agent。
    被 orchestrator（节点失败 hook）和 tool_recovery（连续失败）和
    routes/standards_lookup.py（手动触发）调用。
    """

    def __init__(self, agent_service, model_config=None):
        self._agent_service = agent_service
        self._model_config = model_config
        # 冷却窗口：key = problem_description 的 hash
        self._cooldowns: dict[str, float] = {}
        # 冷却期内缓存最近一次结果
        self._cache: dict[str, dict] = {}

    async def lookup(
        self,
        problem_description: str,
        domain: str = "general",
        force: bool = False,
    ) -> dict:
        """4 步降级查询。

        Args:
            problem_description: 技术问题描述（错误信息/不确定点）
            domain: 问题领域（python/frontend/database/general 等）
            force: True 跳过冷却 + 跳过缓存（手动触发时用）

        Returns:
            {ok, sources, summary, recommendations, source_type, ts, cached?}
        """
        key = self._problem_key(problem_description)

        # 冷却检查
        if not force and self._in_cooldown(key):
            cached = self._cache.get(key)
            if cached:
                return {**cached, "cached": True}
            return self._fallback("cooldown no cache")

        if not problem_description or not problem_description.strip():
            result = self._fallback("empty problem_description")
            self._record(key, result)
            return result

        # Step 1: 生成英文查询 + 候选库
        queries_data = await self._generate_queries(problem_description, domain)
        libraries = queries_data.get("libraries", []) or []
        queries = queries_data.get("queries", []) or []

        snippets: list[dict] = []
        source_type = "fallback"

        # Step 2: Context7 查询（每个 library × 每个 query，取前 N 条）
        if libraries and queries:
            ctx7_snippets = await self._context7_lookup(libraries, queries)
            if ctx7_snippets:
                snippets = ctx7_snippets
                source_type = "context7"

        # Step 3: Context7 未命中 → OpenClaw web_search
        if not snippets and queries:
            web_snippets = await self._web_search(queries)
            if web_snippets:
                snippets = web_snippets
                source_type = "web_search"

        # Step 4: 生成建议
        if snippets:
            advice = await self._generate_advice(problem_description, snippets)
        else:
            advice = await self._generate_generic_advice(problem_description)
            source_type = "generic_advice" if advice.get("summary") else "fallback"

        result = {
            "ok": True,
            "sources": advice.get("sources", []),
            "summary": advice.get("summary", ""),
            "recommendations": advice.get("recommendations", []),
            "source_type": source_type,
            "ts": int(_time.time()),
        }
        self._record(key, result)
        _log.info(
            "StandardsLookup %s lookup done (source=%s, %d recs)",
            domain, source_type, len(result["recommendations"]),
        )
        return result

    def should_lookup(
        self,
        context: dict | None = None,
        error: str = "",
        output: str = "",
        tool_name: str = "",
        failure_streak: int = 0,
    ) -> bool:
        """触发条件检查——任一命中即触发（冷却由 lookup 内部处理）。

        Args:
            context: 综合上下文 dict（可含 error/output/tool_name/failure_streak），
                     传 dict 时其他参数被忽略；传 None 时用其他参数
            error: 错误信息字符串
            output: Agent 输出字符串（检查不确定短语）
            tool_name: 工具名（仅用于日志）
            failure_streak: 同工具连续失败次数

        Returns:
            True 表示应该触发查询
        """
        if isinstance(context, dict):
            error = str(context.get("error", "") or context.get("err", ""))
            output = str(context.get("output", ""))
            tool_name = str(context.get("tool_name", ""))
            failure_streak = int(context.get("failure_streak", 0))

        # 条件 1: 错误关键词
        if error:
            for kw in _ERROR_KEYWORDS:
                if kw in error:
                    _log.debug("should_lookup hit by error keyword '%s' (tool=%s)", kw, tool_name)
                    return True

        # 条件 2: 同工具连续失败 ≥ 阈值
        if failure_streak >= _FAILURE_STREAK_THRESHOLD:
            _log.debug(
                "should_lookup hit by failure_streak=%d (tool=%s)",
                failure_streak, tool_name,
            )
            return True

        # 条件 3: 输出含不确定短语
        if output:
            for phrase in _UNCERTAIN_PHRASES:
                if phrase in output:
                    _log.debug("should_lookup hit by uncertain phrase '%s'", phrase)
                    return True

        return False

    # ── 4 步降级链实现 ──

    async def _generate_queries(self, problem: str, domain: str) -> dict:
        """Step 1: LLM 生成英文查询 + 候选库——best-effort，失败返回空 dict"""
        from prompts.spec_prompts import render_standards_lookup_query_prompt
        prompt = render_standards_lookup_query_prompt(problem, domain)
        raw = await self._invoke_llm(prompt)
        parsed = _extract_json(raw)
        if not isinstance(parsed, dict):
            _log.debug("query generation failed to parse, raw=%s", raw[:200])
            return {}
        libraries = parsed.get("libraries", [])
        queries = parsed.get("queries", [])
        if not isinstance(libraries, list):
            libraries = []
        if not isinstance(queries, list):
            queries = []
        return {
            "libraries": [str(x).strip().lower() for x in libraries if str(x).strip()],
            "queries": [str(x).strip() for x in queries if str(x).strip()],
        }

    async def _context7_lookup(
        self, libraries: list[str], queries: list[str],
    ) -> list[dict]:
        """Step 2: Context7 查询——每个 library × 每个 query，取前 N 条 snippet

        Returns: [{library, query, content}, ...]
        """
        snippets: list[dict] = []
        try:
            from services.context7_client import get_context7
            client = await get_context7()
        except Exception:
            _log.debug("Context7 client unavailable", exc_info=True)
            return []

        for lib in libraries[:3]:  # 最多 3 个库
            if len(snippets) >= _MAX_SNIPPETS:
                break
            for query in queries[:2]:  # 每个库最多 2 个查询
                try:
                    result = await client.lookup(lib, query)
                except Exception:
                    _log.debug("Context7 lookup exception for %s/%s", lib, query, exc_info=True)
                    continue
                if result.get("error"):
                    continue
                results = result.get("results", [])
                for r in results[:1]:  # 每个 query 最多 1 条
                    content = str(r.get("content", ""))[:_SNIPPET_CHARS]
                    if content:
                        snippets.append({
                            "library": lib,
                            "query": query,
                            "content": content,
                        })
                        if len(snippets) >= _MAX_SNIPPETS:
                            break
                if len(snippets) >= _MAX_SNIPPETS:
                    break

        _log.debug("Context7 returned %d snippets", len(snippets))
        return snippets

    async def _web_search(self, queries: list[str]) -> list[dict]:
        """Step 3: OpenClaw openclaw_web_search 降级——best-effort

        Returns: [{library: "web", query, content}, ...]
        """
        try:
            oc = self._agent_service.adapters.get(OPENCLAW_AGENT_ID)
            if not oc or not hasattr(oc, "execute_tool"):
                return []
        except Exception:
            return []

        snippets: list[dict] = []
        for query in queries[:3]:
            if len(snippets) >= _MAX_SNIPPETS:
                break
            try:
                # 拼接更精确的查询——加入 "documentation" 提升质量
                result = await oc.execute_tool(
                    "openclaw_web_search", {"query": f"{query} documentation"},
                )
            except Exception:
                _log.debug("web_search exception for query=%s", query, exc_info=True)
                continue
            if not result.get("ok") and not result.get("success"):
                continue
            # OpenClaw 返回结构可能是 {result: {content}} 或 {data: {...}}
            content = ""
            if isinstance(result, dict):
                content = str(result.get("result") or result.get("data") or "")
            content = content[:_SNIPPET_CHARS]
            if content:
                snippets.append({
                    "library": "web",
                    "query": query,
                    "content": content,
                })

        _log.debug("web_search returned %d snippets", len(snippets))
        return snippets

    async def _generate_advice(
        self, problem: str, snippets: list[dict],
    ) -> dict:
        """Step 4a: 基于检索片段生成建议——best-effort"""
        from prompts.spec_prompts import render_standards_lookup_advice_prompt
        snippets_text = self._format_snippets(snippets)
        prompt = render_standards_lookup_advice_prompt(problem, snippets_text)
        raw = await self._invoke_llm(prompt)
        parsed = _extract_json(raw)
        if not isinstance(parsed, dict):
            _log.debug("advice generation parse failed, raw=%s", raw[:200])
            return self._empty_advice()
        return {
            "summary": str(parsed.get("summary", "")),
            "recommendations": [str(r) for r in parsed.get("recommendations", []) if str(r).strip()],
            "sources": [str(s) for s in parsed.get("sources", []) if str(s).strip()],
        }

    async def _generate_generic_advice(self, problem: str) -> dict:
        """Step 4b: 无任何 snippets 时，让 LLM 给元建议（升级查询关键词等）

        明确告知 LLM 不可编造 API——只能给「如何找到答案」的建议。
        """
        prompt = (
            "## 角色：技术顾问\n\n"
            "用户遇到以下技术问题，但文档检索未命中。**不要编造 API**，"
            "只给出「如何找到答案」的建议。\n\n"
            f"### 问题\n{problem}\n\n"
            "### 输出格式——严格 JSON\n"
            '```json\n{"summary": "未找到直接相关文档", '
            '"recommendations": ["建议1", "建议2"], "sources": []}\n```\n'
            "### 规则\n1. summary 明说「未找到直接相关文档」\n"
            "2. recommendations 给出元建议（升级查询关键词/检查依赖版本/查看官方 changelog 等）\n"
            "3. **绝对禁止编造 API 名/参数**"
        )
        raw = await self._invoke_llm(prompt)
        parsed = _extract_json(raw)
        if not isinstance(parsed, dict):
            return self._empty_advice()
        return {
            "summary": str(parsed.get("summary", "未找到直接相关文档")),
            "recommendations": [str(r) for r in parsed.get("recommendations", []) if str(r).strip()],
            "sources": [],
        }

    # ── LLM 调用（参考 final_review_agent._invoke_llm）──

    async def _invoke_llm(self, prompt: str) -> str:
        """轻量 LLM 调用——hermes adapter 单次 invoke（非 ReAct loop）

        adapter 缺失/调用失败返回空字符串（best-effort）。
        """
        try:
            adapter = self._agent_service.adapters.get(DEFAULT_AGENT_ID)
        except Exception:
            adapter = None
        if adapter is None:
            _log.warning("standards_lookup: %s adapter missing", DEFAULT_AGENT_ID)
            return ""
        try:
            result = await adapter.invoke(prompt)
        except Exception:
            _log.debug("standards_lookup: adapter.invoke failed", exc_info=True)
            return ""
        if isinstance(result, dict):
            return str(result.get("content", ""))
        return str(result) if result else ""

    # ── 内部辅助 ──

    @staticmethod
    def _problem_key(problem: str) -> str:
        """problem_description → 8 位 hash（冷却去重 key）"""
        return hashlib.md5(problem.strip().encode("utf-8")).hexdigest()[:8]

    def _in_cooldown(self, key: str) -> bool:
        last = self._cooldowns.get(key)
        if last is None:
            return False
        return _time.monotonic() - last < _COOLDOWN_SECONDS

    def _record(self, key: str, result: dict) -> None:
        """记录冷却时间戳 + 缓存结果"""
        self._cooldowns[key] = _time.monotonic()
        self._cache[key] = result
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        """Phase 87 审查 C3: LRU 淘汰——_cooldowns 超限时按最旧时间戳淘汰

        key 是 problem hash（不含 task_id），无法按 task 清理，故用 LRU。
        """
        if len(self._cooldowns) <= _MAX_CACHE_SIZE:
            return
        # 按时间戳排序，淘汰最旧的
        sorted_keys = sorted(self._cooldowns.items(), key=lambda x: x[1])
        evict_count = len(self._cooldowns) - _MAX_CACHE_SIZE
        for key, _ in sorted_keys[:evict_count]:
            self._cooldowns.pop(key, None)
            self._cache.pop(key, None)

    @staticmethod
    def _format_snippets(snippets: list[dict]) -> str:
        """格式化 snippets 为 LLM 可读文本（含 library + query + content）"""
        if not snippets:
            return ""
        parts: list[str] = []
        total = 0
        for i, s in enumerate(snippets, 1):
            lib = s.get("library", "?")
            content = s.get("content", "")
            # 总预算控制
            remaining = _TOTAL_SNIPPETS_CHARS - total
            if remaining <= 0:
                break
            content = content[:remaining]
            total += len(content)
            parts.append(f"--- 片段 {i}（库: {lib}）---\n{content}")
        return "\n\n".join(parts)

    @staticmethod
    def _empty_advice() -> dict:
        return {"summary": "", "recommendations": [], "sources": []}

    @staticmethod
    def _fallback(reason: str) -> dict:
        """best-effort fallback——任何失败均返回 ok=False，不阻塞主流程"""
        return {
            "ok": False,
            "sources": [],
            "summary": "",
            "recommendations": [],
            "source_type": "fallback",
            "reason": f"fallback: {reason}",
            "ts": int(_time.time()),
        }


# ── JSON 提取（复用 final_review_agent._extract_json 模式）──

def _extract_json(text: str) -> dict | None:
    """从 LLM 输出提取 JSON——支持 ```json 代码块、裸 JSON、{ ... } 块"""
    if not text:
        return None
    m = re.search(r'```(?:json)?\s*\n(.*?)```', text, re.DOTALL)
    if m:
        try:
            return _json.loads(m.group(1))
        except _json.JSONDecodeError:
            pass
    try:
        return _json.loads(text)
    except _json.JSONDecodeError:
        pass
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return _json.loads(m.group(0))
        except _json.JSONDecodeError:
            pass
    return None


# ── 全局单例（对齐 long_task 家族模式）──

_agent: StandardsLookupAgent | None = None


def get_standards_lookup_agent() -> StandardsLookupAgent | None:
    """获取全局 StandardsLookupAgent 单例（未初始化返回 None）"""
    return _agent


def set_standards_lookup_agent(agent: StandardsLookupAgent | None) -> None:
    """设置全局 StandardsLookupAgent 单例（lifespan 启动时调，测试可覆盖）"""
    global _agent
    _agent = agent
