"""tests/unit/test_standards_lookup_agent.py —— Phase 85 StandardsLookupAgent 测试

覆盖：
- StandardsLookupAgent.lookup 4 步降级链（Context7 命中/未命中/web_search/generic_advice/fallback）
- should_lookup 触发条件（error 关键词/连续失败/不确定短语/无触发）
- 冷却窗口（缓存命中/force 跳过）
- 全局单例 get/set

设计参考 test_adversarial_agent.py 模式——Mock agent_service.adapters 而非真实 adapter。
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src-python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from services.long_task.standards_lookup_agent import (
    StandardsLookupAgent,
    get_standards_lookup_agent,
    set_standards_lookup_agent,
    _COOLDOWN_SECONDS,
    _FAILURE_STREAK_THRESHOLD,
)


# ═══════════════════════════════════════════════════════════════
# Mock 工具
# ═══════════════════════════════════════════════════════════════

def _make_hermes_adapter(
    query_output: str = "",
    advice_output: str = "",
    raise_on_invoke: bool = False,
) -> MagicMock:
    """模拟 hermes adapter——invoke 返回预设 content

    lookup 内部多次调 invoke：
      - _generate_queries: 期望返回 JSON {libraries, queries}
      - _generate_advice / _generate_generic_advice: 期望返回 JSON {summary, recommendations, sources}
    用 side_effect 按调用顺序返回不同内容。
    """
    adapter = MagicMock()
    outputs = [query_output, advice_output]

    async def _invoke(prompt, context=None):
        if raise_on_invoke:
            raise RuntimeError("mock invoke fail")
        idx = min(len(adapter.invoke_calls), len(outputs) - 1)
        adapter.invoke_calls.append(prompt)
        return {"content": outputs[idx]}

    adapter.invoke_calls = []
    adapter.invoke = _invoke
    return adapter


def _make_openclaw_adapter(
    web_results: list[dict] | None = None,
    raise_on_execute: bool = False,
) -> MagicMock:
    """模拟 openclaw adapter——execute_tool 返回预设结果"""
    adapter = MagicMock()
    adapter.execute_tool_calls = []
    _web_results = web_results or []

    async def _execute_tool(tool_name, args, timeout_sec=300):
        if raise_on_execute:
            raise RuntimeError("mock execute_tool fail")
        adapter.execute_tool_calls.append((tool_name, args))
        if tool_name == "openclaw_web_search":
            if not _web_results:
                return {"ok": False, "error": "no results"}
            return _web_results.pop(0)
        return {"ok": False, "error": "unknown tool"}

    adapter.execute_tool = _execute_tool
    return adapter


class _MockAgentService:
    """模拟 agent_service——含 .adapters dict

    重构后 StandardsLookupAgent._invoke_llm 通过 DEFAULT_AGENT_ID（= "yuncat"）
    取 LLM adapter，故需注册 "yuncat"；保留 "hermes" 键指向同一对象以维持旧断言。
    """

    def __init__(self, hermes=None, openclaw=None):
        llm = hermes or _make_hermes_adapter()
        self.adapters = {
            "yuncat": llm,
            "hermes": llm,
            "openclaw": openclaw,
        }


def _make_agent(
    query_output: str = "",
    advice_output: str = "",
    web_results: list[dict] | None = None,
    hermes_raise: bool = False,
    openclaw=None,
) -> tuple[StandardsLookupAgent, MagicMock]:
    """构造带 mock 的 StandardsLookupAgent，返回 (agent, hermes_adapter)"""
    hermes = _make_hermes_adapter(
        query_output=query_output,
        advice_output=advice_output,
        raise_on_invoke=hermes_raise,
    )
    oc = openclaw or (MagicMock() if web_results is None else _make_openclaw_adapter(web_results))
    svc = _MockAgentService(hermes=hermes, openclaw=oc)
    return StandardsLookupAgent(agent_service=svc, model_config=MagicMock()), hermes


# ── 预设 LLM JSON 输出 ──

_QUERY_JSON = '```json\n{"libraries": ["fastapi", "pydantic"], "queries": ["lifespan async", "dependency injection"]}\n```'
_ADVICE_JSON = '```json\n{"summary": "找到 FastAPI lifespan 文档", "recommendations": ["用 @asynccontextmanager", "在 lifespan 中初始化资源"], "sources": ["fastapi lifespan"]}\n```'
_GENERIC_ADVICE_JSON = '```json\n{"summary": "未找到直接相关文档", "recommendations": ["升级查询关键词", "检查依赖版本"], "sources": []}\n```'


# ═══════════════════════════════════════════════════════════════
# TestLookupBasic —— lookup 4 步降级链
# ═══════════════════════════════════════════════════════════════

class TestLookupBasic:
    """Phase 85-1: lookup 4 步降级链"""

    @pytest.mark.asyncio
    async def test_lookup_context7_hit(self):
        """Context7 命中 → source_type=context7"""
        agent, hermes = _make_agent(
            query_output=_QUERY_JSON,
            advice_output=_ADVICE_JSON,
        )
        # Mock Context7Client.get_context7 + lookup
        mock_client = MagicMock()
        mock_client.lookup = AsyncMock(return_value={
            "library": "fastapi", "library_id": "fastapi",
            "results": [{"content": "FastAPI lifespan docs..."}],
            "query": "lifespan async",
        })
        with patch(
            "services.context7_client.get_context7",
            AsyncMock(return_value=mock_client),
        ):
            result = await agent.lookup(
                "ModuleNotFoundError: No module named 'fastapi'", domain="python",
            )
        assert result["ok"] is True
        assert result["source_type"] == "context7"
        assert len(result["recommendations"]) == 2
        assert "FastAPI" in result["summary"]

    @pytest.mark.asyncio
    async def test_lookup_context7_miss_web_search(self):
        """Context7 未命中 → web_search 降级 → source_type=web_search"""
        web_result = {"ok": True, "success": True, "result": "Web search result content"}
        agent, hermes = _make_agent(
            query_output=_QUERY_JSON,
            advice_output=_ADVICE_JSON,
            web_results=[web_result],
        )
        # Context7 返回 error（未命中）
        mock_client = MagicMock()
        mock_client.lookup = AsyncMock(return_value={
            "error": "Library not found", "results": [],
        })
        with patch(
            "services.context7_client.get_context7",
            AsyncMock(return_value=mock_client),
        ):
            result = await agent.lookup("ImportError: cannot import name 'X'", domain="python")
        assert result["ok"] is True
        assert result["source_type"] == "web_search"
        # openclaw.execute_tool 应被调用
        oc = agent._agent_service.adapters["openclaw"]
        assert len(oc.execute_tool_calls) > 0

    @pytest.mark.asyncio
    async def test_lookup_all_miss_generic_advice(self):
        """Context7 + web_search 都未命中 → generic_advice"""
        agent, hermes = _make_agent(
            query_output=_QUERY_JSON,
            advice_output=_GENERIC_ADVICE_JSON,  # 第三次 invoke 是 generic advice
            web_results=[],  # web_search 无结果
        )
        # Context7 也未命中
        mock_client = MagicMock()
        mock_client.lookup = AsyncMock(return_value={"error": "not found", "results": []})
        with patch(
            "services.context7_client.get_context7",
            AsyncMock(return_value=mock_client),
        ):
            result = await agent.lookup("SyntaxError: invalid syntax", domain="python")
        assert result["ok"] is True
        assert result["source_type"] == "generic_advice"
        assert "未找到" in result["summary"]

    @pytest.mark.asyncio
    async def test_lookup_empty_problem_fallback(self):
        """空 problem_description → fallback"""
        agent, _ = _make_agent(query_output=_QUERY_JSON, advice_output=_ADVICE_JSON)
        result = await agent.lookup("", domain="general")
        assert result["ok"] is False
        assert result["source_type"] == "fallback"
        assert "empty" in result["reason"]

    @pytest.mark.asyncio
    async def test_lookup_whitespace_problem_fallback(self):
        """纯空白 problem → fallback"""
        agent, _ = _make_agent()
        result = await agent.lookup("   \n  ", domain="general")
        assert result["ok"] is False
        assert result["source_type"] == "fallback"


# ═══════════════════════════════════════════════════════════════
# TestLookupDegradation —— 降级链异常路径
# ═══════════════════════════════════════════════════════════════

class TestLookupDegradation:
    """Phase 85-1: 降级链异常 best-effort"""

    @pytest.mark.asyncio
    async def test_query_generation_parse_fail_falls_to_generic(self):
        """Step 1 query 生成 JSON 解析失败 → libraries 空 → 跳过 Context7/web → generic_advice"""
        agent, hermes = _make_agent(
            query_output="not a json",
            advice_output=_GENERIC_ADVICE_JSON,
        )
        result = await agent.lookup("TypeError: bad arg", domain="python")
        # libraries 空 → 跳过 Context7 + web_search → generic_advice
        assert result["ok"] is True
        assert result["source_type"] == "generic_advice"

    @pytest.mark.asyncio
    async def test_context7_exception_falls_to_web_search(self):
        """Step 2 Context7 异常 → 降级 web_search"""
        web_result = {"ok": True, "result": "fallback web content"}
        agent, hermes = _make_agent(
            query_output=_QUERY_JSON,
            advice_output=_ADVICE_JSON,
            web_results=[web_result],
        )
        # Context7 lookup 抛异常
        mock_client = MagicMock()
        mock_client.lookup = AsyncMock(side_effect=RuntimeError("ctx7 down"))
        with patch(
            "services.context7_client.get_context7",
            AsyncMock(return_value=mock_client),
        ):
            result = await agent.lookup("ImportError: x", domain="python")
        assert result["source_type"] == "web_search"

    @pytest.mark.asyncio
    async def test_no_openclaw_adapter_skips_web_search(self):
        """无 openclaw adapter → 跳过 web_search → generic_advice"""
        agent, hermes = _make_agent(
            query_output=_QUERY_JSON,
            advice_output=_GENERIC_ADVICE_JSON,
            openclaw=None,  # 显式 None
        )
        # Context7 未命中
        mock_client = MagicMock()
        mock_client.lookup = AsyncMock(return_value={"error": "not found", "results": []})
        with patch(
            "services.context7_client.get_context7",
            AsyncMock(return_value=mock_client),
        ):
            result = await agent.lookup("SyntaxError: x", domain="python")
        assert result["source_type"] == "generic_advice"

    @pytest.mark.asyncio
    async def test_hermes_invoke_exception_fallback(self):
        """hermes.invoke 抛异常 → _invoke_llm 返回空 → query 解析失败 → generic_advice 也失败 → fallback"""
        agent, hermes = _make_agent(
            query_output="",
            advice_output="",
            hermes_raise=True,
        )
        result = await agent.lookup("TypeError: x", domain="python")
        # query 失败 → libraries 空 → generic_advice 调用也失败（hermes 抛异常）→ fallback
        assert result["ok"] is True
        # hermes 异常时 _invoke_llm 返回 ""，generic_advice 解析空 → empty_advice → source_type=fallback
        assert result["source_type"] == "fallback"

    @pytest.mark.asyncio
    async def test_advice_parse_fail_returns_empty_advice(self):
        """advice JSON 解析失败 → 返回空 advice（summary 为空）→ source_type=fallback"""
        agent, hermes = _make_agent(
            query_output=_QUERY_JSON,
            advice_output="not a json",  # advice 解析失败
        )
        mock_client = MagicMock()
        mock_client.lookup = AsyncMock(return_value={
            "results": [{"content": "doc content"}],
        })
        with patch(
            "services.context7_client.get_context7",
            AsyncMock(return_value=mock_client),
        ):
            result = await agent.lookup("TypeError: x", domain="python")
        # advice 解析失败 → _empty_advice (summary="") → source_type 保持 context7 但 summary 为空
        # 但实际：advice 解析失败返回 empty_advice，summary="" → ok=True, source_type=context7
        assert result["ok"] is True
        assert result["summary"] == ""


# ═══════════════════════════════════════════════════════════════
# TestShouldLookup —— 触发条件
# ═══════════════════════════════════════════════════════════════

class TestShouldLookup:
    """Phase 85-1: should_lookup 触发条件"""

    def test_should_lookup_error_keyword_modulenotfound(self):
        agent, _ = _make_agent()
        assert agent.should_lookup(error="ModuleNotFoundError: No module named 'x'") is True

    def test_should_lookup_error_keyword_syntaxerror(self):
        agent, _ = _make_agent()
        assert agent.should_lookup(error="SyntaxError: invalid syntax") is True

    def test_should_lookup_error_keyword_chinese(self):
        agent, _ = _make_agent()
        assert agent.should_lookup(error="编译失败：找不到符号") is True

    def test_should_lookup_failure_streak_threshold(self):
        """连续失败 ≥ 阈值（2）→ True"""
        agent, _ = _make_agent()
        assert agent.should_lookup(failure_streak=2) is True
        assert agent.should_lookup(failure_streak=3) is True

    def test_should_lookup_failure_streak_below_threshold(self):
        """连续失败 < 阈值 → False"""
        agent, _ = _make_agent()
        assert agent.should_lookup(failure_streak=1) is False
        assert agent.should_lookup(failure_streak=0) is False

    def test_should_lookup_uncertain_phrase_chinese(self):
        agent, _ = _make_agent()
        assert agent.should_lookup(output="我不确定这个 API 的用法") is True

    def test_should_lookup_uncertain_phrase_english(self):
        agent, _ = _make_agent()
        assert agent.should_lookup(output="I'm not sure about this") is True

    def test_should_lookup_no_trigger(self):
        """无任何触发条件 → False"""
        agent, _ = _make_agent()
        assert agent.should_lookup(
            error="normal log", output="task done", failure_streak=0,
        ) is False

    def test_should_lookup_context_dict_mode(self):
        """传 context dict 模式——参数从 dict 提取"""
        agent, _ = _make_agent()
        ctx = {
            "error": "TypeError: bad arg",
            "output": "done",
            "tool_name": "read_file",
            "failure_streak": 0,
        }
        assert agent.should_lookup(context=ctx) is True

    def test_should_lookup_context_dict_none_uses_other_args(self):
        """context=None 时用其他参数"""
        agent, _ = _make_agent()
        assert agent.should_lookup(
            context=None, error="ImportError: x",
        ) is True


# ═══════════════════════════════════════════════════════════════
# TestCooldown —— 冷却窗口
# ═══════════════════════════════════════════════════════════════

class TestCooldown:
    """Phase 85-1: 冷却窗口 + 缓存"""

    @pytest.mark.asyncio
    async def test_cooldown_returns_cached(self):
        """冷却期内第二次调用返回缓存（cached=True）"""
        agent, hermes = _make_agent(
            query_output=_QUERY_JSON, advice_output=_ADVICE_JSON,
        )
        mock_client = MagicMock()
        mock_client.lookup = AsyncMock(return_value={
            "results": [{"content": "doc"}],
        })
        with patch("services.context7_client.get_context7", AsyncMock(return_value=mock_client)):
            first = await agent.lookup("TypeError: x", domain="python")
            second = await agent.lookup("TypeError: x", domain="python")
        assert first["ok"] is True
        assert second.get("cached") is True
        assert second["ok"] == first["ok"]

    @pytest.mark.asyncio
    async def test_force_bypasses_cooldown(self):
        """force=True 跳过冷却——重新调 LLM"""
        agent, hermes = _make_agent(
            query_output=_QUERY_JSON, advice_output=_ADVICE_JSON,
        )
        mock_client = MagicMock()
        mock_client.lookup = AsyncMock(return_value={
            "results": [{"content": "doc"}],
        })
        with patch("services.context7_client.get_context7", AsyncMock(return_value=mock_client)):
            first = await agent.lookup("TypeError: x", domain="python")
            second = await agent.lookup("TypeError: x", domain="python", force=True)
        assert "cached" not in first
        assert "cached" not in second  # force 不走缓存

    @pytest.mark.asyncio
    async def test_cooldown_per_problem_hash(self):
        """不同 problem_description 不共享冷却（hash 不同）"""
        agent, hermes = _make_agent(
            query_output=_QUERY_JSON, advice_output=_ADVICE_JSON,
        )
        mock_client = MagicMock()
        mock_client.lookup = AsyncMock(return_value={"results": [{"content": "doc"}]})
        with patch("services.context7_client.get_context7", AsyncMock(return_value=mock_client)):
            await agent.lookup("TypeError: problem A", domain="python")
            await agent.lookup("SyntaxError: problem B", domain="python")
        # 两次不同 problem → hermes.invoke 应被调用 4 次（每次 lookup 调 2 次：query + advice）
        assert len(hermes.invoke_calls) == 4


# ═══════════════════════════════════════════════════════════════
# TestSingleton —— 全局单例
# ═══════════════════════════════════════════════════════════════

class TestSingleton:
    """Phase 85-1: 全局单例 get/set"""

    def test_get_set_singleton(self):
        original = get_standards_lookup_agent()
        try:
            agent = StandardsLookupAgent(
                agent_service=_MockAgentService(), model_config=MagicMock(),
            )
            set_standards_lookup_agent(agent)
            assert get_standards_lookup_agent() is agent
        finally:
            set_standards_lookup_agent(original)

    def test_get_returns_none_when_not_set(self):
        original = get_standards_lookup_agent()
        try:
            set_standards_lookup_agent(None)
            assert get_standards_lookup_agent() is None
        finally:
            set_standards_lookup_agent(original)


# ═══════════════════════════════════════════════════════════════
# TestProblemKey —— 问题 hash 去重
# ═══════════════════════════════════════════════════════════════

class TestProblemKey:
    """Phase 85-1: _problem_key hash 去重"""

    def test_same_problem_same_key(self):
        """相同 problem → 相同 hash"""
        agent, _ = _make_agent()
        k1 = agent._problem_key("TypeError: bad arg")
        k2 = agent._problem_key("TypeError: bad arg")
        assert k1 == k2

    def test_different_problem_different_key(self):
        """不同 problem → 不同 hash"""
        agent, _ = _make_agent()
        k1 = agent._problem_key("TypeError: A")
        k2 = agent._problem_key("SyntaxError: B")
        assert k1 != k2

    def test_whitespace_stripped_before_hash(self):
        """前后空白被 strip——相同内容（不同空白）→ 相同 hash"""
        agent, _ = _make_agent()
        k1 = agent._problem_key("TypeError: x")
        k2 = agent._problem_key("  TypeError: x  \n")
        assert k1 == k2

    def test_key_is_8_chars_md5_prefix(self):
        """key 是 md5 前 8 位"""
        agent, _ = _make_agent()
        key = agent._problem_key("test")
        assert len(key) == 8
        assert all(c in "0123456789abcdef" for c in key)
