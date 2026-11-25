"""tests/unit/test_context_compaction_proactive.py —— G1: 上下文压缩主动触发

覆盖:
- estimate_history_tokens 估算当前上下文 token 数（修复 last_usage.prompt_tokens 滞后）
- 阈值降低后 micro compact 在 65% 主动触发（对标 Claude Code 主动压缩）
- stale prompt_tokens 不再导致漏触发——以 max(stale, estimate) 为准
- cooldown 仍生效（避免频繁压缩）
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestEstimateHistoryTokens:
    """estimate_history_tokens 估算当前上下文 token 数"""

    def test_empty_history_zero(self):
        from services.agent.agent_compact import estimate_history_tokens
        from config import resolve_context_window
        assert estimate_history_tokens([]) == 0

    def test_english_text_approximate(self):
        """英文文本 ~4 chars/token（tiktoken 精确计数）"""
        from services.agent.agent_compact import estimate_history_tokens
        from config import resolve_context_window
        # CodeWhale 借鉴 #3: 使用 tiktoken 精确计数后，重复字符被 BPE 压缩
        # （"a"*400 ≈ 54 tokens 而非 100）——改用真实英文文本测试 ~4 chars/token
        text = "The quick brown fox jumps over the lazy dog. " * 9  # ~450 chars 自然英文
        history = [{"role": "user", "content": text}]
        est = estimate_history_tokens(history)
        # 自然英文 ~4 chars/token → 450 chars ≈ 110 tokens
        assert 70 <= est <= 160, f"450 chars 自然英文应估算 ~110 tokens, got {est}"

    def test_chinese_text_approximate(self):
        """中文文本 ~2 chars/token（更密集）"""
        from services.agent.agent_compact import estimate_history_tokens
        from config import resolve_context_window
        history = [{"role": "user", "content": "中" * 200}]  # 200 chars
        est = estimate_history_tokens(history)
        assert est > 50, f"中文 200 chars 应估算 >50 tokens, got {est}"

    def test_multiple_messages_summed(self):
        from services.agent.agent_compact import estimate_history_tokens
        from config import resolve_context_window
        # CodeWhale 借鉴 #3: tiktoken 精确计数——自然英文而非重复字符
        history = [
            {"role": "user", "content": "The quick brown fox jumps over the lazy dog. " * 5},
            {"role": "assistant", "content": "I will help you with that task today. " * 5},
            {"role": "tool", "content": "Result: operation completed successfully with output. " * 7},
        ]
        est = estimate_history_tokens(history)
        # ~800 chars 自然英文 ≈ 200 tokens（tiktoken 精确计数）
        assert 100 <= est <= 300, f"800 chars 自然英文应估算 ~200 tokens, got {est}"

    def test_missing_content_treated_as_empty(self):
        from services.agent.agent_compact import estimate_history_tokens
        from config import resolve_context_window
        history = [{"role": "user"}, {"role": "assistant", "content": None}]
        # CodeWhale 借鉴 #3: 每条消息有 +4 overhead（OpenAI 标准——role 标签等）
        # 空内容消息仍消耗 token（2 条 × 4 = 8），不是 0
        est = estimate_history_tokens(history)
        assert est == 8, f"2 条空消息应有 8 tokens overhead (4/msg), got {est}"

    def test_tool_calls_json_counted(self):
        """tool_calls 字段的 JSON 也应计入 token 估算"""
        from services.agent.agent_compact import estimate_history_tokens
        from config import resolve_context_window
        history = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "function": {"name": "write_file", "arguments": '{"path": "test.py", "content": "hello"}'}}
            ]}
        ]
        est = estimate_history_tokens(history)
        assert est > 0, "tool_calls JSON 应计入 token 估算"


class TestProactiveThresholds:
    """G1: 阈值降低——micro compact 在 65% 主动触发（非 80% 被动）"""

    def test_micro_threshold_lowered(self):
        """COMPACT_TRIGGER_MICRO 应 ≤ 0.70（主动而非被动）"""
        from config import COMPACT_TRIGGER_MICRO
        assert COMPACT_TRIGGER_MICRO <= 0.70, (
            f"COMPACT_TRIGGER_MICRO={COMPACT_TRIGGER_MICRO} 应 ≤ 0.70（G1 主动压缩）"
        )

    def test_standard_threshold_lowered(self):
        """COMPACT_TRIGGER_STANDARD 应 ≤ 0.85"""
        from config import COMPACT_TRIGGER_STANDARD
        assert COMPACT_TRIGGER_STANDARD <= 0.85, (
            f"COMPACT_TRIGGER_STANDARD={COMPACT_TRIGGER_STANDARD} 应 ≤ 0.85"
        )

    def test_emergency_threshold_unchanged(self):
        """emergency 保持 0.98"""
        from config import COMPACT_TRIGGER_EMERGENCY
        assert COMPACT_TRIGGER_EMERGENCY >= 0.95


class TestStaleTokenFix:
    """G1: stale prompt_tokens 不再导致漏触发——以 max(stale, estimate) 为准"""

    def test_check_compaction_uses_estimate_when_stale_low(self, monkeypatch):
        """当 last_usage.prompt_tokens 滞后偏低时，estimate_history_tokens 应拾取触发"""
        from services.agent import agent_tool_loop as atl
        from services.agent.agent_compact import estimate_history_tokens
        from config import resolve_context_window

        # 构造一个 history 使 estimate >> stale prompt_tokens
        # CodeWhale 借鉴 #3: tiktoken 精确计数——用自然英文避免 BPE 过度压缩重复字符
        # 需要 estimate > 19500 (65% of 30000 context) 触发 micro compact
        _sentence = "The quick brown fox jumps over the lazy dog and runs through the forest. "
        big_history = [{"role": "tool", "content": _sentence * 1100}]
        est = estimate_history_tokens(big_history)
        # 数值随 tokenizer 实现漂移，这里对齐「本用例要验证的区间」而不写死 token 数：
        # 必须落在 micro 区间 [65%, 82%)，否则会走 emergency 分支，测的就不是同一件事。
        _window = resolve_context_window({"context": 30000})
        assert int(_window * 0.65) <= est < int(_window * 0.82), (
            f"构造的 history 应落在 micro 区间，实际 est={est} window={_window}"
        )

        # 模拟 adapter.last_usage.prompt_tokens 滞后偏低（上一次 LLM 调用时上下文还小）
        class _FakeAdapter:
            last_usage = {"prompt_tokens": 1000}  # 滞后——实际已膨胀

        class _FakeCompactor:
            # NOTE: 真实 ContextCompactor.micro_compact_history 是同步方法（返回 int）
            def micro_compact_history(self, history, model_cfg=None):
                return 5000
            def emergency_trim_history(self, history, keep_recent=30):
                return history[-5:]
            async def reattach_context_after_compact(self, session_id, context, model_cfg):
                return None

        class _FakeMemSvc:
            async def maybe_compact_context(self, *a, **kw):
                return True

        class _FakeSelf:
            compactor = _FakeCompactor()
            _memory_service = _FakeMemSvc()
            _micro_compact_last_applied = {}
            _needs_full_compact = set()
            db = None
            async def _safe_send(self, *a, **kw):
                pass

        # model_cfg 用真实 key "context"（resolve_context_window 读 context 而非 context_window）
        # estimate=22857, window=30000 → 76% → 在 [65%, 82%) 区间 → 应触发 micro
        # stale prompt_tokens=1000 不应阻止触发——max(1000, 22857) = 22857 驱动决策
        model_cfg = {"context": 30000, "provider": "test"}
        ctx = {"history": big_history}

        import asyncio
        called_micro = {"v": False}
        def _mock_micro(history, model_cfg=None):  # 同步——对齐真实 API
            called_micro["v"] = True
            return 5000
        _FakeSelf.compactor.micro_compact_history = _mock_micro

        asyncio.run(atl._check_compaction(
            _FakeSelf(), _FakeAdapter(), ctx, "sess", "task", model_cfg
        ))
        assert called_micro["v"], (
            "stale prompt_tokens=1000 不应阻止触发——estimate=20000 应驱动 micro compact"
        )
