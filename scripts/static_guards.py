#!/usr/bin/env python
"""scripts/static_guards.py —— 静态守卫的扫描实现（基线生成 + 校验共用**同一份**逻辑）

为什么单独抽成一个脚本：基线生成与守卫校验若各写一份扫描逻辑，必然漂移——
实测过同类问题（token 字段名在两处各写一份 → 改名即静默失配）。
本文件是**唯一实现**，`tests/contract/test_static_guards_ratchet.py` 直接 import 它。

两个维度：

1. **尺寸**（AGENTS.md 硬约束 #4：文件 <800 行、函数 <50 行）
2. **静默异常**（宽泛 `except Exception` 只 pass/返回空值时必须二选一：
   无回退 → 记日志留痕；有回退 → 注释声明意图。两者皆无 = 静默吞噬）

用法::

    python scripts/static_guards.py                    # 只校验，非 0 退出即违规
    python scripts/static_guards.py --update --only size
    python scripts/static_guards.py --update --only silent
"""
from __future__ import annotations

import ast
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

SIZE_BASELINE = os.path.join(ROOT, "scripts", "size_baseline.json")
SILENT_BASELINE = os.path.join(ROOT, "scripts", "silent_except_baseline.json")

SKIP_DIRS = {
    "__pycache__", "python-embed", "node-embed", ".venv", "node_modules",
    "release", "build", "dist", ".electron-dist",
}
SIZE_SCAN_ROOTS = ("src-python", "src-frontend/src", "src-electron", "scripts")
SIZE_EXTS = (".py", ".ts", ".tsx", ".js")
SILENT_SCAN_ROOTS = (
    "src-python/services", "src-python/routes", "src-python/tools", "src-python/adapters",
    "src-python/repositories", "src-python/workflow", "src-python/harness", "src-python/evolution",
)
# 允许声明"这是有意的回退/尽力而为"——handler 内出现这些词即视为已声明意图
INTENT_HINTS = ("回退", "降级", "best-effort", "best effort", "尽力", "可选", "忽略", "容忍", "不影响")


def _walk(root_rel: str):
    base = os.path.join(ROOT, root_rel)
    if not os.path.isdir(base):
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            yield os.path.relpath(full, ROOT).replace("\\", "/"), full


def _read(full: str) -> str:
    with open(full, encoding="utf-8", errors="ignore") as fh:
        return fh.read()


# ── 维度 1：尺寸 ──────────────────────────────────────────────

def measure_size(file_limit: int, func_limit: int) -> tuple[dict[str, int], dict[str, int]]:
    """返回 (超限文件 -> 行数, 超限函数 `文件::函数名` -> 行数)。"""
    files: dict[str, int] = {}
    funcs: dict[str, int] = {}
    for scan_root in SIZE_SCAN_ROOTS:
        for rel, full in _walk(scan_root) or ():
            if not rel.endswith(SIZE_EXTS):
                continue
            text = _read(full)
            lines = text.count("\n") + 1
            if lines > file_limit:
                files[rel] = lines
            if rel.endswith(".py"):
                try:
                    tree = ast.parse(text)
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        length = (node.end_lineno or node.lineno) - node.lineno + 1
                        if length > func_limit:
                            key = f"{rel}::{node.name}"      # 行号会漂移，用名字做稳定键
                            funcs[key] = max(funcs.get(key, 0), length)
    return files, funcs


# ── 维度 2：静默异常吞噬 ──────────────────────────────────────

def _handler_body(node: ast.ExceptHandler) -> list[ast.stmt]:
    """去掉 docstring 后的处理体。"""
    return [
        n for n in node.body
        if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str))
    ]


def _is_empty_literal(v: ast.expr) -> bool:
    """判断返回值是否是"空值"（None/False/0/""/[]/{} → 与"没找到"不可区分）。"""
    if isinstance(v, ast.Constant):
        return v.value in (None, False, 0, "", b"")
    if isinstance(v, ast.Dict):
        return not v.keys
    if isinstance(v, (ast.List, ast.Tuple)):
        return not v.elts
    return False


def _is_silent(node: ast.ExceptHandler) -> bool:
    """只 pass / continue / 返回空值 → 视为静默。"""
    body = _handler_body(node)
    if not body:
        return True
    for n in body:
        if isinstance(n, (ast.Pass, ast.Continue)):
            continue
        if isinstance(n, ast.Return) and (n.value is None or _is_empty_literal(n.value)):
            continue
        return False
    return True


def _is_broad(node: ast.ExceptHandler) -> bool:
    t = node.type
    return t is None or (isinstance(t, ast.Name) and t.id in ("Exception", "BaseException"))


def measure_silent_except() -> dict[str, int]:
    """返回 文件 -> 未声明意图的静默宽泛异常处理数量。"""
    counts: dict[str, int] = {}
    for scan_root in SILENT_SCAN_ROOTS:
        for rel, full in _walk(scan_root) or ():
            if not rel.endswith(".py"):
                continue
            src = _read(full)
            lines = src.split("\n")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            n_undeclared = 0
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler) or not _is_broad(node):
                    continue
                if not _is_silent(node):
                    continue
                start = node.lineno - 1
                end = max((n.end_lineno or n.lineno) for n in node.body) if node.body else node.lineno
                chunk = "\n".join(lines[start:end])
                if not any(h in chunk for h in INTENT_HINTS):
                    n_undeclared += 1
            if n_undeclared:
                counts[rel] = n_undeclared
    return counts


# ── 基线读写 ──────────────────────────────────────────────────

def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def update(only: str = "") -> None:
    if only in ("", "size"):
        base = load_json(SIZE_BASELINE)
        files, funcs = measure_size(base["limits"]["file_lines"], base["limits"]["func_lines"])
        base["files"], base["funcs"] = dict(sorted(files.items())), dict(sorted(funcs.items()))
        save_json(SIZE_BASELINE, base)
        print(f"size: {len(files)} files, {len(funcs)} funcs")
    if only in ("", "silent"):
        base = load_json(SILENT_BASELINE)
        counts = measure_silent_except()
        base["counts"] = dict(sorted(counts.items()))
        base["total"] = sum(counts.values())
        save_json(SILENT_BASELINE, base)
        print(f"silent_except: {len(counts)} files, {base['total']} sites")


if __name__ == "__main__":
    if "--update" in sys.argv:
        which = ""
        if "--only" in sys.argv:
            which = sys.argv[sys.argv.index("--only") + 1]
        update(which)
    else:
        print("用法: python scripts/static_guards.py [--update [--only size|silent]]")
