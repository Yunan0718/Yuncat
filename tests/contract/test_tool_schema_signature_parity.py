"""工具 schema 与实现签名必须对得上（契约守卫）。

问题形态（2026-10-22 实测）：`run_command` 的 schema 声明了 `shell_id`
（模型据此把持久 Shell id 传进来），但函数签名里没有这个参数——
ToolService 用 `tool.func(**arguments)` 调用，直接 TypeError：
"run_command() got an unexpected keyword argument 'shell_id'"。
整个持久 Shell 能力（open_shell → 带 shell_id 执行）从入口就不可用，
而工具列表里它看起来完全正常。

这类 bug 不会在调用方暴露：schema 是给模型看的，签名是给 Python 看的，
两边各写各的，中间的偏差只有真正调用时才炸。所以必须用守卫盯住。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

import tools.builtin.system_tools  # noqa: F401  触发内置工具注册
from tools.registry import get_all_tools


def _declared_params(meta) -> set[str]:
    schema = meta.parameters or {}
    return set((schema.get("properties") or {}).keys())


def _accepted_params(func) -> tuple[set[str], bool]:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return set(), True
    accepts_kwargs = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    return set(sig.parameters.keys()), accepts_kwargs


def test_tool_schema_params_are_accepted_by_implementation():
    """schema 声明的每个参数，实现都要能接住——否则模型一传就 TypeError。"""
    mismatches = []
    for name, meta in sorted(get_all_tools().items()):
        accepted, accepts_kwargs = _accepted_params(meta.func)
        if accepts_kwargs:
            continue
        missing = sorted(_declared_params(meta) - accepted)
        if missing:
            mismatches.append(f"{name}: {missing}")
    assert not mismatches, (
        "工具 schema 声明了实现不接受的参数（调用即 TypeError）: " + "; ".join(mismatches)
    )


def test_required_params_are_not_missing_from_schema():
    """required 里的参数必须出现在 schema 的 properties 中——否则模型无从得知。"""
    problems = []
    for name, meta in sorted(get_all_tools().items()):
        schema = meta.parameters or {}
        properties = set((schema.get("properties") or {}).keys())
        for required in schema.get("required") or []:
            if required not in properties:
                problems.append(f"{name}: required={required}")
    assert not problems, (
        "工具 schema 的 required 参数未出现在 properties 中: " + "; ".join(problems)
    )
