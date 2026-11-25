"""tests/contract/test_api_surface_match.py —— 前端调用 ↔ 后端路由一致性

2026-09-15 新增。起因是三个「前端看着有功能、实际永远失败」的断链：

  · POST /api/v1/conversations/{task_id}/rate   后端从未实现（👍/👎 永远 404）
  · POST /api/v1/workspace/files/upload         后端从未实现（文件上传永远 404）
  · GET  /api/v1/workspace/git/branch           实际路由是 /api/v1/git/branches（状态栏分支恒为空）

三处前端都 catch 后只弹一句「失败」，用户无法分辨「接口不存在」还是「网络抖动」。
本测试把「前端调用的每个 /api 路径都必须有对应后端路由」钉死；接口改动时先红后绿。

例外必须写进 ALLOWED_MISSING 并注明原因——默认空集合，不要为了让它变绿而加例外。
"""
import os
import re

import pytest

_SRC = os.path.join(os.path.dirname(__file__), "..", "..")
_PY = os.path.join(_SRC, "src-python")
_FE = os.path.join(_SRC, "src-frontend", "src")

# 允许「前端调用但没有后端路由」的路径（保持为空；确有例外时写清原因）
ALLOWED_MISSING: set[str] = set()

_ROUTE_RE = re.compile(r"@(?:router|app)\.(get|post|put|delete|patch)\(\s*[\"\x27]([^\"\x27]*)[\"\x27]")
_PREFIX_RE = re.compile(r"APIRouter\(\s*prefix\s*=\s*[\"\x27]([^\"\x27]+)")
_CALL_RE = re.compile(r"(?:\bapi(?:<[^>]*>)?\(\s*|\bfetch\(\s*)[`\"\x27]?(/api/[^`\"\x27\s)]*)")
# 模板插值先收敛成 `${X}`：`${encodeURIComponent(row.id)}` 里的 `)` 会让上面的
# URL 捕获提前结束，把 `/cancel` 这类静态后缀整段丢掉——接口面就对不上了。
_TEMPLATE_RE = re.compile(r"\$\{[^{}]*\}")


def _normalize(path: str) -> str:
    shape = re.sub(r"\{[^}]+\}", "{}", path)
    return shape.rstrip("/") or "/"


def _backend_routes() -> set[str]:
    routes: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(_PY):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "python-embed")]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            with open(os.path.join(dirpath, name), encoding="utf-8", errors="ignore") as fh:
                src = fh.read()
            prefix_match = _PREFIX_RE.search(src)
            prefix = prefix_match.group(1) if prefix_match else ""
            for method, path in _ROUTE_RE.findall(src):
                full = _normalize(prefix + path)
                if full.startswith("/api"):
                    routes.add(full)
    return routes


def _frontend_calls() -> dict[str, set[str]]:
    calls: dict[str, set[str]] = {}
    for dirpath, dirnames, filenames in os.walk(_FE):
        dirnames[:] = [d for d in dirnames if d not in ("__tests__", "node_modules")]
        for name in filenames:
            if not name.endswith((".ts", ".tsx")):
                continue
            full = os.path.join(dirpath, name)
            with open(full, encoding="utf-8", errors="ignore") as fh:
                src = _TEMPLATE_RE.sub("${X}", fh.read())
            for match in _CALL_RE.finditer(src):
                raw = match.group(1).split("?")[0]
                # 模板插值 `${...}` → 单段通配，与后端 `{param}` 对齐
                shape = "/".join("{}" if "$" in seg else seg for seg in raw.split("/"))
                rel = os.path.relpath(full, _SRC).replace("\\", "/")
                calls.setdefault(shape, set()).add(rel)
    return calls


def _segment_matches(route_seg: str, call_seg: str) -> bool:
    if route_seg == call_seg:
        return True
    return route_seg == "{}" or call_seg == "{}"


def _has_route(routes: set[str], shape: str) -> bool:
    if shape in routes:
        return True
    call_parts = shape.split("/")
    for route in routes:
        route_parts = route.split("/")
        if len(route_parts) == len(call_parts) and all(
            _segment_matches(r, c) for r, c in zip(route_parts, call_parts)
        ):
            return True
    return False


