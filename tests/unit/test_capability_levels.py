"""tests/unit/test_capability_levels.py —— 能力档只有两档（默认 / MAX）

2026-09-17 收敛：原来是轻量/标准/强力三档 + 名存实亡的 deep_reasoning/long_task
flag。现在只有两套能力集，差别在“能做多大”——下面把这些约束钉住。
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _read(*parts: str) -> str:
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class TestTwoLevelFlags:
    def test_default_level_has_base_only(self):
        """默认档：基础能力全开，MAX 增强一个都不开"""
        from config import BASE_CAPABILITY_FLAGS, MAX_ONLY_CAPABILITY_FLAGS, capability_flags_for_level

        flags = capability_flags_for_level(1)
        assert all(flags.get(k) is True for k in BASE_CAPABILITY_FLAGS), flags
        assert all(not flags.get(k) for k in MAX_ONLY_CAPABILITY_FLAGS), flags

    def test_max_adds_enhancements(self):
        from config import MAX_ONLY_CAPABILITY_FLAGS, capability_flags_for_level

        flags = capability_flags_for_level(2)
        for key, value in MAX_ONLY_CAPABILITY_FLAGS.items():
            assert flags[key] is value, key

    @pytest.mark.parametrize("level", [0, 1, "bogus", None])
    def test_non_max_levels_collapse_to_default(self, level):
        from config import capability_flags_for_level

        assert not capability_flags_for_level(level).get("external_agent")

    def test_is_max_level(self):
        from config import is_max_level

        assert is_max_level(2) and is_max_level(3)
        assert not is_max_level(0) and not is_max_level(1) and not is_max_level(None)

    def test_old_three_level_name_is_gone(self):
        """旧的 CAPABILITY_FLAGS（三档）不能再回来——两档是产品决定"""
        cfg = _read("src-python", "config.py")
        assert "\nCAPABILITY_FLAGS: dict" not in cfg
        assert "MAX_ONLY_CAPABILITY_FLAGS" in cfg


class TestLevelLimits:
    def test_default_vs_max(self):
        from services.capability_limits import limits_for

        default, max_ = limits_for(1), limits_for(2)
        assert (default.parallel_tool_calls, max_.parallel_tool_calls) == (8, 16)
        assert (default.delegate_concurrency, max_.delegate_concurrency) == (10, 20)
        assert (default.max_tool_rounds, max_.max_tool_rounds) == (100, 200)
        assert (default.rollover_generations, max_.rollover_generations) == (6, 12)
        assert (default.parallel_candidates, max_.parallel_candidates) == (3, 5)

    def test_adversarial_is_max_only(self):
        from services.capability_limits import limits_for

        assert limits_for(1).adversarial_replans == 0
        assert 1 <= limits_for(2).adversarial_replans <= 3

    def test_unknown_level_falls_back_to_default(self):
        from services.capability_limits import limits_for

        assert limits_for("bogus") == limits_for(1)


class TestMaxOnlyBehaviors:
    def test_team_tool_needs_parallel_team(self):
        from config import capability_required_by_tool

        assert capability_required_by_tool("spawn_subagent_team") == "parallel_team"
        assert capability_required_by_tool("spawn_subagent") == "subagent_spawn"

    def test_hint_only_for_max(self):
        from config import capability_flags_for_level
        from prompts.commander import render_capability_hint

        assert render_capability_hint(capability_flags_for_level(1)) == ""
        assert "MAX" in render_capability_hint(capability_flags_for_level(2))

    def test_deliberation_gate_is_max_only(self):
        src = _read("src-python", "routes", "agents.py")
        assert "is_max_level(payload.get(\"capability_level\"" in src or "is_max_level(payload.get" in src

    def test_adversarial_review_is_max_only(self):
        src = _read("src-python", "services", "long_task", "review_hooks.py")
        assert "limits_for" in src and "adversarial_replans" in src

    def test_auto_approve_is_audited(self):
        """自动放行（含 MAX 无人值守）必须留审计记录"""
        src = _read("src-python", "services", "tools", "tool_service.py")
        assert "log_audit" in src and "_audit_auto_approval" in src
        assert 'phase=f"auto:{reason}"' in src

    def test_limits_are_wired(self):
        loop = _read("src-python", "services", "agent", "agent_tool_loop.py")
        assert "current_limits().parallel_tool_calls" in loop, "单轮并行应读档位"
        assert "resolve_tool_round_limit(" in loop, "轮次上限应读档位"
        cfg = _read("src-python", "tools", "builtin", "system_tool_config.py")
        assert "current_limits().delegate_concurrency" in cfg
        adv = _read("src-python", "tools", "builtin", "system_tool_advanced.py")
        assert "current_limits().parallel_candidates" in adv
        roll = _read("src-python", "services", "long_task", "window_rollover.py")
        assert "current_limits().rollover_generations" in roll
