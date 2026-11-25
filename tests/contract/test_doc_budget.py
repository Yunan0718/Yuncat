"""文档预算门禁：给人读的文档必须有上限，别退化成没人读的流水账。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from verify_doc_budget import EXCLUSIONS, FILE_BUDGETS, GLOB_BUDGETS, violations


def test_no_doc_exceeds_budget():
    assert violations() == [], (
        "文档超出预算——先想这条能不能删/挪到更专门的文档，"
        "而不是直接调高预算: " + "; ".join(violations())
    )


def test_budgets_are_explicit_and_sane():
    """预算必须有理由、是正数——防止有人把预算改成 99999 了事。"""
    for rel, (budget, reason) in FILE_BUDGETS.items():
        assert budget > 0, rel
        assert reason.strip(), f"{rel} 缺少理由"
    for pattern, (budget, reason) in GLOB_BUDGETS.items():
        assert budget > 0, pattern
        assert reason.strip(), f"{pattern} 缺少理由"


def test_exclusions_have_reasons():
    """显式排除必须写原因——否则"排除"会变成默认。"""
    for rel, reason in EXCLUSIONS.items():
        assert reason.strip(), f"{rel} 被排除但没写原因"
