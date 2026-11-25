#!/usr/bin/env python
"""scripts/verify_decision_notes.py —— 决策记录格式门禁

对标 deepseek-harness 的 Agent Notes（`.agents/notes/`）机制，Yuncat 落到
`docs/decisions/`。核心约束（机器可校验）：

1. 路径编码三要素：`{lifecycle}/{class}/yyyy-mm-dd-topic.md`
   - lifecycle ∈ {proposed, implemented, rejected, archived}
   - class ∈ {feature, bug-fix, simplification, architecture, process, testing}
2. 头部固定：首行 `# 决策记录：<标题>`，空行，第三个非空行 `Status: ...`；
   Status 必须与所在 lifecycle 目录一致（rejected 需附一行原因）。
3. 正文骨架按 lifecycle：
   - implemented：`## 问题` / `## 决策` / `## 备选方案` / `## 后果`
   - proposed：`## 问题` / `## 提案` / `## 备选方案` / `## 验收标准` / `## 风险`
   - rejected：`## 问题` / `## 提案` + Status 行给出否决原因
4. `## 备选方案` **强制存在**——没有记录"否决了什么"的决策会招致反复重提，
   这正是本机制要防的失败模式。
5. implemented 不得出现提案期标题（`## 提案` / `## 验收标准` / `## 迁移计划`）。

用法：``python scripts/verify_decision_notes.py``（退出码非 0 即有违规）。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "decisions")

LIFECYCLES = ("proposed", "implemented", "rejected", "archived")
CLASSES = ("feature", "bug-fix", "simplification", "architecture", "process", "testing")
ARCHIVED_CLASSES = CLASSES  # archived 保留 class 分层

FILENAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-[a-z0-9][a-z0-9-]*\.md$")
STATUS_RE = re.compile(r"^Status:\s*(proposed|implemented|rejected)\s*(.*)$")

# 每个 lifecycle 必需的二级标题（顺序不强制，存在性强制）
REQUIRED_SECTIONS = {
    "proposed": ("## 问题", "## 提案", "## 备选方案", "## 验收标准", "## 风险"),
    "implemented": ("## 问题", "## 决策", "## 备选方案", "## 后果"),
    "rejected": ("## 问题", "## 提案", "## 备选方案"),
    "archived": ("## 问题", "## 决策", "## 备选方案", "## 后果"),
}
# implemented / archived 不得出现的提案期标题
PROPOSAL_PERIOD_SECTIONS = ("## 提案", "## 验收标准", "## 迁移计划")
EXEMPT_FILES = {"README.md"}


def _iter_note_files():
    for lifecycle in LIFECYCLES:
        lifecycle_dir = os.path.join(ROOT, lifecycle)
        if not os.path.isdir(lifecycle_dir):
            continue
        for class_name in sorted(os.listdir(lifecycle_dir)):
            class_dir = os.path.join(lifecycle_dir, class_name)
            if not os.path.isdir(class_dir):
                continue
            if class_name not in (ARCHIVED_CLASSES if lifecycle == "archived" else CLASSES):
                yield class_dir, None, f"未知 class 目录：{lifecycle}/{class_name}"
                continue
            for filename in sorted(os.listdir(class_dir)):
                if filename in EXEMPT_FILES or not filename.endswith(".md"):
                    continue
                yield class_dir, filename, None


def _check_file(path: str, lifecycle: str) -> list[str]:
    problems: list[str] = []
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    lines = text.split("\n")

    # ---- 头部 ----
    if not lines or not lines[0].startswith("# 决策记录："):
        problems.append("首行必须是 `# 决策记录：<标题>`")
    # 头部固定三行：第 1 行标题、第 2 行空行、第 3 行 Status（对齐 dsh 的 in-file format）
    if len(lines) < 3 or lines[1].strip() != "" or not lines[2].startswith("Status:"):
        problems.append("头部必须严格为：第 1 行标题、第 2 行空行、第 3 行 `Status: ...`")
    else:
        m = STATUS_RE.match(lines[2])
        if not m:
            problems.append(f"Status 取值非法：{non_empty[2]!r}")
        else:
            declared = m.group(1)
            if declared != lifecycle:
                problems.append(f"Status({declared}) 与所在目录({lifecycle}) 不一致")
            if declared == "rejected" and not m.group(2).strip():
                problems.append("rejected 必须在 Status 行附否决原因：`Status: rejected — <原因>`")

    # ---- 正文骨架 ----
    for section in REQUIRED_SECTIONS.get(lifecycle, ()):
        if section not in text:
            problems.append(f"缺少必需小节：{section}")
    if lifecycle in ("implemented", "archived"):
        for section in PROPOSAL_PERIOD_SECTIONS:
            if section in text:
                problems.append(f"implemented/archived 不得出现提案期小节：{section}")
    return problems


def main() -> int:
    if not os.path.isdir(ROOT):
        print(f"[决策记录门禁] 未找到目录：{os.path.normpath(ROOT)}", file=sys.stderr)
        return 1

    violations: list[tuple[str, str]] = []
    count = 0
    for class_dir, filename, dir_problem in _iter_note_files():
        rel_dir = os.path.relpath(class_dir, ROOT).replace("\\", "/")
        lifecycle = rel_dir.split("/")[0]
        if dir_problem:
            violations.append((rel_dir, dir_problem))
            continue
        if filename is None:
            continue
        if not FILENAME_RE.match(filename):
            violations.append((f"{rel_dir}/{filename}", "文件名须为 yyyy-mm-dd-topic.md（小写字母/数字/连字符）"))
            continue
        count += 1
        path = os.path.join(class_dir, filename)
        for problem in _check_file(path, lifecycle):
            violations.append((f"{rel_dir}/{filename}", problem))

    if violations:
        print(f"[决策记录门禁] 发现 {len(violations)} 处违规（已检查 {count} 篇）：", file=sys.stderr)
        for where, what in violations:
            print(f"  - {where}: {what}", file=sys.stderr)
        return 1
    print(f"[决策记录门禁] 通过（{count} 篇）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
