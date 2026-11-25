"""scripts/api_surface_scan.py —— 后端路由 → 前端引用 反向扫描

正向的"前端调的接口后端都有"由 tests/contract/test_api_surface_match.py 守住；
这里做反向：**后端有、前端（渲染进程 + Electron 主进程）从不引用**的路由。

这类路由的危害是"让人以为功能存在"——例如 `/llm/complete`（真正在用的是
`/inline-completion`）挂了很久没人发现。

用法：``python scripts/api_surface_scan.py`` 打印未被引用的路由。
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTES_DIR = os.path.join(ROOT, "src-python", "routes")
CLIENT_DIRS = (os.path.join(ROOT, "src-frontend", "src"), os.path.join(ROOT, "src-electron"))
CLIENT_EXT = (".ts", ".tsx", ".js", ".mjs")

# 末段允许的"动态"形态：JS 模板占位符 ${x} 或普通标识
_DYNAMIC_LAST = r"(?:\$\{[^}]{0,120}\}|[^/\"'\s]{1,120})"


def collect_routes() -> list[tuple[str, str, str]]:
    """返回 [(method, path, 文件)]。"""
    out: list[tuple[str, str, str]] = []
    for fn in sorted(os.listdir(ROUTES_DIR)):
        if not fn.endswith(".py"):
            continue
        src = open(os.path.join(ROUTES_DIR, fn), encoding="utf-8").read()
        m = re.search(r'APIRouter\(prefix="([^"]+)"', src)
        prefix = m.group(1) if m else ""
        for mm in re.finditer(r'@router\.(get|post|put|delete|patch)\("([^"]+)"', src):
            out.append((mm.group(1).upper(), prefix + mm.group(2), fn))
    return out


def client_blob() -> str:
    """前端 + Electron 主进程的全部源码文本。"""
    chunks: list[str] = []
    for base in CLIENT_DIRS:
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in filenames:
                if fn.endswith(CLIENT_EXT):
                    path = os.path.join(dirpath, fn)
                    try:
                        chunks.append(open(path, encoding="utf-8", errors="replace").read())
                    except OSError:
                        continue
    return "\n".join(chunks)


def route_pattern(route: str) -> re.Pattern:
    """把路由编译成"前端可能的调用形态"正则。

    - 中间段：静态段字面匹配，``{param}`` 段允许任意非斜杠内容；
    - 末段：字面量、JS 模板占位符或普通标识都算命中
      （前端常用 ``/agents/${id}/${kind}`` 这类拼接）。
    - 末段之后不能再接路径段：否则单段路由 ``/hooks/channel`` 会被
      ``/hooks/triggers/pending`` 这种更长路径"前缀命中"，
      把外部 webhook 误判成"前端已引用"（守卫因此变松）。
    """
    segs = [s for s in route.split("/") if s]
    parts: list[str] = []
    for i, seg in enumerate(segs):
        if seg.startswith("{"):
            parts.append(r"[^/]{0,120}")
        elif i == len(segs) - 1:
            parts.append("(?:" + re.escape(seg) + "|" + _DYNAMIC_LAST + ")")
        else:
            parts.append(re.escape(seg))
    # 末段后必须是收尾（引号/空白/查询串/括号）而不是又一段路径
    return re.compile("/" + "/".join(parts) + r"(?![\w/\-])")


def unreferenced_routes(blob: str | None = None) -> list[tuple[str, str, str]]:
    """返回前端从不引用的路由 [(method, path, 文件)]。"""
    text = client_blob() if blob is None else blob
    out: list[tuple[str, str, str]] = []
    for method, route, fn in collect_routes():
        if not route_pattern(route).search(text):
            out.append((method, route, fn))
    return out


if __name__ == "__main__":
    rows = unreferenced_routes()
    print(f"未被前端引用的路由：{len(rows)}")
    for method, route, fn in rows:
        print(f"  {method:6s} {route:56s} {fn}")
