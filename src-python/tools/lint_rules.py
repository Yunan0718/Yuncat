"""tools/lint_rules.py —— Yuncat 自定义 lint 规则（AST 实现）

R1: 禁止硬编码绝对路径（检查字符串常量值）
R2: 禁止 print() → 必须用 logging（AST Call 检测）
R3: 禁止裸 except Exception: pass（AST ExceptHandler 检测）
R4: 禁止 f-string SQL 构造（AST Call + JoinedStr 检测）

Task 4.3: 原 regex 实现误报率高——字符串字面量/注释中的 "print(" / "except Exception: pass"
会被误匹配。AST 实现只检测真实代码节点，消除注释/字符串误报。对标 Ruff / ESLint / pyflakes。

ALLOWED_VIOLATIONS 改用 snippet 匹配（去空格前 60 字符）替代行号——行号随代码漂移失效，
snippet 对行号漂移免疫。对标 Ruff noqa 的 per-line comment / ESLint disable 规则。
"""
import ast
import re
import sys
from pathlib import Path

SRC = Path(__file__).parent.parent  # src-python/ 目录

# R1 仍需 regex 检查字符串常量值（AST Constant 里的 str）
# Task 4.3: 改进 regex——(?<!\w) 排除 URL 协议部分（http:// 里的 p:/ 不再误报）
# 原 regex [A-Za-z]:[/\\] 会匹配 http://127.0.0.1 里的 p:/ —— 严重误报
_R1_PATTERN = re.compile(r'(?<!\w)[A-Za-z]:[\\/]|/home/|/Users/')

EXCLUDE = {"node-embed", "python-embed", "__pycache__", "tests/", "Lib/site-packages", ".venv", "lint_rules.py", "_gen_baseline.py", "_gen_baseline_json.py"}

# ═══════════════════════════════════════════════════════════════
# 已知违规 baseline——从 lint_baseline.json 加载
# Task 4.3: 改用 JSON 外部文件存储 baseline，snippet 匹配替代行号（行号漂移免疫）。
# 对标 Ruff noqa / ESLint disable——baseline 冻结历史技术债务，CI 只检测新增违规。
# 更新 baseline: python tools/_gen_baseline_json.py
# ═══════════════════════════════════════════════════════════════
import json as _json

_BASELINE_PATH = Path(__file__).parent / "lint_baseline.json"


def _load_baseline() -> dict[tuple[str, str, str], str]:
    """从 lint_baseline.json 加载 baseline——返回 {(file, rule, snippet): reason}。"""
    if not _BASELINE_PATH.exists():
        return {}
    try:
        data = _json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
        result = {}
        for entry in data:
            key = (entry["file"], entry["rule"], entry["snippet"])
            result[key] = entry.get("reason", "baseline")
        return result
    except Exception:
        return {}


ALLOWED_VIOLATIONS = _load_baseline()


def _snippet_from_lines(lines: list[str], lineno: int, end_lineno: int | None = None, max_len: int = 120) -> str:
    """提取违规代码的 snippet（拼接多行，去空格，截断到 max_len）。

    Task 4.3: 对于跨行调用（如 execute(f"..." 多行），用 end_lineno 拼接完整范围。
    """
    end = end_lineno or lineno
    start = max(1, lineno)
    parts = []
    for i in range(start, min(end, len(lines)) + 1):
        parts.append(lines[i - 1].strip())
    s = " ".join(parts)
    return s[:max_len]


# ═══════════════════════════════════════════════════════════════
# AST 规则检测器
# ═══════════════════════════════════════════════════════════════

def _check_r1(tree: ast.AST, lines: list[str]) -> list[tuple[int, str, str]]:
    """R1: 字符串常量中的硬编码绝对路径。

    返回 [(lineno, msg, snippet), ...]
    """
    issues = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _R1_PATTERN.search(node.value):
                end_ln = getattr(node, "end_lineno", None) or node.lineno
                snippet = _snippet_from_lines(lines, node.lineno, end_ln)
                issues.append((node.lineno, "硬编码路径", snippet))
    return issues


def _check_r2(tree: ast.AST, lines: list[str]) -> list[tuple[int, str, str]]:
    """R2: print() 调用（非 logging.print）。

    AST Call 节点 func.id == 'print'——字符串字面量中的 "print(" 不再误报。
    """
    issues = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "print":
                end_ln = getattr(node, "end_lineno", None) or node.lineno
                snippet = _snippet_from_lines(lines, node.lineno, end_ln)
                issues.append((node.lineno, "print() 调用 — 使用 logging", snippet))
    return issues


def _check_r3(tree: ast.AST, lines: list[str]) -> list[tuple[int, str, str]]:
    """R3: 裸 except Exception: pass（body 只有 Pass）。

    AST ExceptHandler type=Exception 且 body=[Pass]——注释不再误报。
    """
    issues = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            is_exception = (
                isinstance(node.type, ast.Name) and node.type.id == "Exception"
            ) or (
                isinstance(node.type, ast.Attribute) and node.type.attr == "Exception"
            )
            if not is_exception:
                continue
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                end_ln = getattr(node, "end_lineno", None) or node.lineno
                snippet = _snippet_from_lines(lines, node.lineno, end_ln)
                issues.append((node.lineno, "裸 except Exception: pass", snippet))
    return issues


def _check_r4(tree: ast.AST, lines: list[str]) -> list[tuple[int, str, str]]:
    """R4: f-string SQL 构造（execute(f"...")）。

    AST Call func.attr=='execute' 且 args[0] 是 JoinedStr——注释不再误报。
    用 end_lineno 拼接跨行调用的完整 snippet。
    """
    issues = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
                if node.args and isinstance(node.args[0], ast.JoinedStr):
                    end_ln = getattr(node, "end_lineno", None) or node.lineno
                    snippet = _snippet_from_lines(lines, node.lineno, end_ln)
                    issues.append((node.lineno, "f-string SQL 构造 — 使用参数化查询", snippet))
    return issues


_RULE_CHECKERS = [
    ("R1", _check_r1),
    ("R2", _check_r2),
    ("R3", _check_r3),
    ("R4", _check_r4),
]


def lint_file(f: Path) -> list[str]:
    """对单个文件运行 AST lint——返回违规消息列表。"""
    issues: list[str] = []
    try:
        content = f.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return issues
    try:
        tree = ast.parse(content, filename=str(f))
    except SyntaxError:
        # 语法错误文件跳过 AST 检查（不应出现在生产代码）
        return issues
    lines = content.split("\n")
    rel = f.relative_to(SRC).as_posix()
    for rule_id, checker in _RULE_CHECKERS:
        for lineno, msg, snippet in checker(tree, lines):
            key = (rel, rule_id, snippet)
            if key not in ALLOWED_VIOLATIONS:
                issues.append(f"{f}:{lineno} [{rule_id}] {msg}")
    return issues


def main() -> int:
    issues: list[str] = []
    total = 0
    for pyf in SRC.glob("**/*.py"):
        if any(e in str(pyf) for e in EXCLUDE):
            continue
        total += 1
        issues.extend(lint_file(pyf))
    if issues:
        print(f"FAIL: {len(issues)} violations in {total} files (new issues, see above):\n" + "\n".join(issues[:50]))
        return 1
    print(f"PASS: {total} files scanned, {len(ALLOWED_VIOLATIONS)} known violations suppressed, 0 new violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