def test_scanner_sees_both_sides():
    """扫描器自检——避免正则失效导致本文件「永远通过」。"""
    routes = _backend_routes()
    calls = _frontend_calls()
    assert len(routes) > 200, f"后端路由只扫到 {len(routes)} 条，正则可能失效"
    assert len(calls) > 100, f"前端调用只扫到 {len(calls)} 条，正则可能失效"


def test_every_frontend_call_has_backend_route():
    routes = _backend_routes()
    calls = _frontend_calls()
    missing = {
        shape: sorted(where)
        for shape, where in calls.items()
        if not _has_route(routes, shape) and shape not in ALLOWED_MISSING
    }
    detail = "\n  ".join(f"{shape}  ← {', '.join(where[:3])}" for shape, where in sorted(missing.items()))
    assert missing == {}, (
        "以下接口前端在调用、后端没有对应路由（点下去必然失败）：\n  " + detail
    )


def test_rate_and_upload_routes_exist():
    """回归锚定：三个曾经缺失的接口必须继续存在。"""
    routes = _backend_routes()
    for shape in (
        "/api/v1/conversations/{}/rate",
        "/api/v1/workspace/files/upload",
        "/api/v1/git/branches",
    ):
        assert _has_route(routes, shape), f"缺少路由 {shape}"


# ═══════════════════════════════════════════════════════════════
# WebSocket 路径一致性（2026-09-15 补充）
# ═══════════════════════════════════════════════════════════════
#
# 起因：终端 WS 后端挂在带 /api/v1 前缀的 router 上（/api/v1/ws/terminal），
# 前端连的是 /ws/terminal —— 握手被拒，集成终端从未连上，而 HTTP 路径扫描看不出来。
_WS_CLIENT_RE = re.compile(r"new WebSocket\(\s*`ws://[^`]*?(/ws[^`?]*)")


def _backend_ws_paths() -> set:
    paths = set()
    for dirpath, dirnames, filenames in os.walk(_PY):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "python-embed")]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            with open(os.path.join(dirpath, name), encoding="utf-8", errors="ignore") as fh:
                src = fh.read()
            prefix_match = _PREFIX_RE.search(src)
            prefix = prefix_match.group(1) if prefix_match else ""
            for path in re.findall(r"@(?:router|app)\.websocket\(\s*[\"\x27]([^\"\x27]*)[\"\x27]", src):
                paths.add(prefix + path)
            for path in re.findall(r"add_api_websocket_route\(\s*[\"\x27]([^\"\x27]*)", src):
                paths.add(path)
    return paths


def _frontend_ws_paths() -> dict:
    found = {}
    for dirpath, dirnames, filenames in os.walk(_FE):
        dirnames[:] = [d for d in dirnames if d not in ("__tests__", "node_modules")]
        for name in filenames:
            if not name.endswith((".ts", ".tsx")):
                continue
            full = os.path.join(dirpath, name)
            with open(full, encoding="utf-8", errors="ignore") as fh:
                src = fh.read()
            for path in _WS_CLIENT_RE.findall(src):
                found.setdefault(path, set()).add(os.path.relpath(full, _SRC).replace("\\\\", "/"))
    return found


def test_frontend_ws_paths_exist():
    """前端连接的每个 WebSocket 路径都必须有后端注册。"""
    backend = _backend_ws_paths()
    frontend = _frontend_ws_paths()
    assert frontend, "没有扫描到前端 WebSocket 路径，正则可能失效"
    missing = {p: sorted(v) for p, v in frontend.items() if p not in backend}
    detail = "\n  ".join(f"{p} ← {', '.join(v)}" for p, v in sorted(missing.items()))
    assert missing == {}, (
        "前端连接的 WebSocket 路径在后端不存在（握手会直接被拒）：\n  " + detail
        + f"\n后端已注册: {sorted(backend)}"
    )
