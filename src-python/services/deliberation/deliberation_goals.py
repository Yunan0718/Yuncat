"""services/deliberation_goals.py —— Phase 45 D.2: 内置 /goal 条件函数。

从 deliberation_service.py 拆分而来，保留全部功能。
"""
from __future__ import annotations


# ═══════════════════════════════════════════════════════════
# Phase 45 D.2: 内置 /goal 条件函数
# ═══════════════════════════════════════════════════════════

def goal_all_tests_pass(audit: dict) -> bool:
    """所有验证命令通过 + 零 MISMATCH + 零 MISSING。
    对标 OpenAI: exit code ≠ 0 → 不收敛。
    """
    vr = audit.get("verification_results", [])
    all_pass = all(r.get("passed", True) for r in vr) if vr else audit.get("verification_passed", True)
    mismatches = audit.get("mismatches", 0)
    missing = audit.get("missing", 0)
    return all_pass and mismatches == 0 and missing == 0


def goal_mismatch_zero(audit: dict) -> bool:
    """仅要求零 MISMATCH + 零 MISSING——验证命令允许失败。
    用于 Spec 精确性优先于实现正确性的场景。
    """
    return audit.get("mismatches", 0) == 0 and audit.get("missing", 0) == 0


def goal_verification_only(audit: dict) -> bool:
    """仅要求所有验证命令通过——允许 MISMATCH（例如格式差异）。
    用于快速迭代场景。
    """
    vr = audit.get("verification_results", [])
    if vr:
        return all(r.get("passed", True) for r in vr)
    return audit.get("verification_passed", True)
