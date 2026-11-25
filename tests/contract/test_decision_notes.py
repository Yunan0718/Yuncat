"""tests/contract/test_decision_notes.py —— 决策记录格式门禁

对标 deepseek-harness 的 Agent Notes：`docs/decisions/` 下的决策记录必须满足
路径编码（lifecycle/class/日期-主题）、头部固定（标题+Status 与目录一致）、
正文骨架（含**强制**的 `## 备选方案`）。

本测试直接调用 scripts/verify_decision_notes.py，保证"写决策记录"不是自觉行为而是门禁。
"""
import os
import subprocess
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
VERIFIER = os.path.join(REPO_ROOT, "scripts", "verify_decision_notes.py")


def test_decision_notes_pass_format_gate():
    assert os.path.isfile(VERIFIER), f"缺少门禁脚本：{VERIFIER}"
    result = subprocess.run(
        [sys.executable, VERIFIER],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        "决策记录格式违规：\n" + (result.stderr or result.stdout)
    )


def test_decisions_root_exists():
    root = os.path.join(REPO_ROOT, "docs", "decisions")
    assert os.path.isdir(root), "docs/decisions 目录缺失（见 README 约定）"
    assert os.path.isfile(os.path.join(root, "README.md")), "缺少 docs/decisions/README.md"


def test_every_note_declares_alternatives():
    """`## 备选方案` 是硬要求——没有它，决策会被反复重提。"""
    root = os.path.join(REPO_ROOT, "docs", "decisions")
    checked = 0
    for lifecycle in ("proposed", "implemented", "rejected", "archived"):
        for dirpath, _dirs, files in os.walk(os.path.join(root, lifecycle)):
            for name in files:
                if not name.endswith(".md"):
                    continue
                with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                    text = fh.read()
                assert "## 备选方案" in text, f"{name} 缺少 ## 备选方案"
                checked += 1
    assert checked > 0, "docs/decisions 下没有任何决策记录"
