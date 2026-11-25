"""tests/contract/test_api_surface_reverse.py —— 后端路由 → 前端引用 反向棘轮

正向门禁（tests/contract/test_api_surface_match.py）守的是"前端调的接口后端都有"。
这里守反向：**后端新增了路由，前端却从不调用**。

为什么需要：这类路由会制造"功能都有"的错觉。实测 `/llm/complete` 挂了很久没人发现
（真正在用的是 `/inline-completion`）；`/workflows/runs/{id}/pause|cancel|resume`
后端做得很好，前端却没有入口。

规则：
- 新增未被引用的路由 → 失败（要么接上前端，要么写进 scripts/api_surface_baseline.json）；
- 基线里的路由已被前端引用或被删掉 → 失败（说明基线该更新了）。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from api_surface_scan import unreferenced_routes  # noqa: E402

BASELINE = os.path.join(ROOT, "scripts", "api_surface_baseline.json")


def _baseline_routes() -> dict[str, str]:
    with open(BASELINE, encoding="utf-8") as fh:
        return dict(json.load(fh)["routes"])


def test_no_new_unreferenced_routes():
    current = {f"{method} {route}" for method, route, _ in unreferenced_routes()}
    baseline = set(_baseline_routes())
    new = sorted(current - baseline)
    assert new == [], (
        "新增了前端从不引用的路由——请接上前端，或写进 scripts/api_surface_baseline.json "
        f"（并说明它为什么只对外部调用方开放）: {new}"
    )


def test_baseline_has_no_stale_entries():
    current = {f"{method} {route}" for method, route, _ in unreferenced_routes()}
    baseline = set(_baseline_routes())
    stale = sorted(baseline - current)
    assert stale == [], (
        "基线里的路由已经被前端引用或删除了——请更新 scripts/api_surface_baseline.json: "
        f"{stale}"
    )


def test_baseline_entries_have_reasons():
    """每条登记都要写明"为什么只对外部调用方开放"——否则登记就变成了垃圾桶。"""
    missing = sorted(k for k, v in _baseline_routes().items() if not str(v).strip())
    assert missing == [], (
        "以下基线条目没有写原因（AGENTS.md 规则 12：只对外部调用方开放的接口"
        f"要显式登记并注明原因）: {missing}"
    )
