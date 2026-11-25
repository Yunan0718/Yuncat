"""tests/unit/test_final_review_agent.py —— Phase 83 FinalReviewAgent 测试

覆盖：
- FinalReviewAgent.review 基本流程（3 stage）
- 冷却窗口（per task / per step / force 跳过 / 缓存命中）
- LLM 调用失败 best-effort fallback（adapter 缺失/异常/非 JSON 输出）
- _parse_result 容错（非法 action / issues 非列表）
- 单例 get/set
- render_final_review_prompt 渲染
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src-python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import DEFAULT_AGENT_ID

from services.long_task.final_review_agent import (
    FinalReviewAgent,
    get_final_review_agent,
    set_final_review_agent,
    _extract_json,
    _COOLDOWN_PRE_DANGEROUS_TOOL,
    _COOLDOWN_EXECUTION,
)
from prompts.spec_prompts import (
    FINAL_REVIEW_PROMPT,
    render_final_review_prompt,
)


# ═══════════════════════════════════════════════════════════════
# Mock 工具
# ═══════════════════════════════════════════════════════════════

class _MockAdapter:
    """模拟 hermes adapter——invoke 返回预设 content。

    带 _ready 事件：真实 adapter 都有，_invoke_llm 现在会先检查就绪态
    （未就绪 → 抛 FinalReviewUnavailable，由 review 按审批模式处理）。
    """

    def __init__(self, content: str = "", raise_on_invoke: bool = False,
                 ready: bool = True):
        self._content = content
        self._raise = raise_on_invoke
        self.invoke_calls: list[str] = []
        import asyncio as _asyncio
        self._ready = _asyncio.Event()
        if ready:
            self._ready.set()

    async def invoke(self, prompt, context=None):
        self.invoke_calls.append(prompt)
        if self._raise:
            raise RuntimeError("mock invoke fail")
        return {"content": self._content}


def _make_agent(content: str = "", adapters=None, raise_on_invoke: bool = False) -> FinalReviewAgent:
    """构造带 mock adapter 的 FinalReviewAgent

    DEFAULT_AGENT_ID 已从 hermes 改为 yuncat（Phase N）——注册双键指向同一
    adapter，兼容服务按默认 agent 查找 + 测试直接访问 _adapters["hermes"]。
    """
    if adapters is None:
        adapter = _MockAdapter(content=content, raise_on_invoke=raise_on_invoke)
        adapters = {"hermes": adapter, DEFAULT_AGENT_ID: adapter}
    return FinalReviewAgent(adapters=adapters, model_config=MagicMock())


def _llm_json(pass_: bool, score: int = 80, action: str = "proceed",
              issues=None, reason: str = "ok") -> str:
    """生成 LLM 返回的 JSON 字符串（带 ```json 代码块）"""
    import json as _json
    payload = {
        "pass": pass_, "score": score, "issues": issues or [],
        "action": action, "reason": reason,
    }
    return f"```json\n{_json.dumps(payload, ensure_ascii=False)}\n```"


# ═══════════════════════════════════════════════════════════════
# TestReviewBasic —— review 基本流程
# ═══════════════════════════════════════════════════════════════

class TestReviewBasic:
    """Phase 83-1: FinalReviewAgent.review 基本流程"""

    @pytest.mark.asyncio
    async def test_review_pre_dangerous_tool_pass(self):
        """pre_dangerous_tool 审查通过——LLM 返回 pass=true"""
        agent = _make_agent(content=_llm_json(True, score=90, action="proceed"))
        result = await agent.review(
            task_id="t1", stage="pre_dangerous_tool",
            context={"tool_name": "write_file", "args": {"path": "/tmp/a.txt"}},
        )
        assert result["pass"] is True
        assert result["score"] == 90
        assert result["action"] == "proceed"
        assert result["stage"] == "pre_dangerous_tool"
        assert result["task_id"] == "t1"
        assert result["issues"] == []
        assert "cached" not in result  # 首次调用非缓存

    @pytest.mark.asyncio
    async def test_review_pre_execution_halt(self):
        """pre_execution 审查 halt——LLM 返回 pass=false action=halt"""
        agent = _make_agent(content=_llm_json(
            False, score=30, action="halt", issues=["目标偏离"], reason="偏离目标",
        ))
        result = await agent.review(
            task_id="t1", stage="pre_execution", step_id="s1",
            context={"goal": "G", "step_title": "S", "step_prompt": "P"},
        )
        assert result["pass"] is False
        assert result["action"] == "halt"
        assert result["issues"] == ["目标偏离"]
        assert result["reason"] == "偏离目标"

    @pytest.mark.asyncio
    async def test_review_post_execution_ask_user(self):
        """post_execution 审查 ask_user"""
        agent = _make_agent(content=_llm_json(
            False, score=60, action="ask_user", issues=["需确认"],
        ))
        result = await agent.review(
            task_id="t1", stage="post_execution", step_id="s1",
            context={"goal": "G", "output": "产出"},
        )
        assert result["action"] == "ask_user"
        assert result["pass"] is False

    @pytest.mark.asyncio
    async def test_review_unknown_stage_fallback(self):
        """未知 stage：默认模式不阻塞（best-effort 语义保持）。"""
        agent = _make_agent(content=_llm_json(True))
        result = await agent.review(
            task_id="t1", stage="invalid_stage", context={},
        )
        assert result["pass"] is True
        assert result["action"] == "proceed"
        assert "unknown stage" in result["reason"]

    @pytest.mark.asyncio
    async def test_review_unknown_stage_ai_review_asks_user(self):
        """未知 stage + AI 代审：同样不能默认放行（fail-closed）。"""
        agent = _make_agent(content=_llm_json(True))
        result = await agent.review(
            task_id="t1", stage="invalid_stage", context={},
            approve_on_uncertain=False,
        )
        assert result["action"] == "ask_user"


# ═══════════════════════════════════════════════════════════════
# TestCooldown —— 冷却窗口
# ═══════════════════════════════════════════════════════════════

class TestCooldown:
    """Phase 83-1: 冷却窗口 + 缓存"""

    @pytest.mark.asyncio
    async def test_cooldown_returns_cached(self):
        """冷却期内第二次调用返回缓存结果（cached=True）"""
        agent = _make_agent(content=_llm_json(True, score=85))
        ctx = {"tool_name": "write_file", "args": {}}
        first = await agent.review("t1", "pre_dangerous_tool", ctx)
        second = await agent.review("t1", "pre_dangerous_tool", ctx)
        assert first["score"] == 85
        assert second["cached"] is True
        assert second["score"] == 85
        # LLM 只调用一次（第二次走缓存）
        adapter = agent._adapters["hermes"]
        assert len(adapter.invoke_calls) == 1

    @pytest.mark.asyncio
    async def test_force_bypasses_cooldown(self):
        """force=True 跳过冷却——LLM 被调用两次"""
        agent = _make_agent(content=_llm_json(True, score=85))
        ctx = {"tool_name": "write_file", "args": {}}
        first = await agent.review("t1", "pre_dangerous_tool", ctx)
        second = await agent.review("t1", "pre_dangerous_tool", ctx, force=True)
        assert "cached" not in first
        assert "cached" not in second  # force 不走缓存
        adapter = agent._adapters["hermes"]
        assert len(adapter.invoke_calls) == 2

    @pytest.mark.asyncio
    async def test_cooldown_per_task_pre_dangerous(self):
        """pre_dangerous_tool 冷却 per task——同 task 不同参数仍命中缓存"""
        agent = _make_agent(content=_llm_json(True, score=70))
        await agent.review("t1", "pre_dangerous_tool",
                           {"tool_name": "write_file", "args": {"a": 1}})
        await agent.review("t1", "pre_dangerous_tool",
                           {"tool_name": "run_command", "args": {"b": 2}})
        adapter = agent._adapters["hermes"]
        assert len(adapter.invoke_calls) == 1  # 同 task 共享冷却

    @pytest.mark.asyncio
    async def test_cooldown_per_step_execution(self):
        """pre/post_execution 冷却 per step——不同 step 不命中缓存"""
        agent = _make_agent(content=_llm_json(True, score=75))
        await agent.review("t1", "pre_execution", step_id="s1",
                           context={"step_prompt": "p1"})
        await agent.review("t1", "pre_execution", step_id="s2",
                           context={"step_prompt": "p2"})
        adapter = agent._adapters["hermes"]
        assert len(adapter.invoke_calls) == 2  # 不同 step 各调一次

    @pytest.mark.asyncio
    async def test_cooldown_different_tasks_not_shared(self):
        """不同 task 的冷却不共享"""
        agent = _make_agent(content=_llm_json(True, score=80))
        await agent.review("t1", "pre_dangerous_tool",
                           {"tool_name": "write_file", "args": {}})
        await agent.review("t2", "pre_dangerous_tool",
                           {"tool_name": "write_file", "args": {}})
        adapter = agent._adapters["hermes"]
        assert len(adapter.invoke_calls) == 2


# ═══════════════════════════════════════════════════════════════
# TestBestEffort —— LLM 失败 best-effort fallback
# ═══════════════════════════════════════════════════════════════

class TestUnavailableSemantics:
    """2026-10-08: 审查员「没说话」时怎么算——由审批模式决定。

    旧实现无论何种失败都 fail-open 放行，实测对 `rm -rf /` 这种调用也 pass，
    属于危险缺陷。现在：
      - approve_on_uncertain=True（默认，审查只是加道保险）→ 放行，不阻塞；
      - approve_on_uncertain=False（AI 代审，结论即决策）→ ask_user 交回用户。
    """

    @pytest.mark.asyncio
    async def test_adapter_missing_default_approves(self):
        """默认（非代审）：adapter 缺失不阻塞执行。"""
        agent = FinalReviewAgent(adapters={}, model_config=MagicMock())
        result = await agent.review("t1", "pre_dangerous_tool",
                                    {"tool_name": "x", "args": {}})
        assert result["pass"] is True
        assert result["action"] == "proceed"
        assert result["reviewer_unavailable"] is True

    @pytest.mark.asyncio
    async def test_adapter_missing_ai_review_asks_user(self):
        """AI 代审：adapter 缺失必须交回用户，绝不默认放行。"""
        agent = FinalReviewAgent(adapters={}, model_config=MagicMock())
        result = await agent.review("t1", "pre_dangerous_tool",
                                    {"tool_name": "rm -rf", "args": {}},
                                    approve_on_uncertain=False)
        assert result["pass"] is False
        assert result["action"] == "ask_user"
        assert result["reviewer_unavailable"] is True

    @pytest.mark.asyncio
    async def test_adapter_invoke_exception_ai_review_asks_user(self):
        agent = _make_agent(raise_on_invoke=True)
        result = await agent.review("t1", "pre_dangerous_tool",
                                    {"tool_name": "x", "args": {}},
                                    approve_on_uncertain=False)
        assert result["action"] == "ask_user"

    @pytest.mark.asyncio
    async def test_non_json_output_ai_review_asks_user(self):
        """LLM 输出无法解析成 JSON——AI 代审下同样交回用户。"""
        agent = _make_agent(content="这不是 JSON，是纯文本")
        result = await agent.review("t1", "pre_dangerous_tool",
                                    {"tool_name": "x", "args": {}},
                                    approve_on_uncertain=False)
        assert result["action"] == "ask_user"

    @pytest.mark.asyncio
    async def test_empty_llm_output_ai_review_asks_user(self):
        """空响应（adapter 返回 {'error': ...} 时会退化成这个）也不能放行。"""
        agent = _make_agent(content="")
        result = await agent.review("t1", "pre_dangerous_tool",
                                    {"tool_name": "x", "args": {}},
                                    approve_on_uncertain=False)
        assert result["action"] == "ask_user"

    @pytest.mark.asyncio
    async def test_adapter_not_ready_ai_review_asks_user(self):
        """adapter 存在但未就绪（真实场景：模型没配好）→ 交回用户。"""
        adapter = _MockAdapter(content=_llm_json(True), ready=False)
        agent = FinalReviewAgent(
            adapters={DEFAULT_AGENT_ID: adapter}, model_config=MagicMock())
        result = await agent.review("t1", "pre_dangerous_tool",
                                    {"tool_name": "x", "args": {}},
                                    approve_on_uncertain=False)
        assert result["action"] == "ask_user"
        assert adapter.invoke_calls == []  # 未就绪时不该真的去调

    @pytest.mark.asyncio
    async def test_empty_output_default_mode_still_blocks_nothing(self):
        """默认模式仍是 best-effort：空响应不阻塞（保持历史语义）。"""
        agent = _make_agent(content="")
        result = await agent.review("t1", "pre_dangerous_tool",
                                    {"tool_name": "x", "args": {}})
        assert result["pass"] is True


# ═══════════════════════════════════════════════════════════════
# TestParseResult —— _parse_result 容错
# ═══════════════════════════════════════════════════════════════

class TestParseResult:
    """Phase 83-1: _parse_result 容错"""

    @pytest.mark.asyncio
    async def test_invalid_action_normalized(self):
        """非法 action 值——根据 pass 归一化为 proceed/ask_user"""
        agent = _make_agent(content=_llm_json(True, action="weird"))
        result = await agent.review("t1", "pre_dangerous_tool",
                                   {"tool_name": "x", "args": {}})
        assert result["action"] == "proceed"  # pass=True → proceed

    @pytest.mark.asyncio
    async def test_invalid_action_with_fail(self):
        """非法 action + pass=false → ask_user"""
        agent = _make_agent(content=_llm_json(False, action="weird"))
        result = await agent.review("t1", "pre_dangerous_tool",
                                   {"tool_name": "x", "args": {}})
        assert result["action"] == "ask_user"

    @pytest.mark.asyncio
    async def test_issues_non_list_normalized(self):
        """issues 非列表——归一化为单元素列表"""
        import json as _json
        payload = '{"pass": false, "score": 50, "issues": "单个问题字符串", "action": "retry"}'
        agent = _make_agent(content=f"```json\n{payload}\n```")
        result = await agent.review("t1", "pre_dangerous_tool",
                                   {"tool_name": "x", "args": {}})
        assert result["issues"] == ["单个问题字符串"]

    @pytest.mark.asyncio
    async def test_score_string_float_fallback(self):
        """score 为字符串浮点 '85.5'——int() 失败，fallback 80（不抛异常）"""
        payload = '{"pass": true, "score": "85.5", "action": "proceed"}'
        agent = _make_agent(content=f"```json\n{payload}\n```")
        result = await agent.review("t1", "pre_dangerous_tool",
                                   {"tool_name": "x", "args": {}})
        assert result["score"] == 80  # 容错降级
        assert result["pass"] is True

    @pytest.mark.asyncio
    async def test_score_null_fallback(self):
        """score 为 null——default 不生效（key 存在），TypeError 被捕获，fallback 80"""
        payload = '{"pass": true, "score": null, "action": "proceed"}'
        agent = _make_agent(content=f"```json\n{payload}\n```")
        result = await agent.review("t1", "pre_dangerous_tool",
                                   {"tool_name": "x", "args": {}})
        assert result["score"] == 80

    @pytest.mark.asyncio
    async def test_score_numeric_float_truncated(self):
        """score 为数值浮点 85.9——int() 截断为 85（合法转换）"""
        payload = '{"pass": true, "score": 85.9, "action": "proceed"}'
        agent = _make_agent(content=f"```json\n{payload}\n```")
        result = await agent.review("t1", "pre_dangerous_tool",
                                   {"tool_name": "x", "args": {}})
        assert result["score"] == 85

    def test_extract_json_code_block(self):
        """_extract_json 解析 ```json 代码块"""
        text = '前置文字\n```json\n{"pass": true, "score": 90}\n```\n后置'
        parsed = _extract_json(text)
        assert parsed == {"pass": True, "score": 90}

    def test_extract_json_bare(self):
        """_extract_json 解析裸 JSON"""
        parsed = _extract_json('{"pass": false}')
        assert parsed == {"pass": False}

    def test_extract_json_empty(self):
        """_extract_json 空输入返回 None"""
        assert _extract_json("") is None
        assert _extract_json("无 JSON") is None


# ═══════════════════════════════════════════════════════════════
# TestSingleton —— 全局单例 get/set
# ═══════════════════════════════════════════════════════════════

class TestSingleton:
    """Phase 83-1: 全局单例 get/set_final_review_agent"""

    def test_singleton_get_set(self):
        """set 后 get 返回同一实例"""
        original = get_final_review_agent()
        try:
            agent = FinalReviewAgent(adapters={}, model_config=MagicMock())
            set_final_review_agent(agent)
            assert get_final_review_agent() is agent
        finally:
            set_final_review_agent(original)  # 恢复原始单例


# ═══════════════════════════════════════════════════════════════
# TestPromptRender —— FINAL_REVIEW_PROMPT 渲染
# ═══════════════════════════════════════════════════════════════

class TestPromptRender:
    """Phase 83-2: render_final_review_prompt 渲染"""

    def test_render_pre_dangerous_tool(self):
        """渲染 pre_dangerous_tool stage——含工具名 + 参数"""
        prompt = render_final_review_prompt(
            stage="pre_dangerous_tool",
            task_context="确认目标: 测试",
            review_input="工具名: write_file\n参数: {}",
        )
        assert "终审员" in prompt
        assert "pre_dangerous_tool" in prompt or "危险工具" in prompt
        assert "write_file" in prompt
        assert "测试" in prompt

    def test_render_pre_execution(self):
        """渲染 pre_execution stage——含步骤 prompt"""
        prompt = render_final_review_prompt(
            stage="pre_execution",
            task_context="确认目标: G",
            review_input="步骤 prompt:\n执行 X",
        )
        assert "启动前" in prompt
        assert "执行 X" in prompt

    def test_render_post_execution(self):
        """渲染 post_execution stage"""
        prompt = render_final_review_prompt(
            stage="post_execution",
            task_context="确认目标: G",
            review_input="步骤产出:\n产出 Y",
        )
        assert "完成后" in prompt
        assert "产出 Y" in prompt

    def test_render_unknown_stage_uses_generic(self):
        """未知 stage 用通用描述"""
        prompt = render_final_review_prompt(
            stage="unknown",
            task_context="ctx",
            review_input="input",
        )
        assert "通用审查" in prompt

    def test_render_empty_context_defaults(self):
        """空上下文用默认占位符"""
        prompt = render_final_review_prompt(
            stage="pre_execution",
            task_context="",
            review_input="",
        )
        assert "（无任务上下文）" in prompt
        assert "（无审查输入）" in prompt

    def test_prompt_template_has_required_fields(self):
        """FINAL_REVIEW_PROMPT 含必要字段——action 含义 + 输出格式 + 规则"""
        assert "proceed" in FINAL_REVIEW_PROMPT
        assert "ask_user" in FINAL_REVIEW_PROMPT
        assert "retry" in FINAL_REVIEW_PROMPT
        assert "halt" in FINAL_REVIEW_PROMPT
        assert "```json" in FINAL_REVIEW_PROMPT
        assert "score" in FINAL_REVIEW_PROMPT


# ═══════════════════════════════════════════════════════════════
# TestAiDecidesStrictVerdict —— AI 代审必须"明确表态"才放行
# ═══════════════════════════════════════════════════════════════

class TestAiDecidesStrictVerdict:
    """2026-10-22：AI 代审档位下，审查员没给可识别的 action 就不能当放行。

    此前 action 缺失/非法一律推断成 proceed（"pass=true 就当同意"），等于审查员
    没表态我们替他点头——与 2026-10-08 修的"解析失败默认通过"同类，只是深一层。
    """

    @staticmethod
    def _ctx():
        return {"tool_name": "run_command", "args": {"command": "rm -rf /"}}

    @pytest.mark.asyncio
    async def test_missing_action_asks_user(self):
        import json as _json
        from security.approval import review_outcome

        raw = _json.dumps({"pass": True, "score": 90, "issues": [], "reason": "ok"})
        agent = _make_agent(content=raw)
        result = await agent.review(
            "t1", "pre_dangerous_tool", self._ctx(),
            force=True, approve_on_uncertain=False,
        )
        assert result["action"] == "ask_user"
        assert review_outcome(result) == "uncertain"

    @pytest.mark.asyncio
    async def test_invalid_action_asks_user(self):
        from security.approval import review_outcome

        agent = _make_agent(content=_llm_json(True, action="weird"))
        result = await agent.review(
            "t1", "pre_dangerous_tool", self._ctx(),
            force=True, approve_on_uncertain=False,
        )
        assert review_outcome(result) == "uncertain"

    @pytest.mark.asyncio
    async def test_explicit_proceed_approves(self):
        from security.approval import review_outcome

        agent = _make_agent(content=_llm_json(True, action="proceed"))
        result = await agent.review(
            "t1", "pre_dangerous_tool", self._ctx(),
            force=True, approve_on_uncertain=False,
        )
        assert review_outcome(result) == "approve"

    @pytest.mark.asyncio
    async def test_explicit_halt_denies(self):
        from security.approval import review_outcome

        agent = _make_agent(content=_llm_json(False, action="halt"))
        result = await agent.review(
            "t1", "pre_dangerous_tool", self._ctx(),
            force=True, approve_on_uncertain=False,
        )
        assert review_outcome(result) == "deny"

    @pytest.mark.asyncio
    async def test_proceed_with_pass_false_is_not_approval(self):
        """action=proceed 但 pass=false → 不能算批准（模型自相矛盾）。"""
        from security.approval import review_outcome

        agent = _make_agent(content=_llm_json(False, action="proceed"))
        result = await agent.review(
            "t1", "pre_dangerous_tool", self._ctx(),
            force=True, approve_on_uncertain=False,
        )
        assert review_outcome(result) == "uncertain"

    @pytest.mark.asyncio
    async def test_best_effort_mode_keeps_inference(self):
        """非代审档位（审查只是加道保险）保持原推断语义，不改变既有行为。"""
        agent = _make_agent(content=_llm_json(True, action="weird"))
        result = await agent.review(
            "t1", "pre_dangerous_tool", self._ctx(), force=True,
        )
        assert result["action"] == "proceed"
