"""tests/contract/test_delegation_arg_parity.py —— 委托包装不得丢参数

2026-10-07 新增。起因是一个「看起来写完了、实际链路断在最后一米」的 bug：

`MemoryService.add_message` 的签名声明了 `tool_calls` / `reasoning`，但转发给
`services.memory.memory_sessions.add_message` 时只传了前 5 个位置参数。
后果不是报错，而是**两列永远为空**：

  · `conversations.reasoning_content`（思考链）非空数 = 0
  · `conversations.tool_calls_json`（行动链）非空数 = 0

外层调用全部成功（不抛异常、不记日志），只有读 DB 时才发现两条链路从未落库。
这类"签名对了、转发漏了"的缺陷无法靠类型检查发现，必须用 AST 静态比对钉死。

规则：`*Service` facade 里以 `_impl` 转发的包装函数，其声明的每个具名形参
都必须被转发到（位置传参或关键字传参均可）。`*args/**kwargs` 一律放行。
"""
import ast
import os

import pytest

_SERVICES = os.path.join(
    os.path.dirname(__file__), "..", "..", "src-python", "services"
)


def _collect_violations(base: str) -> list[str]:
    violations: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8", errors="ignore") as fh:
                try:
                    tree = ast.parse(fh.read())
                except SyntaxError:
                    continue
            for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
                for fn in cls.body:
                    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if fn.args.vararg is not None:
                        continue
                    call = _find_impl_call(fn)
                    if call is None:
                        continue
                    declared = [a.arg for a in fn.args.args[1:]]
                    declared += [a.arg for a in fn.args.kwonlyargs]
                    forwarded = {a.id for a in call.args[1:] if isinstance(a, ast.Name)}
                    forwarded |= {k.arg for k in call.keywords if k.arg}
                    missing = [p for p in declared if p not in forwarded]
                    if missing:
                        rel = os.path.relpath(path, os.path.dirname(base))
                        violations.append(
                            f"{rel}:{call.lineno} {cls.name}.{fn.name} 丢失 {missing}"
                        )
    return violations


def _find_impl_call(fn: ast.AST) -> ast.Call | None:
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_impl"
        ):
            return node
    return None


def test_delegation_forwards_all_declared_params():
    violations = _collect_violations(_SERVICES)
    assert not violations, (
        "以下 facade 委托丢失了已声明的参数——调用方不会报错，但数据会静默丢弃:\n  "
        + "\n  ".join(violations)
    )


def test_guard_self_check_detects_known_violation(tmp_path):
    """守卫自检：造一个"声明了却没转发"的样本，必须被抓到。"""
    sample = tmp_path / "services"
    sample.mkdir()
    (sample / "fake.py").write_text(
        "class S:\n"
        "    async def add_message(self, a, b, tool_calls=None):\n"
        "        from x import y as _impl\n"
        "        return await _impl(self, a, b)\n",
        encoding="utf-8",
    )
    assert _collect_violations(str(sample)), "守卫本身失效，样本未被检出"
