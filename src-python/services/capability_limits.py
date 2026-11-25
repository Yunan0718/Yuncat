"""services/capability_limits.py —— 能力档位的数值上限（单一真源）

能力档只有两档（默认 / MAX），差别不只是“开不开”，还有“能做多大”：

| 上限 | 默认 | MAX | 对应主流 |
|---|---|---|---|
| 单轮工具并行 | 8 | 16 | Codex/Cursor 都允许同一轮多个独立调用并行 |
| 委托/子 Agent 并发 | 10 | 20 | 同上 |
| 单任务工具轮次 | 100 | 200 | Claude Code 默认 max_turns=100 |
| 长任务窗口翻滚代数 | 6 | 12 | 长任务续跑能力 |
| best_of_n / multi 路数 | 3 | 5 | 成本乘数 |
| 对抗验证重规划 | 0（不启用） | 1~3（AI 自己判断） | MAX 专属 |

为什么单独一个模块：这些数字原来散在 config.py / token_budget.py /
window_rollover.py / system_tool_config.py 里，而且不随档位变化——现在按档位取，
新增一档只改这里一张表。
"""
from dataclasses import dataclass

from config import DEFAULT_CAPABILITY_LEVEL, is_max_level


@dataclass(frozen=True)
class LevelLimits:
    """一个能力档的数值上限"""

    parallel_tool_calls: int
    delegate_concurrency: int
    max_tool_rounds: int
    rollover_generations: int
    parallel_candidates: int      # best_of_n / multi 的候选数上限
    adversarial_replans: int      # 对抗验证允许重规划的次数（0 = 不启用）


LIMITS: dict[str, LevelLimits] = {
    "default": LevelLimits(
        parallel_tool_calls=8,
        delegate_concurrency=10,
        max_tool_rounds=100,
        rollover_generations=6,
        parallel_candidates=3,
        adversarial_replans=0,
    ),
    "max": LevelLimits(
        parallel_tool_calls=16,
        delegate_concurrency=20,
        max_tool_rounds=200,
        rollover_generations=12,
        parallel_candidates=5,
        adversarial_replans=3,
    ),
}


def limits_for(level) -> LevelLimits:
    """按能力档取上限（不认识的档一律当默认）"""
    return LIMITS["max" if is_max_level(level) else "default"]


def current_limits() -> LevelLimits:
    """当前调用链的上限（从 context 的能力档读；未设定时默认档）"""
    from context import get_current_capability_level

    level = get_current_capability_level()
    return limits_for(DEFAULT_CAPABILITY_LEVEL if level is None else level)


def resolve_tool_round_limit(model_cfg: dict, user_max_rounds: int | None) -> int:
    """单任务工具轮次上限（默认 100 / MAX 200），用户自定义只能在档位上限内生效"""
    from token_budget import calc_max_tool_rounds

    cap = current_limits().max_tool_rounds
    rounds = min(int(user_max_rounds), cap) if user_max_rounds else cap
    return calc_max_tool_rounds(model_cfg, hard_limit=rounds)
