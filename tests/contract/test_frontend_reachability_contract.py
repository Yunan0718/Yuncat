"""tests/contract/test_frontend_reachability_contract.py —— 组件必须真的被挂上

2026-09-16 审计发现一批组件写完就放着：驾驶舱抽屉没人渲染，
ArenaLeaderboard / AutoFixPanel / ChatWorkspacePanel / SnapshotTimeline / StatCard /
SystemStatusBar 全局无引用。此后新增组件必须在非测试代码里被引用。
"""
import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_SRC_FE = os.path.abspath(os.path.join(_ROOT, "src-frontend"))
_SRC = os.path.join(_SRC_FE, "src")
_COMPONENTS = os.path.join(_SRC, "components")

# 允许只被测试引用的组件（目前没有）；新增必须写明理由。
_ALLOWLIST: frozenset = frozenset()

_IMPORT_RE = re.compile(r'from\s+"([^"]+)"')
_DYNAMIC_RE = re.compile(r'import\("([^"]+)"\)')


def _read_sources() -> dict:
    """非测试文件 -> 内容"""
    non_test = {}
    for dirpath, dirnames, filenames in os.walk(_SRC):
        dirnames[:] = [d for d in dirnames if d != "node_modules"]
        for fn in filenames:
            if not fn.endswith((".ts", ".tsx")):
                continue
            path = os.path.join(dirpath, fn)
            if "__tests__" in path:
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                non_test[path] = f.read()
    return non_test


def _referenced_stems(non_test: dict) -> set:
    """被 import 过的模块名（去后缀）集合"""
    stems = set()
    for content in non_test.values():
        for target in _IMPORT_RE.findall(content) + _DYNAMIC_RE.findall(content):
            if target.startswith(".") or target.startswith("@/"):
                stems.add(os.path.basename(target))
    return stems


def test_every_component_is_mounted_somewhere():
    non_test = _read_sources()
    referenced = _referenced_stems(non_test)
    orphans = []
    for path in non_test:
        if not path.startswith(_COMPONENTS):
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        if stem in _ALLOWLIST:
            continue
        if stem not in referenced:
            orphans.append(os.path.relpath(path, _SRC_FE))

    assert not orphans, (
        "这些组件写完没有地方挂（删掉或接上）：\n"
        + "\n".join(sorted(orphans))
    )
