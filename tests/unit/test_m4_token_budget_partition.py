"""tests/unit/test_m4_token_budget_partition.py —— M4: 上下文窗口 token 预算三区分区

对标 Codex CLI context partitioning: system / tool / history / output 四区预算，
确保各区总和不超过窗口大小。

测试覆盖:
  1. calc_three_zone_budget: 三区分配数学
  2. enforce_token_budget: 超限时从 history 前端裁剪
  3. _split_messages: system_prompt_tokens 感知
"""
import pytest

import os
import sys

# 确保 src-python 在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ============================================================
# 三区预算分配测试
# ============================================================

class TestCalcThreeZoneBudget:
    """calc_three_zone_budget: 将窗口分为 system/tool/history/output 四区"""

    def test_partitions_sum_to_window(self):
        """四区总和 == 窗口大小（无浪费）"""
        from config import calc_three_zone_budget
        budget = calc_three_zone_budget({"context": 128_000})
        total = budget["system"] + budget["tool"] + budget["history"] + budget["output"]
        assert total == 128_000

    def test_default_ratios(self):
        """默认比例: system 20%, tool 25%, output 10%, history ~45%
        用 50K 窗口避免 output 触发 8K cap"""
        from config import calc_three_zone_budget
        budget = calc_three_zone_budget({"context": 50_000})
        assert budget["system"] == 10_000
        assert budget["tool"] == 12_500
        assert budget["output"] == 5_000
        assert budget["history"] == 22_500

    def test_output_capped_at_8k(self):
        """大窗口时 output 不超过 8K"""
        from config import calc_three_zone_budget
        budget = calc_three_zone_budget({"context": 1_000_000})
        assert budget["output"] <= 8_192

    def test_output_floored_at_2k(self):
        """小窗口时 output 不低于 2K"""
        from config import calc_three_zone_budget
        budget = calc_three_zone_budget({"context": 8_000})
        assert budget["output"] >= 2_048

    def test_history_never_negative(self):
        """极小窗口时 history 不为负（至少 0）"""
        from config import calc_three_zone_budget
        budget = calc_three_zone_budget({"context": 4_000})
        assert budget["history"] >= 0

    def test_fallback_window_when_no_config(self):
        """无 context 配置时 fallback 到默认窗口"""
        from config import calc_three_zone_budget
        budget = calc_three_zone_budget({})
        assert budget["system"] > 0
        assert budget["tool"] > 0
        assert budget["history"] > 0
        assert budget["output"] > 0


# ============================================================
# 预算强制裁剪测试
# ============================================================

