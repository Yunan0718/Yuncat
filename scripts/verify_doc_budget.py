#!/usr/bin/env python
"""scripts/verify_doc_budget.py —— 文档预算门禁

对标 DSH 的 doc budget：**给人读的文档必须有上限**。没有上限时，
每个 Phase 都往里追加一段，AGENTS.md 从"新 agent 先读这一页"变成没人读的流水账，
决策记录也会退化成大段复述——结论埋在字里行间。

三条预算（数值是预算，不是目标；超了先想"这条能不能删/挪到更专门的文档"）：

| 文件 | 行数上限 | 理由 |
|---|---|---|
| `AGENTS.md` | 200 | 新 agent 的第一入口，一次读完才有用（当前 141） |
| `CLAUDE.md` | 220 | 同上（Claude 入口，略有出入） |
| `docs/decisions/**/*.md` | 120 | 单篇决策记录讲清一个问题就够（当前最大 85） |

`PROGRESS.md` 显式排除：它是按 Phase 追加的历史流水账，不是"要先读完"的文档。

用法：``python scripts/verify_doc_budget.py``（退出码非 0 即超预算）
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

# 文件 → (行数上限, 理由)
FILE_BUDGETS: dict[str, tuple[int, str]] = {
    "AGENTS.md": (200, "新 agent 的第一入口，一次读完才有用"),
    "CLAUDE.md": (220, "Claude 入口，同样要能一次读完"),
}
# 逐篇预算（glob → (上限, 理由)）
GLOB_BUDGETS: dict[str, tuple[int, str]] = {
    "docs/decisions/**/*.md": (120, "单篇决策记录讲清一个问题就够"),
}
# 显式排除（写清原因，别让排除变成默认）
EXCLUSIONS: dict[str, str] = {
    "PROGRESS.md": "按 Phase 追加的历史流水账，不是要先读完的文档",
}


def _line_count(path: str) -> int:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return sum(1 for _ in fh)


def violations() -> list[str]:
    """返回超预算的文件说明；空列表表示通过。"""
    import glob

    out: list[str] = []
    for rel, (budget, reason) in FILE_BUDGETS.items():
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        lines = _line_count(path)
        if lines > budget:
            out.append(f"{rel}: {lines} 行 > 预算 {budget}（{reason}）")
    for pattern, (budget, reason) in GLOB_BUDGETS.items():
        for path in sorted(glob.glob(os.path.join(ROOT, pattern), recursive=True)):
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            lines = _line_count(path)
            if lines > budget:
                out.append(f"{rel}: {lines} 行 > 预算 {budget}（{reason}）")
    return out


if __name__ == "__main__":
    problems = violations()
    if problems:
        print("[文档预算] 超预算：")
        for item in problems:
            print("  -", item)
        sys.exit(1)
    print("[文档预算] 通过")
