"""临时脚本：生成 lint baseline JSON 文件——收集当前所有违规的 snippet"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.lint_rules import (
    SRC, EXCLUDE, _check_r1, _check_r2, _check_r3, _check_r4, _RULE_CHECKERS
)
from pathlib import Path
import ast

REASONS = {
    "R1": "危险路径黑名单守卫列表，非硬编码访问路径",
    "R2": "构建脚本（非运行时），print() 为 CLI 输出",
    "R3": "best-effort 清理——后续可改为 logging.debug",
    "R4": "动态 SQL 构造（列名/表名来自硬编码白名单，值已参数化）或 PRAGMA 不支持参数化",
}

baseline = {}
for pyf in SRC.glob("**/*.py"):
    if any(e in str(pyf) for e in EXCLUDE):
        continue
    try:
        content = pyf.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(content, filename=str(pyf))
    except Exception:
        continue
    lines = content.split("\n")
    rel = pyf.relative_to(SRC).as_posix()
    for rule_id, checker in _RULE_CHECKERS:
        for lineno, msg, snippet in checker(tree, lines):
            key = f"{rel}|{rule_id}|{snippet}"
            if key not in baseline:
                baseline[key] = {
                    "file": rel,
                    "rule": rule_id,
                    "snippet": snippet,
                    "line": lineno,
                    "reason": REASONS.get(rule_id, "Task 4.3 baseline"),
                }

out_path = Path(__file__).parent / "lint_baseline.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(list(baseline.values()), f, ensure_ascii=False, indent=2)
print(f"Wrote {len(baseline)} entries to {out_path}", file=sys.stderr)
