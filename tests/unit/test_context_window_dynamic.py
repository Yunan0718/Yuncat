"""tests/unit/test_context_window_dynamic.py —— Task 7.3 上下文窗口动态化测试

覆盖三个子目标：
1. calc_history_limit 用真实历史均值替代 _AVG_TURN_FALLBACK=1200
2. emergency_trim_history 的 keep_recent 感知模型窗口
3. behaviors MAX_CHARS=5000 用 calc_context_budget

对标 Claude Code / Cursor——上下文预算按模型窗口百分比动态分配，而非固定值。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ═══════════════════════════════════════════════════════════════
# 子目标 1: calc_history_limit 用真实历史均值
# ═══════════════════════════════════════════════════════════════

class TestCalcHistoryLimitRealAvg:
    """Task 7.3 子目标 1: calc_history_limit 接受 avg_turn_tokens 参数"""

    def test_uses_real_avg_when_provided(self):
        """提供 avg_turn_tokens 时使用真实均值而非 fallback 1200"""
        from token_budget import calc_history_limit, _AVG_TURN_FALLBACK
        cfg = {"model": "deepseek-chat", "context": 131_072}

        fallback_result = calc_history_limit(cfg, avg_turn_tokens=0)
        real_avg_result = calc_history_limit(cfg, avg_turn_tokens=600)

        # 600 < 1200，真实均值更小 → 可容纳更多轮数
        assert real_avg_result > fallback_result, (
            f"avg=600 应比 fallback={_AVG_TURN_FALLBACK} 容纳更多轮数"
        )

    def test_falls_back_when_no_avg(self):
        """avg_turn_tokens=0 时使用 _AVG_TURN_FALLBACK=1200（向后兼容）"""
        from token_budget import calc_history_limit, _AVG_TURN_FALLBACK, resolve_context_window
        cfg = {"model": "deepseek-chat", "context": 131_072}

        result = calc_history_limit(cfg, avg_turn_tokens=0)
        window = resolve_context_window(cfg)
        expected = max(10, (window - 24_000) // _AVG_TURN_FALLBACK)
        assert result == expected

    def test_real_avg_inverse_relationship(self):
        """avg_turn_tokens 越大 → 可容纳轮数越少（反比关系）"""
        from token_budget import calc_history_limit
        cfg = {"model": "deepseek-chat", "context": 131_072}

        small_avg = calc_history_limit(cfg, avg_turn_tokens=400)
        large_avg = calc_history_limit(cfg, avg_turn_tokens=2000)
        assert small_avg > large_avg

    def test_estimate_avg_turn_tokens_from_history(self):
        """estimate_avg_turn_tokens 从实际历史内容估算均值"""
        from token_budget import estimate_avg_turn_tokens
        history = [
            {"role": "user", "content": "a" * 500},
            {"role": "assistant", "content": "b" * 750},
        ]
        avg = estimate_avg_turn_tokens(history)
        # (500 + 750) / 2 messages / 2.5 chars_per_token = 250
        assert avg == 250

    def test_estimate_avg_returns_zero_for_empty(self):
        """空历史返回 0（调用方用 fallback）"""
        from token_budget import estimate_avg_turn_tokens
        assert estimate_avg_turn_tokens([]) == 0


# ═══════════════════════════════════════════════════════════════
# 子目标 2: emergency_trim_history keep_recent 感知模型窗口
# ═══════════════════════════════════════════════════════════════

class TestEmergencyTrimModelAware:
    """Task 7.3 子目标 2: emergency_trim_history 的 keep_recent 感知模型窗口"""

    def _make_history(self, n: int) -> list[dict]:
        return [{"role": "user", "content": f"msg {i}", "id": f"m{i}"} for i in range(n)]

    def test_uses_model_window_when_provided(self):
        """提供 model_config 时按窗口动态计算 keep_recent（大窗口保留更多）"""
        from services.agent.agent_compact import emergency_trim_history
        from config import COMPACT_KEEP_RECENT_EMERGENCY

        history = self._make_history(200)
        large_window_cfg = {"model": "deepseek-v4", "context": 1_048_576}

        trimmed = emergency_trim_history(history, model_config=large_window_cfg)
        # 1M 窗口应保留 > 默认 30 条
        assert len(trimmed) > COMPACT_KEEP_RECENT_EMERGENCY, (
            f"1M 窗口应保留 > {COMPACT_KEEP_RECENT_EMERGENCY} 条，实际 {len(trimmed)}"
        )

    def test_falls_back_without_model_config(self):
        """无 model_config 时用默认 keep_recent=30（向后兼容）"""
        from services.agent.agent_compact import emergency_trim_history
        from config import COMPACT_KEEP_RECENT_EMERGENCY

        history = self._make_history(200)
        trimmed = emergency_trim_history(history)
        assert len(trimmed) == COMPACT_KEEP_RECENT_EMERGENCY

    def test_small_window_keeps_default(self):
        """小窗口（128K）至少保留默认 30 条（下限保护）"""
        from services.agent.agent_compact import emergency_trim_history
        from config import COMPACT_KEEP_RECENT_EMERGENCY

        history = self._make_history(200)
        small_window_cfg = {"model": "deepseek-chat", "context": 131_072}

        trimmed = emergency_trim_history(history, model_config=small_window_cfg)
        assert len(trimmed) >= COMPACT_KEEP_RECENT_EMERGENCY

    def test_explicit_keep_recent_overrides_model(self):
        """显式传 keep_recent 时优先于 model_config（调用方控制）"""
        from services.agent.agent_compact import emergency_trim_history

        history = self._make_history(200)
        large_window_cfg = {"model": "deepseek-v4", "context": 1_048_576}

        trimmed = emergency_trim_history(history, keep_recent=15, model_config=large_window_cfg)
        assert len(trimmed) == 15


# ═══════════════════════════════════════════════════════════════
# 子目标 3: behaviors MAX_CHARS 用 calc_context_budget
# ═══════════════════════════════════════════════════════════════

class TestBehaviorsDynamicBudget:
    """Task 7.3 子目标 3: behaviors MAX_CHARS 用 calc_context_budget 动态化"""

    def test_load_behaviors_with_large_model_allows_more(self, monkeypatch, tmp_path):
        """大窗口模型（1M）的 behavior 预算 > 小窗口模型（16K）

        通过 monkeypatch 行为文件内容，确保总量超过小窗口预算但不超过大窗口预算。
        """
        import yaml
        from prompts.commander import _load_behaviors, _load_selected_behaviors

        # 创建临时行为目录——内容总量 ~4000 chars（超过 16K 预算 3000 但不超过 1M 预算 12000）
        beh_dir = str(tmp_path / "behaviors")
        os.makedirs(beh_dir, exist_ok=True)
        # general.yaml: 2 条 behavior，每条 ~1000 chars
        big_text = "x" * 1000
        with open(os.path.join(beh_dir, "general.yaml"), "w", encoding="utf-8") as f:
            yaml.dump({"behavior_a": big_text, "behavior_b": big_text}, f)
        with open(os.path.join(beh_dir, "python.yaml"), "w", encoding="utf-8") as f:
            yaml.dump({"behavior_c": big_text, "behavior_d": big_text}, f)
        # manifest.yaml: priority
        with open(os.path.join(beh_dir, "manifest.yaml"), "w", encoding="utf-8") as f:
            yaml.dump({"behavior_a": 100, "behavior_b": 90, "behavior_c": 80, "behavior_d": 70}, f)

        import prompts.commander as cmd_mod
        monkeypatch.setattr(cmd_mod, "_detect_project_language", lambda: "python")

        small_cfg = {"model": "tiny", "context": 16_384}  # 预算 = max(3000, 16384*2.5*0.015) = 3000
        large_cfg = {"model": "deepseek-v4", "context": 1_048_576}  # 预算 = min(12000, 1M*2.5*0.015) = 12000

        import re as _re
        # 用 selected_ids 路径（更可控）
        text_small = _load_selected_behaviors(
            ["behavior_a", "behavior_b", "behavior_c", "behavior_d"],
            beh_dir, _re, model_config=small_cfg,
        )
        text_large = _load_selected_behaviors(
            ["behavior_a", "behavior_b", "behavior_c", "behavior_d"],
            beh_dir, _re, model_config=large_cfg,
        )

        # 小窗口预算 3000 < 4000 内容 → 截断；大窗口预算 12000 > 4000 → 全部加载
        assert len(text_large) > len(text_small), (
            f"大窗口应加载更多 behavior 内容，实际 large={len(text_large)} small={len(text_small)}"
        )

    def test_load_behaviors_without_model_uses_fallback(self):
        """无 model_config 时用 get_default_model_config()（131K → ~4915 chars）"""
        from prompts.commander import _load_behaviors

        all_ids = [
            "simplicity", "naming", "code-review", "testing", "security-mindset",
            "input-validation", "error-communication", "documentation", "performance",
            "immutability", "file-organization", "communication", "git-workflow",
            "dependency-management",
        ]
        text = _load_behaviors(selected_ids=all_ids)
        # 默认 131K 窗口 → calc_context_budget(131072, 0.015, 3000, 12000) ≈ 4915
        # 截断后 text ≤ budget + 容差
        assert len(text) <= 5500  # 原测试容差保持

    def test_render_commander_prompt_accepts_model_config(self):
        """render_commander_prompt 接受可选 model_config 参数"""
        from prompts.commander import render_commander_prompt
        large_window_cfg = {"model": "deepseek-v4", "context": 1_048_576}

        # 不传 model_config（向后兼容）
        prompt1 = render_commander_prompt(agent_id="hermes")
        assert len(prompt1) > 0

        # 传 model_config（大窗口）
        prompt2 = render_commander_prompt(agent_id="hermes", model_config=large_window_cfg)
        assert len(prompt2) > 0