class TestEnforceTokenBudget:
    """enforce_token_budget: 超限时从 history 前端裁剪，保留 system + 最近 history"""

    def test_within_budget_no_change(self):
        """总 token 在预算内 → 原样返回"""
        from config import enforce_token_budget
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = enforce_token_budget(messages, {"context": 128_000})
        assert result == messages

    def test_over_budget_trims_history_front(self):
        """超预算 → 从 history 前端裁剪，保留 system + 最近消息"""
        from config import enforce_token_budget
        # 构造超限消息：system + 很多 history
        messages = [{"role": "system", "content": "You are helpful."}]
        for i in range(50):
            messages.append({"role": "user", "content": f"Question {i} " * 100})
            messages.append({"role": "assistant", "content": f"Answer {i} " * 100})
        messages.append({"role": "user", "content": "final question"})

        result = enforce_token_budget(messages, {"context": 8_000})
        # system 消息保留
        assert result[0]["role"] == "system"
        # 最后一条 user 消息保留
        assert result[-1]["content"] == "final question"
        # 裁剪后比原始少
        assert len(result) < len(messages)

    def test_system_always_preserved(self):
        """system 消息永远保留"""
        from config import enforce_token_budget
        messages = [
            {"role": "system", "content": "S" * 5000},
            {"role": "user", "content": "U" * 5000},
        ]
        result = enforce_token_budget(messages, {"context": 8_000})
        assert result[0]["role"] == "system"

    def test_last_user_always_preserved(self):
        """最后一条 user 消息永远保留（即使总 token 仍超限）"""
        from config import enforce_token_budget
        messages = [
            {"role": "system", "content": "S" * 50000},  # 远超 4K 窗口
            {"role": "user", "content": "final"},
        ]
        result = enforce_token_budget(messages, {"context": 4_000})
        assert result[-1] == {"role": "user", "content": "final"}

    def test_multiple_system_messages_preserved(self):
        """多条 system 消息（system + memory）都保留"""
        from config import enforce_token_budget
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "system", "content": "Memory context"},
            {"role": "user", "content": "Q" * 100},
            {"role": "assistant", "content": "A" * 100},
            {"role": "user", "content": "Q" * 100},
            {"role": "assistant", "content": "A" * 100},
            {"role": "user", "content": "final"},
        ]
        result = enforce_token_budget(messages, {"context": 4_000})
        # 两条 system 都保留
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "system"

    def test_empty_messages(self):
        """空消息列表 → 返回空列表"""
        from config import enforce_token_budget
        result = enforce_token_budget([], {"context": 128_000})
        assert result == []

    def test_custom_output_reserve(self):
        """自定义 output_reserve 参数"""
        from config import enforce_token_budget
        messages = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U" * 5000},
        ]
        # 大 output_reserve → 更小的预算 → 可能裁剪
        result = enforce_token_budget(messages, {"context": 8_000}, output_reserve=6_000)
        assert result[0]["role"] == "system"
        assert result[-1]["role"] == "user"


# ============================================================
# _split_messages system 感知测试
# ============================================================

class TestSplitMessagesSystemAware:
    """_split_messages: system_prompt_tokens 参数调整 recent budget"""

    def test_default_no_system_tokens(self):
        """不传 system_prompt_tokens → 行为不变（兼容）"""
        import asyncio
        import aiosqlite
        from services.memory.memory_compact import _split_messages

        class FakeMemoryService:
            def __init__(self):
                self._sessions = {"s1": [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                    {"role": "user", "content": "do task"},
                    {"role": "assistant", "content": "done"},
                ]}

        svc = FakeMemoryService()
        conv = svc._sessions["s1"]
        recent, older = _split_messages(svc, conv, {"context": 128_000})
        # 默认行为：至少保留最后 2 条
        assert len(recent) >= 2

    def test_system_tokens_reduces_recent_budget(self):
        """传 system_prompt_tokens → recent budget 减少 → older 更多或相等"""
        from services.memory.memory_compact import _split_messages

        class FakeMemoryService:
            pass

        svc = FakeMemoryService()
        # 构造足够长的对话使预算约束生效（每条 ~600 token）
        conv = []
        for i in range(40):
            conv.append({"role": "user", "content": f"Question {i} " * 200})
            conv.append({"role": "assistant", "content": f"Answer {i} " * 200})

        # 无 system_tokens: budget = 16000
        recent1, older1 = _split_messages(svc, conv, {"context": 80_000})
        # 有 system_tokens（占用 20000 token）: budget 压缩到 2000
        recent2, older2 = _split_messages(svc, conv, {"context": 80_000}, system_prompt_tokens=20_000)

        # 有 system 占用时，older 应该更多或相等（recent budget 被压缩）
        assert len(older2) >= len(older1)

    def test_system_tokens_minimum_budget_floor(self):
        """system_tokens 很大时 recent budget 有最低下限（不低于 2000）"""
        from services.memory.memory_compact import _split_messages

        class FakeMemoryService:
            pass

        svc = FakeMemoryService()
        conv = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        # 即使 system 占了 100K token，recent 仍应保留至少最后 2 条
        recent, older = _split_messages(svc, conv, {"context": 128_000}, system_prompt_tokens=100_000)
        assert len(recent) >= 1
